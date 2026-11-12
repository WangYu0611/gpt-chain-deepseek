from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unattended import dashboard_evidence as e
from unattended.dashboard_reworks import collect_reworks, MAX_ROUNDS
from tests.test_unattended_dashboard_evidence import binding, write


class ReworkEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.runtime = self.root / "art"
        self.public = self.runtime / "batches/BATCH"
        approved = self.root / "approved/batch.json"; write(approved, {"batch_id": "BATCH"})
        self.config = {"project_path": str(self.root / "art-source"), "runtime_dir": str(self.runtime), "branch": "art",
                       "art_pipeline": {"config_file": str(approved), "artifacts_dir": str(self.public), "task_ids": ["TASK-ART-1"]}}
        self.task = {"id": "TASK-ART-1", "kind": "art", "reworks": 2, "art_workspace_head": "c" * 40,
                     "art_request": {"config_file": str(approved), "config_sha256": e._digest(approved), "artifacts_dir": str(self.public)},
                     "art_attempts": [], "art_recipe_commits": [], "maintenance_history": []}
        self.state = {"version": 1, "binding": binding(self.config), "phase": "CODE", "task": self.task,
                      "operation": {"kind": "code", "task_id": self.task["id"], "head_sha": "c" * 40,
                                    "output_dir": str(self.runtime / "runs" / ("code-" + "f" * 32)), "started_at": "2026-11-07T13:58:39+00:00"}}
        self.directory = self.runtime / "tasks/TASK-ART-1"
        write(self.runtime / "art-queue.json", {"version": 1, "requests": [{"id": self.task["id"], "decision_history": [
            {"decision": "rework", "feedback": "细节还不够。", "recorded_at": "2026-11-06T13:00:00+00:00", "review_package_sha256": "a" * 64}]}]})
        self.config_path = self.root / "main.json"
        for target in ("load_config", "worker_config"):
            p = patch.object(e, target, return_value=self.config); p.start(); self.addCleanup(p.stop)
        self.capture_id = "a" * 12 + "-" + "b" * 12
        self.review(0, "a" * 40, "初审。", "accepted")
        self.review(1, "b" * 40, "已修复：院落。\n\n必须修复：屋顶。\n\n证据限制：门洞未验证。\n截图 screenshots/" + self.capture_id + "/", "rework")
        (self.directory / "delivery-1.md").write_text("统一到北宋是我的判断，请确认。\napi_key=DO_NOT_LEAK https://signed.example/?token=secret", encoding="utf-8")
        self.task["art_attempts"] = [{"rework": 1, "at": "2026-11-07T13:50:00+00:00", "check_results": [
            {"id": "art-pipeline-verify", "head_sha": "b" * 40, "passed": True, "exit_code": 0}],
            "art_review": {"review_package_sha256": "d" * 64}}]
        self.receipt()
        self.save()

    def save(self): write(self.runtime / "state.json", self.state)

    def review(self, index, head, feedback, decision):
        write(self.directory / f"review-{index}.json", {"decision": decision, "summary": "该轮审核结论", "feedback": feedback, "head_sha": head})

    def receipt(self):
        shots = []
        for i in range(8):
            p = self.public / "screenshots" / self.capture_id / f"{i + 1:02}_shot.png"
            p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([i]))
            shots.append({"name": p.stem, "path": str(p), "size": p.stat().st_size, "sha256": e._digest(p)})
        self.receipt_path = self.public / "receipts" / (self.capture_id + "-capture.json")
        write(self.receipt_path, {"status": "passed", "real_rhi": True, "capture_id": self.capture_id, "shots": shots})

    def detail(self): return e.collect_node_detail(self.config_path, "art", "review")
    def evidence(self, ref): return e.read_node_evidence(self.config_path, "art", "review", ref)
    def row(self, index=1): return next(r for r in self.detail()["rework_history"]["rounds"] if r["index"] == index)

    def test_current_code_keeps_previous_round_and_separates_authors_and_initial_review(self):
        history = self.detail()["rework_history"]
        self.assertEqual([r["index"] for r in history["rounds"]], [0, 1, 2])
        self.assertIn("非返工", history["rounds"][0]["label"])
        self.assertEqual(history["rounds"][-1]["decision"], "in_progress")
        previous = history["rounds"][1]
        self.assertEqual(previous["review_source"], "Codex技术审核")
        self.assertEqual(previous["delivery_source"], "Claude交付声明")
        self.assertIn("北宋是我的判断", previous["delivery_text"])
        self.assertEqual(history["user_feedback"][0]["feedback"], "细节还不够。")
        self.assertEqual(history["user_feedback"][0]["source"], "用户")
        self.assertNotIn("DO_NOT_LEAK", json.dumps(history)); self.assertNotIn("signed.example", json.dumps(history))
        self.assertEqual([s["kind"] for s in previous["sections"][:3]], ["completed", "required", "limitations"])
        self.assertEqual(history["summary"]["current_started_at"], "2026-11-07T13:58:39+00:00")

    def test_historical_images_use_cited_receipt_not_overwritten_package(self):
        write(self.public / "review-package.json", {"capture": {"shots": []}})
        row = self.row()
        self.assertEqual(row["evidence_status"], "historical_capture_receipt")
        self.assertIn("旧完整验收包未归档", row["evidence_note"])
        self.assertEqual(len(row["evidence_images"]), 8)
        image = row["evidence_images"][0]
        self.assertIn("第 1 轮", image["title"])
        self.assertEqual(self.evidence(image["id"])[1], "image/png")
        receipt = json.loads(self.receipt_path.read_text()); Path(receipt["shots"][0]["path"]).write_bytes(b"\x89PNG\r\n\x1a\nX")
        with self.assertRaisesRegex(ValueError, "SHA"): self.evidence(image["id"])

    def test_receipt_replacement_invalidates_image_ref_and_never_borrows_other_task(self):
        image = self.row()["evidence_images"][0]
        receipt = json.loads(self.receipt_path.read_text()); receipt["completed_epoch"] = 123
        write(self.receipt_path, receipt)
        with self.assertRaisesRegex(ValueError, "范围已变化"): self.evidence(image["id"])
        self.task["id"] = "TASK-OTHER"; self.state["operation"]["task_id"] = "TASK-OTHER"; self.save()
        with self.assertRaises(ValueError): self.evidence(image["id"])

    def test_invalid_receipt_path_or_duplicate_shots_refuses_every_image(self):
        value = json.loads(self.receipt_path.read_text()); value["shots"][0]["path"] = str(self.root / "secret.png")
        write(self.receipt_path, value)
        self.assertFalse(self.row()["evidence_images"])
        self.assertEqual(self.row()["evidence_status"], "unavailable")
        self.receipt(); value = json.loads(self.receipt_path.read_text()); value["shots"][0] = value["shots"][1]
        write(self.receipt_path, value)
        self.assertFalse(self.row()["evidence_images"])

    def test_checks_require_exact_round_and_review_head(self):
        self.assertTrue(self.row()["checks"][0]["passed"])
        self.task["art_attempts"][0]["rework"] = 2; self.save()
        self.assertEqual(self.row()["checks_status"], "unavailable"); self.assertFalse(self.row()["checks"])
        self.task["art_attempts"][0]["rework"] = 1
        self.task["art_attempts"][0]["check_results"][0]["head_sha"] = "e" * 40; self.save()
        self.assertFalse(self.row()["checks"])

    def test_job_round_counts_come_from_bound_commits_and_migration(self):
        self.task["maintenance_history"] = [{"at": "2026-11-07T12:00:00+00:00", "head_sha": "a" * 40,
            "system_change": "versioned job execution; no authored model geometry"}]
        self.task["art_recipe_commits"] = [{"head_sha": "b" * 40,
            "files": {"art-jobs/TASK-ART-1/blender_job.py": "c" * 64}}]
        self.save(); history = self.detail()["rework_history"]
        self.assertEqual(history["summary"]["completed_job_rounds"], 1)
        self.assertEqual(history["summary"]["current_job_round"], 2)
        self.assertEqual(history["rounds"][1]["production_mode"], "claude_blender_job")
        self.task["maintenance_history"] = []; self.task["art_recipe_commits"] = []; self.save()
        self.assertIsNone(self.detail()["rework_history"]["summary"]["current_job_round"])

    def test_edited_review_or_delivery_invalidates_document_and_image_refs(self):
        row = self.row(); ref = row["evidence_images"][0]["id"]
        document = next(a for a in row["artifacts"] if "交付声明" in a["title"])
        data, mime, _ = self.evidence(document["id"])
        self.assertIn("北宋是我的判断", data.decode()); self.assertNotIn(b"DO_NOT_LEAK", data)
        self.assertTrue(mime.startswith("text/plain"))
        (self.directory / "delivery-1.md").write_text("changed", encoding="utf-8")
        with self.assertRaises(ValueError): self.evidence(document["id"])
        with self.assertRaises(ValueError): self.evidence(ref)

    def test_named_audit_image_requires_same_run_import_and_manifest_digest(self):
        run = self.capture_id.split("-")[0]
        image = self.public / "processed" / run / "claude_job_audit/L8_house_after_iso_rear.png"
        image.parent.mkdir(parents=True); image.write_bytes(b"\x89PNG\r\n\x1a\naudit")
        manifest = image.parent.parent / "assets_manifest.json"
        write(manifest, {"files": [{"path": "claude_job_audit/" + image.name, "size_bytes": image.stat().st_size, "sha256": e._digest(image)}]})
        imported = self.public / "receipts" / (run + "-import.json")
        write(imported, {"status": "passed", "run": run, "manifest_sha256": e._digest(manifest)})
        self.review(1, "b" * 40, f"截图 screenshots/{self.capture_id}/。\n必须修复：processed/{run}/claude_job_audit/{image.name}显示折痕。", "rework")
        row = self.row(); self.assertEqual(len(row["evidence_images"]), 9)
        audit = row["evidence_images"][-1]; self.assertIn("Blender 审计", audit["title"])
        self.assertEqual(self.evidence(audit["id"])[0], image.read_bytes())
        write(imported, {"status": "passed", "run": run, "manifest_sha256": "f" * 64})
        self.assertEqual(len(self.row()["evidence_images"]), 8)
        self.assertIn("审计图不可核实", self.row()["evidence_note"])
        with self.assertRaises(ValueError): self.evidence(audit["id"])

    def test_audit_image_from_other_run_is_not_borrowed(self):
        self.review(1, "b" * 40, f"截图 screenshots/{self.capture_id}/。\nprocessed/{'c' * 12}/claude_job_audit/L8_house_after_iso_rear.png", "rework")
        row = self.row()
        self.assertEqual(len(row["evidence_images"]), 8)
        self.assertIn("批次不一致", row["evidence_note"])

    def test_history_is_bounded_without_directory_scan_or_writes(self):
        self.task["reworks"] = 200; self.save()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        with patch.object(Path, "glob", side_effect=AssertionError("no history scan")), patch.object(Path, "rglob", side_effect=AssertionError("no history scan")):
            history = self.detail()["rework_history"]
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertLessEqual(len(history["rounds"]), MAX_ROUNDS)
        self.assertTrue(history["summary"]["history_truncated"])
        self.assertIsNone(history["summary"]["current_job_round"])


if __name__ == "__main__": unittest.main()
