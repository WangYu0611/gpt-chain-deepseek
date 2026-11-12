from pathlib import Path
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from unattended import full_flow_validation as flow
from unattended.storage import read_json, write_json


class FullFlowOperatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name).resolve() / "runtime"
        self.source = self.runtime / "workspace"
        self.source.mkdir(parents=True)
        self.head = "a" * 40
        self.session = "93f21123-fe41-4218-83e6-411af1a855d3"
        self.branch = "99db06e4-336d-4d8e-927d-1db262334adf"
        self.state = {"phase": "BLOCKED", "operation": None, "task": {
            "id": "TASK-LOCAL-0091", "session_id": self.session, "reworks": 9}}
        write_json(self.runtime / "state.json", self.state)
        self.original_state = (self.runtime / "state.json").read_bytes()
        self.config = {"runtime_dir": str(self.runtime), "claude": {}}
        self.addCleanup(patch.stopall)
        patch.object(flow, "load_config", return_value=self.config).start()
        patch.object(flow.gitops, "head", return_value=self.head).start()
        patch.object(flow.gitops, "clean", return_value=True).start()
        patch.object(flow, "_task_output_directory", return_value=self.runtime / "native-task-outputs").start()

    def create(self):
        return Path(flow.create(Path("ignored.json"))["request"])

    def test_preserves_task_and_user_pause(self):
        pause = self.runtime / "PAUSE"
        pause.write_text("user paused\n", encoding="utf-8")
        request = self.create()
        self.assertEqual(pause.read_text(encoding="utf-8"), "user paused\n")
        self.assertFalse(read_json(request.parent / "operator.json")["owns_pause"])
        self.assertEqual((self.runtime / "state.json").read_bytes(), self.original_state)
        self.assertEqual(read_json(request)["request_sha256"], flow.canonical_hash(read_json(request)))

    def test_does_not_interrupt_an_active_main_operation(self):
        self.state.update(phase="CHECK", operation={"kind": "check"})
        write_json(self.runtime / "state.json", self.state)
        with self.assertRaisesRegex(RuntimeError, "idle"):
            self.create()
        self.assertFalse((self.runtime / "PAUSE").exists())

    def test_does_not_duplicate_pending_operator_job(self):
        self.create()
        with self.assertRaisesRegex(RuntimeError, "already pending"):
            self.create()

    def test_rejects_modified_request_binding(self):
        request = self.create()
        value = read_json(request)
        value["head_sha"] = "b" * 40
        write_json(request, value)
        with self.assertRaisesRegex(ValueError, "binding"):
            flow._request(request)

    def native(self, answer_session="99db06e4-336d-4d8e-927d-1db262334adf", rate=None):
        captured = []
        def run(argv, **kwargs):
            captured.append(argv)
            self.native_environment = kwargs["environment"]
            (kwargs["output_dir"] / "stdout.log").write_text("{}\n", encoding="utf-8")
            return SimpleNamespace(exit_code=0, timed_out=False, output_limit_exceeded=False)
        patch.object(flow, "claude_subscription_ready", return_value=True).start()
        patch.object(flow, "_claude_environment", return_value={"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}).start()
        patch.object(flow, "run_process", side_effect=run).start()
        self.turn = patch.object(flow, "_claude_turn", return_value=(answer_session,
                     {"result": "test report", "is_error": False}, [], rate)).start()
        patch.object(flow, "_claude_model_evidence", return_value={"model_verified": True}).start()
        self.full_turn = patch.object(flow, "parse_full_flow_native", return_value=(answer_session,
                     {"result": "test report", "is_error": False}, [], rate, {"model_verified": True})).start()
        return captured

    def test_plan_uses_real_native_opus_contract_and_forks_coding_session(self):
        request = self.create()
        captured = self.native()
        flow.invoke(Path("ignored"), request, "plan")
        argv = captured[0]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5-5")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(argv[argv.index("--resume") + 1], self.session)
        self.assertIn("--fork-session", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(argv.count("--allowedTools"), 1)
        self.assertEqual(self.native_environment["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "1")
        self.assertEqual((self.runtime / "state.json").read_bytes(), self.original_state)

    def test_finished_model_reply_does_not_complete_unrun_tests(self):
        request = self.create()
        write_json(request.parent / "native/plan/answer.json", {"session_id": self.branch})
        captured = self.native()
        flow.invoke(Path("ignored"), request, "test")
        status = read_json(request.parent / "status.json")
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["missing_stages"], list(flow.STAGES[:-1]))
        self.assertEqual(captured[0].count("--allowedTools"), 1)
        self.assertNotIn("Monitor", captured[0])
        self.assertNotIn("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", self.native_environment)
        self.assertEqual(captured[0][captured[0].index("--resume") + 1], self.branch)
        self.assertEqual(self.full_turn.call_args.args[1], self.branch)
        self.assertNotIn("--fork-session", captured[0])
        settings = json.loads(captured[0][captured[0].index("--settings") + 1])
        directories = settings["permissions"]["additionalDirectories"]
        self.assertIn(str(flow.ROOT / "unattended"), directories)
        self.assertIn(str(Path(sys.executable).resolve().parent), directories)
        self.assertIn(str(self.runtime / "native-task-outputs"), directories)
        self.assertEqual((self.runtime / "state.json").read_bytes(), self.original_state)

    def test_running_receipt_is_not_a_completed_stage(self):
        request = self.create()
        binding = read_json(request)
        write_json(request.parent / "native/plan/answer.json", {"session_id": self.branch})
        for stage in flow.STAGES[:-1]:
            write_json(request.parent / "checks" / stage / "receipt.json", {
                **{key: binding[key] for key in ("id", "task_id", "head_sha", "request_sha256")},
                "stage": stage, "status": "running" if stage == "regression" else "passed",
                "finished_at": None if stage == "regression" else "2026-11-08T00:00:00Z"})
        self.native()
        flow.invoke(Path("ignored"), request, "test")
        self.assertEqual(read_json(request.parent / "status.json")["missing_stages"], ["regression"])

    def test_changed_native_session_fails_and_records_terminal_status(self):
        request = self.create()
        write_json(request.parent / "native/plan/answer.json", {"session_id": self.branch})
        self.native(answer_session="00000000-0000-0000-0000-000000000000")
        with self.assertRaisesRegex(RuntimeError, "resume"):
            flow.invoke(Path("ignored"), request, "test")
        self.assertEqual(read_json(request.parent / "status.json")["status"], "failed")

    def test_allowed_quota_metadata_does_not_reject_successful_plan(self):
        request = self.create()
        self.native(rate={"status": "allowed", "rateLimitType": "five_hour"})
        flow.invoke(Path("ignored"), request, "plan")
        self.assertEqual(read_json(request.parent / "status.json")["status"], "preparing")

    def test_analysis_continuation_preserves_old_logs_and_precise_read_permissions(self):
        request = self.create()
        write_json(request.parent / "native/plan/answer.json", {"session_id": self.branch})
        captured = self.native()
        flow.invoke(Path("ignored"), request, "analysis")
        original = (request.parent / "native/analysis/stdout.log").read_bytes()
        flow.invoke(Path("ignored"), request, "analysis")
        self.assertEqual((request.parent / "native/analysis/stdout.log").read_bytes(), original)
        self.assertEqual(read_json(request.parent / "status.json")["output_dir"],
                         str(request.parent / "native/analysis-2"))
        settings = json.loads(captured[-1][captured[-1].index("--settings") + 1])
        self.assertIn(str(flow.ROOT / "unattended"), settings["permissions"]["additionalDirectories"])
        self.assertNotIn("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", self.native_environment)

    def test_copied_receipt_from_another_stage_cannot_complete_test(self):
        request = self.create()
        binding = read_json(request)
        write_json(request.parent / "native/plan/answer.json", {"session_id": self.branch})
        for stage in flow.STAGES[:-1]:
            write_json(request.parent / "checks" / stage / "receipt.json", {
                **{key: binding[key] for key in ("id", "task_id", "head_sha", "request_sha256")},
                "stage": "build" if stage == "visual" else stage, "status": "passed",
                "finished_at": "2026-11-08T00:00:00Z"})
        self.native()
        with self.assertRaisesRegex(RuntimeError, "stage"):
            flow.invoke(Path("ignored"), request, "test")
        self.assertEqual(read_json(request.parent / "status.json")["status"], "failed")


if __name__ == "__main__":
    unittest.main()
