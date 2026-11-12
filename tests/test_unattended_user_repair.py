"""User-authorized scope/model handoff with preserved audits and independent gates."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

from tests import test_unattended_task_replanning as fixtures
from unattended import user_repair as repair, ue_checks
from unattended.storage import read_json, write_json, write_text

TARGET = "99db06e4-336d-4d8e-927d-1db262334adf"


class UserRepairTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TaskReplanningTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.engine, self.config = self.f.engine, self.f.config
        self.runtime, self.work = self.engine.runtime, self.f.work
        self.config["checks"].append({"id": "ue-task-validation", "argv": [sys.executable, "check.py", "--engine", str(self.runtime / "UE_5.8/Engine")], "timeout_seconds": 30})
        state = self.f.state(); state["binding"] = self.engine._binding()
        state.update(phase="BLOCKED", blocked_from="REVIEW")
        state["task"].update(reworks=9, autonomous_rework_allowance=6,
            replanning_history=[{"round": i, "head_sha": fixtures.HEAD, "execution_decision": "continue"} for i in range(1, 4)])
        self.engine._save(state)
        write_json(self.f.folder / "review-9.json", {**self.f.review, "decision": "accepted"})
        self.review_bytes = (self.f.folder / "review-9.json").read_bytes()
        self.before = self.f.state()
        self.run = self.runtime / "full-flow/runs" / ("e" * 32); self.run.mkdir(parents=True)
        write_json(self.run / "request.json", {"task_id": fixtures.TASK, "head_sha": fixtures.HEAD, "run_dir": str(self.run), "request_sha256": "f" * 64})
        write_json(self.run / "source-manifest.json", {"head_sha": fixtures.HEAD, "automation_inventory": {"regression": ["ZhuGuoCity.M3.DeathFlow"], "visual": {}}})
        write_json(self.run / "checks/retest/receipt.json", {"task_id": fixtures.TASK, "head_sha": fixtures.HEAD, "request_sha256": "f" * 64,
            "status": "failed", "finished_at": self.f.f.now.isoformat(), "source_manifest_sha256": repair.file_sha(self.run / "source-manifest.json")})
        write_text(self.run / "report.md", "Actual independent report; failed evidence remains failed.")
        self.answer_path = self.run / "native/analysis/answer.json"
        self.native_answer(self.answer_path)
        self.id = "d" * 32
        self.path = self.runtime / "user-repairs" / self.id / "request.json"
        records = [("full_flow_request", self.run / "request.json"), ("report", self.run / "report.md"),
                   ("retest", self.run / "checks/retest/receipt.json"), ("source_manifest", self.run / "source-manifest.json"),
                   ("model_answer", self.answer_path)]
        self.request = {"version": 1, "id": self.id, "task_id": fixtures.TASK, "user_instruction": "Have actual Opus repair the full-flow failures and verify the original requirements.",
            "authorized_at": self.f.f.now.isoformat(), "source_head": fixtures.HEAD,
            "source_state_sha256": repair.file_sha(self.engine.state_path),
            "source_scope_sha256": ue_checks.task_validation_scope_sha256(self.before["task"], self.config["checks"]),
            "source_full_flow_run": str(self.run), "source_session_id": fixtures.SESSION, "target_session_id": TARGET,
            "model": repair.MODEL, "reasoning_effort": "high", "rework_window": 3, "max_cycles": 2,
            "evidence": [{"role": role, "path": str(path), "sha256": repair.file_sha(path), "size_bytes": path.stat().st_size} for role, path in records]}
        write_json(self.path, self.request)

    def native_answer(self, path):
        write_json(path, {"session_id": TARGET, "answer": "Actual Claude autonomous proposal, with no controller-authored implementation."})
        write_json(path.parent / "process.json", {"exit_code": 0, "timed_out": False, "output_limit_exceeded": False})
        events = [{"type": "system", "subtype": "init", "session_id": TARGET, "model": repair.MODEL},
            {"type": "result", "subtype": "success", "session_id": TARGET, "is_error": False,
             "result": read_json(path)["answer"], "modelUsage": {repair.MODEL: {}}}]
        write_text(path.parent / "stdout.log", "\n".join(json.dumps(row) for row in events) + "\n")

    def activate(self):
        return self.engine.activate_user_repair(self.path)

    def test_authorized_accepted_review_can_expand_scope_without_rewriting_baseline_or_history(self):
        result = self.activate(); after = self.f.state(); task = after["task"]
        self.assertEqual(result["phase"], "CODE"); self.assertEqual(task["reworks"], 10)
        self.assertEqual(task["session_id"], TARGET); self.assertEqual(task["code_base"], fixtures.HEAD)
        self.assertEqual(task["base_sha"], self.before["task"]["base_sha"])
        self.assertEqual(after["expected_head"], self.before["expected_head"])
        self.assertEqual(task["replanning_history"], self.before["task"]["replanning_history"])
        self.assertEqual(task["core_tests"], self.before["task"]["core_tests"])
        self.assertEqual(task["manual_rework_allowance"], 3)
        self.assertEqual((self.f.folder / "review-9.json").read_bytes(), self.review_bytes)
        self.assertIn(self.request["user_instruction"], task["body"])
        self.assertEqual(read_json(self.f.folder / "task.json"), task)
        self.assertEqual(self.activate()["phase"], "CODE")
        self.assertEqual(self.f.state()["task"]["reworks"], 10)
        self.assertEqual(self.config["claude"]["model"], "claude-sonnet-5-5")

    def test_state_head_scope_and_evidence_drift_refuse_before_activation(self):
        for field in ("source_state_sha256", "source_scope_sha256", "source_head"):
            value = deepcopy(self.request); value[field] = "a" * (40 if field == "source_head" else 64)
            if value[field] == self.request[field]: value[field] = "9" * len(value[field])
            write_json(self.path, value)
            with self.subTest(field=field), self.assertRaises(ValueError): self.activate()
        write_json(self.path, self.request)
        write_text(self.run / "report.md", "Changed")
        with self.assertRaisesRegex(ValueError, "evidence changed"): self.activate()
        self.assertEqual(self.f.state()["phase"], "BLOCKED")

    def test_actual_native_model_and_session_are_required_for_handoff(self):
        write_text(self.answer_path.parent / "stdout.log", (self.answer_path.parent / "stdout.log").read_text().replace(repair.MODEL, "claude-sonnet-5-5"))
        with self.assertRaises(ValueError): self.activate()
        self.assertEqual(self.f.state()["task"]["session_id"], fixtures.SESSION)

    def test_plan_source_is_native_answer_and_plan_bytes_are_bound(self):
        plan = self.path.parent / "native/plan/answer.json"; self.native_answer(plan)
        self.request.update(plan_path=str(plan), plan_sha256=repair.file_sha(plan)); write_json(self.path, self.request)
        self.activate()
        self.assertIn(read_json(plan)["answer"], self.f.state()["task"]["feedback"])
        write_text(plan, "changed")
        with self.assertRaisesRegex(ValueError, "plan evidence changed"):
            self.engine._task_checks(self.f.state()["task"])

    def test_only_repair_code_clone_and_operation_use_opus(self):
        self.activate(); state = self.f.state()
        with patch("unattended.user_repair.UserRepair._user_repair_providers", wraps=self.engine._user_repair_providers):
            op = self.engine._new_operation(state, "code")
        self.assertEqual(op["coding_model"], repair.MODEL)
        self.assertEqual(op["coding_reasoning_effort"], "high")
        self.assertEqual(op["requested_session_id"], TARGET)
        self.assertEqual(op["user_repair_id"], self.id)
        self.engine._providers_injected = False
        with patch("unattended.providers.AgentProviders") as provider:
            self.engine._user_repair_providers(state["task"])
        cfg = provider.call_args.args[0]
        self.assertEqual(cfg["_user_repair_profile"], state["task"]["user_repair"])
        self.assertEqual(cfg["claude"]["model"], repair.MODEL)
        self.assertEqual(self.config["claude"]["model"], "claude-sonnet-5-5")

    def test_full_check_is_added_only_to_authorized_task_and_missing_cannot_publish(self):
        old_checks = deepcopy(self.config["checks"])
        self.activate(); task = self.f.state()["task"]
        checks = self.engine._task_checks(task)
        self.assertEqual(checks[-1]["id"], repair.CHECK_ID)
        self.assertEqual(self.config["checks"], old_checks)
        self.assertEqual(self.engine._task_checks(self.before["task"]), old_checks)
        self.assertFalse(self.engine._user_repair_gate(task, self.work, require_pass=False))
        with self.assertRaisesRegex(ValueError, "mandatory full-flow"):
            self.engine._publish(self.f.state(), self.work)
        self.assertEqual(self.f.state()["accepted"], [])

    def test_full_check_success_requires_real_receipt_verifier(self):
        self.activate(); task = self.f.state()["task"]
        task["check_results"] = [{"id": repair.CHECK_ID, "head_sha": fixtures.HEAD, "passed": True}]
        verify = Mock(return_value={"passed": False})
        module = types.SimpleNamespace(verify_repair_check=verify)
        with patch.dict(sys.modules, {"unattended.full_repair_check": module}):
            self.assertFalse(self.engine._user_repair_gate(task, self.work, require_pass=False))
            with self.assertRaises(ValueError): self.engine._user_repair_gate(task, self.work, require_pass=True)
            verify.return_value = {"passed": True}
            self.assertTrue(self.engine._user_repair_gate(task, self.work, require_pass=True))
        verify.assert_called_with(self.runtime, self.work, task, fixtures.HEAD)

    def test_dynamic_check_interpreter_is_fixed_across_python_and_pythonw_hosts(self):
        self.activate(); task = self.f.state()["task"]
        configured = next(row for row in self.config["checks"] if row["id"] == "ue-task-validation")["argv"][0]
        with patch("sys.executable", str(self.runtime / "maintenance/python.exe")):
            cli = self.engine._task_checks(task)
        with patch("sys.executable", str(self.runtime / "worker/pythonw.exe")):
            background = self.engine._task_checks(task)
        self.assertEqual(cli, background)
        self.assertEqual(background[-1]["argv"][0], configured)
        self.assertEqual(self.engine._replanning_digest(cli), self.engine._replanning_digest(background))
        self.assertEqual(task["user_repair"]["activated_scope_sha256"],
                         ue_checks.task_validation_scope_sha256(task, self.config["checks"]))

    def test_old_replanning_history_is_preserved_but_does_not_consume_new_epoch(self):
        self.activate(); task = self.f.state()["task"]
        self.assertEqual(self.engine._epoch_replanning_history(task), [])
        self.assertEqual(self.engine._task_replanning_policy(task), {"enabled": True, "max_cycles": 2, "rework_window": 3})
        task["replanning_history"].append({"round": 4, "user_repair_epoch": self.id})
        self.assertEqual(len(self.engine._epoch_replanning_history(task)), 1)
        self.assertEqual(len(task["replanning_history"]), 4)

    def test_changed_active_scope_cannot_dispatch_or_silently_use_another_model(self):
        self.activate(); task = self.f.state()["task"]
        task["body"] += "Unauthorized change"
        with self.assertRaisesRegex(ValueError, "scope changed"): self.engine._user_repair_providers(task)

    def test_save_crash_is_recoverable_without_counter_or_session_duplication(self):
        with patch.object(self.engine, "_save", side_effect=OSError("fixture crash before state save")):
            with self.assertRaises(OSError): self.activate()
        self.assertEqual(self.f.state()["phase"], "BLOCKED")
        self.activate(); self.activate()
        state = self.f.state()
        self.assertEqual(state["task"]["reworks"], 10)
        self.assertEqual(len(state["task"]["session_history"]), 1)
        self.assertEqual(state["task"]["session_id"], TARGET)

    def test_cli_repairs_only_with_explicit_request_path(self):
        from unattended.cli import parser
        args = parser().parse_args(["repair", "--request", str(self.path)])
        self.assertEqual(args.command, "repair"); self.assertEqual(args.request, self.path)

    def test_accepted_codex_review_cannot_bypass_missing_full_flow_gate(self):
        self.activate(); state = self.f.state(); state["phase"] = "REVIEW"
        state["task"]["check_results"] = [{"id": row["id"], "head_sha": fixtures.HEAD, "passed": True}
                                         for row in self.config["checks"]]
        self.f.providers.review.return_value = {**self.f.review, "decision": "accepted"}
        self.engine._review(state, self.work)
        self.assertEqual(state["phase"], "CODE")
        self.assertEqual(state["task"]["reworks"], 11)
        self.assertEqual(read_json(self.f.folder / "review-10.json")["decision"], "accepted")
        self.assertEqual(state["accepted"], [])

    def test_repair_review_covers_authorized_reported_residuals_without_prescribing_implementation(self):
        self.activate(); state = self.f.state(); state["phase"] = "REVIEW"
        task_before = deepcopy(state["task"])
        checks_before = deepcopy(self.config["checks"])
        self.f.providers.review.return_value = {**self.f.review, "decision": "blocked"}
        self.engine._review(state, self.work)
        prompt = self.f.providers.review.call_args.args[0]
        self.assertIn("逐项核对交付报告（含后续工具交付）中已知残留和未验证项", prompt)
        self.assertIn("不能仅因旧文档曾冻结、问题并非本任务引入", prompt)
        self.assertIn("确实范围外的历史人工待办可以保留，但须逐项说明保留依据", prompt)
        self.assertIn("由 Claude 自行提出并实施方案", prompt)
        self.assertIn("不改变任何机器检查门槛", prompt)
        self.assertEqual(state["task"], task_before)
        self.assertEqual(self.config["checks"], checks_before)

    def test_normal_review_does_not_receive_full_repair_scope_addition(self):
        state = deepcopy(self.before); state.update(phase="REVIEW", operation=None)
        self.engine._save(state)
        self.f.providers.review.return_value = {**self.f.review, "decision": "blocked"}
        self.engine._review(state, self.work)
        prompt = self.f.providers.review.call_args.args[0]
        self.assertNotIn("本任务有用户明确追加的完整修复授权", prompt)
        self.assertNotIn("不能仅因旧文档曾冻结", prompt)

    def test_incomplete_repair_timeout_binds_native_notification_prefix_to_original_session(self):
        self.activate(); task = self.f.state()["task"]
        out = self.runtime / "runs" / ("code-" + "c" * 32)
        events = [{"type": "system", "subtype": "task_notification", "session_id": TARGET},
                  {"type": "system", "subtype": "init", "session_id": TARGET, "model": repair.MODEL}]
        write_text(out / "stdout.log", "\n".join(json.dumps(row) for row in events))
        profile = task["user_repair"]
        op = {"kind": "code", "task_id": task["id"], "coding_provider": "claude", "head_sha": fixtures.HEAD,
              "code_base": task["code_base"], "window_limited": False, "output_dir": str(out),
              "requested_session_id": TARGET, "user_repair_id": profile["id"], "user_repair_request_sha256": profile["request_sha256"]}
        write_json(out / "process.json", {"kind": "code", "coding_provider": "claude", "timed_out": True,
            "output_limit_exceeded": False, "exit_code": 1, "session_id": None, "requested_session": TARGET,
            "user_repair_profile": profile, "native_protocol": "full_flow_native_v1", "task_id": task["id"],
            "head_sha": fixtures.HEAD, "coding_model": repair.MODEL, "coding_reasoning_effort": "high", "strict_model_selection": True})
        self.assertEqual(self.engine._code_timeout_receipt(task, self.work, op), out)
        events[0]["session_id"] = fixtures.SESSION
        write_text(out / "stdout.log", "\n".join(json.dumps(row) for row in events))
        with self.assertRaisesRegex(ValueError, "会话证据"):
            self.engine._code_timeout_receipt(task, self.work, op)


if __name__ == "__main__":
    unittest.main()
