"""Asset production is an exact, opt-in command scoped to a managed Claude task."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

from github_harness_lab.lab_harness.process import ProcessResult
from tests.test_unattended_providers import write_events
from unattended.config import load_config, validate_art_pipeline_settings
from unattended.providers import AgentProviders, ProviderError, _claude_art_preflight


class ArtIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "original"
        self.runtime = self.root / "runtime"
        self.workspace = self.runtime / "workspace"
        self.out = self.runtime / "runs" / "code-art"
        self.artifacts = self.root / "public-artifacts"
        self.batch = self.root / "approved" / "batch.json"
        self.script = self.root / "trusted-tools" / "art_pipeline.py"
        for directory in (self.project, self.workspace, self.out.parent, self.artifacts,
                          self.batch.parent, self.script.parent):
            directory.mkdir(parents=True, exist_ok=True)
        self.batch_config = {"artifacts_dir": str(self.artifacts), "private_state_dir": str(self.root / "private"),
                             "key_file": str(self.root / "secrets" / "tripo.txt")}
        self.batch.write_text(json.dumps(self.batch_config), encoding="utf-8")
        self.script.write_text("raise AssertionError('must never execute in this test')", encoding="utf-8")
        self.settings = {"config_file": str(self.batch), "artifacts_dir": str(self.artifacts),
                         "task_ids": ["TASK-ART-0001"]}
        self.config = {
            "version": 1, "goal": "Produce the approved three-level preview", "branch": "unattended/art-test",
            "project_path": str(self.project), "runtime_dir": str(self.runtime),
            "coding_provider": "claude", "claude": {"command": ["claude.exe"]},
            "codex": {"command": ["codex.exe"]}, "art_pipeline": self.settings,
        }
        self.state = {"phase": "CODE", "task": {"id": "TASK-ART-0001"},
                      "operation": {"kind": "code", "task_id": "TASK-ART-0001", "output_dir": str(self.out)}}
        self.save_state()
        self.script_patch = patch("unattended.providers._ART_PIPELINE_SCRIPT", self.script)
        self.script_patch.start()
        self.addCleanup(self.script_patch.stop)

    def save_state(self):
        (self.runtime / "state.json").write_text(json.dumps(self.state), encoding="utf-8")

    def preview(self, config=None, cwd=None, out=None):
        return _claude_art_preflight(config or self.config, cwd or self.workspace, out or self.out)

    def test_no_configuration_preserves_existing_behavior(self):
        config = copy.deepcopy(self.config)
        del config["art_pipeline"]
        self.assertEqual(self.preview(config, self.root, self.root / "unmanaged"), ([], "", []))

    def test_config_load_is_opt_in_and_does_not_read_credentials(self):
        path = self.root / "project.json"
        path.write_text(json.dumps(self.config), encoding="utf-8")
        loaded = load_config(path)
        self.assertEqual(loaded["art_pipeline"], self.settings)
        self.assertEqual(json.loads(self.batch.read_text(encoding="utf-8")), self.batch_config)
        self.assertFalse(Path(self.batch_config["key_file"]).exists())
        self.config["coding_provider"] = "dsh"
        self.config["dsh"] = {"command": ["dsh.exe"]}
        path.write_text(json.dumps(self.config), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "coding_provider"):
            load_config(path)

    def test_rejects_wildcards_duplicates_relative_paths_and_additional_commands(self):
        for override in ({"task_ids": []}, {"task_ids": ["TASK-ART-*"]},
                         {"task_ids": ["TASK-ART-0001", "TASK-ART-0001"]},
                         {"task_ids": "TASK-ART-0001"}, {"config_file": "batch.json"},
                         {"config_file": str(self.root / "private" / ".." / "batch.json")},
                         {"artifacts_dir": str(self.root / "art*")},
                         {"argv": ["python.exe", "unrestricted.py"]}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                validate_art_pipeline_settings({**self.settings, **override}, project=self.project, runtime=self.runtime)

    def test_rejects_writable_config_or_broad_evidence_directory(self):
        for override in ({"config_file": str(self.workspace / "batch.json")},
                         {"config_file": str(self.project / "batch.json")},
                         {"config_file": str(self.artifacts / "batch.json")},
                         {"artifacts_dir": str(self.workspace / "Saved")},
                         {"artifacts_dir": str(self.runtime)}, {"artifacts_dir": str(self.root)},
                         {"artifacts_dir": str(self.project)}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                validate_art_pipeline_settings({**self.settings, **override}, project=self.project, runtime=self.runtime)

    def test_config_read_grant_requires_a_dedicated_single_file_directory(self):
        (self.batch.parent / "unrelated-secret.txt").write_text("must not be granted", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "专用目录"):
            validate_art_pipeline_settings(self.settings, project=self.project, runtime=self.runtime)
        with self.assertRaises(ProviderError):
            self.preview()

    def test_private_paths_cannot_overlap_any_additional_read_directory(self):
        directories = [self.artifacts, self.script.parent, Path(sys.executable).resolve().parent, self.batch.parent]
        for field in ("private_state_dir", "key_file"):
            for directory in directories:
                candidate = {**self.batch_config, field: str(directory / "private-data")}
                self.batch.write_text(json.dumps(candidate), encoding="utf-8")
                with self.subTest(field=field, directory=str(directory)), self.assertRaises(ProviderError):
                    self.preview()
        self.batch.write_text(json.dumps(self.batch_config), encoding="utf-8")

    def test_batch_and_bridge_must_describe_the_same_public_artifacts(self):
        self.batch.write_text(json.dumps({**self.batch_config, "artifacts_dir": str(self.root / "other-artifacts")}), encoding="utf-8")
        with self.assertRaisesRegex(ProviderError, "differs"):
            self.preview()

    def test_non_art_task_gets_no_command_or_additional_directory(self):
        self.state["task"]["id"] = self.state["operation"]["task_id"] = "TASK-LOCAL-0099"
        self.save_state()
        self.assertEqual(self.preview(), ([], "", []))

    def test_operation_must_bind_task_phase_workspace_and_log_directory(self):
        for field, value in (("phase", "CHECK"), ("kind", "check"), ("task_id", "TASK-ART-0002"),
                             ("output_dir", str(self.runtime / "runs" / "another-operation"))):
            state = copy.deepcopy(self.state)
            if field == "phase":
                self.state[field] = value
            else:
                self.state["operation"][field] = value
            self.save_state()
            with self.subTest(field=field), self.assertRaises(ProviderError):
                self.preview()
            self.state = state
        self.save_state()
        with self.assertRaises(ProviderError):
            self.preview(cwd=self.project)
        with self.assertRaises(ProviderError):
            self.preview(out=self.root / "unmanaged")

    def test_exact_command_and_review_instructions_without_private_contents(self):
        before = (self.runtime / "state.json").read_bytes(), self.batch.read_bytes()
        commands, prompt, directories = self.preview()
        self.assertEqual(len(commands), 1)
        self.assertEqual(shlex.split(commands[0]), [Path(sys.executable).resolve().as_posix(),
                         self.script.as_posix(), "--config", self.batch.as_posix(), "preview"])
        self.assertEqual(directories, [self.artifacts, self.script.parent, Path(sys.executable).resolve().parent, self.batch.parent])
        for requirement in ("3000", "三个住宅等级", "其余五级", "CHECK", "不能修改 Content", "密钥"):
            self.assertIn(requirement, prompt)
        self.assertNotIn("1200", prompt)
        self.assertNotIn("private batch contents", prompt)
        self.assertEqual(before, ((self.runtime / "state.json").read_bytes(), self.batch.read_bytes()))

    def test_missing_private_batch_or_public_output_fails_closed(self):
        self.batch.unlink()
        with self.assertRaises(ProviderError):
            self.preview()

    def test_linked_artifacts_are_not_granted(self):
        child = self.artifacts / "linked-evidence"
        child.write_text("evidence", encoding="utf-8")
        from unattended import providers
        original = providers._reparse
        with patch("unattended.providers._reparse", side_effect=lambda path: path == child or original(path)):
            with self.assertRaisesRegex(ProviderError, "linked artifact"):
                self.preview()

    @patch("unattended.providers.claude_subscription_ready", return_value=True)
    @patch("unattended.providers.run_process")
    def test_claude_receives_only_fixed_command_and_read_only_public_directory(self, launch, auth):
        session = "550e8400-e29b-41d4-a716-446655440000"
        def capture(*args, **kwargs):
            self.out.mkdir(exist_ok=True)
            write_events(self.out / "stdout.log", [
                {"type": "system", "subtype": "init", "session_id": session},
                {"type": "result", "subtype": "success", "session_id": session,
                 "is_error": False, "result": "Asset preview needs visual confirmation."},
            ])
            (self.out / "stderr.log").write_text("", encoding="utf-8")
            return ProcessResult(0, False, False, self.out / "stdout.log", self.out / "stderr.log")
        launch.side_effect = capture
        AgentProviders(self.config).code("Produce the approved preview.", self.workspace, self.out, 120)
        argv = launch.call_args.args[0]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "default")
        self.assertNotIn("Bash(git add *)", argv)
        self.assertNotIn("Bash(git commit *)", argv)
        permissions = json.loads(argv[argv.index("--settings") + 1])["permissions"]
        expected_directories = [self.artifacts, self.script.parent, Path(sys.executable).resolve().parent, self.batch.parent]
        self.assertEqual(permissions["additionalDirectories"], [str(directory) for directory in expected_directories])
        self.assertTrue(permissions["blockReadsOutsideWorkingDirectories"])
        for directory in expected_directories:
            pattern = directory.as_posix()
            if directory.drive:
                pattern = "/" + directory.drive[0].lower() + pattern[2:]
            self.assertIn(f"Edit(/{pattern})", permissions["deny"])
            self.assertIn(f"Edit(/{pattern}/**)", permissions["deny"])
        commands, _, _ = self.preview()
        self.assertIn("Bash(" + commands[0] + ")", argv)
        self.assertNotIn("Bash(python *)", argv)
        self.assertNotIn("Bash(*)", argv)
        self.assertIn("mcp__*", argv)
        self.assertNotIn(str(self.runtime), permissions["additionalDirectories"])
        self.assertNotIn(str(Path(self.batch_config["key_file"]).parent), permissions["additionalDirectories"])
        self.assertNotIn(self.batch_config["private_state_dir"], permissions["additionalDirectories"])
        self.assertNotIn("private batch contents", launch.call_args.kwargs["stdin"].decode())

    @patch("unattended.providers.claude_subscription_ready", return_value=True)
    @patch("unattended.providers.run_process")
    def test_blender_job_and_recipe_receive_exact_edit_rules_and_only_fixed_preview(self, launch, auth):
        recipe, job = self.workspace / "housing-recipe.json", self.workspace / "housing-job.py"
        recipe.write_text("{}", encoding="utf-8")
        job.write_text("def run_job(context):\n    return {}\n", encoding="utf-8")
        self.batch_config.update(recipe_file=str(recipe), blender_job_file=str(job))
        self.batch.write_text(json.dumps(self.batch_config), encoding="utf-8")
        session = "550e8400-e29b-41d4-a716-446655440000"
        def capture(*args, **kwargs):
            write_events(self.out / "stdout.log", [
                {"type": "system", "subtype": "init", "session_id": session},
                {"type": "result", "subtype": "success", "session_id": session, "is_error": False, "result": "Synthetic job"}])
            (self.out / "stderr.log").write_text("", encoding="utf-8")
            return ProcessResult(0, False, False, self.out / "stdout.log", self.out / "stderr.log")
        launch.side_effect = capture
        AgentProviders(self.config).code("Author the approved Blender job.", self.workspace, self.out, 120)
        argv = launch.call_args.args[0]
        expected = []
        for path in (recipe, job):
            pattern = path.as_posix()
            if path.drive:
                pattern = "/" + path.drive[0].lower() + pattern[2:]
            expected.append(f"Edit(/{pattern})")
        self.assertEqual([arg for arg in argv if arg.startswith(("Edit(", "Write("))], expected)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "default")
        allowed = argv[argv.index("--allowedTools") + 1:]
        commands, instructions, _ = self.preview()
        self.assertEqual([arg for arg in allowed if arg.startswith("Bash(")], ["Bash(" + commands[0] + ")"])
        for contract in ("run_job(context)", "BLENDER_JOB_API.md", "export_asset", "instances", "plots", "report"):
            self.assertIn(contract, instructions)
        self.assertNotIn("Bash(python *)", argv)
        self.assertNotIn("acceptEdits", argv)
        self.assertFalse(Path(self.batch_config["key_file"]).exists())

    def test_blender_job_edit_grant_rejects_external_or_missing_files(self):
        outside = self.root / "outside-job.py"
        outside.write_text("def run_job(context):\n    return {}\n", encoding="utf-8")
        for path in (outside, self.workspace / "missing-job.py"):
            self.batch.write_text(json.dumps({**self.batch_config, "blender_job_file": str(path)}), encoding="utf-8")
            with self.subTest(path=path.name), self.assertRaises(ProviderError):
                self.preview()


if __name__ == "__main__":
    unittest.main()
