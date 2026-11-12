"""Budget interruption preserves real WIP and uses bounded critical planning."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_unattended_task_replanning as fixtures
from unattended.providers import ProviderError
from unattended.storage import read_json, write_json, write_text

HEAD, SESSION = fixtures.HEAD, fixtures.SESSION


class CodeTimeoutReplanningTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TaskReplanningTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.engine, self.config, self.providers = self.f.engine, self.f.config, self.f.providers
        self.work, self.folder = self.f.work, self.f.folder
        self.head = patch("unattended.engine.gitops.head", return_value=HEAD).start()
        self.clean = patch("unattended.engine.gitops.clean", return_value=False).start()
        self.git = patch("unattended.task_replanning.gitops.git", side_effect=self.git_output).start()
        write_text(self.work / "Source/change.cpp", "unfinished work\n")
        write_text(self.work / "new-test.txt", "new test evidence\n")
        state = self.f.state()
        state.update(phase="CODE", failures=2, operation=None, last_error="ProviderError: Provider exceeded its time limit")
        state["task"].update(reworks=4, code_base=HEAD, code_resume=True)
        self.engine._save(state)
        self.providers.code.side_effect = self.timeout

    @staticmethod
    def git_output(work, *args, **kwargs):
        if "--name-only" in args:
            return "Source/change.cpp\0"
        if args[0] == "ls-files":
            return "new-test.txt\0"
        return ""

    def receipt(self, out, **changes):
        record = {"kind": "code", "coding_provider": "claude", "timed_out": True,
                  "exit_code": 0, "output_limit_exceeded": False,
                  "requested_session": SESSION, "session_id": SESSION}
        record.update(changes)
        write_json(out / "process.json", record)
        write_text(out / "stdout.log", json.dumps({"type": "system", "subtype": "init",
                   "session_id": SESSION, "model": "claude-sonnet-5-5"}) + "\n")
        write_text(out / "stderr.log", "")
        write_text(self.work / "Saved/Logs" / out.name / "core-1.log", "Test still running when wall budget ended.\n")

    def timeout(self, prompt, work, out, timeout, **kwargs):
        self.receipt(out)
        raise ProviderError("Provider exceeded its time limit", interrupted=True, session_id=SESSION)

    def legacy(self, **record_changes):
        state = self.f.state()
        op = self.engine._new_operation(state, "code")
        op["window_limited"] = False
        self.receipt(Path(op["output_dir"]), **record_changes)
        state.update(phase="BLOCKED", blocked_from="CODE", failures=3,
                     last_error="ProviderError: Provider exceeded its time limit")
        self.engine._save(state)
        return op

    def test_dirty_budget_expiry_plans_then_continues_original_session_without_new_rework(self):
        self.assertEqual(self.engine.step()["phase"], "REPLAN")
        state = self.f.state()
        self.assertEqual(state["failures"], 0)
        self.assertEqual(state["task"]["code_timeout_recoveries"][0]["previous_failures"], 2)
        source = state["task"]["replanning"]["fingerprint"]
        self.assertEqual(source["review_round"], 3)
        self.assertEqual(source["review_head_sha"], HEAD)
        self.assertEqual(len(source["wip"]["files"]), 2)
        self.assertIn("core-1.log", " ".join(source["evidence_files"]))
        self.assertEqual(self.engine.step()["phase"], "CODE")
        task = self.f.state()["task"]
        self.assertEqual(task["reworks"], 4)
        self.assertTrue(task["code_resume"])
        self.assertEqual(task["check_results"], self.f.task["check_results"])
        self.assertNotIn("autonomous_rework_allowance", task)
        self.assertFalse((self.folder / "delivery-4.md").exists())
        self.assertEqual((self.work / "Source/change.cpp").read_text(), "unfinished work\n")
        self.assertEqual(self.providers.plan.call_args.kwargs["model"], "claude-opus-5-5")
        def code(*args, **kwargs):
            self.head.return_value = "c" * 40
            self.clean.return_value = True
            return {"completed": True, "session_id": SESSION, "text": "Finished."}
        self.providers.code.side_effect = code
        with patch("unattended.engine.gitops.verify_candidate", return_value={"head_sha": "c" * 40}):
            self.assertEqual(self.engine.step()["phase"], "CHECK")
        self.assertEqual(self.providers.code.call_args.kwargs["session_id"], SESSION)
        self.f.assert_original_task(self.f.state()["task"])

    def test_legacy_blocked_timeout_recovers_once_and_pause_is_respected(self):
        self.legacy()
        self.engine.pause()
        self.assertEqual(self.engine.step()["status"], "paused")
        self.engine.resume()
        self.assertEqual(self.f.restart().step()["phase"], "REPLAN")
        self.assertEqual(len(self.f.state()["task"]["code_timeout_recoveries"]), 1)
        self.assertEqual(self.engine.step()["phase"], "CODE")
        self.assertEqual(self.providers.plan.call_count, 1)
        self.assertEqual(self.providers.code.call_count, 0)

    def test_pause_arriving_during_native_call_defers_timeout_recovery(self):
        def paused_timeout(*args, **kwargs):
            self.engine.pause()
            return self.timeout(*args, **kwargs)
        self.providers.code.side_effect = paused_timeout
        self.engine.step()
        state = self.f.state()
        self.assertEqual(state["phase"], "BLOCKED")
        self.assertEqual(state["failures"], 3)
        self.assertNotIn("replanning", state["task"])
        self.assertNotIn("code_timeout_recoveries", state["task"])
        self.assertEqual(self.engine.step()["status"], "paused")
        self.engine.resume()
        self.assertEqual(self.engine.step()["phase"], "REPLAN")
        self.providers.plan.assert_not_called()

    def test_real_failure_foreign_session_and_window_cutoff_do_not_auto_replan(self):
        for mutation in ({"timed_out": False}, {"requested_session": "another-session"},
                         {"coding_provider": "dsh"}, {"output_limit_exceeded": True}):
            with self.subTest(mutation=mutation):
                self.legacy(**mutation)
                self.assertEqual(self.engine.step()["phase"], "BLOCKED")
        self.legacy()
        state = self.f.state()
        state["last_error"] = "RuntimeError: Code verification failed"
        self.engine._save(state)
        self.assertEqual(self.engine.step()["phase"], "BLOCKED")
        state["last_error"] = "ProviderError: Provider exceeded its time limit"
        state["operation"]["window_limited"] = True
        self.engine._save(state)
        self.assertEqual(self.engine.step()["phase"], "BLOCKED")
        self.providers.plan.assert_not_called()

    def test_legacy_failure_count_is_audited_and_new_planning_failure_has_its_own_budget(self):
        self.legacy()
        self.assertEqual(self.engine.step()["phase"], "REPLAN")
        state = self.f.state()
        self.assertEqual(state["failures"], 0)
        self.assertEqual(state["task"]["code_timeout_recoveries"][0]["previous_failures"], 3)
        self.providers.plan.side_effect = ProviderError("Planning transport failed")
        result = self.engine.step()
        self.assertEqual(result["phase"], "REPLAN")
        self.assertEqual(result["failures"], 1)
        self.assertIn("transport failed", result["last_error"])

    def test_explicit_retry_and_dirty_new_head_keep_prior_review_head_separate(self):
        self.legacy()
        self.head.return_value = "c" * 40
        self.assertEqual(self.engine.retry(replan=True)["phase"], "REPLAN")
        source = self.f.state()["task"]["replanning"]["fingerprint"]
        self.assertEqual(source["head_sha"], "c" * 40)
        self.assertEqual(source["review_head_sha"], HEAD)
        self.assertEqual(self.engine.step()["phase"], "CODE")
        self.assertEqual(self.f.state()["task"]["code_base"], HEAD)
        write_text(self.work / "Source/change.cpp", "more progress after the first plan")
        self.assertEqual(self.engine.step()["phase"], "REPLAN")
        source = self.f.state()["task"]["replanning"]["fingerprint"]
        self.assertEqual(source["timeout_operation"]["head_sha"], "c" * 40)
        self.assertEqual(source["timeout_operation"]["code_base"], HEAD)
        self.assertEqual(self.engine.step()["phase"], "CODE")
        self.assertEqual(self.f.state()["task"]["code_base"], HEAD)

    def test_foreign_operation_base_is_not_a_timeout_recovery_authority(self):
        self.legacy()
        state = self.f.state()
        state["operation"]["code_base"] = "f" * 40
        self.engine._save(state)
        self.assertEqual(self.engine.step()["phase"], "BLOCKED")
        self.providers.plan.assert_not_called()

    def test_initial_code_without_review_or_checks_can_plan_truthfully_and_keep_original_scope(self):
        state = self.f.state()
        state["task"].update(reworks=0, candidate=None, check_results=[])
        self.engine._save(state)
        self.assertEqual(self.engine.step()["phase"], "REPLAN")
        source = self.f.state()["task"]["replanning"]["fingerprint"]
        self.assertIsNone(source["review_round"])
        self.assertIsNone(source["review_sha256"])
        self.assertIsNone(source["delivery_sha256"])
        self.assertEqual(source["review_status"], "not_yet_created")
        self.assertEqual(self.engine.step()["phase"], "CODE")
        task = self.f.state()["task"]
        self.f.assert_original_task(task)
        self.assertEqual(task["reworks"], 0)
        self.assertEqual(task["check_results"], [])
        self.assertIsNone(task["candidate"])
        self.assertTrue(task["code_resume"])

    def test_escaping_wip_fails_closed(self):
        self.legacy()
        self.git.side_effect = lambda *args, **kwargs: "../outside.txt\0"
        self.assertEqual(self.engine.step()["phase"], "BLOCKED")
        self.assertEqual(self.f.state()["task"]["code_timeout_recoveries"][0]["decision"], "refused")
        self.providers.plan.assert_not_called()

    def test_exact_current_attempt_core_and_scoped_logs_are_bound(self):
        self.config["claude"]["test_preflight"] = {"task_id": fixtures.TASK,
            "commands": [["UE.exe", "-abslog={output_dir}/additional.log"]]}
        op = self.legacy()
        extra = self.work / "Saved/Logs" / Path(op["output_dir"]).name / "additional.log"
        write_text(extra, "Actual extra test result")
        self.engine.step()
        source = self.f.state()["task"]["replanning"]["fingerprint"]
        self.assertIn(str(extra), source["evidence_files"])
        write_text(extra, "Replaced result")
        self.assertIn("来源已改变", self.engine.step()["last_error"])
        self.providers.plan.assert_not_called()

    def test_code_timeout_configuration_is_optional_and_bounded(self):
        fixture = fixtures.config_fixtures.ConfigurationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.value["code_timeout_seconds"] = 7200
        self.assertEqual(fixtures.load_config(fixture.save())["code_timeout_seconds"], 7200)
        for invalid in (True, 0, 86401, "7200"):
            fixture.value["code_timeout_seconds"] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "code_timeout_seconds"):
                fixtures.load_config(fixture.save())

    def test_wip_or_attempt_log_change_rejects_stale_plan(self):
        self.engine.step()
        write_text(self.work / "new-test.txt", "changed by another actor")
        self.assertIn("来源已改变", self.engine.step()["last_error"])
        self.providers.plan.assert_not_called()

    def test_wip_change_during_planning_rejects_result(self):
        self.engine.step()
        def plan(*args, **kwargs):
            write_text(self.work / "Source/change.cpp", "modified during read-only planning")
            return self.f.plan
        self.providers.plan.side_effect = plan
        self.assertIn("证据被改变", self.engine.step()["last_error"])
        self.assertNotIn("replanning_history", self.f.state()["task"])

    def test_committed_timeout_uses_existing_code_reconciliation_without_a_plan(self):
        self.legacy()
        self.head.return_value = "c" * 40
        self.clean.return_value = True
        self.assertEqual(self.engine.step()["phase"], "CODE")
        with patch("unattended.engine.gitops.verify_candidate", return_value={"head_sha": "c" * 40}):
            self.assertEqual(self.engine.step()["phase"], "CHECK")
        self.providers.plan.assert_not_called()
        self.providers.code.assert_not_called()
        self.assertTrue(self.f.state()["task"]["candidate"]["recovered_from_git"])

    def test_cap_and_refused_source_are_durable_not_retried_on_restart(self):
        self.legacy()
        write_text(self.folder / "review-3.json", "invalid review receipt")
        self.assertEqual(self.engine.step()["phase"], "BLOCKED")
        first = self.f.state()
        self.assertEqual(first["task"]["code_timeout_recoveries"][0]["decision"], "refused")
        self.f.restart().step()
        self.assertEqual(self.f.state(), first)
        self.providers.plan.assert_not_called()

    def test_same_wip_cannot_repeat_plan_but_changed_wip_can_within_cycle_limit(self):
        self.engine.step()
        self.engine.step()
        self.assertEqual(self.engine.step()["phase"], "BLOCKED")
        self.assertIn("同一候选", self.f.state()["last_error"])
        self.assertEqual(self.providers.plan.call_count, 1)
        # A distinct, later operation with actual changed WIP is independent progress.
        state = self.f.state()
        state.update(phase="CODE", operation=None, last_error=None)
        self.engine._save(state)
        write_text(self.work / "Source/change.cpp", "new meaningful WIP")
        self.assertEqual(self.engine.step()["phase"], "REPLAN")
        self.assertEqual(self.engine.step()["phase"], "CODE")
        self.assertEqual(self.providers.plan.call_count, 2)
        self.assertEqual(self.engine.step()["phase"], "BLOCKED")
        count = len(self.f.state()["task"]["code_timeout_recoveries"])
        self.engine.step()
        self.assertEqual(len(self.f.state()["task"]["code_timeout_recoveries"]), count)

    def test_dirty_plan_quota_wait_and_completed_receipt_recovery_preserve_fingerprint(self):
        self.engine.step()
        self.providers.plan.side_effect = ProviderError("Quota", transient=True,
            retry_reason="claude_subscription_limit", retry_after_seconds=1800)
        self.assertEqual(self.engine.step()["status"], "waiting_provider")
        self.assertEqual(self.engine.step()["status"], "waiting_provider")
        state = self.f.state()
        state.update(phase="BLOCKED", blocked_from="REPLAN", failures=3)
        self.engine._save(state)
        self.providers.recover.return_value = deepcopy(self.f.plan)
        self.assertEqual(self.engine.retry()["phase"], "REPLAN")
        self.assertEqual(self.engine.step()["phase"], "CODE")
        self.assertEqual(self.providers.plan.call_count, 1)

    def test_new_operation_archives_old_error_without_clearing_failures_and_records_budget(self):
        self.config["code_timeout_seconds"] = 7200
        def failure(prompt, work, out, timeout, **kwargs):
            current = self.f.state()
            self.assertIsNone(current["last_error"])
            self.assertEqual(current["failures"], 2)
            self.assertEqual(current["operation"]["timeout_seconds"], 7200)
            self.assertEqual(current["operation"]["coding_model"], "claude-sonnet-5-5")
            raise ProviderError("A genuinely new failure")
        self.providers.code.side_effect = failure
        result = self.engine.step()
        self.assertEqual(result["phase"], "BLOCKED")
        self.assertIn("genuinely new", result["last_error"])
        self.assertEqual(len(self.f.state()["operation_error_history"]), 1)
        self.assertEqual(result["failures"], 3)


if __name__ == "__main__":
    unittest.main()
