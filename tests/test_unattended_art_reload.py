"""No models/processes are launched; real Windows marker ownership is exercised."""
from copy import deepcopy
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

from tests import test_unattended_art_engine as fixtures
from unattended import art_reload
from unattended.art_worker import ArtWorkerManager
from unattended.storage import lock, read_json, write_json, write_text


class Scheduler:
    def __init__(self, manager):
        self.manager = manager
        self.calls = []
        self.state = 4
        self.foreign = False
        self.on_run = lambda: None

    def _owned(self, name, description):
        if self.foreign:
            raise RuntimeError("foreign scheduler registration")

    def stop(self, name, description):
        self._owned(name, description)
        self.calls.append("stop")
        self.manager.alive = False
        self.state = 3

    def run(self, name):
        self.calls.append("run")
        self.manager.identity = {"pid": 456, "process_created": 789, "image": "owned-pythonw.exe"}
        self.manager.alive = self.manager.healthy = True
        self.state = 4
        self.on_run()


class Manager:
    _stop_paused_for_reload = ArtWorkerManager._stop_paused_for_reload
    _start_paused_for_reload = ArtWorkerManager._start_paused_for_reload

    def __init__(self, fixture):
        self.config = fixture.config
        self.config_path = Path(self.config["config_path"]).resolve()
        write_json(self.config_path, self.config)
        self.runtime = Path(self.config["runtime_dir"]).resolve()
        self.name, self.description = "isolated-art-host", "owned-art-registration"
        self.identity = {"pid": 123, "process_created": 321, "image": "owned-pythonw.exe"}
        self.alive = self.healthy = True
        self.scheduler = Scheduler(self)

    def _scheduler(self):
        return self.scheduler

    def _registration(self):
        self.scheduler.calls.append("register")
        self.scheduler.state = 3

    def status(self):
        return {"process_alive": self.alive, "healthy": self.alive and self.healthy,
                "health": dict(self.identity), "task": {"state": self.scheduler.state}}


@unittest.skipUnless(os.name == "nt", "Art host and atomic pause guard require Windows")
class ArtReloadTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ArtEngineTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.dispatch()
        self.manager = Manager(self.f)
        self.clock = time.time()
        fingerprint = patch("unattended.art_reload._code_fingerprint", return_value={"engine.py": "approved-code"})
        self.code = fingerprint.start()
        self.addCleanup(fingerprint.stop)
        self.pause = self.manager.runtime / "PAUSE"
        self.state_before = self.f.engine.state_path.read_bytes()
        self.config_before = self.manager.config_path.read_bytes()
        self.manager.scheduler.on_run = lambda: self.paused_heartbeat(
            art_reload.reload_status(self.manager.config_path, manager=self.manager))

    def now(self):
        return self.clock

    def request(self, **kwargs):
        return art_reload.request_reload(self.manager.config_path, manager=self.manager, launch=False,
                                         now=self.now, **kwargs)

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
        self.advance(request)  # Claim pause while the original CODE is still active.
        self.paused_heartbeat(request)
        self.advance(request)  # The completed main-loop boundary becomes durable.
        self.advance(request)  # Stop only the paused original host.
        self.advance(request)  # Start only the replacement, still paused.
        self.assertEqual(request["status"], "starting")

    def test_request_is_durable_idempotent_and_does_not_pause_or_start_a_model(self):
        request = self.request()
        again = self.request()
        self.assertEqual(request["id"], again["id"])
        self.assertFalse(self.pause.exists())
        self.assertEqual(self.manager.scheduler.calls, [])
        self.assertEqual(self.f.engine.state_path.read_bytes(), self.state_before)
        self.f.providers.code.assert_not_called()
        self.assertEqual(art_reload.reload_status(self.manager.config_path, manager=self.manager)["id"], request["id"])

    def test_active_code_review_and_check_never_stop_on_supervisor_or_stale_heartbeat(self):
        request = self.request()
        self.advance(request)
        for phase in ("CODE", "ART_REVIEW", "ART_CHECK", "ART_DIAGNOSE"):
            state = read_json(self.f.engine.state_path)
            state["phase"] = phase
            write_json(self.f.engine.state_path, state)
            write_json(self.manager.runtime / "background-health.json", {"status": "running", "heartbeat_epoch": self.clock})
            self.advance(request)
            self.paused_heartbeat(request, heartbeat_at=art_reload._iso(request["pause"]["boundary_epoch"] - 1))
            self.advance(request)
            self.paused_heartbeat(request, updated_at="foreign-state")
            self.advance(request)
            self.paused_heartbeat(request, task_id="another-task")
            self.advance(request)
            self.assertEqual(self.manager.scheduler.calls, [])
            self.assertEqual(request["status"], "waiting_boundary")

    def test_success_restarts_only_after_a_matching_paused_loop_and_preserves_every_task_byte(self):
        request = self.request()
        self.reach_starting(request)
        self.assertTrue(self.pause.exists())
        self.assertEqual(self.manager.scheduler.calls, ["stop", "register", "run"])
        self.advance(request)
        self.assertEqual(request["status"], "completed")
        self.assertTrue(request["owned_pause_released"])
        self.assertFalse(self.pause.exists())
        self.assertNotEqual(request["source_identity"], request["replacement_identity"])
        self.assertEqual(self.f.engine.state_path.read_bytes(), self.state_before)
        self.assertEqual(self.manager.config_path.read_bytes(), self.config_before)
        self.f.providers.code.assert_not_called()
        self.f.providers.review.assert_not_called()

    def test_preexisting_or_replaced_user_pause_is_never_cleared(self):
        write_text(self.pause, "user paused before reload\n")
        request = self.request()
        self.reach_starting(request)
        self.advance(request)
        self.assertEqual(request["status"], "completed")
        self.assertEqual(self.pause.read_text(), "user paused before reload\n")
        self.assertFalse(request["owned_pause_released"])
        request = self.request()
        self.advance(request)
        write_text(self.pause, "a newer user pause\n")
        self.advance(request)
        self.assertEqual(request["pause"]["mode"], "user")
        self.paused_heartbeat(request)
        for _ in range(4): self.advance(request)
        self.assertEqual(self.pause.read_text(), "a newer user pause\n")

    def test_user_resume_cancels_wait_without_stopping_the_active_provider(self):
        request = self.request()
        self.advance(request)
        self.pause.unlink()
        self.advance(request)
        self.assertEqual(request["status"], "cancelled")
        self.assertEqual(self.manager.scheduler.calls, [])
        self.assertFalse(self.pause.exists())

    def test_native_guard_prevents_marker_replacement_between_boundary_check_and_stop(self):
        request = self.request()
        self.advance(request)
        with art_reload._pause_guard(self.manager, request) as held:
            self.assertTrue(held)
            with self.assertRaises(OSError): self.pause.unlink()
        write_text(self.pause, "user replaced marker after guard\n")
        self.assertFalse(art_reload._release_owned_pause(self.manager, request))
        self.assertEqual(self.pause.read_text(), "user replaced marker after guard\n")

    def test_identity_change_config_change_and_unknown_start_never_stop_or_start_twice(self):
        request = self.request()
        self.advance(request)
        self.manager.identity["process_created"] += 1
        self.advance(request)
        self.assertEqual(request["status"], "superseded")
        self.assertEqual(self.manager.scheduler.calls, [])
        request = self.request()
        self.advance(request)
        self.code.return_value = {"engine.py": "different-code"}
        self.advance(request)
        self.assertEqual(request["status"], "cancelled")
        self.assertEqual(self.manager.scheduler.calls, [])
        request = self.request()
        self.reach_starting(request)
        # A crash after durable start intent cannot establish whether the
        # scheduler received the launch. Neither idle nor unhealthy restarts it.
        self.manager.alive = False
        self.manager.scheduler.state = 3
        for _ in range(3): self.advance(request)
        self.assertEqual(request["status"], "starting")
        self.assertEqual(self.manager.scheduler.calls, ["stop", "register", "run"])

    def test_recovery_after_launch_recognizes_new_identity_without_resubmitting(self):
        request = self.request()
        self.reach_starting(request)
        request.pop("launch_submitted_at")  # Simulate death after scheduler Run, before this receipt.
        art_reload._save(self.manager, request, now=self.now)
        recovered = art_reload.run_helper(self.manager.config_path, request["id"], manager=self.manager,
                                           now=self.now, sleep=lambda _: None)
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(self.manager.scheduler.calls, ["stop", "register", "run"])

    def test_timeout_during_model_only_releases_owned_pause_but_unknown_start_stays_paused(self):
        request = self.request(timeout_seconds=30)
        self.advance(request)
        self.clock = request["deadline_epoch"]
        self.advance(request)
        self.assertEqual(request["status"], "timed_out")
        self.assertFalse(self.pause.exists())
        self.assertEqual(self.manager.scheduler.calls, [])
        request = self.request(timeout_seconds=30)
        self.reach_starting(request)
        self.manager.healthy = False
        self.clock = request["deadline_epoch"]
        self.advance(request)
        self.assertEqual(request["status"], "timed_out")
        self.assertTrue(self.pause.exists())

    def test_missing_art_binding_stopped_worker_and_foreign_registration_are_rejected(self):
        self.manager.alive = False
        with self.assertRaisesRegex(RuntimeError, "尚未运行"): self.request()
        self.manager.alive = True
        self.manager.scheduler.foreign = True
        with self.assertRaisesRegex(RuntimeError, "foreign"): self.request()
        self.manager.scheduler.foreign = False
        original = self.manager.config.pop("art_pipeline")
        with self.assertRaisesRegex(ValueError, "独立 Claude"): self.request()
        self.manager.config["art_pipeline"] = original
        self.assertFalse(self.pause.exists())
        self.assertEqual(self.manager.scheduler.calls, [])

    def test_helper_lock_and_request_binding_prevent_parallel_or_foreign_coordination(self):
        request = self.request()
        with lock(self.manager.runtime / "art-reload-helper.lock"):
            self.assertEqual(art_reload.run_helper(self.manager.config_path, request["id"], manager=self.manager),
                             {"status": "helper_already_running", "request_id": request["id"]})
        with self.assertRaises(ValueError):
            art_reload.run_helper(self.manager.config_path, "../state", manager=self.manager)
        request["runtime_dir"] = str(self.f.root / "main-game")
        write_json(art_reload._request_path(self.manager.runtime, request["id"]), request)
        with self.assertRaisesRegex(RuntimeError, "绑定"):
            art_reload.run_helper(self.manager.config_path, request["id"], manager=self.manager)
        self.assertFalse(self.pause.exists())

    def test_hidden_helper_launch_cannot_overwrite_a_faster_helpers_progress(self):
        def launched(manager, request_id):
            request = art_reload._read_request(manager, request_id)
            self.advance(request)
            return {"task_name": "isolated-art-host-Reload", "status": "submitted"}
        with patch("unattended.art_reload._launch_helper", side_effect=launched):
            request = art_reload.request_reload(self.manager.config_path, manager=self.manager, now=self.now)
        self.assertEqual(request["status"], "waiting_boundary")
        self.assertEqual(request["pause"]["mode"], "owned")
        self.assertTrue(self.pause.exists())

    def test_helper_uses_a_separately_owned_one_shot_scheduler_task_not_a_child_process(self):
        request = self.request()
        scheduler = Mock()
        scheduler._owned.return_value = None
        self.manager.scheduler = scheduler
        executable = self.f.root / "python.exe"
        executable.with_name("pythonw.exe").write_text("fixture; never executed", encoding="utf-8")
        with patch("unattended.art_reload.sys.executable", str(executable)), \
             patch("unattended.art_reload.subprocess.Popen", side_effect=AssertionError("must not inherit a parent job")):
            receipt = art_reload._launch_helper(self.manager, request["id"])
        name, description, pythonw, arguments = scheduler.register.call_args.args
        self.assertEqual(name, "isolated-art-host-Reload")
        self.assertNotEqual(name, self.manager.name)
        self.assertEqual(json.loads(description)["worker_description"], self.manager.description)
        self.assertEqual(pythonw, executable.with_name("pythonw.exe"))
        self.assertIn("reload-worker", arguments)
        self.assertIn(request["id"], arguments)
        self.assertEqual(scheduler.register.call_args.kwargs, {"logon": False})
        scheduler.run.assert_called_once_with(name)
        self.assertEqual(receipt, {"task_name": name, "status": "submitted"})
        scheduler.reset_mock()
        scheduler._owned.return_value = {"state": 4, "logon_trigger": False}
        with patch("unattended.art_reload.sys.executable", str(executable)):
            self.assertEqual(art_reload._launch_helper(self.manager, request["id"])["status"], "already_running")
        scheduler.run.assert_not_called()
        scheduler.register.assert_not_called()

    def test_repeated_request_cannot_overwrite_concurrent_helpers_new_pause_ownership(self):
        request = self.request()
        stale = deepcopy(request)
        def observed_while_helper_progressed(*args, **kwargs):
            self.advance(request)
            return stale
        with patch("unattended.art_reload.reload_status", side_effect=observed_while_helper_progressed), \
             patch("unattended.art_reload._launch_helper", return_value={"status": "already_running"}):
            result = art_reload.request_reload(self.manager.config_path, manager=self.manager, now=self.now)
        self.assertEqual(result["id"], request["id"])
        self.assertEqual(result["status"], "waiting_boundary")
        self.assertEqual(result["pause"]["mode"], "owned")
        self.assertEqual(self.manager.scheduler.calls, [])

    def test_new_supervisor_health_without_a_new_paused_loop_cannot_release_pause(self):
        self.manager.scheduler.on_run = lambda: None
        request = self.request()
        self.reach_starting(request)
        self.advance(request)
        self.assertTrue(self.manager.healthy)
        self.assertEqual(request["status"], "starting")
        self.assertTrue(self.pause.exists())
        self.paused_heartbeat(request)
        self.advance(request)
        self.assertEqual(request["status"], "completed")

    def test_completion_receipt_recovers_after_owned_marker_was_already_released(self):
        request = self.request()
        self.reach_starting(request)
        request.update(status="completing", replacement_identity=dict(self.manager.identity))
        art_reload._save(self.manager, request, now=self.now)
        self.assertTrue(art_reload._release_owned_pause(self.manager, request))
        result = art_reload.run_helper(self.manager.config_path, request["id"], manager=self.manager, now=self.now)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.manager.scheduler.calls, ["stop", "register", "run"])


if __name__ == "__main__":
    unittest.main()
