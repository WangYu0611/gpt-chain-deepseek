"""Offline independent-worker isolation, durable approval and promotion tests."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from unattended.art_worker import ArtWorker, ArtWorkerManager
from unattended.storage import lock, read_json, write_json
from tests import test_unattended_art_engine as fixtures


class ArtWorkerTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.ArtEngineTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.f = fixture
        self.config = deepcopy(fixture.config)
        self.config["checks"] = []
        self.worker = ArtWorker(self.config, fixture.providers, now=lambda: fixture.now)
        (fixture.work / ".git").mkdir()
        self.common = patch("unattended.art_worker.gitops.common_dir", return_value=fixture.work / ".git").start()
        self.git = patch("unattended.art_worker.gitops.git", return_value=self.config["branch"]).start()
        self.ensure = patch("unattended.engine.gitops.ensure_workspace", side_effect=AssertionError("Art must not clone game")).start()

    def pending(self):
        self.worker.enqueue(fixtures.TASK)
        for expected in ("CODE", "ART_CHECK", "ART_REVIEW", "PLAN"):
            result = self.worker.step()
            self.assertEqual(result["phase"], expected, result)
        return read_json(self.worker.state_path)["art_pending"][0]

    def accept(self):
        pending = self.pending()
        self.worker.resolve(fixtures.TASK, "accepted", "用户通过三级样板", pending["review_package_sha256"])
        result = self.worker.step()
        self.assertEqual(result["status"], "idle", result)
        return pending, result["promotions"]["requests"][0]

    def test_idle_never_clones_checks_git_or_invokes_any_planner(self):
        for _ in range(2):
            result = self.worker.step()
            self.assertEqual(result["status"], "idle")
        self.assertFalse(self.worker.state_path.exists())
        self.ensure.assert_not_called()
        self.common.assert_not_called()
        self.git.assert_not_called()
        self.f.providers.plan.assert_not_called()
        self.f.providers.code.assert_not_called()

    def test_full_art_lane_uses_fixed_check_and_waits_without_normal_planning(self):
        self.pending()
        self.assertEqual(self.worker.step()["status"], "awaiting_approval")
        self.assertEqual(self.worker.promotion_queue()["requests"], [])
        self.assertEqual(read_json(self.worker.state_path)["accepted"], [])
        self.assertEqual(read_json(self.worker.state_path)["sequence"], 1)
        self.f.providers.plan.assert_not_called()
        self.ensure.assert_not_called()

    def test_acceptance_automatically_emits_one_bound_promotion_without_game_writes(self):
        pending, request = self.accept()
        self.assertEqual(request["kind"], "approved_art")
        self.assertEqual(request["status"], "queued")
        self.assertEqual(request["source_task_id"], fixtures.TASK)
        self.assertEqual(request["batch_id"], "HOUSE-COMMON-001")
        self.assertEqual(request["approved_levels"], ["L1", "L4", "L8"])
        self.assertEqual(request["review_package_sha256"], pending["review_package_sha256"])
        self.assertEqual(request["approval"]["decision"], "accepted")
        self.assertEqual(request["technical_review"]["decision"], "accepted")
        self.assertFalse(Path(self.config["project_path"]).exists())
        for _ in range(2):
            ArtWorker(self.config, self.f.providers, now=lambda: self.f.now).step()
        self.assertEqual(len(self.worker.promotion_queue()["requests"]), 1)
        self.f.providers.plan.assert_not_called()

    def test_consumer_progress_is_preserved_on_worker_restart(self):
        self.accept()
        queue = self.worker.promotion_queue()
        queue["requests"][0].update(status="integrated", integration_receipt="consumer-owned-receipt")
        write_json(self.worker.runtime / "promotion-requests.json", queue)
        self.worker.step()
        self.assertEqual(self.worker.promotion_queue(), queue)

    def test_crash_between_acceptance_and_promotion_enqueue_recovers_without_model_replay(self):
        pending = self.pending()
        self.worker.resolve(fixtures.TASK, "accepted", "通过", pending["review_package_sha256"])
        with patch.object(self.worker, "_emit_promotions", side_effect=OSError("simulated queue write failure")):
            with self.assertRaises(OSError):
                self.worker.step()
        self.assertEqual(len(read_json(self.worker.state_path)["art_history"]), 1)
        self.assertEqual(self.worker.promotion_queue()["requests"], [])
        self.worker.step()
        self.assertEqual(len(self.worker.promotion_queue()["requests"]), 1)
        self.f.providers.code.assert_called_once()
        self.f.providers.review.assert_called_once()

    def test_rework_never_emits_promotion_and_keeps_same_art_session(self):
        pending = self.pending()
        self.worker.resolve(fixtures.TASK, "rework", "修正庭院布局", pending["review_package_sha256"])
        result = self.worker.step()
        self.assertEqual(result["phase"], "CODE")
        self.assertEqual(self.worker.promotion_queue()["requests"], [])
        self.assertEqual(read_json(self.worker.state_path)["task"]["session_id"], fixtures.SESSION)

    def test_worker_locks_exclude_duplicate_and_legacy_runner_without_main_repo_lock(self):
        for name in ("art-worker.lock", "controller.lock", "claude-runner.lock"):
            with self.subTest(name=name), lock(self.worker.runtime / name):
                with self.assertRaises(RuntimeError):
                    self.worker.step()
        self.common.assert_not_called()
        self.assertFalse(self.worker.state_path.exists())

    def test_status_is_read_only_and_legacy_or_game_state_is_not_taken_over(self):
        self.assertEqual(self.worker.status()["status"], "idle")
        self.assertFalse((self.worker.runtime / "art-worker.lock").exists())
        write_json(self.worker.state_path, {"version": 1, "phase": "CODE", "task": {"id": fixtures.TASK}})
        before = self.worker.state_path.read_bytes()
        with self.assertRaises(ValueError):
            self.worker.step()
        self.assertEqual(self.worker.state_path.read_bytes(), before)
        state = {"version": 1, "binding": self.worker._binding(),
            "phase": "CODE", "task": {"id": "TASK-GAME-1", "kind": "code"}, "operation": None}
        write_json(self.worker.state_path, state)
        with self.assertRaises(ValueError):
            self.worker.status()

    def test_active_dispatch_requires_own_existing_git_branch(self):
        self.worker.enqueue(fixtures.TASK)
        self.git.return_value = "main"
        result = self.worker.step()
        self.assertIn("工作分支", result["last_error"])
        self.f.providers.code.assert_not_called()
        self.ensure.assert_not_called()

    def test_separate_main_config_route_rejects_any_runtime_or_project_overlap(self):
        main = {"project_path": str(self.f.root / "game"), "runtime_dir": str(self.f.root / "main-runtime"),
                "art_worker": {"config_file": str(self.f.root / "worker.json")}}
        with patch("unattended.art_worker.load_config", return_value=self.config):
            result = ArtWorker.from_main_config(main, self.f.providers)
            self.assertEqual(result.runtime, self.worker.runtime)
        bad = deepcopy(self.config)
        bad["runtime_dir"] = main["runtime_dir"]
        with patch("unattended.art_worker.load_config", return_value=bad), self.assertRaises(ValueError):
            ArtWorker.from_main_config(main)

    def test_publication_and_recursive_worker_configs_are_rejected(self):
        for extra in ({"push": True}, {"publication": {"mode": "milestone"}},
                      {"art_worker": {"config_file": "unrelated.json"}}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                ArtWorker({**self.config, **extra})

    def test_manager_registers_art_entry_only_and_preserves_supervisor_contract(self):
        manager = object.__new__(ArtWorkerManager)
        manager.config = self.config
        manager.config_path = Path(self.config["config_path"])
        manager.name, manager.description = "art-worker-task", "test-binding"
        manager.scheduler = Mock()
        with patch("unattended.art_worker.Path.is_file", return_value=True):
            manager._registration(logon=True)
        call = manager.scheduler.register.call_args
        self.assertIn("unattended.art_worker", call.args[3])
        self.assertIn("worker", call.args[3])
        self.assertNotIn("background worker", call.args[3])
        self.assertTrue(call.kwargs["logon"])

    def test_persistent_run_waits_idle_instead_of_exiting_or_busy_polling(self):
        class StopTestLoop(BaseException):
            pass
        with patch("unattended.art_worker.time.sleep", side_effect=StopTestLoop) as sleep:
            with self.assertRaises(StopTestLoop):
                self.worker.run()
        sleep.assert_called_once_with(self.config["poll_seconds"])
        self.f.providers.plan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
