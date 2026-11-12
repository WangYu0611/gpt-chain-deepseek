from __future__ import annotations

import contextlib
import io
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import desktop_background as background
from relay.storage import read_json, write_json


class FakeScheduler:
    def __init__(self, runtime, config):
        self.runtime, self.config = runtime, config
        self.task = None
        self.calls = []
        self.progress = True
    def info(self, name): return self.task
    def register(self, name, description, executable, arguments):
        self.calls.append(("register", name, str(executable), arguments))
        self.task = {"owner": background.OWNER, "description": description, "state": 3, "last_result": 0}
        self.arguments = arguments
    def run(self, name):
        self.calls.append(("run", name))
        self.task["state"] = 4
        instance = re.search(r"--instance-id ([0-9a-f-]+)", self.arguments).group(1)
        write_json(self.runtime / "background-health.json", {
            "owner": background.OWNER, "config": str(self.config), "pid": 555, "process_created": 100,
            "image": "fake-pythonw.exe", "status": "running", "heartbeat_epoch": time.time(),
            "started_epoch": time.time() - 1, "instance_id": instance,
        })
        if self.progress:
            write_json(self.runtime / "cycle-state.json", {"updated_at": "first loop", "current": {"token": "preserve-me"}})


class BackgroundManagerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config with spaces.json"
        write_json(self.config, {"runtime_dir": "runtime"})
        self.runtime = self.root / "runtime"
        fake_python = self.root / ".venv" / "Scripts" / "pythonw.exe"
        fake_python.parent.mkdir(parents=True)
        fake_python.write_text("fake; never executed", encoding="utf-8")
        root_patch = patch.object(background, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.scheduler = FakeScheduler(self.runtime, self.config)
        self.identity = {"pid": 555, "process_created": 100, "image": "fake-pythonw.exe"}
        self.inspector = Mock(return_value=dict(self.identity))
        self.manager = background.BackgroundManager(self.config, self.scheduler, self.inspector)

    def test_start_waits_for_live_identity_and_first_loop_progress_without_resetting_nonce(self):
        result = self.manager.start(timeout=0.2)
        self.assertEqual(result["status"], "started")
        self.assertEqual([item[0] for item in self.scheduler.calls], ["register", "run"])
        self.assertEqual(read_json(self.runtime / "cycle-state.json")["current"]["token"], "preserve-me")
        self.assertIn('"' + str(self.config) + '"', self.scheduler.arguments)
        self.assertIn("worker --config", self.scheduler.arguments)

    def test_heartbeat_without_any_loop_progress_does_not_report_started(self):
        self.scheduler.progress = False
        result = self.manager.start(timeout=0)
        self.assertEqual(result["status"], "start_timeout")
        self.assertTrue(result["process_alive"])
        self.assertIsNone(result["state_updated_epoch"])

    def test_second_start_refuses_matching_live_process(self):
        self.manager.start(timeout=0.2)
        calls = list(self.scheduler.calls)
        with self.assertRaisesRegex(RuntimeError, "重复"): self.manager.start()
        self.assertEqual(self.scheduler.calls, calls)

    def test_pause_is_not_silently_removed(self):
        self.runtime.mkdir()
        (self.runtime / "PAUSE").touch()
        with self.assertRaisesRegex(RuntimeError, "暂停"): self.manager.start()
        self.assertTrue((self.runtime / "PAUSE").exists())
        self.assertEqual(self.scheduler.calls, [])

    def test_controller_lock_prevents_registration(self):
        with patch.object(background, "run_lock", side_effect=RuntimeError("foreground runner")):
            with self.assertRaisesRegex(RuntimeError, "foreground"): self.manager.start()
        self.assertEqual(self.scheduler.calls, [])

    def test_reused_pid_does_not_count_as_the_original_process(self):
        self.manager.start(timeout=0.2)
        self.inspector.return_value["process_created"] = 101
        self.assertFalse(self.manager.status()["process_alive"])

    def test_status_separates_process_heartbeat_from_stale_loop_progress(self):
        self.manager.start(timeout=0.2)
        state = self.runtime / "cycle-state.json"
        os.utime(state, (time.time() - 120, time.time() - 120))
        result = self.manager.status()
        self.assertTrue(result["healthy"])
        self.assertLess(result["heartbeat_age_seconds"], 5)
        self.assertGreater(result["state_age_seconds"], 110)

    def test_stop_requests_pause_and_waits_without_killing_any_process(self):
        self.manager.start(timeout=0.2)
        def graceful(_):
            health = read_json(self.manager.health_path)
            health.update(status="stopped", exit_code=0)
            write_json(self.manager.health_path, health)
        self.manager.sleep = graceful
        result = self.manager.stop(timeout=0.2)
        self.assertEqual(result["status"], "stopped")
        self.assertTrue((self.runtime / "PAUSE").exists())
        self.assertEqual([call[0] for call in self.scheduler.calls], ["register", "run"])

    def test_worker_redirects_before_import_and_persists_utf8_errors(self):
        checks = []
        def imported(name):
            checks.append((name, Path(background.sys.stdout.name).name, Path(background.sys.stderr.name).name))
            def relay_main(argv):
                self.assertEqual(argv, ["--config", str(self.config), "run", "--send"])
                print("中文日志")
                raise RuntimeError("测试异常完整保留")
            return SimpleNamespace(main=relay_main)
        with patch.object(background.importlib, "import_module", side_effect=imported), patch.object(background.faulthandler, "enable"), patch.object(background.faulthandler, "disable"):
            result = background.worker(self.config, "fake-instance", inspector=lambda: dict(self.identity), interval=0.01)
        self.assertEqual(result, 1)
        self.assertEqual(checks, [("desktop_relay", "auto-run.log", "auto-run.err.log")])
        self.assertIn("中文日志", (self.runtime / "auto-run.log").read_text(encoding="utf-8"))
        self.assertIn("测试异常完整保留", (self.runtime / "auto-run.err.log").read_text(encoding="utf-8"))
        health = read_json(self.manager.health_path)
        self.assertEqual(health["status"], "stopped")
        self.assertEqual(health["exit_code"], 1)
        self.assertIn("测试异常完整保留", health["last_error"])
        self.assertIsNotNone(health["exited_at"])

    def test_worker_heartbeat_advances_while_loop_state_is_unchanged(self):
        observed = []
        def relay_main(_):
            first = read_json(self.manager.health_path)["heartbeat_epoch"]
            time.sleep(0.04)
            observed.append(read_json(self.manager.health_path)["heartbeat_epoch"] > first)
            return 0
        with patch.object(background.faulthandler, "enable"), patch.object(background.faulthandler, "disable"):
            self.assertEqual(background.worker(self.config, relay_main=relay_main, inspector=lambda: dict(self.identity), interval=0.01), 0)
        self.assertEqual(observed, [True])

    def test_cli_accepts_config_before_or_after_command(self):
        manager = Mock()
        manager.status.return_value = {"process_alive": False}
        with patch.object(background, "BackgroundManager", return_value=manager), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(background.main(["--config", str(self.config), "status"]), 0)
            self.assertEqual(background.main(["status", "--config", str(self.config)]), 0)


class SchedulerRegistrationTests(unittest.TestCase):
    def scheduler(self, tasks=()):
        collection = SimpleNamespace(Count=len(tasks), Item=lambda index: tasks[index - 1])
        folder = SimpleNamespace(GetTasks=Mock(return_value=collection), GetTask=Mock(side_effect=RuntimeError("wrapped DISP_E_EXCEPTION")), RegisterTaskDefinition=Mock())
        action = SimpleNamespace()
        definition = SimpleNamespace(RegistrationInfo=SimpleNamespace(), Principal=SimpleNamespace(), Settings=SimpleNamespace(),
                                     Actions=SimpleNamespace(Create=Mock(return_value=action)), Triggers=SimpleNamespace(Count=0))
        scheduler = object.__new__(background.TaskScheduler)
        scheduler.folder, scheduler.service = folder, SimpleNamespace(NewTask=Mock(return_value=definition))
        return scheduler, folder, definition, action

    def test_missing_task_uses_bounded_enumeration_not_wrapped_gettask(self):
        scheduler, folder, _, _ = self.scheduler()
        self.assertIsNone(scheduler.info("GPTDSHRelay-missing"))
        folder.GetTask.assert_not_called()
        folder.GetTasks.assert_called_once_with(1)

    def test_registration_is_visible_interactive_least_privilege_without_triggers_or_password(self):
        scheduler, folder, definition, action = self.scheduler()
        scheduler.register("GPTDSHRelay-fake", "owned-description", Path("C:/fake/pythonw.exe"), "fake.py worker")
        self.assertEqual(definition.Principal.LogonType, 3)
        self.assertEqual(definition.Principal.RunLevel, 0)
        self.assertFalse(definition.Settings.Hidden)
        self.assertEqual(definition.Settings.ExecutionTimeLimit, "PT0S")
        self.assertEqual(definition.Settings.MultipleInstances, 2)
        self.assertEqual(definition.Triggers.Count, 0)
        self.assertEqual(action.Path, "C:\\fake\\pythonw.exe" if os.name == "nt" else "C:/fake/pythonw.exe")
        args = folder.RegisterTaskDefinition.call_args.args
        self.assertEqual(args[2], 6)
        self.assertIsNone(args[4])
        self.assertEqual(args[5], 3)

    def test_foreign_owned_name_is_never_overwritten(self):
        task = SimpleNamespace(Name="GPTDSHRelay-fake", State=3, LastTaskResult=0, LastRunTime="never",
                               Definition=SimpleNamespace(RegistrationInfo=SimpleNamespace(Source="someone else", Description="foreign")))
        scheduler, folder, _, _ = self.scheduler([task])
        with self.assertRaisesRegex(RuntimeError, "拒绝覆盖"):
            scheduler.register("GPTDSHRelay-fake", "ours", Path("C:/fake/pythonw.exe"), "fake")
        folder.RegisterTaskDefinition.assert_not_called()


if __name__ == "__main__":
    unittest.main()
