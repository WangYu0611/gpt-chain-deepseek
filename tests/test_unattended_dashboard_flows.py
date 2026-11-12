"""Flow projections cannot invent executions, models or visual acceptance."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_unattended_dashboard_art as fixture
from unattended import dashboard_art, dashboard_flows as flows
from unattended.storage import write_json


class DashboardFlowsTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ArtDashboardTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.main = self.f.main
        self.healthy = {"alive": True, "healthy": True}
        self.snapshot = {"worker": self.healthy, "status": {"code": "running", "phase": "CODE"},
                         "workflow": {"current": "code", "nodes": [], "edges": [], "summary": "当前任务"}}

    def project(self, art=None):
        with patch("unattended.dashboard_state._worker", return_value=deepcopy(self.healthy)):
            self.snapshot["art"] = art if art is not None else dashboard_art.collect_art_snapshot(self.main, self.f.now)
        return flows.collect_flows(self.f.main_path, self.snapshot)

    @staticmethod
    def nodes(result, lane="art"):
        return {row["id"]: row for row in result[lane]["nodes"]}

    def production(self, stage="blender"):
        state, progress = self.f.production_state(stage)
        batch = Path(self.main["art_worker"]["config_file"])
        cfg = json.loads(batch.read_text(encoding="utf-8"))
        state["task"]["art_request"]["config_sha256"] = self.f.digest(Path(cfg["art_pipeline"]["config_file"]))
        state["operation"].update(output_dir=str(self.f.art_runtime / "runs" / "current"), coding_provider="claude")
        write_json(self.f.art_runtime / "state.json", state)
        return state, progress

    def test_unconfigured_art_and_empty_snapshots_keep_fixed_contract_without_writes(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.f.root.rglob("*") if p.is_file()}
        result = flows.collect_flows(self.f.main_path, {})
        self.assertEqual(set(result), {"development", "art", "operations"})
        self.assertIsNone(result["art"]["current"])
        self.assertTrue(all(row["state"] == "disabled" for row in result["art"]["nodes"]))
        for lane, value in result.items():
            for row in value["nodes"]:
                self.assertEqual(row["detail_url"], f"/api/flow/detail?lane={lane}&node={row['id']}")
                self.assertEqual(set(row), {"id", "title", "state", "state_label", "current", "actor", "model", "effort", "model_source", "summary", "blocked_reason", "detail_url"})
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.f.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_development_reuses_real_workflow_states_and_distinguishes_configured_review_model(self):
        cfg = json.loads(self.f.main_path.read_text(encoding="utf-8"))
        cfg["codex"] = {"model": "gpt-6-astra", "reasoning_effort": "high"}
        write_json(self.f.main_path, cfg)
        self.snapshot["status"].update(phase="REVIEW", code="blocked", error="真正阻塞 token=secret-example")
        self.snapshot["workflow"] = {"current": "review", "summary": "审核暂停", "edges": [{"from": "check", "to": "review", "label": "检查完成"}],
            "nodes": [{"id": "code", "title": "开发", "state": "completed", "model": "claude-sonnet-5-5", "effort": "high", "model_source": "recorded"},
                      {"id": "review", "title": "审核", "state": "blocked", "summary": "审核暂停", "model": None, "model_source": "unknown"}]}
        rows = self.nodes(self.project(), "development")
        self.assertEqual(rows["code"]["state"], "completed")
        self.assertEqual(rows["code"]["model_source"], "recorded")
        self.assertEqual(rows["review"]["state"], "blocked")
        self.assertEqual(rows["review"]["model"], "gpt-6-astra")
        self.assertEqual(rows["review"]["model_source"], "configured")
        self.assertIn("真正阻塞", rows["review"]["blocked_reason"])
        self.assertNotIn("secret-example", rows["review"]["blocked_reason"])

    def test_current_production_stage_is_tool_not_an_invented_language_model(self):
        self.production("blender")
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.f.root.rglob("*") if p.is_file()}
        result = self.project()
        rows = self.nodes(result)
        self.assertEqual(result["art"]["current"], "blender")
        self.assertEqual(rows["blender"]["state"], "running")
        self.assertEqual(rows["blender"]["actor"], "Blender")
        self.assertIsNone(rows["blender"]["model"])
        self.assertEqual(rows["model"]["state"], "completed")
        self.assertEqual(rows["ue_capture"]["state"], "idle")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.f.root.rglob("*") if p.is_file()})

    def test_old_wrong_task_batch_or_changed_approved_config_never_claims_tripo_running(self):
        for case in ("old", "foreign_task", "foreign_batch", "changed_config"):
            with self.subTest(case=case):
                state, progress = self.production("model")
                if case == "old":
                    progress["updated_epoch"] = (self.f.now - timedelta(hours=1)).timestamp()
                elif case == "foreign_batch":
                    progress["batch_id"] = "OLD-BATCH"
                elif case == "foreign_task":
                    state["operation"]["task_id"] = "OTHER"
                else:
                    state["task"]["art_request"]["config_sha256"] = "f" * 64
                write_json(self.f.art_runtime / "state.json", state)
                write_json(self.f.artifacts / "pipeline-state.json", progress)
                result = self.project()
                self.assertEqual(result["art"]["current"], "queue")
                self.assertNotEqual(self.nodes(result)["model"]["state"], "running")

    def test_pipeline_does_not_override_pause_block_quota_stop_or_stale_heartbeat(self):
        self.production("ue_capture")
        with patch("unattended.dashboard_state._worker", return_value=self.healthy):
            baseline = dashboard_art.collect_art_snapshot(self.main, self.f.now)
        for status, worker, expected in (("paused", self.healthy, "paused"), ("blocked", self.healthy, "blocked"),
                                         ("code", {"alive": False}, "waiting"), ("code", {"alive": True, "healthy": False}, "stale"),
                                         ("waiting_provider", self.healthy, "waiting")):
            art = deepcopy(baseline)
            art.update(status=status, worker=worker, blocking_summary="真实几何缺陷")
            if status == "waiting_provider":
                art["provider_retry"] = {"status": "waiting", "reason_label": "Claude 额度不足", "next_retry_at": "2026-11-08T10:00:00+00:00"}
            with self.subTest(status=status, worker=worker):
                result = self.project(art)
                row = self.nodes(result)["ue_capture"]
                self.assertEqual(row["state"], expected)
                self.assertTrue(row["blocked_reason"])
                if status == "waiting_provider":
                    self.assertIn("2026-11-08 18:00:00（北京时间）", row["summary"])
                if status == "blocked":
                    self.assertIn("真实几何缺陷", row["blocked_reason"])

    def test_completed_capture_waits_for_checks_not_visual_acceptance(self):
        self.production("pending_visual_acceptance")
        result = self.project()
        rows = self.nodes(result)
        self.assertEqual(result["art"]["current"], "ue_capture")
        self.assertEqual(rows["ue_capture"]["state"], "completed")
        self.assertIn("等待技术检查", rows["ue_capture"]["summary"])
        self.assertEqual(rows["approval"]["state"], "idle")

    def test_recorded_claude_init_requires_current_session_and_never_borrows_other_logs(self):
        state, _ = self.production("model")
        (self.f.artifacts / "pipeline-state.json").unlink()
        session = "550e8400-e29b-41d4-a716-446655440000"
        state["task"]["session_id"] = session
        write_json(self.f.art_runtime / "state.json", state)
        out = Path(state["operation"]["output_dir"])
        out.mkdir(parents=True)
        (out / "stdout.log").write_text(json.dumps({"type": "system", "subtype": "init", "session_id": session, "model": "claude-opus-5-5"}) + "\n", encoding="utf-8")
        row = self.nodes(self.project())["queue"]
        self.assertEqual((row["model"], row["model_source"]), ("claude-opus-5-5", "recorded"))
        state["task"]["session_id"] = "00000000-0000-0000-0000-000000000000"
        write_json(self.f.art_runtime / "state.json", state)
        self.assertEqual(self.nodes(self.project())["queue"]["model_source"], "configured")
        state["operation"]["output_dir"] = str(self.f.root)
        write_json(self.f.art_runtime / "state.json", state)
        self.assertEqual(self.nodes(self.project())["queue"]["model_source"], "configured")

    def test_finished_current_call_does_not_remain_running_and_racing_state_is_stale(self):
        state, _ = self.production("blender")
        out = Path(state["operation"]["output_dir"])
        write_json(out / "process.json", {"kind": "code", "exit_code": 0})
        result = self.project()
        self.assertEqual(self.nodes(result)["blender"]["state"], "ready")
        art = deepcopy(self.snapshot["art"])
        state["task"]["id"] = "NEW-TASK"
        write_json(self.f.art_runtime / "state.json", state)
        result = self.project(art)
        self.assertEqual(self.nodes(result)[result["art"]["current"]]["state"], "stale")

    def test_technical_preview_block_and_user_approval_remain_separate(self):
        self.f.item["status"] = "active"
        self.f.save_queue()
        self.f.save_active_task("BLOCKED")
        result = self.project()
        rows = self.nodes(result)
        self.assertEqual(result["art"]["current"], "review")
        self.assertEqual(rows["review"]["state"], "blocked")
        self.assertIn("屋顶", rows["review"]["blocked_reason"])
        self.assertEqual(rows["approval"]["state"], "idle")
        self.f.item["status"] = "awaiting_approval"
        self.f.save_queue()
        self.f.save_technical_review()
        result = self.project()
        rows = self.nodes(result)
        self.assertEqual(result["art"]["current"], "approval")
        self.assertEqual(rows["review"]["state"], "completed")
        self.assertEqual(rows["approval"]["state"], "waiting")
        self.assertEqual(rows["promotion"]["state"], "idle")

    def test_notifications_enabled_is_not_delivery_success_and_workers_are_independent(self):
        self.snapshot["notifications"] = {"enabled": True}
        self.snapshot["worker"] = {"alive": False, "healthy": False}
        rows = self.nodes(self.project(), "operations")
        self.assertEqual(rows["development_worker"]["state"], "waiting")
        self.assertEqual(rows["art_worker"]["state"], "running")
        self.assertEqual(rows["notifications"]["state"], "ready")
        self.assertIn("尚无已确认", rows["notifications"]["summary"])
        self.snapshot["notifications"]["last_error"] = "投递失败"
        rows = self.nodes(self.project(), "operations")
        self.assertEqual(rows["notifications"]["state"], "blocked")
        self.assertIn("投递失败", rows["notifications"]["blocked_reason"])

    def test_promotion_requires_same_accepted_task_and_reports_wait_block_integrated(self):
        self.f.save_technical_review()
        self.f.item["status"] = "accepted"
        self.f.save_queue()
        for status, expected in (("waiting_target_lock", "waiting"), ("blocked", "blocked"), ("integrated", "completed")):
            write_json(self.f.art_runtime / "promotion-requests.json", {"requests": [
                {"source_task_id": "OTHER", "status": "integrated"},
                {"source_task_id": self.f.item["id"], "status": status, "message": "原始原因 https://example.org/model?signature=private-signed-token"}]})
            with self.subTest(status=status):
                result = self.project()
                self.assertEqual(result["art"]["current"], "promotion")
                self.assertEqual(self.nodes(result)["promotion"]["state"], expected)
                self.assertNotIn("private-signed-token", json.dumps(result))

    def test_review_check_completion_requires_same_head_and_package(self):
        self.f.item["status"] = "active"
        self.f.save_queue()
        state = self.f.save_active_task("ART_REVIEW")
        state["task"].update(art_workspace_head="a" * 40, check_results=[
            {"id": "art-pipeline-verify", "head_sha": "a" * 40, "passed": True}])
        write_json(self.f.art_runtime / "state.json", state)
        self.assertEqual(self.nodes(self.project())["check"]["state"], "completed")
        state["task"]["check_results"][0]["head_sha"] = "b" * 40
        write_json(self.f.art_runtime / "state.json", state)
        self.assertEqual(self.nodes(self.project())["check"]["state"], "idle")

    def test_previous_approved_batch_promotion_remains_visible_during_new_production(self):
        self.production("blender")
        with patch("unattended.dashboard_state._worker", return_value=self.healthy):
            art = dashboard_art.collect_art_snapshot(self.main, self.f.now)
        art["requests"].append({"id": "PREVIOUS-ART", "status": "accepted"})
        art["promotions"] = [{"source_task_id": "PREVIOUS-ART", "batch_id": "OLD-APPROVED-BATCH", "status": "blocked", "message": "目标工作区存在修改"}]
        result = self.project(art)
        self.assertEqual(result["art"]["current"], "blender")
        self.assertEqual(self.nodes(result)["blender"]["state"], "running")
        promotion = self.nodes(result)["promotion"]
        self.assertFalse(promotion["current"])
        self.assertEqual(promotion["state"], "blocked")
        self.assertIn("OLD-APPROVED-BATCH", promotion["summary"])
        self.assertIn("目标工作区存在修改", promotion["blocked_reason"])


if __name__ == "__main__":
    unittest.main()
