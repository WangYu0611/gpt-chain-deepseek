"""Offline, read-only art diagnosis projections and task-bound public evidence."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_unattended_dashboard_art as fixture
from unattended import dashboard_art, dashboard_evidence as evidence, dashboard_flows as flows
from unattended.config import ART_CODING_MODEL, ART_CODING_REASONING_EFFORT
from unattended.storage import write_json


class DashboardArtDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ArtDashboardTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.healthy = {"alive": True, "healthy": True}
        self.state, _ = self.f.production_state("blender")
        self.f.item["status"] = "active"
        self.f.save_queue()
        self.run = self.f.art_runtime / "runs" / ("art_diagnose-" + "a" * 32)
        self.run.mkdir(parents=True)
        batch = Path(json.loads(self.f.art_path.read_text(encoding="utf-8"))["art_pipeline"]["config_file"])
        task = self.state["task"]
        task.pop("art_review", None)
        task.update(art_workspace_head="a" * 40, code_base="a" * 40, reworks=6,
                    art_diagnosis={"feedback": "屋面交接需继续处理", "trigger": "rework_threshold", "round": 6,
                                   "fingerprint": "f" * 64, "source_phase": "ART_REVIEW"})
        task["art_request"]["config_sha256"] = self.f.digest(batch)
        self.state.update(phase="ART_DIAGNOSE", expected_head="a" * 40,
                          operation={"kind": "art_diagnose", "task_id": task["id"], "head_sha": "a" * 40,
                                     "output_dir": str(self.run), "coding_provider": "claude",
                                     "started_at": (self.f.now - timedelta(seconds=5)).isoformat()})
        self.result = {"action": "continue", "diagnosis": "制作方案需要进一步检查局部连接。",
                       "plan": "保留已完成院墙，检查屋面交接并提交新证据。", "reason": "现有授权内继续制作。",
                       "required_approvals": [], "questions": [], "additional_tripo_credits": 0,
                       "head_sha": "a" * 40}
        write_json(self.run / "result.json", self.result)
        self.save()

    def save(self):
        write_json(self.f.art_runtime / "state.json", self.state)

    def snapshot(self):
        with patch("unattended.dashboard_state._worker", return_value=deepcopy(self.healthy)):
            return dashboard_art.collect_art_snapshot(self.f.main, self.f.now)

    def projection(self, art=None):
        snapshot = {"art": art if art is not None else self.snapshot(), "worker": self.healthy,
                    "status": {}, "workflow": {"nodes": [], "edges": []}}
        return flows.collect_flows(self.f.main_path, snapshot)["art"]

    def detail(self):
        return evidence.collect_node_detail(self.f.main_path, "art", "queue")

    def read(self, ref):
        return evidence.read_node_evidence(self.f.main_path, "art", "queue", ref)

    def public_log(self):
        (self.run / "stdout.log").write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "PRIVATE_NATIVE_CONVERSATION"},
            {"type": "tool_use", "name": "Read", "input": {"path": "PRIVATE_TOOL_ARGUMENT"}}]}}) + "\n", encoding="utf-8")
        (self.run / "stderr.log").write_text("", encoding="utf-8")

    def test_diagnosis_snapshot_is_read_only_and_does_not_show_stale_tool_progress_or_visual_approval(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.f.root.rglob("*") if p.is_file()}
        value = self.snapshot()
        self.assertEqual(value["status"], "art_diagnose")
        self.assertIn("自主诊断", value["label"])
        self.assertEqual(value["phase"], "ART_DIAGNOSE")
        self.assertIsNone(value["production_progress"])
        self.assertFalse(value["requests"][0]["can_resolve"])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.f.root.rglob("*") if p.is_file()})

    def test_existing_queue_node_shows_diagnosis_without_new_node_or_invented_tool_execution(self):
        value = self.projection()
        rows = {row["id"]: row for row in value["nodes"]}
        self.assertEqual(value["current"], "queue")
        self.assertIn("自主诊断", rows["queue"]["title"])
        self.assertEqual(rows["queue"]["state"], "running")
        self.assertEqual(rows["queue"]["actor"], "Claude Code")
        self.assertIn("只读", rows["queue"]["summary"])
        self.assertEqual((rows["queue"]["model"], rows["queue"]["effort"], rows["queue"]["model_source"]),
                         (ART_CODING_MODEL, ART_CODING_REASONING_EFFORT, "configured"))
        self.assertNotEqual(rows["blender"]["state"], "running")
        self.assertNotEqual(rows["approval"]["state"], "completed")
        self.assertEqual(tuple(rows), flows.NODE_IDS["art"])

    def test_recorded_model_uses_the_current_diagnosis_branch_and_verified_receipt(self):
        source = "550e8400-e29b-41d4-a716-446655440000"
        branch = "550e8400-e29b-41d4-a716-446655440001"
        self.state["task"].update(session_id=source, session_owner_task_id=self.state["task"]["id"])
        self.state["operation"]["diagnosis_reasoning_effort"] = "high"
        self.save()
        event = {"type": "system", "subtype": "init", "session_id": branch, "model": "claude-opus-5-5"}
        (self.run / "stdout.log").write_text(json.dumps(event) + "\n", encoding="utf-8")
        receipt = {"kind": "art_diagnose", "diagnosis_provider": "claude", "diagnosis_model": "claude-opus-5-5",
                   "diagnosis_reasoning_effort": "high", "task_id": self.state["task"]["id"],
                   "head_sha": "a" * 40, "requested_session": source, "diagnosis_session": branch,
                   "model_selection": {"model_verified": True, "observed_model": "claude-opus-5-5",
                                       "requested_reasoning_effort": "high"}}
        write_json(self.run / "process.json", receipt)
        row = next(row for row in self.projection()["nodes"] if row["id"] == "queue")
        self.assertEqual((row["model"], row["effort"], row["model_source"]), ("claude-opus-5-5", "high", "recorded"))
        for variant in ("source_as_branch", "wrong_task", "wrong_head", "wrong_source", "unverified_model", "model_mismatch"):
            with self.subTest(variant=variant):
                changed = deepcopy(receipt)
                if variant == "source_as_branch":
                    changed["diagnosis_session"] = source
                elif variant == "wrong_task":
                    changed["task_id"] = "OTHER"
                elif variant == "wrong_head":
                    changed["head_sha"] = "b" * 40
                elif variant == "wrong_source":
                    changed["requested_session"] = branch
                elif variant == "unverified_model":
                    changed["model_selection"]["model_verified"] = False
                else:
                    changed["model_selection"]["observed_model"] = "claude-sonnet-5-5"
                write_json(self.run / "process.json", changed)
                row = next(row for row in self.projection()["nodes"] if row["id"] == "queue")
                self.assertEqual(row["model_source"], "configured")
        self.assertEqual(json.loads((self.f.art_runtime / "state.json").read_text(encoding="utf-8"))["task"]["session_id"], source)

    def test_claude_quota_wait_belongs_to_diagnosis_and_does_not_claim_running(self):
        self.state["provider_retry"] = {"phase": "ART_DIAGNOSE", "task_id": self.state["task"]["id"],
            "head_sha": "a" * 40, "attempt": 2, "reason": "claude_subscription_limit",
            "next_retry_at": "2026-11-08T11:00:00+00:00", "status": "waiting", "raw_error": "PRIVATE_RETRY"}
        self.save()
        art = self.snapshot()
        self.assertEqual(art["status"], "waiting_provider")
        self.assertEqual(art["provider_retry"]["provider"], "Claude Code")
        self.assertEqual(art["provider_retry"]["phase"], "ART_DIAGNOSE")
        self.assertNotIn("PRIVATE_RETRY", json.dumps(art))
        queue = next(row for row in self.projection(art)["nodes"] if row["id"] == "queue")
        self.assertEqual(queue["state"], "waiting")
        self.assertIn("额度", queue["blocked_reason"])

    def test_diagnosis_log_is_event_projection_and_plan_is_public_structured_result(self):
        self.public_log()
        private = self.run / "private"
        write_json(private / "native.json", {"text": "DO_NOT_READ_PRIVATE"})
        value = self.detail()
        self.assertTrue(value["logs"])
        output = b"\n".join(self.read(row["id"])[0] for row in value["logs"])
        self.assertIn("读取文件", output.decode("utf-8"))
        self.assertNotIn(b"PRIVATE_NATIVE_CONVERSATION", output)
        self.assertNotIn(b"PRIVATE_TOOL_ARGUMENT", output)
        plans = [self.read(row["id"])[0] for row in value["artifacts"]]
        self.assertTrue(any(self.result["plan"].encode("utf-8") in data for data in plans))
        self.assertNotIn("DO_NOT_READ_PRIVATE", json.dumps(value))
        self.assertNotIn(str(self.run), json.dumps(value))

    def test_diagnosis_wait_rejects_foreign_task_non_art_task_and_codex_quota(self):
        baseline = deepcopy(self.state)
        for variant in ("foreign_task", "non_art_task", "codex_quota"):
            with self.subTest(variant=variant):
                self.state = deepcopy(baseline)
                self.state["provider_retry"] = {"phase": "ART_DIAGNOSE", "task_id": self.state["task"]["id"],
                    "head_sha": "a" * 40, "attempt": 1, "reason": "claude_subscription_limit",
                    "next_retry_at": "2026-11-08T11:00:00+00:00", "status": "waiting"}
                if variant == "foreign_task":
                    self.state["provider_retry"]["task_id"] = "OTHER"
                elif variant == "non_art_task":
                    self.state["task"]["kind"] = "code"
                else:
                    self.state["provider_retry"]["reason"] = "codex_subscription_limit"
                self.save()
                self.assertEqual(self.snapshot()["status"], "error")

    def test_changed_task_head_or_operation_invalidates_diagnosis_evidence_reference(self):
        self.public_log()
        baseline = deepcopy(self.state)
        for change in ("task", "head", "operation"):
            with self.subTest(change=change):
                self.state = deepcopy(baseline)
                self.save()
                reference = self.detail()["logs"][0]["id"]
                if change == "task":
                    self.state["task"]["id"] = "ART-OTHER"
                    self.state["operation"]["task_id"] = "ART-OTHER"
                elif change == "head":
                    self.state["task"]["art_workspace_head"] = "b" * 40
                    self.state["task"]["code_base"] = "b" * 40
                    self.state["operation"]["head_sha"] = "b" * 40
                else:
                    new = self.f.art_runtime / "runs" / ("art_diagnose-" + "b" * 32)
                    new.mkdir(parents=True, exist_ok=True)
                    (new / "stdout.log").write_text('{"type":"result","subtype":"success"}\n', encoding="utf-8")
                    self.state["operation"]["output_dir"] = str(new)
                self.save()
                with self.assertRaises(ValueError):
                    self.read(reference)

    def test_live_diagnosis_log_append_preserves_reference_but_not_native_text(self):
        self.public_log()
        reference = self.detail()["logs"][0]["id"]
        with (self.run / "stdout.log").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "PRIVATE_RESULT_TEXT"}) + "\n")
        body, mime, filename = self.read(reference)
        self.assertIn("本轮执行结束", body.decode("utf-8"))
        self.assertNotIn(b"PRIVATE_RESULT_TEXT", body)
        self.assertTrue(mime.startswith("text/plain"))
        self.assertIsNone(filename)

    def test_structured_plan_from_another_head_is_not_current_diagnosis_evidence(self):
        self.result["head_sha"] = "b" * 40
        write_json(self.run / "result.json", self.result)
        try:
            value = self.detail()
        except ValueError:
            return
        contents = [self.read(row["id"])[0] for row in value["artifacts"]]
        self.assertFalse(any(self.result["plan"].encode("utf-8") in body for body in contents))

    def test_foreign_operation_or_escaped_output_never_publishes_diagnosis_files(self):
        self.public_log()
        self.state["operation"]["task_id"] = "OTHER"
        self.save()
        self.assertFalse(self.detail()["logs"])
        self.state["operation"]["task_id"] = self.state["task"]["id"]
        self.state["operation"]["output_dir"] = str(self.f.root)
        self.save()
        with self.assertRaises(ValueError):
            self.detail()

    def test_pending_diagnosis_without_operation_is_ready_not_a_stale_or_running_tool(self):
        self.state["operation"] = None
        self.save()
        rows = {row["id"]: row for row in self.projection()["nodes"]}
        self.assertEqual(rows["queue"]["state"], "ready")
        self.assertIn("自主诊断", rows["queue"]["title"])
        self.assertNotEqual(rows["blender"]["state"], "running")

    def test_changed_diagnosis_fingerprint_invalidates_current_log_and_plan_references(self):
        self.public_log()
        value = self.detail()
        log = value["logs"][0]["id"]
        plan = next(row["id"] for row in value["artifacts"]
                    if self.result["plan"].encode("utf-8") in self.read(row["id"])[0])
        self.state["task"]["art_diagnosis"]["fingerprint"] = "e" * 64
        self.save()
        for reference in (log, plan):
            with self.assertRaises(ValueError):
                self.read(reference)

    def test_completed_diagnosis_logs_and_public_plan_remain_scoped_as_history_after_code_resumes(self):
        self.public_log()
        write_json(self.run / "process.json", {"kind": "art_diagnose", "task_id": self.state["task"]["id"],
                   "head_sha": "a" * 40, "exit_code": 0})
        saved = {**self.result, "private_native": "DO_NOT_PROJECT_NATIVE_ENVELOPE"}
        self.state["task"]["art_diagnosis_history"] = [{"round": 6, "trigger": "rework_threshold",
            "source_fingerprint": "f" * 64, "output_dir": str(self.run), "result": saved,
            "at": self.f.now.isoformat(), "private_file": "/private/do-not-read"}]
        self.state["task"].pop("art_diagnosis")
        self.state["phase"] = "CODE"
        self.state["operation"] = None
        self.save()
        value = self.detail()
        self.assertTrue(any("历史自主诊断" in row["title"] for row in value["logs"]))
        plans = [row for row in value["artifacts"] if "调整计划" in row["title"]]
        self.assertEqual(len(plans), 1)
        body = self.read(plans[0]["id"])[0]
        self.assertIn(self.result["plan"].encode("utf-8"), body)
        self.assertNotIn(b"DO_NOT_PROJECT_NATIVE_ENVELOPE", body)
        self.assertIn("不代表制作或验收通过", plans[0]["title"])
        self.state["task"]["art_diagnosis_history"][0]["source_fingerprint"] = "e" * 64
        self.save()
        with self.assertRaises(ValueError):
            self.read(plans[0]["id"])

    def test_controller_source_object_can_bind_historical_diagnosis_without_exposing_it(self):
        self.public_log()
        write_json(self.run / "process.json", {"kind": "art_diagnose", "task_id": self.state["task"]["id"],
                   "head_sha": "a" * 40, "exit_code": 0})
        self.state["phase"] = "CODE"
        self.state["operation"] = None
        self.state["task"]["art_diagnosis_history"] = [{"round": 1, "trigger": "rework_threshold",
            "source_fingerprint": {"head_sha": "a" * 40, "editable_files": {"job": "b" * 64}},
            "output_dir": str(self.run), "result": self.result, "at": self.f.now.isoformat()}]
        self.save()
        value = self.detail()
        plans = [row for row in value["artifacts"] if "历史自主诊断" in row["title"] and "调整计划" in row["title"]]
        self.assertEqual(len(plans), 1)
        body = self.read(plans[0]["id"])[0]
        self.assertIn(self.result["plan"].encode("utf-8"), body)
        self.assertNotIn(b"editable_files", body)
        self.state["task"]["art_diagnosis_history"][0]["source_fingerprint"]["editable_files"]["job"] = "c" * 64
        self.save()
        with self.assertRaises(ValueError):
            self.read(plans[0]["id"])


if __name__ == "__main__":
    unittest.main()
