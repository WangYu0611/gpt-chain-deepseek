"""Isolated full-flow evidence and fixed commands, with no game/model execution."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import struct
import tempfile
import unittest
from unittest.mock import patch

from github_harness_lab.lab_harness.process import ProcessResult
from unattended import full_flow_checks as flow
from unattended.storage import write_json, write_text


def automation(selector, names, *, failed=(), package_skip=False, normal=False):
    lines = [f"LogAutomationCommandLine: Display: Found {len(names)} automation tests based on '{selector}'"]
    lines += ["LogAutomationCommandLine: Display:     " + name for name in names]
    lines += [f"Test Completed. Result={{{'Fail' if name in failed else 'Success'}}} Name={{{name.rsplit('.', 1)[-1]}}} Path={{{name}}}"
              for name in names]
    lines.append(f"{len(names)} tests performed.")
    if package_skip:
        lines.append("skipped: package integration runs only when its exact test is selected")
    if normal:
        lines.append("LogRHI: Using Default RHI: D3D12")
    return "\n".join(lines)


class FullFlowChecksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.runtime = self.root / "runtime"
        self.run = self.runtime / "full-flow/runs" / ("a" * 32)
        self.run.mkdir(parents=True)
        self.project = self.run / "project"
        self.engine = self.root / "UE_5.8/Engine"
        for name in ("Binaries/Win64/UnrealEditor.exe", "Binaries/Win64/UnrealEditor-Cmd.exe",
                     "Binaries/ThirdParty/DotNet/10.0/win-x64/dotnet.exe",
                     "Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.dll"):
            write_text(self.engine / name, "test placeholder, never executed")
        self.request = {"version": 1, "id": self.run.name, "task_id": "TASK-LOCAL-0091", "head_sha": "b" * 40,
                        "source_workspace": str(self.source), "runtime_dir": str(self.runtime), "run_dir": str(self.run),
                        "project_dir": str(self.project), "engine_dir": str(self.engine),
                        "authorized_at": datetime.now(timezone.utc).isoformat(), "model": "claude-opus-5-5", "effort": "high"}
        self.path = self.run / "request.json"
        self.save()
        self.output = "Result: Succeeded\n"
        self.native_calls = []

    def save(self):
        self.request["request_sha256"] = flow.request_hash(self.request)
        write_json(self.path, self.request)

    def receipt(self, stage, **extra):
        value = {"version": 1, **{key: self.request[key] for key in ("id", "task_id", "head_sha", "request_sha256")},
                 "stage": stage, "status": "passed", "passed": True, "attempt": 1, "updated_at": flow._now(), **extra}
        write_json(self.run / "checks" / stage / "receipt.json", value)
        return value

    def fake_process(self, argv, *, output_dir, **kwargs):
        self.native_calls.append((argv, kwargs))
        write_text(output_dir / "stdout.log", self.output)
        write_text(output_dir / "stderr.log", "")
        for arg in argv:
            if arg.startswith("-abslog="):
                write_text(Path(arg.split("=", 1)[1]), self.output)
        return ProcessResult(0, False, False, output_dir / "stdout.log", output_dir / "stderr.log")

    def test_request_hash_paths_and_model_cannot_drift(self):
        self.assertEqual(flow.load_request(self.path), self.request)
        original = self.request["request_sha256"]
        self.request["request_sha256"] = "ignored self field"
        self.assertEqual(flow.request_hash(self.request), original)
        self.request["head_sha"] = "c" * 40
        write_json(self.path, self.request)
        with self.assertRaisesRegex(ValueError, "Canonical"):
            flow.load_request(self.path)
        self.request["project_dir"] = str(self.source)
        self.save()
        with self.assertRaisesRegex(ValueError, "isolated"):
            flow.load_request(self.path)
        self.request["project_dir"] = str(self.project)
        self.request["effort"] = "low"
        self.save()
        with self.assertRaisesRegex(ValueError, "Opus"):
            flow.load_request(self.path)

    def test_full_namespace_parser_counts_failure_and_preserves_skip_truth(self):
        names = ["ZhuGuoCity.M0.Fixture", flow.PACKAGE_TEST, "ZhuGuoCity.M3.DeathFlow"]
        result = flow.parse_regression(automation(flow.REGRESSION_SELECTOR, names, failed=[names[0]], package_skip=True))
        self.assertEqual((result["discovered"], result["executed"], result["successful"], result["failed"]), (3, 3, 2, 1))
        self.assertTrue(result["complete"])
        self.assertFalse(result["passed"])
        self.assertTrue(result["package_skipped"])
        self.assertTrue(result["skip_or_not_run_lines"])
        self.assertFalse(flow.parse_regression("0 tests performed.")["passed"])

    def test_fixed_commands_select_entire_namespace_and_real_rhi_with_bounded_budgets(self):
        regression = flow._automation_command(self.request, "regression", self.run / "all.log")
        self.assertIn("-ExecCmds=Automation RunTests ^ZhuGuoCity.M", regression)
        self.assertIn("-nullrhi", regression)
        visual = flow._automation_command(self.request, "visual", self.run / "visual.log")
        self.assertNotIn("-nullrhi", visual)
        self.assertIn("-d3d12", visual)
        for name in flow.VISUAL_TESTS:
            self.assertIn(name, visual[2])
        self.assertEqual(flow.BUDGETS, {"prepare": 300, "build": 1800, "regression": 10800, "visual": 1800, "package": 3600})

    def test_visual_success_without_runtime_rhi_proof_is_failure(self):
        name = flow.VISUAL_TESTS[0]
        selector = "^" + name + "$"
        self.output = automation(selector, [name])
        with patch.object(flow, "run_process", side_effect=self.fake_process), patch.object(flow, "_inventory", return_value={"visual": {}}):
            result = flow.run_visual_case(self.request, name, self.run / "first", 600)
            self.assertFalse(result["passed"])
            self.output = automation(selector, [name], normal=True)
            self.assertTrue(flow.run_visual_case(self.request, name, self.run / "second", 600)["passed"])
        self.assertEqual(self.native_calls[0][1]["max_output_bytes"], 128 * 1024 * 1024)

    def test_stage_cache_is_bound_to_raw_evidence_and_never_reexecutes(self):
        self.receipt("prepare")
        with patch.object(flow, "_verify_inputs", return_value="manifest-sha"), patch.object(flow, "run_process", side_effect=self.fake_process):
            first = flow.run_stage(self.path, "build")
            self.assertEqual(first["status"], "passed")
            self.assertEqual(flow.run_stage(self.path, "build"), first)
            self.assertEqual(len(self.native_calls), 1)
            write_text(Path(first["process"]["stdout_path"]), "Substituted result")
            with self.assertRaisesRegex(ValueError, "evidence changed"):
                flow.run_stage(self.path, "build")

    def test_request_byte_or_runner_change_rejects_existing_run(self):
        flow._bind(self.request, self.path)
        self.path.write_text(json.dumps(self.request, separators=(",", ":")), encoding="utf-8")
        self.assertEqual(flow.load_request(self.path), self.request)
        with self.assertRaisesRegex(ValueError, "changed"):
            flow._bind(self.request, self.path)

    def test_forced_d3d12_is_proven_but_null_or_missing_rhi_is_not(self):
        for kind in ("Default", "Selected", "Forced"):
            self.assertTrue(flow._normal_rhi(f"LogRHI: Using {kind} RHI: D3D12"))
        self.assertFalse(flow._normal_rhi("LogRHI: Using Forced RHI: Null\nLogRHI: Using Forced RHI: D3D12"))
        self.assertFalse(flow._normal_rhi("LogRHI: Using Forced RHI: D3D12\nCommand Line: game -nullrhi"))
        self.assertFalse(flow._normal_rhi("LogRHI: Using Forced RHI: D3D11"))

    def migration_fixture(self):
        current = flow._bind(self.request, self.path)
        old = {**current, "runner_sha256": "1" * 64, "retest_runner_sha256": "2" * 64}
        target = self.run / "binding.json"; write_json(target, old)
        migration = {"version": 1, **{key: self.request[key] for key in ("id", "task_id", "head_sha", "request_sha256")},
                     "binding_sha256": flow._sha(target), "from_runner_sha256": old["runner_sha256"],
                     "from_retest_runner_sha256": old["retest_runner_sha256"],
                     "to_runner_sha256": current["runner_sha256"], "to_retest_runner_sha256": current["retest_runner_sha256"],
                     "reason": flow.RUNNER_MIGRATION_REASON}
        write_json(self.run / "operator-runner-migration.json", migration)
        return old, migration

    def test_exact_operator_migration_preserves_binding_and_is_in_new_receipt(self):
        old, _ = self.migration_fixture()
        original = (self.run / "binding.json").read_bytes()
        result = flow._bind(self.request, self.path)
        self.assertEqual(result["operator_runner_migration"]["original_binding_sha256"], flow._sha(self.run / "binding.json"))
        self.receipt("prepare")
        with patch.object(flow, "_verify_inputs", return_value="manifest-sha"), patch.object(flow, "run_process", side_effect=self.fake_process):
            receipt = flow.run_stage(self.path, "build")
        self.assertEqual(receipt["operator_runner_migration"], result["operator_runner_migration"])
        self.assertEqual((self.run / "binding.json").read_bytes(), original)
        self.assertNotEqual(receipt["runner_sha256"], old["runner_sha256"])

    def test_migration_cannot_change_identity_bytes_reason_or_code_hash(self):
        _, migration = self.migration_fixture()
        for field in migration:
            invalid = {**migration, field: "wrong"}
            write_json(self.run / "operator-runner-migration.json", invalid)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "migration"):
                flow._bind(self.request, self.path)
        write_json(self.run / "operator-runner-migration.json", migration)
        self.path.write_text(json.dumps(self.request, separators=(",", ":")), encoding="utf8")
        with self.assertRaisesRegex(ValueError, "migration"):
            flow._bind(self.request, self.path)

    def test_migration_proof_reparse_is_rejected(self):
        self.migration_fixture()
        original = flow.ue_checks._is_reparse
        target = self.run / "operator-runner-migration.json"
        with patch.object(flow.ue_checks, "_is_reparse", side_effect=lambda path: path == target or original(path)):
            with self.assertRaisesRegex(ValueError, "junctions"):
                flow._bind(self.request, self.path)

    def test_skipped_package_success_does_not_satisfy_packaging(self):
        output = automation("^" + flow.PACKAGE_TEST + "$", [flow.PACKAGE_TEST], package_skip=True)
        self.assertFalse(flow._package_evidence(self.request, output, 0)["passed"])
        self.output = output
        self.receipt("prepare")
        self.receipt("build")
        self.receipt("regression", package_execution={"passed": False}, evidence=[])
        with patch.object(flow, "_verify_inputs", return_value="manifest-sha"), patch.object(flow, "run_process", side_effect=self.fake_process):
            result = flow.run_stage(self.path, "package")
        self.assertEqual(result["status"], "failed")
        self.assertIn("^" + flow.PACKAGE_TEST + "$", self.native_calls[0][0][2])

    def test_only_exact_bound_actual_package_evidence_can_be_reused(self):
        summary = self.project / "Saved/Evidence/TASK-LOCAL-0066/package-summary.json"
        write_json(summary, {"status": "passed", "head_sha": self.request["head_sha"], "workspace": str(self.project)})
        proof = flow._package_evidence(self.request, "package integration PASS: exit=0 summary=fixture", 0)
        self.assertTrue(proof["passed"])
        self.receipt("prepare")
        self.receipt("build")
        self.receipt("regression", source_manifest_sha256="manifest-sha", package_execution=proof, evidence=[proof["summary"]])
        with patch.object(flow, "_verify_inputs", return_value="manifest-sha"), patch.object(flow, "run_process") as process:
            result = flow.run_stage(self.path, "package")
        self.assertEqual(result["status"], "passed")
        self.assertIn("reused_regression_receipt", result)
        process.assert_not_called()
        self.assertFalse(flow._package_evidence(self.request, "package integration PASS: exit=0", summary.stat().st_mtime + 1)["passed"])

    def test_package_retry_is_explicit_once_and_keeps_prior_failure(self):
        self.receipt("prepare")
        self.receipt("build")
        self.output = automation("^" + flow.PACKAGE_TEST + "$", [flow.PACKAGE_TEST], package_skip=True)
        with patch.object(flow, "_verify_inputs", return_value="manifest-sha"), patch.object(flow, "run_process", side_effect=self.fake_process):
            flow.run_stage(self.path, "package")
            flow.run_stage(self.path, "package")
            self.assertEqual(len(self.native_calls), 1)
            result = flow.run_stage(self.path, "package", retry=True)
            self.assertEqual(result["attempt"], 2)
            self.assertTrue((self.run / "checks/package/attempt-1/receipt.json").is_file())
            with self.assertRaisesRegex(ValueError, "exactly one"):
                flow.run_stage(self.path, "package", retry=True)

    def test_all_completed_is_distinct_from_passed_and_missing_is_never_done(self):
        for stage in flow.REQUIRED_STAGES[:-1]:
            self.receipt(stage)
        self.assertFalse(flow.status(self.request)["all_stages_finished"])
        self.receipt("package", status="failed", passed=False, error="actual packaging failure")
        facts = flow.status(self.request)
        self.assertEqual(facts["status"], "completed")
        self.assertFalse(facts["passed"])
        self.assertEqual(facts["failed_stages"], ["package"])

    def test_registered_but_missing_automation_is_explicitly_not_run(self):
        names = ["ZhuGuoCity.M0.First", "ZhuGuoCity.M0.Missing"]
        result = flow.parse_regression(automation(flow.REGRESSION_SELECTOR, names[:1]), names)
        self.assertFalse(result["complete"])
        self.assertEqual(result["expected_count"], 2)
        self.assertEqual(result["not_run_paths"], names[1:])
        self.assertEqual(result["cases"][1]["status"], "NOT_RUN")

    def test_inventory_reads_registrations_and_fixed_existing_visual_artifacts(self):
        write_text(self.project / "Source/Tests.cpp", '''
IMPLEMENT_SIMPLE_AUTOMATION_TEST(FOne, "ZhuGuoCity.M0.First", Flags)
namespace ZgFixtureHost {
 FString EvidenceDir() { return FPaths::ProjectSavedDir() / TEXT("Evidence/TASK-FIXTURE"); }
 const auto Png = TEXT("01-frame.png");
}
IMPLEMENT_SIMPLE_AUTOMATION_TEST(FVisual, "ZhuGuoCity.Local.FixtureVisual", Flags)
bool FVisual::RunTest() { auto State = MakeShared<ZgFixtureHost::FState>(); }
''')
        inventory = flow.source_inventory(self.project)
        self.assertEqual(inventory["regression"], ["ZhuGuoCity.M0.First"])
        self.assertEqual(inventory["visual"]["ZhuGuoCity.Local.FixtureVisual"]["images"], ["01-frame.png"])

    def test_visual_fresh_images_are_archived_and_prior_case_survives_source_overwrite(self):
        name = "ZhuGuoCity.Local.FixtureVisual"
        source = self.project / "Saved/Evidence/TASK-FIXTURE/01-frame.png"
        inventory = {"visual": {name: {"directory": "Saved/Evidence/TASK-FIXTURE", "images": [source.name]}}}
        self.output = automation("^" + name + "$", [name], normal=True)
        def rendered(*args, **kwargs):
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 1280, 720))
            return self.fake_process(*args, **kwargs)
        with patch.object(flow, "_inventory", return_value=inventory), patch.object(flow, "run_process", side_effect=rendered):
            row = flow.run_visual_case(self.request, name, self.run / "checks/visual/attempt-1/case-001", 600)
        self.assertTrue(row["passed"], row)
        self.assertIn("case-001/images/01-frame.png", row["images"][0]["relative_path"])
        source.write_bytes(b"overwritten by later test")
        flow._check_files(row["evidence"], self.run)
        with patch.object(flow, "_inventory", return_value=inventory), patch.object(flow, "run_process", side_effect=self.fake_process):
            second = flow.run_visual_case(self.request, name, self.run / "checks/retest/attempt-1/case-001", 600)
        self.assertFalse(second["passed"])
        self.assertTrue(second["missing_images"])

    def test_package_snapshot_binds_images_and_slot97_header(self):
        directory = self.project / "Saved/Evidence/TASK-LOCAL-0066"
        directory.mkdir(parents=True)
        screenshots = {}
        for scenario in ("plain", "loop"):
            path = directory / (scenario + ".png")
            path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 1280, 720))
            screenshots[scenario] = {"path": str(path), "sha256": flow._sha(path)}
            write_text(directory / (scenario + ".log"), "real fixture process evidence")
        save = directory / "loop-Slot97.zgs"
        write_text(save, 'ZGSAVE1\n{"structureVersion":44,"contentVersion":"zg-defs-dev-56","savedTick":99}\n{}\n')
        value = {"status": "passed", "head_sha": self.request["head_sha"], "workspace": str(self.project),
            "runs": {key: {"facts": {"screenshot": screenshots[key], "log": str(directory / (key + ".log"))}}
                     for key in ("plain", "loop")}}
        value["runs"]["loop"]["facts"].update(save={"path": str(save), "sha256": flow._sha(save)}, summary="compared=300 structure=44 content=zg-defs-dev-56")
        write_json(directory / "package-summary.json", value)
        proof = flow._package_evidence(self.request, "package integration PASS: exit=0", 0,
                                      self.run / "checks/package/attempt-1/package-evidence")
        self.assertTrue(proof["passed"], proof)
        self.assertEqual(proof["save_header"]["structureVersion"], 44)
        self.assertEqual(len(proof["images"]), 2)
        self.assertEqual(proof["compared_steps"], 300)
        write_json(directory / "package-summary.json", {"overwritten": True})
        flow._check_files([proof["summary"], *proof["images"], *proof["artifacts"]], self.run)

    def test_exception_has_failed_receipt_without_synthetic_pass(self):
        self.receipt("prepare")
        with patch.object(flow, "_verify_inputs", side_effect=ValueError("source input changed")):
            result = flow.run_stage(self.path, "build")
        self.assertEqual(result["status"], "failed")
        self.assertIn("source input changed", result["error"])
        self.assertEqual(flow.status(self.request)["failed_stages"], ["build"])

    def test_real_git_archive_and_lfs_hydration_create_exact_isolated_head(self):
        def git(*args):
            return subprocess.check_output(["git", "-C", str(self.source), *args], text=True, encoding="utf-8", stderr=subprocess.STDOUT).strip()
        git("init")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        git("config", "core.autocrlf", "false")
        git("lfs", "install", "--local")
        write_text(self.source / ".gitattributes", "*.uasset filter=lfs diff=lfs merge=lfs -text\n")
        write_text(self.source / ".gitignore", "Saved/\nBinaries/\nIntermediate/\n")
        write_text(self.source / "ZhuGuoCity.uproject", "{}\n")
        write_text(self.source / "Source/test.cpp", "original source\n")
        write_text(self.source / "Tools/package_verification.py", "# committed formal verifier fixture\n")
        write_text(self.source / "Docs/rules.md", "original test dependency\n")
        asset = self.source / "Content/Test.uasset"
        asset.parent.mkdir()
        asset.write_bytes(b"real binary asset\0" * 100)
        git("add", ".")
        git("commit", "-m", "Fixture exact snapshot")
        self.request["head_sha"] = git("rev-parse", "HEAD")
        self.save()
        original = hashlib.sha256(asset.read_bytes()).hexdigest()
        result = flow.run_stage(self.path, "prepare")
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["hydrated_lfs_files"], 1)
        self.assertTrue((self.project / "Docs/rules.md").is_file())
        self.assertEqual(flow._verify_inputs(self.request), result["source_manifest_sha256"])
        self.assertEqual(hashlib.sha256(asset.read_bytes()).hexdigest(), original)
        self.assertEqual(git("status", "--porcelain"), "")
        write_text(self.project / "Source/test.cpp", "changed snapshot")
        with self.assertRaisesRegex(ValueError, "cleanliness changed"):
            flow._verify_inputs(self.request)
        with self.assertRaisesRegex(ValueError, "cleanliness changed"):
            flow.run_stage(self.path, "prepare")


if __name__ == "__main__":
    unittest.main()
