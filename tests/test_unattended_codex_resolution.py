"""Desktop CLI updates must not strand a fixed planning/review checkpoint."""
import errno
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from github_harness_lab.lab_harness.process import ProcessResult
from unattended.providers import AgentProviders, ProviderError, _codex_command
from unattended import config
from tests.test_unattended_providers import CODEX_EVENTS, PLAN, REVIEW, write_events


class CodexResolutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.local = self.root / "Local"
        self.bin = self.local / "OpenAI/Codex/bin"
        self.bin.mkdir(parents=True)
        self.old = self.bin / ("1" * 16) / "codex.exe"
        self.old.parent.mkdir()
        self.environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.local)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.provider = AgentProviders({"codex": {
            "command": [str(self.old), "--existing-option"], "model": "gpt-6-astra", "reasoning_effort": "high"}})

    def candidate(self, name="2" * 16, *, mtime=2_000_000_000):
        path = self.bin / name / "codex.exe"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"fixture only; never execute")
        os.utime(path, ns=(mtime, mtime))
        return path

    def fake_process(self, argv, **kwargs):
        out = kwargs["output_dir"]
        write_events(out / "stdout.log", CODEX_EVENTS)
        (out / "stderr.log").write_text("", encoding="utf-8")
        result = REVIEW if b"Review" in kwargs["stdin"] else PLAN
        Path(argv[argv.index("-o") + 1]).write_text(json.dumps(result), encoding="utf-8")
        return ProcessResult(0, False, False, out / "stdout.log", out / "stderr.log")

    def test_existing_configured_version_is_preserved_even_with_newer_sibling(self):
        self.old.write_bytes(b"configured fixture")
        self.candidate(mtime=3_000_000_000)
        self.assertEqual(_codex_command([str(self.old), "--existing-option"]),
                         [str(self.old), "--existing-option"])

    @patch("unattended.providers.run_process")
    def test_removed_version_resolves_sibling_for_both_roles_preserving_models_and_arguments(self, process):
        current = self.candidate()
        process.side_effect = self.fake_process
        for role, prompt in (("plan", "Plan"), ("review", "Review")):
            with self.subTest(role=role):
                result = getattr(self.provider, role)(prompt, self.root, self.root / role, 20)
                self.assertEqual(result, REVIEW if role == "review" else PLAN)
                argv = process.call_args.args[0]
                self.assertEqual(argv[:2], [str(current), "--existing-option"])
                self.assertEqual(argv[argv.index("--model") + 1], "gpt-6-astra")
                self.assertEqual(argv[argv.index("-c") + 1], 'model_reasoning_effort="high"')
                self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(self.provider.doctor()["codex_command"][0], str(current))
        self.assertEqual(self.provider.config["codex"]["command"][0], str(self.old))

    @patch("unattended.providers.run_process")
    def test_temporarily_empty_install_waits_without_launch_and_doctor_remains_read_only(self, process):
        with self.assertRaises(ProviderError) as caught:
            self.provider.review("Review", self.root, self.root / "out", 20)
        self.assertTrue(caught.exception.transient)
        self.assertEqual(caught.exception.retry_reason, "local_cli_unavailable")
        self.assertFalse(self.provider.doctor()["codex_available"])
        process.assert_not_called()

    @patch("unattended.providers.run_process")
    def test_custom_missing_command_stays_real_failure_and_launch_facts_are_safe(self, process):
        self.candidate()
        custom = self.root / "custom/codex.exe"
        self.provider.config["codex"]["command"] = [str(custom)]
        process.side_effect = FileNotFoundError(errno.ENOENT, "secret-value-in-original-error")
        out = self.root / "custom-out"
        with self.assertRaises(ProviderError) as caught:
            self.provider.review("Review", self.root, out, 20)
        self.assertFalse(caught.exception.transient)
        self.assertEqual(process.call_args.args[0][0], str(custom))
        raw = (out / "process.json").read_text(encoding="utf-8")
        record = json.loads(raw)
        self.assertEqual(record["launch_error"]["type"], "FileNotFoundError")
        self.assertEqual(record["launch_error"]["errno"], errno.ENOENT)
        self.assertNotIn("secret-value-in-original-error", raw)

    def test_multiple_candidates_choose_unique_newest_and_ambiguous_times_wait(self):
        self.candidate("2" * 16, mtime=2_000_000_000)
        current = self.candidate("3" * 16, mtime=3_000_000_000)
        self.candidate("not-a-version", mtime=9_000_000_000)
        self.assertEqual(_codex_command([str(self.old)]), [str(current)])
        self.candidate("4" * 16, mtime=3_000_000_000)
        with self.assertRaises(ProviderError) as caught:
            _codex_command([str(self.old)])
        self.assertEqual(caught.exception.retry_reason, "local_cli_unavailable")

    def test_lookalike_or_outside_paths_do_not_discover_other_executables(self):
        self.candidate()
        paths = [self.bin / "short" / "codex.exe", self.local / "Other/Codex/bin" / ("1" * 16) / "codex.exe",
                 self.old.with_name("different.exe"), Path("codex.exe")]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(_codex_command([str(path)]), [str(path)])

    def test_linked_candidates_are_ignored_and_linked_original_is_rejected(self):
        linked = self.bin / ("2" * 16)
        target = self.root / "outside"
        target.mkdir()
        (target / "codex.exe").write_bytes(b"outside fixture")
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(linked))
        else:
            linked.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ProviderError) as caught:
            _codex_command([str(self.old)])
        self.assertTrue(caught.exception.transient)
        self.provider.config["codex"]["command"] = [str(linked / "codex.exe")]
        with self.assertRaises(ProviderError) as unsafe:
            self.provider.doctor()
        self.assertFalse(unsafe.exception.transient)

    @patch("unattended.providers.run_process")
    def test_update_between_resolution_and_launch_is_temporary_with_safe_failure_record(self, process):
        current = self.candidate()
        def disappeared(argv, **kwargs):
            self.assertEqual(argv[0], str(current))
            current.unlink()
            raise FileNotFoundError(errno.ENOENT, "private text is never persisted")
        process.side_effect = disappeared
        out = self.root / "race"
        with self.assertRaises(ProviderError) as caught:
            self.provider.review("Review", self.root, out, 20)
        self.assertTrue(caught.exception.transient)
        self.assertEqual(caught.exception.retry_reason, "local_cli_unavailable")
        raw = (out / "process.json").read_text(encoding="utf-8")
        record = json.loads(raw)
        self.assertTrue(record["launch_or_capture_failed"])
        self.assertEqual(record["retry_reason"], "local_cli_unavailable")
        self.assertEqual(record["launch_error"]["errno"], errno.ENOENT)
        self.assertNotIn("private text", raw)

    @patch("unattended.providers.run_process")
    def test_config_doctor_uses_same_resolution_and_safely_reports_missing_or_invalid_cli(self, process):
        current = self.candidate()
        value = {"project_path": str(self.root), "config_path": str(self.root / "config.json"),
                 "codex": self.provider.config["codex"], "dsh": {}, "checks": [], "schedule": {}}
        with patch.object(config.gitops, "head", return_value="a" * 40), \
             patch.object(config.gitops, "git", return_value="identity"), \
             patch.object(config.shutil, "which", return_value=None):
            self.assertTrue(config.doctor(value)["checks"]["codex_available"])
            self.assertEqual(value["codex"]["command"][0], str(self.old))
            current.unlink()
            self.assertFalse(config.doctor(value)["checks"]["codex_available"])
            value["codex"]["command"] = ["codex.cmd"]
            self.assertFalse(config.doctor(value)["checks"]["codex_available"])
        process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
