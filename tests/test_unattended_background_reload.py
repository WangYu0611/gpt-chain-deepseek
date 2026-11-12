"""Main-host reload uses fresh paused loop evidence, never kills active work.

The scheduler and model are mocked. Pause ownership uses real Windows handles.
"""
from copy import deepcopy
import contextlib
import io
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

from unattended import art_reload, background_reload, cli
from unattended.background import BackgroundManager
from unattended.storage import lock, read_json, write_json, write_text
from tests import test_unattended_art_engine as fixtures
from tests.test_unattended_art_reload import Manager, Scheduler


class MainScheduler(Scheduler):
    def _owned(self, name, description):
        super()._owned(name, description)
        return {"state": self.state, "logon_trigger": True}


class MainManager(Manager):
    _stop_paused_for_reload = BackgroundManager._stop_paused_for_reload
    _start_paused_for_reload = BackgroundManager._start_paused_for_reload

    def __init__(self, fixture):
        super().__init__(fixture)
        self.name, self.description = "owned-development-host", "owned-development-registration"
        self.scheduler = MainScheduler(self)


@unittest.skipUnless(os.name == "nt", "Reload pause ownership uses Windows file handles")
class BackgroundReloadTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ArtEngineTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.dispatch()
        self.f.config.pop("art_pipeline")
        state = read_json(self.f.engine.state_path)
        state["task"].update(id="TASK-DEV-0001", kind="code", session_id="original-production-session")
        self.f.engine._save(state)
        self.manager = MainManager(self.f)
        self.clock = time.time()
        self.pause = self.manager.runtime / "PAUSE"
        self.state_before = self.f.engine.state_path.read_bytes()
        self.config_before = self.manager.config_path.read_bytes()
        self.real_fingerprint = art_reload._code_fingerprint
        fingerprint = patch("unattended.art_reload._code_fingerprint", return_value={"engine.py": "new-approved-host"})
        self.code = fingerprint.start()
        self.addCleanup(fingerprint.stop)
        self.manager.scheduler.on_run = lambda: self.paused_heartbeat(
            background_reload.reload_status(self.manager.config_path, manager=self.manager))

    def now(self):
        return self.clock

    def request(self, **kwargs):
        return background_reload.request_reload(self.manager.config_path, manager=self.manager,
            launch=False, now=self.now, **kwargs)

    def advance(self, request):
        self.clock += 1
        return art_reload._advance(self.manager, request, now=self.now)

    def paused_heartbeat(self, request, **overrides):
        self.clock = max(self.clock, request["pause"]["boundary_epoch"]) + 1
        state = read_json(self.f.engine.state_path)
        value = {"paused": True, "status": "paused", "heartbeat_at": art_reload._iso(self.clock),
                 "phase": state["phase"], "task_id": state["task"]["id"], "updated_at": state["updated_at"],
                 "runtime_dir": str(self.manager.runtime), "workspace": str(self.manager.runtime / "workspace")}
        value.update(overrides)
        path = self.manager.runtime / "heartbeat.json"
        write_json(path, value)
        os.utime(path, (self.clock, self.clock))

    def reach_starting(self, request):
        self.advance(request)
        self.paused_heartbeat(request)
        for _ in range(3): self.advance(request)
        self.assertEqual(request["status"], "starting")

    def test_request_is_main_only_durable_idempotent_and_never_touches_task_or_model(self):
        request = self.request()
        self.assertEqual(request["lane"], "development")
        self.assertEqual(self.request()["id"], request["id"])
        self.assertEqual(read_json(self.manager.runtime / "background-reload-request.json")["id"], request["id"])
        self.assertFalse((self.manager.runtime / "art-reload-request.json").exists())
        self.assertFalse(self.pause.exists())
        self.assertEqual(self.manager.scheduler.calls, [])
        self.assertEqual(self.f.engine.state_path.read_bytes(), self.state_before)
        self.f.providers.code.assert_not_called()

    def test_code_check_review_and_replan_need_fresh_matching_main_loop_not_supervisor(self):
        request = self.request()
        self.advance(request)
        self.assertTrue(self.pause.read_text().startswith("background-reload:"))
        for phase in ("CODE", "CHECK", "REVIEW", "REPLAN"):
            state = read_json(self.f.engine.state_path)
            state["phase"] = phase
            write_json(self.f.engine.state_path, state)
            write_json(self.manager.runtime / "background-health.json", {"status": "running", "heartbeat_epoch": self.clock})
            self.advance(request)
            for mismatch in ({"heartbeat_at": art_reload._iso(request["pause"]["boundary_epoch"] - 1)},
                             {"updated_at": "old-state"}, {"task_id": "foreign-task"}, {"phase": "PLAN"}):
                self.paused_heartbeat(request, **mismatch)
                self.advance(request)
            self.assertEqual(request["status"], "waiting_boundary")
            self.assertEqual(self.manager.scheduler.calls, [])

    def test_complete_reload_preserves_checkpoint_and_config_byte_for_byte(self):
        request = self.request()
        self.reach_starting(request)
        self.assertTrue(self.pause.exists())
        self.advance(request)
        self.assertEqual(request["status"], "completed")
        self.assertNotEqual(request["source_identity"], request["replacement_identity"])
        self.assertEqual(self.manager.scheduler.calls, ["stop", "register", "run"])
        self.assertFalse(self.pause.exists())
        self.assertEqual(self.f.engine.state_path.read_bytes(), self.state_before)
        self.assertEqual(self.manager.config_path.read_bytes(), self.config_before)
        self.f.providers.code.assert_not_called()
        self.f.providers.review.assert_not_called()

    def test_original_timeout_can_reach_paused_blocked_checkpoint_without_helper_retrying_it(self):
        request = self.request()
        self.advance(request)
        state = read_json(self.f.engine.state_path)
        state.update(phase="BLOCKED", blocked_from="CODE", failures=3, last_error="Provider exceeded its time limit")
        write_json(self.f.engine.state_path, state)
        expected = self.f.engine.state_path.read_bytes()
        self.paused_heartbeat(request)
        for _ in range(4): self.advance(request)
        self.assertEqual(request["status"], "completed")
        self.assertEqual(self.f.engine.state_path.read_bytes(), expected)
        self.assertEqual(read_json(self.f.engine.state_path)["task"]["session_id"], "original-production-session")

    def test_preexisting_and_replaced_user_pause_survive_reload(self):
        write_text(self.pause, "user-paused")
        request = self.request()
        self.reach_starting(request)
        self.advance(request)
        self.assertEqual(request["pause"]["mode"], "user")
        self.assertEqual(self.pause.read_text(), "user-paused")
        self.assertFalse(request["owned_pause_released"])
        request = self.request()
        self.advance(request)
        write_text(self.pause, "new-user-pause")
        self.advance(request)
        self.paused_heartbeat(request)
        for _ in range(4): self.advance(request)
        self.assertEqual(self.pause.read_text(), "new-user-pause")

    def test_source_or_code_changes_cancel_without_stopping_foreign_or_current_native_work(self):
        request = self.request()
        self.advance(request)
        self.manager.identity["process_created"] += 1
        self.advance(request)
        self.assertEqual(request["status"], "superseded")
        self.assertEqual(self.manager.scheduler.calls, [])
        request = self.request()
        self.advance(request)
        self.code.return_value = {"engine.py": "changed-after-request"}
        self.advance(request)
        self.assertEqual(request["status"], "cancelled")
        self.assertEqual(self.manager.scheduler.calls, [])

    def test_new_healthy_supervisor_without_fresh_paused_loop_never_releases_marker(self):
        self.manager.scheduler.on_run = lambda: None
        request = self.request()
        self.reach_starting(request)
        self.advance(request)
        self.assertEqual(request["status"], "starting")
        self.assertTrue(self.pause.exists())
        self.paused_heartbeat(request)
        self.advance(request)
        self.assertEqual(request["status"], "completed")

    def test_unknown_launch_never_resubmits_and_timeout_keeps_paused_checkpoint(self):
        request = self.request(timeout_seconds=30)
        self.reach_starting(request)
        self.manager.alive = False
        self.manager.scheduler.state = 3
        for _ in range(3): self.advance(request)
        self.assertEqual(request["status"], "starting")
        self.clock = request["deadline_epoch"]
        self.advance(request)
        self.assertEqual(request["status"], "timed_out")
        self.assertTrue(self.pause.exists())
        self.assertEqual(self.manager.scheduler.calls, ["stop", "register", "run"])

    def test_replacement_identity_change_before_completion_keeps_pause(self):
        request = self.request()
        self.reach_starting(request)
        request.update(status="completing", replacement_identity=dict(self.manager.identity))
        self.manager.identity["process_created"] += 1
        self.advance(request)
        self.assertEqual(request["status"], "blocked")
        self.assertTrue(self.pause.exists())

    def test_helper_recovery_after_launch_or_marker_release_never_starts_twice(self):
        request = self.request()
        self.reach_starting(request)
        request.pop("launch_submitted_at")
        art_reload._save(self.manager, request, now=self.now)
        result = background_reload.run_helper(self.manager.config_path, request["id"], manager=self.manager,
            now=self.now, sleep=lambda _: None)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.manager.scheduler.calls, ["stop", "register", "run"])
        request.update(status="completing", replacement_identity=dict(self.manager.identity))
        art_reload._save(self.manager, request, now=self.now)
        result = background_reload.run_helper(self.manager.config_path, request["id"], manager=self.manager,
            now=self.now, sleep=lambda _: None)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.manager.scheduler.calls, ["stop", "register", "run"])

    def test_reject_art_config_or_foreign_scheduler_and_keep_main_helper_lock_separate(self):
        self.manager.config["art_pipeline"] = {"task_ids": ["art"]}
        with self.assertRaisesRegex(ValueError, "美术"): self.request()
        self.manager.config.pop("art_pipeline")
        self.manager.scheduler.foreign = True
        with self.assertRaisesRegex(RuntimeError, "foreign"): self.request()
        self.manager.scheduler.foreign = False
        request = self.request()
        with lock(self.manager.runtime / "background-reload-helper.lock"):
            result = background_reload.run_helper(self.manager.config_path, request["id"], manager=self.manager)
        self.assertEqual(result["status"], "helper_already_running")
        self.assertFalse(self.pause.exists())

    def test_main_helper_is_a_separate_owned_scheduler_action_and_code_hash_covers_new_runtime(self):
        request = self.request()
        scheduler = Mock()
        scheduler._owned.return_value = None
        self.manager.scheduler = scheduler
        executable = self.f.root / "python.exe"
        executable.with_name("pythonw.exe").write_text("never executed", encoding="utf-8")
        with patch("unattended.art_reload.sys.executable", str(executable)), \
             patch("unattended.art_reload.subprocess.Popen", side_effect=AssertionError("must not inherit old job")):
            art_reload._launch_helper(self.manager, request["id"])
        name, description, _, arguments = scheduler.register.call_args.args
        self.assertEqual(name, "owned-development-host-Reload")
        self.assertEqual(json.loads(description)["owner"], "gpt_chain_deepseek.background-reload/v1")
        self.assertIn("-m unattended background reload-worker", arguments)
        self.assertNotIn("unattended.art_worker", arguments)
        self.assertEqual(scheduler.register.call_args.kwargs, {"logon": False})
        hashes = self.real_fingerprint(self.manager)
        for expected in ("task_replanning.py", "engine.py", "background.py", "background_reload.py", "cli.py",
                         "github_harness_lab/lab_harness/process.py"):
            self.assertEqual(len(hashes[expected]), 64)


class BackgroundReloadCliTests(unittest.TestCase):
    def test_reload_status_and_hidden_helper_dispatch_without_constructing_engine(self):
        path = Path("main-config.json").resolve()
        for action, function, options, kwargs in (
                ("reload", "request_reload", ["--timeout-seconds", "900"], {"timeout_seconds": 900}),
                ("reload-status", "reload_status", [], {}),
                ("reload-worker", "run_helper", ["--request-id", "a" * 32], {})):
            with self.subTest(action=action), patch.object(background_reload, function, return_value={"status": "pending"}) as call, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["background", action, "--config", str(path), *options]), 0)
                if action == "reload-worker": call.assert_called_once_with(path, "a" * 32)
                else: call.assert_called_once_with(path, **kwargs)


if __name__ == "__main__":
    unittest.main()
