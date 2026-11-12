"""Read-only feature re-planning stays bound to the original task and coder."""
from copy import deepcopy
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock

from tests import test_unattended_dashboard_workflow as fixtures
from unattended import dashboard, dashboard_evidence as evidence, dashboard_flows as flows
from unattended import dashboard_state


class DashboardReplanningTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.DashboardWorkflowTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.base = self.f.fixture
        self.task = self.f.task("routine")
        self.source = {"head_sha": "a" * 40, "scope_sha256": "b" * 64, "review_sha256": "c" * 64,
                       "check_results_sha256": "d" * 64, "evidence_files": {"review": "e" * 64}}
        self.task.update(body="Original feature scope", acceptance_criteria=["Original criterion"], core_tests=["OriginalTest"],
                         code_base="a" * 40, candidate={"head_sha": "a" * 40}, reworks=3,
                         session_id="550e8400-e29b-41d4-a716-446655440000", session_owner_task_id="TASK-1",
                         check_results=[{"id": "ue-behavior", "passed": True, "head_sha": "a" * 40}],
                         replanning={"round": 1, "status": "pending", "trigger": "review_limit", "source_phase": "REVIEW",
                                     "feedback": "The previous review still requires a reproducible edge-case check.",
                                     "fingerprint": self.source, "planning_model": "claude-opus-5-5", "planning_reasoning_effort": "high"})
        self.state = self.f.state
        self.run = self.base.runtime / "runs" / ("plan-" + "a" * 32); self.run.mkdir(parents=True)
        self.state.update(phase="REPLAN", expected_head="a" * 40, operation={"kind": "plan", "task_id": "TASK-1",
            "head_sha": "a" * 40, "output_dir": str(self.run), "started_at": "2026-11-02T11:00:00+00:00",
            "planning_provider": "claude", "planning_level": "critical", "planning_model": "claude-opus-5-5",
            "planning_reasoning_effort": "high"})
        self.branch = "550e8400-e29b-41d4-a716-446655440001"
        self.opening = {"type": "system", "subtype": "init", "session_id": self.branch, "model": "claude-opus-5-5"}
        (self.run / "stdout.log").write_text(json.dumps(self.opening) + "\n" + json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "PRIVATE_PLANNING_NATIVE_TEXT"},
            {"type": "tool_use", "name": "Read", "input": {"path": "PRIVATE_TOOL_ARGUMENT"}}]}}) + "\n")
        self.result = {"action": "task", "title": "Repair analysis title", "body": "Investigate the evidence and choose the next repair.",
                       "acceptance_criteria": ["Suggested validation step"], "reason": "Repair the existing task", "core_tests": ["SuggestedTest"]}
        self.base.write(self.run / "result.json", self.result)
        self.review = {"head_sha": "a" * 40, "decision": "rework", "summary": "A reproducible edge-case check is still missing.",
                       "feedback": "Full review feedback belongs in the evidence."}
        self.review_path = self.base.runtime / "tasks/TASK-1/review-3.json"
        self.base.write(self.review_path, self.review)
        self.source.update(review_round=3, review_sha256=hashlib.sha256(self.review_path.read_bytes()).hexdigest())
        self.save()

    def save(self):
        self.base.write(self.base.runtime / "state.json", self.state)

    def snapshot(self):
        value = dashboard_state.collect_snapshot(self.base.config_path)
        value["flows"] = flows.collect_flows(self.base.config_path, value)
        return value

    def node(self, value):
        return next(row for row in value["workflow"]["nodes"] if row["id"] == "critical_plan")

    def detail(self):
        return evidence.collect_node_detail(self.base.config_path, "development", "critical_plan", snapshot=self.snapshot())

    def read(self, ref):
        return evidence.read_node_evidence(self.base.config_path, "development", "critical_plan", ref)

    def test_replan_reuses_critical_node_and_real_fresh_opus_metadata_without_changing_the_task(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.base.root.rglob("*") if p.is_file()}
        value = self.snapshot(); node = self.node(value)
        self.assertIsNone(value["error"])
        self.assertEqual(value["status"]["code"], "replan")
        self.assertEqual(value["status"]["actor"], "Claude Code")
        self.assertEqual(value["workflow"]["current"], "critical_plan")
        self.assertEqual(node["state"], "running")
        self.assertIn("重新分析", node["title"])
        self.assertEqual((node["model"], node["effort"], node["model_source"]), ("claude-opus-5-5", "high", "recorded"))
        self.assertIn("edge-case", node["summary"])
        self.assertEqual(value["task"]["session_id"], self.task["session_id"])
        self.assertEqual(value["task"]["acceptance_criteria"], ["Original criterion"])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.base.root.rglob("*") if p.is_file()})

    def test_prior_checks_are_not_current_replan_passes_and_review_loop_goes_through_planning(self):
        value = self.snapshot(); rows = {row["id"]: row for row in value["workflow"]["nodes"]}
        self.assertEqual(value["check_progress"]["evidence_scope"], "previous_attempt")
        self.assertNotEqual(rows["check"]["state"], "completed")
        self.assertNotEqual(rows["code"]["state"], "completed")
        self.assertTrue(any(edge["active"] and edge["from"] == "review" and edge["to"] == "critical_plan" for edge in value["workflow"]["edges"]))

    def test_requested_model_without_a_native_opening_is_not_recorded_or_running(self):
        (self.run / "stdout.log").write_text('{"type":"assistant","text":"not an opening"}\n')
        node = self.node(self.snapshot())
        self.assertIsNone(node["model"])
        self.assertNotEqual(node["model_source"], "recorded")
        self.assertNotEqual(node["state"], "running")

    def test_finished_and_foreign_operations_do_not_claim_current_replanning_execution(self):
        self.base.write(self.run / "process.json", {"kind": "plan", "exit_code": 0, "model_selection": {
            "model_verified": True, "observed_model": "claude-opus-5-5", "requested_reasoning_effort": "high"}})
        self.assertNotEqual(self.node(self.snapshot())["state"], "running")
        (self.run / "process.json").unlink()
        for field, value in (("task_id", "OTHER"), ("head_sha", "b" * 40)):
            previous = self.state["operation"][field]
            self.state["operation"][field] = value; self.save()
            self.assertNotEqual(self.node(self.snapshot())["state"], "running")
            self.state["operation"][field] = previous
        self.save()

    def test_quota_pause_and_stopped_worker_keep_the_critical_stage_without_claiming_execution(self):
        self.state["provider_retry"] = {"phase": "REPLAN", "task_id": "TASK-1", "head_sha": "a" * 40,
            "attempt": 2, "reason": "claude_subscription_limit", "next_retry_at": "2026-11-02T11:10:00+00:00", "status": "waiting"}
        self.save(); value = self.snapshot()
        self.assertEqual(value["provider_retry"]["provider"], "Claude Code")
        self.assertEqual(self.node(value)["state"], "waiting")
        self.state.pop("provider_retry"); self.save()
        pause = self.base.runtime / "PAUSE"; pause.write_text("user pause")
        self.assertEqual(self.node(self.snapshot())["state"], "paused")
        pause.unlink(); self.base.inspector.side_effect = OSError("stopped")
        value = self.snapshot(); self.assertEqual(value["status"]["code"], "stopped")
        self.assertEqual(self.node(value)["state"], "waiting")
        self.assertIn("edge-case", self.node(value)["summary"])

    def test_scoped_logs_and_repair_plan_never_expose_native_chat_or_replace_original_criteria(self):
        value = self.detail()
        self.assertIn("重新分析", value["title"])
        logs = b"\n".join(self.read(row["id"])[0] for row in value["logs"])
        self.assertIn("读取文件", logs.decode("utf-8"))
        self.assertNotIn(b"PRIVATE_PLANNING_NATIVE_TEXT", logs)
        self.assertNotIn(b"PRIVATE_TOOL_ARGUMENT", logs)
        plans = [self.read(row["id"])[0] for row in value["artifacts"]]
        self.assertTrue(any(self.result["body"].encode() in body for body in plans))
        self.assertTrue(any("不替换原任务".encode() in body for body in plans))
        self.assertEqual(self.state["task"]["core_tests"], ["OriginalTest"])
        self.assertNotIn(self.branch, json.dumps(value))

    def test_task_head_operation_or_fingerprint_changes_invalidate_previous_evidence(self):
        baseline = deepcopy(self.state)
        for variant in ("task", "head", "operation", "fingerprint"):
            self.state = deepcopy(baseline); self.save(); reference = self.detail()["logs"][0]["id"]
            if variant == "task": self.state["task"]["id"] = self.state["operation"]["task_id"] = "TASK-OTHER"
            elif variant == "head":
                self.state["task"]["candidate"]["head_sha"] = self.state["task"]["code_base"] = self.state["operation"]["head_sha"] = "b" * 40
            elif variant == "operation": self.state["operation"]["output_dir"] = str(self.base.runtime / "runs" / ("plan-" + "b" * 32))
            else: self.state["task"]["replanning"]["fingerprint"]["review_sha256"] = "f" * 64
            self.save()
            with self.assertRaises(ValueError): self.read(reference)

    def test_history_remains_a_discoverable_repair_plan_after_code_resumes(self):
        path = self.base.runtime / "tasks/TASK-1/replan-1.json"
        receipt = {"round": 1, "task_id": "TASK-1", "head_sha": "a" * 40, "source_fingerprint": self.source,
            "output_dir": str(self.run), "result": self.result, "planning_model": "claude-opus-5-5",
            "planning_reasoning_effort": "high", "execution_decision": "continue", "controller_reason": None,
            "at": self.base.now.isoformat(), "receipt_path": str(path)}
        self.base.write(path, receipt)
        self.state["task"]["replanning_history"] = [receipt]
        self.state["task"]["replanning"]["status"] = "applied"
        self.state["phase"] = "CODE"; self.state["operation"] = None; self.save()
        value = self.snapshot(); node = self.node(value)
        self.assertEqual(node["state"], "completed")
        self.assertIn("重新分析", node["title"])
        self.assertEqual(node["model"], "claude-opus-5-5")
        detail = self.detail(); plans = [row for row in detail["artifacts"] if "重新分析计划" in row["title"]]
        self.assertEqual(len(plans), 1)
        self.assertIn(self.result["body"].encode(), self.read(plans[0]["id"])[0])
        self.assertEqual(self.snapshot()["task"]["session_id"], self.task["session_id"])

    def test_summaries_use_the_bound_short_review_reason_and_keep_full_feedback_in_the_artifact(self):
        full = "C:/workspace/source.cpp assertion details and repair instructions.\n" * 30 + "FULL_FEEDBACK_TAIL"
        self.task["replanning"]["feedback"] = full; self.save()
        value = self.snapshot(); detail = self.detail()
        summaries = [value["status"]["summary"], self.node(value)["summary"], value["workflow"]["summary"],
                     value["flows"]["development"]["summary"], detail["summary"]]
        for summary in summaries:
            self.assertLessEqual(len(summary), 160)
            self.assertIn(self.review["summary"], summary)
            self.assertNotIn("source.cpp", summary)
            self.assertNotIn("FULL_FEEDBACK_TAIL", summary)
        artifact = next(row for row in detail["artifacts"] if "触发原因" in row["title"])
        public = json.loads(self.read(artifact["id"])[0])
        self.assertEqual(public["feedback"], full)

    def test_changed_review_cannot_supply_a_short_reason_and_large_review_summaries_remain_bounded(self):
        self.review["summary"] = "FOREIGN_REVIEW_SHORT_REASON"
        self.base.write(self.review_path, self.review)
        value = self.snapshot()
        self.assertNotIn("FOREIGN_REVIEW_SHORT_REASON", value["status"]["summary"])
        self.assertIn("上轮审核仍有未解决问题", value["status"]["summary"])
        self.review["summary"] = "A very long review summary. " * 100
        self.base.write(self.review_path, self.review)
        self.source["review_sha256"] = hashlib.sha256(self.review_path.read_bytes()).hexdigest(); self.save()
        value = self.snapshot()
        self.assertLessEqual(len(value["status"]["summary"]), 160)
        self.assertLessEqual(len(self.node(value)["summary"]), 160)

    def test_code_timeout_replan_uses_current_wip_and_labels_prior_review_as_historical(self):
        self.task["reworks"] = 5
        self.task["candidate"] = {"head_sha": "c" * 40}
        self.task["replanning"]["source_phase"] = "CODE"
        self.source.update(head_sha="b" * 40, review_head_sha="a" * 40, wip={"changed": ["source.cpp"]}, wip_sha256="f" * 64)
        self.state["operation"]["head_sha"] = "b" * 40
        self.save()
        value = self.snapshot()
        self.assertEqual(self.node(value)["state"], "running")
        self.assertIn("历史审核提示", value["status"]["summary"])
        self.assertLessEqual(len(value["status"]["summary"]), 160)
        detail = self.detail()
        historical = next(row for row in detail["artifacts"] if "历史审核回执" in row["title"])
        review = json.loads(self.read(historical["id"])[0])
        self.assertEqual(review["head_sha"], "a" * 40)
        self.assertIn("当前工作区改动仍需独立检查与审核", review["scope"])
        trigger = next(row for row in detail["artifacts"] if "触发原因" in row["title"])
        current = json.loads(self.read(trigger["id"])[0])
        self.assertEqual(current["head_sha"], "b" * 40)
        self.assertEqual(current["source_phase"], "CODE")
        self.assertEqual(current["wip_sha256"], "f" * 64)
        self.assertEqual(value["task"]["reworks"], 5)
        self.assertEqual(value["task"]["session_id"], self.task["session_id"])


@unittest.skipUnless(os.environ.get("UNATTENDED_TEST_BROWSER") == "1", "opt-in local headless browser")
class ReplanningBrowserTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright
        self.f = DashboardReplanningTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.cache = Mock(); self.cache.get.side_effect = self.f.snapshot
        self.cache.flow_detail.side_effect = lambda lane, node: evidence.collect_node_detail(self.f.base.config_path, lane, node, snapshot=self.f.snapshot())
        self.cache.flow_evidence.side_effect = lambda lane, node, ref: evidence.read_node_evidence(self.f.base.config_path, lane, node, ref)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(self.cache, {}))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.pw = sync_playwright().start(); self.browser = self.pw.chromium.launch(channel="chrome", headless=True)
        self.page = self.browser.new_page(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
        self.errors = []; self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.addCleanup(self.close)
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/#development")
        self.page.locator("#dev-stage-name").get_by_text("Claude重新分析修复计划", exact=True).wait_for()

    def close(self):
        self.browser.close(); self.pw.stop(); self.server.shutdown(); self.server.server_close(); self.thread.join(2)
        self.assertFalse(self.errors, self.errors); self.cache.resolve_art.assert_not_called()

    def test_replan_stage_model_and_safe_logs_are_accessible_on_mobile(self):
        from playwright.sync_api import expect
        expect(self.page.locator("#dev-model")).to_contain_text("Opus 5.5")
        expect(self.page.locator("#dev-model")).to_contain_text("high")
        self.page.locator('.links a[href="#flows"]').click()
        self.page.locator('[data-flow-lane="development"][data-flow-node="critical_plan"]').click()
        dialog = self.page.locator("#flow-detail-dialog")
        expect(self.page.locator("#flow-detail-title")).to_contain_text("重新分析")
        dialog.get_by_role("button", name="当前 Claude 只读修复规划 · 事件摘要", exact=False).click()
        expect(self.page.locator("#flow-evidence-text")).to_contain_text("读取文件")
        self.assertNotIn("PRIVATE_PLANNING_NATIVE_TEXT", self.page.locator("#flow-evidence-text").inner_text())
        dialog.get_by_role("button", name="本轮重新分析修复计划", exact=False).click()
        expect(self.page.locator("#flow-evidence-text")).to_contain_text(self.f.result["body"])
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 390)


if __name__ == "__main__":
    unittest.main()
