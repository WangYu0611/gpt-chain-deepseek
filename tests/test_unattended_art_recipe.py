"""Bounded art edits and real ledger reuse, using only local fake HTTP responses."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_unattended_art_pipeline as fixtures
from tests.test_unattended_art_pipeline import digest, save_json
from tests import test_unattended_art_integration as integration
from unattended import art_pipeline as pipeline
from unattended.art_tripo import RequestConflict, TripoClient
from unattended.providers import ProviderError, _claude_art_preflight


class ArtRecipeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ArtPipelineTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.cfg = self.f.config
        self.workspace = self.f.root / "worker-workspace"
        self.workspace.mkdir()
        self.file = self.workspace / "housing-recipe.json"
        self.cfg["recipe_file"] = str(self.file)
        self.document = {"version": 1, "assets": []}
        for asset in self.cfg["assets"]:
            entry = {key: copy.deepcopy(asset[key]) for key in pipeline.RECIPE_FIELDS}
            entry["max_footprint_m"] = [12, 12]
            self.document["assets"].append(entry)
        self.save()

    def save(self):
        save_json(self.file, self.document)

    def rework_low(self):
        self.document["assets"][0].update(revision="v002", prompt="Poor inhabited hut, uneven patched thatch, "
            "wattle and earth, plain wood door, small window, detailed realistic materials, no ornate pillars.",
            target_height_m=3.0, max_footprint_m=[5, 4])
        self.save()

    def test_overlay_preserves_authorization_and_original_config(self):
        self.rework_low()
        before = copy.deepcopy(self.cfg)
        effective, provenance, raw = pipeline.recipe_overlay(self.cfg)
        self.assertEqual(self.cfg, before)
        self.assertEqual(effective["budget_limit"], 3000)
        self.assertEqual(effective["references"], self.cfg["references"])
        self.assertEqual([a["level"] for a in effective["assets"]], ["L1", "L4", "L8"])
        self.assertEqual(effective["assets"][0]["target_height_m"], 3.0)
        self.assertEqual(provenance["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(pipeline.recipe_file_path(self.cfg, workspace=self.workspace), self.file)

    def test_immutable_fields_asset_scope_and_missing_assets_are_rejected(self):
        original = copy.deepcopy(self.document)
        changes = [lambda d: d.update(budget_limit=9000), lambda d: d.update(references=[]),
                   lambda d: d["assets"][0].update(level="L2"),
                   lambda d: d["assets"][0].update(asset_id="HOUSE_L2"),
                   lambda d: d["assets"].reverse(), lambda d: d["assets"].pop()]
        for change in changes:
            self.document = copy.deepcopy(original)
            change(self.document)
            self.save()
            with self.subTest(document=self.document), self.assertRaises(ValueError):
                pipeline.recipe_overlay(self.cfg)

    def test_recipe_values_and_prompt_revision_are_bounded(self):
        original = copy.deepcopy(self.document)
        for field, value in (("revision", "../v002"), ("revision", "v000"), ("revision", "v1000"),
                             ("prompt", ""), ("prompt", "x" * 8001), ("prompt", "@foreign_task"),
                             ("prompt", "Changed without revision"), ("target_height_m", 1.49),
                             ("target_height_m", 8.01), ("target_height_m", float("nan")),
                             ("target_height_m", True), ("max_footprint_m", [13, 4]),
                             ("max_footprint_m", [5, 0]), ("max_footprint_m", [5]),
                             ("front_rotation_deg", 45), ("front_rotation_deg", True)):
            self.document = copy.deepcopy(original)
            self.document["assets"][0][field] = value
            self.save()
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                pipeline.recipe_overlay(self.cfg)

    def test_recipe_path_duplicate_fields_and_size_are_rejected(self):
        for path in ("relative.json", str(self.workspace / ".." / "recipe.json"),
                     str(self.f.public / "recipe.json"), str(self.file.with_suffix(".py"))):
            with self.subTest(path=path), self.assertRaises(ValueError):
                pipeline.recipe_file_path({**self.cfg, "recipe_file": path})
        with self.assertRaises(ValueError):
            pipeline.recipe_file_path(self.cfg, workspace=self.f.root / "another-workspace")
        for raw in ('{"version":1,"version":1,"assets":[]}', " " * (128 * 1024 + 1)):
            self.file.write_text(raw, encoding="utf-8")
            with self.assertRaises(ValueError):
                pipeline.recipe_overlay(self.cfg)

    def local_tripo(self):
        client = TripoClient(self.cfg["private_state_dir"], self.cfg["key_file"])
        posts, tasks = [], {}
        def api(method, endpoint, body=None):
            if method == "POST":
                task = "offline-" + str(len(posts) + 1)
                posts.append((endpoint, copy.deepcopy(body)))
                cost, field, extension = ((5, "generated_image_url", "png") if endpoint.endswith("image-to-image")
                                           else (60, "model_url", "glb"))
                tasks[task] = {"code": 0, "data": {"status": "success", "credits_consumed": cost,
                                "output": {field: "https://offline.invalid/" + task + "." + extension}}}
                return {"code": 0, "data": {"task_id": task}}
            return tasks[endpoint.rsplit("/", 1)[-1]]
        def download(url, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(("Offline artifact " + url).encode() * 3)
            return {"path": str(destination), "sha256": digest(destination), "size": destination.stat().st_size}
        client._api_json, client._download_one = api, download
        return client, posts

    def test_one_revised_level_adds_65_and_other_levels_reuse_real_ledger(self):
        client, posts = self.local_tripo()
        with patch("unattended.art_tripo.TripoClient", return_value=client), patch.object(pipeline, "log"):
            first = pipeline.run_preview(self.cfg, until="models")
            self.assertEqual(first["budget"]["budget"]["actual"], 195)
            self.assertEqual(len(posts), 6)
            self.rework_low()
            second = pipeline.run_preview(self.cfg, until="models")
            self.assertEqual(second["budget"]["budget"]["actual"], 260)
            self.assertEqual(len(posts), 8)
            self.assertEqual(posts[-2][1]["prompt"], self.document["assets"][0]["prompt"])
            self.assertEqual(posts[-1][1]["input"], "offline-7")
            self.assertEqual(digest(second["recipe"]["snapshot"]), second["recipe"]["sha256"])
            pipeline.run_preview(self.cfg, until="models")
            self.assertEqual(len(posts), 8)
            # A second changed prompt cannot hide behind the same paid tag.
            self.document["assets"][0]["prompt"] += " Extra patched wall."
            self.save()
            with self.assertRaises(RequestConflict):
                pipeline.run_preview(self.cfg, until="models")
            self.assertEqual(len(posts), 8)
            self.assertEqual(client.status()["budget"]["actual"], 260)
            self.assertEqual(client.status()["budget"]["limit"], 3000)

    def test_prepared_input_contains_bounded_recipe_and_provenance(self):
        self.rework_low()
        client, _ = self.local_tripo()
        with patch("unattended.art_tripo.TripoClient", return_value=client), patch.object(pipeline, "log"), \
                patch.object(pipeline, "verify_processed", return_value=True), \
                patch.object(pipeline, "process", side_effect=AssertionError("No Blender execution in this test")):
            result = pipeline.run_preview(self.cfg, until="prepare")
        manifest = pipeline.read(self.f.public / "production-input.json")
        self.assertEqual(manifest["recipe"], result["recipe"])
        self.assertEqual(manifest["assets"][0]["target_height_m"], 3.0)
        self.assertEqual(manifest["assets"][0]["max_footprint_m"], [5, 4])

    def test_review_binds_current_recipe_snapshot_and_processed_input_hash(self):
        repo, report, imported_file, _, _ = self.f.review_fixture()
        _, provenance, raw = pipeline.recipe_overlay(self.cfg)
        snapshot = Path(provenance["snapshot"])
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(raw)
        source = self.f.public / "production-input.json"
        save_json(source, {"recipe": provenance, "blender_provenance": report["blender_provenance"]})
        manifest = pipeline.read(report["manifest"])
        manifest["input_manifest_sha256"] = digest(source)
        save_json(report["manifest"], manifest)
        imported = pipeline.read(imported_file)
        imported["manifest_sha256"] = digest(report["manifest"])
        save_json(imported_file, imported)
        report["ue_import"] = imported
        report["recipe"] = provenance
        capture_script = repo / "art_tools/ue/capture_preview.py"
        report["capture"]["capture_id"] = pipeline.capture_fingerprint(imported["run"], imported_file, capture_script)
        save_json(self.f.public / "receipts" / (report["capture"]["capture_id"] + "-capture.json"), report["capture"])
        save_json(self.f.public / "review-package.json", report)
        self.assertTrue(pipeline.recipe_matches_review(self.cfg, report, report["manifest"]))
        with patch.object(pipeline, "REPO", repo), patch.object(pipeline, "log"), \
                patch.object(TripoClient, "_api_json", side_effect=AssertionError("No API during review")):
            checked = pipeline.verify_review(self.cfg, persist=False)
            self.assertTrue(checked["checks"]["recipe_matches_current_inputs"])
            self.rework_low()
            with self.assertRaises(RuntimeError):
                pipeline.verify_review(self.cfg, persist=False)
        self.assertFalse((self.f.public / "independent-check.json").exists())
        self.assertFalse(pipeline.recipe_matches_review(self.cfg, report, report["manifest"]))
        self.file.write_bytes(raw)
        snapshot.write_bytes(b"tampered snapshot")
        self.assertFalse(pipeline.recipe_matches_review(self.cfg, report, report["manifest"]))
        snapshot.write_bytes(raw)
        manifest["input_manifest_sha256"] = "0" * 64
        save_json(report["manifest"], manifest)
        self.assertFalse(pipeline.recipe_matches_review(self.cfg, report, report["manifest"]))

    def test_no_recipe_preserves_existing_config_and_manifest_contract(self):
        cfg = {key: value for key, value in self.cfg.items() if key != "recipe_file"}
        effective, provenance, raw = pipeline.recipe_overlay(cfg)
        self.assertIs(effective, cfg)
        self.assertIsNone(provenance)
        self.assertIsNone(raw)
        self.assertTrue(pipeline.recipe_matches_review(cfg, {}, self.f.root / "unused.json"))


class ArtRecipeBridgeTests(unittest.TestCase):
    def test_bridge_grants_one_workspace_recipe_and_rejects_external_edit_path(self):
        f = integration.ArtIntegrationTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        batch = pipeline.read(f.batch)
        recipe = f.workspace / "housing-recipe.json"
        recipe.write_text("{}", encoding="utf-8")
        batch["recipe_file"] = str(recipe)
        save_json(f.batch, batch)
        commands, instructions, directories = _claude_art_preflight(f.config, f.workspace, f.out)
        self.assertEqual(len(commands), 1)
        self.assertIn(recipe.as_posix(), instructions)
        self.assertIn("max_footprint_m", instructions)
        self.assertNotIn(recipe, directories)
        external = f.artifacts.parent / "outside-recipe.json"
        external.write_text("{}", encoding="utf-8")
        batch["recipe_file"] = str(external)
        save_json(f.batch, batch)
        with self.assertRaisesRegex(ProviderError, "workspace"):
            _claude_art_preflight(f.config, f.workspace, f.out)


if __name__ == "__main__":
    unittest.main()
