"""Retest authority, mode, budgets, and idempotence without UE or models."""
from copy import deepcopy
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from unattended import full_flow_checks as flow, full_flow_retest as retest, ue_checks
from unattended.storage import read_json, write_json, write_text


A, B, V = "ZhuGuoCity.M2.One", "ZhuGuoCity.M3.Two", "ZhuGuoCity.Local.SampleVisual"


def raw(selector, names, failed=(), normal=False):
    text = f"Found {len(names)} automation tests based on '{selector}'\n"
    text += "".join(f"LogAutomationCommandLine: Display:     {name}\n" for name in names)
    text += "".join(f"Test Completed. Result={{{'Fail' if name in failed else 'Success'}}} Name={{{name.rsplit('.',1)[-1]}}} Path={{{name}}}\n" for name in names)
    text += f"{len(names)} tests performed.\n"
    if normal:
        text += "LogRHI: Using Default RHI: D3D12\n"
    return text


class RetestTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.run_dir = self.root / "runtime/full-flow/runs" / ("a" * 32)
        self.project = self.run_dir / "project"; self.project.mkdir(parents=True)
        engine = self.root / "UE_5.8/Engine"
        for name in ("UnrealEditor.exe", "UnrealEditor-Cmd.exe"):
            write_text(engine / "Binaries/Win64" / name, "fixture never executed")
        self.request = {"id": "a" * 32, "task_id": "TASK-LOCAL-0091", "head_sha": "b" * 40,
                        "request_sha256": "c" * 64, "run_dir": str(self.run_dir), "project_dir": str(self.project),
                        "engine_dir": str(engine)}
        self.folder = self.run_dir / "checks/retest/attempt-1"; self.folder.mkdir(parents=True)
        self.binding = {**self.request, "source_manifest_sha256": "manifest-sha", "finished_at": flow._now()}
        self.calls, self.clock, self.consume_timeout = [], 0.0, False
        self.real_visual_case = flow.run_visual_case
        self.fail_retests, self.timeout_retests = set(), False
        self.addCleanup(patch.stopall)
        patch.object(flow, "load_request", return_value=self.request).start()
        self.inputs = patch.object(flow, "_verify_inputs", return_value="manifest-sha").start()
        patch.object(flow, "_execute", side_effect=self.execute).start()
        patch.object(flow, "run_visual_case", side_effect=self.visual_case, create=True).start()
        patch.object(retest.time, "time", return_value=1000.0).start()
        patch.object(retest.time, "monotonic", side_effect=lambda: self.clock).start()
        self.first_pass([A, B], [V])

    def logs(self, folder, command, text, *, timed_out=False):
        write_text(folder / "stdout.log", text); write_text(folder / "stderr.log", "")
        write_text(folder / "Unreal.log", text)
        process = {"argv": command, "stdout_path": str(folder / "stdout.log"), "stderr_path": str(folder / "stderr.log"),
                   "exit_code": 0, "timed_out": timed_out, "completed": not timed_out, "output_limit_exceeded": False}
        write_json(folder / "process.json", process)
        evidence = [flow._file_record(folder / name) for name in ("stdout.log", "stderr.log", "Unreal.log", "process.json")]
        return process, evidence

    def first_pass(self, regression_failed, visual_failed):
        folder = self.run_dir / "checks/regression/attempt-1"
        text = raw(flow.REGRESSION_SELECTOR, [A, B], regression_failed)
        process, evidence = self.logs(folder, ["UE.exe", "-nullrhi"], text)
        self.regression = {**self.binding, "stage": "regression", "status": "failed" if regression_failed else "passed",
                           "process": process, "evidence": evidence, "automation": flow.parse_regression(text)}
        cases, all_evidence = [], []
        for index, name in enumerate([V], 1):
            text = raw("^" + name + "$", [name], visual_failed, normal=True)
            process, evidence = self.logs(self.run_dir / f"checks/visual/attempt-1/case-{index:03d}",
                                         ["UE.exe", "-ExecCmds=Automation RunTests ^" + name + "$"], text)
            cases.append({"name": name, "mode": "normal", "passed": name not in visual_failed,
                          "status": "FAIL" if name in visual_failed else "PASS", "process": process,
                          "automation": ue_checks.parse_automation_output(text, name),
                          "evidence": evidence})
            all_evidence.extend(evidence)
        self.visual = {**self.binding, "stage": "visual", "status": "failed" if visual_failed else "passed",
                       "cases": cases, "evidence": all_evidence}
        self.save_first_pass()

    def save_first_pass(self):
        write_json(self.run_dir / "checks/regression/receipt.json", self.regression)
        write_json(self.run_dir / "checks/visual/receipt.json", self.visual)

    def execute(self, argv, cwd, output, timeout):
        self.calls.append((argv, timeout))
        selector = next(arg.split("Automation RunTests ", 1)[1] for arg in argv if arg.startswith("-ExecCmds="))
        name = selector[1:-1]
        text = raw(selector, [name], self.fail_retests, normal="-nullrhi" not in argv)
        process, _ = self.logs(output, argv, text, timed_out=self.timeout_retests)
        if self.consume_timeout:
            self.clock += timeout
        return process

    def visual_case(self, request, name, folder, timeout):
        argv = ["UE.exe", "-ExecCmds=Automation RunTests ^" + name + "$", "-d3d12"]
        process = self.execute(argv, self.project, folder, timeout)
        passed = name not in self.fail_retests and not self.timeout_retests
        return {"name": name, "mode": "normal", "status": "PASS" if passed else "FAIL", "passed": passed,
                "process": process, "evidence": [flow._file_record(folder / f) for f in ("stdout.log", "stderr.log", "process.json", "Unreal.log")]}

    def invoke(self, started=1000.0):
        return retest.run(self.request, self.folder, started)

    def test_only_verified_failures_run_separately_once_in_original_modes(self):
        original = [(self.run_dir / f"checks/{stage}/receipt.json").read_bytes() for stage in ("regression", "visual")]
        result = self.invoke()
        self.assertEqual(result["planned_count"], 3); self.assertEqual(result["attempted_count"], 3)
        self.assertEqual(result["newly_passed"], [A, B, V]); self.assertTrue(result["passed"])
        for (argv, timeout), name in zip(self.calls, [A, B, V]):
            self.assertIn("-ExecCmds=Automation RunTests ^" + name + "$", argv)
            self.assertEqual("-nullrhi" in argv, name != V); self.assertLessEqual(timeout, 600)
        self.assertEqual(original, [(self.run_dir / f"checks/{stage}/receipt.json").read_bytes() for stage in ("regression", "visual")])
        self.invoke(); self.assertEqual(len(self.calls), 3)

    def test_no_failures_means_zero_executions_without_fake_successes(self):
        self.first_pass([], [])
        result = self.invoke()
        self.assertTrue(result["no_failures"]); self.assertTrue(result["passed"])
        self.assertEqual(result["rechecked"], []); self.assertEqual(result["attempted_count"], 0)
        self.assertEqual(self.calls, [])

    def test_still_failed_and_new_success_are_kept_separately(self):
        self.fail_retests = {B}
        result = self.invoke()
        self.assertEqual(result["still_failed"], [B]); self.assertEqual(result["newly_passed"], [A, V])
        self.assertFalse(result["passed"])

    def test_total_budget_caps_later_calls_and_records_not_run(self):
        self.consume_timeout = True
        result = self.invoke(1000.0 - retest.TOTAL_SECONDS + 650)
        self.assertEqual([timeout for _, timeout in self.calls], [600, 50])
        self.assertEqual(result["not_run"], [V]); self.assertFalse(result["passed"])

    def test_foreign_receipt_or_wrong_source_never_launches(self):
        for key in ("request_sha256", "head_sha", "source_manifest_sha256", "task_id"):
            old = self.regression[key]; self.regression[key] = "wrong"; self.save_first_pass()
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "exact source/run"):
                self.invoke()
            self.regression[key] = old
        self.assertEqual(self.calls, [])

    def test_modified_first_pass_log_and_invented_names_are_rejected(self):
        path = Path(self.regression["process"]["stdout_path"]); original = path.read_bytes()
        path.write_bytes(original + b"changed")
        with self.assertRaisesRegex(ValueError, "Recorded evidence changed"):
            self.invoke()
        path.write_bytes(original)
        self.regression["automation"]["failed_paths"] = ["ZhuGuoCity.M2.Invented"]
        self.save_first_pass()
        with self.assertRaisesRegex(ValueError, "disagree"):
            self.invoke()
        self.assertEqual(self.calls, [])

    def test_wrong_mode_does_not_get_replayed(self):
        self.visual["cases"][0]["mode"] = "nullrhi"; self.save_first_pass()
        with self.assertRaisesRegex(ValueError, "mode/name"):
            self.invoke()
        self.assertEqual(self.calls, [])

    def test_false_pass_and_not_run_remain_unresolved_without_new_authority(self):
        self.first_pass([], [])
        self.visual["cases"][0].update(status="FALSE_PASS", passed=False)
        self.visual["status"] = "failed"; self.save_first_pass()
        result = self.invoke()
        self.assertFalse(result["passed"]); self.assertFalse(result["no_failures"])
        self.assertEqual(result["unresolved_first_pass"][0]["reason"], "FALSE_PASS")
        self.assertEqual(self.calls, [])

    def test_visual_setup_failure_does_not_prevent_other_bound_failure_retests(self):
        self.visual["cases"][0].pop("process"); self.save_first_pass()
        result = self.invoke()
        self.assertEqual(result["newly_passed"], [A, B])
        self.assertEqual(result["unresolved_first_pass"][0]["reason"], "first_pass_process_not_run")
        self.assertFalse(result["passed"])

    def test_real_visual_case_interface_preserves_archived_png_on_retest(self):
        import base64
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+cclcAAAAASUVORK5CYII=")
        source = self.project / "Saved/Evidence/TASK-LOCAL-0001/01.png"
        def execute(argv, cwd, output, timeout):
            process = self.execute(argv, cwd, output, timeout)
            source.parent.mkdir(parents=True, exist_ok=True); source.write_bytes(png)
            return process
        inventory = {"visual": {V: {"directory": "Saved/Evidence/TASK-LOCAL-0001", "images": ["01.png"]}}}
        with patch.object(flow, "run_visual_case", side_effect=self.real_visual_case), patch.object(flow, "_inventory", return_value=inventory), patch.object(flow, "_execute", side_effect=execute):
            result = self.invoke()
        self.assertEqual(result["newly_passed"], [A, B, V])
        visual = result["rechecked"][-1]
        self.assertEqual(visual["images"][0]["sha256"], flow._sha(source))
        self.assertTrue(Path(visual["images"][0]["path"]).is_relative_to(self.folder / "case-003/images"))

    def test_timed_out_retest_is_failure_even_with_success_text(self):
        self.timeout_retests = True
        result = self.invoke()
        self.assertEqual(result["newly_passed"], [])
        self.assertEqual(result["still_failed"], [A, B, V])

    def test_source_drift_stops_before_next_process(self):
        self.inputs.side_effect = ["manifest-sha", "manifest-sha", "changed"]
        with self.assertRaisesRegex(ValueError, "source changed during"):
            self.invoke()
        self.assertEqual(len(self.calls), 1)

    def test_interrupted_retest_is_not_implicitly_submitted_again(self):
        result = self.invoke()
        path = self.folder / "case-001/receipt.json"; value = read_json(path)
        value["status"] = "RUNNING"; write_json(path, value)
        result = self.invoke()
        self.assertIn(A, result["not_run"]); self.assertEqual(len(self.calls), 3)

    def test_reparse_evidence_path_is_rejected(self):
        from unattended import ue_checks
        real = ue_checks._is_reparse
        target = self.run_dir / "checks/regression"
        with patch.object(ue_checks, "_is_reparse", side_effect=lambda p: p == target or real(p)):
            with self.assertRaisesRegex(ValueError, "junctions"):
                self.invoke()
        self.assertEqual(self.calls, [])

    def forced_rhi_fixture(self, *, suffix="", failure=False, missing=False, timed_out=False):
        self.first_pass([], [])
        folder = self.run_dir / "checks/visual/attempt-1/case-001"
        command = ue_checks._command(ue_checks._binaries(Path(self.request["engine_dir"])),
            self.project / "ZhuGuoCity.uproject", {"name": V, "mode": "normal"}, folder / "Unreal.log")
        command.extend(["-d3d12", "-windowed", "-ResX=1280", "-ResY=720"])
        text = raw("^" + V + "$", [V], [V] if failure else []) + "LogRHI: Using Forced RHI: D3D12\n" + suffix
        process, evidence = self.logs(folder, command, text, timed_out=timed_out)
        image_path = folder / "images/01.png"
        image_path.parent.mkdir(exist_ok=True)
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 2, 2))
        image = {**flow._file_record(image_path), "relative_path": image_path.relative_to(self.run_dir).as_posix(),
                 "source_relative_path": "project/Saved/Evidence/TASK-LOCAL-0001/01.png", "dimensions": [2, 2]}
        case = {"name": V, "mode": "normal", "passed": False, "status": "FAIL", "process": process,
                "automation": ue_checks.parse_automation_output(text, V), "images": [image],
                "evidence": [*evidence, image], "rhi": {"normal_rhi_proven": False, "expected": "D3D12"},
                "skip_lines": [], "missing_images": [{"path": "missing"}] if missing else []}
        write_json(folder / "receipt.json", case)
        self.visual.update(attempt=1, status="failed", cases=[case], evidence=[*case["evidence"], flow._file_record(folder / "receipt.json")])
        self.save_first_pass()
        inventory = {"visual": {V: {"directory": "Saved/Evidence/TASK-LOCAL-0001", "images": ["01.png"]}}}
        patch.object(flow, "_inventory", return_value=inventory).start()
        return folder

    def test_forced_rhi_parser_failure_is_derived_without_reexecution_or_rewriting(self):
        folder = self.forced_rhi_fixture(suffix="LogD3D12RHI: Skipped NVAPI RT queries since feature level is below SM6\n"
                                        "LogShaderCompilers: Display: Autogen file is unchanged, skipping write.\n")
        originals = {path: path.read_bytes() for path in (folder / "receipt.json", self.run_dir / "checks/visual/receipt.json")}
        result = self.invoke()
        self.assertTrue(result["passed"]); self.assertFalse(result["no_failures"])
        self.assertEqual(result["attempted_count"], 0); self.assertEqual(result["newly_passed"], [])
        self.assertEqual(result["rechecked"], []); self.assertEqual(self.calls, [])
        correction = result["reconciled_tool_false_failure"][0]
        self.assertEqual(correction["original_status"], "FAIL")
        self.assertEqual(correction["execution"], "not_reexecuted")
        derived = Path(correction["derived_receipt"]["path"])
        self.assertEqual(read_json(derived)["source_manifest_sha256"], "manifest-sha")
        self.assertEqual(flow._sha(derived), correction["derived_receipt"]["sha256"])
        self.assertEqual(self.invoke()["reconciled_tool_false_failure"], result["reconciled_tool_false_failure"])
        self.assertEqual(originals, {path: path.read_bytes() for path in originals})

    def test_forced_rhi_never_reconciles_real_failure_missing_image_timeout_or_skip(self):
        variants = ({"failure": True}, {"missing": True}, {"timed_out": True}, {"suffix": "skipped: graphics scenario\n"},
                    {"suffix": "LogRHI: Using Forced RHI: Null\n"}, {"suffix": "Fatal error: actual crash\n"})
        for variant in variants:
            self.forced_rhi_fixture(**variant)
            with self.subTest(variant=variant):
                selected, _, _, corrected = retest._first_pass(flow, self.request, "manifest-sha")
                self.assertEqual(corrected, [])
                self.assertEqual(selected, [{"name": V, "mode": "normal", "source_stage": "visual"}])

    def test_reconciliation_needs_bound_original_case_receipt_and_all_expected_images(self):
        folder = self.forced_rhi_fixture()
        self.visual["evidence"] = [record for record in self.visual["evidence"] if record["path"] != str(folder / "receipt.json")]
        self.save_first_pass()
        selected, _, _, corrected = retest._first_pass(flow, self.request, "manifest-sha")
        self.assertEqual(corrected, []); self.assertEqual(len(selected), 1)
        self.forced_rhi_fixture()
        with patch.object(flow, "_inventory", return_value={"visual": {V: {"directory": "Saved/Evidence/TASK-LOCAL-0001", "images": ["01.png", "02.png"]}}}):
            self.assertEqual(retest._first_pass(flow, self.request, "manifest-sha")[3], [])

    def test_reconciliation_rejects_tampered_archived_png_before_any_launch(self):
        folder = self.forced_rhi_fixture()
        (folder / "images/01.png").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            self.invoke()
        self.assertEqual(self.calls, [])

    def test_derived_receipt_cannot_be_silently_replaced(self):
        self.forced_rhi_fixture()
        result = self.invoke()
        path = Path(result["reconciled_tool_false_failure"][0]["derived_receipt"]["path"])
        value = read_json(path); value["head_sha"] = "wrong"; write_json(path, value)
        with self.assertRaisesRegex(ValueError, "different evidence"):
            self.invoke()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
