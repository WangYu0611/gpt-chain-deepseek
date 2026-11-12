"""Durable bounded-delay, unbounded-count provider recovery; real Git, fake APIs."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_unattended_recovery as recovery
from unattended import burst, gitops
from unattended.engine import Engine
from unattended.providers import ProviderError
from unattended.storage import read_json


def transient(reason="server_unavailable", *, session_id="session-retry", retry_after=None):
    error = ProviderError("Temporary provider failure", session_id=session_id)
    error.transient = True
    error.retry_reason = reason
    error.retry_after_seconds = retry_after
    return error


class TransientRetryTests(unittest.TestCase):
    def setUp(self):
        self.f = recovery.RecoveryTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.runtime = Path(self.f.config["runtime_dir"])
        # These fake provider attempts have no real protocol/process receipt.
        self.classifier = patch("unattended.providers.classify_transient_failure", return_value=None, create=True)
        self.classifier.start()
        self.addCleanup(self.classifier.stop)

    def begin(self):
        self.f.plan()
        self.f.providers.code_callback = lambda *args: (_ for _ in ()).throw(transient())
        self.assertEqual(self.f.engine.step()["status"], "waiting_provider")
        return self.f.load()

    def advance(self):
        self.f.clock[0] = datetime.fromisoformat(self.f.load()["provider_retry"]["next_retry_at"])

    def test_five_transient_attempts_preserve_session_dirty_tree_and_real_failure_budget_then_succeed(self):
        self.f.plan()
        state = self.f.load()
        state["failures"] = 1  # A real earlier failure must not be erased by a 503.
        self.f.save(state)
        def code(prompt, cwd, out, timeout, session_id):
            count = len(self.f.providers.code_calls)
            if count > 1:
                self.assertEqual(session_id, "session-retry")
                self.assertEqual((cwd / "feature.txt").read_text(), "unfinished preserved work")
            if count <= 5:
                out.mkdir(parents=True)
                (out / "stdout.log").write_text(json.dumps({"type": "session", "sessionId": "session-retry"}) + "\n", encoding="utf-8")
                (cwd / "feature.txt").write_text("unfinished preserved work", encoding="utf-8")
                raise transient("server_unavailable" if count % 2 else "rate_limited")
            self.f.providers.commit(cwd, "finished after provider recovered")
            return {"completed": True, "session_id": session_id, "text": "Committed"}
        self.f.providers.code_callback = code
        outputs = []
        for attempt, delay in enumerate([180, 300, 600, 600, 600], 1):
            started = self.f.clock[0]
            result = self.f.restart().step()
            self.assertEqual(result["status"], "waiting_provider")
            state = self.f.load()
            retry = state["provider_retry"]
            self.assertEqual(retry["attempt"], attempt)
            self.assertEqual(datetime.fromisoformat(retry["next_retry_at"]) - started, timedelta(seconds=delay))
            self.assertEqual((state["failures"], state["task"]["reworks"]), (1, 0))
            self.assertEqual(state["phase"], "CODE")
            outputs.append(state["operation"]["output_dir"])
            self.assertEqual(self.f.restart().step()["status"], "waiting_provider")
            self.assertEqual(len(self.f.providers.code_calls), attempt)
            self.assertEqual(self.f.load()["operation"]["output_dir"], outputs[-1])
            self.advance()
        self.assertEqual(len(set(outputs)), 5)
        self.assertTrue(all((Path(out) / "stdout.log").exists() for out in outputs))
        self.assertEqual(self.f.restart().step()["phase"], "CHECK")
        state = self.f.load()
        self.assertNotIn("provider_retry", state)
        self.assertEqual(state["failures"], 0)
        self.assertEqual(len(self.f.providers.code_calls), 6)

    def test_retry_after_is_bounded_and_stop_resume_cannot_skip_deadline(self):
        self.f.plan()
        hints = [(1, 180), (99999, 600), (240, 240), (True, 600)]
        for hint, expected_delay in hints:
            self.f.providers.code_callback = lambda *args, value=hint: (_ for _ in ()).throw(transient(retry_after=value))
            start = self.f.clock[0]
            self.assertEqual(self.f.restart().step()["status"], "waiting_provider")
            self.assertEqual(datetime.fromisoformat(self.f.load()["provider_retry"]["next_retry_at"]) - start,
                             timedelta(seconds=expected_delay))
            before = len(self.f.providers.code_calls)
            self.f.engine.pause()
            self.assertEqual(self.f.restart().step()["status"], "paused")
            self.f.engine.resume()
            self.assertEqual(self.f.restart().step()["status"], "waiting_provider")
            self.assertEqual(len(self.f.providers.code_calls), before)
            self.advance()

    def test_claude_pro_waits_for_reported_reset_across_calendar_and_restart(self):
        self.f.config["coding_provider"] = "claude"
        self.f.config["claude"] = {}
        self.f.config["schedule"]["always_on"] = True
        self.f.clock[0] = self.f.clock[0].replace(year=2027, hour=12)
        self.f.engine = self.f.restart()
        self.f.plan()
        session = "b274ea91-1874-4c36-8758-32593d610bdd"
        def code(prompt, cwd, out, timeout, session_id):
            if not session_id:
                out.mkdir(parents=True)
                (cwd / "feature.txt").write_text("unfinished Claude edit", encoding="utf-8")
                raise transient("claude_subscription_limit", session_id=session, retry_after=3600 * 30)
            self.assertEqual(session_id, session)
            self.assertEqual((cwd / "feature.txt").read_text(), "unfinished Claude edit")
            self.f.providers.commit(cwd, "finished after the Pro reset")
            return {"completed": True, "session_id": session, "text": "Committed"}
        self.f.providers.code_callback = code
        started = self.f.clock[0]
        self.assertEqual(self.f.restart().step()["status"], "waiting_provider")
        state = self.f.load()
        self.assertEqual(state["failures"], 0)
        self.assertEqual(state["task"]["coding_provider"], "claude")
        self.assertEqual(datetime.fromisoformat(state["provider_retry"]["next_retry_at"]) - started,
                         timedelta(hours=30))
        self.f.clock[0] += timedelta(hours=29)
        self.assertEqual(self.f.restart().step()["status"], "waiting_provider")
        self.assertEqual(len(self.f.providers.code_calls), 1)
        self.advance()
        self.assertEqual(self.f.restart().step()["phase"], "CHECK")
        self.assertEqual(len(self.f.providers.code_calls), 2)

    def test_calendar_wins_over_elapsed_backoff(self):
        self.f.clock[0] = self.f.clock[0].replace(day=3, hour=7, minute=59)
        self.begin()
        self.advance()  # 08:02, outside normal work hours.
        self.assertEqual(self.f.restart().step()["status"], "waiting_window")
        self.assertEqual(len(self.f.providers.code_calls), 1)
        self.assertIsNotNone(self.f.load()["provider_retry"])
        self.f.clock[0] = self.f.clock[0].replace(hour=18)
        self.assertEqual(self.f.restart().step()["status"], "waiting_provider")
        self.assertEqual(len(self.f.providers.code_calls), 2)

    def test_permanent_failure_still_blocks_at_three_and_clears_cooldown(self):
        self.begin()
        self.advance()
        self.f.providers.code_callback = lambda *args: (_ for _ in ()).throw(ProviderError("Permission denied"))
        for failures in (1, 2, 3):
            result = self.f.restart().step()
            self.assertEqual(result["failures"], failures)
            self.assertNotIn("provider_retry", self.f.load())
        self.assertEqual(result["phase"], "BLOCKED")

    def test_local_codex_update_retries_review_without_spending_real_failure_budget(self):
        self.f.plan()
        self.assertEqual(self.f.restart().step()["phase"], "CHECK")
        self.assertEqual(self.f.restart().step()["phase"], "REVIEW")
        state = self.f.load()
        state["failures"] = 2
        self.f.save(state)
        candidate = deepcopy(state["task"]["candidate"])
        session = state["task"]["session_id"]
        accepted = {"decision": "accepted", "summary": "Reviewed after local update", "feedback": "",
                    "head_sha": candidate["head_sha"]}
        errors = [transient("local_cli_unavailable", session_id=None) for _ in range(4)]
        with patch.object(self.f.providers, "review", side_effect=errors + [accepted]) as review:
            for attempt in range(1, 5):
                self.assertEqual(self.f.restart().step()["status"], "waiting_provider")
                state = self.f.load()
                self.assertEqual((state["failures"], state["task"]["reworks"]), (2, 0))
                self.assertEqual(state["task"]["candidate"], candidate)
                self.assertEqual(state["task"]["session_id"], session)
                self.assertEqual(state["provider_retry"]["reason"], "local_cli_unavailable")
                self.assertEqual(state["provider_retry"]["attempt"], attempt)
                self.assertEqual(self.f.restart().step()["status"], "waiting_provider")
                self.assertEqual(review.call_count, attempt)
                self.advance()
            self.assertEqual(self.f.restart().step()["phase"], "PUBLISH")
        self.assertEqual(self.f.load()["failures"], 0)
        self.assertNotIn("provider_retry", self.f.load())

    def test_codex_quota_wait_keeps_review_head_and_resumes_after_quota_recovers(self):
        self.f.plan()
        self.assertEqual(self.f.restart().step()["phase"], "CHECK")
        self.assertEqual(self.f.restart().step()["phase"], "REVIEW")
        before = self.f.load()
        candidate = deepcopy(before["task"]["candidate"])
        session = before["task"]["session_id"]
        accepted = {"decision": "accepted", "summary": "Quota recovered", "feedback": "",
                    "head_sha": candidate["head_sha"]}
        errors = [transient("codex_subscription_limit", session_id=None),
                  transient("codex_subscription_limit", session_id=None, retry_after=5 * 86400)]
        with patch.object(self.f.providers, "review", side_effect=errors + [accepted]) as review:
            for attempt, seconds in ((1, 1800), (2, 3600)):
                started = self.f.clock[0]
                self.assertEqual(self.f.restart().step()["status"], "waiting_provider")
                state = self.f.load()
                self.assertEqual(state["phase"], "REVIEW")
                self.assertEqual(state["failures"], 0)
                self.assertEqual(state["task"]["candidate"], candidate)
                self.assertEqual(state["task"]["session_id"], session)
                self.assertEqual(state["provider_retry"]["attempt"], attempt)
                self.assertEqual(state["provider_retry"]["reason"], "codex_subscription_limit")
                self.assertEqual(datetime.fromisoformat(state["provider_retry"]["next_retry_at"]) - started,
                                 timedelta(seconds=seconds))
                self.assertEqual(self.f.restart().step()["status"], "waiting_provider")
                self.assertEqual(review.call_count, attempt)
                self.advance()
            self.assertEqual(self.f.restart().step()["phase"], "PUBLISH")
        self.assertNotIn("provider_retry", self.f.load())

    def test_codex_quota_reason_cannot_authorize_a_non_codex_coding_wait(self):
        self.f.plan()
        self.f.providers.code_callback = lambda *args: (_ for _ in ()).throw(
            transient("codex_subscription_limit"))
        result = self.f.restart().step()
        self.assertEqual(result["failures"], 1)
        self.assertNotEqual(result["status"], "waiting_provider")
        self.assertNotIn("provider_retry", self.f.load())

    def test_retry_checkpoint_wrong_task_phase_head_or_operation_is_rejected_before_calls(self):
        baseline = self.begin()
        for field, value in [("task_id", "TASK-OTHER"), ("phase", "REVIEW"),
                             ("head_sha", "0" * 40), ("operation_output_dir", "unrelated"),
                             ("next_retry_at", "not-a-date"), ("reason", "temporary_provider_failure")]:
            state = deepcopy(baseline)
            state["provider_retry"][field] = value
            self.f.save(state)
            with self.subTest(field=field):
                result = self.f.restart().step()
                self.assertEqual(result["failures"], 1)
                self.assertEqual(len(self.f.providers.code_calls), 1)
                self.assertNotIn("provider_retry", self.f.load())

    def test_transient_flag_without_recognized_cause_does_not_authorize_unbounded_retry(self):
        self.f.plan()
        for cause in (None, "unknown"):
            self.f.providers.code_callback = lambda *args, reason=cause: (_ for _ in ()).throw(transient(reason))
            result = self.f.restart().step()
            self.assertNotEqual(result["status"], "waiting_provider")
            self.assertNotIn("provider_retry", self.f.load())
        self.assertEqual(result["failures"], 2)

    def test_saved_failed_process_after_crash_is_classified_before_any_new_request(self):
        state = self.f.plan()
        operation = self.f.partial_code(state, session="session-saved-transient")
        with patch("unattended.providers.classify_transient_failure", return_value={"reason": "transport", "retry_after_seconds": 300}, create=True):
            result = self.f.restart().step()
        self.assertEqual(result["status"], "waiting_provider")
        self.assertEqual(result["failures"], 0)
        self.assertEqual(self.f.load()["task"]["session_id"], "session-saved-transient")
        self.assertEqual(self.f.load()["operation"], operation)
        self.assertFalse(self.f.providers.code_calls)

    def test_crash_during_retry_attempt_reclassifies_new_failure_and_keeps_attempt_counter(self):
        self.begin()
        self.advance()
        class Crash(BaseException):
            pass
        def fail_after_launch(prompt, cwd, out, timeout, session_id):
            persisted = self.f.load()
            self.assertIsNone(persisted["last_error"])
            self.assertEqual(persisted["provider_retry"]["status"], "retrying")
            raise Crash("Supervisor stopped after the new process receipt")
        self.f.providers.code_callback = fail_after_launch
        with self.assertRaises(Crash):
            self.f.restart().step()
        state = self.f.load()
        self.assertEqual(state["provider_retry"]["status"], "retrying")
        self.assertEqual(state["provider_retry"]["operation_output_dir"], state["operation"]["output_dir"])
        with patch("unattended.providers.classify_transient_failure", return_value={"reason": "server_unavailable"}, create=True):
            self.assertEqual(self.f.restart().step()["status"], "waiting_provider")
        self.assertEqual(self.f.load()["provider_retry"]["attempt"], 2)
        self.assertEqual(len(self.f.providers.code_calls), 2)

    def test_burst_waits_then_completes_original_target_and_restores_schedule(self):
        self.f.plan()
        original = deepcopy(self.f.config)
        def code(prompt, cwd, out, timeout, session_id):
            if len(self.f.providers.code_calls) <= 4:
                raise transient()
            self.f.providers.commit(cwd, "burst work finished")
            return {"completed": True, "session_id": session_id, "text": "Committed"}
        self.f.providers.code_callback = code
        slept = []
        def sleep(seconds):
            slept.append(seconds)
            self.advance()
        with patch("unattended.burst.time.sleep", side_effect=sleep):
            result = burst._execute_burst(self.f.config, 1, True, self.f.providers, now=lambda: self.f.clock[0])
        self.assertEqual(result["status"], "completed")
        self.assertEqual((result["start_accepted_count"], result["target_accepted_count"], result["completed_tasks"]), (0, 1, 1))
        self.assertEqual(len(slept), 4)
        self.assertTrue(all(0 < seconds <= 60 for seconds in slept))
        self.assertEqual(self.f.config, original)
        self.assertEqual(self.f.load()["phase"], "PLAN")
        self.assertNotIn("provider_retry", self.f.load())

    def test_normal_run_sleeps_during_cooldown_instead_of_busy_looping(self):
        self.begin()
        class StopObservedLoop(BaseException):
            pass
        with patch("unattended.engine.time.sleep", side_effect=StopObservedLoop) as sleep:
            with self.assertRaises(StopObservedLoop):
                self.f.restart().run()
        sleep.assert_called_once()
        self.assertEqual(len(self.f.providers.code_calls), 1)
        self.assertEqual(read_json(self.runtime / "heartbeat.json")["status"], "waiting_provider")

    def test_planning_and_review_retries_keep_distinct_phase_and_task_bindings(self):
        with patch.object(self.f.providers, "plan", side_effect=[transient("transport", session_id=None), dict(recovery.PLAN)]):
            self.assertEqual(self.f.restart().step()["status"], "waiting_provider")
            retry = self.f.load()["provider_retry"]
            self.assertEqual(retry["phase"], "PLAN")
            self.assertIsNone(retry["task_id"])
            self.advance()
            self.assertEqual(self.f.restart().step()["phase"], "CODE")
        self.assertNotIn("provider_retry", self.f.load())

    def test_milestone_review_transient_wait_resumes_same_candidate_without_repeating_checks(self):
        self.f.config["publication"] = {"mode": "milestone", "milestone_id": "M2", "goal": "Verify the complete milestone"}
        self.f.engine = self.f.restart()
        self.f.plan()
        for phase in ["CHECK", "REVIEW", "PUBLISH", "PLAN"]:
            self.assertEqual(self.f.restart().step()["phase"], phase)
        with patch.object(self.f.providers, "plan", return_value={"action": "complete", "reason": "All required work done"}):
            self.assertEqual(self.f.restart().step()["phase"], "MILESTONE_REVIEW")
        self.assertEqual(self.f.restart().step()["phase"], "MILESTONE_REVIEW")
        baseline = self.f.load()
        verdict = {"decision": "accepted", "summary": "Full milestone verified", "feedback": "", "head_sha": gitops.head(self.f.work)}
        with patch.object(self.f.providers, "review", side_effect=[transient("transport", session_id=None), verdict]) as review:
            self.assertEqual(self.f.restart().step()["status"], "waiting_provider")
            retry = self.f.load()["provider_retry"]
            self.assertEqual(retry["phase"], "MILESTONE_REVIEW")
            self.assertIsNone(retry["task_id"])
            self.assertEqual(self.f.restart().step()["status"], "waiting_provider")
            self.assertEqual(review.call_count, 1)
            self.advance()
            self.assertEqual(self.f.restart().step()["phase"], "MILESTONE_PUBLISH")
        state = self.f.load()
        self.assertNotIn("provider_retry", state)
        self.assertEqual(state["milestone"]["head_sha"], baseline["milestone"]["head_sha"])
        self.assertEqual(state["milestone"]["check_results"], baseline["milestone"]["check_results"])
        self.assertEqual(state["accepted"], baseline["accepted"])

    def test_task_review_retries_same_candidate_with_task_bound_cooldown(self):
        self.f.plan()
        self.assertEqual(self.f.restart().step()["phase"], "CHECK")
        self.assertEqual(self.f.restart().step()["phase"], "REVIEW")
        verdict = {"decision": "accepted", "summary": "Verified", "feedback": "", "head_sha": gitops.head(self.f.work)}
        with patch.object(self.f.providers, "review", side_effect=[transient("workspace_routing_network", session_id=None), verdict]) as review:
            self.assertEqual(self.f.restart().step()["status"], "waiting_provider")
            retry = self.f.load()["provider_retry"]
            self.assertEqual(retry["phase"], "REVIEW")
            self.assertEqual(retry["task_id"], "TASK-AUTO-0001")
            self.assertEqual(retry["attempt"], 1)
            self.assertEqual(self.f.restart().step()["status"], "waiting_provider")
            self.assertEqual(review.call_count, 1)
            self.advance()
            self.assertEqual(self.f.restart().step()["phase"], "PUBLISH")
        self.assertNotIn("provider_retry", self.f.load())


if __name__ == "__main__":
    unittest.main()
