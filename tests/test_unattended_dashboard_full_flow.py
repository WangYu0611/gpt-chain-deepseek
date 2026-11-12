"""An isolated Opus test run has its own strict scope and never replaces main state."""
import base64
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
from unattended import dashboard, dashboard_evidence as evidence, dashboard_state


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lS8AAAAASUVORK5CYII=")


class FullFlowDashboardTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.DashboardWorkflowTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.base = self.f.fixture; self.state = self.f.state; self.task = self.f.task()
        self.task.update(code_base="a" * 40, candidate={"head_sha": "a" * 40})
        self.state.update(phase="BLOCKED", blocked_from="REVIEW", operation=None, expected_head="a" * 40,
                          last_error="Main task remains blocked pending its own decision.")
        self.base.write(self.base.runtime / "state.json", self.state)
        self.identity = "b" * 32
        self.run = self.base.runtime / "full-flow/runs" / self.identity
        self.native = self.run / "native/plan"; self.native.mkdir(parents=True)
        self.project = self.run / "project"; self.project.mkdir()
        self.source = self.base.runtime / "workspace"
        self.base.write(self.source / ".git/unused.json", {})
        (self.source / ".git/HEAD").write_text("ref: refs/heads/test\n")
        self.ref = self.source / ".git/refs/heads/test"; self.ref.parent.mkdir(parents=True)
        self.ref.write_text("a" * 40 + "\n")
        self.request = {"version": 1, "id": self.identity, "task_id": "TASK-1", "head_sha": "a" * 40,
            "source_workspace": str(self.source), "runtime_dir": str(self.base.runtime), "run_dir": str(self.run),
            "project_dir": str(self.project), "engine_dir": str(self.base.root / "engine"),
            "authorized_at": self.base.now.isoformat(), "model": "claude-opus-5-5", "effort": "high"}
        self.status = {"version": 1, "id": self.identity, "task_id": "TASK-1", "head_sha": "a" * 40,
            "status": "planning", "stage": "plan", "updated_at": self.base.now.isoformat(), "model": "claude-opus-5-5",
            "effort": "high", "summary": "Opus independently plans the full test.", "output_dir": str(self.native),
            "report_path": str(self.run / "report.md")}
        (self.run / "report.md").write_text("# Isolated full-flow report\nNo asset acceptance is performed.\n")
        (self.native / "stdout.log").write_text(json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5-5",
            "session_id": "550e8400-e29b-41d4-a716-446655440001"}) + "\n" + json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "PRIVATE_NATIVE_PROMPT_AND_CHAT"},
                {"type": "tool_use", "name": "Read", "input": {"path": "PRIVATE_NATIVE_TOOL_INPUT"}}]}}) + "\n")
        (self.native / "stderr.log").write_text("PRIVATE_NATIVE_PROMPT_LITERAL\n" + json.dumps({"type": "assistant", "message": {
            "content": [{"type": "text", "text": "PRIVATE_NATIVE_ERROR_CHAT"}]}}) + "\n")
        self.bind()

    def bind(self):
        unsigned = {key: value for key, value in self.request.items() if key != "request_sha256"}
        self.request["request_sha256"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        self.base.write(self.run / "request.json", self.request)
        self.pointer = {"version": 1, "id": self.identity, "request_path": "full-flow/runs/" + self.identity + "/request.json",
                        "request_sha256": self.request["request_sha256"],
                        "request_file_sha256": hashlib.sha256((self.run / "request.json").read_bytes()).hexdigest()}
        self.base.write(self.base.runtime / "full-flow/current.json", self.pointer)
        self.status["request_sha256"] = self.request["request_sha256"]
        self.base.write(self.run / "status.json", self.status)

    def snapshot(self):
        return dashboard_state.collect_snapshot(self.base.config_path)

    def detail(self):
        return evidence.collect_node_detail(self.base.config_path, "development", "full_flow_test")

    def read(self, ref):
        return evidence.read_node_evidence(self.base.config_path, "development", "full_flow_test", ref)

    def test_bound_opus_run_is_visible_while_main_block_and_files_remain_unchanged(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.base.root.rglob("*") if p.is_file()}
        value = self.snapshot(); test = value["full_flow_test"]
        self.assertEqual(value["status"]["code"], "blocked")
        self.assertEqual(test["stage"], "plan")
        self.assertEqual((test["model"], test["effort"], test["model_source"]), ("claude-opus-5-5", "high", "recorded"))
        self.assertFalse(test["historical"])
        self.assertNotIn(str(self.run), json.dumps(test))
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.base.root.rglob("*") if p.is_file()})

    def test_future_main_head_marks_the_same_test_as_historical_without_overwriting_main(self):
        self.ref.write_text("c" * 40 + "\n")
        value = self.snapshot()
        self.assertTrue(value["full_flow_test"]["historical"])
        self.assertEqual(value["full_flow_test"]["head_sha"], "a" * 40)
        self.assertEqual(value["status"]["code"], "blocked")
        self.assertIn("历史版本", self.detail()["scope_label"])

    def accepted_repair(self):
        head, task_id = "c" * 40, self.task["id"]
        authorization = {"version": 1, "id": "d" * 32, "task_id": task_id, "model": "claude-opus-5-5",
                         "reasoning_effort": "high", "source_head": self.request["head_sha"],
                         "source_full_flow_run": str(self.run), "target_session_id": "550e8400-e29b-41d4-a716-446655440001"}
        request_path = self.base.runtime / "user-repairs" / authorization["id"] / "request.json"
        self.base.write(request_path, authorization)
        profile = {**authorization, "status": "active", "request_path": str(request_path),
                   "request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest()}
        directory = self.base.runtime / "tasks" / task_id
        check_output = self.base.runtime / "runs" / ("check-" + "e" * 32)
        task = {**self.task, "reworks": 2, "candidate": {"head_sha": head}, "user_repair": profile,
                "coding_model": "claude-opus-5-5", "coding_reasoning_effort": "high", "session_id": authorization["target_session_id"],
                "check_results": [{"id": "ue-editor-build", "head_sha": head, "passed": True},
                                  {"id": "ue-full-flow-repair", "head_sha": head, "passed": True, "stdout_path": str(check_output / "stdout.log")}]}
        published = {"id": task_id, "title": task["title"], "head_sha": head, "base_sha": "a" * 40,
                     "accepted_at": self.base.now.isoformat(), "publication": {"local_only": True}}
        self.base.write(directory / "completed-task.json", task)
        self.base.write(directory / "accepted.json", published)
        self.base.write(directory / "review-2.json", {"head_sha": head, "decision": "accepted"})
        receipt = {"version": 1, "id": "f" * 32, "task_id": task_id, "authorization_id": profile["id"],
                   "request_sha256": profile["request_sha256"], "head_sha": head, "scope": "user_repair_check", "status": "passed",
                   "passed": True, "finished_at": published["accepted_at"],
                   "operation": {"kind": "check", "task_id": task_id, "head_sha": head,
                                 "check_id": "ue-full-flow-repair", "output_dir": str(check_output)}}
        receipt_path = directory / "full-repair-checks" / receipt["id"] / "receipt.json"
        self.base.write(receipt_path, receipt)
        self.state.update(phase="PLAN", task=None, accepted=[{**published, "milestone_publication": {"id": "M3"}}])
        self.base.write(self.base.runtime / "state.json", self.state)
        self.status.update(status="completed", stage="analysis", summary="Original isolated test failed.")
        self.bind()
        self.ref.write_text("7" * 40 + "\n")
        return directory, task, published, receipt_path, receipt

    def test_historical_test_links_only_its_published_repair_without_current_workspace_evidence(self):
        _, _, published, _, _ = self.accepted_repair()
        value = self.snapshot(); run = value["full_flow_test"]
        self.assertIsNone(value["task"])
        self.assertTrue(run["historical"])
        self.assertEqual(run["summary"], "Original isolated test failed.")
        self.assertEqual(run["accepted_repair"], {"task_id": published["id"], "head_sha": published["head_sha"],
            "accepted_at": published["accepted_at"], "model": "claude-opus-5-5", "effort": "high", "passed_checks": 2, "total_checks": 2})

    def test_unmatched_archives_do_not_convert_the_original_test_into_an_accepted_repair(self):
        for variant in ("state", "publication", "source", "candidate", "review", "failed_check", "receipt_scope", "receipt_operation", "receipt_auth"):
            with self.subTest(variant=variant):
                directory, task, published, path, receipt = self.accepted_repair()
                if variant == "state":
                    self.state["accepted"] = []
                    self.base.write(self.base.runtime / "state.json", self.state)
                elif variant == "publication":
                    self.base.write(directory / "accepted.json", {**published, "head_sha": "7" * 40})
                elif variant == "source":
                    task["user_repair"]["source_head"] = "7" * 40
                    self.base.write(directory / "completed-task.json", task)
                elif variant == "candidate":
                    self.base.write(directory / "completed-task.json", {**task, "candidate": {"head_sha": "7" * 40}})
                elif variant == "review":
                    self.base.write(directory / "review-2.json", {"head_sha": published["head_sha"], "decision": "rework"})
                else:
                    if variant == "failed_check": receipt.update(status="failed", passed=False)
                    elif variant == "receipt_scope": receipt["scope"] = "user_repair_selftest"
                    elif variant == "receipt_operation": receipt["operation"]["output_dir"] = str(self.base.runtime / "runs" / ("check-" + "7" * 32))
                    else: receipt["authorization_id"] = "7" * 32
                    self.base.write(path, receipt)
                self.assertNotIn("accepted_repair", self.snapshot()["full_flow_test"])

    def test_canonical_and_raw_digest_and_all_source_roots_are_independently_validated(self):
        baseline = deepcopy(self.request)
        for key, value in (("source_workspace", str(self.base.root)), ("runtime_dir", str(self.base.root)),
                           ("run_dir", str(self.base.root)), ("project_dir", str(self.base.root)), ("effort", "low")):
            with self.subTest(key=key):
                self.request = {**deepcopy(baseline), key: value}; self.bind()
                self.assertIsNone(self.snapshot()["full_flow_test"])
                with self.assertRaises(ValueError): self.detail()
        self.request = deepcopy(baseline); self.bind()
        self.pointer["request_file_sha256"] = "f" * 64
        self.base.write(self.base.runtime / "full-flow/current.json", self.pointer)
        with self.assertRaises(ValueError): self.detail()
        self.bind(); self.pointer["request_sha256"] = "f" * 64
        self.base.write(self.base.runtime / "full-flow/current.json", self.pointer)
        with self.assertRaises(ValueError): self.detail()

    def test_arbitrary_output_and_report_paths_or_status_identity_are_rejected(self):
        baseline = deepcopy(self.status)
        for key, value in (("output_dir", str(self.base.root)), ("report_path", str(self.base.root / "private.md")),
                           ("head_sha", "c" * 40), ("id", "c" * 32), ("task_id", "OTHER")):
            self.status = {**baseline, key: value}; self.base.write(self.run / "status.json", self.status)
            with self.subTest(key=key), self.assertRaises(ValueError): self.detail()

    def test_native_logs_are_labels_only_and_report_is_a_separate_public_work_product(self):
        detail = self.detail()
        body = b"\n".join(self.read(row["id"])[0] for row in detail["logs"])
        self.assertIn("读取文件", body.decode())
        self.assertNotIn(b"PRIVATE_NATIVE", body)
        self.assertNotIn(b"550e8400", body)
        report = next(row for row in detail["artifacts"] if row["title"] == "Opus全流程测试报告")
        self.assertIn(b"Isolated full-flow report", self.read(report["id"])[0])

    def checks(self):
        directory = self.run / "checks/build/attempt-1"; directory.mkdir(parents=True)
        log = directory / "stdout.log"; log.write_text("UE build output\n<script>literal only</script>\n")
        image = self.project / "Saved/Screenshots/test.png"; image.parent.mkdir(parents=True); image.write_bytes(PNG)
        external = self.base.root / "outside.png"; external.write_bytes(PNG)
        identities = {"version": 1, "id": self.identity, "task_id": "TASK-1", "head_sha": "a" * 40,
                      "request_sha256": self.request["request_sha256"]}
        receipt = {**identities, "stage": "build", "status": "passed", "passed": True, "evidence": [
            {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size}
            for path in (log, image, external)]}
        self.base.write(self.run / "checks/build/receipt.json", receipt)
        self.base.write(self.run / "facts.json", {**identities, "stage": "status", "status": "completed",
            "all_stages_finished": True, "passed": False, "failed_stages": ["package"], "stages": {"build": receipt}})
        return image

    def test_check_facts_logs_and_png_are_bound_and_completion_is_not_mislabeled_passed(self):
        image = self.checks(); detail = self.detail()
        self.assertTrue(any("UE/构建输出" in row["title"] for row in detail["logs"]))
        images = [row for row in detail["artifacts"] if row["kind"] == "png"]
        self.assertEqual(len(images), 1)
        self.assertEqual(self.read(images[0]["id"])[1], "image/png")
        facts = next(row for row in detail["artifacts"] if "事实汇总" in row["title"])
        self.assertFalse(json.loads(self.read(facts["id"])[0])["passed"])
        image.write_bytes(PNG + b"changed")
        with self.assertRaisesRegex(ValueError, "SHA"): self.read(images[0]["id"])
        self.status.update(status="completed", stage="analysis", output_dir=str(self.run / "native/analysis"))
        self.base.write(self.run / "status.json", self.status)
        self.assertEqual(self.snapshot()["full_flow_test"]["status_label"], "本次测试已结束")

    def test_request_change_invalidates_previous_opaque_evidence(self):
        reference = self.detail()["logs"][0]["id"]
        self.request["authorized_at"] = "2026-11-02T11:05:01+00:00"; self.bind()
        with self.assertRaisesRegex(ValueError, "范围已变化"): self.read(reference)

    def test_only_bounded_analysis_continuations_are_valid_and_original_logs_remain_visible(self):
        original_dir = self.run / "native/analysis"; original_dir.mkdir()
        original_log = original_dir / "stdout.log"
        original_log.write_text(json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5-5",
            "session_id": "550e8400-e29b-41d4-a716-446655440002"}) + "\n")
        original = original_log.read_bytes()
        old_reference = next(row["id"] for row in self.detail()["logs"] if row["title"] == "结果分析 · 原生事件摘要")
        for name in ("analysis-2", "analysis-9"):
            directory = self.run / "native" / name; directory.mkdir()
            (directory / "stdout.log").write_text(json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5-5",
                "session_id": "550e8400-e29b-41d4-a716-446655440002"}) + "\n" + json.dumps({"type": "assistant", "message": {
                    "content": [{"type": "text", "text": "PRIVATE_CONTINUATION_TEXT"}, {"type": "tool_use", "name": "Read", "input": {}}]}}) + "\n")
            self.status.update(status="analyzing", stage="analysis", output_dir=str(directory))
            self.base.write(self.run / "status.json", self.status)
            public = self.snapshot()["full_flow_test"]
            self.assertEqual(public["model_source"], "recorded")
            self.assertEqual(public["id"], self.identity)
            detail = self.detail()
            self.assertTrue(any("只读规划" in row["title"] for row in detail["logs"]))
            self.assertTrue(any(row["title"] == "结果分析 · 原生事件摘要" for row in detail["logs"]))
            self.assertIn("开始本轮执行", self.read(old_reference)[0].decode())
            continuation = [row for row in detail["logs"] if "第 " + name.split("-")[1] + " 次调用" in row["title"]]
            self.assertTrue(continuation)
            body = b"\n".join(self.read(row["id"])[0] for row in continuation)
            self.assertNotIn(b"PRIVATE_CONTINUATION_TEXT", body)
            self.assertIn("读取文件", body.decode())
        self.assertEqual(original_log.read_bytes(), original)
        for name in ("analysis-1", "analysis-10", "analysis-private"):
            self.status["output_dir"] = str(self.run / "native" / name)
            self.base.write(self.run / "status.json", self.status)
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.detail()

    def retest(self):
        self.checks()
        identities = {"version": 1, "id": self.identity, "task_id": "TASK-1", "head_sha": "a" * 40,
                      "request_sha256": self.request["request_sha256"]}
        initial = {**identities, "stage": "visual", "status": "failed", "passed": False,
                   "cases": [{"name": "Exact.RHI.Test", "status": "FAIL"}], "evidence": []}
        first = self.run / "checks/visual/receipt.json"; self.base.write(first, initial)
        directory = self.run / "checks/retest/attempt-1/case-001"; directory.mkdir(parents=True)
        (directory / "stdout.log").write_text("Exact RHI retest output\n")
        (directory / "stderr.log").write_text("")
        (directory / "Unreal.log").write_text("UE retest frame complete\n")
        self.base.write(directory / "process.json", {"exit_code": 0, "argv": ["hidden command arguments"], "prompt": "DO_NOT_PROJECT_PROMPT"})
        self.base.write(directory / "receipt.json", {"name": "Exact.RHI.Test", "status": "PASS", "passed": True})
        image = directory / "images/retest.png"; image.parent.mkdir(); image.write_bytes(PNG)
        forbidden = self.run / "native/test/raw-prompt.log"; forbidden.parent.mkdir(parents=True); forbidden.write_text("DO_NOT_SERVE_NATIVE_RAW")
        wrong_case = self.run / "checks/retest/attempt-1/test-001/stdout.log"; wrong_case.parent.mkdir(); wrong_case.write_text("NOT_WRAPPER_CASE")
        bad_image = self.run / "checks/retest/attempt-1/foreign/images/not-a-case.png"; bad_image.parent.mkdir(parents=True); bad_image.write_bytes(PNG)
        paths = [directory / name for name in ("stdout.log", "stderr.log", "Unreal.log", "process.json", "receipt.json")]
        receipt = {**identities, "stage": "retest", "status": "passed", "passed": True, "attempted_count": 1, "planned_count": 1,
            "no_failures": False, "newly_passed": ["Exact.RHI.Test"], "still_failed": [], "not_run": [], "first_pass_unchanged": True,
            "rechecked": [{"name": "Exact.RHI.Test", "source_stage": "visual", "mode": "RHI", "status": "PASS", "passed": True}],
            "evidence": [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size}
                         for path in paths + [image, forbidden, wrong_case, bad_image]]}
        self.base.write(self.run / "checks/retest/receipt.json", receipt)
        facts = json.loads((self.run / "facts.json").read_text())
        facts["stages"].update(visual=initial, retest=receipt)
        facts.update(passed=False, failed_stages=["visual", "package"])
        self.base.write(self.run / "facts.json", facts)
        self.status.update(status="testing", stage="retest"); self.base.write(self.run / "status.json", self.status)
        return first, first.read_bytes(), receipt

    def test_optional_retest_is_separate_and_only_bound_case_wrapper_outputs_are_exposed(self):
        first, original, receipt = self.retest()
        public = self.snapshot()["full_flow_test"]
        self.assertEqual(public["stage_label"], "结果分析")
        self.assertEqual(public["retest"]["attempted_count"], 1)
        self.assertIn("首轮失败记录保持独立", public["retest"]["summary"])
        detail = self.detail()
        raw = b"\n".join(self.read(row["id"])[0] for row in detail["logs"])
        self.assertIn(b"Exact RHI retest output", raw)
        self.assertIn(b"UE retest frame complete", raw)
        self.assertNotIn(b"DO_NOT_SERVE_NATIVE_RAW", raw)
        self.assertNotIn(b"NOT_WRAPPER_CASE", raw)
        process = next(row for row in detail["artifacts"] if "case-001" in row["title"] and "process.json" in row["title"])
        self.assertNotIn(b"DO_NOT_PROJECT_PROMPT", self.read(process["id"])[0])
        visual = next(row for row in detail["artifacts"] if row["title"] == "visual · 检查回执")
        self.assertFalse(json.loads(self.read(visual["id"])[0])["passed"])
        retest = next(row for row in detail["artifacts"] if row["title"] == "retest · 检查回执")
        self.assertTrue(json.loads(self.read(retest["id"])[0])["passed"])
        facts = next(row for row in detail["artifacts"] if "事实汇总" in row["title"])
        self.assertFalse(json.loads(self.read(facts["id"])[0])["passed"])
        png = [row for row in detail["artifacts"] if row["kind"] == "png"]
        self.assertEqual(len(png), 2)  # original project screenshot plus the allowed archived retest image.
        self.assertEqual(first.read_bytes(), original)

    def test_retest_with_no_initial_failures_does_not_claim_new_test_execution(self):
        _, _, receipt = self.retest()
        receipt.update(planned_count=0, attempted_count=0, no_failures=True, newly_passed=[], rechecked=[], evidence=[])
        self.base.write(self.run / "checks/retest/receipt.json", receipt)
        line = self.snapshot()["full_flow_test"]["retest"]["summary"]
        self.assertIn("未新增测试执行", line)
        receipt.update(status="failed", planned_count=1, attempted_count=0, no_failures=False, not_run=["Exact.RHI.Test"])
        self.base.write(self.run / "checks/retest/receipt.json", receipt)
        line = self.snapshot()["full_flow_test"]["retest"]["summary"]
        self.assertIn("已逐项尝试 0/1", line)
        self.assertIn("未运行 1", line)

    def test_visual_and_package_archived_pngs_require_the_fixed_manifest_namespaces(self):
        self.checks()
        identities = {"version": 1, "id": self.identity, "task_id": "TASK-1", "head_sha": "a" * 40,
                      "request_sha256": self.request["request_sha256"]}
        for stage, relative in (("visual", "attempt-1/case-022/images/frame.png"), ("package", "attempt-2/package-evidence/package.png")):
            path = self.run / "checks" / stage / relative; path.parent.mkdir(parents=True); path.write_bytes(PNG)
            self.base.write(self.run / "checks" / stage / "receipt.json", {**identities, "stage": stage, "status": "failed", "passed": False,
                "evidence": [{"path": str(path), "sha256": hashlib.sha256(PNG).hexdigest(), "size_bytes": len(PNG)}]})
        images = [row for row in self.detail()["artifacts"] if row["kind"] == "png"]
        self.assertEqual(len(images), 3)
        for row in images:
            self.assertEqual(self.read(row["id"])[1], "image/png")

    def test_notification_proof_needs_test_native_start_and_terminal_facts_report(self):
        self.assertNotIn("notification_proof", self.snapshot()["full_flow_test"])
        test = self.run / "native/test"; test.mkdir()
        (test / "stdout.log").write_text(json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5-5",
            "session_id": "550e8400-e29b-41d4-a716-446655440002"}) + "\n")
        self.status.update(status="testing", stage="regression", output_dir=str(test))
        self.base.write(self.run / "status.json", self.status)
        proof = self.snapshot()["full_flow_test"]["notification_proof"]
        self.assertTrue(proof["test_native_started"]); self.assertFalse(proof["terminal_evidence"])
        identities = {"version": 1, "id": self.identity, "task_id": "TASK-1", "head_sha": "a" * 40,
                      "request_sha256": self.request["request_sha256"]}
        for stage in ("prepare", "build", "regression", "visual", "package"):
            self.base.write(self.run / "checks" / stage / "receipt.json", {**identities, "stage": stage,
                "status": "failed" if stage == "visual" else "passed", "passed": stage != "visual", "finished_at": self.base.now.isoformat()})
        self.base.write(self.run / "facts.json", {**identities, "stage": "status", "status": "completed", "all_stages_finished": True,
            "passed": False, "failed_stages": ["visual"]})
        self.status.update(status="completed", stage="analysis", output_dir=str(self.run / "native/analysis"))
        self.base.write(self.run / "status.json", self.status)
        proof = self.snapshot()["full_flow_test"]["notification_proof"]
        self.assertTrue(proof["terminal_evidence"])
        self.assertEqual(proof["outcome"], "checks_failed_or_not_run")
        (self.run / "report.md").unlink()
        self.assertFalse(self.snapshot()["full_flow_test"]["notification_proof"]["terminal_evidence"])
        self.status["status"] = "failed"; self.base.write(self.run / "status.json", self.status)
        self.base.write(test / "process.json", {"exit_code": 1, "timed_out": True})
        proof = self.snapshot()["full_flow_test"]["notification_proof"]
        self.assertTrue(proof["terminal_evidence"])
        self.assertEqual(proof["outcome"], "execution_failed")

    def manifest_sha(self):
        path = self.run / "source-manifest.json"
        if not path.exists():
            self.base.write(path, {"version": 1, "id": self.identity, "head_sha": "a" * 40, "files": []})
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def short_package(self):
        if not (self.run / "checks/build/receipt.json").is_file():
            self.checks()
        common = {"version": 1, "id": self.identity, "task_id": "TASK-1", "head_sha": "a" * 40,
                  "request_sha256": self.request["request_sha256"], "source_manifest_sha256": self.manifest_sha()}
        original = self.run / "checks/package/receipt.json"
        self.base.write(original, {**common, "stage": "package", "status": "failed", "passed": False, "error": "UBT path exceeds 260 characters"})
        initial = original.read_bytes()
        attempt = self.run / "checks/package-short/attempt-1"
        rawlogs = attempt / "rawlogs"; rawlogs.mkdir(parents=True)
        paths = []
        for name in ("stdout.log", "stderr.log", "Unreal.log"):
            path = rawlogs / name; path.write_text("Formal short-path UE output\n"); paths.append(path)
        self.base.write(rawlogs / "process.json", {"exit_code": 0, "argv": ["do not publish"], "short_source_path": "C:/zg-flow/test/project"})
        paths.append(rawlogs / "process.json")
        for name in ("package-summary.json", "raw-package-summary.json", "raw-package-artifacts.json"):
            path = attempt / name; self.base.write(path, {"passed": True, "slot97": 97}); paths.append(path)
        for name in ("plain.log", "loop.log", "raw-package-uat.log"):
            path = attempt / name; path.write_text("Bound archived run output\n"); paths.append(path)
        slot = attempt / "loop-Slot97.zgs"; slot.write_bytes(b"ISOLATED_SLOT_97"); paths.append(slot)
        images = attempt / "package-evidence"; images.mkdir()
        for name in ("plain.png", "loop.png"):
            path = images / name; path.write_bytes(PNG); paths.append(path)
        outside = self.base.root / "short-copy/project/not-archived.log"; outside.parent.mkdir(parents=True)
        outside.write_text("DO_NOT_OPEN_SHORT_SOURCE")
        paths.append(outside)
        receipt = {**common, "stage": "package-short", "attempt": 1, "status": "passed", "passed": True,
            "short_source_path": "C:/zg-flow/test/project", "package_execution": {"save_header": "ISOLATED", "slot97": 97, "compared_steps": 5},
            "evidence": [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size} for path in paths]}
        self.base.write(self.run / "checks/package-short/receipt.json", receipt)
        self.base.write(attempt / "receipt.json", receipt)
        self.status.update(status="testing", stage="package-short"); self.base.write(self.run / "status.json", self.status)
        return original, initial, receipt

    def reconciled(self):
        original, initial, receipt = self.retest()
        common = {"id": self.identity, "task_id": "TASK-1", "head_sha": "a" * 40,
                  "request_sha256": self.request["request_sha256"], "source_manifest_sha256": self.manifest_sha()}
        case = self.run / "checks/visual/attempt-1/case-001/receipt.json"
        self.base.write(case, {"name": "Exact.RHI.Test", "mode": "normal", "status": "FAIL", "process": {"exit_code": 0}})
        ref = lambda path: {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size}
        item = {**common, "name": "Exact.RHI.Test", "mode": "normal", "source_stage": "visual", "case_index": 1,
            "status": "reconciled_tool_false_failure", "original_status": "FAIL", "execution": "not_reexecuted",
            "passed_after_parser_correction": True, "normal_rhi_proven": True, "reason": "Forced RHI parser corrected",
            "original_visual_receipt": ref(original), "original_case_receipt": ref(case)}
        derived = self.run / "checks/retest/attempt-1/reconciliations/case-001.json"
        self.base.write(derived, item)
        item["derived_receipt"] = ref(derived)
        receipt.update(planned_count=0, attempted_count=0, newly_passed=[], still_failed=[], not_run=[], rechecked=[], no_failures=False,
                       reconciled_tool_false_failure=[item], source_manifest_sha256=common["source_manifest_sha256"],
                       evidence=[item["original_visual_receipt"], item["original_case_receipt"], item["derived_receipt"]])
        self.base.write(self.run / "checks/retest/receipt.json", receipt)
        return original, initial, receipt, derived

    def test_package_short_is_separate_and_only_run_archives_are_exposed(self):
        original, initial, receipt = self.short_package()
        value = self.snapshot()["full_flow_test"]
        self.assertTrue(value["package_short"]["passed"])
        self.assertIn("原 package", value["package_short"]["summary"])
        detail = self.detail()
        first = next(row for row in detail["artifacts"] if row["title"] == "package · 检查回执")
        self.assertFalse(json.loads(self.read(first["id"])[0])["passed"])
        slot = next(row for row in detail["artifacts"] if row["kind"] == "binary")
        content, mime, filename = self.read(slot["id"])
        self.assertEqual((content, mime, filename), (b"ISOLATED_SLOT_97", "application/octet-stream", "loop-Slot97.zgs"))
        log = b"\n".join(self.read(row["id"])[0] for row in detail["logs"])
        self.assertIn(b"Formal short-path UE output", log)
        self.assertNotIn(b"DO_NOT_OPEN_SHORT_SOURCE", log)
        self.assertEqual(len([row for row in detail["artifacts"] if row["kind"] == "png"]), 3)
        self.assertEqual(original.read_bytes(), initial)
        receipt["source_manifest_sha256"] = "f" * 64
        self.base.write(self.run / "checks/package-short/receipt.json", receipt)
        self.assertNotIn("package_short", self.snapshot()["full_flow_test"])
        self.assertFalse(any(row["title"] == "package-short · 检查回执" for row in self.detail()["artifacts"]))

    def test_verified_parser_correction_is_not_a_real_reexecution_and_original_fail_is_unchanged(self):
        original, initial, receipt, derived = self.reconciled()
        retest = self.snapshot()["full_flow_test"]["retest"]
        self.assertEqual(retest["attempted_count"], 0)
        self.assertIn("本次通过 0", retest["summary"])
        self.assertEqual(retest["reconciled_tool_false_failure_count"], 1)
        self.assertIn("校验器误报已核实", retest["reconciliation_summary"])
        self.assertIn("未重新执行", retest["reconciliation_summary"])
        detail = self.detail()
        self.assertTrue(any("校验器误报校核" in row["title"] for row in detail["artifacts"]))
        self.assertEqual(original.read_bytes(), initial)
        value = json.loads(derived.read_bytes()); value["normal_rhi_proven"] = False; self.base.write(derived, value)
        self.assertNotIn("reconciled_tool_false_failure_count", self.snapshot()["full_flow_test"]["retest"])
        self.assertFalse(any("校验器误报校核" in row["title"] for row in self.detail()["artifacts"]))

    def running_stage(self, stage, **changes):
        row = {"version": 1, "id": self.identity, "task_id": "TASK-1", "head_sha": "a" * 40,
               "request_sha256": self.request["request_sha256"], "source_manifest_sha256": self.manifest_sha(),
               "stage": stage, "status": "running", "started_at": self.base.now.isoformat(), "updated_at": self.base.now.isoformat(),
               **({"attempt": 1} if stage == "package-short" else {}), **changes}
        self.base.write(self.run / "checks" / stage / "receipt.json", row)
        return row

    def test_actual_bound_running_stage_replaces_stale_prepare_without_changing_raw_status(self):
        self.status.update(status="testing", stage="prepare"); self.base.write(self.run / "status.json", self.status)
        self.running_stage("retest")
        original = (self.run / "status.json").read_bytes()
        public = self.snapshot()["full_flow_test"]
        self.assertEqual(public["status"], "testing")
        self.assertEqual(public["stage"], "retest")
        self.assertEqual(public["operator_stage"], "prepare")
        self.assertEqual(public["stage_source"], "receipt")
        self.assertEqual(public["stage_label"], "失败项逐项复测")
        self.assertIn("正在执行", public["summary"])
        self.assertEqual((self.run / "status.json").read_bytes(), original)

    def test_no_running_stage_falls_back_to_analysis_and_foreign_receipts_cannot_claim_activity(self):
        self.status.update(status="testing", stage="prepare"); self.base.write(self.run / "status.json", self.status)
        self.running_stage("retest", head_sha="c" * 40)
        public = self.snapshot()["full_flow_test"]
        self.assertEqual(public["stage"], "analysis")
        self.assertEqual(public["stage_source"], "fallback")
        self.assertIn("没有绑定阶段标记为运行中", public["summary"])
        self.running_stage("retest", source_manifest_sha256="f" * 64)
        self.assertEqual(self.snapshot()["full_flow_test"]["stage"], "analysis")

    def test_multiple_bound_running_stages_are_a_conflict_and_optional_package_short_is_supported(self):
        self.status.update(status="testing", stage="prepare"); self.base.write(self.run / "status.json", self.status)
        self.running_stage("package-short")
        self.assertEqual(self.snapshot()["full_flow_test"]["stage"], "package-short")
        self.running_stage("retest")
        public = self.snapshot()["full_flow_test"]
        self.assertEqual(public["stage"], "conflict")
        self.assertEqual(public["stage_conflicts"], ["retest", "package-short"])
        self.assertIn("冲突", public["stage_label"])
        self.assertIn("暂不判定", public["summary"])

    def running_before_manifest_is_saved(self, *, migrated=False):
        self.status.update(status="testing", stage="prepare"); self.base.write(self.run / "status.json", self.status)
        manifest = {"head_sha": self.request["head_sha"], "request_sha256": self.request["request_sha256"], "files": []}
        self.base.write(self.run / "source-manifest.json", manifest)
        binding = {key: self.request[key] for key in ("version", "id", "task_id", "head_sha", "request_sha256")}
        binding.update(request_file_sha256=self.pointer["request_file_sha256"], runner_sha256="1" * 64, retest_runner_sha256="2" * 64)
        self.base.write(self.run / "binding.json", binding)
        prepare = {**binding, "stage": "prepare", "status": "passed", "passed": True,
                   "finished_at": self.base.now.isoformat(), "source_manifest_sha256": self.manifest_sha()}
        self.base.write(self.run / "checks/prepare/receipt.json", prepare)
        row = {**binding, "stage": "retest", "status": "running", "passed": False, "attempt": 1,
               "started_at": self.base.now.isoformat(), "updated_at": self.base.now.isoformat(),
               "output_dir": str(self.run / "checks/retest/attempt-1")}
        if migrated:
            migration = {key: binding[key] for key in ("version", "id", "task_id", "head_sha", "request_sha256")}
            migration.update(binding_sha256=hashlib.sha256((self.run / "binding.json").read_bytes()).hexdigest(),
                from_runner_sha256=binding["runner_sha256"], from_retest_runner_sha256=binding["retest_runner_sha256"],
                to_runner_sha256="3" * 64, to_retest_runner_sha256="4" * 64,
                reason="recognize_forced_d3d12_rhi_and_reconcile_visual_parser_false_failures")
            path = self.run / "operator-runner-migration.json"; self.base.write(path, migration)
            row.update(runner_sha256=migration["to_runner_sha256"], retest_runner_sha256=migration["to_retest_runner_sha256"],
                operator_runner_migration={"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size_bytes": path.stat().st_size, "reason": migration["reason"], "original_binding_sha256": migration["binding_sha256"]})
        self.base.write(self.run / "checks/retest/receipt.json", row)
        return row, prepare

    def test_running_master_without_manifest_field_inherits_original_or_migrated_prepare_binding(self):
        for migrated in (False, True):
            with self.subTest(migrated=migrated):
                row, _ = self.running_before_manifest_is_saved(migrated=migrated)
                path = self.run / "checks/retest/receipt.json"; before = path.read_bytes()
                public = self.snapshot()["full_flow_test"]
                self.assertEqual((public["stage"], public["stage_source"]), ("retest", "receipt"))
                self.assertIn("正在执行", public["summary"])
                self.assertNotIn("source_manifest_sha256", row)
                self.assertFalse(row["passed"])
                self.assertEqual(path.read_bytes(), before)

    def test_missing_running_manifest_cannot_inherit_foreign_prepare_or_unbound_migration(self):
        mutations = ("prepare_head", "prepare_request_file", "prepare_manifest", "prepare_unfinished", "runner", "migration", "output")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                row, prepare = self.running_before_manifest_is_saved(migrated=True)
                if mutation == "prepare_head": prepare["head_sha"] = "f" * 40
                elif mutation == "prepare_request_file": prepare["request_file_sha256"] = "f" * 64
                elif mutation == "prepare_manifest": prepare["source_manifest_sha256"] = "f" * 64
                elif mutation == "prepare_unfinished": prepare["status"] = "running"
                elif mutation == "runner": row["runner_sha256"] = "f" * 64
                elif mutation == "migration": row["operator_runner_migration"]["sha256"] = "f" * 64
                elif mutation == "output": row["output_dir"] = str(self.base.root)
                self.base.write(self.run / "checks/prepare/receipt.json", prepare)
                self.base.write(self.run / "checks/retest/receipt.json", row)
                self.assertEqual(self.snapshot()["full_flow_test"]["stage_source"], "fallback")

    def test_existing_wrong_running_manifest_is_never_replaced_by_prepare_manifest(self):
        row, _ = self.running_before_manifest_is_saved(migrated=True)
        row["source_manifest_sha256"] = "f" * 64
        self.base.write(self.run / "checks/retest/receipt.json", row)
        self.assertEqual(self.snapshot()["full_flow_test"]["stage_source"], "fallback")


@unittest.skipUnless(os.environ.get("UNATTENDED_TEST_BROWSER") == "1", "opt-in isolated headless browser")
class FullFlowBrowserTests(unittest.TestCase):
    def test_card_is_visible_above_blocked_main_and_safe_evidence_opens_on_mobile(self):
        from playwright.sync_api import expect, sync_playwright
        f = FullFlowDashboardTests(); f.setUp(); self.addCleanup(f.doCleanups); f.reconciled(); f.short_package()
        cache = Mock(); cache.get.side_effect = f.snapshot
        cache.flow_detail.side_effect = lambda lane, node: f.detail()
        cache.flow_evidence.side_effect = lambda lane, node, ref: f.read(ref)
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(cache, {}))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="chrome", headless=True)
                page = browser.new_page(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
                writes, errors = [], []
                def read_only(route):
                    if route.request.method in {"GET", "HEAD"}: route.continue_()
                    else: writes.append(route.request.method); route.abort()
                page.route("**/*", read_only); page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/#development")
                expect(page.locator("#full-flow-test")).to_be_visible()
                expect(page.locator("#full-flow-model")).to_contain_text("Opus 5.5")
                expect(page.locator("#full-flow-model")).to_contain_text("high")
                expect(page.locator("#dev-state")).to_contain_text("需要处理")
                expect(page.locator("#full-flow-retest")).to_contain_text("首轮失败记录保持独立")
                expect(page.locator("#full-flow-retest")).to_contain_text("校验器误报已核实")
                expect(page.locator("#full-flow-package-short")).to_contain_text("短路径环境补测")
                self.assertLess(page.locator("#full-flow-test").bounding_box()["y"], page.locator(".dev-focus").bounding_box()["y"])
                page.locator("#full-flow-detail").click()
                dialog = page.locator("#flow-detail-dialog")
                expect(dialog).to_be_visible()
                dialog.get_by_role("button", name="只读规划 · 原生事件摘要", exact=False).click()
                expect(page.locator("#flow-evidence-text")).to_contain_text("读取文件")
                self.assertNotIn("PRIVATE_NATIVE", page.locator("#flow-evidence-text").inner_text())
                dialog.get_by_role("button", name="build测试截图", exact=False).click()
                expect(page.locator("#flow-evidence-image")).to_be_visible()
                page.wait_for_function("() => document.getElementById('flow-evidence-image').naturalWidth > 0")
                page.keyboard.press("Escape")
                expect(page.locator("#full-flow-detail")).to_be_focused()
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
                self.assertFalse(writes); self.assertFalse(errors); cache.resolve_art.assert_not_called()
                browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


if __name__ == "__main__":
    unittest.main()
