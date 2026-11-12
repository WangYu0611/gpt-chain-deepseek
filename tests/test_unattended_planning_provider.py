"""Provider changes preserve planning checkpoints and independent review."""
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from tests import test_unattended_config as configuration
from tests import test_unattended_dashboard_state as dashboard_fixture
from tests import test_unattended_recovery as recovery
from unattended.config import load_config
from unattended.providers import ProviderError
from unattended import gitops


class PlanningConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = configuration.ConfigurationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_legacy_configuration_keeps_codex_and_claude_planner_is_validated(self):
        self.assertEqual(load_config(self.fixture.save())["planning_provider"], "codex")
        self.fixture.value["planning_provider"] = "claude"
        with self.assertRaisesRegex(ValueError, "claude"):
            load_config(self.fixture.save())
        self.fixture.value["claude"] = {"model": "sonnet", "reasoning_effort": "high"}
        self.assertEqual(load_config(self.fixture.save())["planning_provider"], "claude")
        self.fixture.value["claude"]["reasoning_effort"] = "invalid"
        with self.assertRaisesRegex(ValueError, "claude.reasoning_effort"):
            load_config(self.fixture.save())

    def test_unknown_planner_is_rejected_before_execution(self):
        self.fixture.value["planning_provider"] = "dsh"
        with self.assertRaisesRegex(ValueError, "planning_provider"):
            load_config(self.fixture.save())


class PlanningEngineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = recovery.RecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.config.update(planning_provider="claude", coding_provider="claude", claude={})
        self.fixture.engine = self.fixture.restart()

    def test_claude_plan_is_tagged_and_task_still_reaches_independent_review(self):
        original = self.fixture.providers.plan

        def plan(prompt, cwd, out, timeout):
            self.assertIn("Claude Code 项目经理", prompt)
            self.assertIn("Codex 负责独立任务验收和阶段验收", prompt)
            operation = self.fixture.load()["operation"]
            self.assertEqual(operation["planning_provider"], "claude")
            self.assertNotIn("coding_provider", operation)
            return original(prompt, cwd, out, timeout)

        with patch.object(self.fixture.providers, "plan", side_effect=plan):
            self.assertEqual(self.fixture.engine.step()["phase"], "CODE")
        self.assertEqual(self.fixture.restart().step()["phase"], "CHECK")
        self.assertEqual(self.fixture.restart().step()["phase"], "REVIEW")
        self.assertEqual(self.fixture.restart().step()["phase"], "PUBLISH")
        self.assertEqual(self.fixture.providers.review_calls, 1)

    def test_existing_codex_plan_receipt_recovers_after_provider_switch(self):
        self.fixture.config["planning_provider"] = "codex"
        work = gitops.ensure_workspace(self.fixture.config)
        state = self.fixture.engine._load()
        state["expected_head"] = gitops.head(work)
        operation = self.fixture.engine._new_operation(state, "plan")
        operation.pop("planning_provider")  # Legacy operations predate the selector.
        self.fixture.engine._save(state)
        self.fixture.providers.recovered[("plan", operation["output_dir"])] = dict(recovery.PLAN)
        self.fixture.config["planning_provider"] = "claude"
        self.assertEqual(self.fixture.restart().step()["phase"], "CODE")
        self.assertEqual(self.fixture.providers.plan_calls, 0)

    def test_claude_planning_quota_wait_survives_restart_and_config_switch(self):
        error = ProviderError("Claude limit", transient=True, retry_reason="claude_subscription_limit",
                              retry_after_seconds=3600)
        started = self.fixture.clock[0]
        with patch.object(self.fixture.providers, "plan", side_effect=error):
            self.assertEqual(self.fixture.engine.step()["status"], "waiting_provider")
        state = self.fixture.load()
        self.assertEqual(state["operation"]["planning_provider"], "claude")
        self.assertEqual(datetime.fromisoformat(state["provider_retry"]["next_retry_at"]),
                         started + timedelta(hours=1))
        self.assertEqual(state["failures"], 0)
        self.fixture.config["planning_provider"] = "codex"
        self.assertEqual(self.fixture.restart().step()["status"], "waiting_provider")
        self.fixture.clock[0] += timedelta(hours=1)
        with patch("unattended.providers.classify_transient_failure", return_value=None):
            self.assertEqual(self.fixture.restart().step()["phase"], "CODE")


class PlanningDashboardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = dashboard_fixture.DashboardStateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.config.update(planning_provider="claude", coding_provider="claude", claude={})
        self.fixture.write(self.fixture.config_path, self.fixture.config)

    def test_claude_plan_activity_and_legacy_codex_plan_use_their_saved_provider(self):
        self.fixture.logs([{"type": "system", "subtype": "init", "session_id": "unused"}])
        self.fixture.state["operation"]["planning_provider"] = "claude"
        snapshot = self.fixture.snapshot()
        self.assertEqual(snapshot["status"]["actor"], "Claude Code")
        self.assertEqual(snapshot["planning_provider"], "claude")
        self.assertIsNone(snapshot["codex_auth"]["current"])
        self.fixture.state["operation"].pop("planning_provider")
        self.assertEqual(self.fixture.snapshot()["status"]["actor"], "Codex")


if __name__ == "__main__":
    unittest.main()
