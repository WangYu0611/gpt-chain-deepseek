"""Critical planning overrides must be validated before starting a model."""
import copy
import unittest

from tests import test_unattended_config as configuration
from tests import test_unattended_dashboard_state as dashboard_fixture
from unattended.config import load_config


class PlanningModelConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = configuration.ConfigurationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.value.update(planning_provider="claude", coding_provider="claude")
        self.fixture.value["claude"] = {
            "model": "claude-sonnet-5-5", "reasoning_effort": "high",
            "critical_plan_model": "claude-opus-5-5", "critical_plan_reasoning_effort": "high",
        }

    def test_critical_model_keeps_the_coding_model_and_legacy_defaults(self):
        settings = load_config(self.fixture.save())["claude"]
        self.assertEqual(settings["model"], "claude-sonnet-5-5")
        self.assertEqual(settings["critical_plan_model"], "claude-opus-5-5")
        self.fixture.value["claude"].pop("critical_plan_model")
        self.fixture.value["claude"].pop("critical_plan_reasoning_effort")
        self.assertNotIn("critical_plan_model", load_config(self.fixture.save())["claude"])

    def test_invalid_critical_model_or_effort_is_rejected(self):
        for field, value in (("critical_plan_model", ""), ("critical_plan_model", []),
                             ("critical_plan_reasoning_effort", "invalid")):
            candidate = copy.deepcopy(self.fixture.value)
            candidate["claude"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                load_config(self.fixture.save(candidate))

    def test_critical_effort_and_provider_require_the_corresponding_model(self):
        candidate = copy.deepcopy(self.fixture.value)
        candidate["claude"].pop("critical_plan_model")
        with self.assertRaisesRegex(ValueError, "critical_plan_model"):
            load_config(self.fixture.save(candidate))
        self.fixture.value["planning_provider"] = "codex"
        with self.assertRaisesRegex(ValueError, "planning_provider"):
            load_config(self.fixture.save())

    def test_critical_routing_requires_an_explicit_routine_model(self):
        self.fixture.value["claude"].pop("model")
        with self.assertRaisesRegex(ValueError, "claude.model"):
            load_config(self.fixture.save())


class PlanningModelDashboardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = dashboard_fixture.DashboardStateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.config.update(planning_provider="claude", coding_provider="claude", claude={
            "model": "claude-sonnet-5-5", "reasoning_effort": "high",
            "critical_plan_model": "claude-opus-5-5",
        })
        self.fixture.write(self.fixture.config_path, self.fixture.config)

    def test_actual_planner_model_is_shown_without_overriding_the_coding_model(self):
        self.fixture.logs([])
        self.fixture.state["operation"].update(
            planning_provider="claude", planning_model="claude-opus-5-5",
            planning_reasoning_effort="high", planning_level="critical")
        snapshot = self.fixture.snapshot()
        self.assertIsNone(snapshot["error"])
        self.assertEqual(snapshot["models"]["claude"]["model"], "claude-opus-5-5")
        self.fixture.state["phase"] = "CODE"
        self.fixture.state["operation"] = None
        self.assertEqual(self.fixture.snapshot()["models"]["claude"]["model"], "claude-sonnet-5-5")

    def test_pending_critical_selection_is_visible_between_planning_calls(self):
        self.fixture.state["planning_selection"] = {
            "planning_model": "claude-opus-5-5", "planning_level": "critical",
            "planning_reasoning_effort": "high",
        }
        snapshot = self.fixture.snapshot()
        self.assertIsNone(snapshot["error"])
        self.assertEqual(snapshot["models"]["claude"]["model"], "claude-opus-5-5")
        self.assertEqual(snapshot["planning_selection"]["level"], "critical")


if __name__ == "__main__":
    unittest.main()
