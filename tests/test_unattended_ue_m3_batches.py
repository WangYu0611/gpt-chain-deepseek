"""M2/M3 batch schema regression: real temporary Git and fake UE processes."""
from copy import deepcopy
from pathlib import Path
import unittest

from tests import test_unattended_ue_checks as fixtures
from unattended import ue_checks
from unattended.storage import read_json, write_json


M3 = "ZhuGuoCity.M3.FireResponseHud"
M3_OTHER = "ZhuGuoCity.M3.ServicesSceneFlow"
M2 = "ZhuGuoCity.M2.FoodMarketLoopFlow"


class M3BatchSchemaTests(unittest.TestCase):
    def request(self, batches):
        return {"version": 1, "task_id": fixtures.TASK, "head_sha": "a" * 40,
                "tests": [{"name": "ZhuGuoCity.M3.DeathFlow", "mode": "nullrhi"}], "test_batches": batches}

    def test_exact_m2_m3_mixed_batches_are_preserved_without_request_rewriting(self):
        request = self.request([[M2, M3], [M3_OTHER]])
        before = deepcopy(request)
        actual = ue_checks.validate_request(request, fixtures.TASK, "a" * 40)
        self.assertEqual(actual["test_batches"], before["test_batches"])
        self.assertEqual(request, before)

    def test_other_suites_prefixes_patterns_and_commands_remain_forbidden(self):
        for name in ("ZhuGuoCity.M1.Save", "ZhuGuoCity.M4.Save", "ZhuGuoCity.M30.Save",
                     "ZhuGuoCity.Local.Visual", "ZhuGuoCity.M3", "ZhuGuoCity.M3.*", M3 + ";Quit",
                     M3 + "+" + M2, "^" + M3 + "$", "zhuguocity.M3.FireResponseHud"):
            with self.subTest(name=name), self.assertRaises(ue_checks.ValidationError):
                ue_checks.validate_request(self.request([[name]]), fixtures.TASK, "a" * 40)

    def test_m3_batches_keep_all_count_duplicate_and_mode_limits(self):
        invalid = [[], [[]], [[M3, M3]], [[M3], [M3.upper()]],
                   [["ZhuGuoCity.M3.DeathFlow"]], [[{"name": M3, "mode": "normal"}]],
                   [[f"ZhuGuoCity.M3.Test{i}" for i in range(17)]],
                   [[f"ZhuGuoCity.M3.Test{i}"] for i in range(17)]]
        for batches in invalid:
            with self.subTest(batches=batches), self.assertRaises(ue_checks.ValidationError):
                ue_checks.validate_request(self.request(batches), fixtures.TASK, "a" * 40)


class M3BatchExecutionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.BridgeTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.core = ["ZhuGuoCity.M3." + name for name in ("DeathRules", "DeathFlow", "DeathHud", "DeathSave", "HealthFlow")]
        self.batches = [[M2, M3], [M3_OTHER]]
        self.f.state["task"]["core_tests"] = self.core
        write_json(self.f.runtime / "state.json", self.f.state)
        self.f.request.update(tests=[{"name": name, "mode": "nullrhi"} for name in self.core], test_batches=self.batches)
        write_json(self.f.manifest, self.f.request)

    def test_mixed_batches_execute_exact_nullrhi_and_mirror_every_requested_result(self):
        request_before = self.f.manifest.read_bytes()
        result = self.f.run_bridge()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(self.f.runner.calls), 7)
        self.assertEqual([row["name"] for row in result["tests"]], self.core + [M2, M3, M3_OTHER])
        self.assertTrue(all(row["passed"] for row in result["tests"]))
        for index, names in enumerate(self.batches):
            argv, options = self.f.runner.calls[5 + index]
            self.assertEqual(Path(argv[0]).name, "UnrealEditor-Cmd.exe")
            self.assertIn("-nullrhi", argv)
            self.assertIn("-ExecCmds=Automation RunTests " + "+".join("^" + name + "$" for name in names), argv)
            self.assertEqual(options["timeout_seconds"], ue_checks.TEST_TIMEOUT_SECONDS)
        mirror = read_json(Path(result["results_mirror_path"]))
        self.assertEqual(len(mirror["tests"]), 8)
        self.assertEqual(mirror["head_sha"], self.f.head)
        self.assertEqual(self.f.manifest.read_bytes(), request_before)

    def test_m3_failure_retains_failed_item_and_continues_later_batch(self):
        self.f.runner.batch_output = lambda names: fixtures.batch_output(names, [M3])
        result = self.f.run_bridge()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.f.runner.calls), 7)
        self.assertEqual(result["failed_tests"], [M3])
        self.assertTrue(result["tests"][-1]["passed"])
        mirror = read_json(Path(result["results_mirror_path"]))
        self.assertEqual(mirror["failed_tests"], [M3])
        self.assertEqual(mirror["status"], "failed")

    def test_m3_batch_does_not_replace_a_missing_required_core_test(self):
        self.f.request["tests"] = self.f.request["tests"][:-1]
        self.f.request["test_batches"][0].append(self.core[-1])
        write_json(self.f.manifest, self.f.request)
        result = self.f.run_bridge()
        self.assertEqual(result["status"], "failed")
        self.assertIn("missing planned NullRHI core tests", result["error"]["message"])
        self.assertEqual(self.f.runner.calls, [])

    def test_incomplete_m3_discovery_completion_and_duplicates_stop_later_batches(self):
        base = fixtures.batch_output
        variants = [lambda names: base(names).replace("2 tests performed", "1 tests performed"),
                    lambda names: base(names).replace("Path={" + M3 + "}", "Path={" + M3 + "Extra}"),
                    lambda names: base(names) + base(names)]
        for variant in variants:
            self.f.runner.calls.clear(); self.f.runner.batch_output = variant
            result = self.f.run_bridge()
            self.assertEqual(result["status"], "failed")
            self.assertEqual(len(self.f.runner.calls), 6)
            self.assertFalse(result["batches"][0]["automation"]["complete"])

    def test_batch_nonzero_timeout_and_source_change_are_never_success(self):
        for failure in ("nonzero", "timeout", "source"):
            self.f.runner.calls.clear(); self.f.runner.exit_code = 0; self.f.runner.timed_out = False
            def effect(argv, options):
                if any("+" in arg for arg in argv if arg.startswith("-ExecCmds=")):
                    if failure == "nonzero": self.f.runner.exit_code = 1
                    elif failure == "timeout": self.f.runner.timed_out = True
                    else: (self.f.work / "Source/Game.cpp").write_text("changed during batch\n", encoding="utf-8")
            self.f.runner.effect = effect
            result = self.f.run_bridge()
            self.assertEqual(result["status"], "failed")
            self.assertEqual(len(self.f.runner.calls), 6)
            if failure == "source": self.assertIn("changed during validation", result["error"]["message"])

