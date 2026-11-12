"""Explicit repair authority is task scoped; all browser requests are read only."""
from copy import deepcopy
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import Mock

from tests.test_unattended_dashboard_current_attempt import CurrentCodeAttemptTests
from unattended import dashboard, dashboard_evidence, notifications


class UserRepairDashboardTests(unittest.TestCase):
    def setUp(self):
        self.f = CurrentCodeAttemptTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.base, self.task, self.state = self.f.base, self.f.task, self.f.state
        self.identity = "d" * 32
        self.root = self.base.runtime / "user-repairs" / self.identity
        plan = self.root / "native/plan/answer.json"
        self.base.write(plan, {"session_id": self.task["session_id"], "answer": "Keep every original criterion. Repair the actual failures."})
        self.request = {"version": 1, "id": self.identity, "task_id": self.task["id"],
            "model": "claude-opus-5-5", "reasoning_effort": "high", "source_head": "a" * 40,
            "source_full_flow_run": str(self.base.runtime / "full-flow/runs" / ("e" * 32)),
            "target_session_id": self.task["session_id"], "user_instruction": "Handle all problems found in the full-flow test.",
            "rework_window": 3, "max_cycles": 3, "plan_path": str(plan), "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest()}
        self.bind()
        self.task["reworks"] = 10
        self.state["last_error"] = None
        self.state["operation"].update(coding_model="claude-opus-5-5", coding_reasoning_effort="high", user_repair_id=self.identity,
            user_repair_request_sha256=self.task["user_repair"]["request_sha256"])
        self.f.opening["model"] = "claude-opus-5-5"
        (self.f.run / "stdout.log").write_text(json.dumps(self.f.opening) + "\n", encoding="utf-8")
        self.f.save()

    def bind(self):
        path = self.root / "request.json"; self.base.write(path, self.request)
        self.task["user_repair"] = {key: deepcopy(self.request[key]) for key in
            ("version", "id", "model", "reasoning_effort", "source_head", "source_full_flow_run", "plan_path", "plan_sha256", "rework_window", "max_cycles")}
        self.task["user_repair"].update(request_path=str(path), request_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            status="active", epoch=self.identity, activation_rework=10)

    def snapshot(self):
        return self.f.snapshot()

    def detail(self, lane="development", node="code"):
        return dashboard_evidence.collect_node_detail(self.base.config_path, lane, node)

    def test_actual_model_and_effort_are_task_scoped_and_original_history_and_config_survive(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.base.root.rglob("*") if p.is_file()}
        value = self.snapshot(); code = next(row for row in value["workflow"]["nodes"] if row["id"] == "code")
        self.assertEqual((code["title"], code["model"], code["effort"], code["model_source"], code["state"]),
                         ("Opus修复全流程问题", "claude-opus-5-5", "high", "recorded", "running"))
        self.assertEqual(value["task"]["reworks"], 10)
        self.assertEqual(value["task"]["user_repair"]["source_full_flow_run"], "e" * 32)
        self.assertEqual(value["models"]["claude"]["model"], "claude-sonnet-5-5")
        self.assertEqual(value["check_progress"]["total"], len(self.f.f.config["checks"]) + 1)
        card = notifications.build_card(value, "window_start")
        self.assertEqual(card["card"]["header"]["template"], "blue")
        self.assertIn("Opus修复全流程问题", json.dumps(card, ensure_ascii=False))
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.base.root.rglob("*") if p.is_file()})
        self.task.pop("user_repair"); self.state.update(phase="PLAN", task=None, operation=None); self.f.save()
        self.assertNotIn("user_repair", self.snapshot().get("task") or {})

    def test_authorization_and_plan_use_safe_evidence_without_native_session_or_extra_actions(self):
        detail = self.detail()
        self.assertEqual(detail["title"], "Opus修复全流程问题")
        texts = []
        for row in detail["artifacts"]:
            if row["title"].startswith(("本批用户修复授权", "本批修复计划")):
                body, kind, _ = dashboard_evidence.read_node_evidence(self.base.config_path, "development", "code", row["id"])
                texts.append(body.decode())
                self.assertTrue(kind.startswith("text/plain"))
        combined = "\n".join(texts)
        self.assertIn(self.request["user_instruction"], combined)
        self.assertIn("Repair the actual failures", combined)
        self.assertNotIn(self.task["session_id"], combined)
        self.assertNotIn("accepted", combined)

    def test_foreign_task_or_changed_authorization_and_plan_are_not_projected_as_repair(self):
        original = deepcopy(self.request)
        for variant in ("task", "source", "plan", "hash"):
            with self.subTest(variant=variant):
                self.request = deepcopy(original)
                if variant == "task": self.request["task_id"] = "OTHER"
                elif variant == "source": self.request["source_full_flow_run"] = str(self.base.root)
                elif variant == "plan": self.request["plan_sha256"] = "f" * 64
                self.bind()
                if variant == "hash": self.task["user_repair"]["request_sha256"] = "f" * 64
                self.f.save(); value = self.snapshot()
                self.assertNotIn("user_repair", value["task"])
                self.assertIn("待核实", value["task"]["user_repair_error"])

    def test_foreign_repair_operation_cannot_borrow_model_or_logs_and_check_stage_keeps_extra_gate(self):
        self.state["operation"]["user_repair_id"] = "f" * 32; self.f.save()
        value = self.snapshot(); code = next(row for row in value["workflow"]["nodes"] if row["id"] == "code")
        self.assertNotEqual(code["model_source"], "recorded")
        self.assertNotEqual(code["state"], "running")
        with self.assertRaisesRegex(ValueError, "不属于本批"): self.detail()
        self.state.update(phase="CHECK", operation={"kind": "check", "task_id": self.task["id"], "head_sha": "a" * 40,
            "output_dir": str(self.f.run), "started_at": self.base.now.isoformat()})
        self.task["candidate"] = {"head_sha": "a" * 40}
        self.task["check_results"] = [{"id": row["id"], "head_sha": "a" * 40, "passed": True} for row in self.f.f.config["checks"]]
        self.f.save(); value = self.snapshot()
        self.assertEqual(value["check_progress"]["current_id"], "ue-full-flow-repair")
        self.assertNotEqual(next(row for row in value["workflow"]["nodes"] if row["id"] == "check")["state"], "completed")


@unittest.skipUnless(os.environ.get("UNATTENDED_TEST_BROWSER") == "1", "opt-in isolated headless browser")
class UserRepairBrowserTests(unittest.TestCase):
    def test_repair_is_distinct_from_ended_test_and_preserves_read_only_mobile_dialog(self):
        from playwright.sync_api import expect, sync_playwright
        fixture = UserRepairDashboardTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        def snapshot():
            value = fixture.snapshot()
            value["full_flow_test"] = {"id": "e" * 32, "task_id": fixture.task["id"], "head_sha": "a" * 40,
                "title": "Opus全流程测试", "status": "completed", "status_label": "本次测试已结束", "stage": "analysis",
                "stage_label": "结果分析", "summary": "本轮未通过，失败记录保留。", "model": "claude-opus-5-5", "effort": "high", "model_source": "recorded",
                "detail_url": "/api/flow/detail?lane=development&node=full_flow_test"}
            return value
        cache = Mock(); cache.get.side_effect = snapshot
        cache.flow_detail.side_effect = fixture.detail
        cache.flow_evidence.side_effect = lambda lane, node, ref: dashboard_evidence.read_node_evidence(fixture.base.config_path, lane, node, ref)
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(cache, {}))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="chrome", headless=True)
                page = browser.new_page(viewport={"width": 390, "height": 844}); writes, errors = [], []
                def read_only(route):
                    if route.request.method in {"GET", "HEAD"}: route.continue_()
                    else: writes.append(route.request.method); route.abort()
                page.route("**/*", read_only); page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/#development")
                expect(page.locator("#user-repair-title")).to_have_text("Opus修复全流程问题")
                expect(page.locator("#user-repair-model")).to_contain_text("high")
                expect(page.locator("#dev-model")).to_contain_text("Opus")
                expect(page.locator("#full-flow-title")).to_contain_text("历史测试依据")
                expect(page.locator("#full-flow-status")).to_have_text("本次测试已结束")
                page.locator("#user-repair-detail").click()
                expect(page.locator("#flow-detail-title")).to_have_text("Opus修复全流程问题")
                page.locator("#flow-detail-artifacts button", has_text="本批修复计划与报告").click()
                expect(page.locator("#flow-evidence-text")).to_contain_text("Repair the actual failures")
                page.keyboard.press("Escape")
                expect(page.locator("#user-repair-detail")).to_be_focused()
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
                self.assertFalse(writes); self.assertFalse(errors); cache.resolve_art.assert_not_called()
                browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


if __name__ == "__main__":
    unittest.main()
