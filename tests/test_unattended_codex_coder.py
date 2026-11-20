"""Native Codex coding keeps its own thread, subscription and writable policy."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from github_harness_lab.lab_harness.process import ProcessResult
from unattended import providers
from unattended.providers import _codex_coding_profile as build_profile

SESSION = "550e8400-e29b-41d4-a716-446655440000"
OTHER = "550e8400-e29b-41d4-a716-446655440001"


class CodexCoderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.out = self.root / "code"
        self.config = {"coding_provider": "codex", "codex": {"command": ["codex.exe"], "model": "review-only-model",
                       "auth_mode": "api_key", "coding_model": "gpt-6.2-sol", "coding_reasoning_effort": "high"}}
        self.provider = providers.AgentProviders(self.config)
        self.addCleanup(patch.stopall)
        patch.object(providers, "_codex_coding_profile", return_value=(["-c", 'default_permissions="fixture-coding"'], [str(self.root / ".git")])).start()
        patch.object(providers.AgentProviders, "_check_codex_subscription").start()
        self.launch = patch.object(providers, "run_process", side_effect=self.capture).start()
        self.session, self.exit_code = SESSION, 0

    def capture(self, argv, **kwargs):
        out = kwargs["output_dir"]
        rows = [{"type": "thread.started", "thread_id": self.session}, {"type": "turn.started"}]
        if self.exit_code:
            rows.append({"type": "turn.failed", "error": {"code": "ECONNRESET", "message": "connection reset"}})
        else:
            rows += [{"type": "item.completed", "item": {"type": "agent_message", "text": "Implemented and verified."}},
                     {"type": "turn.completed", "usage": {}}]
        (out / "stdout.log").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        (out / "stderr.log").write_text("", encoding="utf-8")
        return ProcessResult(self.exit_code, False, False, out / "stdout.log", out / "stderr.log")

    def test_regular_code_uses_native_subscription_and_dedicated_coding_model(self):
        self.config["codex"]["windows_sandbox"] = "unelevated"
        with patch.dict(os.environ, {"CODEX_API_KEY": "private-api-key", "OPENAI_API_KEY": "private-openai-key"}):
            result = self.provider.code("Implement the task", self.root, self.out, 60)
        self.assertEqual(result, {"session_id": SESSION, "text": "Implemented and verified.", "completed": True})
        argv = self.launch.call_args.args[0]
        self.assertEqual(argv[:6], ["codex.exe", "--no-daemon", "-a", "never", "exec", "-c"])
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-6.2-sol")
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertEqual('windows.sandbox="unelevated"' in argv, os.name == "nt")
        self.assertIn('forced_login_method="chatgpt"', argv)
        self.assertNotIn("resume", argv)
        environment = self.launch.call_args.kwargs["environment"]
        self.assertNotIn("CODEX_API_KEY", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)
        receipt = json.loads((self.out / "process.json").read_text())
        self.assertEqual(receipt["coding_provider"], "codex")
        self.assertEqual(receipt["coding_protocol"], "codex_exec_v1")
        self.assertEqual(receipt["coding_sandbox"], "workspace-write")
        self.assertEqual(receipt["codex_auth_mode"], "chatgpt")
        self.assertEqual(providers.AgentProviders({}).recover("code", self.out), result)
        (self.out / "final.txt").unlink()
        (self.out / "result.json").unlink()
        self.assertEqual(providers.AgentProviders({}).recover("code", self.out), result)
        self.assertEqual(self.launch.call_count, 1)

    def test_resume_uses_exact_thread_and_foreign_response_is_rejected(self):
        result = self.provider.code("Continue task", self.root, self.out, 60, session_id=SESSION)
        argv = self.launch.call_args.args[0]
        self.assertIn("resume", argv)
        self.assertEqual(argv[-2:], [SESSION, "-"])
        self.assertNotIn("--last", argv)
        self.assertEqual(result["session_id"], SESSION)
        self.out = self.root / "foreign"
        self.session = OTHER
        with self.assertRaises(providers.ProviderError):
            self.provider.code("Continue task", self.root, self.out, 60, session_id=SESSION)
        self.assertIsNone(providers.read_session(self.out))

    def test_managed_task_cannot_reuse_foreign_or_retired_session(self):
        runtime = self.root / "runtime"
        work = runtime / "workspace"
        work.mkdir(parents=True)
        self.out = runtime / "runs" / "code-current"
        self.config["runtime_dir"] = str(runtime)
        state = {"phase": "CODE", "task": {"id": "TASK-1", "coding_provider": "codex", "session_id": OTHER,
                 "session_owner_task_id": "TASK-1"}, "operation": {"kind": "code", "task_id": "TASK-1",
                 "output_dir": str(self.out), "requested_session_id": SESSION}}
        (runtime / "state.json").write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(providers.ProviderError, "active managed task"):
            self.provider.code("Continue", work, self.out, 60, session_id=SESSION)
        self.launch.assert_not_called()

    def test_failed_native_turn_keeps_session_and_transient_transport_classification(self):
        self.exit_code = 1
        with self.assertRaises(providers.ProviderError) as caught:
            self.provider.code("Code", self.root, self.out, 60)
        self.assertTrue(caught.exception.transient)
        self.assertEqual(caught.exception.session_id, SESSION)
        self.assertEqual(providers.classify_transient_failure("code", self.out), {"reason": "transport"})
        self.assertIsNone(self.provider.recover("code", self.out))

    def test_saved_final_response_cannot_override_native_turn(self):
        self.provider.code("Code", self.root, self.out, 60)
        (self.out / "final.txt").write_text("Different final response", encoding="utf-8")
        with self.assertRaises(providers.ProviderError):
            self.provider.recover("code", self.out)

    def test_profile_grants_only_supported_workspace_and_exact_git_paths(self):
        admin = self.root / "admin.git" / "worktrees" / "current"
        common = self.root / "admin.git"
        admin.mkdir(parents=True)
        with patch.object(providers.subprocess, "run", side_effect=[
                SimpleNamespace(returncode=0, stdout=str(admin).encode()),
                SimpleNamespace(returncode=0, stdout=str(common).encode())]):
            args, roots = build_profile(self.root, self.out, {})
        self.assertEqual(roots, [admin.as_posix(), common.as_posix()])
        filesystem = next(value for value in args if ".filesystem=" in value)
        self.assertIn('":workspace_roots"="write"', filesystem)
        self.assertIn(json.dumps(admin.as_posix()) + '="write"', filesystem)
        self.assertIn(json.dumps(common.as_posix()) + '="write"', filesystem)
        self.assertNotIn(":workspace_roots/.git", filesystem)
        self.assertNotIn('":root"="write"', filesystem)

    def test_missing_final_message_and_conflicting_observed_model_are_rejected(self):
        capture = self.capture
        for label in ("missing-message", "wrong-model"):
            self.out = self.root / label
            def invalid(argv, **kwargs):
                result = capture(argv, **kwargs)
                path = kwargs["output_dir"] / "stdout.log"
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                if label == "missing-message":
                    rows = [row for row in rows if row["type"] != "item.completed"]
                else:
                    rows[0]["model"] = "different-model"
                path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
                return result
            self.launch.side_effect = invalid
            with self.subTest(label=label), self.assertRaises(providers.ProviderError):
                self.provider.code("Code", self.root, self.out, 60)

    def test_known_credentials_are_redacted_without_being_forwarded_to_native_coder(self):
        central = self.root / "secrets.local.json"
        central.write_text(json.dumps({"CODEX_API_KEY": "current-private-test-key",
                                       "CODEX_API_KEY_BACKUPS": ["archived-private-test-key"]}), encoding="utf-8")
        self.config["secrets_file"] = str(central)
        self.provider.code("Code", self.root, self.out, 60)
        launch = self.launch.call_args.kwargs
        self.assertIn("current-private-test-key", launch["secrets"])
        self.assertIn("archived-private-test-key", launch["secrets"])
        self.assertNotIn("current-private-test-key", launch["environment"].values())


if __name__ == "__main__":
    unittest.main()
