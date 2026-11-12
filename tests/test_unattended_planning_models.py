"""Routine planning is direct, while critical plans survive an Opus handoff."""
from datetime import timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_unattended_engine as gate
from unattended.engine import Engine
from unattended.providers import ProviderError
from unattended.storage import read_json


def proposal(level="routine", action="task"):
    return {"action": action, "title": "A bounded feature", "body": "Implement the requested behavior.",
            "acceptance_criteria": ["The requested behavior passes its focused check."], "reason": "Next step",
            "planning_level": level, "planning_reason": "The actual change scope determines the selected planning tier."}


class PlanningModelTests(unittest.TestCase):
    def setUp(self):
        self.fixture = gate.EngineGateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = self.fixture.config
        self.config.update(planning_provider="claude", coding_provider="claude", claude={
            "model": "claude-sonnet-5-5", "reasoning_effort": "high", "critical_plan_model": "claude-opus-5-5",
        })
        self.providers = self.fixture.providers
        self.providers.recover.return_value = None
        self.engine = self.fixture.engine

    def restart(self):
        return Engine(self.config, self.providers, now=lambda: self.fixture.now)

    def state(self):
        return read_json(self.engine.state_path)

    def test_routine_plan_goes_directly_to_code_with_one_sonnet_call(self):
        self.providers.plan.return_value = proposal()
        self.assertEqual(self.engine.step()["phase"], "CODE")
        self.providers.plan.assert_called_once()
        self.assertEqual(self.providers.plan.call_args.kwargs["model"], "claude-sonnet-5-5")
        task = self.state()["task"]
        self.assertEqual(task["planning_model"], "claude-sonnet-5-5")
        self.assertEqual(task["planning_level"], "routine")
        self.assertNotIn("planning_selection", self.state())
        self.providers.code.assert_not_called()
        self.providers.review.assert_not_called()

    def test_critical_plan_uses_fresh_opus_step_and_retains_sonnet_proposal(self):
        draft, final = proposal("critical"), proposal("critical")
        final["body"] = "Opus validates the system boundaries and publishes the final task."
        self.providers.plan.side_effect = [draft, final]
        self.assertEqual(self.engine.step()["phase"], "PLAN")
        selected = self.state()["planning_selection"]
        self.assertEqual(selected["planning_model"], "claude-opus-5-5")
        self.assertEqual(selected["critical_candidate"], draft)
        self.assertIsNone(self.state()["operation"])
        source = Path(selected["source_output_dir"])
        self.assertEqual(read_json(source / "planning-decision.json")["result"], draft)
        self.assertEqual(self.restart().step()["phase"], "CODE")
        first, second = self.providers.plan.call_args_list
        self.assertEqual((first.kwargs["model"], second.kwargs["model"]),
                         ("claude-sonnet-5-5", "claude-opus-5-5"))
        self.assertNotEqual(first.args[2], second.args[2])
        self.assertIn("待核实的提案", second.args[0])
        task = self.state()["task"]
        self.assertEqual(task["body"], final["body"])
        self.assertEqual(task["planning_level"], "critical")
        self.assertEqual(task["planning_source_output_dir"], str(source))
        self.assertNotIn("planning_selection", self.state())

    def test_opus_transient_retry_keeps_model_and_effort_when_config_changes(self):
        error = ProviderError("Temporary overload", transient=True, retry_reason="server_unavailable")
        self.providers.plan.side_effect = [proposal("critical"), error, proposal("critical")]
        self.assertEqual(self.engine.step()["phase"], "PLAN")
        self.assertEqual(self.restart().step()["status"], "waiting_provider")
        state = self.state()
        self.assertEqual(state["operation"]["planning_model"], "claude-opus-5-5")
        self.config["claude"].update(model="changed-routine", critical_plan_model="changed-critical", reasoning_effort="low")
        self.fixture.now += timedelta(minutes=3)
        with patch("unattended.providers.classify_transient_failure", return_value=None):
            self.assertEqual(self.restart().step()["phase"], "CODE")
        self.assertEqual(self.providers.plan.call_args.kwargs["model"], "claude-opus-5-5")
        self.assertEqual(self.providers.plan.call_args.kwargs["reasoning_effort"], "high")
        self.assertEqual(self.state()["failures"], 0)

    def test_recovered_sonnet_receipt_is_escalated_once_after_crash(self):
        draft = proposal("critical")

        def crash_after_receipt(*args, **kwargs):
            self.providers.recover.return_value = draft
            raise SystemExit("Simulated process loss after the durable model result")

        self.providers.plan.side_effect = crash_after_receipt
        with self.assertRaises(SystemExit):
            self.engine.step()
        self.assertEqual(self.restart().step()["phase"], "PLAN")
        self.assertEqual(self.providers.plan.call_count, 1)
        self.assertEqual(self.state()["planning_selection"]["planning_level"], "critical")
        self.providers.recover.return_value = None
        self.providers.plan.side_effect = None
        self.providers.plan.return_value = proposal("critical")
        self.assertEqual(self.restart().step()["phase"], "CODE")
        self.assertEqual(self.providers.plan.call_count, 2)

    def test_pending_opus_call_keeps_tier_schema_after_critical_config_is_removed(self):
        error = ProviderError("Temporary overload", transient=True, retry_reason="server_unavailable")
        self.providers.plan.side_effect = [proposal("critical"), error, proposal("critical")]
        self.assertEqual(self.engine.step()["phase"], "PLAN")
        self.assertEqual(self.restart().step()["status"], "waiting_provider")
        self.config["claude"].pop("critical_plan_model")
        self.fixture.now += timedelta(minutes=3)
        with patch("unattended.providers.classify_transient_failure", return_value=None):
            self.assertEqual(self.restart().step()["phase"], "CODE")
        self.assertEqual(self.providers.plan.call_args.kwargs["model"], "claude-opus-5-5")
        self.assertTrue(self.providers.plan.call_args.kwargs["tiered"])

    def test_complete_proposal_uses_opus_before_independent_milestone_review(self):
        self.config["publication"] = {"mode": "milestone", "milestone_id": "M2", "goal": "The complete milestone"}
        state = self.engine._load()
        state["accepted"] = [{"id": "TASK-TEST-0000", "title": "Earlier task", "base_sha": "b" * 40,
                              "head_sha": "a" * 40, "publication": {"local_only": True}}]
        self.engine._save(state)
        self.providers.plan.side_effect = [proposal(action="complete"), proposal("critical", action="complete")]
        self.assertEqual(self.engine.step()["phase"], "PLAN")
        self.assertEqual(self.restart().step()["phase"], "MILESTONE_REVIEW")
        self.assertEqual(self.providers.plan.call_args.kwargs["model"], "claude-opus-5-5")
        self.providers.review.assert_not_called()
        self.assertNotIn("planning_selection", self.state())

    def test_critical_candidate_is_bound_to_its_exact_head(self):
        self.providers.plan.return_value = proposal("critical")
        self.assertEqual(self.engine.step()["phase"], "PLAN")
        with patch("unattended.engine.gitops.head", return_value="b" * 40):
            result = self.restart().step()
        self.assertEqual(result["phase"], "PLAN")
        self.assertEqual(self.providers.plan.call_count, 1)
        self.assertEqual(self.state()["planning_selection"]["head_sha"], "a" * 40)

    def test_legacy_receipt_without_tier_fields_recovers_under_new_configuration(self):
        state = self.engine._load()
        state["expected_head"] = "a" * 40
        operation = self.engine._new_operation(state, "plan")
        self.assertNotIn("planning_model", operation)
        old_result = proposal()
        old_result.pop("planning_level")
        old_result.pop("planning_reason")
        self.providers.recover.return_value = old_result
        self.assertEqual(self.restart().step()["phase"], "CODE")
        self.providers.plan.assert_not_called()
        self.assertNotIn("planning_selection", self.state())

    def test_missing_tier_classification_cannot_publish_a_new_plan(self):
        result = proposal()
        result.pop("planning_level")
        self.providers.plan.return_value = result
        self.assertEqual(self.engine.step()["phase"], "PLAN")
        self.assertIsNone(self.state()["task"])
        self.assertIn("planning_level", self.state()["last_error"])


if __name__ == "__main__":
    unittest.main()
