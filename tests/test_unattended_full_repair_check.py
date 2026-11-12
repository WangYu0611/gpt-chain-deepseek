"""Complete repair gates against temporary Git and simulated native UE receipts."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import struct
import subprocess
import unittest
from unittest.mock import patch

from tests.test_unattended_claude_user_repair import ClaudeUserRepairTests, save, sha
from tests.test_unattended_full_flow_checks import automation
from unattended import full_repair_check as check
from unattended.storage import read_json, write_json, write_text


class FullRepairCheckTests(unittest.TestCase):
    def setUp(self):
        self.f = ClaudeUserRepairTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.work, self.runtime, self.engine = self.f.work, self.f.runtime, self.f.engine
        for name in ("Binaries/ThirdParty/DotNet/10.0/win-x64/dotnet.exe", "Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.dll"):
            write_text(self.engine / name, "fixture native executable")
        write_text(self.work / ".gitignore", "Saved/\nBinaries/\nIntermediate/\n")
        write_text(self.work / "Source/ZhuGuoCity/ZgSave.h", "static constexpr int32 CurrentStructureVersion = 45;\n")
        write_text(self.work / "Tools/package_verification.py", 'EXPECTED_STRUCTURE_VERSION = 45\nEXPECTED_CONTENT_VERSION = "zg-defs-dev-57"\n')
        write_json(self.work / "Content/Data/ZgDefinitions/resources.json", {"contentVersion": "zg-defs-dev-57"})
        self.git("init"); self.git("config", "user.email", "test@example.invalid"); self.git("config", "user.name", "Fixture")
        self.git("add", "."); self.git("commit", "-m", "fixture")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.inventory = {"regression": ["ZhuGuoCity.M0.Fixture", check.PACKAGE],
                          "visual": {name: {"directory": "Saved/Evidence/FIXTURE", "images": [f"{index:02}.png"]}
                                     for index, name in enumerate(self.f.visuals)}, "sources": {}}
        manifest = self.f.run / "source-manifest.json"
        data = read_json(manifest); data["automation_inventory"] = self.inventory; write_json(manifest, data)
        retest = self.f.run / "checks/retest/receipt.json"
        data = read_json(retest); data["source_manifest_sha256"] = sha(manifest); write_json(retest, data)
        source_inventory = self.f.request_path.parent / "source-inventory.json"
        write_json(source_inventory, self.inventory)
        for row in self.f.request["evidence"]:
            p = Path(row["path"]); row.update(sha256=sha(p), size_bytes=p.stat().st_size)
        self.f.request["evidence"].append({"role": "source_inventory", "path": str(source_inventory),
                                         "sha256": sha(source_inventory), "size_bytes": source_inventory.stat().st_size})
        save(self.f.request_path, self.f.request)
        self.f.profile["request_sha256"] = sha(self.f.request_path)
        self.f.state["task"]["user_repair"] = deepcopy(self.f.profile)
        self.f.state["task"]["candidate"] = {"head_sha": self.head}
        self.f.state["phase"] = "CHECK"
        self.f.state["operation"].update(kind="check", check_id=check.CHECK_ID, head_sha=self.head,
                                         user_repair_request_sha256=self.f.profile["request_sha256"])
        self.f.persist_state()
        self.calls = []
        self.failed = set(); self.package_pass = True; self.missing_image = None; self.drift = False
        self.addCleanup(patch.stopall)
        patch.object(check.flow, "source_inventory", side_effect=lambda _: deepcopy(self.inventory)).start()
        self.execute = patch.object(check.flow, "_execute", side_effect=self.fake_execute).start()

    def git(self, *argv):
        return subprocess.check_output(["git", "-C", str(self.work), *argv], stderr=subprocess.DEVNULL, text=True)

    def png(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 1280, 720))

    def package(self):
        root = self.work / "Saved/Evidence/TASK-LOCAL-0066"
        runs = {}
        for name in ("plain", "loop"):
            image = root / (name + ".png"); self.png(image)
            log = root / ("game-" + name + ".log")
            write_text(log, "actual packaged plain" if name == "plain" else "[ZgPackageLoopDemo] SUMMARY PASS compared=300")
            # The external source is deliberately outside the readable workspace.
            facts = {"log": str(self.runtime / "zgpkg-runs/20261108T120000Z-abcd1234" / name / "Saved/Logs" / (name + ".log")),
                     "screenshot": {"path": str(image), "sha256": sha(image)}}
            runs[name] = {"passed": True, "facts": facts, "game_log_copy": {"path": str(log), "sha256": sha(log)}}
        savefile = root / "loop-Slot97.zgs"
        write_text(savefile, "ZGSAVE1\n" + json.dumps({"structureVersion": 45, "contentVersion": "zg-defs-dev-57"}) + "\n{}")
        runs["loop"]["facts"].update(summary="compared=300", save={"path": str(savefile), "sha256": sha(savefile)})
        write_json(root / "package-summary.json", {"status": "passed", "head_sha": self.head, "workspace": str(self.work), "runs": runs})

    def fake_execute(self, argv, cwd, output, timeout):
        self.calls.append((argv, timeout))
        output.mkdir(parents=True, exist_ok=True)
        if "-Rebuild" in argv:
            raw, log = "Result: Succeeded", output / "UnrealBuildTool.log"
        else:
            selector = next(arg.split("Automation RunTests ", 1)[1] for arg in argv if arg.startswith("-ExecCmds="))
            if selector == "^ZhuGuoCity.M":
                raw = automation(selector, self.inventory["regression"], failed=self.failed, package_skip=True)
            else:
                name = selector[1:-1]
                raw = automation(selector, [name], failed=self.failed, normal=name != check.PACKAGE)
                if name in self.inventory["visual"]:
                    raw = raw.replace("Using Default RHI", "Using Forced RHI")
                    for filename in self.inventory["visual"][name]["images"]:
                        if filename != self.missing_image:
                            self.png(self.work / self.inventory["visual"][name]["directory"] / filename)
                elif self.package_pass:
                    raw += "\npackage integration PASS: exit=0"; self.package()
                else:
                    raw += "\npackage integration actual FAIL: UAT build failed"
            raw += "\nLogD3D12RHI: Skipped NVAPI RT queries since feature level is below SM6"
            raw += "\nLogShaderCompilers: Display: Autogen file is unchanged, skipping write."
            raw += "\nLogPython: Display: No pip-enabled plugins with python dependencies found, skipping"
            log = output / "Unreal.log"
        write_text(output / "stdout.log", raw); write_text(output / "stderr.log", ""); write_text(log, raw)
        result = {"argv": argv, "cwd": str(cwd), "timeout_seconds": timeout, "exit_code": 0, "timed_out": False,
                  "output_limit_exceeded": False, "completed": True, "stdout_path": str(output / "stdout.log"),
                  "stderr_path": str(output / "stderr.log")}
        write_json(output / "process.json", result)
        if self.drift:
            write_text(self.work / "Tools/package_verification.py", "changed during execution")
        return result

    def run_check(self, self_test=False):
        with redirect_stdout(io.StringIO()) as output:
            value = check.run(self.runtime, self.work, self.engine, self_test=self_test)
        self.output = output.getvalue()
        return value

    def verify(self):
        return check.verify_repair_check(self.runtime, self.work, self.f.state["task"], self.head)

    def test_complete_gate_parses_real_receipts_archives_all_images_and_supports_upgraded_save(self):
        value = self.run_check()
        self.assertTrue(value["passed"], value.get("error") or value["stages"])
        self.assertEqual(len(self.calls), 22)  # rebuild, full M, 19 exact Local, formal package
        self.assertEqual(len(value["images"]), 21)
        self.assertTrue(value["stages"]["regression"]["automation"]["package_skipped"])
        self.assertFalse(value["stages"]["regression"]["automation"]["passed"])
        self.assertEqual(value["stages"]["package"]["package"]["save_header"]["structureVersion"], 45)
        self.assertTrue(self.verify()["passed"])
        self.assertIn('"receipt_path"', self.output); self.assertIn(self.f.visuals[-1], self.output)
        self.f.state.update(phase="PUBLISH", operation=None); self.f.persist_state()
        self.assertTrue(self.verify()["passed"])  # saved CHECK proof survives phase transitions

    def test_test_failure_or_missing_new_screenshot_cannot_be_hidden_by_package_success(self):
        self.failed = {"ZhuGuoCity.M0.Fixture"}; self.missing_image = "00.png"
        value = self.run_check()
        self.assertFalse(value["passed"])
        self.assertFalse(value["stages"]["regression"]["passed"])
        self.assertFalse(value["stages"]["visual"]["passed"])
        self.assertTrue(value["stages"]["package"]["passed"])
        with self.assertRaisesRegex(ValueError, "did not pass"): self.verify()

    def test_prefix_package_skip_never_closes_without_successful_formal_package(self):
        self.package_pass = False
        value = self.run_check()
        self.assertFalse(value["passed"])
        self.assertTrue(value["stages"]["regression"]["passed"])
        self.assertFalse(value["stages"]["package"]["passed"])

    def test_wrong_stage_check_identity_and_dirty_final_fail_before_any_native_launch(self):
        for key, bad in (("check_id", "ue-task-validation"), ("task_id", "OTHER"), ("head_sha", "0" * 40)):
            original = self.f.state["operation"][key]; self.f.state["operation"][key] = bad; self.f.persist_state()
            with self.subTest(key=key), self.assertRaises(ValueError): self.run_check()
            self.f.state["operation"][key] = original
        self.f.persist_state(); write_text(self.work / "Tools/new.py", "uncommitted")
        with self.assertRaises(ValueError): self.run_check()
        self.execute.assert_not_called()

    def test_selftest_dirty_snapshot_is_distinct_and_never_an_independent_gate(self):
        self.f.state["phase"] = "CODE"; self.f.state["operation"]["kind"] = "code"; self.f.persist_state()
        write_text(self.work / "Tools/new.py", "uncommitted source")
        value = self.run_check(self_test=True)
        self.assertEqual(value["scope"], check.SELF_SCOPE)
        self.assertFalse(value["source_before"]["clean"])
        self.assertFalse((self.work / "Saved/Automation" / self.f.task_id / "full-repair-results.json").exists())
        with self.assertRaises(ValueError): self.verify()

    def test_source_drift_including_tools_is_detected_before_more_execution(self):
        self.drift = True
        value = self.run_check()
        self.assertFalse(value["passed"]); self.assertIn("Source inputs changed", value["error"])
        self.assertEqual(len(self.calls), 1)

    def test_removing_registered_case_or_reducing_original_images_is_rejected(self):
        original = deepcopy(self.inventory)
        for mutation in ("case", "images"):
            self.inventory = deepcopy(original)
            if mutation == "case": self.inventory["visual"].pop(self.f.visuals[0])
            else: self.inventory["visual"][self.f.visuals[0]]["images"] = []
            value = self.run_check()
            self.assertFalse(value["passed"]); self.assertTrue("removed" in value["error"] or "reduced" in value["error"])
        self.execute.assert_not_called()

    def test_new_registered_local_is_automatically_included(self):
        name = "ZhuGuoCity.Local.M3ServicesVisual"
        self.inventory["visual"][name] = {"directory": "Saved/Evidence/NEW", "images": [f"new-{n}.png" for n in range(6)]}
        value = self.run_check()
        self.assertTrue(value["passed"], value.get("error"))
        self.assertEqual(value["stages"]["visual"]["cases"][-1]["name"], name)
        self.assertEqual(len(value["images"]), 27)

    def test_archived_raw_proof_or_source_mutation_cannot_reuse_passed_flags(self):
        value = self.run_check(); self.assertTrue(value["passed"])
        mirror = check._mirror(self.work, self.f.task_id, False)
        pointer = read_json(mirror); path = Path(pointer["receipt_path"]); run = path.parent
        write_text(run / "regression/Unreal.log", "Forged successful message")
        with self.assertRaisesRegex(ValueError, "evidence changed"): self.verify()
        # Even replacing both the aggregate SHA and the per-file hash cannot
        # replace the missing automation execution with a passed JSON flag.
        receipt = read_json(path)
        for row in receipt["evidence"]:
            if row["relative_path"] == "regression/Unreal.log":
                p = run / row["relative_path"]; row.update(sha256=sha(p), size_bytes=p.stat().st_size)
        write_json(path, receipt); pointer["receipt_sha256"] = sha(path); write_json(mirror, pointer)
        with self.assertRaisesRegex(ValueError, "raw execution"): self.verify()

    def test_actual_normal_startup_skips_do_not_mask_explicit_test_skip(self):
        process = {"exit_code": 0, "completed": True, "timed_out": False, "output_limit_exceeded": False}
        name = self.f.visuals[0]
        raw = automation("^" + name + "$", [name], normal=True).replace("Default", "Forced")
        raw += "\nLogD3D12RHI: Skipped NVAPI RT queries since feature level is below SM6"
        raw += "\nLogShaderCompilers: Autogen file is unchanged, skipping write.\nLogPython: No dependencies found, skipping"
        self.assertTrue(check._parse("visual", raw, process, self.inventory, name)["passed"])
        raw += "\nLogAutomationController: skipped: visual host unavailable"
        self.assertFalse(check._parse("visual", raw, process, self.inventory, name)["passed"])

    def test_packaged_summary_cannot_claim_a_loop_absent_from_actual_native_log(self):
        self.package()
        source = self.work / "Saved/Evidence/TASK-LOCAL-0066"
        write_text(source / "game-loop.log", "No game loop ran")
        summary = read_json(source / "package-summary.json")
        summary["runs"]["loop"]["game_log_copy"]["sha256"] = sha(source / "game-loop.log")
        write_json(source / "package-summary.json", summary)
        run = self.runtime / "proof-test"; run.mkdir()
        with self.assertRaisesRegex(ValueError, "archived native log"):
            check._package_archive(self.work, run, run / "package", self.head, 0)

    def test_packaged_summary_preserved_windows_cr_matches_actual_log(self):
        self.package()
        source = self.work / "Saved/Evidence/TASK-LOCAL-0066"
        summary = read_json(source / "package-summary.json")
        summary["runs"]["loop"]["facts"]["summary"] += "\r"
        write_json(source / "package-summary.json", summary)
        run = self.runtime / "proof-crlf-test"; run.mkdir()
        proof = check._package_archive(self.work, run, run / "package", self.head, 0)
        self.assertTrue(proof["passed"])
        self.assertEqual(proof["compared_steps"], 300)


if __name__ == "__main__":
    unittest.main()
