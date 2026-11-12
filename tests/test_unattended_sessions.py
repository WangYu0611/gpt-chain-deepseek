"""Task-owned DSH sessions and explicit one-round review recovery; no models."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_unattended_recovery as recovery
from tests.test_unattended_burst import _SerialProviders
from unattended import cli, gitops
from unattended.storage import read_json, write_json


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.f = recovery.RecoveryTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.runtime = Path(self.f.config["runtime_dir"])

    def blocked_review(self, *, decision="rework", at_limit=True):
        self.f.plan()
        self.f.engine.step()
        self.f.engine.step()
        state = self.f.load()
        state["task"]["reworks"] = self.f.config["max_reworks"] if at_limit else 0
        state["task"]["feedback"] = "stale previous feedback"
        self.f.save(state)
        self.f.providers.review_decision = decision
        self.assertEqual(self.f.engine.step()["phase"], "BLOCKED")
        state = self.f.load()
        path = self.runtime / "tasks" / state["task"]["id"] / f"review-{state['task']['reworks']}.json"
        review = read_json(path)
        review["feedback"] = "Latest roof occlusion: change camera only"
        write_json(path, review)
        return state, path

    def test_two_tasks_start_fresh_and_same_task_rework_resumes(self):
        self.f.providers = _SerialProviders(self.runtime)
        self.f.engine = self.f.restart()
        self.f.plan()
        self.f.engine.step()
        first = self.f.load()["task"]["session_id"]
        self.f.engine.step()
        self.f.providers.review_decision = "rework"
        self.f.engine.step()
        state = self.f.load()
        self.assertEqual(state["task"]["session_id"], first)
        result = {"completed": True, "session_id": first, "text": "done"}
        with patch.object(self.f.providers, "code", return_value=result) as resumed:
            self.f.engine._invoke(state, "code", "repair", self.f.work)
        self.assertEqual(resumed.call_args.kwargs["session_id"], first)
        # Publish the first candidate, then plan a genuinely different task.
        state["phase"], state["operation"] = "PUBLISH", None
        self.f.save(state)
        self.f.engine.step()
        self.f.plan()
        self.f.engine.step()
        second = self.f.load()["task"]["session_id"]
        self.assertNotEqual(first, second)
        self.assertEqual([c["session_id"] for c in self.f.providers.code_calls], [None, None])
        self.assertEqual(read_json(self.runtime / "dsh-sessions.json"),
                         {first: "TASK-AUTO-0001", second: "TASK-AUTO-0002"})

    def test_legacy_initial_operation_without_task_field_resumes_from_log(self):
        state = self.f.plan()
        operation = self.f.partial_code(state)
        operation.pop("task_id")
        self.f.save(state)
        self.assertEqual(self.f.restart().step()["phase"], "CHECK")
        self.assertEqual(self.f.providers.code_calls[0]["session_id"], "session-persisted")
        self.assertEqual(self.f.load()["task"]["session_owner_task_id"], "TASK-AUTO-0001")

    def test_foreign_operation_rejected_before_recovery_or_code(self):
        state = self.f.plan()
        op = self.f.partial_code(state)
        op["task_id"] = "TASK-AUTO-9999"
        self.f.save(state)
        result = self.f.restart().step()
        self.assertIn("another task", result["last_error"])
        self.assertFalse(self.f.providers.code_calls)
        self.assertFalse(self.f.providers.recover_calls)

    def test_legacy_completed_task_session_cannot_be_adopted_by_current_task(self):
        state = self.f.plan()
        state["task"]["session_id"] = "session-old"
        write_json(self.runtime / "tasks/TASK-OLD/completed-task.json",
                   {"id": "TASK-OLD", "session_id": "session-old"})
        self.f.save(state)
        result = self.f.restart().step()
        self.assertIn("another task", result["last_error"])
        self.assertFalse(self.f.providers.code_calls)

    def test_new_session_response_cannot_reuse_any_registered_identity(self):
        state = self.f.plan()
        write_json(self.runtime / "dsh-sessions.json", {"session-old": state["task"]["id"]})
        with patch.object(self.f.providers, "code", return_value={"session_id": "session-old"}):
            with self.assertRaisesRegex(RuntimeError, "previously used"):
                self.f.engine._invoke(state, "code", "new task", self.f.work)

    def test_manual_rework_uses_latest_feedback_resets_session_once_and_keeps_cap(self):
        before, _ = self.blocked_review()
        old = before["task"]["session_id"]
        config = deepcopy(self.f.config)
        result = self.f.restart().retry(rework=True, new_session=True)
        self.assertEqual(result["phase"], "CODE")
        state = self.f.load()
        task = state["task"]
        self.assertEqual(task["manual_rework_allowance"], 1)
        self.assertEqual(task["manual_review_recoveries"][0]["original_decision"], "rework")
        self.assertEqual(task["reworks"], self.f.config["max_reworks"] + 1)
        self.assertIn("Latest roof occlusion", task["feedback"])
        self.assertNotIn("stale previous", task["feedback"])
        self.assertIsNone(task["session_id"])
        self.assertNotIn("session_owner_task_id", task)
        self.assertEqual(task["session_history"][0]["session_id"], old)
        self.assertEqual(task["code_base"], gitops.head(self.f.work))
        self.assertEqual(task["check_results"], [])
        self.assertIsNone(state["operation"])
        self.assertEqual(self.f.config, config)
        with self.assertRaisesRegex(ValueError, "BLOCKED"):
            self.f.restart().retry(rework=True, new_session=True)
        self.assertEqual(self.f.load()["task"]["manual_rework_allowance"], 1)
        with patch.object(self.f.providers, "code", return_value={"completed": True, "session_id": "session-fresh", "text": "done"}) as code:
            self.f.engine._invoke(state, "code", "fix", self.f.work)
        self.assertIsNone(code.call_args.kwargs["session_id"])
        # The explicit round has consumed the one extra allowance. A further
        # failing review must stop, not quietly grant another round.
        state["phase"], state["operation"] = "REVIEW", None
        self.f.engine._review(state, self.f.work)
        self.assertEqual(state["phase"], "BLOCKED")
        self.assertEqual(task["manual_rework_allowance"], 1)

    def test_explicit_blocked_rework_uses_existing_budget_preserves_receipt_and_waits_for_window(self):
        before, path = self.blocked_review(decision="blocked", at_limit=False)
        original_receipt = path.read_bytes()
        calls_before = len(self.f.providers.code_calls)
        self.f.clock[0] = self.f.clock[0].replace(hour=10)
        self.assertEqual(self.f.restart().retry(rework=True)["phase"], "CODE")
        task = self.f.load()["task"]
        self.assertEqual(task["reworks"], 1)
        self.assertNotIn("manual_rework_allowance", task)
        self.assertEqual(task["session_id"], before["task"]["session_id"])
        self.assertIn("Latest roof occlusion", task["feedback"])
        self.assertEqual(task["check_results"], [])
        self.assertEqual(path.read_bytes(), original_receipt)
        self.assertEqual(task["manual_review_recoveries"], [{
            "original_decision": "blocked", "review_round": 0,
            "head_sha": before["task"]["candidate"]["head_sha"],
            "recovered_at": self.f.clock[0].isoformat(), "new_session": False,
        }])
        with self.assertRaises(ValueError):
            self.f.restart().retry(rework=True)
        self.assertEqual(len(self.f.load()["task"]["manual_review_recoveries"]), 1)
        self.assertEqual(self.f.restart().step()["status"], "waiting_window")
        self.assertEqual(len(self.f.providers.code_calls), calls_before)

    def test_blocked_rework_at_cap_adds_only_one_round_and_can_reset_session(self):
        before, path = self.blocked_review(decision="blocked")
        original_receipt = path.read_bytes()
        self.f.restart().retry(rework=True, new_session=True)
        task = self.f.load()["task"]
        self.assertEqual(task["manual_rework_allowance"], 1)
        self.assertEqual(task["reworks"], self.f.config["max_reworks"] + 1)
        self.assertIsNone(task["session_id"])
        self.assertEqual(task["session_history"][0]["session_id"], before["task"]["session_id"])
        self.assertEqual(task["manual_review_recoveries"][0]["original_decision"], "blocked")
        self.assertEqual(path.read_bytes(), original_receipt)

    def test_manual_rework_rejects_wrong_phase_dirty_head_and_invalid_latest_receipt(self):
        baseline, path = self.blocked_review()
        valid_review = read_json(path)
        mutations = [
            lambda state, review: state.update(blocked_from="CODE"),
            lambda state, review: review.update(decision="accepted"),
            lambda state, review: review.update(head_sha="wrong-head"),
            lambda state, review: state["task"].update(reworks=0),
        ]
        for mutate in mutations:
            state, review = deepcopy(baseline), dict(valid_review)
            mutate(state, review)
            self.f.save(state)
            write_json(path, review)
            with self.assertRaises(ValueError):
                self.f.restart().retry(rework=True, new_session=True)
            self.assertNotIn("manual_rework_allowance", self.f.load()["task"])
        self.f.save(baseline)
        write_json(path, valid_review)
        (self.f.work / "feature.txt").write_text("dirty", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "HEAD"):
            self.f.restart().retry(rework=True, new_session=True)

    def test_plain_retry_preserves_existing_behavior_and_session(self):
        before, _ = self.blocked_review()
        self.assertEqual(self.f.restart().retry()["phase"], "REVIEW")
        after = self.f.load()["task"]
        self.assertEqual(after["session_id"], before["task"]["session_id"])
        self.assertEqual(after["reworks"], before["task"]["reworks"])
        self.assertNotIn("manual_rework_allowance", after)

    def test_cli_flag_dependencies_and_unknown_flags(self):
        with self.assertRaises(SystemExit) as missing:
            cli.main(["retry", "--new-session"])
        self.assertEqual(missing.exception.code, 2)
        with self.assertRaises(SystemExit) as unknown:
            cli.parser().parse_args(["retry", "--new-session-id", "invented"])
        self.assertEqual(unknown.exception.code, 2)
        parsed = cli.parser().parse_args(["retry", "--rework", "--new-session"])
        self.assertTrue(parsed.rework and parsed.new_session)


if __name__ == "__main__":
    unittest.main()
