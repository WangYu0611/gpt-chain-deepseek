"""Art dispatch, evidence, recovery, and human decisions in the real Engine.

All models are mocked. Independent verification runs as a real local subprocess
against temporary, explicitly synthetic evidence; no Tripo, Blender, or UE runs.
"""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from unattended import art_pipeline
from unattended.calendar import BEIJING
from unattended.engine import Engine
from unattended.storage import read_json, write_json
from tests import test_unattended_art_pipeline as fixtures


HEAD = "a" * 40
TASK = "TASK-ART-0001"
SESSION = "8fd5aeb0-aacb-42a6-b976-c36461eb374f"


class ArtEngineTests(unittest.TestCase):
    def setUp(self):
        evidence = fixtures.ArtPipelineTests()
        evidence.setUp()
        self.addCleanup(evidence.doCleanups)
        self.evidence, self.root = evidence, evidence.root
        _, report, import_file, _, _ = evidence.review_fixture()
        scripts = Path(art_pipeline.__file__).resolve().parent.parent / "art_tools" / "ue"
        imported = read_json(import_file)
        imported["script_sha256"] = fixtures.digest(scripts / "import_preview.py")
        write_json(import_file, imported)
        report["ue_import"] = imported
        report["capture"]["capture_id"] = art_pipeline.capture_fingerprint(
            imported["run"], import_file, scripts / "capture_preview.py")
        write_json(evidence.public / "receipts" / (report["capture"]["capture_id"] + "-capture.json"), report["capture"])
        write_json(evidence.public / "review-package.json", report)
        approved = self.root / "approved" / "batch.json"
        write_json(approved, evidence.config)
        calendar = self.root / "calendar.json"
        write_json(calendar, {"years": [2026], "holidays": [], "workdays": [], "overrides": {}})
        self.config = {
            "version": 1, "config_path": str(self.root / "config.json"), "project_path": str(self.root / "project"),
            "runtime_dir": str(self.root / "runtime"), "goal": "Continue ordinary game work", "branch": "unattended/test",
            "base_ref": "HEAD", "task_prefix": "TASK-TEST", "schedule": {"holidays_file": str(calendar)},
            "checks": [{"id": "main-game-check", "argv": ["must-not-run.exe"], "timeout_seconds": 30}],
            "close_policy": "strict", "step_timeout_seconds": 3600, "poll_seconds": 1,
            "max_failures": 3, "max_reworks": 3, "push": False, "remote": "origin", "codex": {},
            "coding_provider": "claude", "claude": {"model": "sonnet", "reasoning_effort": "medium",
                "ue_preflight": True, "planned_core_tests": True, "instructions": "normal game instructions",
                "test_preflight": {"enabled": True}, "visual_preflight": {"enabled": True}},
            "art_pipeline": {"config_file": str(approved), "task_ids": [TASK], "artifacts_dir": str(evidence.public)},
        }
        self.providers = Mock()
        self.providers.recover.return_value = None
        self.providers.plan.return_value = {"action": "complete", "reason": "Normal game work complete"}
        self.providers.code.return_value = {"completed": True, "session_id": SESSION, "text": "Synthetic delivery"}
        self.providers.review.return_value = {"decision": "accepted", "summary": "Synthetic technical review",
                                              "feedback": "", "head_sha": HEAD}
        self.now = datetime(2026, 11, 2, 19, tzinfo=BEIJING)
        self.engine = self.restart()
        self.work = self.engine.runtime / "workspace"
        self.work.mkdir(parents=True)
        self.addCleanup(patch.stopall)
        patch.object(Engine, "_locks", side_effect=lambda: nullcontext()).start()
        patch("unattended.engine.gitops.ensure_workspace", return_value=self.work).start()
        self.head = patch("unattended.engine.gitops.head", return_value=HEAD).start()
        self.clean = patch("unattended.engine.gitops.clean", return_value=True).start()
        self.publish = patch("unattended.engine.gitops.publish").start()

    def restart(self):
        return Engine(self.config, self.providers, now=lambda: self.now)

    def state(self):
        return read_json(self.engine.state_path)

    def dispatch(self):
        self.engine.enqueue_art(TASK)
        result = self.engine.step()
        self.assertEqual(result["phase"], "CODE", result)
        self.assertEqual(result["task_kind"], "art")
        self.providers.plan.assert_not_called()

    def checked(self):
        self.dispatch()
        self.assertEqual(self.engine.step()["phase"], "ART_CHECK")
        result = self.engine.step()
        self.assertEqual(result["phase"], "ART_REVIEW", result)

    def pending(self):
        self.checked()
        result = self.engine.step()
        self.assertEqual(result["phase"], "PLAN", result)
        return self.state()["art_pending"][0]

    def test_full_engine_lifecycle_waits_for_bound_human_acceptance_without_publishing(self):
        pending = self.pending()
        state = self.state()
        self.assertIsNone(state["task"])
        self.assertEqual(state["sequence"], 1)
        self.assertEqual(state["accepted"], [])
        self.assertEqual(self.engine.art_queue()["requests"][0]["status"], "awaiting_approval")
        check = pending["task"]["check_results"][0]
        self.assertTrue(check["passed"])
        self.assertEqual(check["argv"][-1], "verify")
        self.assertIn("art_pipeline.py", check["argv"][1])
        original = self.engine.state_path.read_bytes()
        self.engine.resolve_art(TASK, "accepted", "三级住宅样板通过", pending["review_package_sha256"])
        self.assertEqual(self.engine.state_path.read_bytes(), original)
        self.assertEqual(self.restart().step()["phase"], "DONE")
        state = self.state()
        self.assertEqual(state["art_pending"], [])
        self.assertEqual(len(state["art_history"]), 1)
        self.assertEqual(state["art_history"][0]["visual_decision"]["feedback"], "三级住宅样板通过")
        self.assertEqual(state["accepted"], [])
        self.assertEqual(state["sequence"], 1)
        self.publish.assert_not_called()
        self.providers.code.assert_called_once()
        self.providers.review.assert_called_once()

    def test_enqueue_is_separate_from_active_game_state_and_idempotent(self):
        state = self.engine._load()
        state.update(phase="CODE", task={"id": "TASK-TEST-0032", "body": "active game task"})
        self.engine._save(state)
        before = self.engine.state_path.read_bytes()
        first, duplicate = self.engine.enqueue_art(TASK), self.engine.enqueue_art(TASK)
        self.assertFalse(first["already_queued"])
        self.assertTrue(duplicate["already_queued"])
        self.assertEqual(self.engine.state_path.read_bytes(), before)
        self.assertEqual(len(self.engine.art_queue()["requests"]), 1)
        with self.assertRaises(ValueError):
            self.engine.enqueue_art(TASK, body="Changed scope")
        with self.assertRaises(ValueError):
            self.engine.enqueue_art("TASK-ART-UNAUTHORIZED")
        self.providers.code.assert_not_called()

    def test_art_has_its_own_opus_high_config_and_persisted_operation(self):
        self.dispatch()
        task = self.state()["task"]
        live = Engine(self.config)
        isolated = live._art_providers(task)
        self.assertEqual(isolated.config["claude"]["model"], "claude-opus-5-5")
        self.assertEqual(isolated.config["claude"]["reasoning_effort"], "high")
        self.assertFalse(isolated.config["claude"]["ue_preflight"])
        self.assertNotIn("test_preflight", isolated.config["claude"])
        self.assertEqual(self.config["claude"]["model"], "sonnet")
        self.providers.code.side_effect = RuntimeError("Interrupted before model result")
        self.engine.step()
        op = self.state()["operation"]
        self.assertEqual(op["coding_model"], "claude-opus-5-5")
        self.assertEqual(op["coding_reasoning_effort"], "high")

    def test_pending_art_does_not_block_normal_task_planning_or_repeat_production(self):
        pending = self.pending()
        self.providers.plan.return_value = {"action": "task", "reason": "next", "title": "Normal game task",
            "body": "Continue game feature", "acceptance_criteria": ["Feature works"], "core_tests": ["ZhuGuoCity.Example"]}
        result = self.restart().step()
        self.assertEqual(result["task_kind"], "code")
        self.assertEqual(result["task_id"], "TASK-TEST-0001")
        self.assertEqual(len(result["art_pending"]), 1)
        self.providers.code.assert_called_once()
        self.assertIn(pending["review_package_sha256"], self.providers.plan.call_args.args[0])

    def test_visual_rework_reuses_task_and_session_after_safe_boundary(self):
        pending = self.pending()
        self.engine.resolve_art(TASK, "rework", "调整厢房朝向", pending["review_package_sha256"])
        self.head.return_value = "b" * 40
        state = self.state()
        state["expected_head"] = "b" * 40
        self.engine._save(state)
        result = self.restart().step()
        self.assertEqual(result["phase"], "CODE", result)
        task = self.state()["task"]
        self.assertEqual(task["id"], TASK)
        self.assertEqual(task["session_id"], SESSION)
        self.assertEqual(task["base_sha"], "b" * 40)
        self.assertEqual(task["feedback"], "调整厢房朝向")
        self.assertEqual(task["reworks"], 1)
        self.assertTrue(task["code_resume"])
        self.assertEqual(self.state()["art_pending"], [])
        self.assertNotIn("decision", self.engine.art_queue()["requests"][0])

    def test_real_independent_verification_rejects_corrupt_asset_and_routes_rework(self):
        self.dispatch()
        self.engine.step()
        (self.evidence.public / "processed" / "house.fbx").write_bytes(b"corrupted after production")
        result = self.engine.step()
        self.assertEqual(result["phase"], "CODE", result)
        task = self.state()["task"]
        self.assertEqual(task["reworks"], 1)
        self.assertFalse(task["art_attempts"][0]["check_results"][0]["passed"])
        self.providers.review.assert_not_called()
        self.publish.assert_not_called()

    def test_codex_technical_rework_returns_to_same_claude_session(self):
        self.checked()
        self.providers.review.return_value = {"decision": "rework", "head_sha": HEAD,
                                               "summary": "Scale issue", "feedback": "Correct human scale"}
        result = self.engine.step()
        self.assertEqual(result["phase"], "CODE", result)
        self.assertEqual(self.state()["task"]["session_id"], SESSION)
        self.assertEqual(self.state()["task"]["feedback"], "Correct human scale")
        self.assertFalse(self.state().get("art_pending"))

    def test_completed_provider_receipt_recovers_without_a_second_model_call(self):
        self.dispatch()
        state = self.state()
        self.engine._new_operation(state, "code")
        self.providers.recover.return_value = deepcopy(self.providers.code.return_value)
        result = self.restart().step()
        self.assertEqual(result["phase"], "ART_CHECK", result)
        self.providers.code.assert_not_called()
        self.assertEqual(self.state()["task"]["session_id"], SESSION)

    def test_changed_batch_config_never_dispatches_model(self):
        self.engine.enqueue_art(TASK)
        path = Path(self.config["art_pipeline"]["config_file"])
        path.write_bytes(path.read_bytes() + b"\n")
        result = self.engine.step()
        self.assertEqual(result["phase"], "PLAN")
        self.assertIn("已改变", result["last_error"])
        self.providers.code.assert_not_called()
        self.providers.plan.assert_not_called()

    def test_review_or_human_decision_rejects_changed_evidence(self):
        self.checked()
        package = self.evidence.public / "review-package.json"
        package.write_bytes(package.read_bytes() + b"\n")
        result = self.engine.step()
        self.assertEqual(result["phase"], "ART_REVIEW")
        self.assertIn("证据", result["last_error"])
        self.providers.review.assert_not_called()

    def test_changed_screenshot_after_check_never_reaches_technical_review(self):
        self.checked()
        (self.evidence.public / "screenshots" / "shot-0.png").write_bytes(b"changed after check")
        result = self.engine.step()
        self.assertEqual(result["phase"], "ART_REVIEW")
        self.assertIn("Independent asset check failed", result["last_error"])
        self.providers.review.assert_not_called()

    def test_changed_asset_during_reviewer_is_not_submitted_for_visual_acceptance(self):
        self.checked()
        def review(*args):
            (self.evidence.public / "processed" / "house.fbx").write_bytes(b"changed during review")
            return {"decision": "accepted", "summary": "stale", "feedback": "", "head_sha": HEAD}
        self.providers.review.side_effect = review
        result = self.engine.step()
        self.assertEqual(result["phase"], "ART_REVIEW")
        self.assertFalse(self.state().get("art_pending"))

    def test_human_acceptance_rechecks_current_artifacts_without_rewriting_check_receipt(self):
        pending = self.pending()
        check = Path(pending["independent_check"])
        before = check.read_bytes()
        self.engine.resolve_art(TASK, "accepted", "通过", pending["review_package_sha256"])
        self.assertEqual(check.read_bytes(), before)
        (self.evidence.public / "screenshots" / "shot-0.png").write_bytes(b"changed after decision")
        result = self.restart().step()
        self.assertEqual(result["phase"], "PLAN")
        self.assertIn("Independent asset check failed", result["last_error"])
        self.assertFalse(self.state().get("art_history"))
        self.assertEqual(len(self.state()["art_pending"]), 1)
        self.assertEqual(check.read_bytes(), before)

    def test_changed_artifacts_reject_accept_but_still_allow_requested_rework(self):
        pending = self.pending()
        (self.evidence.public / "processed" / "house.fbx").write_bytes(b"changed after review")
        with self.assertRaisesRegex(RuntimeError, "Independent asset check failed"):
            self.engine.resolve_art(TASK, "accepted", "通过", pending["review_package_sha256"])
        self.engine.resolve_art(TASK, "rework", "修复当前损坏模型", pending["review_package_sha256"])
        self.assertEqual(self.restart().step()["phase"], "CODE")

    def test_visual_decision_requires_current_exact_package(self):
        pending = self.pending()
        with self.assertRaises(ValueError):
            self.engine.resolve_art(TASK, "accepted", "looks good", "f" * 64)
        package = Path(pending["review_package"])
        package.write_bytes(package.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            self.engine.resolve_art(TASK, "accepted", "looks good", pending["review_package_sha256"])
        self.assertNotIn("decision", self.engine.art_queue()["requests"][0])

    def test_work_window_and_pause_apply_to_art_dispatch(self):
        self.engine.enqueue_art(TASK)
        self.now = datetime(2026, 11, 2, 12, tzinfo=BEIJING)
        self.assertEqual(self.engine.step()["status"], "waiting_window")
        self.now = datetime(2026, 11, 2, 19, tzinfo=BEIJING)
        self.engine.pause()
        self.assertEqual(self.engine.step()["status"], "paused")
        self.assertEqual(self.engine.art_queue()["requests"][0]["status"], "queued")
        self.providers.code.assert_not_called()
        self.providers.plan.assert_not_called()

    def test_done_engine_reopens_only_for_explicit_art_request_or_visual_decision(self):
        self.assertEqual(self.engine.step()["phase"], "DONE")
        self.providers.plan.reset_mock()
        self.engine.enqueue_art(TASK)
        self.assertEqual(self.restart().step()["phase"], "CODE")
        self.providers.plan.assert_not_called()

    def test_art_after_completed_main_work_restores_done_without_calling_main_planner(self):
        self.assertEqual(self.engine.step()["phase"], "DONE")
        reason = self.state()["completion_reason"]
        self.providers.plan.reset_mock()
        pending = self.pending()
        self.assertEqual(self.engine.step()["phase"], "DONE")
        self.assertEqual(self.state()["completion_reason"], reason)
        self.engine.resolve_art(TASK, "accepted", "通过", pending["review_package_sha256"])
        self.assertEqual(self.restart().step()["phase"], "DONE")
        self.assertEqual(self.state()["completion_reason"], reason)
        self.assertEqual(len(self.state()["art_history"]), 1)
        self.providers.plan.assert_not_called()

    def test_queue_lock_failure_retains_completed_review_for_receipt_recovery(self):
        self.checked()
        with patch("unattended.engine.lock", side_effect=RuntimeError("queue lock temporarily busy")):
            result = self.engine.step()
        self.assertEqual(result["phase"], "ART_REVIEW")
        self.assertEqual(self.state()["operation"]["kind"], "review")
        self.providers.recover.return_value = deepcopy(self.providers.review.return_value)
        result = self.restart().step()
        self.assertEqual(result["phase"], "PLAN", result)
        self.providers.review.assert_called_once()
        self.assertEqual(len(self.state()["art_pending"]), 1)

    def test_pending_state_reconciles_interrupted_queue_write_without_model_replay(self):
        self.checked()
        def interrupted(path, value):
            if Path(path).name == "art-queue.json":
                raise OSError("Simulated write failure")
            return write_json(path, value)
        with patch("unattended.engine.write_json", side_effect=interrupted):
            result = self.engine.step()
        self.assertEqual(result["phase"], "PLAN")
        self.assertEqual(len(self.state()["art_pending"]), 1)
        self.assertEqual(self.engine.art_queue()["requests"][0]["status"], "active")
        self.assertEqual(self.restart().step()["phase"], "DONE")
        self.assertEqual(self.engine.art_queue()["requests"][0]["status"], "awaiting_approval")
        self.providers.code.assert_called_once()
        self.providers.review.assert_called_once()


if __name__ == "__main__":
    unittest.main()
