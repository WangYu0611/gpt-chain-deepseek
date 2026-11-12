from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from unattended import background


def sharing_error(winerror=5):
    error = PermissionError("simulated temporary Windows sharing violation")
    error.winerror = winerror
    return error


class FakeScheduler:
    def __init__(self):
        self.task = None
        self.calls = []
        self.on_run = lambda: None

    def info(self, name):
        return self.task

    def _owned(self, name, description):
        if self.task and (self.task["owner"] != background.OWNER or self.task["description"] != description):
            raise RuntimeError("foreign ownership")
        return self.task

    def register(self, name, description, executable, arguments, *, logon=False):
        self._owned(name, description)
        self.calls.append(("register", str(executable), arguments, logon))
        self.task = {"owner": background.OWNER, "description": description, "state": 3, "logon_trigger": logon}

    def run(self, name):
        self.calls.append(("run", name))
        self.task["state"] = 4
        self.on_run()

    def stop(self, name, description):
        self._owned(name, description)
        self.calls.append(("stop", name))
        if self.task:
            self.task["state"] = 3

    def delete(self, name, description):
        self._owned(name, description)
        self.calls.append(("delete", name))
        self.task = None


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "config with spaces.json"
        self.runtime = self.root / "runtime"
        self.config = {"runtime_dir": str(self.runtime)}
        self.identity = {"pid": 123, "process_created": 456, "image": "fake-pythonw.exe"}
        self.inspector = Mock(return_value=dict(self.identity))
        self.scheduler = FakeScheduler()
        self.context_patch = patch.object(background, "context", return_value=(self.config, self.runtime, "CodexDSH-fake", "our-config"))
        self.context_patch.start()
        self.addCleanup(self.context_patch.stop)
        executable = self.root / "python.exe"
        executable.with_name("pythonw.exe").write_text("fake, never executed", encoding="utf-8")
        python_patch = patch.object(background.sys, "executable", str(executable))
        python_patch.start()
        self.addCleanup(python_patch.stop)
        self.manager = background.BackgroundManager(self.path, scheduler=self.scheduler, inspector=self.inspector)

    def health(self, status="running"):
        background._write(self.manager.health_path, dict(self.identity, owner=background.OWNER,
                          config=str(self.path), status=status, heartbeat_epoch=time.time()))

    def test_install_registers_logon_without_starting_or_running_a_model(self):
        result = self.manager.install()
        self.assertEqual(result["status"], "installed")
        self.assertEqual([row[0] for row in self.scheduler.calls], ["register"])
        register = self.scheduler.calls[0]
        self.assertTrue(register[3])
        self.assertIn('"' + str(self.path) + '"', register[2])
        self.assertIn("-m unattended background worker", register[2])

    def test_start_waits_for_matching_live_health(self):
        self.scheduler.on_run = self.health
        result = self.manager.start(timeout=0)
        self.assertEqual(result["status"], "started")
        self.assertTrue(result["healthy"])
        self.assertEqual([row[0] for row in self.scheduler.calls], ["register", "run"])

    def test_stale_heartbeat_cannot_confirm_start(self):
        def stale():
            self.health()
            value = background._read(self.manager.health_path)
            value["heartbeat_epoch"] -= 120
            background._write(self.manager.health_path, value)
        self.scheduler.on_run = stale
        self.assertEqual(self.manager.start(timeout=0)["status"], "start_timeout")

    def test_reused_pid_does_not_count_as_original_worker(self):
        self.health()
        self.inspector.return_value["process_created"] += 1
        self.assertFalse(self.manager.status()["process_alive"])

    def test_duplicate_running_task_is_not_started_again(self):
        self.scheduler.on_run = self.health
        self.manager.start(timeout=0)
        count = len(self.scheduler.calls)
        with self.assertRaisesRegex(RuntimeError, "重复"):
            self.manager.start(timeout=0)
        self.assertEqual(len(self.scheduler.calls), count)

    def test_stop_persists_pause_before_stopping_the_owned_task(self):
        ordering = []
        module = types.ModuleType("unattended.engine")
        engine = Mock()
        engine.pause.side_effect = lambda: ordering.append("pause")
        module.Engine = Mock(return_value=engine)
        original = self.scheduler.stop
        self.scheduler.stop = lambda name, description: (ordering.append("stop"), original(name, description))
        with patch.dict(sys.modules, {"unattended.engine": module}):
            self.assertEqual(self.manager.stop()["status"], "stopped")
        self.assertEqual(ordering, ["pause", "stop"])

    def test_foreign_task_is_not_paused_or_stopped(self):
        self.scheduler.task = {"owner": "another-app", "description": "foreign", "state": 4}
        module = types.ModuleType("unattended.engine")
        module.Engine = Mock()
        with patch.dict(sys.modules, {"unattended.engine": module}):
            with self.assertRaisesRegex(RuntimeError, "foreign"):
                self.manager.stop()
        module.Engine.assert_not_called()
        self.assertEqual(self.scheduler.calls, [])

    def test_worker_logs_unicode_errors_and_final_heartbeat(self):
        def fail():
            print("中文日志")
            raise RuntimeError("可恢复错误")
        result = background.worker(self.path, run=fail, inspector=lambda: dict(self.identity), interval=0.01)
        self.assertEqual(result, 1)
        self.assertIn("中文日志", (self.runtime / "background.log").read_text(encoding="utf-8"))
        self.assertIn("可恢复错误", (self.runtime / "background.err.log").read_text(encoding="utf-8"))
        health = background._read(self.manager.health_path)
        self.assertEqual(health["status"], "stopped")
        self.assertEqual(health["exit_code"], 1)
        self.assertGreater(health["heartbeat_epoch"], 0)

    def test_heartbeat_updates_while_engine_step_is_running(self):
        observed = []
        def execute():
            before = background._read(self.manager.health_path)["heartbeat_epoch"]
            deadline = time.monotonic() + 1
            refreshed = False
            while time.monotonic() < deadline:
                try:
                    value = background._read(self.manager.health_path)
                    refreshed = bool(value and value["heartbeat_epoch"] > before)
                except OSError as error:
                    if not background.is_windows_sharing_error(error):
                        raise
                if refreshed:
                    break
                time.sleep(0.005)
            observed.append(refreshed)
            return 0
        self.assertEqual(background.worker(self.path, run=execute, inspector=lambda: dict(self.identity), interval=0.01), 0)
        self.assertEqual(observed, [True])

    def test_default_worker_reloads_checkpoint_after_sharing_error_without_changing_pause(self):
        self.runtime.mkdir()
        pause = self.runtime / "PAUSE"
        pause.write_text("keep user pause", encoding="utf-8")
        state = self.runtime / "state.json"
        state.write_text('{"phase":"REVIEW","failures":0}', encoding="utf-8")
        state_bytes = state.read_bytes()
        module = types.ModuleType("unattended.engine")
        instances = [Mock() for _ in range(5)]
        for engine in instances:
            engine.status.return_value = {"status": "review"}
        for engine, winerror in zip(instances[:-1], (5, 32, 33, 5)):
            engine.run.side_effect = sharing_error(winerror)
        instances[-1].run.return_value = {"status": "paused"}
        module.Engine = Mock(side_effect=instances)
        writes = []
        actual_write = background._write
        def record(path, value):
            writes.append(json.loads(json.dumps(value)))
            actual_write(path, value)
        with patch.dict(sys.modules, {"unattended.engine": module}), patch.object(background, "_write", side_effect=record):
            result = background.worker(self.path, inspector=lambda: dict(self.identity), recovery_seconds=0)
        self.assertEqual(result, 0)
        self.assertEqual(module.Engine.call_count, 5)
        self.assertTrue(all(engine.status.call_count == 1 and engine.run.call_count == 1 for engine in instances))
        waiting = next(row for row in writes if row.get("status") == "recovering")
        self.assertEqual(set(waiting["recovery"]), {"kind", "attempt", "next_retry_at"})
        self.assertEqual(waiting["recovery"]["kind"], "filesystem_sharing")
        self.assertEqual(waiting["recovery"]["attempt"], 1)
        self.assertEqual([row["recovery"]["attempt"] for row in writes if row.get("status") == "recovering"],
                         [1, 2, 3, 4])
        self.assertNotIn("simulated", json.dumps(waiting))
        self.assertNotIn("recovery", writes[-1])
        self.assertEqual(state.read_bytes(), state_bytes)
        self.assertEqual(pause.read_text(encoding="utf-8"), "keep user pause")

    def test_default_worker_returns_blocked_without_restart(self):
        module = types.ModuleType("unattended.engine")
        engine = Mock()
        engine.run.return_value = {"status": "blocked"}
        module.Engine = Mock(return_value=engine)
        with patch.dict(sys.modules, {"unattended.engine": module}):
            result = background.worker(self.path, inspector=lambda: dict(self.identity), recovery_seconds=0)
        self.assertEqual(result, 1)
        module.Engine.assert_called_once()
        self.assertEqual(background._read(self.manager.health_path)["exit_code"], 1)

    def test_nonsharing_oserror_is_not_retried(self):
        run = Mock(side_effect=PermissionError("ordinary permission failure, no Windows sharing code"))
        original_out, original_err = sys.stdout, sys.stderr
        result = background.worker(self.path, run=run, inspector=lambda: dict(self.identity), recovery_seconds=0)
        self.assertEqual(result, 1)
        run.assert_called_once()
        self.assertIs(sys.stdout, original_out)
        self.assertIs(sys.stderr, original_err)

    def test_start_and_final_health_sharing_failures_do_not_skip_engine_or_stdout_restoration(self):
        actual_write = background._write
        count = 0
        def intermittently_locked(path, value):
            nonlocal count
            count += 1
            if count in {1, 3}:
                raise sharing_error()
            actual_write(path, value)
        run = Mock(return_value=0)
        original_out, original_err = sys.stdout, sys.stderr
        with patch.object(background, "_write", side_effect=intermittently_locked):
            result = background.worker(self.path, run=run, inspector=lambda: dict(self.identity))
        self.assertEqual(result, 0)
        run.assert_called_once()
        self.assertEqual(count, 3)
        self.assertIs(sys.stdout, original_out)
        self.assertIs(sys.stderr, original_err)

    def test_heartbeat_keeps_retrying_after_a_failed_publication(self):
        actual_write = background._write
        count = 0
        def fail_one_heartbeat(path, value):
            nonlocal count
            count += 1
            if count == 3:
                raise sharing_error(32)
            actual_write(path, value)
        observed = []
        def run():
            before = background._read(self.manager.health_path)["heartbeat_epoch"]
            deadline = time.monotonic() + 1
            while background._read(self.manager.health_path)["heartbeat_epoch"] <= before and time.monotonic() < deadline:
                time.sleep(0.005)
            observed.append(background._read(self.manager.health_path)["heartbeat_epoch"] > before)
            return 0
        with patch.object(background, "_write", side_effect=fail_one_heartbeat):
            self.assertEqual(background.worker(self.path, run=run, inspector=lambda: dict(self.identity), interval=0.01), 0)
        self.assertEqual(observed, [True])
        self.assertGreaterEqual(count, 4)

    def test_matching_fresh_recovery_worker_is_healthy_but_preserves_wait_state(self):
        self.health(status="recovering")
        value = self.manager.status()
        self.assertTrue(value["healthy"])
        self.assertEqual(value["health"]["status"], "recovering")


class SchedulerTests(unittest.TestCase):
    def scheduler(self, existing=None):
        action, trigger = SimpleNamespace(), SimpleNamespace()
        task = SimpleNamespace(RegistrationInfo=SimpleNamespace(), Principal=SimpleNamespace(),
                               Settings=SimpleNamespace(), Actions=SimpleNamespace(Create=Mock(return_value=action)),
                               Triggers=SimpleNamespace(Create=Mock(return_value=trigger)))
        scheduler = object.__new__(background.TaskScheduler)
        scheduler.service = SimpleNamespace(NewTask=Mock(return_value=task))
        scheduler.folder = SimpleNamespace(RegisterTaskDefinition=Mock())
        scheduler.info = Mock(return_value=existing)
        return scheduler, task, action, trigger

    def test_register_uses_current_interactive_user_no_password_or_elevation(self):
        scheduler, task, action, trigger = self.scheduler()
        scheduler.register("test", "owned", Path("pythonw.exe"), "-m unattended", logon=True)
        self.assertEqual(task.Principal.LogonType, 3)
        self.assertEqual(task.Principal.RunLevel, 0)
        self.assertEqual(task.Settings.ExecutionTimeLimit, "PT0S")
        self.assertEqual(task.Settings.MultipleInstances, 2)
        self.assertFalse(task.Settings.Hidden)
        task.Triggers.Create.assert_called_once_with(9)
        self.assertEqual(trigger.UserId, task.Principal.UserId)
        arguments = scheduler.folder.RegisterTaskDefinition.call_args.args
        self.assertIsNone(arguments[4])
        self.assertEqual(arguments[5], 3)
        self.assertEqual(action.WorkingDirectory, str(background.ROOT))

    def test_start_registration_preserves_existing_logon_trigger(self):
        scheduler, task, _, _ = self.scheduler({"owner": background.OWNER, "description": "owned", "state": 3, "logon_trigger": True})
        scheduler.register("test", "owned", Path("pythonw.exe"), "args", logon=False)
        task.Triggers.Create.assert_called_once_with(9)

    def test_foreign_or_running_registration_is_not_overwritten(self):
        for info in ({"owner": "foreign", "description": "owned", "state": 3},
                     {"owner": background.OWNER, "description": "owned", "state": 4}):
            scheduler, _, _, _ = self.scheduler(info)
            with self.assertRaises(RuntimeError):
                scheduler.register("test", "owned", Path("pythonw.exe"), "args")
            scheduler.folder.RegisterTaskDefinition.assert_not_called()


if __name__ == "__main__":
    unittest.main()
