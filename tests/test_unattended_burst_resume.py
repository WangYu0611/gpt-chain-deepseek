"""Normal/login entry points must respect a persisted bounded-batch target."""
from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from tests import test_unattended_burst as fixtures
from unattended import burst
from unattended.calendar import WorkCalendar
from unattended.calendar import BEIJING
from unattended.engine import Engine
from unattended.storage import lock, read_json, write_json, write_text


class NormalRunBatchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BurstBoundaryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config, self.steps = self.fixture.config, self.fixture.steps
        factory = patch.object(burst, "Engine", side_effect=lambda config, providers=None, now=None:
                               fixtures._ScriptedEngine(config, self.steps))
        factory.start()
        self.addCleanup(factory.stop)
        # Exercise real Engine.run/step dispatch and real OS locks. The phase
        # worker is a persistent local fixture, so no agent/Git/build is started.
        self.engine = Engine(self.config, providers=object(), now=lambda: self.steps.now)
        self.engine._locks = lambda: fixtures._ScriptedEngine(self.config, self.steps)._locks()
        self.normal = Mock(return_value={"status": "done", "failures": 0, "task_id": None, "last_error": None})
        self.engine._step = self.normal

    @property
    def record_path(self):
        return Path(self.config["runtime_dir"]) / "burst.json"

    @property
    def state_path(self):
        return Path(self.config["runtime_dir"]) / "state.json"

    def interrupt_after_one(self):
        def interrupt(engine, state):
            if len(state["accepted"]) == 3:
                raise KeyboardInterrupt()
        self.steps.hook = interrupt
        receipt = burst.run_burst(self.config, 3, ignore_window=True)
        self.steps.hook = None
        self.assertEqual((receipt["status"], receipt["completed_tasks"], receipt["remaining_tasks"]), ("interrupted", 1, 2))
        return receipt

    def run_normal(self, once=False):
        with redirect_stdout(io.StringIO()):
            return self.engine.run(once=once)

    def test_normal_run_recovers_original_remaining_target_then_restores_original_schedule(self):
        first = self.interrupt_after_one()
        def regular_step():
            # Normal scheduling only resumes after the fixed target is durable,
            # with the original weekday calendar, not the private all-day one.
            record = read_json(self.record_path)
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["target_accepted_count"], first["target_accepted_count"])
            self.assertFalse(WorkCalendar(self.engine.config["schedule"]).allowed(self.steps.now))
            self.assertEqual(self.engine.config["close_policy"], "strict")
            return {"status": "done", "failures": 0, "task_id": None, "last_error": None}
        self.normal.side_effect = regular_step
        self.assertEqual(self.run_normal()["status"], "done")
        record = read_json(self.record_path)
        self.assertEqual(record["id"], first["burst_id"])
        self.assertEqual(record["completed_task_ids"], ["TASK-3", "TASK-4", "TASK-5"])
        self.assertTrue(record["ignore_window"])
        self.assertEqual(self.steps.plans, 3)
        self.assertEqual(len(read_json(self.state_path)["accepted"]), 5)
        self.normal.assert_called_once_with()

    def test_run_once_advances_only_the_pending_batch_and_never_runs_n_plus_one_phase(self):
        first = self.interrupt_after_one()
        completed = None
        for _ in range(12):
            before = self.steps.count
            result = self.run_normal(once=True)
            self.assertEqual(self.steps.count, before + 1)
            self.normal.assert_not_called()
            self.assertEqual(result["burst_id"], first["burst_id"])
            self.assertEqual(result["target_accepted_count"], 5)
            if result["status"] == "completed":
                completed = result
                break
        self.assertIsNotNone(completed)
        self.assertEqual(completed["phase"], "PLAN")
        self.assertEqual(self.steps.plans, 3)
        self.assertEqual(read_json(self.state_path)["phase"], "PLAN")
        # The next explicit call is regular scheduling; the historical receipt
        # no longer traps all future --once calls in the old completed batch.
        self.assertEqual(self.run_normal(once=True)["status"], "done")
        self.normal.assert_called_once_with()

    def test_crash_after_last_acceptance_is_reconciled_before_any_new_planning(self):
        self.steps.crash_after_count = 3
        with self.assertRaises(fixtures._HardCrash):
            burst.run_burst(self.config, 1, ignore_window=True)
        before = self.steps.count
        self.steps.crash_after_count = None
        result = self.run_normal(once=True)
        self.assertEqual((result["status"], result["phase"]), ("completed", "PLAN"))
        self.assertEqual(self.steps.count, before)
        self.assertEqual(self.steps.plans, 1)
        self.normal.assert_not_called()

    def test_paused_blocked_and_local_failure_never_fall_through_to_regular_run(self):
        first = self.interrupt_after_one()
        pause = Path(self.config["runtime_dir"]) / "PAUSE"
        for outcome in ("paused", "blocked", "failed"):
            state = read_json(self.state_path)
            state.update(phase="BLOCKED" if outcome == "blocked" else "PLAN", task=None,
                         last_error="test external condition")
            write_json(self.state_path, state)
            if outcome == "paused":
                write_text(pause, "explicit pause\n")
            else:
                pause.unlink(missing_ok=True)
            def stopped(engine, state):
                if outcome == "failed":
                    raise RuntimeError("unrecoverable local condition")
                self.assertTrue(pause.exists())
                return {"status": "paused", "failures": 0, "last_error": "explicit pause"}
            self.steps.hook = stopped
            with self.subTest(outcome=outcome):
                result = self.run_normal(once=outcome == "paused")
                self.assertEqual(result["status"], outcome)
                self.assertEqual(result["target_accepted_count"], first["target_accepted_count"])
                self.assertEqual((result["completed_tasks"], result["remaining_tasks"]), (1, 2))
                self.assertEqual(result["burst_id"], first["burst_id"])
                self.normal.assert_not_called()
                if outcome == "paused":
                    self.assertEqual(pause.read_text(), "explicit pause\n")

    def test_closed_historical_batch_does_not_block_regular_run_after_config_changes(self):
        self.interrupt_after_one()
        for closed in ("completed", "done"):
            record = read_json(self.record_path)
            record.update(status=closed, binding="old historical configuration")
            write_json(self.record_path, record)
            with self.subTest(closed=closed):
                self.assertEqual(self.run_normal()["status"], "done")
        self.assertEqual(self.normal.call_count, 2)

    def test_active_scope_mismatch_fails_closed_instead_of_resetting_or_regular_run(self):
        self.interrupt_after_one()
        record = read_json(self.record_path)
        record["original_schedule_hash"] = "a changed schedule"
        write_json(self.record_path, record)
        before = self.steps.count
        for once in (False, True):
            with self.subTest(once=once), self.assertRaisesRegex(ValueError, "工作时间"):
                self.run_normal(once=once)
        self.assertEqual(self.steps.count, before)
        self.normal.assert_not_called()

    def test_pending_receipt_is_inspected_only_after_both_os_locks_are_owned(self):
        observed = []
        original = burst.resume_pending_burst
        def inspect_locked(engine, once=False):
            for path in (Path(self.config["project_path"]) / "project-controller.lock",
                         Path(self.config["runtime_dir"]) / "controller.lock"):
                with self.assertRaises((RuntimeError, OSError)):
                    with lock(path):
                        self.fail("The pending-batch guard ran before acquiring its OS lock")
            observed.append(True)
            return original(engine, once=once)
        with patch.object(burst, "resume_pending_burst", side_effect=inspect_locked):
            self.run_normal()
            self.run_normal(once=True)
        self.assertEqual(observed, [True, True])

    def test_continuous_recovery_waits_across_work_window_and_keeps_original_target(self):
        first = burst.run_burst(self.config, 3)
        self.assertEqual(first["status"], "waiting_window")
        observed = []
        def evening(seconds):
            self.assertEqual(seconds, self.config["poll_seconds"])
            heartbeat = read_json(Path(self.config["runtime_dir"]) / "heartbeat.json")
            observed.append(heartbeat["status"])
            self.assertEqual(heartbeat["target_accepted_count"], first["target_accepted_count"])
            self.assertEqual(heartbeat["burst_id"], first["burst_id"])
            self.assertFalse(heartbeat["ignore_window"])
            self.normal.assert_not_called()
            for path in (Path(self.config["project_path"]) / "project-controller.lock",
                         Path(self.config["runtime_dir"]) / "controller.lock"):
                with self.assertRaises((RuntimeError, OSError)):
                    with lock(path):
                        self.fail("The batch released its project/runtime lock while waiting")
            self.steps.now = datetime(2026, 11, 2, 19, tzinfo=BEIJING)
        with patch("unattended.engine.time.sleep", side_effect=evening) as sleep:
            self.assertEqual(self.run_normal()["status"], "done")
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(observed, ["waiting_window"])
        record = read_json(self.record_path)
        self.assertEqual((record["status"], record["target_accepted_count"]), ("completed", 5))
        self.assertEqual(record["id"], first["burst_id"])
        self.assertEqual(self.steps.plans, 3)
        self.normal.assert_called_once_with()

    def test_continuous_recovery_waits_for_calendar_update_before_same_batch_runs(self):
        self.steps.now = datetime(2026, 11, 2, 19, tzinfo=BEIJING)
        write_json(self.fixture.calendar, {"years": [], "holidays": [], "workdays": [], "overrides": {}})
        def calendar_gate(engine, state):
            description = WorkCalendar(engine.config["schedule"]).describe(engine.now())
            if not description["calendar_covered"]:
                return {"status": "calendar_update_required", "failures": 0}
        self.steps.hook = calendar_gate
        first = burst.run_burst(self.config, 3)
        self.assertEqual(first["status"], "calendar_update_required")
        def update_calendar(seconds):
            heartbeat = read_json(Path(self.config["runtime_dir"]) / "heartbeat.json")
            self.assertEqual(heartbeat["status"], "calendar_update_required")
            self.assertEqual(heartbeat["target_accepted_count"], 5)
            self.normal.assert_not_called()
            write_json(self.fixture.calendar, {"years": [2026], "holidays": [], "workdays": [], "overrides": {}})
        with patch("unattended.engine.time.sleep", side_effect=update_calendar) as sleep:
            self.assertEqual(self.run_normal()["status"], "done")
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(read_json(self.record_path)["id"], first["burst_id"])
        self.assertEqual(self.steps.plans, 3)

    def test_continuous_paused_batch_waits_for_explicit_user_resume_without_clearing_marker(self):
        first = self.interrupt_after_one()
        pause = Path(self.config["runtime_dir"]) / "PAUSE"
        write_text(pause, "explicit pause\n")
        def pause_gate(engine, state):
            if pause.exists():
                return {"status": "paused", "failures": 0}
        self.steps.hook = pause_gate
        def user_resumes(seconds):
            self.assertEqual(pause.read_text(), "explicit pause\n")
            heartbeat = read_json(Path(self.config["runtime_dir"]) / "heartbeat.json")
            self.assertEqual(heartbeat["status"], "paused")
            self.assertEqual((heartbeat["completed_tasks"], heartbeat["remaining_tasks"]), (1, 2))
            self.normal.assert_not_called()
            pause.unlink()  # Test-driven external user action, not runner code.
        with patch("unattended.engine.time.sleep", side_effect=user_resumes) as sleep:
            self.assertEqual(self.run_normal()["status"], "done")
        self.assertEqual(sleep.call_count, 1)
        record = read_json(self.record_path)
        self.assertEqual(record["id"], first["burst_id"])
        self.assertEqual(record["target_accepted_count"], first["target_accepted_count"])
        self.assertEqual(self.steps.plans, 3)


if __name__ == "__main__":
    unittest.main()
