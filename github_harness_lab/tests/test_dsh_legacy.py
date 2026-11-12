import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab_harness.dsh import DshError
from lab_harness.dsh_legacy import LegacyDshRunner, parse_legacy_events, VERIFIED_VERSION
from lab_harness.process import ProcessResult


class LegacyProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = [{"type": "session", "sessionId": "session-fixture"},
                       {"type": "status", "phase": "turn_start", "turn": 1},
                       {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}},
                       {"type": "final", "text": "fixture output"}]

    def parse(self, events=None, **kwargs):
        path = self.root / "events.ndjson"
        path.write_text("\n".join(json.dumps(e) for e in (self.events if events is None else events)), encoding="utf-8")
        return parse_legacy_events(path, exit_code=kwargs.pop("exit_code", 0), **kwargs)

    def test_completed_legacy_stream_uses_machine_turn_reason(self):
        result = self.parse()
        self.assertEqual(result["final_kind"], "completed")
        self.assertEqual(result["completion_source"], "status.turn_end.reason")

    def test_final_text_alone_is_not_completion(self):
        with self.assertRaises(DshError):
            self.parse([self.events[0], self.events[-1]])

    def test_failure_reason_rejects_zero_exit_code(self):
        self.events[2]["reason"]["kind"] = "error"
        with self.assertRaises(DshError):
            self.parse()

    def test_nonzero_exit_rejects_completed_turn(self):
        with self.assertRaises(DshError):
            self.parse(exit_code=1)

    def test_missing_final_is_rejected(self):
        with self.assertRaises(DshError):
            self.parse(self.events[:-1])

    def test_missing_or_mismatched_turn_start_is_rejected(self):
        with self.assertRaises(DshError):
            self.parse([self.events[0], *self.events[2:]])
        self.events[2]["turn"] = 2
        with self.assertRaises(DshError):
            self.parse()

    def test_second_incomplete_turn_invalidates_prior_completed_turn(self):
        with self.assertRaises(DshError):
            self.parse(self.events[:-1] + [{"type": "status", "phase": "turn_start", "turn": 2}, self.events[-1]])

    def test_error_event_and_post_final_data_are_rejected(self):
        for extra in ({"type": "error", "message": "fixture"}, {"type": "text", "text": "late"}):
            with self.assertRaises(DshError):
                self.parse(self.events + [extra])
        with self.assertRaises(DshError):
            self.parse([self.events[0], {"type": "error"}, *self.events[1:]])

    def test_duplicate_session_and_truncated_status_are_rejected(self):
        with self.assertRaises(DshError):
            self.parse([self.events[0], *self.events])
        self.events[2]["truncated"] = True
        with self.assertRaises(DshError):
            self.parse()

    def test_resume_requires_same_session(self):
        with self.assertRaises(DshError):
            self.parse(requested_session="someone-else")
        self.assertEqual(self.parse(requested_session="session-fixture")["session_id"], "session-fixture")

    def test_sending_and_environment_gates_precede_subprocess(self):
        configurations = [({}, False), ({}, True), ({"verified_version": VERIFIED_VERSION}, True),
                          ({"verified_version": VERIFIED_VERSION, "isolated_environment_verified": True}, True)]
        with patch("lab_harness.dsh_legacy.run_process") as process:
            for config, authorized in configurations:
                with self.assertRaises(DshError):
                    LegacyDshRunner(config, authorized=authorized).run("fixture", self.root, self.root / "output")
            process.assert_not_called()

    def test_authorized_compatibility_contract_uses_only_fake_process(self):
        install = self.root / "desktop"
        (install / "resources").mkdir(parents=True)
        (install / "DeepSeek Harness.exe").touch()
        (install / "resources/app.asar").touch()
        home = self.root / "home"
        home.mkdir()
        workspace = self.root / "work"
        workspace.mkdir()
        output = self.root / "output"
        output.mkdir()
        stream = output / "stdout.log"
        stream.write_text("\n".join(json.dumps(e) for e in self.events))
        err = output / "stderr.log"
        err.touch()
        configuration = {"verified_version": VERIFIED_VERSION, "isolated_environment_verified": True,
                         "budget_enforced_externally": True, "desktop_install": str(install),
                         "home": str(home), "model": "offline-fixture"}
        with patch("lab_harness.dsh_legacy.run_process", return_value=ProcessResult(0, False, False, stream, err)) as process:
            result = LegacyDshRunner(configuration, authorized=True).run("fictional prompt", workspace, output)
        argv = process.call_args.args[0]
        self.assertEqual(argv[-4:], ["--profile", "headless", "--json", "-"])
        self.assertNotIn("fictional prompt", argv)
        self.assertNotIn("--permission-mode", argv)
        self.assertEqual(process.call_args.kwargs["stdin"], b"fictional prompt")
        self.assertEqual(process.call_args.kwargs["environment"]["ELECTRON_RUN_AS_NODE"], "1")
        self.assertEqual(result["completion_source"], "status.turn_end.reason")
        policy = (output / "invocation-policy.yml").read_text()
        self.assertIn("mode: workspace-write", policy)
        self.assertIn("policy: never", policy)
        self.assertIn("id: llm-retry\n  disabled: true", policy)


if __name__ == "__main__":
    unittest.main()
