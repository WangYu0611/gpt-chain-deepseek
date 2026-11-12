"""Meaningful offline checks: no live GitHub or model execution."""
import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import unittest

from lab_harness.state import InvalidTransition, LeaseLost, StateError, StateStore


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = StateStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def ready_for_review(self, issue=1, event="start", head="a" * 40, now=0):
        claimed = self.store.claim(issue, event, now=now)
        return self.store.finish(issue, claimed["owner"], "REVIEW",
                                 {"result_commit_sha": head, "pr_id": 5}, now=now + 1)

    def test_duplicate_active_and_completed_event_are_not_claimed(self):
        run = self.store.claim(1, "delivery", now=0)
        self.assertIsNone(self.store.claim(1, "delivery", now=1))
        self.store.finish(1, run["owner"], "REVIEW", {"head_sha": "a" * 40}, now=2)
        self.assertIsNone(self.store.claim(1, "delivery", now=3))
        self.assertEqual(self.store.snapshot(1)["attempt"], 1)

    def test_only_one_concurrent_worker_claims_issue(self):
        barrier = threading.Barrier(8)

        def compete(i):
            barrier.wait()
            return self.store.claim(1, f"event-{i}", now=0)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(compete, range(8)))
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertEqual(self.store.snapshot(1)["attempt"], 1)

    def test_restart_recovers_same_attempt_and_checkpoints(self):
        original = self.store.claim(1, "delivery", now=0, lease_seconds=10)
        self.store.checkpoint(1, original["owner"], {"pr_id": 9, "result_commit_sha": "a" * 40}, now=1)
        restarted = StateStore(self.path)
        recovered = restarted.claim(1, "delivery", now=11)
        self.assertEqual(recovered["attempt"], original["attempt"])
        self.assertNotEqual(recovered["owner"], original["owner"])
        self.assertEqual(recovered["result"]["pr_id"], 9)

    def test_expired_owner_cannot_finish_or_heartbeat_or_checkpoint(self):
        original = self.store.claim(1, "delivery", now=0, lease_seconds=10)
        self.store.claim(1, "delivery", now=11)
        for call in (
            lambda: self.store.heartbeat(1, original["owner"], now=12),
            lambda: self.store.checkpoint(1, original["owner"], {"pr_id": 8}, now=12),
            lambda: self.store.finish(1, original["owner"], "FAILED", now=12),
        ):
            with self.assertRaises(LeaseLost):
                call()

    def test_expired_lease_cannot_be_extended_by_old_owner(self):
        run = self.store.claim(1, "delivery", now=0, lease_seconds=10)
        with self.assertRaises(LeaseLost):
            self.store.heartbeat(1, run["owner"], now=10)

    def test_heartbeat_extends_lease(self):
        run = self.store.claim(1, "delivery", now=0, lease_seconds=10)
        self.store.heartbeat(1, run["owner"], now=9, lease_seconds=10)
        self.assertIsNone(self.store.claim(1, "delivery", now=11))
        self.assertEqual(self.store.snapshot(1)["lease_until"], 19)

    def test_busy_event_not_consumed_and_wrong_event_cannot_recover(self):
        run = self.store.claim(1, "first", now=0, lease_seconds=10)
        self.assertIsNone(self.store.claim(1, "second", now=1))
        self.assertIsNone(self.store.claim(1, "second", now=11))
        recovered = self.store.claim(1, "first", now=12)
        self.store.finish(1, recovered["owner"], "FAILED", now=13)
        second = self.store.claim(1, "second", now=14)
        self.assertIsNotNone(second)
        self.assertEqual(second["attempt"], run["attempt"] + 1)

    def test_event_id_cannot_claim_different_issue(self):
        self.store.claim(1, "one", now=0)
        with self.assertRaises(StateError):
            self.store.claim(2, "one", now=1)
        self.assertIsNone(self.store.snapshot(2))

    def test_review_requires_machine_commit_reference(self):
        run = self.store.claim(1, "delivery", now=0)
        with self.assertRaises(InvalidTransition):
            self.store.finish(1, run["owner"], "REVIEW", {"text": "tests passed"}, now=1)
        self.assertEqual(self.store.snapshot(1)["status"], "RUNNING")

    def test_worker_cannot_self_accept(self):
        run = self.store.claim(1, "delivery", now=0)
        with self.assertRaises(InvalidTransition):
            self.store.finish(1, run["owner"], "ACCEPTED", now=1)

    def test_rework_duplicate_does_not_spend_extra_attempt(self):
        self.ready_for_review()
        first = self.store.request_rework(1, "rework-1", "a" * 40, now=2)
        replay = self.store.request_rework(1, "rework-1", "a" * 40, now=3)
        self.assertEqual(first["reworks"], 1)
        self.assertEqual(replay["reworks"], 1)
        claimed = self.store.claim(1, "rework-1", now=4)
        self.assertEqual(claimed["attempt"], 2)
        self.assertEqual(self.store.request_rework(1, "rework-1", "a" * 40, now=5)["reworks"], 1)

    def test_fourth_automatic_rework_is_blocked(self):
        self.ready_for_review()
        for index in range(1, 4):
            event = f"rework-{index}"
            queued = self.store.request_rework(1, event, "a" * 40, now=index * 10)
            self.assertEqual(queued["status"], "REWORK")
            run = self.store.claim(1, event, now=index * 10 + 1)
            self.store.finish(1, run["owner"], "REVIEW", {"head_sha": "a" * 40}, now=index * 10 + 2)
        blocked = self.store.request_rework(1, "rework-4", "a" * 40, now=41)
        self.assertEqual((blocked["status"], blocked["reworks"]), ("BLOCKED", 3))
        self.assertIsNone(self.store.claim(1, "rework-4", now=42))
        self.assertIsNone(self.store.claim(1, "another", now=43))

    def test_wrong_head_and_running_rework_do_not_consume_event(self):
        self.ready_for_review()
        with self.assertRaises(InvalidTransition):
            self.store.request_rework(1, "revision", "b" * 40, now=2)
        self.store.request_rework(1, "revision", "a" * 40, now=3)
        run = self.store.claim(1, "revision", now=4)
        with self.assertRaises(InvalidTransition):
            self.store.request_rework(1, "next", "a" * 40, now=5)
        self.store.finish(1, run["owner"], "REVIEW", now=6)
        self.assertEqual(self.store.request_rework(1, "next", "a" * 40, now=7)["reworks"], 2)

    def test_failure_circuit_breaker_persists_across_restart(self):
        for index in range(3):
            run = StateStore(self.path).claim(1, f"attempt-{index}", now=index * 2)
            outcome = self.store.finish(1, run["owner"], "FAILED", {"reason": "fixture"}, now=index * 2 + 1)
        self.assertEqual(outcome["status"], "BLOCKED")
        self.assertEqual(outcome["failures"], 3)
        self.assertIsNone(StateStore(self.path).claim(1, "attempt-4", now=7))

    def test_acceptance_is_bound_to_exact_head(self):
        self.ready_for_review()
        with self.assertRaises(InvalidTransition):
            self.store.accept(1, "review", "b" * 40, now=2)
        self.assertEqual(self.store.accept(1, "review", "a" * 40, now=3)["status"], "ACCEPTED")
        self.assertEqual(self.store.accept(1, "review", "a" * 40, now=4)["status"], "ACCEPTED")
        changed = self.store.invalidate_acceptance(1, "b" * 40, now=5)
        self.assertEqual((changed["status"], changed["head_sha"]), ("REVIEW", "b" * 40))
        with self.assertRaises(InvalidTransition):
            self.store.accept(1, "stale-review", "a" * 40, now=6)

    def test_pause_persists_and_allows_active_run_to_finish(self):
        run = self.store.claim(1, "one", now=0)
        self.store.pause()
        self.assertTrue(StateStore(self.path).paused)
        self.assertIsNone(self.store.claim(2, "two", now=1))
        self.assertIsNone(self.store.snapshot(2))
        self.store.finish(1, run["owner"], "FAILED", now=2)
        self.store.resume()
        self.assertIsNotNone(StateStore(self.path).claim(2, "two", now=3))

    def test_export_restore_preserves_idempotency_and_expires_live_owner(self):
        self.ready_for_review()
        self.store.request_rework(1, "revision", "a" * 40, now=2)
        run = self.store.claim(1, "revision", now=3)
        self.store.checkpoint(1, run["owner"], {"pr_id": 7}, now=4)
        exported = json.loads(json.dumps(self.store.export_snapshot()))
        self.assertIsNone(exported["tasks"][0]["owner"])
        restored = StateStore(Path(self.temp.name) / "restored.sqlite3")
        restored.import_snapshot(exported)
        recovered = restored.claim(1, "revision", now=5)
        self.assertEqual((recovered["attempt"], recovered["reworks"], recovered["result"]["pr_id"]), (2, 1, 7))
        with self.assertRaises(LeaseLost):
            restored.finish(1, run["owner"], "FAILED", now=6)
        restored.finish(1, recovered["owner"], "FAILED", now=6)
        self.assertIsNone(restored.claim(1, "revision", now=7))

    def test_restore_cannot_overwrite_nonempty_cache(self):
        snapshot = self.store.export_snapshot()
        self.store.claim(1, "one", now=0)
        with self.assertRaises(StateError):
            self.store.import_snapshot(snapshot)
        self.assertEqual(self.store.snapshot(1)["attempt"], 1)

    def test_restore_inconsistent_running_event_rolls_back_all_rows(self):
        self.store.claim(1, "one", now=0)
        snapshot = self.store.export_snapshot()
        snapshot["events"] = []
        restored = StateStore(Path(self.temp.name) / "invalid.sqlite3")
        with self.assertRaises(ValueError):
            restored.import_snapshot(snapshot)
        self.assertEqual(restored.list_tasks(), [])

    def test_rework_limit_cannot_be_configured_above_three(self):
        with self.assertRaises(ValueError):
            StateStore(self.path, max_reworks=4)


if __name__ == "__main__":
    unittest.main()
