"""Approved asset-library promotion with real temporary Git/LFS, no UE/API."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from unattended import art_pipeline, gitops
from unattended.art_promotion import ArtPromotion, approved_payload
from unattended.storage import lock, read_json, write_json
from tests import test_unattended_art_worker as fixtures
from tests.test_unattended_art_pipeline import digest


class ArtPromotionTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.ArtWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.f, self.root = fixture, fixture.f.root
        public = fixture.f.evidence.public
        report = read_json(public / "review-package.json")
        source = public / "UE/HousingPreview"
        base = "/Game/ArtPreview/Versions/V_0123456789abcdef"
        imported = report["ue_import"]
        imported.update(asset_base=base, asset_version_id="0123456789abcdef", level_to_house={}, ue_files=[])
        for level in ("L1", "L4", "L8"):
            relative = "Content/ArtPreview/Versions/V_0123456789abcdef/Meshes/House_" + level + ".uasset"
            file = source / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(("synthetic UE package " + level).encode() * 20)
            imported["ue_files"].append({"path": relative, "sha256": digest(file)})
            imported["level_to_house"][level] = base + "/Meshes/House_" + level + ".House_" + level
        for relative in ("Content/ArtPreview/Maps/HousingPreview.umap",
                         "Content/ArtPreview/Versions/V_0000000000000000/Unapproved.uasset"):
            file = source / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"not approved for promotion")
            imported["ue_files"].append({"path": relative, "sha256": digest(file)})
        import_file = public / "receipts" / (imported["run"] + "-import.json")
        write_json(import_file, imported)
        report["capture"]["capture_id"] = art_pipeline.capture_fingerprint(
            imported["run"], import_file, art_pipeline.REPO / "art_tools/ue/capture_preview.py")
        write_json(public / "receipts" / (report["capture"]["capture_id"] + "-capture.json"), report["capture"])
        write_json(public / "review-package.json", report)
        self.pending, self.request = fixture.accept()
        self.history = read_json(fixture.worker.state_path)["art_history"][0]
        patch.stopall()  # Promotion below uses real Git, not the Engine fixture's stubs.
        self.target = self.root / "target-game"
        self.target.mkdir()
        gitops.git(self.target, "init", "-b", "main")
        gitops.git(self.target, "config", "user.name", "Offline Test")
        gitops.git(self.target, "config", "user.email", "offline@example.invalid")
        (self.target / "Game.uproject").write_text("{}", encoding="utf-8")
        (self.target / ".gitattributes").write_text("*.uasset filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8")
        (self.target / "Source.txt").write_text("unchanged gameplay source", encoding="utf-8")
        gitops.git(self.target, "add", ".")
        gitops.git(self.target, "commit", "-m", "BASE")
        self.base = gitops.head(self.target)
        main = deepcopy(fixture.config)
        main.pop("art_pipeline")
        main.update(project_path=str(self.target), runtime_dir=str(self.root / "game-runtime"))
        main["claude"]["planned_core_tests"] = False
        self.main_file = self.root / "main-config.json"
        write_json(self.main_file, main)
        self.config = {**fixture.config, "art_promotion": {"target_config_file": str(self.main_file),
                       "target_branch": "main", "push": False, "remote": "origin"}}
        self.consumer = ArtPromotion(self.config)
        self.free = patch("unattended.art_promotion.require_free_project").start()
        self.addCleanup(patch.stopall)

    def test_only_accepted_current_version_is_joined_with_real_lfs_and_no_gameplay_edits(self):
        user_log = self.target / "old-user-build.log"
        user_log.write_text("keep this preexisting log", encoding="utf-8")
        result = self.consumer.step()
        self.assertEqual(result["status"], "integrated", result)
        self.assertNotEqual(gitops.head(self.target), self.base)
        self.assertEqual((self.target / "Source.txt").read_text(), "unchanged gameplay source")
        self.assertFalse((self.target / "Content/ArtPreview/Maps").exists())
        self.assertFalse((self.target / "Content/ArtPreview/Versions/V_0000000000000000").exists())
        approved = list((self.target / "Content/ArtPreview/Versions/V_0123456789abcdef").rglob("*.uasset"))
        self.assertEqual(len(approved), 3)
        for file in approved:
            pointer = gitops.git(self.target, "show", "HEAD:" + file.relative_to(self.target).as_posix())
            self.assertIn("oid sha256:" + digest(file), pointer)
            self.assertTrue(file.read_bytes().startswith(b"synthetic UE package"))
        catalogs = list((self.target / "Content/Data/ArtApproved").rglob("catalog.json"))
        self.assertEqual(len(catalogs), 1)
        catalog = read_json(catalogs[0])
        self.assertEqual(set(catalog["levels"]), {"L1", "L4", "L8"})
        self.assertEqual(catalog["gameplay_binding"], "not_modified")
        self.assertEqual(user_log.read_text(), "keep this preexisting log")
        self.assertFalse(gitops.git(self.target, "status", "--porcelain=v1", "--untracked-files=no"))
        final = gitops.head(self.target)
        self.assertEqual(self.consumer.step()["status"], "idle")
        self.assertEqual(gitops.head(self.target), final)

    def test_existing_game_controller_lock_queues_without_staging_or_target_changes(self):
        with lock(gitops.common_dir(self.target) / "unattended-controller.lock"):
            result = self.consumer.step()
        self.assertEqual(result["status"], "waiting_target_busy")
        self.assertEqual(gitops.head(self.target), self.base)
        self.assertFalse((self.consumer.runtime / "promotions").exists())

    def test_dirty_or_open_target_waits_without_cleaning_user_files(self):
        user_file = self.target / "Content/ArtPreview/Versions/V_0123456789abcdef/user-note.txt"
        user_file.parent.mkdir(parents=True, exist_ok=True)
        user_file.write_text("keep this", encoding="utf-8")
        result = self.consumer.step()
        self.assertEqual(result["status"], "waiting_target_clean")
        self.assertEqual(user_file.read_text(), "keep this")
        user_file.unlink()
        self.free.side_effect = RuntimeError("Editor owns project")
        result = self.consumer.step()
        self.assertEqual(result["status"], "waiting_target_editor")
        self.assertEqual(gitops.head(self.target), self.base)

    def test_missing_user_acceptance_or_changed_approved_bytes_never_stage(self):
        state = read_json(self.f.worker.state_path)
        state["art_history"] = []
        write_json(self.f.worker.state_path, state)
        result = self.consumer.step()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(gitops.head(self.target), self.base)
        with self.assertRaises(ValueError):
            approved_payload(self.request, {**self.history, "status": "awaiting_approval"})
        package = Path(self.request["review_package"])
        package.write_bytes(package.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            approved_payload(self.request, self.history)

    def test_crash_after_fast_forward_reconciles_without_duplicate_commit(self):
        with patch.object(self.consumer, "_update", side_effect=OSError("after join receipt before public queue")):
            with self.assertRaises(OSError):
                self.consumer.step()
        joined = gitops.head(self.target)
        self.assertNotEqual(joined, self.base)
        result = self.consumer.step()
        self.assertEqual(result["status"], "integrated", result)
        self.assertEqual(gitops.head(self.target), joined)

    def test_changed_import_receipt_is_rejected_by_approval_hash_before_copy(self):
        report = read_json(self.request["review_package"])
        report["ue_import"]["ue_files"].append({"path": "../escape.uasset", "sha256": "0" * 64})
        # Rebind only the test package/hash; the real independent verifier still
        # rejects it before any staging. No caller-supplied path may be trusted.
        write_json(self.request["review_package"], report)
        with self.assertRaises(ValueError):
            approved_payload(self.request, self.history)
        self.assertFalse((self.target / "escape.uasset").exists())

    def test_tracked_changes_and_ignored_asset_overlap_are_preserved(self):
        source = self.target / "Source.txt"
        source.write_text("user in-progress edit", encoding="utf-8")
        self.assertEqual(self.consumer.step()["status"], "waiting_target_clean")
        self.assertEqual(source.read_text(), "user in-progress edit")
        source.write_text("unchanged gameplay source", encoding="utf-8")
        relative = "Content/ArtPreview/Versions/V_0123456789abcdef/ignored.bin"
        ignored = self.target / relative
        ignored.parent.mkdir(parents=True, exist_ok=True)
        ignored.write_bytes(b"preserve ignored user data")
        (self.target / ".git/info/exclude").write_text("Content/ArtPreview/\n", encoding="utf-8")
        self.assertEqual(self.consumer.step()["status"], "waiting_target_clean")
        self.assertEqual(ignored.read_bytes(), b"preserve ignored user data")
        self.assertEqual(gitops.head(self.target), self.base)

    def test_unsettled_budget_after_acceptance_blocks_promotion(self):
        self.f.f.evidence.review_ledger(actual=195, unknown=65)
        result = self.consumer.step()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("Independent asset check failed", result["message"])
        self.assertEqual(gitops.head(self.target), self.base)
        self.assertFalse((self.consumer.runtime / "promotion-staging").exists())

    def test_changed_staged_candidate_cannot_smuggle_gameplay_changes(self):
        original = self.consumer._stage
        def altered(request, payload, base, receipt_path):
            receipt = original(request, payload, base, receipt_path)
            stage = Path(receipt["staging"])
            (stage / "Source.txt").write_text("unexpected gameplay edit", encoding="utf-8")
            gitops.git(stage, "add", "Source.txt")
            gitops.git(stage, "commit", "-m", request["source_task_id"])
            receipt["candidate_sha"] = gitops.head(stage)
            write_json(receipt_path, receipt)
            return receipt
        with patch.object(self.consumer, "_stage", side_effect=altered):
            result = self.consumer.step()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("non-approved changes", result["message"])
        self.assertEqual(gitops.head(self.target), self.base)
        self.assertEqual((self.target / "Source.txt").read_text(), "unchanged gameplay source")

    def test_interrupted_copy_never_leaves_a_partial_asset_or_lfs_object(self):
        source = self.target / "Source.txt"
        destination = self.root / "copy-target" / "object"
        def interrupted(src, dst):
            Path(dst).write_bytes(b"partial")
            raise OSError("simulated interrupted copy")
        with patch("unattended.art_promotion.shutil.copyfile", side_effect=interrupted), self.assertRaises(OSError):
            self.consumer._copy_checked(source, destination, digest(source))
        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.iterdir()), [])
        self.consumer._copy_checked(source, destination, digest(source))
        self.assertEqual(destination.read_bytes(), source.read_bytes())


if __name__ == "__main__":
    unittest.main()
