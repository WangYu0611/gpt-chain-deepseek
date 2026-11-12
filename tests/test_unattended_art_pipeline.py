"""Offline evidence checks for the three-level asset preview; no DCC/API execution."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from unattended import art_pipeline as pipeline
from unattended.art_tripo import TripoClient


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_json(path, value):
    Path(path).write_text(json.dumps(value), encoding="utf-8")


def png(width=1920, height=1080, color=(130, 100, 70)):
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    pixels = (b"\x00" + bytes(color) * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


class ArtPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.public = self.root / "public"
        self.public.mkdir()
        self.reference = self.root / "approved-reference.png"
        self.reference.write_bytes(png(32, 32))
        self.config_path = self.root / "batch.json"
        self.config = {
            "version": 1, "batch_id": "HOUSE-COMMON-001", "phase": "preview", "budget_limit": 3000,
            "approved_levels": ["L1", "L4", "L8"], "style_reference_task_id": "approved-reference-task",
            "artifacts_dir": str(self.public), "private_state_dir": str(self.root / "private"),
            "key_file": str(self.root / "secrets" / "tripo.txt"),
            "blender": str(self.root / "tools" / "blender.exe"), "engine": str(self.root / "tools" / "UE_5.8"),
            "references": [{"path": str(self.reference), "sha256": digest(self.reference)}],
            "assets": [{"asset_id": "HOUSE_" + level, "level": level, "revision": "v001",
                        "prompt": "Approved refined A reference; " + level,
                        "target_height_m": height, "max_footprint_m": 12, "front_rotation_deg": 0}
                       for level, height in (("L1", 3.5), ("L4", 5.0), ("L8", 7.5))],
        }

    def load(self, config=None):
        save_json(self.config_path, config if config is not None else self.config)
        return pipeline.load_config(self.config_path)

    def downloaded(self):
        path = self.root / "downloaded.glb"
        path.write_bytes(b"test fixture GLB bytes, not a rendered or parsed model\n" * 4)
        return {"ready": True, "api_status": "success", "download_status": "complete",
                "actual_credits": 60, "budget": {"breached": False},
                "files": {"model_url": {"path": str(path), "size": path.stat().st_size, "sha256": digest(path)}}}, path

    def processed(self):
        folder = self.public / "processed"
        folder.mkdir(exist_ok=True)
        paths = {}
        for name in ("house.fbx", "house_L4.fbx", "house_L8.fbx", "base.png", "orm.png", "normal.png", "scene.blend"):
            path = folder / name
            path.write_bytes(png(32, 32) if name.endswith(".png") else ("fixture " + name).encode() * 8)
            paths[name] = path
        manifest = {
            "schema_version": 1, "style_id": "A", "blend": "scene.blend", "blend_sha256": digest(paths["scene.blend"]),
            "assets": [{"asset_id": "HOUSE_" + level, "type": "building", "fbx": name,
                        "fbx_sha256": digest(paths[name])}
                       for level, name in (("L1", "house.fbx"), ("L4", "house_L4.fbx"), ("L8", "house_L8.fbx"))],
            "materials": [{"material_name": "M_House", "base_color": "base.png", "orm": "orm.png", "normal": "normal.png",
                           "base_color_sha256": digest(paths["base.png"]), "orm_sha256": digest(paths["orm.png"]),
                           "normal_sha256": digest(paths["normal.png"])}],
            "plots": [{"level": level} for level in ("L1", "L4", "L8")],
            "files": [{"path": name, "size_bytes": path.stat().st_size, "sha256": digest(path)}
                      for name, path in paths.items()],
        }
        file = folder / "assets_manifest.json"
        save_json(file, manifest)
        return file, manifest, paths

    def captured(self):
        folder = self.public / "screenshots"
        folder.mkdir(exist_ok=True)
        shots = []
        for index in range(8):
            file = folder / f"shot-{index}.png"
            file.write_bytes(png(color=(100 + index, 90, 70)))
            shots.append({"path": str(file), "sha256": digest(file), "size": file.stat().st_size,
                          "width": 1920, "height": 1080})
        receipt = self.public / "capture.json"
        record = {"status": "passed", "real_rhi": True, "shots": shots}
        save_json(receipt, record)
        return receipt, record

    def review_ledger(self, *, actual=195, reserved=0, unknown=0):
        client = TripoClient(self.config["private_state_dir"], self.config["key_file"])
        client.root.mkdir(exist_ok=True)
        tasks = {}
        for name, cost, reservation, submission in (("finished", actual, 0, "submitted"),
                                                   ("running", None, reserved, "submitted"),
                                                   ("lost-response", None, unknown, "unknown")):
            if cost is None and not reservation:
                continue
            tasks[name] = {"tag": name, "request_hash": "offline-fixture", "submission": submission,
                           "actual_credits": cost, "reserved_credits": reservation,
                           "api_status": "success" if cost is not None else "unknown",
                           "download_status": "complete" if cost is not None else "pending"}
        save_json(client.ledger_path, {"version": 1, "base_url": client.base_url,
                                      "budget_limit": 3000, "tasks": tasks})
        return client.status()["budget"]

    def review_fixture(self):
        manifest, processed, paths = self.processed()
        repo = self.root / "repo"
        # Copy only the trusted dependency whitelist, so this shared fixture also
        # supports Engine's real verifier subprocess against the current code.
        for relative in pipeline.BLENDER_DEPENDENCIES:
            source = pipeline.REPO / relative
            if source.is_file():
                target = repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
        with patch.object(pipeline, "REPO", repo):
            provenance = pipeline.blender_provenance()
        source = self.public / "production-input.json"
        save_json(source, {"blender_provenance": provenance})
        processed.update(blender_provenance=provenance, input_manifest_sha256=digest(source))
        save_json(manifest, processed)
        scripts = repo / "art_tools" / "ue"
        scripts.mkdir(parents=True)
        import_script, capture_script = scripts / "import_preview.py", scripts / "capture_preview.py"
        import_script.write_text("# import version one", encoding="utf-8")
        capture_script.write_text("# capture version one", encoding="utf-8")
        project = self.public / "UE" / "HousingPreview" / "HousingPreview.uproject"
        asset = project.parent / "Content" / "House.uasset"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"offline UE package fixture")
        project.write_text("{}", encoding="utf-8")
        receipts = self.public / "receipts"
        receipts.mkdir()
        imported = {"run": "recipe-one", "status": "passed", "script_sha256": digest(import_script),
                    "manifest_sha256": digest(manifest),
                    "ue_files": [{"path": "Content/House.uasset", "sha256": digest(asset)}]}
        import_file = receipts / "recipe-one-import.json"
        save_json(import_file, imported)
        _, captured = self.captured()
        captured["capture_id"] = pipeline.capture_fingerprint("recipe-one", import_file, capture_script)
        capture_file = receipts / (captured["capture_id"] + "-capture.json")
        save_json(capture_file, captured)
        report = {"batch_id": self.config["batch_id"], "approved_levels": ["L1", "L4", "L8"],
                  "full_batch_released": False, "manifest": str(manifest), "ue_import": imported,
                  "blender_provenance": provenance,
                  "capture": captured, "budget": {"budget": self.review_ledger()},
                  "files": [{"path": str(path), "sha256": digest(path)} for path in paths.values()]}
        save_json(self.public / "review-package.json", report)
        return repo, report, import_file, capture_file, capture_script

    def verify_review(self, repo):
        with patch.object(pipeline, "REPO", repo), patch.object(pipeline, "log"), \
                patch.object(TripoClient, "_api_json", side_effect=AssertionError("independent checks must not call Tripo")):
            return pipeline.verify_review(self.config)

    def test_valid_scope_verifies_reference_without_reading_credentials(self):
        self.assertFalse(Path(self.config["key_file"]).exists())
        result = self.load()
        self.assertEqual(result["approved_levels"], ["L1", "L4", "L8"])
        self.assertEqual(result["budget_limit"], 3000)
        self.assertFalse(Path(self.config["private_state_dir"]).exists())

    def test_budget_cannot_expand_or_change_type(self):
        for budget in (3001, 2999, "3000", True, None, float("nan")):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                self.load({**self.config, "budget_limit": budget})

    def test_only_approved_three_levels_and_preview_phase_are_accepted(self):
        variants = [
            {**self.config, "phase": "full"},
            {**self.config, "approved_levels": ["L1", "L2", "L3"]},
            {**self.config, "approved_levels": ["L1", "L4", "L8", "L2"]},
            {**self.config, "assets": self.config["assets"][:2]},
            {**self.config, "assets": list(reversed(self.config["assets"]))},
            {**self.config, "assets": self.config["assets"] + [{"level": "L2", "asset_id": "HOUSE_L2"}]},
        ]
        for config in variants:
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.load(config)

    def test_changed_reference_invalidates_config(self):
        self.reference.write_bytes(png(32, 32, (30, 40, 50)))
        with self.assertRaisesRegex(ValueError, "reference changed"):
            self.load()

    def test_config_requires_nonempty_hash_pinned_absolute_references(self):
        for references in (None, [], {}, [{"path": "reference.png", "sha256": digest(self.reference)}],
                           [{"path": str(self.reference)}], [{"path": str(self.reference), "sha256": "not-a-hash"}]):
            with self.subTest(references=references), self.assertRaises(ValueError):
                self.load({**self.config, "references": references})

    def test_private_state_and_key_cannot_be_public_artifacts(self):
        for field in ("private_state_dir", "key_file"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load({**self.config, field: str(self.public / "private")})

    def test_ready_download_requires_current_ready_flag_even_when_old_file_exists(self):
        record, path = self.downloaded()
        self.assertEqual(pipeline.ready_file(record, "model_url"), str(path.resolve()))
        for state in ({"ready": False, "error": "status_refresh_failed"},
                      {"ready": False, "cost_unknown": True},
                      {"ready": False, "budget": {"breached": True}}):
            with self.subTest(state=state), self.assertRaises(RuntimeError):
                pipeline.ready_file({**record, **state}, "model_url")

    def test_ready_download_must_have_a_digest_and_structured_metadata(self):
        record, path = self.downloaded()
        for entry in (str(path), {"path": str(path)}, {"path": str(path), "sha256": ""}):
            with self.subTest(entry=entry), self.assertRaises(RuntimeError):
                pipeline.ready_file({**record, "files": {"model_url": entry}}, "model_url")

    def test_ready_download_rejects_changed_or_missing_bytes(self):
        record, path = self.downloaded()
        path.write_bytes(b"x" * path.stat().st_size)
        with self.assertRaises(RuntimeError):
            pipeline.ready_file(record, "model_url")
        path.unlink()
        with self.assertRaises(RuntimeError):
            pipeline.ready_file(record, "model_url")

    def test_processed_cache_requires_all_mesh_texture_and_blend_payloads(self):
        for name in ("house.fbx", "house_L8.fbx", "base.png", "orm.png", "normal.png", "scene.blend"):
            for modification in ("change", "delete"):
                file, _, paths = self.processed()
                self.assertTrue(pipeline.verify_processed(file))
                path = paths[name]
                if modification == "change":
                    path.write_bytes(b"x" * path.stat().st_size)
                else:
                    path.unlink()
                with self.subTest(name=name, modification=modification):
                    self.assertFalse(pipeline.verify_processed(file))

    def test_processed_cache_rejects_missing_or_malformed_manifest(self):
        file = self.public / "assets_manifest.json"
        self.assertFalse(pipeline.verify_processed(file))
        for contents in ("not json", "{}", "[]"):
            file.write_text(contents, encoding="utf-8")
            with self.subTest(contents=contents):
                self.assertFalse(pipeline.verify_processed(file))

    def test_processed_cache_requires_material_digest_even_if_file_exists(self):
        file, manifest, _ = self.processed()
        del manifest["materials"][0]["normal_sha256"]
        save_json(file, manifest)
        self.assertFalse(pipeline.verify_processed(file))

    def test_blender_fingerprint_tracks_only_fixed_trusted_dependencies(self):
        repo = self.root / "trusted-repo"
        main = repo / pipeline.BLENDER_DEPENDENCIES[0]
        main.parent.mkdir(parents=True)
        main.write_text("# prepare v1", encoding="utf-8")
        with patch.object(pipeline, "REPO", repo):
            def fingerprint():
                return pipeline.blender_fingerprint({"assets": ["same cached Tripo source"],
                                                     "blender_provenance": pipeline.blender_provenance()})
            previous = fingerprint()
            (main.parent / "untrusted-extra.json").write_text('{"ignored":true}', encoding="utf-8")
            self.assertEqual(previous, fingerprint())
            for relative in pipeline.BLENDER_DEPENDENCIES:
                path = repo / relative
                with self.subTest(dependency=relative):
                    path.write_text("# dependency first revision", encoding="utf-8")
                    current = fingerprint()
                    self.assertNotEqual(previous, current)
                    path.write_text("# dependency next revision", encoding="utf-8")
                    self.assertNotEqual(current, fingerprint())
                    if relative != pipeline.BLENDER_DEPENDENCIES[0]:
                        present = fingerprint()
                        path.unlink()
                        self.assertNotEqual(present, fingerprint())
                    previous = fingerprint()
            self.assertFalse(Path(self.config["key_file"]).exists())

    def test_processed_cache_rejects_current_tool_or_source_mismatch(self):
        repo, report, _, _, _ = self.review_fixture()
        source = self.public / "production-input.json"
        with patch.object(pipeline, "REPO", repo):
            self.assertTrue(pipeline.verify_processed(report["manifest"], source_manifest=source))
            main = repo / pipeline.BLENDER_DEPENDENCIES[0]
            original = main.read_bytes()
            main.write_bytes(original + b"\n# changed tool")
            self.assertFalse(pipeline.verify_processed(report["manifest"], source_manifest=source))
            main.write_bytes(original)
            data = pipeline.read(source)
            data["changed_processing_input"] = True
            save_json(source, data)
            self.assertFalse(pipeline.verify_processed(report["manifest"], source_manifest=source))

    def test_capture_cache_requires_eight_existing_hashed_shots(self):
        file, record = self.captured()
        self.assertTrue(pipeline.verify_capture(file))
        record["shots"] = record["shots"][:7]
        save_json(file, record)
        self.assertFalse(pipeline.verify_capture(file))

    def test_capture_cache_requires_eight_distinct_shots(self):
        file, record = self.captured()
        record["shots"] = [record["shots"][0]] * 8
        save_json(file, record)
        self.assertFalse(pipeline.verify_capture(file))

    def test_capture_cache_rejects_changed_or_missing_shot_even_with_passed_receipt(self):
        for modification in ("change", "delete"):
            file, record = self.captured()
            image = Path(record["shots"][3]["path"])
            if modification == "change":
                image.write_bytes(png(color=(20, 40, 80)))
            else:
                image.unlink()
            with self.subTest(modification=modification):
                self.assertFalse(pipeline.verify_capture(file))

    def test_capture_cache_rejects_failed_or_malformed_receipt(self):
        file, record = self.captured()
        record["status"] = "failed"
        save_json(file, record)
        self.assertFalse(pipeline.verify_capture(file))
        for contents in ("{}", "not json", "[]"):
            file.write_text(contents, encoding="utf-8")
            with self.subTest(contents=contents):
                self.assertFalse(pipeline.verify_capture(file))

    def test_capture_fingerprint_changes_after_import_or_capture_recipe_changes(self):
        receipt = self.public / "import.json"
        script = self.root / "capture.py"
        save_json(receipt, {"status": "passed", "script_sha256": "import-version-one", "materials": "warm"})
        script.write_text("# capture version one", encoding="utf-8")
        original = pipeline.capture_fingerprint("processed-recipe-one", receipt, script)
        self.assertEqual(original, pipeline.capture_fingerprint("processed-recipe-one", receipt, script))
        self.assertNotEqual(original, pipeline.capture_fingerprint("processed-recipe-two", receipt, script))
        save_json(receipt, {"status": "passed", "script_sha256": "import-version-two", "materials": "neutral"})
        self.assertNotEqual(original, pipeline.capture_fingerprint("processed-recipe-one", receipt, script))
        after_import = pipeline.capture_fingerprint("processed-recipe-one", receipt, script)
        script.write_text("# capture version two", encoding="utf-8")
        self.assertNotEqual(after_import, pipeline.capture_fingerprint("processed-recipe-one", receipt, script))

    def test_import_cache_requires_current_script_manifest_and_saved_ue_asset(self):
        manifest, _, _ = self.processed()
        project = self.public / "UE" / "HousingPreview.uproject"
        asset = project.parent / "Content" / "House.uasset"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"offline UE package integrity fixture")
        project.write_text("{}", encoding="utf-8")
        script = self.root / "import_preview.py"
        script.write_text("# import version one", encoding="utf-8")
        receipt = self.public / "import.json"
        record = {"status": "passed", "script_sha256": digest(script), "manifest_sha256": digest(manifest),
                  "ue_files": [{"path": "Content/House.uasset", "sha256": digest(asset)}]}
        save_json(receipt, record)
        self.assertTrue(pipeline.verify_import(receipt, project, script, manifest))
        for target in (asset, script, manifest):
            original = target.read_bytes()
            target.write_bytes(original + b"changed")
            with self.subTest(target=target.name):
                self.assertFalse(pipeline.verify_import(receipt, project, script, manifest))
            target.write_bytes(original)
        asset.unlink()
        self.assertFalse(pipeline.verify_import(receipt, project, script, manifest))

    def test_import_cache_does_not_accept_status_only_or_malformed_receipt(self):
        manifest, _, _ = self.processed()
        project = self.public / "HousingPreview.uproject"
        project.write_text("{}", encoding="utf-8")
        script = self.root / "import_preview.py"
        script.write_text("# import", encoding="utf-8")
        receipt = self.public / "import.json"
        for value in ({"status": "passed", "script_sha256": digest(script), "manifest_sha256": digest(manifest)}, [], {}):
            save_json(receipt, value)
            with self.subTest(value=value):
                self.assertFalse(pipeline.verify_import(receipt, project, script, manifest))

    def test_independent_review_checks_bound_evidence_and_local_ledger_without_api(self):
        repo, _, _, _, _ = self.review_fixture()
        self.assertFalse(Path(self.config["key_file"]).exists())
        result = self.verify_review(repo)
        self.assertEqual(result["status"], "passed")
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(result["budget"]["committed"], 195)
        self.assertEqual(result["human_visual_acceptance"], "pending")
        self.assertFalse(result["full_batch_released"])

    def test_independent_review_rejects_old_shots_after_import_receipt_changes(self):
        repo, report, imported, _, _ = self.review_fixture()
        receipt = json.loads(imported.read_text())
        receipt["import_revision"] = "changed-import"
        save_json(imported, receipt)
        # Even updating the embedded import receipt cannot rebind old screenshots.
        report["ue_import"] = receipt
        save_json(self.public / "review-package.json", report)
        with self.assertRaises(RuntimeError):
            self.verify_review(repo)
        checks = json.loads((self.public / "independent-check.json").read_text())["checks"]
        self.assertTrue(checks["ue_assets"])
        self.assertTrue(checks["fresh_capture_artifacts"])
        self.assertFalse(checks["capture_matches_current_import"])

    def test_independent_review_rejects_old_shots_after_capture_script_changes(self):
        repo, _, _, _, script = self.review_fixture()
        script.write_text("# capture version two", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.verify_review(repo)
        checks = json.loads((self.public / "independent-check.json").read_text())["checks"]
        self.assertFalse(checks["capture_matches_current_import"])

    def test_independent_review_rejects_changed_blender_helpers_and_fixed_plan(self):
        repo, _, _, _, _ = self.review_fixture()
        for relative in pipeline.BLENDER_DEPENDENCIES:
            path = repo / relative
            original = path.read_bytes() if path.is_file() else None
            with self.subTest(dependency=relative):
                path.write_bytes((original or b"") + b"\nchanged trusted dependency")
                with self.assertRaises(RuntimeError):
                    self.verify_review(repo)
                checks = pipeline.read(self.public / "independent-check.json")["checks"]
                self.assertFalse(checks["blender_matches_current_inputs"])
                self.assertTrue(checks["processed_artifacts"])
            if original is None:
                path.unlink()
            else:
                path.write_bytes(original)
        self.assertEqual(self.verify_review(repo)["status"], "passed")

    def test_independent_review_requires_provenance_in_all_three_bound_documents(self):
        repo, report, _, _, _ = self.review_fixture()
        for path in (self.public / "review-package.json", self.public / "production-input.json", Path(report["manifest"])):
            original = path.read_bytes()
            data = pipeline.read(path)
            data.pop("blender_provenance")
            save_json(path, data)
            with self.subTest(document=path.name), self.assertRaises(RuntimeError):
                self.verify_review(repo)
            checks = pipeline.read(self.public / "independent-check.json")["checks"]
            self.assertFalse(checks["blender_matches_current_inputs"])
            path.write_bytes(original)

    def test_independent_review_rejects_stale_budget_snapshot(self):
        repo, _, _, _, _ = self.review_fixture()
        self.review_ledger(actual=260)
        with self.assertRaises(RuntimeError):
            self.verify_review(repo)
        result = json.loads((self.public / "independent-check.json").read_text())
        self.assertTrue(result["checks"]["budget_within_cap"])
        self.assertFalse(result["checks"]["budget_matches_report"])
        self.assertEqual(result["budget"]["actual"], 260)

    def test_independent_review_rejects_unsettled_budget_and_cost_overrun(self):
        repo, report, _, _, _ = self.review_fixture()
        for updates, failing_check in (({"reserved": 60}, "budget_settled"),
                                       ({"unknown": 60}, "budget_settled"),
                                       ({"actual": 3001}, "budget_within_cap")):
            report["budget"]["budget"] = self.review_ledger(**updates)
            save_json(self.public / "review-package.json", report)
            with self.subTest(updates=updates), self.assertRaises(RuntimeError):
                self.verify_review(repo)
            checks = json.loads((self.public / "independent-check.json").read_text())["checks"]
            self.assertTrue(checks["budget_matches_report"])
            self.assertFalse(checks[failing_check])

    def test_resuming_production_clears_old_active_error_before_new_stage(self):
        state_file = self.public / "pipeline-state.json"
        save_json(state_file, {"version": 1, "batch_id": self.config["batch_id"], "stage": "failed",
                               "error": "old rendering failure", "error_type": "RuntimeError"})
        record, path = self.downloaded()
        record["files"]["generated_image_url"] = dict(record["files"]["model_url"])
        observed = []
        def completed_task(*args, **kwargs):
            observed.append(json.loads(state_file.read_text(encoding="utf-8")))
            return record
        with patch("unattended.art_tripo.TripoClient") as client, patch.object(pipeline, "log"):
            client.return_value.run.side_effect = completed_task
            client.return_value.status.return_value = {"budget": {"committed": 195}}
            result = pipeline.run_preview(self.config, until="models")
        self.assertEqual(result["stage"], "generated")
        self.assertEqual(len(observed), 6)
        for state in [*observed, json.loads(state_file.read_text(encoding="utf-8"))]:
            self.assertNotIn("error", state)
            self.assertNotIn("error_type", state)

    def test_resuming_production_preserves_a_new_failure_in_active_state(self):
        state_file = self.public / "pipeline-state.json"
        save_json(state_file, {"version": 1, "batch_id": self.config["batch_id"], "stage": "failed",
                               "error": "old rendering failure", "error_type": "RuntimeError"})
        with patch("unattended.art_tripo.TripoClient") as client, patch.object(pipeline, "log"):
            client.return_value.run.side_effect = ValueError("new generation failure")
            with self.assertRaises(ValueError):
                pipeline.run_preview(self.config, until="models")
        state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["stage"], "failed")
        self.assertEqual(state["error"], "new generation failure")
        self.assertEqual(state["error_type"], "ValueError")


if __name__ == "__main__":
    unittest.main()
