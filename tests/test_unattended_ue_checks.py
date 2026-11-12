"""Fixed UE bridge contract tests: local Git plus fake processes, never UE/models."""
from __future__ import annotations

import base64
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from github_harness_lab.lab_harness.process import ProcessResult
from unattended import gitops
from unattended.storage import read_json, write_json
from unattended.ue_checks import (
    BUILD_TIMEOUT_SECONDS, MAX_OUTPUT_BYTES, REGRESSION_TIMEOUT_SECONDS, TEST_TIMEOUT_SECONDS,
    ValidationError, main, parse_automation_output, parse_batch_automation_output, parse_milestone_regression_output, run_validation,
    validate_core_tests, validate_request,
)


TASK = "TASK-LOCAL-0042"
TEST = "ZhuGuoCity.M2.CatalogGroups"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+cclcAAAAASUVORK5CYII=")


def successful_output(name=TEST):
    return (
        f"LogAutomationCommandLine: Display: Found 1 automation tests based on '^{name}$'\n"
        f"LogAutomationController: Display: Test Completed. Result={{Success}} Name={{{name.rsplit('.', 1)[-1]}}} Path={{{name}}}\n"
        "LogAutomationCommandLine: Display: ...Automation Test Queue Empty 1 tests performed.\n"
    )


def milestone_output(*names):
    return (
        f"LogAutomationCommandLine: Display: Found {len(names)} automation tests based on '^ZhuGuoCity.M'\n"
        + "".join(f"LogAutomationCommandLine: Display: \t{name}\n" for name in names)
        + "".join(
            f"LogAutomationController: Display: Test Completed. Result={{Success}} Name={{{name.rsplit('.', 1)[-1]}}} Path={{{name}}}\n"
            for name in names
        )
        + f"LogAutomationCommandLine: Display: ...Automation Test Queue Empty {len(names)} tests performed.\n"
    )


def batch_output(names, failed=()):
    selector = "+".join("^" + name + "$" for name in names)
    output = f"LogAutomationCommandLine: Display: Found {len(names)} automation tests based on '{selector}'\n"
    output += "".join(f"LogAutomationCommandLine: Display: \t{name}\n" for name in names)
    for name in names:
        result = "Fail" if name in failed else "Success"
        output += f"LogAutomationController: Display: Test Completed. Result={{{result}}} Name={{{name.rsplit('.', 1)[-1]}}} Path={{{name}}}\n"
    return output + f"LogAutomationCommandLine: Display: ...Automation Test Queue Empty {len(names)} tests performed.\n"


class RequestContractTests(unittest.TestCase):
    def request(self, **changes):
        return {"version": 1, "task_id": TASK, "head_sha": "a" * 40,
                "tests": [{"name": TEST, "mode": "nullrhi"}], **changes}

    def test_unknown_fields_arbitrary_commands_wrong_binding_and_invalid_version_rejected(self):
        values = [self.request(command="whoami"), self.request(head_sha="b" * 40),
                  self.request(task_id="OTHER"), self.request(version=True), self.request(version=2),
                  self.request(tests=[{"name": TEST, "mode": "normal", "argv": ["whoami"]}])]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_request(value, TASK, "a" * 40)

    def test_no_tests_duplicate_and_excessive_tests_rejected(self):
        values = [[], [{"name": TEST, "mode": "normal"}] * 2,
                  [{"name": f"ZhuGuoCity.M2.T{i}", "mode": "normal"} for i in range(17)]]
        for tests in values:
            with self.subTest(tests=tests), self.assertRaises(ValidationError):
                validate_request(self.request(tests=tests), TASK, "a" * 40)

    def test_batches_are_bounded_exact_m2_names_without_duplicates_or_commands(self):
        valid = [["ZhuGuoCity.M2.VarietyHud", "ZhuGuoCity.M2.VarietySave"]]
        self.assertEqual(validate_request(self.request(test_batches=valid), TASK, "a" * 40)["test_batches"], valid)
        invalid = [[], "names", [[]], [[TEST]], [["ZhuGuoCity.M1.Save"]], [["ZhuGuoCity.M2.Save;Quit"]],
                   [["ZhuGuoCity.M2.Save", "ZhuGuoCity.M2.Save"]],
                   [["ZhuGuoCity.M2.Save"], ["ZhuGuoCity.M2.Save"]],
                   [[f"ZhuGuoCity.M2.T{i}" for i in range(17)]],
                   [[f"ZhuGuoCity.M2.T{i}"] for i in range(17)],
                   [[{"name": "ZhuGuoCity.M2.Save", "mode": "normal"}]]]
        for batches in invalid:
            with self.subTest(batches=batches), self.assertRaises(ValidationError):
                validate_request(self.request(test_batches=batches), TASK, "a" * 40)

    def test_planned_core_tests_require_unique_exact_project_names(self):
        self.assertEqual(validate_core_tests([TEST]), [TEST])
        invalid = (None, TEST, [], [TEST, TEST.lower()], [TEST + ";Quit"],
                   ["ZhuGuoCity.M2.*"], [f"ZhuGuoCity.M2.T{i}" for i in range(6)])
        for names in invalid:
            with self.subTest(names=names), self.assertRaises(ValidationError):
                validate_core_tests(names)

    def test_clean_build_accepts_only_boolean_and_never_arguments(self):
        for value in (True, False):
            self.assertEqual(validate_request(self.request(clean_build=value), TASK, "a" * 40)["clean_build"], value)
        for value in (None, 0, 1, "true", [], {}, {"argv": ["whoami"]}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_request(self.request(clean_build=value), TASK, "a" * 40)
        with self.assertRaises(ValidationError):
            validate_request(self.request(clean_build=True, build_argv=["whoami"]), TASK, "a" * 40)

    def test_command_injection_test_names_and_unsafe_modes_rejected(self):
        names = ["OtherProject.Test", TEST + ";Quit", TEST + " -ExecCmds=Quit", TEST + "\nQuit",
                 "ZhuGuoCity..Test", "ZhuGuoCity.M2.*", "ZhuGuoCity.M2.Test'", "ZhuGuoCity.", "ZhuGuoCity",
                 "^" + TEST, TEST + "$"]
        for name in names:
            with self.subTest(name=name), self.assertRaises(ValidationError):
                validate_request(self.request(tests=[{"name": name, "mode": "normal"}]), TASK, "a" * 40)
        with self.assertRaises(ValidationError):
            validate_request(self.request(tests=[{"name": TEST, "mode": "unrestricted"}]), TASK, "a" * 40)

    def test_only_task_artifact_roots_with_no_traversal_drive_ads_or_aliases(self):
        paths = ["../outside.png", "/Saved/Evidence/" + TASK + "/x.png", r"C:\outside.png",
                 r"\\server\share\x.png", f"Saved/Evidence/{TASK}/../x.png",
                 "Saved/Evidence/OTHER/x.png", f"Saved/Evidence/{TASK}/x.png:ads",
                 f"Saved/Evidence/{TASK}/CON", f"Saved/Evidence/{TASK}/x.png.",
                 f"Saved/Evidence/{TASK}//x.png", f"Saved/Evidence/{TASK}/x\n.png"]
        for path in paths:
            with self.subTest(path=path), self.assertRaises(ValidationError):
                validate_request(self.request(artifacts=[path]), TASK, "a" * 40)
        with self.assertRaises(ValidationError):
            validate_request(self.request(artifacts=[f"Saved/Evidence/{TASK}/x.png", f"Saved/Evidence/{TASK}/X.PNG"]), TASK, "a" * 40)

    def test_same_test_can_be_requested_once_in_each_mode_and_both_artifact_roots_work(self):
        value = self.request(tests=[{"name": TEST, "mode": "nullrhi"}, {"name": TEST, "mode": "normal"}],
                             artifacts=[f"Saved/Evidence/{TASK}/shot.png", f"Docs/Reports/Evidence/{TASK}/stage.png"])
        self.assertEqual(validate_request(value, TASK, "a" * 40), value)


class AutomationOutputTests(unittest.TestCase):
    def test_exact_real_ue_success_contract(self):
        self.assertTrue(parse_automation_output(successful_output(), TEST)["passed"])

    def test_batch_requires_exact_discovery_and_completion_sets(self):
        names = ["ZhuGuoCity.M2.VarietyHud", "ZhuGuoCity.M2.VarietySave"]
        good = batch_output(names)
        self.assertTrue(parse_batch_automation_output(good, names)["passed"])
        mixed = parse_batch_automation_output(batch_output(names, [names[0]]), names)
        self.assertTrue(mixed["complete"])
        self.assertFalse(mixed["passed"])
        self.assertEqual(mixed["failed_paths"], [names[0]])
        variants = [good.replace("Found 2", "Found 1"), good.replace("2 tests performed", "1 tests performed"),
                    good.replace("^" + names[0] + "$+", names[0] + "+"),
                    good.replace("\t" + names[0], "\tZhuGuoCity.M2.Other"),
                    good.replace("Path={" + names[0] + "}", "Path={ZhuGuoCity.M2.Other}"),
                    good + good, batch_output([names[0], names[0]])]
        for output in variants:
            with self.subTest(output=output):
                self.assertFalse(parse_batch_automation_output(output, names)["complete"])

    def test_success_prose_zero_discovery_wrong_path_and_false_exit_are_insufficient(self):
        good = successful_output()
        variants = ["Success!", good.replace("Found 1", "Found 0"), good.replace("Found 1", "Found 2"),
                    good.replace("1 tests performed", "0 tests performed"),
                    good.replace(f"Path={{{TEST}}}", "Path={ZhuGuoCity.M2.Other}"),
                    good.replace("Result={Success}", "Result={Fail}"),
                    good + "Test Completed. Result={Fail}\n", good + "Fatal error: stopped\n",
                    good.replace(f"'^{TEST}$'", f"'{TEST}'"),
                    good + good]
        for output in variants:
            with self.subTest(output=output):
                self.assertFalse(parse_automation_output(output, TEST)["passed"])

    def test_milestone_regression_requires_every_discovered_nonvisual_case_to_succeed(self):
        names = ("ZhuGuoCity.M0.Save", "ZhuGuoCity.M10.Save", TEST)
        good = milestone_output(*names)
        self.assertTrue(parse_milestone_regression_output(good)["passed"])
        variants = [
            milestone_output(),
            milestone_output(TEST, TEST),
            milestone_output(TEST, "ZhuGuoCity.Local.BridgeVisual"),
            good.replace("3 tests performed", "2 tests performed"),
            good.replace("Found 3", "Found 4"),
            good.replace("Result={Success}", "Result={Fail}", 1),
            good.replace("Path={ZhuGuoCity.M0.Save}", f"Path={{{TEST}}}"),
        ]
        for output in variants:
            with self.subTest(output=output):
                self.assertFalse(parse_milestone_regression_output(output)["passed"])


class FakeUE:
    def __init__(self):
        self.calls = []
        self.effect = None
        self.output = successful_output
        self.batch_output = batch_output
        self.exit_code = 0
        self.timed_out = False
        self.overflow = False
        self.build_exit_code = 0
        self.build_timed_out = False
        self.build_overflow = False
        self.build_output = "Result: Succeeded\n"

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.effect:
            self.effect(argv, kwargs)
        if "-Rebuild" in argv:
            directory = kwargs["output_dir"]
            stdout, stderr = directory / "stdout.log", directory / "stderr.log"
            stdout.write_text(self.build_output, encoding="utf-8")
            stderr.write_text("", encoding="utf-8")
            log = Path(next(value.removeprefix("-log=") for value in argv if value.startswith("-log=")))
            log.write_text("Parsing headers for ZhuGuoCityEditor\nRunning Internal UnrealHeaderTool\n", encoding="utf-8")
            return ProcessResult(self.build_exit_code, self.build_timed_out, self.build_overflow, stdout, stderr)
        selector = next(value.removeprefix("-ExecCmds=Automation RunTests ") for value in argv if value.startswith("-ExecCmds="))
        if not selector.startswith("^") or not selector.endswith("$"):
            raise AssertionError("The fixed bridge must request UE literal full-name boundaries")
        directory = kwargs["output_dir"]
        stdout, stderr = directory / "stdout.log", directory / "stderr.log"
        if "+" in selector or "-Batch" in directory.name:
            names = [part[1:-1] for part in selector.split("+")]
            stdout.write_text(self.batch_output(names), encoding="utf-8")
        else:
            stdout.write_text(self.output(selector[1:-1]), encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        return ProcessResult(self.exit_code, self.timed_out, self.overflow, stdout, stderr)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.work = self.runtime / "workspace"
        self.work.mkdir(parents=True)
        self.engine = self.root / "UE_5.8"
        binaries = self.engine / "Engine/Binaries/Win64"
        binaries.mkdir(parents=True)
        (binaries / "UnrealEditor.exe").write_bytes(b"fixture only; must never execute")
        (binaries / "UnrealEditor-Cmd.exe").write_bytes(b"fixture only; must never execute")
        for relative in ("Engine/Binaries/ThirdParty/DotNet/10.0/win-x64/dotnet.exe",
                         "Engine/Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.dll"):
            target = self.engine / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fixture only; must never execute")
        for name in ("Source", "Config", "Content"):
            (self.work / name).mkdir()
        (self.work / "Source/Game.cpp").write_text("int fixed_source = 1;\n", encoding="utf-8")
        (self.work / "Config/DefaultGame.ini").write_text("[Fixture]\nEnabled=True\n", encoding="utf-8")
        (self.work / "Content/data.bin").write_bytes(b"fixed content")
        (self.work / "ZhuGuoCity.uproject").write_text("{}\n", encoding="utf-8")
        (self.work / ".gitignore").write_text("Saved/\nDocs/Reports/Evidence/\n", encoding="utf-8")
        gitops.git(self.work, "init", "-b", "main")
        gitops.git(self.work, "config", "user.name", "Bridge Test")
        gitops.git(self.work, "config", "user.email", "fixture@example.invalid")
        gitops.git(self.work, "add", ".")
        gitops.git(self.work, "commit", "-m", TASK)
        self.head = gitops.head(self.work)
        self.state = {"phase": "CHECK", "task": {"id": TASK, "candidate": {"head_sha": self.head}}}
        write_json(self.runtime / "state.json", self.state)
        self.manifest = self.work / "Saved/Automation" / TASK / "validation-request.json"
        self.request = {"version": 1, "task_id": TASK, "head_sha": self.head,
                        "tests": [{"name": TEST, "mode": "nullrhi"}], "artifacts": []}
        write_json(self.manifest, self.request)
        self.runner = FakeUE()
        previous = Path.cwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, previous)

    def run_bridge(self):
        return run_validation(self.runtime, self.work, self.engine, runner=self.runner)

    def artifact(self):
        path = self.work / "Saved/Evidence" / TASK / "panel.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.request["artifacts"] = [path.relative_to(self.work).as_posix()]
        write_json(self.manifest, self.request)
        return path

    def test_planned_core_tests_cannot_be_omitted_or_replaced_by_visual_runs(self):
        self.state["task"]["core_tests"] = [TEST]
        write_json(self.runtime / "state.json", self.state)
        for tests in ([{"name": "ZhuGuoCity.M2.Other", "mode": "nullrhi"}],
                      [{"name": TEST, "mode": "normal"}]):
            with self.subTest(tests=tests):
                write_json(self.manifest, {**self.request, "tests": tests})
                result = self.run_bridge()
                self.assertEqual(result["status"], "failed")
                self.assertIn("missing planned NullRHI core tests: " + TEST, result["error"]["message"])
                self.assertEqual(self.runner.calls, [])

        write_json(self.manifest, self.request)
        result = self.run_bridge()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["core_tests"], [TEST])
        self.assertEqual(len(self.runner.calls), 1)

    def test_fixed_native_commands_modes_timeouts_and_immutable_receipt(self):
        self.request["tests"].append({"name": "ZhuGuoCity.Local.VisualStage", "mode": "normal"})
        write_json(self.manifest, self.request)
        result = self.run_bridge()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(self.runner.calls), 2)
        for index, (argv, kwargs) in enumerate(self.runner.calls):
            self.assertEqual(Path(argv[0]).name, "UnrealEditor-Cmd.exe" if index == 0 else "UnrealEditor.exe")
            self.assertEqual("-nullrhi" in argv, index == 0)
            self.assertEqual(kwargs["timeout_seconds"], TEST_TIMEOUT_SECONDS)
            self.assertEqual(kwargs["max_output_bytes"], MAX_OUTPUT_BYTES)
            self.assertEqual(kwargs["cwd"], self.work)
            self.assertIn("-unattended", argv)
            self.assertIn("-DisablePlugins=Fab", argv)
            self.assertEqual(len([arg for arg in argv if arg.startswith("-ExecCmds=")]), 1)
            self.assertTrue(next(arg for arg in argv if arg.startswith("-abslog=")).startswith("-abslog=" + str(self.runtime / "tasks")))
        self.assertEqual(read_json(Path(result["receipt_path"]))["status"], "passed")
        self.assertEqual(result["before"]["tracked_inputs"], result["after"]["tracked_inputs"])
        self.assertFalse(result["image_content_validated"])
        self.assertFalse(result["visual_review_required"])
        self.assertEqual(result["evidence_scope"], "automation_only_no_graphical_claim")

    def test_leaf_selector_excludes_prefix_sibling_without_regex_escaping_the_literal_dots(self):
        leaf = "ZhuGuoCity.M2.CatalogToolGroupScroll"
        sibling = leaf + "Tick"
        self.request["tests"] = [{"name": leaf, "mode": "nullrhi"}]
        write_json(self.manifest, self.request)
        result = self.run_bridge()
        self.assertEqual(result["status"], "passed")
        argv = self.runner.calls[0][0]
        command = next(value for value in argv if value.startswith("-ExecCmds="))
        self.assertEqual(command, "-ExecCmds=Automation RunTests ^ZhuGuoCity.M2.CatalogToolGroupScroll$")
        self.assertNotIn("\\", command)
        self.assertEqual(result["tests"][0]["automation"]["selector"], "^" + leaf + "$")
        # These are the two literal checks in UE's FAutomatedTestFilterBase.
        matching = [name for name in [leaf, sibling] if name.startswith(leaf) and name.endswith(leaf)]
        self.assertEqual(matching, [leaf])

    def test_optional_clean_build_runs_fixed_rebuild_before_tests_with_bound_archives(self):
        self.request["clean_build"] = True
        write_json(self.manifest, self.request)
        result = self.run_bridge()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(self.runner.calls), 2)
        argv, kwargs = self.runner.calls[0]
        self.assertEqual(argv[:5], [str(self.engine / "Engine/Binaries/ThirdParty/DotNet/10.0/win-x64/dotnet.exe"),
                                  str(self.engine / "Engine/Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.dll"),
                                  "ZhuGuoCityEditor", "Win64", "Development"])
        self.assertEqual(argv[5:11], ["-project=" + str(self.work / "ZhuGuoCity.uproject"), "-Rebuild",
                                   "-ForceHeaderGeneration", "-NoUBTMakefiles", "-waitmutex", "-NoHotReloadFromIDE"])
        self.assertEqual(len(argv), 12)
        self.assertNotIn("-Session", argv)
        self.assertEqual(kwargs["timeout_seconds"], BUILD_TIMEOUT_SECONDS)
        self.assertEqual(kwargs["cwd"], self.work)
        build = result["clean_build"]
        self.assertTrue(build["passed"])
        self.assertEqual(build["head_sha"], self.head)
        self.assertTrue(Path(build["build_log_path"]).is_file())
        process = read_json(Path(build["stdout_path"]).parent / "process.json")
        self.assertEqual(process["head_sha"], self.head)
        self.assertEqual(process["argv"], argv)
        self.assertEqual(result["after_clean_build"]["tracked_inputs"], result["before"]["tracked_inputs"])
        self.request["clean_build"] = False
        write_json(self.manifest, self.request)
        self.runner.calls.clear()
        result = self.run_bridge()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(self.runner.calls), 1)
        self.assertNotIn("clean_build", result)

    def test_failed_incomplete_or_false_success_clean_build_never_runs_tests(self):
        self.request["clean_build"] = True
        write_json(self.manifest, self.request)
        variants = [(1, False, False, "Result: Succeeded\n"), (0, True, False, "Result: Succeeded\n"),
                    (0, False, True, "Result: Succeeded\n"), (0, False, False, "build attempted\n"),
                    (0, False, False, "Result: Succeeded\nResult: Failed\n")]
        for code, timeout, overflow, output in variants:
            with self.subTest(code=code, timeout=timeout, overflow=overflow, output=output):
                self.runner.calls.clear()
                self.runner.build_exit_code = code
                self.runner.build_timed_out = timeout
                self.runner.build_overflow = overflow
                self.runner.build_output = output
                result = self.run_bridge()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(len(self.runner.calls), 1)
                self.assertEqual(result["tests"], [])
                self.assertFalse(result["clean_build"]["passed"])
                self.assertTrue(Path(result["clean_build"]["stdout_path"]).is_file())

    def test_clean_build_accepts_engine_subdirectory_and_still_requires_its_toolchain(self):
        self.request["clean_build"] = True
        write_json(self.manifest, self.request)
        actual_engine = self.engine / "Engine"
        result = run_validation(self.runtime, self.work, actual_engine, runner=self.runner)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(self.runner.calls), 2)
        argv, _ = self.runner.calls[0]
        self.assertEqual(argv[:2], [str(actual_engine / "Binaries/ThirdParty/DotNet/10.0/win-x64/dotnet.exe"),
                                   str(actual_engine / "Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.dll")])
        self.assertTrue(result["clean_build"]["passed"])
        self.assertEqual(result["clean_build"]["head_sha"], self.head)
        self.runner.calls.clear()
        (actual_engine / "Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.dll").unlink()
        result = run_validation(self.runtime, self.work, actual_engine, runner=self.runner)
        self.assertEqual(result["status"], "failed")
        self.assertIn("requires the bundled", result["error"]["message"])
        self.assertFalse(self.runner.calls)

    def test_clean_build_cannot_change_tracked_inputs_or_run_with_missing_toolchain(self):
        self.request["clean_build"] = True
        write_json(self.manifest, self.request)
        self.runner.effect = lambda argv, kwargs: (self.work / "Source/Game.cpp").write_text("changed\n", encoding="utf-8")
        result = self.run_bridge()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(result["tests"], [])
        self.assertIn("changed during clean build", result["error"]["message"])
        (self.work / "Source/Game.cpp").write_text("int fixed_source = 1;\n", encoding="utf-8")
        self.runner.calls.clear()
        self.runner.effect = None
        (self.engine / "Engine/Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.dll").unlink()
        result = self.run_bridge()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(self.runner.calls)

    def test_manifest_duplicate_fields_and_injection_fail_without_native_process(self):
        self.manifest.write_text('{"version":1,"version":1}', encoding="utf-8")
        result = self.run_bridge()
        self.assertEqual(result["status"], "failed")
        self.assertIn("Duplicate JSON", result["error"]["message"])
        self.assertFalse(self.runner.calls)
        self.assertTrue(Path(result["receipt_path"]).is_file())
        self.request["tests"][0]["name"] += ";Quit"
        write_json(self.manifest, self.request)
        self.assertEqual(self.run_bridge()["status"], "failed")
        self.assertFalse(self.runner.calls)

    def test_head_mismatch_dirty_git_wrong_phase_and_wrong_cwd_do_not_execute(self):
        scenarios = [
            {"phase": "CHECK", "task": {"id": TASK, "candidate": {"head_sha": "a" * 40}}},
            {"phase": "PLAN", "task": self.state["task"]},
            {"phase": "CHECK", "task": None},
        ]
        for state in scenarios:
            write_json(self.runtime / "state.json", state)
            self.assertEqual(self.run_bridge()["status"], "failed")
        write_json(self.runtime / "state.json", self.state)
        (self.work / "Source/Game.cpp").write_text("uncommitted change", encoding="utf-8")
        self.assertEqual(self.run_bridge()["status"], "failed")
        (self.work / "Source/Game.cpp").write_text("int fixed_source = 1;\n", encoding="utf-8")
        os.chdir(self.root)
        self.assertEqual(self.run_bridge()["status"], "failed")
        self.assertFalse(self.runner.calls)

    def test_process_nonzero_timeout_overflow_or_false_success_are_failure_even_with_exit_zero(self):
        for code, timed_out, overflow, output in [(1, False, False, successful_output),
                                                (0, True, False, successful_output),
                                                (0, False, True, successful_output),
                                                (0, False, False, lambda name: successful_output(name).replace("Result={Success}", "Result={Fail}"))]:
            self.runner.exit_code, self.runner.timed_out, self.runner.overflow, self.runner.output = code, timed_out, overflow, output
            result = self.run_bridge()
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["tests"][0]["passed"])
            self.assertTrue(Path(result["tests"][0]["stdout_path"]).is_file())

    def test_completed_assertion_failures_collect_every_requested_result_after_clean_build(self):
        names = [TEST, "ZhuGuoCity.M2.VarietyHud", "ZhuGuoCity.M2.VarietySave"]
        self.request.update(clean_build=True, tests=[{"name": name, "mode": "nullrhi"} for name in names])
        write_json(self.manifest, self.request)
        failed = [names[0], names[2]]

        def output(name):
            text = successful_output(name)
            return text.replace("Result={Success}", "Result={Fail}") if name in failed else text

        self.runner.output = output
        result = self.run_bridge()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["clean_build"]["passed"])
        self.assertEqual(len(self.runner.calls), 4)
        self.assertEqual([test["name"] for test in result["tests"]], names)
        self.assertEqual([test["passed"] for test in result["tests"]], [False, True, False])
        self.assertEqual(result["failed_tests"], failed)
        self.assertEqual(read_json(Path(result["receipt_path"]))["failed_tests"], failed)
        self.assertEqual(result["before"]["tracked_inputs"], result["after"]["tracked_inputs"])

    def test_infrastructure_or_incomplete_failure_stops_before_other_requested_tests(self):
        self.request["tests"].append({"name": "ZhuGuoCity.M2.VarietySave", "mode": "nullrhi"})
        write_json(self.manifest, self.request)
        assertion = lambda name: successful_output(name).replace("Result={Success}", "Result={Fail}")
        cases = [(0, True, False, assertion), (0, False, True, assertion), (3, False, False, assertion),
                 (0, False, False, lambda name: assertion(name) + "Fatal error: crashed\n"),
                 (0, False, False, lambda name: assertion(name).replace("1 tests performed", "0 tests performed")),
                 (0, False, False, lambda name: assertion(name).replace(f"Path={{{name}}}", "Path={ZhuGuoCity.M2.Other}"))]
        for code, timeout, overflow, output in cases:
            with self.subTest(code=code, timeout=timeout, overflow=overflow):
                self.runner.calls.clear()
                self.runner.exit_code, self.runner.timed_out, self.runner.overflow, self.runner.output = code, timeout, overflow, output
                result = self.run_bridge()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(len(self.runner.calls), 1)
                self.assertEqual(len(result["tests"]), 1)

    def test_assertion_failure_cannot_continue_on_changed_source(self):
        self.request["tests"].append({"name": "ZhuGuoCity.M2.VarietySave", "mode": "nullrhi"})
        write_json(self.manifest, self.request)
        self.runner.output = lambda name: successful_output(name).replace("Result={Success}", "Result={Fail}")
        self.runner.effect = lambda argv, kwargs: (self.work / "Source/Game.cpp").write_text("changed during failed test\n", encoding="utf-8")
        result = self.run_bridge()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(result["tests"]), 1)
        self.assertIn("changed during validation", result["error"]["message"])

    def test_successful_test_cannot_continue_on_changed_source(self):
        self.request["tests"].append({"name": "ZhuGuoCity.M2.VarietySave", "mode": "nullrhi"})
        write_json(self.manifest, self.request)
        self.runner.effect = lambda argv, kwargs: (self.work / "Source/Game.cpp").write_text("changed during successful test\n", encoding="utf-8")
        result = self.run_bridge()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(result["tests"]), 1)
        self.assertTrue(result["tests"][0]["passed"])
        self.assertIn("changed during validation", result["error"]["message"])

    def test_task_batches_run_fixed_selectors_preserve_core_tests_and_mirror_each_result(self):
        core = [TEST, *[f"ZhuGuoCity.M2.Core{i}" for i in range(2, 6)]]
        batches = [["ZhuGuoCity.M2.VarietyHud", "ZhuGuoCity.M2.VarietySave"],
                   ["ZhuGuoCity.M2.OtherHud", "ZhuGuoCity.M2.OtherSave"]]
        self.state["task"]["core_tests"] = core
        write_json(self.runtime / "state.json", self.state)
        self.request.update(tests=[{"name": name, "mode": "nullrhi"} for name in core], test_batches=batches)
        write_json(self.manifest, self.request)
        result = self.run_bridge()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["scope"], "task")
        self.assertEqual(len(self.runner.calls), 7)
        self.assertEqual(len(result["tests"]), 9)
        self.assertEqual(len(result["batches"]), 2)
        for index, names in enumerate(batches):
            argv, _ = self.runner.calls[5 + index]
            self.assertEqual(Path(argv[0]).name, "UnrealEditor-Cmd.exe")
            self.assertIn("-nullrhi", argv)
            self.assertIn("-ExecCmds=Automation RunTests " + "+".join("^" + name + "$" for name in names), argv)
        mirror = read_json(self.work / "Saved/Automation" / TASK / "validation-results.json")
        self.assertEqual(mirror["head_sha"], self.head)
        self.assertEqual(mirror["receipt_path"], result["receipt_path"])
        self.assertEqual(len(mirror["tests"]), 9)
        self.assertEqual(mirror["tests"][-1]["automation"]["test_result"], "Success")
        self.assertNotIn("argv", mirror["tests"][0])
        self.assertTrue(gitops.clean(self.work))
        # Core tests stay mandatory in the original tests list, even if a
        # missing core name could otherwise appear in a supplemental batch.
        self.request["tests"] = self.request["tests"][:-1]
        self.request["test_batches"] = [[core[-1]]]
        write_json(self.manifest, self.request)
        self.runner.calls.clear()
        self.assertEqual(self.run_bridge()["status"], "failed")
        self.assertEqual(self.runner.calls, [])

    def test_batch_assertion_failure_continues_later_batch_and_mirrors_failure(self):
        batches = [["ZhuGuoCity.M2.VarietyHud", "ZhuGuoCity.M2.VarietySave"], ["ZhuGuoCity.M2.OtherSave"]]
        self.request["test_batches"] = batches
        write_json(self.manifest, self.request)
        self.runner.batch_output = lambda names: batch_output(names, [batches[0][0]])
        result = self.run_bridge()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.runner.calls), 3)
        self.assertEqual([test["passed"] for test in result["tests"]], [True, False, True, True])
        self.assertEqual(result["failed_tests"], [batches[0][0]])
        mirror = read_json(Path(result["results_mirror_path"]))
        self.assertEqual(mirror["status"], "failed")
        self.assertEqual(mirror["failed_tests"], [batches[0][0]])

    def test_incomplete_batch_or_source_change_stops_following_batch(self):
        self.request["test_batches"] = [["ZhuGuoCity.M2.VarietyHud", "ZhuGuoCity.M2.VarietySave"], ["ZhuGuoCity.M2.OtherSave"]]
        write_json(self.manifest, self.request)
        self.runner.batch_output = lambda names: batch_output(names).replace("2 tests performed", "1 tests performed")
        result = self.run_bridge()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(len(result["tests"]), 3)
        self.runner.calls.clear()
        self.runner.batch_output = batch_output

        def change_during_batch(argv, kwargs):
            if any("+" in arg for arg in argv if arg.startswith("-ExecCmds=")):
                (self.work / "Source/Game.cpp").write_text("changed during batch\n", encoding="utf-8")

        self.runner.effect = change_during_batch
        result = self.run_bridge()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.runner.calls), 2)
        self.assertIn("changed during validation", result["error"]["message"])

    def test_results_mirror_never_overwrites_tracked_file(self):
        mirror = self.work / "Saved/Automation" / TASK / "validation-results.json"
        mirror.write_text("tracked sentinel", encoding="utf-8")
        gitops.git(self.work, "add", "-f", mirror.relative_to(self.work).as_posix())
        gitops.git(self.work, "commit", "-m", "Tracked mirror fixture")
        self.head = gitops.head(self.work)
        self.state["task"]["candidate"]["head_sha"] = self.head
        self.request["head_sha"] = self.head
        write_json(self.runtime / "state.json", self.state)
        write_json(self.manifest, self.request)
        result = self.run_bridge()
        self.assertEqual(result["status"], "failed")
        self.assertIn("tracked file", result["results_mirror_error"])
        self.assertEqual(mirror.read_text(encoding="utf-8"), "tracked sentinel")
        self.assertTrue(gitops.clean(self.work))

    def test_tracked_source_hash_changes_fail_even_if_git_clean_check_misses_them(self):
        def effect(argv, kwargs):
            (self.work / "Source/Game.cpp").write_text("int changed_during_execution = 2;\n", encoding="utf-8")
        self.runner.effect = effect
        with patch("unattended.ue_checks.gitops.clean", return_value=True):
            result = self.run_bridge()
        self.assertEqual(result["status"], "failed")
        self.assertNotEqual(result["before"]["tracked_input_sha256"], result["after"]["tracked_input_sha256"])

    def test_candidate_commit_change_during_execution_fails(self):
        def effect(argv, kwargs):
            (self.work / "Source/Game.cpp").write_text("int changed_and_committed = 2;\n", encoding="utf-8")
            gitops.git(self.work, "add", "Source/Game.cpp")
            gitops.git(self.work, "commit", "-m", "unexpected commit during validation")
        self.runner.effect = effect
        result = self.run_bridge()
        self.assertEqual(result["status"], "failed")
        self.assertNotEqual(result["before"]["head_sha"], result["after"]["head_sha"])

    def test_existing_unchanged_or_missing_artifact_cannot_pass(self):
        path = self.artifact()
        result = self.run_bridge()
        self.assertEqual(result["status"], "failed")
        self.assertIn("not generated", result["error"]["message"])
        path.write_bytes(PNG)
        result = self.run_bridge()
        self.assertEqual(result["status"], "failed")
        self.assertIn("not newly generated or rewritten", result["error"]["message"])

    def test_identical_png_rewritten_this_run_is_fresh_and_backup_keeps_exact_bytes(self):
        path = self.artifact()
        path.write_bytes(PNG)
        previous = path.stat().st_mtime_ns
        self.request["tests"][0]["mode"] = "normal"
        write_json(self.manifest, self.request)
        def effect(argv, kwargs):
            path.write_bytes(PNG)
            os.utime(path, ns=(previous + 10_000_000, previous + 10_000_000))
        self.runner.effect = effect
        result = self.run_bridge()
        self.assertEqual(result["status"], "passed")
        artifact = result["artifacts"][0]
        self.assertEqual(artifact["freshness"], "rewritten")
        self.assertEqual(artifact["png_dimensions"], {"width": 1, "height": 1})
        backup = Path(artifact["backup_path"])
        self.assertTrue(backup.is_relative_to(Path(result["receipt_path"]).parent / "artifacts"))
        self.assertEqual(backup.read_bytes(), PNG)
        self.assertEqual(artifact["backup_sha256"], artifact["sha256"])
        path.write_bytes(PNG + b"later workspace mutation")
        self.assertEqual(backup.read_bytes(), PNG)
        self.assertTrue(result["visual_review_required"])
        self.assertFalse(result["image_content_validated"])

    def test_new_artifact_is_archived_and_bad_png_is_rejected(self):
        path = self.artifact()
        self.runner.effect = lambda argv, kwargs: path.write_bytes(PNG)
        result = self.run_bridge()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["artifacts"][0]["freshness"], "created")
        self.runner.effect = lambda argv, kwargs: path.write_bytes(b"not an actual PNG")
        self.assertEqual(self.run_bridge()["status"], "failed")

    def test_symlink_and_windows_junction_cannot_escape_evidence_roots(self):
        target = self.root / "outside"
        target.mkdir()
        (target / "panel.png").write_bytes(PNG)
        parent = self.work / "Saved/Evidence" / TASK
        parent.mkdir(parents=True)
        link = parent / "linked"
        created = False
        if os.name == "nt":
            try:
                import _winapi
                _winapi.CreateJunction(str(target), str(link))
                created = True
            except (AttributeError, OSError):
                pass
        if not created:
            try:
                link.symlink_to(target, target_is_directory=True)
                created = True
            except OSError:
                self.skipTest("OS does not allow creating a link fixture")
        self.request["artifacts"] = [f"Saved/Evidence/{TASK}/linked/panel.png"]
        write_json(self.manifest, self.request)
        result = self.run_bridge()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(self.runner.calls)
        self.assertIn("Symlinks and junctions", result["error"]["message"])

    def test_milestone_rechecks_only_last_accepted_task_at_final_head(self):
        milestone = {"head_sha": self.head, "task_ids": ["TASK-LOCAL-0041", TASK],
                     "accepted_tasks": [{"id": "TASK-LOCAL-0041", "head_sha": "b" * 40},
                                        {"id": TASK, "head_sha": self.head}]}
        state = {"phase": "MILESTONE_REVIEW", "task": None, "milestone": milestone}
        write_json(self.runtime / "state.json", state)
        result = self.run_bridge()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["scope"], "milestone_last_task_recheck")
        self.assertEqual(result["task_id"], TASK)
        self.assertEqual(result["head_sha"], self.head)
        self.runner.calls.clear()
        for changed in [dict(milestone, accepted_tasks=[]), dict(milestone, head_sha="a" * 40),
                        dict(milestone, task_ids=[]), dict(milestone, task_ids=[TASK, TASK])]:
            write_json(self.runtime / "state.json", {**state, "milestone": changed})
            self.assertEqual(self.run_bridge()["status"], "failed")
        self.assertFalse(self.runner.calls)

    def test_full_milestone_regression_uses_one_fixed_command_without_task_manifest(self):
        milestone = {"id": "M2", "head_sha": self.head}
        operation = {"kind": "milestone_check", "head_sha": self.head}
        state = {"phase": "MILESTONE_REVIEW", "task": None, "milestone": milestone, "operation": operation}
        write_json(self.runtime / "state.json", state)
        self.manifest.unlink()
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            self.assertIn("-ExecCmds=Automation RunTests ^ZhuGuoCity.M", argv)
            self.assertIn("-nullrhi", argv)
            self.assertEqual(Path(argv[0]).name, "UnrealEditor-Cmd.exe")
            self.assertEqual(kwargs["timeout_seconds"], REGRESSION_TIMEOUT_SECONDS)
            directory = kwargs["output_dir"]
            stdout, stderr = directory / "stdout.log", directory / "stderr.log"
            stdout.write_text(milestone_output("ZhuGuoCity.M0.Save", TEST), encoding="utf-8")
            stderr.write_text("", encoding="utf-8")
            return ProcessResult(0, False, False, stdout, stderr)

        result = run_validation(self.runtime, self.work, self.engine, runner=runner, milestone_regression=True)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["scope"], "milestone_full_automation")
        self.assertEqual(result["milestone_id"], "M2")
        self.assertEqual(result["head_sha"], self.head)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["before"]["tracked_inputs"], result["after"]["tracked_inputs"])
        self.assertEqual(read_json(Path(result["receipt_path"]))["status"], "passed")
        write_json(self.runtime / "state.json", {**state, "operation": {**operation, "head_sha": "b" * 40}})
        rejected = run_validation(self.runtime, self.work, self.engine, runner=runner, milestone_regression=True)
        self.assertEqual(rejected["status"], "failed")
        self.assertEqual(len(calls), 1)

    def test_cli_exit_status_and_standalone_help_without_ue(self):
        for status, expected in [("passed", 0), ("failed", 1)]:
            with patch("unattended.ue_checks.run_validation", return_value={"status": status, "tests": [], "artifacts": []}), redirect_stdout(io.StringIO()) as output:
                actual = main(["--runtime", str(self.runtime), "--workspace", str(self.work), "--engine", str(self.engine)])
                self.assertEqual(actual, expected)
                lines = output.getvalue().splitlines()
                self.assertEqual(lines[0], "UE_TASK_VALIDATION_PASSED" if status == "passed" else "UE_TASK_VALIDATION_FAILED")
                self.assertEqual(json.loads(lines[1])["status"], status)
        from unattended import ue_checks
        process = subprocess.run([sys.executable, "-B", str(Path(ue_checks.__file__).resolve()), "--help"],
                                 cwd=self.root, capture_output=True, text=True, timeout=15,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(process.returncode, 0)
        self.assertIn("--runtime", process.stdout)
        self.assertFalse(self.runner.calls)


if __name__ == "__main__":
    unittest.main()
