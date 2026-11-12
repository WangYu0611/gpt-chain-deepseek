"""Claude planning must retain structured, read-only and restart-safe receipts."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from github_harness_lab.lab_harness.process import ProcessResult
from tests.test_unattended_providers import CODEX_EVENTS, PLAN, REVIEW, write_events
from unattended.providers import (
    AgentProviders, PLAN_SCHEMA, PLAN_WITH_CORE_TESTS_SCHEMA, ProviderError,
    STRICT_PLAN_WITH_CORE_TESTS_SCHEMA, STRICT_TIERED_PLAN_WITH_CORE_TESTS_SCHEMA,
    TIERED_PLAN_SCHEMA, TIERED_PLAN_WITH_CORE_TESTS_SCHEMA,
    _claude_log_record, classify_transient_failure, read_session, validate_output,
)


SESSION = "550e8400-e29b-41d4-a716-446655440000"
OTHER_SESSION = "550e8400-e29b-41d4-a716-446655440001"


class ClaudePlannerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / "workspace"
        self.runtime = self.root / "runtime"
        self.work.mkdir()
        self.runtime.mkdir()
        self.out = self.root / "plan"
        self.settings = {"command": ["claude.exe"], "model": "sonnet", "reasoning_effort": "high"}
        self.provider = AgentProviders({"planning_provider": "claude", "coding_provider": "claude",
                                        "runtime_dir": str(self.runtime), "claude": self.settings})

    def events(self, result=None, *, extra=(), is_error=False):
        return [
            {"type": "system", "subtype": "init", "session_id": SESSION},
            *extra,
            {"type": "result", "subtype": "success", "session_id": SESSION, "is_error": is_error,
             "result": "Planning finished.", "structured_output": PLAN if result is None else result},
        ]

    def capture(self, rows, exit_code=0):
        self.out.mkdir(exist_ok=True)
        write_events(self.out / "stdout.log", rows)
        (self.out / "stderr.log").write_text("", encoding="utf-8")
        return ProcessResult(exit_code, False, False, self.out / "stdout.log", self.out / "stderr.log")

    def receipt(self, rows, *, exit_code=0, schema=PLAN_SCHEMA):
        self.capture(rows, exit_code)
        (self.out / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
        (self.out / "process.json").write_text(json.dumps({
            "kind": "plan", "planning_provider": "claude", "exit_code": exit_code,
            "requested_session": None, "timed_out": False, "output_limit_exceeded": False,
        }), encoding="utf-8")

    @patch("unattended.providers.claude_subscription_ready", return_value=True)
    @patch("unattended.providers.run_process")
    def test_plan_is_read_only_structured_and_recovers_without_its_original_config(self, launch, auth):
        launch.side_effect = lambda *args, **kwargs: self.capture(self.events())
        self.settings["plan_instructions"] = "Keep the next task bounded."
        result = self.provider.plan("Plan the next task.", self.work, self.out, 120)
        self.assertEqual(result, PLAN)
        argv = launch.call_args.args[0]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), PLAN_SCHEMA)
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        policy = json.loads(argv[argv.index("--settings") + 1])
        self.assertEqual(policy["permissions"]["additionalDirectories"], [str(self.runtime.resolve())])
        self.assertTrue(policy["disableAllHooks"])
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--resume", argv)
        self.assertIn(b"Keep the next task bounded.", launch.call_args.kwargs["stdin"])
        self.assertIs(launch.call_args.kwargs["stdout_record_filter"], _claude_log_record)
        receipt = json.loads((self.out / "process.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["planning_provider"], "claude")
        self.assertNotIn("coding_provider", receipt)
        self.assertEqual(read_session(self.out), SESSION)
        (self.out / "result.json").unlink()
        self.assertEqual(AgentProviders({}).recover("plan", self.out), PLAN)
        launch.assert_called_once()

    @patch("unattended.providers.AgentProviders._codex")
    def test_codex_remains_the_reviewer_and_the_default_legacy_planner(self, codex):
        codex.return_value = REVIEW
        self.assertEqual(self.provider.review("Review", self.work, self.out, 30), REVIEW)
        codex.assert_called_once_with("review", "Review", self.work, self.out, 30)
        codex.reset_mock()
        codex.return_value = PLAN
        self.assertEqual(AgentProviders({}).plan("Plan", self.work, self.out, 30), PLAN)
        codex.assert_called_once_with("plan", "Plan", self.work, self.out, 30)

    @patch("unattended.providers.claude_subscription_ready", return_value=True)
    @patch("unattended.providers.run_process")
    def test_model_override_is_per_plan_and_leaves_the_coding_model_unchanged(self, launch, auth):
        before = copy.deepcopy(self.provider.config)
        launch.side_effect = lambda *args, **kwargs: self.capture(self.events())
        self.provider.plan("Plan the critical change", self.work, self.out, 30,
                           model="claude-opus-5-5", reasoning_effort="max")
        argv = launch.call_args.args[0]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5-5")
        self.assertEqual(argv[argv.index("--effort") + 1], "max")
        receipt = json.loads((self.out / "process.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["planning_model"], "claude-opus-5-5")
        self.assertEqual(receipt["planning_reasoning_effort"], "max")
        self.assertEqual(self.provider.config, before)
        self.out = self.root / "code"
        self.provider.code("Implement the issued task", self.work, self.out, 30)
        argv = launch.call_args.args[0]
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(self.provider.config, before)

    @patch("unattended.providers.claude_subscription_ready")
    @patch("unattended.providers.run_process")
    def test_invalid_overrides_fail_before_authentication_or_launch(self, launch, auth):
        for overrides in ({"model": " "}, {"reasoning_effort": "invalid"}, {"tiered": "true"}):
            with self.subTest(overrides=overrides), self.assertRaises(ProviderError):
                self.provider.plan("Plan", self.work, self.out, 30, **overrides)
        with self.assertRaisesRegex(ProviderError, "require the Claude"):
            AgentProviders({}).plan("Plan", self.work, self.out, 30, model="claude-opus-5-5")
        with self.assertRaisesRegex(ProviderError, "require the Claude"):
            AgentProviders({}).plan("Plan", self.work, self.out, 30, tiered=True)
        auth.assert_not_called()
        launch.assert_not_called()

    @patch("unattended.providers.claude_subscription_ready", return_value=True)
    @patch("unattended.providers.run_process")
    def test_tiered_schemas_preserve_routing_and_core_tests_across_config_changes(self, launch, auth):
        self.settings["critical_plan_model"] = "claude-opus-5-5"
        routing = {"planning_level": "routine", "planning_reason": "Update an existing test fixture."}
        for planned, schema in ((False, TIERED_PLAN_SCHEMA), (True, STRICT_TIERED_PLAN_WITH_CORE_TESTS_SCHEMA)):
            with self.subTest(planned=planned):
                self.out = self.root / ("tiered-core" if planned else "tiered")
                self.settings["planned_core_tests"] = planned
                result = PLAN | routing
                if planned:
                    result["core_tests"] = ["ZhuGuoCity.M2.Save"]
                launch.side_effect = lambda *args, **kwargs: self.capture(self.events(result))
                self.assertEqual(self.provider.plan("Plan", self.work, self.out, 30), result)
                argv = launch.call_args.args[0]
                self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), schema)
                self.assertIn("实际要改变的内容".encode(), launch.call_args.kwargs["stdin"])
                self.assertEqual(AgentProviders({}).recover("plan", self.out), result)
                write_events(self.out / "stdout.log", self.events(PLAN))
                with self.assertRaisesRegex(ProviderError, "saved schema"):
                    self.provider.recover("plan", self.out)

    def test_tiered_plan_routing_requires_valid_level_and_concrete_reason(self):
        plan = PLAN | {"planning_level": "critical", "planning_reason": "Change the save schema."}
        self.assertEqual(validate_output("plan", plan), plan)
        for invalid in (plan | {"planning_level": "maybe"}, plan | {"planning_reason": " "},
                        {k: v for k, v in plan.items() if k != "planning_level"},
                        {k: v for k, v in plan.items() if k != "planning_reason"}):
            with self.subTest(invalid=invalid), self.assertRaises(ProviderError):
                validate_output("plan", invalid)

    @patch("unattended.providers.claude_subscription_ready", return_value=True)
    @patch("unattended.providers.run_process")
    def test_pinned_tier_schema_survives_removing_the_critical_model_config(self, launch, auth):
        self.assertNotIn("critical_plan_model", self.settings)
        result = PLAN | {"planning_level": "critical", "planning_reason": "Change the save schema."}
        launch.side_effect = lambda *args, **kwargs: self.capture(self.events(result))
        self.assertEqual(self.provider.plan("Plan", self.work, self.out, 30,
                                           model="claude-opus-5-5", reasoning_effort="high", tiered=True), result)
        argv = launch.call_args.args[0]
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), TIERED_PLAN_SCHEMA)
        self.assertEqual(AgentProviders({}).recover("plan", self.out), result)

    def test_legacy_codex_plan_receipts_recover_after_switching_planning_provider(self):
        self.out.mkdir()
        write_events(self.out / "stdout.log", CODEX_EVENTS)
        (self.out / "final.json").write_text(json.dumps(PLAN), encoding="utf-8")
        (self.out / "process.json").write_text(json.dumps({
            "kind": "plan", "exit_code": 0, "timed_out": False, "output_limit_exceeded": False,
        }), encoding="utf-8")
        self.assertEqual(self.provider.recover("plan", self.out), PLAN)

    @patch("unattended.providers.claude_subscription_ready", return_value=True)
    @patch("unattended.providers.run_process")
    def test_saved_schema_preserves_core_test_requirements_when_configuration_changes(self, launch, auth):
        self.settings["planned_core_tests"] = True
        planned = PLAN | {"core_tests": ["ZhuGuoCity.M2.Save"]}
        launch.side_effect = lambda *args, **kwargs: self.capture(self.events(planned))
        self.assertEqual(self.provider.plan("Plan", self.work, self.out, 30), planned)
        argv = launch.call_args.args[0]
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), STRICT_PLAN_WITH_CORE_TESTS_SCHEMA)
        core_rule = STRICT_PLAN_WITH_CORE_TESTS_SCHEMA["properties"]["core_tests"]
        self.assertNotIn("maxItems", core_rule)
        self.assertNotIn("uniqueItems", core_rule)
        self.assertIn("1..5 distinct exact", core_rule["description"])
        self.assertEqual(core_rule["items"]["pattern"],
                         r"^ZhuGuoCity\.[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*$")
        self.assertEqual(PLAN_WITH_CORE_TESTS_SCHEMA["properties"]["core_tests"],
                         {"type": "array", "items": {"type": "string"}})
        self.assertIn("说明只写在 body".encode(), launch.call_args.kwargs["stdin"])
        self.assertEqual(AgentProviders({}).recover("plan", self.out), planned)
        write_events(self.out / "stdout.log", self.events())
        with self.assertRaisesRegex(ProviderError, "saved schema"):
            self.provider.recover("plan", self.out)

    def test_legacy_annotations_recover_without_changing_raw_evidence_or_task_acceptance(self):
        names = ["ZhuGuoCity.M2.FoodSave", "ZhuGuoCity.M2.RoadRules", "ZhuGuoCity.M2.RoadSave",
                 "ZhuGuoCity.M2.RoadQuotePath", "ZhuGuoCity.M2.ResidentRoutes"]
        annotated = [names[0] + "（对应 A1，复用并修正结构版本断言）",
                     names[1] + "(对应 A2，复用，按需修正)",
                     names[2] + "（对应 A3，复用，按需修正）",
                     names[3] + "（对应 A4，复用，按需修正）",
                     names[4] + "（对应 A5，复用，按需修正）"]
        for tiered, schema in ((False, PLAN_WITH_CORE_TESTS_SCHEMA),
                               (True, TIERED_PLAN_WITH_CORE_TESTS_SCHEMA)):
            with self.subTest(tiered=tiered):
                self.out = self.root / ("legacy-tiered" if tiered else "legacy-core")
                plan = PLAN | {"core_tests": annotated}
                if tiered:
                    plan.update(planning_level="routine", planning_reason="Update existing test fixtures.")
                original = copy.deepcopy(plan)
                self.receipt(self.events(plan), schema=schema)
                (self.out / "result.json").write_text('{"preserve_existing_result":true}', encoding="utf-8")
                before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.out.iterdir()}
                result = AgentProviders({}).recover("plan", self.out)
                self.assertEqual(result, plan | {"core_tests": names})
                self.assertEqual(plan, original)
                self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.out.iterdir()}, before)
                with self.assertRaisesRegex(ProviderError, "invalid core_tests"):
                    validate_output("plan", plan)

    def test_legacy_annotation_repair_rejects_unsafe_forms_duplicates_and_limits(self):
        name = "ZhuGuoCity.M2.Save"
        invalid_names = ["run " + name + "（说明）", name + "（说明）;whoami", name + "*（说明）",
                         name + "（说明)", name + "(说明", name + "（嵌套(说明)）",
                         name + "（说明）（第二段）", name + "（说明） ", name + "（说明）\n",
                         name + "（含\x00控制字符）", name + " (说明)", name + ";whoami（说明）"]
        cases = [[value] for value in invalid_names]
        cases.extend([[name + "（说明一）", name + "(说明二)"],
                      [name + "（说明）", "ZhuGuoCity.M2.save（说明）"],
                      [f"ZhuGuoCity.M2.Test{i}（说明）" for i in range(6)], [], [None]])
        for names in cases:
            with self.subTest(names=names):
                self.receipt(self.events(PLAN | {"core_tests": names}), schema=PLAN_WITH_CORE_TESTS_SCHEMA)
                with self.assertRaises(ProviderError):
                    self.provider.recover("plan", self.out)

    def test_strict_core_test_receipts_validate_annotations_duplicates_and_limits(self):
        for tiered, schema in ((False, STRICT_PLAN_WITH_CORE_TESTS_SCHEMA),
                               (True, STRICT_TIERED_PLAN_WITH_CORE_TESTS_SCHEMA)):
            for names in (["ZhuGuoCity.M2.FoodSave（对应 A1，复用）"],
                          ["ZhuGuoCity.M2.FoodSave", "ZhuGuoCity.M2.FoodSave"],
                          [f"ZhuGuoCity.M2.Test{i}" for i in range(6)]):
                with self.subTest(tiered=tiered, names=names):
                    plan = PLAN | {"core_tests": names}
                    if tiered:
                        plan.update(planning_level="routine", planning_reason="Update an existing test fixture.")
                    self.receipt(self.events(plan), schema=schema)
                    with self.assertRaisesRegex(ProviderError, "invalid core_tests"):
                        self.provider.recover("plan", self.out)

    def test_completed_process_requires_matching_protocol_and_valid_structured_output(self):
        cases = {}
        prose_only = self.events()
        del prose_only[-1]["structured_output"]
        cases["prose only"] = prose_only
        cases["invalid task"] = self.events(PLAN | {"acceptance_criteria": []})
        mismatched = self.events()
        mismatched[-1]["session_id"] = OTHER_SESSION
        cases["mismatched session"] = mismatched
        cases["trailing second result"] = self.events() + [copy.deepcopy(self.events()[-1])]
        for name, rows in cases.items():
            with self.subTest(name=name):
                self.receipt(rows)
                with self.assertRaises(ProviderError):
                    self.provider.recover("plan", self.out)

    def test_native_ui_header_requires_the_same_following_init_and_known_prefix_events(self):
        prefix = {"type": "system", "subtype": "ui_invalidate", "session_id": SESSION,
                  "uuid": "native-ui-event"}
        self.receipt([prefix, *self.events()])
        self.assertEqual(self.provider.recover("plan", self.out), PLAN)
        self.assertEqual(read_session(self.out), SESSION)
        self.assertIsNone(read_session(self.out, requested_session=OTHER_SESSION))
        cases = {
            "conflicting prefix": [prefix | {"session_id": OTHER_SESSION}, *self.events()],
            "conflicting second prefix": [prefix, prefix | {"session_id": OTHER_SESSION}, *self.events()],
            "unknown prefix": [prefix | {"subtype": "unknown"}, *self.events()],
            "invalid prefix session": [prefix | {"session_id": "invalid"}, *self.events()],
            "no init": [prefix],
            "mixed protocols": [prefix, *CODEX_EVENTS],
        }
        for name, rows in cases.items():
            with self.subTest(name=name):
                self.receipt(rows)
                self.assertIsNone(read_session(self.out))
                with self.assertRaises(ProviderError):
                    self.provider.recover("plan", self.out)

    @patch("unattended.providers.claude_subscription_ready", return_value=True)
    @patch("unattended.providers.run_process")
    def test_existing_invocation_cannot_be_replayed_or_have_its_schema_overwritten(self, launch, auth):
        self.receipt(self.events(), schema=PLAN_WITH_CORE_TESTS_SCHEMA)
        before = {p.name: p.read_bytes() for p in self.out.iterdir()}
        with self.assertRaisesRegex(ProviderError, "already used"):
            self.provider.plan("Plan again", self.work, self.out, 30)
        self.assertEqual({p.name: p.read_bytes() for p in self.out.iterdir()}, before)
        launch.assert_not_called()

    @patch("unattended.providers.claude_subscription_ready", return_value=True)
    @patch("unattended.providers.run_process")
    @patch("unattended.providers.time.time", return_value=1000)
    def test_native_claude_retry_and_subscription_reset_metadata_are_preserved_for_plan(self, clock, launch, auth):
        cases = [
            ("overloaded", [], "server_unavailable", None),
            ("rate_limit", [{"type": "rate_limit_event", "session_id": SESSION,
                             "rate_limit_info": {"status": "rejected", "resetsAt": 1180}}],
             "claude_subscription_limit", 180),
        ]
        for code, extra, reason, delay in cases:
            with self.subTest(code=code):
                self.out = self.root / code
                error = {"type": "assistant", "session_id": SESSION, "error": code}
                rows = self.events(extra=[error, *extra], is_error=True)
                launch.side_effect = lambda *args, **kwargs: self.capture(rows)
                with self.assertRaises(ProviderError) as caught:
                    self.provider.plan("Plan", self.work, self.out, 30)
                self.assertTrue(caught.exception.transient)
                self.assertEqual(caught.exception.retry_reason, reason)
                self.assertEqual(caught.exception.retry_after_seconds, delay)
                classified = classify_transient_failure("plan", self.out)
                self.assertEqual(classified["reason"], reason)
                self.assertEqual(classified.get("retry_after_seconds"), delay)
                self.assertIsNone(self.provider.recover("plan", self.out))


if __name__ == "__main__":
    unittest.main()
