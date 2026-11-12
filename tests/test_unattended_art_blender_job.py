"""Offline binding checks for the actual Claude-authored Blender program."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_unattended_art_pipeline as fixtures
from tests.test_unattended_art_pipeline import digest, save_json
from unattended import art_pipeline as pipeline


class BlenderJobBindingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ArtPipelineTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.cfg = self.fixture.config
        self.workspace = self.fixture.root / "workspace"
        self.workspace.mkdir()
        self.job = self.workspace / "blender_job.py"
        self.job.write_text("def run_job(context):\n    return {}\n", encoding="utf-8")
        self.cfg["blender_job_file"] = str(self.job)

    def snapshot(self):
        provenance, raw = pipeline.blender_job_snapshot(self.cfg)
        target = Path(provenance["snapshot"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return provenance

    def test_only_the_configured_python_file_inside_workspace_is_editable(self):
        self.assertEqual(pipeline.blender_job_file_path(self.cfg, workspace=self.workspace), self.job)
        for path in ("relative.py", str(self.workspace / ".." / "other.py"),
                     str(self.job.with_suffix(".json")), str(self.fixture.public / "job.py"),
                     str(self.fixture.root / "private" / "job.py")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                pipeline.blender_job_file_path({**self.cfg, "blender_job_file": path}, workspace=self.workspace)
        with self.assertRaises(ValueError):
            pipeline.blender_job_file_path(self.cfg, workspace=self.fixture.root / "other")

    def test_source_is_frozen_by_bytes_and_changes_invalidate_the_fingerprint(self):
        first = self.snapshot()
        self.assertEqual(digest(self.job), first["sha256"])
        manifest = {"assets": self.cfg["assets"], "blender_job": first}
        initial = pipeline.blender_fingerprint(manifest)
        self.job.write_text("def run_job(context):\n    return {'report': {}}\n", encoding="utf-8")
        second, _ = pipeline.blender_job_snapshot(self.cfg)
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertNotEqual(initial, pipeline.blender_fingerprint({**manifest, "blender_job": second}))
        self.assertEqual(digest(first["snapshot"]), first["sha256"])

    def test_invalid_or_overlarge_python_is_rejected_before_launch(self):
        for source, error in (("", ValueError), ("x" * (1024 * 1024 + 1), ValueError),
                              ("def run_job(:\n", SyntaxError)):
            self.job.write_text(source, encoding="utf-8")
            with self.subTest(error=error), self.assertRaises(error):
                pipeline.blender_job_snapshot(self.cfg)

    def test_review_binds_live_source_snapshot_input_and_processed_metadata(self):
        provenance = self.snapshot()
        source = self.fixture.public / "production-input.json"
        save_json(source, {"blender_job": provenance})
        manifest = self.fixture.public / "processed.json"
        save_json(manifest, {"blender_job": provenance, "input_manifest_sha256": digest(source)})
        report = {"blender_job": provenance}
        self.assertTrue(pipeline.blender_job_matches_review(self.cfg, report, manifest))
        self.job.write_text("def run_job(context):\n    raise ValueError('new version')\n", encoding="utf-8")
        self.assertFalse(pipeline.blender_job_matches_review(self.cfg, report, manifest))

    def test_snapshot_tampering_and_missing_processed_binding_are_rejected(self):
        provenance = self.snapshot()
        source = self.fixture.public / "production-input.json"
        save_json(source, {"blender_job": provenance})
        manifest = self.fixture.public / "processed.json"
        save_json(manifest, {"blender_job": provenance, "input_manifest_sha256": digest(source)})
        report = {"blender_job": provenance}
        Path(provenance["snapshot"]).write_text("# altered", encoding="utf-8")
        self.assertFalse(pipeline.blender_job_matches_review(self.cfg, report, manifest))
        self.snapshot()
        save_json(manifest, {"input_manifest_sha256": digest(source)})
        self.assertFalse(pipeline.blender_job_matches_review(self.cfg, report, manifest))

    def test_legacy_without_job_remains_valid_only_without_job_metadata(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.pop("blender_job_file")
        source = self.fixture.public / "production-input.json"
        manifest = self.fixture.public / "processed.json"
        save_json(source, {})
        save_json(manifest, {})
        self.assertTrue(pipeline.blender_job_matches_review(cfg, {}, manifest))
        self.assertFalse(pipeline.blender_job_matches_review(cfg, {"blender_job": {}}, manifest))

    def test_blender_does_not_inherit_controller_provider_tokens(self):
        with patch.dict(pipeline.os.environ, {"PATH": "tools", "SYSTEMROOT": "C:/Windows",
                    "ANTHROPIC_API_KEY": "fixture", "TRIPO_API_KEY": "fixture",
                    "OPENAI_API_KEY": "fixture", "HTTPS_PROXY": "fixture"}, clear=True):
            self.assertEqual(pipeline.blender_environment(), {"PATH": "tools", "SYSTEMROOT": "C:/Windows"})


if __name__ == "__main__":
    unittest.main()
