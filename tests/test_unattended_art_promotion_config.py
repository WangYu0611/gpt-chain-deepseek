"""Promotion configuration is opt-in, exact, and never recursively loaded."""
import json
from pathlib import Path
import tempfile
import unittest

from unattended.config import load_config, validate_art_promotion_settings


class PromotionConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project, self.runtime = self.root / "art-project", self.root / "art-runtime"
        self.target = self.root / "main-config.json"
        self.target.write_text("Do not parse this target configuration during config loading", encoding="utf-8")
        self.settings = {"target_config_file": str(self.target), "target_branch": "main", "push": False, "remote": "origin"}

    def validate(self, settings):
        return validate_art_promotion_settings(settings, project=self.project, runtime=self.runtime)

    def test_load_validates_policy_without_loading_target_or_mutating_files(self):
        config = {"version": 1, "goal": "Art worker", "project_path": str(self.project), "runtime_dir": str(self.runtime),
                  "branch": "unattended/art", "coding_provider": "claude", "codex": {}, "claude": {},
                  "art_pipeline": {"config_file": str(self.root / "approved" / "batch.json"),
                                   "artifacts_dir": str(self.root / "public"), "task_ids": ["ART-001"]},
                  "art_promotion": self.settings}
        path = self.root / "worker.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        before = {file: file.read_bytes() for file in self.root.iterdir() if file.is_file()}
        self.assertEqual(load_config(path)["art_promotion"], self.settings)
        self.assertEqual(before, {file: file.read_bytes() for file in self.root.iterdir() if file.is_file()})
        self.assertFalse(self.runtime.exists())
        config.pop("art_pipeline")
        path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "worker"):
            load_config(path)

    def test_exact_keys_and_trusted_external_target_path(self):
        invalid = [{**self.settings, "command": "anything"}, {key: value for key, value in self.settings.items() if key != "push"}]
        invalid += [{**self.settings, "target_config_file": path} for path in (
            "relative.json", str(self.project / "config.json"), str(self.runtime / "config.json"),
            str(self.root / "*.json"), str(self.root / "main.py"), str(self.root / "sub" / ".." / "main.json"))]
        for settings in invalid:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.validate(settings)

    def test_branches_and_remotes_cannot_be_options_ref_expressions_or_urls(self):
        for branch in ("HEAD", "-main", "main~1", "main^", "main:other", "a..b", "a//b", "a/.hidden", "a.lock", "a/", "main.", "a@{1}", "main\n"):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                self.validate({**self.settings, "target_branch": branch})
        for remote in ("-origin", "https://host/repo", "origin/main", "origin\n", None):
            with self.subTest(remote=remote), self.assertRaises(ValueError):
                self.validate({**self.settings, "remote": remote})
        for value in (0, 1, "false", None):
            with self.subTest(push=value), self.assertRaises(ValueError):
                self.validate({**self.settings, "push": value})
        self.assertEqual(self.validate({**self.settings, "target_branch": "release/art-v1.2"})["target_branch"], "release/art-v1.2")


if __name__ == "__main__":
    unittest.main()
