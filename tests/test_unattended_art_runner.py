"""Single-invocation art runner locking and durable outcomes, with no model calls."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

from art_tools import run_claude_preview as runner
from unattended.providers import ProviderError


class ArtRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        (self.root / "workspace").mkdir()
        self.config = {"runtime_dir": str(self.root), "coding_provider": "claude",
                       "art_pipeline": {"task_ids": ["TASK-ART-0001"], "artifacts_dir": str(self.artifacts)}}
        (self.root / "provider.json").write_text(json.dumps(self.config), encoding="utf-8-sig")
        self.provider = Mock()
        self.provider.code.return_value = {"completed": True, "session_id": "550e8400-e29b-41d4-a716-446655440000",
                                           "text": "Cached preview still awaits visual acceptance."}
        self.factory = Mock(return_value=self.provider)

    def invoke(self):
        with redirect_stdout(io.StringIO()):
            return runner.run(self.root, provider_factory=self.factory)

    def state(self):
        return json.loads((self.root / "state.json").read_text(encoding="utf-8-sig"))

    def prepared_preview(self):
        state = {"stage": "pending_visual_acceptance", "review_package": str(self.artifacts / "review-package.json")}
        (self.artifacts / "pipeline-state.json").write_text(json.dumps(state), encoding="utf-8-sig")

    def test_bom_config_and_state_are_read_without_model_retry(self):
        previous = {"phase": "ART_REWORK", "operation": {"output_dir": "historical-run"}}
        (self.root / "state.json").write_text(json.dumps(previous), encoding="utf-8-sig")
        self.prepared_preview()
        state = self.invoke()
        self.factory.assert_called_once_with(self.config)
        self.provider.code.assert_called_once()
        self.assertEqual(state["phase"], "AWAITING_ART_APPROVAL")
        self.assertEqual(json.loads(Path(state["previous_state_path"]).read_text(encoding="utf-8")), previous)
        out = Path(state["operation"]["output_dir"])
        self.assertEqual(json.loads((out / "runner-state.json").read_text(encoding="utf-8")), self.state())
        self.assertTrue(state["operation"]["completed"])

    def test_explicit_fresh_invocation_preserves_older_runs_and_cached_files(self):
        self.prepared_preview()
        cached = self.artifacts / "cached-model.glb"
        cached.write_bytes(b"unchanged cached asset")
        before = cached.read_bytes(), cached.stat().st_mtime_ns
        first = self.invoke()
        first_dir = Path(first["operation"]["output_dir"])
        old_result = (first_dir / "runner-result.json").read_bytes()
        old_state = (first_dir / "runner-state.json").read_bytes()
        second = self.invoke()
        self.assertEqual(self.provider.code.call_count, 2)
        self.assertNotEqual(first["operation"]["output_dir"], second["operation"]["output_dir"])
        self.assertEqual((first_dir / "runner-result.json").read_bytes(), old_result)
        self.assertEqual((first_dir / "runner-state.json").read_bytes(), old_state)
        self.assertEqual(json.loads(Path(second["previous_state_path"]).read_text(encoding="utf-8")), first)
        self.assertEqual((cached.read_bytes(), cached.stat().st_mtime_ns), before)
        self.assertEqual(len(list((self.root / "runs").iterdir())), 2)

    def test_provider_failure_is_persisted_once_without_exception_secrets(self):
        self.provider.code.side_effect = ProviderError("sensitive detail must not reach state", session_id="saved-session")
        with self.assertRaises(ProviderError):
            self.invoke()
        self.provider.code.assert_called_once()
        state = self.state()
        self.assertEqual(state["phase"], "ART_FAILED")
        self.assertEqual(state["operation"]["status"], "failed")
        self.assertFalse(state["operation"]["completed"])
        self.assertEqual(state["task"]["session_id"], "saved-session")
        self.assertEqual(state["last_error"]["type"], "ProviderError")
        failure = Path(state["operation"]["output_dir"]) / "runner-failure.json"
        self.assertTrue(failure.is_file())
        self.assertNotIn("sensitive detail", failure.read_text(encoding="utf-8"))
        self.assertNotIn("sensitive detail", json.dumps(state))
        with runner.runner_lock(self.root):
            pass

    def test_keyboard_interrupt_is_recorded_and_lock_is_released(self):
        self.provider.code.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.invoke()
        self.assertEqual(self.state()["phase"], "ART_INTERRUPTED")
        self.provider.code.assert_called_once()
        with runner.runner_lock(self.root):
            pass

    def test_finished_provider_without_finished_production_remains_rework(self):
        state = self.invoke()
        self.assertEqual(state["phase"], "ART_REWORK")
        self.assertEqual(state["production_result"], {})
        self.assertTrue(state["operation"]["completed"])

    def test_incomplete_provider_cannot_reuse_old_success_stage(self):
        self.prepared_preview()
        self.provider.code.return_value = {"completed": False, "text": "Incomplete turn"}
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.assertEqual(self.state()["phase"], "ART_FAILED")
        self.provider.code.assert_called_once()

    def test_wrong_runtime_does_not_overwrite_existing_state(self):
        prior = b'{"phase":"ART_REWORK"}'
        (self.root / "state.json").write_bytes(prior)
        self.config["runtime_dir"] = str(self.root / "another-runtime")
        (self.root / "provider.json").write_text(json.dumps(self.config), encoding="utf-8-sig")
        with self.assertRaises(ValueError):
            self.invoke()
        self.factory.assert_not_called()
        self.assertEqual((self.root / "state.json").read_bytes(), prior)
        self.assertFalse((self.root / "runs").exists())

    def test_second_process_cannot_rewrite_owned_state_or_create_a_run(self):
        prior = b'{"phase":"CODE","operation":{"marker":"owned"}}'
        (self.root / "state.json").write_bytes(prior)
        program = """
import sys
from art_tools.run_claude_preview import run, RunnerBusy
try:
    run(sys.argv[1], provider_factory=lambda _: (_ for _ in ()).throw(AssertionError('provider must not start')))
except RunnerBusy:
    raise SystemExit(19)
raise SystemExit(1)
"""
        with runner.runner_lock(self.root):
            result = subprocess.run([sys.executable, "-c", program, str(self.root)],
                                    cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=10,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(result.returncode, 19, result.stderr.decode(errors="replace"))
        self.assertEqual((self.root / "state.json").read_bytes(), prior)
        self.assertFalse((self.root / "runs").exists())

    def test_abrupt_process_exit_releases_runner_lock_for_next_explicit_run(self):
        program = """
import os, sys
from art_tools.run_claude_preview import runner_lock
with runner_lock(sys.argv[1]):
    os._exit(23)
"""
        result = subprocess.run([sys.executable, "-c", program, str(self.root)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(result.returncode, 23, result.stderr.decode(errors="replace"))
        state = self.invoke()
        self.assertEqual(state["phase"], "ART_REWORK")
        self.provider.code.assert_called_once()


if __name__ == "__main__":
    unittest.main()
