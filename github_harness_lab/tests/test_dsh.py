import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab_harness.dsh import DshError, DshRunner, parse_events
from lab_harness.process import ProcessResult


class DshTests(unittest.TestCase):
    def parse(self, events, code=0, requested=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text("\n".join(json.dumps(x) for x in events), encoding="utf-8")
            return parse_events(path, exit_code=code, requested_session=requested)

    def events(self):
        return [{"type": "session", "sessionId": "session-1"},
                {"type": "final", "sessionId": "session-1", "reason": {"kind": "completed"}, "text": "tests passed"}]

    def test_completed_contract(self):
        result = self.parse(self.events())
        self.assertEqual(result["final_kind"], "completed")
        self.assertNotIn("text", result)  # Prose is not machine evidence.

    def test_prose_alone_does_not_succeed(self):
        with self.assertRaises(DshError):
            self.parse([{"type": "final", "text": "all tests passed"}])

    def test_nonzero_exit_invalidates_completed(self):
        with self.assertRaises(DshError):
            self.parse(self.events(), code=1)

    def test_failed_reason_invalidates_zero_exit(self):
        events = self.events()
        events[-1]["reason"]["kind"] = "error"
        with self.assertRaises(DshError):
            self.parse(events)

    def test_session_mismatch(self):
        with self.assertRaises(DshError):
            self.parse(self.events(), requested="another-session")

    def test_final_mismatch(self):
        events = self.events()
        events[-1]["sessionId"] = "another-session"
        with self.assertRaises(DshError):
            self.parse(events)

    def test_error_or_post_final_output(self):
        for events in (self.events() + [{"type": "status"}],
                       [self.events()[0], {"type": "error", "message": "failure"}, self.events()[1]]):
            with self.assertRaises(DshError):
                self.parse(events)

    def test_default_cannot_send(self):
        with self.assertRaisesRegex(DshError, "disabled"):
            DshRunner({}).run("hi", Path.cwd(), Path.cwd())

    def test_truthy_string_is_not_authorization(self):
        with self.assertRaisesRegex(DshError, "disabled"):
            DshRunner({}, authorized="false").run("hi", Path.cwd(), Path.cwd())

    def test_external_budget_is_required_before_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, workspace = root / "home", root / "workspace"
            home.mkdir()
            workspace.mkdir()
            with patch("lab_harness.dsh.run_process") as process:
                with self.assertRaisesRegex(DshError, "spending cap"):
                    DshRunner({"home": str(home), "budget_enforced_externally": "false"}, authorized=True).run("hi", workspace, root / "logs")
                process.assert_not_called()

    def test_workspace_dotenv_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, workspace = root / "home", root / "workspace"
            home.mkdir()
            workspace.mkdir()
            (workspace / ".env").write_text("not-a-secret", encoding="utf-8")
            with self.assertRaisesRegex(DshError, "Workspace .env"):
                DshRunner({"home": str(home)}, authorized=True).run("hi", workspace, root / "logs")

    def test_authorized_adapter_with_fake_process_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, workspace, logs = root / "home", root / "workspace", root / "logs"
            for path in (home, workspace, logs):
                path.mkdir()
            stdout = logs / "stdout.log"
            stdout.write_text("\n".join(json.dumps(e) for e in self.events()), encoding="utf-8")
            fake = ProcessResult(0, False, False, stdout, logs / "stderr.log")
            cfg = {"home": str(home), "budget_enforced_externally": True, "model": "operator-verified", "verified_version": "test-fixture"}
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fake-not-a-secret", "GITHUB_TOKEN": "must-not-inherit"}), patch("lab_harness.dsh.run_process", return_value=fake) as process:
                result = DshRunner(cfg, authorized=True).run("task through stdin", workspace, logs, session_id="session-1")
            self.assertEqual(result["session_id"], "session-1")
            self.assertEqual(process.call_args.kwargs["stdin"], b"task through stdin")
            self.assertNotIn("GITHUB_TOKEN", process.call_args.kwargs["environment"])
            self.assertEqual(process.call_args.kwargs["environment"]["DSH_HOME"], str(home))

    def test_command_prompt_only_via_stdin(self):
        command = DshRunner({"command": ["node.exe", "official.mjs"]}).command("session-1")
        self.assertEqual(command[-1], "-")
        self.assertIn("workspace-write", command)
        self.assertEqual(command[command.index("--approval-policy") + 1], "never")

    def test_doctor_does_not_spawn(self):
        report = DshRunner({"command": ["missing-lab-cli.exe"]}).doctor()
        self.assertFalse(report["available"])
        self.assertFalse(report["sends_model_request"])
        self.assertEqual(report["help_command"], ["missing-lab-cli.exe", "--profile", "headless", "--help"])


if __name__ == "__main__":
    unittest.main()
