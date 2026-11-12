"""Controlled single-case restart fixtures; no actual model or UE execution."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from unattended import full_flow_checks as flow, full_flow_retest as retest, full_flow_retest_resume as resume
from unattended.storage import read_json, write_json, write_text
from tests import test_unattended_full_flow_retest as fixtures
A, B, V = fixtures.A, fixtures.B, fixtures.V


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RetestTests(); self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        f = self.fixture; self.run_dir = f.run_dir; self.folder = f.folder; self.request = f.request
        self.request_path = self.run_dir / "request.json"; write_json(self.request_path, self.request)
        selected, originals, _, _ = retest._first_pass(flow, self.request, "manifest-sha")
        self.binding = {key: self.request[key] for key in resume._IDENTITY}
        self.case_binding = {**self.binding, "source_manifest_sha256": "manifest-sha", "first_pass_evidence_sha256": retest._digest(originals)}
        first = self.folder / "case-001"
        row = {**self.case_binding, **selected[0], "attempt": 1,
               **retest._nullrhi_case(flow, self.request, selected[0], first, 600), "finished_at": flow._now()}
        write_json(first / "receipt.json", row)
        interrupted = self.folder / "case-002"
        write_json(interrupted / "receipt.json", {**self.case_binding, **selected[1], "attempt": 1,
            "status": "RUNNING", "passed": False, "started_at": flow._now(), "evidence": []})
        write_text(interrupted / "stdout.log", "Incomplete native test")
        self.master = self.run_dir / "checks/retest/receipt.json"
        write_json(self.master, {**self.binding, "stage": "retest", "attempt": 1, "status": "running",
                                "started_at": flow._now(), "passed": False})
        killed = self.run_dir / "native/analysis-3/background-killed-event.json"
        write_json(killed, {"sessionId": "99db06e4-336d-4d8e-927d-1db262334adf", "type": "user", "promptSource": "system",
            "turnOrigin": "task_notification", "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content": "<task-notification><task-id>fixture-task</task-id><status>killed</status>"
                        "<summary>Background command stopped after reaching its background time limit</summary></task-notification>"}})
        self.policy_path = self.run_dir / "operator-retest-recovery.json"
        self.policy = {"version": 1, **self.binding, "master_sha256": flow._sha(self.master),
            "completed_cases": [{"index": 1, "receipt_sha256": flow._sha(first / "receipt.json")}],
            "interrupted_case": {"index": 2, "name": B, "receipt_sha256": flow._sha(interrupted / "receipt.json"), "action": "restart_once_after_native_kill"},
            "resume_runner_sha256": flow._sha(resume.__file__), "reason": resume.REASON,
            "native_killed_event": {**flow._file_record(killed), "task_id": "fixture-task", "session_id": "99db06e4-336d-4d8e-927d-1db262334adf"}}
        write_json(self.policy_path, self.policy)
        patch.object(flow, "_bind", return_value=self.binding).start()
        self.liveness = patch.object(resume, "_no_active_case", return_value={"matching_processes": 0}).start()
        f.calls.clear()

    def invoke(self):
        return resume.run(self.request_path)

    def test_only_interrupted_and_never_started_suffix_execute_in_new_attempt(self):
        master_bytes = self.master.read_bytes()
        originals = {path: path.read_bytes() for name in ("case-001", "case-002") for path in (self.folder / name).iterdir()}
        result = self.invoke()
        self.assertEqual([next(arg for arg in argv if arg.startswith("-ExecCmds=")) for argv, _ in self.fixture.calls],
                         ["-ExecCmds=Automation RunTests ^" + B + "$", "-ExecCmds=Automation RunTests ^" + V + "$"])
        self.assertTrue(result["composite_execution"]); self.assertEqual(result["attempt"], 2)
        self.assertEqual(result["completed_prefix_count"], 1); self.assertEqual(result["resumed_count"], 2)
        self.assertEqual(result["attempted_count"], 3); self.assertEqual(result["newly_passed"], [A, B, V])
        self.assertTrue((self.run_dir / "checks/retest/attempt-2/case-002/receipt.json").is_file())
        self.assertEqual((self.folder / "interrupted-checkpoint.json").read_bytes(), master_bytes)
        self.assertEqual(originals, {path: path.read_bytes() for path in originals})
        self.assertEqual(self.invoke(), result); self.assertEqual(len(self.fixture.calls), 2)
        self.assertTrue((self.run_dir / "facts.json").is_file())

    def test_policy_request_runner_and_prefix_receipt_changes_fail_before_launch(self):
        for key in ("head_sha", "request_sha256", "resume_runner_sha256", "master_sha256"):
            invalid = {**self.policy, key: "changed"}; write_json(self.policy_path, invalid)
            with self.subTest(key=key), self.assertRaises(ValueError): self.invoke()
        write_json(self.policy_path, self.policy)
        write_text(self.folder / "case-001/stdout.log", "changed")
        with self.assertRaisesRegex(ValueError, "evidence changed"): self.invoke()
        self.assertEqual(self.fixture.calls, [])

    def test_interrupted_case_cannot_be_replaced_or_look_completed(self):
        path = self.folder / "case-002/receipt.json"
        value = read_json(path); value["finished_at"] = flow._now(); write_json(path, value)
        self.policy["interrupted_case"]["receipt_sha256"] = flow._sha(path); write_json(self.policy_path, self.policy)
        with self.assertRaisesRegex(ValueError, "unfinished"): self.invoke()
        self.assertEqual(self.fixture.calls, [])

    def test_other_started_case_or_active_native_case_refuses_recovery(self):
        (self.folder / "case-003").mkdir()
        with self.assertRaisesRegex(ValueError, "already has artifacts"): self.invoke()
        self.assertEqual(self.fixture.calls, [])

    def test_active_native_case_does_not_archive_or_launch(self):
        self.liveness.side_effect = ValueError("Interrupted case still has an active native process")
        with self.assertRaisesRegex(ValueError, "active native"): self.invoke()
        self.assertFalse((self.folder / "interrupted-checkpoint.json").exists())
        self.assertEqual(self.fixture.calls, [])

    def test_crash_in_new_attempt_is_not_automatically_submitted_again(self):
        self.fixture.execute = None
        with patch.object(flow, "_execute", side_effect=SystemExit("simulated lost parent")):
            with self.assertRaises(SystemExit): self.invoke()
        with self.assertRaisesRegex(ValueError, "incomplete"): self.invoke()
        self.assertEqual(self.fixture.calls, [])

    def test_crash_after_case_completion_can_finish_master_without_reexecution(self):
        original = resume.write_json
        def write(path, value):
            if Path(path) == self.master and value.get("status") in {"passed", "failed"}:
                raise SystemExit("after composite receipt, before master")
            original(path, value)
        with patch.object(resume, "write_json", side_effect=write):
            with self.assertRaises(SystemExit): self.invoke()
        self.assertEqual(len(self.fixture.calls), 2)
        result = self.invoke()
        self.assertEqual(result["submitted_this_invocation_count"], 0)
        self.assertEqual(result["resumed_count"], 2); self.assertEqual(len(self.fixture.calls), 2)

    def test_existing_final_fail_is_never_overwritten(self):
        write_json(self.master, {**read_json(self.master), "status": "failed", "finished_at": flow._now()})
        before = self.master.read_bytes()
        with self.assertRaisesRegex(ValueError, "terminal"): self.invoke()
        self.assertEqual(self.master.read_bytes(), before); self.assertEqual(self.fixture.calls, [])

    def test_native_notification_must_prove_system_timeout_and_same_task(self):
        path = Path(self.policy["native_killed_event"]["path"])
        value = read_json(path); value["promptSource"] = "user"; write_json(path, value)
        self.policy["native_killed_event"].update(flow._file_record(path)); write_json(self.policy_path, self.policy)
        with self.assertRaisesRegex(ValueError, "native task-notification"): self.invoke()
        self.assertEqual(self.fixture.calls, [])

    def test_per_item_budget_remains_at_most_600_seconds(self):
        self.fixture.consume_timeout = True
        self.invoke()
        self.assertTrue(all(timeout <= 600 for _, timeout in self.fixture.calls))
        self.assertLessEqual(sum(timeout for _, timeout in self.fixture.calls), 1800)


if __name__ == "__main__":
    unittest.main()
