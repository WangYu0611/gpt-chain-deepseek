"""Crash/restart integration tests with real local Git and no model/network."""
from __future__ import annotations

from datetime import datetime
import json
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from unattended.calendar import BEIJING
from unattended.engine import Engine
from unattended import gitops
from unattended.providers import ProviderError
from unattended.storage import read_json, write_json


PLAN = {"action": "task", "title": "Implement requested feature", "body": "Change feature.txt.",
        "acceptance_criteria": ["Feature changes are committed and the configured check passes."], "reason": "Next step"}


class FakeProviders:
    def __init__(self):
        self.plan_calls = 0
        self.code_calls = []
        self.review_calls = 0
        self.recover_calls = []
        self.recovered = {}
        self.code_callback = None
        self.review_decision = "accepted"

    def recover(self, kind, out):
        self.recover_calls.append((kind, out))
        return self.recovered.get((kind, str(out)))

    def plan(self, prompt, cwd, out, timeout):
        self.plan_calls += 1
        return dict(PLAN)

    def code(self, prompt, cwd, out, timeout, session_id=None):
        self.code_calls.append({"prompt": prompt, "cwd": cwd, "out": out,
                                "timeout": timeout, "session_id": session_id})
        if self.code_callback:
            return self.code_callback(prompt, cwd, out, timeout, session_id)
        self.commit(cwd, f"feature-{len(self.code_calls)}\n")
        return {"completed": True, "session_id": session_id or "session-recovery", "text": "Committed."}

    @staticmethod
    def commit(cwd, text):
        (cwd / "feature.txt").write_text(text, encoding="utf-8")
        gitops.git(cwd, "add", "feature.txt")
        gitops.git(cwd, "commit", "-m", "TASK-AUTO-0001")

    def review(self, prompt, cwd, out, timeout):
        self.review_calls += 1
        return {"decision": self.review_decision, "summary": "Checked actual commit", "feedback": "Fix it" if self.review_decision == "rework" else "",
                "head_sha": gitops.head(cwd)}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "source"
        self.project.mkdir()
        gitops.git(self.project, "init", "-b", "main")
        gitops.git(self.project, "config", "user.name", "Offline Test")
        gitops.git(self.project, "config", "user.email", "offline@example.invalid")
        (self.project / "feature.txt").write_text("original\n", encoding="utf-8")
        gitops.git(self.project, "add", "feature.txt")
        gitops.git(self.project, "commit", "-m", "Initial")
        self.calendar_path = self.root / "calendar.json"
        write_json(self.calendar_path, {"years": [2026], "holidays": [], "workdays": [], "overrides": {}})
        self.config = {
            "version": 1, "config_path": str(self.root / "config.json"),
            "project_path": str(self.project), "runtime_dir": str(self.root / "runtime"),
            "goal": "Implement the requested feature", "branch": "unattended/test",
            "base_ref": "HEAD", "task_prefix": "TASK-AUTO", "push": False, "remote": "origin",
            "schedule": {"holidays_file": str(self.calendar_path)}, "close_policy": "strict",
            "step_timeout_seconds": 3600, "max_reworks": 2, "max_failures": 3, "poll_seconds": 1,
            "checks": [{"id": "real-check", "argv": [sys.executable, "-c", "raise SystemExit(0)"], "timeout_seconds": 30}],
        }
        self.clock = [datetime(2026, 11, 2, 19, 0, tzinfo=BEIJING)]
        self.providers = FakeProviders()
        self.engine = self.restart()

    def restart(self):
        return Engine(self.config, self.providers, now=lambda: self.clock[0])

    @property
    def work(self):
        return Path(self.config["runtime_dir"]) / "workspace"

    def load(self):
        return read_json(Path(self.config["runtime_dir"]) / "state.json")

    def save(self, state):
        self.engine._save(state)

    def plan(self):
        result = self.engine.step()
        self.assertEqual(result["phase"], "CODE")
        return self.load()

    def partial_code(self, state, session="session-persisted"):
        operation = self.engine._new_operation(state, "code")
        directory = Path(operation["output_dir"])
        directory.mkdir(parents=True)
        (directory / "stdout.log").write_text(json.dumps({"type": "session", "sessionId": session}) + "\n", encoding="utf-8")
        return operation

    def test_restart_recovers_published_plan_without_second_planning_call(self):
        work = gitops.ensure_workspace(self.config)
        state = self.engine._load()
        state["expected_head"] = gitops.head(work)
        operation = self.engine._new_operation(state, "plan")
        self.providers.recovered[("plan", operation["output_dir"])] = dict(PLAN)
        result = self.restart().step()
        self.assertEqual(result["phase"], "CODE")
        self.assertEqual(self.providers.plan_calls, 0)
        self.assertTrue((Path(self.config["runtime_dir"]) / "tasks/TASK-AUTO-0001/TASK.md").is_file())

    def test_crash_before_commit_resumes_exact_session_and_preserves_uncommitted_work(self):
        state = self.plan()
        self.partial_code(state)
        (self.work / "feature.txt").write_text("half-finished\n", encoding="utf-8")

        def finish(prompt, cwd, out, timeout, session_id):
            self.assertEqual(session_id, "session-persisted")
            self.assertEqual((cwd / "feature.txt").read_text(), "half-finished\n")
            self.providers.commit(cwd, "finished without losing work\n")
            return {"completed": True, "session_id": session_id, "text": "Continued."}
        self.providers.code_callback = finish
        self.assertEqual(self.restart().step()["phase"], "CHECK")
        self.assertEqual(len(self.providers.code_calls), 1)
        commits = gitops.git(self.work, "rev-list", state["task"]["base_sha"] + "..HEAD").splitlines()
        self.assertEqual(len(commits), 1)

    def test_crash_after_commit_reconciles_git_without_any_second_coding_call(self):
        state = self.plan()
        self.partial_code(state)
        self.providers.commit(self.work, "committed before supervisor crash\n")
        candidate = gitops.head(self.work)
        result = self.restart().step()
        self.assertEqual(result["phase"], "CHECK")
        self.assertFalse(self.providers.code_calls)
        self.assertEqual(gitops.head(self.work), candidate)
        self.assertTrue(self.load()["task"]["candidate"]["recovered_from_git"])
        # The original user checkout remains untouched throughout recovery.
        self.assertEqual((self.project / "feature.txt").read_text(), "original\n")
        self.assertTrue(gitops.clean(self.project))

    def test_completed_provider_report_is_recovered_after_git_commit_before_state_flush(self):
        state = self.plan()
        operation = self.partial_code(state)
        self.providers.commit(self.work, "committed with a saved provider receipt\n")
        self.providers.recovered[("code", operation["output_dir"])] = {
            "completed": True, "session_id": "session-persisted", "text": "Original complete delivery report."}
        self.assertEqual(self.restart().step()["phase"], "CHECK")
        task = self.load()["task"]
        self.assertEqual(task["delivery_text"], "Original complete delivery report.")
        self.assertEqual(Path(task["delivery_path"]).read_text(), "Original complete delivery report.")
        self.assertFalse(self.providers.code_calls)

    def test_recovered_plan_is_rejected_if_candidate_changed_during_supervisor_gap(self):
        work = gitops.ensure_workspace(self.config)
        state = self.engine._load()
        state["expected_head"] = gitops.head(work)
        operation = self.engine._new_operation(state, "plan")
        self.providers.recovered[("plan", operation["output_dir"])] = dict(PLAN)
        self.providers.commit(work, "unexpected external edit\n")
        result = self.restart().step()
        self.assertEqual(result["phase"], "PLAN")
        self.assertEqual(result["failures"], 1)
        self.assertFalse(self.providers.recover_calls)
        self.assertFalse(self.providers.plan_calls)

    def test_strict_window_timeout_preserves_session_without_incrementing_failure(self):
        state = self.plan()
        self.clock[0] = datetime(2026, 11, 3, 7, 59, tzinfo=BEIJING)

        def interrupt(prompt, cwd, out, timeout, session_id):
            self.assertLessEqual(timeout, 60)
            self.clock[0] = datetime(2026, 11, 3, 8, 0, tzinfo=BEIJING)
            raise ProviderError("window ended", session_id="session-midtask", interrupted=True)
        self.providers.code_callback = interrupt
        result = self.restart().step()
        self.assertEqual(result["status"], "waiting_window")
        self.assertEqual(result["failures"], 0)
        state = self.load()
        self.assertEqual(state["task"]["session_id"], "session-midtask")
        self.assertTrue(state["task"]["code_resume"])
        self.assertEqual(self.restart().step()["status"], "waiting_window")
        self.assertEqual(len(self.providers.code_calls), 1)
        self.providers.code_callback = None
        self.clock[0] = datetime(2026, 11, 3, 18, 0, tzinfo=BEIJING)
        self.assertEqual(self.restart().step()["phase"], "CHECK")
        self.assertEqual(self.providers.code_calls[-1]["session_id"], "session-midtask")

    def test_window_closing_during_workspace_preflight_cannot_start_a_provider(self):
        self.plan()
        self.clock[0] = datetime(2026, 11, 3, 7, 59, tzinfo=BEIJING)

        def delayed_workspace(config):
            self.clock[0] = datetime(2026, 11, 3, 8, 0, tzinfo=BEIJING)
            return self.work
        with patch("unattended.engine.gitops.ensure_workspace", side_effect=delayed_workspace):
            result = self.restart().step()
        self.assertEqual(result["status"], "waiting_window")
        self.assertEqual(result["failures"], 0)
        self.assertFalse(self.providers.code_calls)

    def test_normal_step_timeout_recovers_session_from_raw_log(self):
        self.plan()

        def interrupt(prompt, cwd, out, timeout, session_id):
            out.mkdir(parents=True)
            (out / "stdout.log").write_text(json.dumps({"type": "session", "sessionId": "session-from-log"}) + "\n", encoding="utf-8")
            raise ProviderError("step timeout", interrupted=True)
        self.providers.code_callback = interrupt
        result = self.restart().step()
        self.assertEqual(result["failures"], 1)
        self.providers.code_callback = None
        self.assertEqual(self.restart().step()["phase"], "CHECK")
        self.assertEqual(self.providers.code_calls[-1]["session_id"], "session-from-log")

    def test_rework_retains_original_task_and_history_across_restart(self):
        self.plan()
        self.assertEqual(self.engine.step()["phase"], "CHECK")
        first_candidate = gitops.head(self.work)
        self.assertEqual(self.engine.step()["phase"], "REVIEW")
        self.providers.review_decision = "rework"
        self.assertEqual(self.engine.step()["phase"], "CODE")
        state = self.load()
        self.assertEqual(state["task"]["id"], "TASK-AUTO-0001")
        self.assertEqual(state["task"]["reworks"], 1)
        self.assertEqual(state["task"]["code_base"], first_candidate)
        self.assertIn("Fix it", state["task"]["feedback"])
        self.assertEqual(self.restart().step()["phase"], "CHECK")
        state = self.load()
        self.assertEqual(len(state["task"]["candidate"]["commits"]), 2)
        self.assertEqual(state["task"]["check_results"], [])
        self.assertEqual(self.providers.plan_calls, 1)
        self.assertEqual(self.providers.code_calls[-1]["session_id"], "session-recovery")

    def test_failed_check_cannot_be_accepted_even_when_reviewer_says_accepted(self):
        self.config["checks"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(1)"]
        self.plan()
        self.assertEqual(self.engine.step()["phase"], "CHECK")
        self.assertEqual(self.engine.step()["phase"], "REVIEW")
        self.assertEqual(self.engine.step()["phase"], "CODE")
        state = self.load()
        self.assertEqual(state["task"]["reworks"], 1)
        self.assertFalse(state["accepted"])

    def test_saved_check_receipt_is_applied_without_rerunning_the_command(self):
        self.plan()
        self.assertEqual(self.engine.step()["phase"], "CHECK")
        state = self.load()
        operation = self.engine._new_operation(state, "check")
        directory = Path(operation["output_dir"])
        directory.mkdir(parents=True)
        (directory / "stdout.log").write_text("verified\n", encoding="utf-8")
        (directory / "stderr.log").write_text("", encoding="utf-8")
        receipt = {"id": "real-check", "head_sha": gitops.head(self.work), "exit_code": 0,
                   "timed_out": False, "output_limit_exceeded": False, "passed": True,
                   "stdout_path": str(directory / "stdout.log"), "stderr_path": str(directory / "stderr.log")}
        write_json(directory / "check.json", receipt)
        with patch("unattended.engine.run_process", side_effect=AssertionError("must not rerun a receipted command")):
            self.assertEqual(self.restart().step()["phase"], "REVIEW")
        self.assertEqual(self.load()["task"]["check_results"], [receipt])

    def test_saved_review_receipt_advances_without_another_codex_review(self):
        self.plan()
        self.assertEqual(self.engine.step()["phase"], "CHECK")
        self.assertEqual(self.engine.step()["phase"], "REVIEW")
        state = self.load()
        operation = self.engine._new_operation(state, "review")
        self.providers.recovered[("review", operation["output_dir"])] = {
            "decision": "accepted", "summary": "Saved approval", "feedback": "", "head_sha": gitops.head(self.work)}
        self.assertEqual(self.restart().step()["phase"], "PUBLISH")
        self.assertEqual(self.providers.review_calls, 0)

    def _blocked_review_receipt(self, decision="accepted"):
        self.plan()
        self.assertEqual(self.engine.step()["phase"], "CHECK")
        self.assertEqual(self.engine.step()["phase"], "REVIEW")
        state = self.load()
        operation = self.engine._new_operation(state, "review")
        self.providers.recovered[("review", operation["output_dir"])] = {
            "decision": decision, "summary": "Saved review", "feedback": "Use the restored Clock" if decision == "rework" else "",
            "head_sha": gitops.head(self.work)}
        state.update(phase="BLOCKED", blocked_from="REVIEW", failures=3, last_error="Old parser rejected reconnect notice")
        self.save(state)
        return state, operation

    def test_retry_keeps_complete_accepted_review_without_second_model_request(self):
        state, operation = self._blocked_review_receipt()
        self.assertEqual(self.restart().retry()["phase"], "REVIEW")
        self.assertEqual(self.load()["operation"], operation)
        self.assertEqual(self.restart().step()["phase"], "PUBLISH")
        self.assertEqual(self.providers.review_calls, 0)
        self.assertEqual(self.load()["task"]["reworks"], state["task"]["reworks"])

    def test_retry_keeps_complete_rework_review_without_second_model_request(self):
        state, operation = self._blocked_review_receipt("rework")
        self.assertEqual(self.restart().retry()["phase"], "REVIEW")
        self.assertEqual(self.load()["operation"], operation)
        self.assertEqual(self.restart().step()["phase"], "CODE")
        task = self.load()["task"]
        self.assertIn("Use the restored Clock", task["feedback"])
        self.assertEqual(task["reworks"], state["task"]["reworks"] + 1)
        self.assertEqual(task["session_id"], state["task"]["session_id"])
        self.assertEqual(self.providers.review_calls, 0)

    def test_plain_retry_does_not_reclassify_actionable_blocked_feedback(self):
        before, operation = self._blocked_review_receipt("blocked")
        self.providers.recovered[("review", operation["output_dir"])]["feedback"] = (
            "Use the existing authorized UE bridge to capture evidence; no user approval is needed.")
        self.assertEqual(self.restart().retry()["phase"], "REVIEW")
        self.assertEqual(self.restart().step()["phase"], "BLOCKED")
        task = self.load()["task"]
        self.assertEqual(task["reworks"], before["task"]["reworks"])
        self.assertNotIn("manual_review_recoveries", task)
        self.assertNotIn("manual_rework_allowance", task)
        self.assertEqual(self.providers.review_calls, 0)

    def test_retry_keeps_complete_plan_without_second_model_request(self):
        work = gitops.ensure_workspace(self.config)
        state = self.engine._load()
        state["expected_head"] = gitops.head(work)
        operation = self.engine._new_operation(state, "plan")
        self.providers.recovered[("plan", operation["output_dir"])] = dict(PLAN)
        state.update(phase="BLOCKED", blocked_from="PLAN", failures=3)
        self.save(state)
        self.assertEqual(self.restart().retry()["phase"], "PLAN")
        self.assertEqual(self.load()["operation"], operation)
        self.assertEqual(self.restart().step()["phase"], "CODE")
        self.assertEqual(self.providers.plan_calls, 0)

    def test_retry_discards_missing_incomplete_or_invalid_receipts(self):
        baseline, operation = self._blocked_review_receipt()
        for failure in (None, ProviderError("Saved provider output is invalid")):
            with self.subTest(failure=failure):
                self.save(deepcopy(baseline))
                options = {"side_effect": failure} if failure else {"return_value": None}
                with patch.object(self.providers, "recover", **options):
                    self.assertEqual(self.restart().retry()["phase"], "REVIEW")
                self.assertIsNone(self.load()["operation"])
        self.assertEqual(self.providers.review_calls, 0)
        self.assertEqual(self.restart().step()["phase"], "PUBLISH")
        self.assertEqual(self.providers.review_calls, 1)

    def test_retry_does_not_reuse_foreign_task_head_kind_or_review_result(self):
        baseline, operation = self._blocked_review_receipt()
        for field, value in (("task_id", "TASK-OTHER"), ("head_sha", "0" * 40), ("kind", "plan")):
            with self.subTest(field=field):
                state = deepcopy(baseline)
                state["operation"][field] = value
                self.save(state)
                with patch.object(self.providers, "recover", side_effect=AssertionError("Must check binding first")):
                    self.restart().retry()
                self.assertIsNone(self.load()["operation"])
        self.save(deepcopy(baseline))
        self.providers.recovered[("review", operation["output_dir"])]["head_sha"] = "0" * 40
        self.restart().retry()
        self.assertIsNone(self.load()["operation"])
        self.assertEqual(self.providers.review_calls, 0)

    def test_retry_does_not_reuse_review_on_dirty_workspace_and_keeps_code_semantics(self):
        baseline, _ = self._blocked_review_receipt()
        (self.work / "feature.txt").write_text("unexpected external edit", encoding="utf-8")
        with patch.object(self.providers, "recover", side_effect=AssertionError("Dirty workspace must not recover")):
            self.restart().retry()
        self.assertIsNone(self.load()["operation"])
        state = deepcopy(baseline)
        state["blocked_from"], state["operation"]["kind"] = "CODE", "code"
        self.save(state)
        with patch.object(self.providers, "recover", side_effect=AssertionError("CODE retry unchanged")):
            self.assertEqual(self.restart().retry()["phase"], "CODE")
        self.assertIsNone(self.load()["operation"])
        self.assertTrue(self.load()["task"]["code_resume"])


if __name__ == "__main__":
    unittest.main()
