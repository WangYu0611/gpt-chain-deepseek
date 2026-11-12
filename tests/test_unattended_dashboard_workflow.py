"""Workflow cards distinguish recorded work, configured roles, and waiting states."""
import hashlib
import json
import unittest

from tests import test_unattended_dashboard_state as fixture
from unattended import dashboard_state as dashboard
from unattended.config import load_config


class DashboardWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.DashboardStateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config, self.state = self.fixture.config, self.fixture.state
        self.config.update(planning_provider="claude", coding_provider="claude", claude={
            "model": "claude-sonnet-5-5", "reasoning_effort": "high", "critical_plan_model": "claude-opus-5-5",
        })
        self.save_config()

    def save_config(self):
        self.fixture.write(self.fixture.config_path, self.config)
        config = load_config(self.fixture.config_path)
        keys = ("project_path", "goal", "branch", "base_ref", "checks", "push", "remote", "task_prefix")
        values = {key: config.get(key) for key in keys}
        if "publication" in config:
            values["publication"] = config["publication"]
        self.state["binding"] = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def task(self, level="routine"):
        self.state["task"] = {"id": "TASK-1", "title": "The current feature", "coding_provider": "claude", "reworks": 0,
                              "planning_level": level, "planning_model": "claude-sonnet-5-5" if level == "routine" else "claude-opus-5-5",
                              "planning_reasoning_effort": "high", "planning_reason": "A scoped local change.",
                              "check_results": []}
        return self.state["task"]

    @staticmethod
    def modules(snapshot):
        return {item["id"]: item for item in snapshot["modules"]}

    @staticmethod
    def nodes(snapshot):
        return {item["id"]: item for item in snapshot["workflow"]["nodes"]}

    def critical(self):
        source = self.fixture.runtime / "runs" / "sonnet-draft"
        self.fixture.write(source / "planning-decision.json", {
            "planning_model": "claude-sonnet-5-5", "planning_reasoning_effort": "high",
        })
        self.state["planning_selection"] = {"planning_model": "claude-opus-5-5", "planning_reasoning_effort": "high",
                                             "planning_level": "critical", "source_output_dir": str(source),
                                             "critical_candidate": {"planning_reason": "The proposed change crosses storage boundaries."}}
        return source

    def test_opus_planning_is_separate_from_sonnet_development_and_grounded_in_handoff(self):
        self.critical()
        self.fixture.logs([{"type": "system", "subtype": "init"}])
        self.state["operation"].update(planning_provider="claude", planning_model="claude-opus-5-5",
                                         planning_reasoning_effort="high", planning_level="critical")
        snapshot = self.fixture.snapshot()
        modules = self.modules(snapshot)
        self.assertEqual(snapshot["workflow"]["current"], "critical_plan")
        self.assertEqual(modules["critical_planner"]["state"], "running")
        self.assertEqual(modules["critical_planner"]["model"], "claude-opus-5-5")
        self.assertEqual(modules["routine_planner"]["state"], "completed")
        self.assertEqual(modules["routine_planner"]["model"], "claude-sonnet-5-5")
        self.assertEqual(modules["developer"]["model"], "claude-sonnet-5-5")
        self.assertEqual(modules["developer"]["state"], "idle")
        self.assertTrue(any(edge["active"] and edge["from"] == "routine_plan" and edge["to"] == "critical_plan"
                            for edge in snapshot["workflow"]["edges"]))

    def test_routine_task_marks_opus_not_required_and_does_not_claim_execution_without_operation(self):
        self.task()
        self.state["phase"] = "CODE"
        snapshot = self.fixture.snapshot()
        modules = self.modules(snapshot)
        self.assertEqual(snapshot["workflow"]["route"], "routine")
        self.assertEqual(modules["critical_planner"]["state"], "not_required")
        self.assertEqual(modules["developer"]["state"], "ready")
        self.assertTrue(modules["developer"]["current"])
        self.assertEqual(snapshot["task"]["planning"]["level"], "routine")

    def test_rework_highlights_the_review_to_development_loop(self):
        task = self.task()
        task.update(reworks=2, feedback="Correct the failed behavior and rerun its check.")
        self.state["phase"] = "CODE"
        snapshot = self.fixture.snapshot()
        self.assertEqual(snapshot["workflow"]["rework"]["count"], 2)
        self.assertTrue(snapshot["workflow"]["rework"]["active"])
        active = [edge for edge in snapshot["workflow"]["edges"] if edge["active"]]
        self.assertEqual([(edge["from"], edge["to"], edge["kind"]) for edge in active], [("review", "code", "rework")])
        self.assertIn("2", self.modules(snapshot)["developer"]["summary"])

    def test_paused_stale_and_quota_wait_are_distinct_from_running(self):
        self.critical()
        self.fixture.logs([{"type": "system", "subtype": "init"}])
        self.state["operation"].update(planning_provider="claude", planning_model="claude-opus-5-5", planning_level="critical")
        pause = self.fixture.runtime / "PAUSE"
        pause.write_text("pause", encoding="utf-8")
        self.assertEqual(self.modules(self.fixture.snapshot())["critical_planner"]["state"], "paused")
        pause.unlink()
        self.fixture.health["heartbeat_epoch"] -= 60
        self.fixture.write(self.fixture.runtime / "background-health.json", self.fixture.health)
        self.assertEqual(self.modules(self.fixture.snapshot())["critical_planner"]["state"], "stale")
        self.fixture.health["heartbeat_epoch"] = self.fixture.now.timestamp()
        self.fixture.write(self.fixture.runtime / "background-health.json", self.fixture.health)
        record = self.fixture.retry_record("PLAN")
        record["reason"] = "claude_subscription_limit"
        waiting = self.fixture.snapshot()
        self.assertEqual(self.modules(waiting)["critical_planner"]["state"], "waiting")
        self.assertFalse(any(module["state"] == "running" for module in waiting["modules"]))

    def test_milestone_checks_and_final_review_are_distinct_stages(self):
        self.config["publication"] = {"mode": "milestone", "milestone_id": "M2", "goal": "A complete milestone"}
        self.save_config()
        self.state.update(phase="MILESTONE_REVIEW", milestone={"proposal": {"action": "complete", "planning_level": "critical"}, "check_results": []})
        self.fixture.logs([], kind="milestone_check")
        checking = self.fixture.snapshot()
        self.assertEqual(checking["workflow"]["current"], "check")
        self.assertEqual(self.modules(checking)["checks"]["state"], "running")
        self.state["operation"] = None
        self.state["milestone"]["check_results"] = [{"id": "ue-behavior", "passed": True, "exit_code": 0}]
        reviewing = self.fixture.snapshot()
        self.assertEqual(reviewing["workflow"]["current"], "milestone_review")
        self.assertEqual(self.modules(reviewing)["checks"]["state"], "completed")
        self.assertEqual(self.modules(reviewing)["reviewer"]["state"], "ready")
        self.state["phase"] = "MILESTONE_PUBLISH"
        publishing = self.fixture.snapshot()
        self.assertEqual(publishing["workflow"]["current"], "milestone_publish")
        self.assertEqual(self.nodes(publishing)["milestone_review"]["state"], "completed")

    def test_old_receipts_keep_unknown_model_instead_of_current_config(self):
        self.fixture.logs([{"type": "system", "subtype": "init", "model": "PRIVATE_MODEL"}])
        snapshot = self.fixture.snapshot()
        routine = self.modules(snapshot)["routine_planner"]
        self.assertIsNone(routine["model"])
        self.assertEqual(routine["model_source"], "unknown")
        self.assertEqual(routine["configured_model"], "claude-sonnet-5-5")
        self.assertNotIn("PRIVATE_MODEL", json.dumps(snapshot))

    def test_native_claude_code_model_requires_matching_owned_session(self):
        session = "18d2529d-649c-4ac1-9d45-3c9377291694"
        task = self.task()
        task["session_id"] = session
        self.state["phase"] = "CODE"
        directory = self.fixture.logs([{"type": "system", "subtype": "init", "session_id": session,
                                       "model": "claude-sonnet-5-5"}], kind="code")
        self.state["operation"].update(task_id="TASK-1", coding_provider="claude")
        developer = self.modules(self.fixture.snapshot())["developer"]
        self.assertEqual(developer["model"], "claude-sonnet-5-5")
        self.assertEqual(developer["model_source"], "recorded")
        (directory / "stdout.log").write_text(json.dumps({"type": "system", "subtype": "init",
            "session_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "model": "claude-opus-5-5"}) + "\n", encoding="utf-8")
        self.assertIsNone(self.modules(self.fixture.snapshot())["developer"]["model"])

    def test_historical_registration_does_not_complete_current_task_publication(self):
        self.task()
        self.state["phase"] = "CODE"
        self.state["accepted"] = [{"id": "TASK-OLD", "title": "An earlier task", "head_sha": "a" * 40}]
        snapshot = self.fixture.snapshot()
        self.assertEqual(self.nodes(snapshot)["publish"]["state"], "idle")
        self.assertEqual(self.modules(snapshot)["publisher"]["state"], "idle")

    def test_finished_failed_check_is_blocked_instead_of_running(self):
        task = self.task()
        self.state["phase"] = "CHECK"
        self.fixture.logs([], kind="check")
        self.state["operation"]["task_id"] = "TASK-1"
        task["check_results"] = [{"id": "ue-behavior", "passed": False, "exit_code": 1}]
        snapshot = self.fixture.snapshot()
        checks = self.modules(snapshot)["checks"]
        self.assertEqual(checks["state"], "blocked")
        self.assertIn("未通过", checks["summary"])

    def test_rework_keeps_previous_check_results_separate_from_current_attempt(self):
        task = self.task()
        task.update(reworks=1, candidate={"head_sha": "a" * 40})
        self.state["phase"] = "CODE"
        for passed in (True, False):
            with self.subTest(previous_check_passed=passed):
                task["check_results"] = [{"id": "ue-behavior", "passed": passed, "exit_code": 0 if passed else 1}]
                snapshot = self.fixture.snapshot()
                checks = self.modules(snapshot)["checks"]
                self.assertEqual(snapshot["check_progress"]["evidence_scope"], "previous_attempt")
                self.assertEqual(self.nodes(snapshot)["check"]["state"], "idle")
                self.assertEqual(checks["state"], "idle")
                self.assertIn("上一轮", checks["summary"])
                self.assertIn("新提交", checks["summary"])

    def test_new_projection_is_bounded_filters_secrets_and_is_read_only(self):
        task = self.task("critical")
        task.update(planning_reason="<thinking>PRIVATE_REASONING</thinking> token=secret " + "x" * 2000,
                    planning_source_output_dir=str(self.fixture.root / "outside"))
        self.state["phase"] = "CODE"
        self.fixture.write(self.fixture.runtime / "state.json", self.state)
        before = {path: path.read_bytes() for path in self.fixture.root.rglob("*") if path.is_file()}
        snapshot = dashboard.collect_snapshot(self.fixture.config_path)
        after = {path: path.read_bytes() for path in self.fixture.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertLessEqual(len(snapshot["workflow"]["route_reason"]), 601)
        self.assertNotIn("PRIVATE_REASONING", snapshot["workflow"]["route_reason"])
        self.assertNotIn("secret", snapshot["task"]["planning"]["reason"])
        self.assertIsNone(snapshot["task"]["planning"]["source"])
        self.assertIsNone(self.modules(snapshot)["routine_planner"]["model"])

    def test_safe_defaults_before_state_exists_and_on_bad_configuration(self):
        snapshot = dashboard.collect_snapshot(self.fixture.config_path)
        self.assertEqual(len(snapshot["modules"]), 6)
        self.assertIsNone(snapshot["workflow"]["current"])
        self.assertFalse(any(module["state"] == "running" for module in snapshot["modules"]))
        self.fixture.config_path.write_text("invalid", encoding="utf-8")
        broken = dashboard.collect_snapshot(self.fixture.config_path)
        self.assertEqual(broken["status"]["code"], "error")
        self.assertEqual(broken["modules"], [])
        self.assertIsNone(broken["workflow"]["current"])


if __name__ == "__main__":
    unittest.main()
