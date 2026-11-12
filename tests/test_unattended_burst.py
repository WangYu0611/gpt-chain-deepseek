"""Task-count boundaries, restart receipts and temporary schedule scope."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import test_unattended_recovery as recovery
from unattended import burst, gitops
from unattended.calendar import BEIJING, WorkCalendar
from unattended.engine import Engine as RealEngine
from unattended.storage import lock, read_json, write_json


class _HardCrash(BaseException):
    pass


class _Steps:
    def __init__(self):
        self.count = 0
        self.plans = 0
        self.hook = None
        self.crash_after_count = None
        self.now = datetime(2026, 11, 2, 12, tzinfo=BEIJING)
        self.allowed_samples = []


class _ScriptedEngine:
    """A persistent phase fixture; real locks and calendar, no Git/model calls."""
    def __init__(self, config, steps):
        self.config, self.steps = config, steps
        self.runtime = Path(config["runtime_dir"])
        self.state_path = self.runtime / "state.json"

    def now(self):
        return self.steps.now

    def _binding(self):
        return RealEngine._binding(self)

    @contextmanager
    def _locks(self):
        with lock(Path(self.config["project_path"]) / "project-controller.lock"), lock(self.runtime / "controller.lock"):
            yield

    def _load(self):
        return read_json(self.state_path)

    def _step(self):
        self.steps.count += 1
        state = self._load()
        if self.steps.hook:
            result = self.steps.hook(self, state)
            if result is not None:
                return result
        calendar = WorkCalendar(self.config["schedule"], base_dir=Path(self.config["config_path"]).parent)
        allowed = calendar.allowed(self.now())
        self.steps.allowed_samples.append(allowed)
        if not allowed:
            return {"status": "waiting_window", "failures": 0}
        phases = {"PLAN": "CODE", "CODE": "CHECK", "CHECK": "REVIEW", "REVIEW": "PUBLISH"}
        if state["phase"] == "PLAN":
            self.steps.plans += 1
            state["task"] = {"id": "TASK-" + str(len(state["accepted"]) + 1)}
        if state["phase"] == "PUBLISH":
            state["accepted"].append({"id": state["task"]["id"]})
            state["task"], state["phase"] = None, "PLAN"
        else:
            state["phase"] = phases[state["phase"]]
        state["failures"] = 0
        write_json(self.state_path, state)
        if self.steps.crash_after_count == len(state["accepted"]):
            raise _HardCrash("process lost after engine acceptance, before burst receipt")
        return {"status": state["phase"].lower(), "failures": 0}


class BurstBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.calendar = self.root / "calendar.json"
        write_json(self.calendar, {"years": [2026], "holidays": [], "workdays": [], "overrides": {}})
        self.config = {"project_path": str(self.root / "project"), "runtime_dir": str(self.root / "runtime"),
                       "config_path": str(self.root / "config.json"), "goal": "A bounded development target",
                       "branch": "unattended/test", "base_ref": "HEAD", "task_prefix": "TASK",
                       "checks": [{"id": "behavior", "argv": ["python.exe", "verify.py"]}],
                       "push": False, "remote": "origin", "close_policy": "strict",
                       "max_failures": 3, "poll_seconds": 1,
                       "schedule": {"holidays_file": str(self.calendar), "weekday_start": "18:00", "weekday_end": "08:00"}}
        Path(self.config["project_path"]).mkdir()
        self.steps = _Steps()
        patcher = patch.object(burst, "Engine", side_effect=lambda config, providers=None: _ScriptedEngine(config, self.steps))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.reset_state(accepted=2)

    @property
    def record(self):
        return Path(self.config["runtime_dir"]) / "burst.json"

    def reset_state(self, accepted=0):
        write_json(Path(self.config["runtime_dir"]) / "state.json",
                   {"phase": "PLAN", "task": None, "accepted": [{"id": "TASK-" + str(index + 1)} for index in range(accepted)]})

    def test_exact_new_task_count_stops_at_plan_and_completed_receipt_is_idempotent(self):
        result = burst.run_burst(self.config, tasks=3, ignore_window=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["start_accepted_count"], 2)
        self.assertEqual(result["target_accepted_count"], 5)
        self.assertEqual(result["completed_task_ids"], ["TASK-3", "TASK-4", "TASK-5"])
        self.assertEqual((result["phase"], result["remaining_tasks"]), ("PLAN", 0))
        self.assertEqual(self.steps.plans, 3)
        self.assertEqual(self.steps.count, 15)
        again = burst.run_burst(self.config, tasks=3, ignore_window=True)
        self.assertEqual(again["burst_id"], result["burst_id"])
        self.assertEqual(self.steps.count, 15)
        self.assertEqual(self.steps.plans, 3)

    def test_interrupt_then_restart_only_runs_the_remaining_accepted_tasks(self):
        def interrupt(engine, state):
            if len(state["accepted"]) == 3:
                raise KeyboardInterrupt()
        self.steps.hook = interrupt
        first = burst.run_burst(self.config, tasks=3, ignore_window=True)
        self.assertEqual(first["status"], "interrupted")
        self.assertEqual(first["completed_tasks"], 1)
        self.assertEqual(first["remaining_tasks"], 2)
        self.steps.hook = None
        final = burst.run_burst(self.config, tasks=3, ignore_window=True)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["burst_id"], first["burst_id"])
        self.assertEqual(final["target_accepted_count"], first["target_accepted_count"])
        self.assertEqual(self.steps.plans, 3)

    def test_crash_after_final_engine_acceptance_does_not_start_an_extra_plan(self):
        self.steps.crash_after_count = 3
        with self.assertRaises(_HardCrash):
            burst.run_burst(self.config, tasks=1, ignore_window=True)
        before = self.steps.count
        self.steps.crash_after_count = None
        final = burst.run_burst(self.config, tasks=1, ignore_window=True)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(self.steps.count, before)
        self.assertEqual(self.steps.plans, 1)

    def test_ignore_window_is_private_and_original_calendar_and_dict_never_change(self):
        original = deepcopy(self.config)
        calendar_bytes = self.calendar.read_bytes()
        result = burst.run_burst(self.config, tasks=1, ignore_window=True)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(all(self.steps.allowed_samples))
        self.assertEqual(self.config, original)
        self.assertEqual(self.calendar.read_bytes(), calendar_bytes)
        self.assertFalse(WorkCalendar(self.config["schedule"]).allowed(self.steps.now))
        record = read_json(self.record)
        self.assertEqual(record["original_schedule"], original["schedule"])
        self.assertEqual(record["original_close_policy"], original["close_policy"])
        private = Path(record["private_calendar"])
        self.assertTrue(private.is_relative_to(Path(self.config["runtime_dir"]) / "bursts"))
        self.assertNotEqual(private, self.calendar)

    def test_normal_window_returns_waiting_and_same_burst_can_resume_at_evening(self):
        waiting = burst.run_burst(self.config, tasks=2)
        self.assertEqual(waiting["status"], "waiting_window")
        self.assertEqual(waiting["remaining_tasks"], 2)
        self.assertEqual(self.steps.plans, 0)
        self.steps.now = datetime(2026, 11, 2, 19, tzinfo=BEIJING)
        final = burst.run_burst(self.config, tasks=2)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(waiting["burst_id"], final["burst_id"])
        self.assertEqual(self.steps.plans, 2)

    def test_resume_cannot_reset_count_scope_or_silently_ignore_window(self):
        burst.run_burst(self.config, tasks=3)
        before = self.steps.count
        for tasks, ignore, changes in ((4, False, {}), (3, True, {}),
                                       (3, False, {"close_policy": "finish_step"}),
                                       (3, False, {"goal": "Another target"}),
                                       (3, False, {"schedule": {"weekday_start": "19:00"}})):
            candidate = dict(self.config, **changes)
            with self.subTest(tasks=tasks, ignore=ignore, changes=changes), self.assertRaises(ValueError):
                burst.run_burst(candidate, tasks=tasks, ignore_window=ignore)
        self.assertEqual(self.steps.count, before)
        self.assertEqual(read_json(self.record)["target_accepted_count"], 5)

    def test_project_lock_rejects_a_concurrent_controller_before_burst_creation(self):
        with lock(Path(self.config["project_path"]) / "project-controller.lock"):
            with self.assertRaisesRegex(RuntimeError, "执行器"):
                burst.run_burst(self.config, tasks=1, ignore_window=True)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.steps.count, 0)

    def test_blocked_done_and_failure_stop_without_claiming_requested_tasks_complete(self):
        for outcome in ("blocked", "done", "failed", "paused", "needs_setup", "calendar_update_required"):
            config = dict(self.config, runtime_dir=str(self.root / ("runtime-" + outcome)))
            state_path = Path(config["runtime_dir"]) / "state.json"
            write_json(state_path, {"phase": "PLAN", "task": None, "accepted": []})
            def stop(engine, state):
                if outcome == "failed":
                    raise RuntimeError("Unrecoverable local controller failure")
                state["phase"] = "BLOCKED" if outcome == "blocked" else "DONE" if outcome == "done" else "PLAN"
                write_json(engine.state_path, state)
                return {"status": outcome, "failures": 1 if outcome == "failed" else 0, "last_error": "stopped early"}
            self.steps.hook = stop
            before = self.steps.count
            with self.subTest(outcome=outcome):
                result = burst.run_burst(config, tasks=3, ignore_window=True)
                self.assertEqual(result["status"], outcome)
                self.assertEqual((result["completed_tasks"], result["remaining_tasks"]), (0, 3))
                self.assertEqual(self.steps.count, before + 1)

    def test_transient_step_failures_retry_with_existing_budget_and_then_finish(self):
        failures = [0]
        def fail_twice(engine, state):
            if state["phase"] == "CODE" and failures[0] < 2:
                failures[0] += 1
                state["failures"] = failures[0]
                write_json(engine.state_path, state)
                return {"status": "code", "phase": "CODE", "failures": failures[0], "last_error": "TRANSPORT unavailable"}
        self.steps.hook = fail_twice
        with patch.object(burst.time, "sleep") as sleep:
            result = burst.run_burst(self.config, tasks=1, ignore_window=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_task_ids"], ["TASK-3"])
        self.assertEqual(result["retry_count"], 2)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(self.steps.plans, 1)
        self.assertTrue(all(0 <= call.args[0] <= 60 for call in sleep.call_args_list))

    def test_exhausted_engine_failure_budget_stops_on_blocked(self):
        def fail_until_blocked(engine, state):
            state["failures"] = state.get("failures", 0) + 1
            state["phase"] = "BLOCKED" if state["failures"] >= engine.config["max_failures"] else "PLAN"
            state["last_error"] = "TRANSPORT unavailable"
            write_json(engine.state_path, state)
            return {"status": state["phase"].lower(), "phase": state["phase"],
                    "failures": state["failures"], "last_error": state["last_error"]}
        self.steps.hook = fail_until_blocked
        with patch.object(burst.time, "sleep") as sleep:
            result = burst.run_burst(self.config, tasks=1, ignore_window=True)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["remaining_tasks"], 1)
        self.assertEqual(self.steps.count, self.config["max_failures"])
        self.assertEqual(sleep.call_count, self.config["max_failures"] - 1)

    def test_invalid_task_count_or_implicit_window_values_are_rejected(self):
        for tasks, ignore in ((0, False), (-1, False), (True, False), (1.5, False), (1, "yes")):
            with self.subTest(tasks=tasks, ignore=ignore), self.assertRaises(ValueError):
                burst.run_burst(self.config, tasks, ignore_window=ignore)
        self.assertFalse(self.record.exists())


class _SerialProviders(recovery.FakeProviders):
    def __init__(self, runtime):
        super().__init__()
        self.runtime = runtime

    def code(self, prompt, cwd, out, timeout, session_id=None):
        self.code_calls.append({"session_id": session_id})
        state = read_json(self.runtime / "state.json")
        task_id = state["task"]["id"]
        (cwd / "feature.txt").write_text("implemented " + task_id + "\n", encoding="utf-8")
        gitops.git(cwd, "add", "feature.txt")
        gitops.git(cwd, "commit", "-m", task_id)
        return {"completed": True, "session_id": session_id or "serial-local-session-" + task_id, "text": "Committed " + task_id}


class BurstRealGitTests(unittest.TestCase):
    def test_two_new_tasks_use_real_engine_commits_checks_reviews_and_stop_before_third_plan(self):
        fixture = recovery.RecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        providers = _SerialProviders(Path(fixture.config["runtime_dir"]))
        original = deepcopy(fixture.config)
        result = burst.run_burst(fixture.config, tasks=2, ignore_window=True, providers=providers)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_task_ids"], ["TASK-AUTO-0001", "TASK-AUTO-0002"])
        self.assertEqual(result["phase"], "PLAN")
        self.assertEqual((providers.plan_calls, len(providers.code_calls), providers.review_calls), (2, 2, 2))
        state = read_json(Path(fixture.config["runtime_dir"]) / "state.json")
        self.assertEqual(len(state["accepted"]), 2)
        self.assertIsNone(state["task"])
        self.assertTrue(gitops.clean(fixture.work))
        self.assertEqual((fixture.project / "feature.txt").read_text(), "original\n")
        self.assertEqual(fixture.config, original)
        again = burst.run_burst(fixture.config, tasks=2, ignore_window=True, providers=providers)
        self.assertEqual(again["burst_id"], result["burst_id"])
        self.assertEqual((providers.plan_calls, len(providers.code_calls), providers.review_calls), (2, 2, 2))


if __name__ == "__main__":
    unittest.main()
