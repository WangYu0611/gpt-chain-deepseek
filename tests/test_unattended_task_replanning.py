"""Bounded repair planning preserves the real task and all independent gates."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_unattended_engine as fixtures
from tests import test_unattended_config as config_fixtures
from unattended.config import load_config, validate_task_replanning_settings
from unattended.engine import Engine
from unattended.providers import ProviderError
from unattended.storage import read_json, write_json, write_text

HEAD = "a" * 40
BASE = "b" * 40
SESSION = "550e8400-e29b-41d4-a716-446655440000"
TASK = "TASK-TEST-0001"


class TaskReplanningTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.EngineGateTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.config = self.f.config
        self.config.update(planning_provider="claude", coding_provider="claude",
            claude={"model": "claude-sonnet-5-5", "reasoning_effort": "high",
                    "critical_plan_model": "claude-opus-5-5", "critical_plan_reasoning_effort": "high",
                    "planned_core_tests": True},
            task_replanning={"enabled": True, "max_cycles": 2, "rework_window": 2})
        self.engine = self.f.engine
        self.providers = self.f.providers
        self.providers.recover.return_value = None
        self.work = self.engine.runtime / "workspace"
        self.work.mkdir(parents=True)
        self.folder = self.engine.runtime / "tasks" / TASK
        self.check = self.engine.runtime / "runs" / "check-one"
        self.check.mkdir(parents=True)
        write_text(self.check / "stdout.log", "A real independent check receipt in the fixture.\n")
        write_text(self.check / "stderr.log", "")
        self.check_result = {"id": "behavior", "passed": True, "head_sha": HEAD,
                             "stdout_path": str(self.check / "stdout.log"),
                             "stderr_path": str(self.check / "stderr.log")}
        write_json(self.check / "check.json", self.check_result)
        self.task = {"id": TASK, "title": "Original game behavior", "body": "Original immutable scope.",
                     "acceptance_criteria": ["The original natural player flow passes without artificial state."],
                     "core_tests": ["ZhuGuoCity.M3.DeathFlow"], "base_sha": BASE, "code_base": BASE,
                     "coding_provider": "claude", "reworks": 3, "candidate": {"head_sha": HEAD},
                     "session_id": SESSION, "session_owner_task_id": TASK, "code_resume": False,
                     "check_results": [deepcopy(self.check_result)], "feedback": "Existing feedback"}
        write_text(self.folder / "delivery-3.md", "Previous coding delivery.\n")
        self.review = {"decision": "rework", "summary": "Original behavior is not yet demonstrated.",
                       "feedback": "Identify the missing natural flow and fix the real cause.", "head_sha": HEAD}
        write_json(self.folder / "review-3.json", self.review)
        state = self.engine._load()
        state.update(phase="REVIEW", expected_head=BASE, task=deepcopy(self.task))
        self.engine._save(state)
        self.providers.review.return_value = self.review
        self.plan = {"action": "task", "title": "Inspect the root cause before another attempt",
                     "body": "Read the failed evidence, diagnose the cause, repair it, and rerun all original checks.",
                     "acceptance_criteria": ["Supplemental diagnostic evidence is collected."],
                     "core_tests": ["ZhuGuoCity.M3.DeathFlow"], "reason": "Same task, bounded repair."}
        self.providers.plan.return_value = self.plan

    def state(self):
        return read_json(self.engine.state_path)

    def restart(self):
        return Engine(self.config, self.providers, now=lambda: self.f.now)

    def trigger(self):
        result = self.engine.step()
        self.assertEqual(result["phase"], "REPLAN", result)
        return self.state()

    def assert_original_task(self, current):
        for key in ("id", "title", "body", "acceptance_criteria", "core_tests", "base_sha", "coding_provider", "session_id"):
            self.assertEqual(current[key], self.task[key], key)

    def test_cap_runs_critical_planner_then_original_coder_without_replacing_scope_or_acceptance(self):
        state = self.trigger()
        self.assertNotIn("autonomous_rework_allowance", state["task"])
        self.assertEqual(self.providers.plan.call_count, 0)
        self.assertEqual(self.engine.step()["phase"], "CODE")
        state = self.state()
        task = state["task"]
        self.assert_original_task(task)
        self.assertEqual(task["reworks"], 4)
        self.assertEqual(task["autonomous_rework_allowance"], 2)
        self.assertEqual(task["code_base"], HEAD)
        self.assertEqual(task["check_results"], [])
        self.assertEqual(state["expected_head"], BASE)
        self.assertEqual(state["accepted"], [])
        self.assertEqual(self.providers.plan.call_args.kwargs,
                         {"model": "claude-opus-5-5", "reasoning_effort": "high", "tiered": False})
        self.assertIn(self.plan["body"], task["feedback"])
        self.assertEqual(read_json(self.folder / "replan-1.json")["result"], self.plan)
        head = patch("unattended.engine.gitops.head", return_value=HEAD).start()
        patch("unattended.engine.gitops.verify_candidate", return_value={"head_sha": "c" * 40}).start()
        def code(*args, **kwargs):
            head.return_value = "c" * 40
            return {"completed": True, "session_id": SESSION, "text": "Repaired locally."}
        self.providers.code.side_effect = code
        self.assertEqual(self.engine.step()["phase"], "CHECK")
        self.assertEqual(self.providers.code.call_args.kwargs["session_id"], SESSION)
        self.assertEqual(self.config["claude"]["model"], "claude-sonnet-5-5")
        self.assertEqual(self.state()["accepted"], [])

    def test_changed_plan_metadata_and_core_tests_are_guidance_never_task_mutations(self):
        self.trigger()
        self.plan.update(title="Different plan title", acceptance_criteria=["Short repair verification."],
                         core_tests=["ZhuGuoCity.M3.OtherDiagnostic"])
        self.assertEqual(self.engine.step()["phase"], "CODE")
        self.assert_original_task(self.state()["task"])
        self.assertEqual(self.config["checks"][0]["id"], "behavior")

    def test_disabled_policy_keeps_the_original_hard_cap_and_stop_behavior(self):
        self.config.pop("task_replanning")
        result = self.engine.step()
        self.assertEqual(result["phase"], "BLOCKED")
        self.assertIn("返工次数上限", result["last_error"])
        self.providers.plan.assert_not_called()

    def test_explicit_replan_retry_enters_same_task_without_resetting_history_or_session(self):
        self.config["task_replanning"]["enabled"] = False
        self.engine.step()
        before = self.state()
        self.config["task_replanning"]["enabled"] = True
        self.assertEqual(self.engine.retry(replan=True)["phase"], "REPLAN")
        after = self.state()
        self.assert_original_task(after["task"])
        self.assertEqual(after["task"]["reworks"], 3)
        self.assertEqual(after["expected_head"], before["expected_head"])
        self.assertEqual(after["task"]["check_results"], before["task"]["check_results"])
        self.assertEqual(after["task"]["replanning"]["trigger"], "retry_replan")
        self.providers.plan.assert_not_called()
        for options in ({"rework": True}, {"new_session": True}, {"external_validation": True}):
            with self.subTest(options=options), self.assertRaises(ValueError): self.engine.retry(replan=True, **options)

    def test_retry_replan_rejects_wrong_phase_dirty_head_and_explicit_external_blocker(self):
        state = self.state()
        state.update(phase="BLOCKED", blocked_from="CODE")
        self.engine._save(state)
        with self.assertRaises(ValueError): self.engine.retry(replan=True)
        state["blocked_from"] = "REVIEW"
        self.engine._save(state)
        with patch("unattended.engine.gitops.clean", return_value=False), self.assertRaises(ValueError):
            self.engine.retry(replan=True)
        write_json(self.folder / "review-3.json", {**self.review, "decision": "blocked"})
        with self.assertRaises(ValueError): self.engine.retry(replan=True)
        self.providers.plan.assert_not_called()

    def test_changed_scope_or_evidence_before_call_never_dispatches_a_plan(self):
        self.trigger()
        state = self.state()
        state["task"]["body"] += " unexpected change"
        self.engine._save(state)
        result = self.engine.step()
        self.assertIn("来源已改变", result["last_error"])
        self.providers.plan.assert_not_called()
        self.assertNotIn("autonomous_rework_allowance", self.state()["task"])

    def test_changed_log_during_plan_never_grants_a_window(self):
        self.trigger()
        def plan(*args, **kwargs):
            write_text(self.check / "stdout.log", "Changed after the plan began.\n")
            return self.plan
        self.providers.plan.side_effect = plan
        result = self.engine.step()
        self.assertIn("证据被改变", result["last_error"])
        self.assertNotIn("replanning_history", self.state()["task"])
        self.assertNotIn("autonomous_rework_allowance", self.state()["task"])

    def test_current_ue_mirror_binds_actual_nested_validation_logs(self):
        validation = self.folder / "validation" / "current"
        write_json(validation / "receipt.json", {"task_id": TASK, "head_sha": HEAD})
        write_text(validation / "01-core" / "stdout.log", "actual natural-flow evidence")
        write_json(self.work / "Saved/Automation" / TASK / "validation-results.json", {
            "task_id": TASK, "head_sha": HEAD, "receipt_path": str(validation / "receipt.json"),
            "tests": [{"stdout_path": str(validation / "01-core" / "stdout.log")}]})
        self.trigger()
        write_text(validation / "01-core" / "stdout.log", "a substituted nested log")
        self.assertIn("来源已改变", self.engine.step()["last_error"])
        self.providers.plan.assert_not_called()

    def test_complete_or_external_blocked_plan_cannot_accept_or_publish_original_task(self):
        self.trigger()
        self.plan.update(action="complete", core_tests=[])
        result = self.engine.step()
        self.assertEqual(result["phase"], "BLOCKED")
        self.assertEqual(self.state()["blocked_from"], "REPLAN")
        self.assertIn("不能代替", result["last_error"])
        self.assertEqual(self.state()["accepted"], [])
        self.assertNotIn("autonomous_rework_allowance", self.state()["task"])

    def test_completed_provider_receipt_is_recovered_without_repeating_plan(self):
        self.trigger()
        def crash(*args, **kwargs):
            self.providers.recover.return_value = deepcopy(self.plan)
            raise SystemExit("crash after provider result")
        self.providers.plan.side_effect = crash
        with self.assertRaises(SystemExit): self.engine.step()
        self.assertEqual(self.restart().step()["phase"], "CODE")
        self.assertEqual(self.providers.plan.call_count, 1)
        self.assertEqual(len(self.state()["task"]["replanning_history"]), 1)

    def test_controller_receipt_before_state_save_recovers_exactly_once(self):
        self.trigger()
        self.providers.recover.return_value = deepcopy(self.plan)
        original = self.engine._save
        def crash(state):
            if state["phase"] == "CODE": raise SystemExit("after receipt, before state")
            return original(state)
        with patch.object(self.engine, "_save", side_effect=crash), self.assertRaises(SystemExit): self.engine.step()
        self.assertTrue((self.folder / "replan-1.json").exists())
        self.assertEqual(self.restart().step()["phase"], "CODE")
        state = self.state()
        self.assertEqual(state["task"]["reworks"], 4)
        self.assertEqual(state["task"]["autonomous_rework_allowance"], 2)
        self.assertEqual(len(state["task"]["replanning_history"]), 1)
        self.assertEqual(self.providers.plan.call_count, 1)

    def test_quota_wait_preserves_model_task_session_and_failure_budget(self):
        self.trigger()
        self.providers.plan.side_effect = ProviderError("Quota", transient=True,
            retry_reason="claude_subscription_limit", retry_after_seconds=1800)
        self.assertEqual(self.engine.step()["status"], "waiting_provider")
        state = self.state()
        self.assertEqual(state["provider_retry"]["phase"], "REPLAN")
        self.assertEqual(state["failures"], 0)
        self.assertEqual(state["task"]["reworks"], 3)
        self.assertEqual(state["task"]["session_id"], SESSION)
        self.config["claude"]["critical_plan_model"] = "new-config-model"
        self.f.now += timedelta(minutes=31)
        self.providers.plan.side_effect = None
        with patch("unattended.providers.classify_transient_failure", return_value=None):
            self.assertEqual(self.restart().step()["phase"], "CODE")
        self.assertEqual(self.providers.plan.call_args.kwargs["model"], "claude-opus-5-5")

    def test_successful_but_invalid_output_is_retired_and_does_not_recover_forever(self):
        self.trigger()
        def invalid(prompt, work, out, timeout, **kwargs):
            write_json(out / "process.json", {"kind": "plan", "exit_code": 0, "timed_out": False,
                                               "output_limit_exceeded": False})
            return {**self.plan, "unknown_field": True}
        self.providers.plan.side_effect = invalid
        result = self.engine.step()
        self.assertEqual(result["phase"], "REPLAN")
        self.assertEqual(result["failures"], 1)
        self.assertIsNone(self.state()["operation"])
        self.assertEqual(len(self.state()["provider_output_failures"]), 1)

    def test_same_head_and_cycle_limit_cannot_grant_infinite_windows(self):
        self.trigger()
        self.engine.step()
        state = self.state()
        state["phase"] = "REVIEW"
        state["task"]["reworks"] = 5
        state["task"]["check_results"] = [deepcopy(self.check_result)]
        write_text(self.folder / "delivery-5.md", "No new head.")
        self.engine._save(state)
        result = self.engine.step()
        self.assertEqual(result["phase"], "BLOCKED")
        self.assertIn("同一候选", result["last_error"])
        self.assertEqual(self.providers.plan.call_count, 1)
        state = self.state()
        state["phase"] = "REVIEW"
        state["task"]["replanning_history"].append(deepcopy(state["task"]["replanning_history"][0]))
        self.engine._save(state)
        self.assertIn("窗口已用完", self.engine.step()["last_error"])
        self.assertEqual(self.config["max_reworks"], 3)

    def test_plain_retry_recovers_a_completed_replan_and_pause_window_gates_still_apply(self):
        self.trigger()
        state = self.state()
        op = self.engine._new_operation(state, "plan")
        state.update(phase="BLOCKED", blocked_from="REPLAN", failures=3)
        self.engine._save(state)
        self.providers.recover.return_value = deepcopy(self.plan)
        self.assertEqual(self.engine.retry()["phase"], "REPLAN")
        self.assertEqual(self.state()["operation"], op)
        self.engine.pause()
        self.assertEqual(self.engine.step()["status"], "paused")
        self.engine.resume()
        self.f.now = self.f.now.replace(hour=12)
        self.assertEqual(self.engine.step()["status"], "waiting_window")
        self.providers.plan.assert_not_called()
        self.f.now = self.f.now.replace(hour=19)
        self.assertEqual(self.engine.step()["phase"], "CODE")
        self.providers.plan.assert_not_called()


class TaskReplanningConfigurationTests(unittest.TestCase):
    def test_strict_opt_in_policy_rejects_booleans_unknown_keys_and_unbounded_cycles(self):
        valid = {"enabled": True, "max_cycles": 2, "rework_window": 2}
        self.assertEqual(validate_task_replanning_settings(valid), valid)
        for wrong in ({**valid, "max_cycles": True}, {**valid, "max_cycles": 0},
                      {**valid, "rework_window": 100}, {**valid, "scope": "all"}, {"enabled": True}):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError): validate_task_replanning_settings(wrong)

    def test_enabled_policy_requires_main_claude_lane_and_explicit_critical_model(self):
        f = config_fixtures.ConfigurationTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.value.update(coding_provider="claude", planning_provider="claude", claude={
            "model": "claude-sonnet-5-5", "reasoning_effort": "high", "critical_plan_model": "claude-opus-5-5",
            "critical_plan_reasoning_effort": "high"},
            task_replanning={"enabled": True, "max_cycles": 2, "rework_window": 2})
        self.assertTrue(load_config(f.save())["task_replanning"]["enabled"])
        f.value["claude"].pop("critical_plan_model")
        f.value["claude"].pop("critical_plan_reasoning_effort")
        with self.assertRaisesRegex(ValueError, "critical_plan_model"): load_config(f.save())
        f.value["task_replanning"]["enabled"] = False
        self.assertFalse(load_config(f.save())["task_replanning"]["enabled"])


if __name__ == "__main__":
    unittest.main()
