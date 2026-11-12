"""Pinned nongating environment failures: real isolated Git, fake UE only."""
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from unattended.config import validate_task_validation_policy
from unattended.engine import Engine
from unattended.storage import read_json, write_json
from unattended.ue_checks import (ValidationError, run_validation, task_validation_scope_sha256,
                                  verify_task_validation_policy)
from tests import test_unattended_ue_checks as bridge


PACKAGE = "ZhuGuoCity.M2.PackageIntegration"
TASK, TEST, batch_output = bridge.TASK, bridge.TEST, bridge.batch_output


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TaskValidationPolicyTests(unittest.TestCase):
    def setUp(self):
        bridge.BridgeTests.setUp(self)
        self.uat = self.work / "Saved/Evidence/TASK-LOCAL-0066/package-uat.log"
        self.uat.parent.mkdir(parents=True)
        self.uat.write_text("Unable to build while Live Coding is active.\nExitCode=6\n", encoding="utf-8")
        self.request["test_batches"] = [[PACKAGE, "ZhuGuoCity.M2.SeasonRules"]]
        write_json(self.manifest, self.request)
        self.state["task"].update(title="Exact task", body="Original scope", acceptance_criteria=["Business passes"],
                                  core_tests=[TEST], base_sha=self.head, coding_provider="claude", reworks=9,
                                  session_id="original-coding-session", autonomous_rework_allowance=6)
        write_json(self.runtime / "state.json", self.state)
        def output(names):
            return batch_output(names, failed=[PACKAGE]) + (
                f"BeginEvents: {PACKAGE}\npackage integration failed: exit=1 log={self.uat}\nEndEvents: {PACKAGE}\n")
        self.runner.batch_output = output
        self.receipt = run_validation(self.runtime, self.work, self.engine, runner=self.runner)
        self.assertEqual(self.receipt["status"], "failed")
        self.receipt_path = Path(self.receipt["receipt_path"])
        self.config = {"config_path": str(self.root / "config.json"), "project_path": str(self.work),
                       "runtime_dir": str(self.runtime), "goal": "exact task", "branch": "unattended/test",
                       "checks": [{"id": "build", "argv": ["build"], "timeout_seconds": 60},
                                  {"id": "ue-task-validation", "argv": ["verify"], "timeout_seconds": 60}],
                       "schedule": {}, "max_reworks": 3, "max_failures": 3, "push": False}
        folder = self.runtime / "runs/check-fixture"
        folder.mkdir(parents=True)
        stdout, stderr = folder / "stdout.log", folder / "stderr.log"
        summary = {key: self.receipt[key] for key in ("status", "scope", "task_id", "head_sha", "receipt_path", "error")}
        stdout.write_text("UE_TASK_VALIDATION_FAILED\n" + json.dumps(summary) + "\n", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        self.failed_check = {"id": "ue-task-validation", "head_sha": self.head, "passed": False,
                             "exit_code": 1, "timed_out": False, "output_limit_exceeded": False,
                             "stdout_path": str(stdout), "stderr_path": str(stderr)}
        write_json(folder / "check.json", self.failed_check)
        self.state["task"]["check_results"] = [{"id": "build", "head_sha": self.head, "passed": True}, self.failed_check]
        self.policy = {"task_id": TASK, "scope_sha256": task_validation_scope_sha256(self.state["task"], self.config["checks"]),
                       "head_sha": self.head, "receipt_sha256": sha(self.receipt_path),
                       "deferred_tests": [{"name": PACKAGE, "failure_kind": "live_coding_active",
                           "evidence_path": self.uat.relative_to(self.runtime).as_posix(), "evidence_sha256": sha(self.uat),
                           "reason": "Approved extra environment check; whole-stage packaging remains pending."}]}
        self.config["task_validation_policy"] = self.policy
        write_json(Path(self.config["config_path"]), self.config)
        self.controller = Engine(self.config, Mock())
        self.state.update(version=1, binding=self.controller._binding(), phase="BLOCKED", blocked_from="REVIEW",
                          operation=None, failures=0, last_error="checks failed", accepted=[], sequence=1)
        self.review_path = self.runtime / "tasks" / TASK / "review-9.json"
        write_json(self.review_path, {"decision": "accepted", "summary": "Task business passed", "feedback": "Package deferred",
                                      "head_sha": self.head})
        write_json(self.controller.state_path, self.state)

    def verify(self):
        return verify_task_validation_policy(self.config, self.state["task"], self.runtime, self.work)

    def repin_receipt(self):
        write_json(self.receipt_path, self.receipt)
        self.policy["receipt_sha256"] = sha(self.receipt_path)

    def test_exact_environment_failure_preserves_raw_failed_receipts(self):
        before = (self.receipt_path.read_bytes(), deepcopy(self.state["task"]["check_results"]))
        gate = self.verify()
        self.assertEqual(gate["status"], "policy_verified")
        self.assertTrue(gate["gate_passed"])
        self.assertFalse(gate["raw_check_passed"])
        self.assertEqual(gate["deferred_tests"][0]["status"], "deferred_nongating")
        self.assertEqual(before, (self.receipt_path.read_bytes(), self.state["task"]["check_results"]))

    def test_disabled_or_different_task_never_grants_exception(self):
        self.config.pop("task_validation_policy")
        self.assertIsNone(self.verify())
        self.config["task_validation_policy"] = self.policy
        self.state["task"]["id"] = "OTHER"
        self.assertIsNone(self.verify())

    def test_config_rejects_arbitrary_failures_paths_and_optional_fields(self):
        cases = []
        for key, value in [("name", TEST), ("failure_kind", "any_error"),
                           ("evidence_path", "workspace/Saved/Evidence/../secret/package-uat.log"),
                           ("evidence_path", "C:/private/package-uat.log"), ("evidence_sha256", "0")]:
            policy = deepcopy(self.policy); policy["deferred_tests"][0][key] = value; cases.append(policy)
        policy = deepcopy(self.policy); policy["optional"] = True; cases.append(policy)
        for policy in cases:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                validate_task_validation_policy(policy)

    def test_config_loader_retains_policy_without_rebinding_original_checks(self):
        from unattended.config import load_config
        value = deepcopy(self.config)
        value.update(version=1, project_path=str(self.root / "source-project"), codex={"command": ["codex.exe"]},
                     dsh={"command": ["dsh.exe"]})
        write_json(Path(value["config_path"]), value)
        loaded = load_config(Path(value["config_path"]))
        self.assertEqual(loaded["task_validation_policy"], self.policy)
        baseline = Engine({k: v for k, v in loaded.items() if k != "task_validation_policy"}, Mock())
        self.assertEqual(Engine(loaded, Mock())._binding(), baseline._binding())

    def test_new_head_scope_or_check_configuration_rejects_policy(self):
        for change in (lambda: self.state["task"].update(body="Changed acceptance"),
                       lambda: self.state["task"]["candidate"].update(head_sha="f" * 40),
                       lambda: self.config["checks"][0].update(argv=["different"])):
            task, checks = deepcopy(self.state["task"]), deepcopy(self.config["checks"])
            change()
            with self.assertRaisesRegex(ValidationError, "scope or candidate"):
                self.verify()
            self.state["task"], self.config["checks"] = task, checks

    def test_changed_receipt_or_environment_bytes_rejected(self):
        raw = self.receipt_path.read_bytes()
        self.receipt_path.write_bytes(raw + b" ")
        with self.assertRaisesRegex(ValidationError, "receipt bytes"):
            self.verify()
        self.receipt_path.write_bytes(raw)
        self.uat.write_text("unrelated build failure", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "environment evidence bytes"):
            self.verify()

    def test_business_failure_or_core_test_cannot_be_deferred(self):
        self.state["task"]["check_results"][0]["passed"] = False
        with self.assertRaisesRegex(ValidationError, "Only the pinned"):
            self.verify()
        self.state["task"]["check_results"][0]["passed"] = True
        self.receipt["tests"][0]["passed"] = False
        self.repin_receipt()
        with self.assertRaisesRegex(ValidationError, "mandatory core"):
            self.verify()

    def test_timeout_crash_or_truncated_automation_rejected_even_with_pinned_receipt(self):
        original = deepcopy(self.receipt)
        for field, value in [("timed_out", True), ("exit_code", -1), ("output_limit_exceeded", True)]:
            self.receipt = deepcopy(original); self.receipt["tests"][0][field] = value; self.repin_receipt()
            with self.subTest(field=field), self.assertRaisesRegex(ValidationError, "Incomplete"):
                self.verify()
        self.receipt = original; self.repin_receipt()
        Path(self.receipt["tests"][0]["stdout_path"]).write_text("Found 1 but no result", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "disagrees"):
            self.verify()

    def test_source_change_rejected(self):
        (self.work / "Source/Game.cpp").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "source changed"):
            self.verify()

    def test_unrelated_environment_log_and_non_environment_error_rejected(self):
        self.uat.write_text("Compiler failed with ExitCode=6", encoding="utf-8")
        self.policy["deferred_tests"][0]["evidence_sha256"] = sha(self.uat)
        with self.assertRaisesRegex(ValidationError, "does not prove"):
            self.verify()

    def test_reparse_evidence_rejected(self):
        from unattended import ue_checks
        real = ue_checks._is_reparse
        with patch.object(ue_checks, "_is_reparse", side_effect=lambda p: p == self.uat.parent or real(p)):
            with self.assertRaisesRegex(ValidationError, "junctions"):
                self.verify()

    def reconcile(self):
        with patch.object(self.controller, "_locks", return_value=nullcontext()), patch.object(self.controller, "_snapshot", side_effect=lambda s: s):
            return self.controller.reconcile_task_validation()

    def test_explicit_reconcile_is_idempotent_preserves_pause_session_count_and_raw_failure(self):
        pause = self.runtime / "PAUSE"; pause.write_text("user-owned pause", encoding="utf-8")
        first = self.reconcile()
        audit = Path(first["task"]["validation_gate"]["audit_path"]).read_bytes()
        second = self.reconcile()
        self.assertEqual(second["phase"], "PUBLISH")
        self.assertEqual(second["task"]["reworks"], 9)
        self.assertEqual(second["task"]["session_id"], "original-coding-session")
        self.assertFalse(second["task"]["check_results"][-1]["passed"])
        self.assertEqual(audit, Path(second["task"]["validation_gate"]["audit_path"]).read_bytes())
        self.assertEqual(pause.read_text(encoding="utf-8"), "user-owned pause")
        self.controller.providers.assert_not_called()

    def test_crash_after_audit_before_state_can_reconcile_without_duplicate_audit(self):
        with patch.object(self.controller, "_save", side_effect=OSError("simulated crash")):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                self.reconcile()
        self.assertEqual(read_json(self.controller.state_path)["phase"], "BLOCKED")
        result = self.reconcile()
        self.assertEqual(result["phase"], "PUBLISH")
        self.assertEqual(len(list(self.review_path.parent.glob("validation-gate-*.json"))), 1)

    def test_reconcile_rejects_nonaccepted_review_and_changed_disk_policy(self):
        review = read_json(self.review_path); review["decision"] = "rework"; write_json(self.review_path, review)
        with self.assertRaisesRegex(ValueError, "accepted"):
            self.reconcile()
        review["decision"] = "accepted"; write_json(self.review_path, review)
        disk = deepcopy(self.config); disk["task_validation_policy"]["deferred_tests"][0]["reason"] = "changed"
        write_json(Path(self.config["config_path"]), disk)
        with self.assertRaisesRegex(ValueError, "可信门槛策略"):
            self.reconcile()

    def test_accepted_review_uses_gate_without_rework_and_publish_rechecks_evidence(self):
        state = deepcopy(self.state); state["phase"] = "REVIEW"
        with patch.object(self.controller, "_invoke", return_value=read_json(self.review_path)):
            self.controller._review(state, self.work)
        self.assertEqual(state["phase"], "PUBLISH")
        self.assertEqual(state["task"]["reworks"], 9)
        self.uat.write_text("changed after acceptance", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "environment evidence bytes"):
            self.controller._publish(state, self.work)

    def test_no_policy_still_blocks_accepted_review_with_failed_checks(self):
        self.config.pop("task_validation_policy")
        state = deepcopy(self.state); state["phase"] = "REVIEW"
        with patch.object(self.controller, "_invoke", return_value=read_json(self.review_path)):
            self.controller._review(state, self.work)
        self.assertEqual(state["phase"], "BLOCKED")
        self.assertNotIn("validation_gate", state["task"])

    def test_local_publish_retains_original_failure_and_audit_without_git_writes(self):
        state = self.reconcile()
        self.controller._publish(state, self.work)
        self.assertEqual(state["phase"], "PLAN")
        self.assertEqual(state["accepted"][0]["head_sha"], self.head)
        completed = read_json(self.review_path.parent / "completed-task.json")
        self.assertFalse(completed["check_results"][-1]["passed"])
        self.assertEqual(completed["validation_gate"]["status"], "policy_verified")
        self.assertEqual(completed["reworks"], 9)


if __name__ == "__main__":
    unittest.main()
