"""Short-path package recovery is isolated, single-submit, and evidence-bound."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import struct
import unittest
from unittest.mock import patch

from tests import test_unattended_full_flow_checks as fixtures
from unattended import full_flow_short_package as short
from unattended import full_flow_checks as flow
from unattended.storage import read_json, write_json, write_text


class ShortPackageTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.FullFlowChecksTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.request, self.path, self.run = self.f.request, self.f.path, self.f.run
        self.short_root = self.f.root / "zg-flow"
        self.patch_root = patch.object(short, "SHORT_ROOT", self.short_root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        self.original = self.run / "checks/package/receipt.json"
        write_json(self.original, {"original": "Failed UBT action paths longer than 260"})
        self.uat = self.f.project / "Saved/Evidence/TASK-LOCAL-0066/package-uat.log"
        write_text(self.uat, "The following action paths are longer than 260 characters.")
        self.inputs = {"source_manifest_sha256": "manifest-sha",
                       "original_package_receipt": flow._file_record(self.original),
                       "original_path_failure": flow._file_record(self.uat)}
        self.manifest = {"files": {}}

    def prepare(self, request, directory, folder, manifest, build):
        project = directory / "project"
        project.mkdir()
        path = folder / "short-source-manifest.json"
        write_json(path, manifest)
        return {**request, "run_dir": str(directory), "project_dir": str(project)}, {
            "short_source_manifest": flow._file_record(path), "editor_artifacts": []}

    def execute(self, argv, cwd, output, timeout):
        self.assertEqual(cwd, self.short_root / self.request["id"][:12] / "project")
        self.assertEqual(timeout, 3600)
        self.assertIn("-ExecCmds=Automation RunTests ^ZhuGuoCity.M2.PackageIntegration$", argv)
        self.assertNotIn("-nocompileeditor", " ".join(argv))
        text = fixtures.automation("^" + flow.PACKAGE_TEST + "$", [flow.PACKAGE_TEST])
        text += "\npackage integration PASS: exit=0"
        write_text(output / "stdout.log", text)
        write_text(output / "stderr.log", "")
        write_text(output / "Unreal.log", text)
        process = {"argv": argv, "completed": True, "exit_code": 0, "timed_out": False,
                   "stdout_path": str(output / "stdout.log"), "stderr_path": str(output / "stderr.log")}
        write_json(output / "process.json", process)
        return process

    def patches(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(short, "_original_inputs", return_value=(self.manifest, {}, self.inputs)))
        stack.enter_context(patch.object(short, "_prepare_short", side_effect=self.prepare))
        stack.enter_context(patch.object(short, "_verify_short"))
        stack.enter_context(patch.object(flow, "_verify_inputs", return_value="manifest-sha"))
        execute = stack.enter_context(patch.object(flow, "_execute", side_effect=self.execute))
        stack.enter_context(patch.object(short, "_archive_package", return_value=({"passed": True}, [], [])))
        return stack, execute

    def test_exact_formal_package_runs_once_and_keeps_original_receipt(self):
        before = self.original.read_bytes()
        stack, execute = self.patches()
        with stack:
            first = short.run(self.path)
            self.assertEqual(first["status"], "passed", first)
            self.assertEqual(short.run(self.path), first)
            self.assertEqual(execute.call_count, 1)
        self.assertEqual(self.original.read_bytes(), before)
        self.assertEqual(first["head_sha"], self.request["head_sha"])
        self.assertEqual(first["source_manifest_sha256"], "manifest-sha")
        self.assertTrue((self.run / "checks/package-short/attempt-1/receipt.json").is_file())

    def test_existing_foreign_short_directory_is_preserved_without_model_launch(self):
        directory = short._short_directory(self.request)
        write_text(directory / "user-data.txt", "must not be changed")
        stack, execute = self.patches()
        with stack, self.assertRaisesRegex(ValueError, "not owned"):
            short.run(self.path)
        execute.assert_not_called()
        self.assertEqual((directory / "user-data.txt").read_text(), "must not be changed")

    def test_indeterminate_submission_never_resubmits_on_restart(self):
        stack, execute = self.patches()
        with stack:
            short.run(self.path)
            path = self.run / "checks/package-short/receipt.json"
            receipt = read_json(path)
            receipt.update(status="running", passed=False)
            write_json(path, receipt)
            with self.assertRaisesRegex(ValueError, "not be repeated"):
                short.run(self.path)
            self.assertEqual(execute.call_count, 1)

    def test_changed_short_or_archived_evidence_is_not_silently_adopted(self):
        stack, execute = self.patches()
        with stack:
            result = short.run(self.path)
            write_text(Path(result["process"]["stdout_path"]), "substituted")
            with self.assertRaisesRegex(ValueError, "evidence changed"):
                short.run(self.path)
        self.assertEqual(execute.call_count, 1)

    def test_actual_package_failure_remains_failed_and_is_cached(self):
        stack, execute = self.patches()
        with stack, patch.object(short, "_archive_package", return_value=({"passed": False, "reason": "Actual UAT failure"}, [], [])):
            result = short.run(self.path)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(short.run(self.path), result)
            self.assertEqual(execute.call_count, 1)

    def test_original_scope_and_path_failure_are_required(self):
        flow._bind(self.request, self.path)
        self.f.receipt("prepare", source_manifest_sha256="manifest-sha")
        self.f.receipt("build", source_manifest_sha256="manifest-sha", finished_at=flow._now())
        self.f.receipt("package", status="failed", source_manifest_sha256="manifest-sha", finished_at=flow._now())
        write_json(self.run / "source-manifest.json", {"files": {}})
        with patch.object(flow, "_verify_inputs", return_value="manifest-sha"):
            _, _, inputs = short._original_inputs(self.request, self.path)
            self.assertEqual(inputs["source_manifest_sha256"], "manifest-sha")
            write_text(self.uat, "Some other game test failure")
            with self.assertRaisesRegex(ValueError, "path-length"):
                short._original_inputs(self.request, self.path)

    def test_short_input_hash_drift_fails_before_formal_test(self):
        directory = short._short_directory(self.request)
        source = directory / "project/Source/file.cpp"
        write_text(source, "changed")
        manifest = {"files": {"Source/file.cpp": {"sha256": "a" * 64, "size_bytes": 7}}}
        with patch.object(short.gitops, "head", return_value=self.request["head_sha"]), patch.object(short.gitops, "clean", return_value=True):
            with self.assertRaisesRegex(ValueError, "differs"):
                short._verify_short(self.request, directory, manifest)

    def test_short_package_images_and_save_are_archived_back_into_original_run(self):
        directory = short._short_directory(self.request)
        project = directory / "project"
        artifacts = project / "Saved/Evidence/TASK-LOCAL-0066"
        artifacts.mkdir(parents=True)
        runs = {}
        for scenario in ("plain", "loop"):
            image = artifacts / (scenario + ".png")
            image.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 1280, 720))
            log = artifacts / (scenario + ".log")
            write_text(log, "Package scenario raw evidence")
            runs[scenario] = {"facts": {"screenshot": {"path": str(image), "sha256": flow._sha(image)}, "log": str(log)}}
        save = artifacts / "loop-Slot97.zgs"
        write_text(save, 'ZGSAVE1\n{"structureVersion":44,"contentVersion":"zg-defs-dev-56"}\n{}\n')
        runs["loop"]["facts"].update(save={"path": str(save), "sha256": flow._sha(save)}, summary="compared=300")
        write_json(artifacts / "package-summary.json", {"status": "passed", "head_sha": self.request["head_sha"],
                   "workspace": str(project), "runs": runs})
        write_text(artifacts / "package-uat.log", "BUILD COOK STAGE PAK ARCHIVE succeeded")
        derived = {**self.request, "run_dir": str(directory), "project_dir": str(project)}
        folder = self.run / "checks/package-short/attempt-1"
        folder.mkdir(parents=True)
        proof, evidence, images = short._archive_package(self.request, derived, "package integration PASS: exit=0", 0, folder)
        self.assertTrue(proof["passed"], proof)
        self.assertEqual(proof["save_header"]["structureVersion"], 44)
        self.assertEqual(proof["compared_steps"], 300)
        self.assertEqual(len(images), 2)
        self.assertTrue(all(Path(row["path"]).is_relative_to(self.run) for row in evidence))
        write_text(artifacts / "package-summary.json", "short source later changed")
        flow._check_files(evidence, self.run)

    def test_real_archive_matches_original_and_copies_only_bound_editor_artifacts(self):
        # The shared fixture creates a tiny real Git/LFS snapshot, not a game.
        real = fixtures.FullFlowChecksTests()
        real.setUp()
        self.addCleanup(real.doCleanups)
        real.test_real_git_archive_and_lfs_hydration_create_exact_isolated_head()
        self.request = real.request
        write_text(real.project / "Source/test.cpp", "original source\n")
        manifest = read_json(real.run / "source-manifest.json")
        before = datetime.now(timezone.utc) - timedelta(seconds=1)
        for name in short.EDITOR_FILES:
            write_text(real.project / "Binaries/Win64" / name, "same-head fixture " + name)
        build = {"started_at": before.isoformat(), "finished_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()}
        directory = short._short_directory(self.request)
        directory.mkdir(parents=True)
        output = real.run / "short-fixture"
        output.mkdir()
        derived, proof = short._prepare_short(self.request, directory, output, manifest, build)
        self.assertEqual(len(proof["editor_artifacts"]), 3)
        short._verify_short(self.request, directory, manifest)
        self.assertEqual(short.gitops.head(Path(derived["project_dir"])), self.request["head_sha"])
        self.assertEqual((directory / "project/Content/Test.uasset").read_bytes(), (real.source / "Content/Test.uasset").read_bytes())


if __name__ == "__main__":
    unittest.main()
