"""Offline art preview/review routing, version binding and main-lane isolation."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from contextlib import redirect_stdout

from unattended import cli, dashboard, dashboard_art, dashboard_state
from unattended.config import load_config
from unattended.storage import write_json


class ArtDashboardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.art_runtime = self.root / "art-runtime"
        self.artifacts = self.root / "artifacts"
        self.art_runtime.mkdir()
        self.artifacts.mkdir()
        self.main_path, self.art_path = self.root / "main.json", self.root / "art.json"
        batch = self.root / "approved" / "batch.json"
        write_json(batch, {"batch_id": "HOUSE-001"})
        main = {"version": 1, "project_path": str(self.root / "game"), "runtime_dir": str(self.root / "main-runtime"),
                "goal": "Continue game", "branch": "unattended/main", "codex": {}, "dsh": {},
                "art_worker": {"config_file": str(self.art_path)}}
        art = {"version": 1, "project_path": str(self.root / "art-project"), "runtime_dir": str(self.art_runtime),
               "goal": "Make art", "branch": "unattended/art", "codex": {}, "coding_provider": "claude", "claude": {},
               "art_pipeline": {"config_file": str(batch), "artifacts_dir": str(self.artifacts), "task_ids": ["ART-001"]}}
        write_json(self.main_path, main)
        write_json(self.art_path, art)
        self.main = load_config(self.main_path)
        self.png = self.artifacts / "preview.png"
        self.png.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lS8AAAAASUVORK5CYII="))
        self.package = {"batch_id": "HOUSE-001", "approved_levels": ["L1", "L4", "L8"],
                        "capture": {"shots": [{"name": "courtyard", "path": str(self.png), "sha256": self.digest(self.png)}]},
                        "private_field": "private-secret-do-not-project"}
        self.package_path = self.artifacts / "review-package.json"
        write_json(self.package_path, self.package)
        self.review_sha = self.digest(self.package_path)
        self.item = {"id": "ART-001", "title": "三级住宅", "status": "awaiting_approval",
                     "artifacts_dir": str(self.artifacts), "review_package_sha256": self.review_sha,
                     "body": "private-body-do-not-project"}
        self.save_queue()
        self.now = datetime.now(timezone.utc)

    def digest(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def save_queue(self):
        write_json(self.art_runtime / "art-queue.json", {"version": 1, "requests": [self.item]})

    def large_package(self):
        self.package['files'] = [{'path': 'Content/Versions/' + str(i) + '/' + 'asset_' * 25,
                                 'sha256': 'a' * 64, 'size': 1024} for i in range(18000)]
        self.package['budget'] = {'budget': self.budget(breached=False)}
        self.bind_package()
        self.save_technical_review()
        self.assertGreater(self.package_path.stat().st_size, dashboard_state.MAX_JSON_BYTES)
        self.assertLess(self.package_path.stat().st_size, dashboard_art.MAX_REVIEW_PACKAGE_BYTES)

    def test_large_bound_manifest_keeps_previews_and_human_review_available(self):
        self.large_package()
        before = (self.package_path.read_bytes(), self.package_path.stat().st_mtime_ns)
        value = dashboard_art.collect_art_snapshot(self.main, self.now)['requests'][0]
        self.assertTrue(value['can_resolve'])
        self.assertTrue(value['can_accept'])
        self.assertEqual(len(value['previews']), 1)
        self.assertIsNone(value['preview_error'])
        self.assertNotIn('files', value)
        self.assertEqual(dashboard_state.MAX_JSON_BYTES, 4 * 1024 * 1024)
        self.assertEqual((self.package_path.read_bytes(), self.package_path.stat().st_mtime_ns), before)
        self.assertEqual(dashboard_art.preview_png(self.main, self.item['id'], self.review_sha, 0), self.png.read_bytes())

    def test_large_manifest_tail_tampering_still_rejects_approval_and_preview(self):
        self.large_package()
        self.package_path.write_bytes(self.package_path.read_bytes() + b'\n')
        value = dashboard_art.collect_art_snapshot(self.main, self.now)['requests'][0]
        self.assertFalse(value['can_resolve'])
        self.assertFalse(value['can_accept'])
        self.assertIn('已变化', value['preview_error'])
        with self.assertRaisesRegex(ValueError, '已变化'):
            dashboard_art.preview_png(self.main, self.item['id'], self.review_sha, 0)

    def test_manifest_over_dedicated_limit_is_not_misreported_as_a_changed_version(self):
        with patch.object(dashboard_art, 'MAX_REVIEW_PACKAGE_BYTES', 32):
            value = dashboard_art.collect_art_snapshot(self.main, self.now)['requests'][0]
        self.assertFalse(value['can_resolve'])
        self.assertFalse(value['can_accept'])
        self.assertIn('读取上限', value['preview_error'])
        self.assertNotIn('已变化', value['preview_error'])

    def bind_package(self):
        write_json(self.package_path, self.package)
        self.review_sha = self.digest(self.package_path)
        self.item["review_package_sha256"] = self.review_sha
        self.save_queue()

    def budget(self, **amounts):
        return {"actual": 260.0, "reserved": 0.0, "unknown": 0.0, "committed": 260.0,
                "remaining": 2740.0, "limit": 3000.0, **amounts}

    def save_technical_review(self, *, sha=None, head=None, key="art_pending"):
        config = load_config(self.art_path)
        values = {name: config.get(name) for name in
                  ("project_path", "goal", "branch", "base_ref", "checks", "push", "remote", "task_prefix")}
        binding = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        record = {"id": self.item["id"], "review_package_sha256": sha or self.review_sha, "head_sha": "a" * 40,
                  "technical_review": {"head_sha": head or "a" * 40, "decision": "accepted",
                      "summary": "技术检查通过，等待用户视觉验收。<think>private-reasoning</think>",
                      "feedback": "保留高度差异。token=private-credential", "private_field": "private-review"}}
        write_json(self.art_runtime / "state.json", {"version": 1, "binding": binding, "phase": "PLAN", key: [record]})

    def save_active_task(self, phase="BLOCKED"):
        self.save_technical_review()
        path = self.art_runtime / "state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state.pop("art_pending", None)
        state.update(phase=phase, blocked_from="ART_REVIEW", last_error="检查通过。可观察缺陷：屋顶有穿插。阻塞及解除条件：修复屋顶后复查。",
                     task={"id": self.item["id"], "kind": "art", "art_request": {"artifacts_dir": str(self.artifacts)},
                           "art_review": {"review_package": str(self.package_path), "review_package_sha256": self.digest(self.package_path)}})
        write_json(path, state)
        return state

    def retry_state(self, phase="ART_REVIEW", reason="codex_subscription_limit"):
        self.item["status"] = "active"
        self.save_queue()
        state = self.save_active_task(phase)
        state.update(last_error="PRIVATE_OLD_PROVIDER_ERROR", provider_retry={
            "phase": phase, "task_id": self.item["id"], "head_sha": "a" * 40,
            "attempt": 2, "reason": reason, "next_retry_at": "2026-11-08T11:00:00+00:00", "status": "waiting",
            "operation_output_dir": "PRIVATE_PATH", "raw_error": "PRIVATE_PROVIDER_ERROR", "thinking": "PRIVATE_REASONING"})
        write_json(self.art_runtime / "state.json", state)
        return state

    def production_state(self, stage="concept"):
        state = self.save_active_task("CODE")
        config = load_config(self.art_path)
        batch_file = Path(config["art_pipeline"]["config_file"])
        write_json(batch_file, {"batch_id": "HOUSE-001", "artifacts_dir": str(self.artifacts),
            "assets": [{"asset_id": "house_l1"}, {"asset_id": "house_l4"}, {"asset_id": "house_l8"}],
            "key_file": str(self.root / "credentials-must-not-be-read.txt")})
        state["task"]["art_request"]["config_file"] = str(batch_file)
        state["operation"] = {"kind": "code", "task_id": self.item["id"],
                              "started_at": (self.now - timedelta(seconds=5)).isoformat()}
        write_json(self.art_runtime / "state.json", state)
        progress = {"version": 1, "batch_id": "HOUSE-001", "stage": stage,
                    "updated_epoch": self.now.timestamp(), "asset": "house_l1",
                    "error": "PRIVATE_ERROR", "log": "PRIVATE_PATH", "private_ledger": "PRIVATE_LEDGER"}
        write_json(self.artifacts / "pipeline-state.json", progress)
        return state, progress

    def test_production_progress_exposes_only_fresh_current_task_metadata_without_writes(self):
        self.production_state()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        value = dashboard_art.collect_art_snapshot(self.main, self.now)
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(value["production_progress"], {"stage": "concept", "label": "生成概念图",
            "asset": "house_l1", "updated_at": self.now.isoformat()})
        self.assertNotIn("PRIVATE_", json.dumps(value))
        self.assertFalse((self.root / "credentials-must-not-be-read.txt").exists())

    def test_production_progress_hides_old_foreign_or_untrusted_stage_records(self):
        for variant in ("old", "future", "nonfinite", "bool_time", "unknown_stage", "wrong_batch", "wrong_phase",
                        "wrong_kind", "foreign_task", "wrong_artifacts", "wrong_config", "missing_start"):
            state, progress = self.production_state()
            if variant == "old":
                progress["updated_epoch"] = self.now.timestamp() - 6
            elif variant == "future":
                progress["updated_epoch"] = self.now.timestamp() + 31
            elif variant == "nonfinite":
                progress["updated_epoch"] = float("nan")
            elif variant == "bool_time":
                progress["updated_epoch"] = True
            elif variant == "unknown_stage":
                progress["stage"] = "PRIVATE_STAGE"
            elif variant == "wrong_batch":
                progress["batch_id"] = "OTHER-BATCH"
            elif variant == "wrong_phase":
                state["phase"] = "ART_REVIEW"
            elif variant == "wrong_kind":
                state["operation"]["kind"] = "review"
            elif variant == "foreign_task":
                state["operation"]["task_id"] = "OTHER-TASK"
            elif variant == "wrong_artifacts":
                state["task"]["art_request"]["artifacts_dir"] = str(self.root / "other-artifacts")
            elif variant == "wrong_config":
                state["task"]["art_request"]["config_file"] = str(self.root / "other-config.json")
            else:
                state["operation"].pop("started_at")
            write_json(self.art_runtime / "state.json", state)
            write_json(self.artifacts / "pipeline-state.json", progress)
            with self.subTest(variant=variant):
                value = dashboard_art.collect_art_snapshot(self.main, self.now)
                self.assertIsNone(value["production_progress"])
                self.assertNotEqual(value["status"], "error")

    def test_production_stage_labels_do_not_claim_visual_approval_or_reuse_stale_asset(self):
        _, progress = self.production_state()
        for stage in dashboard_art.PRODUCTION_STAGES:
            progress["stage"] = stage
            write_json(self.artifacts / "pipeline-state.json", progress)
            with self.subTest(stage=stage):
                value = dashboard_art.collect_art_snapshot(self.main, self.now)["production_progress"]
                self.assertEqual(value["stage"], stage)
                self.assertEqual(value["asset"], "house_l1" if stage in {"concept", "model"} else None)
                if stage == "pending_visual_acceptance":
                    self.assertEqual(value["label"], "展示已生成，等待技术检查")
        progress.update(stage="model", asset="PRIVATE_PATH", updated_epoch=self.now.timestamp() + 30)
        write_json(self.artifacts / "pipeline-state.json", progress)
        value = dashboard_art.collect_art_snapshot(self.main, self.now)["production_progress"]
        self.assertEqual(value["stage"], "model")
        self.assertIsNone(value["asset"])

    def test_art_review_quota_wait_projects_safe_task_bound_retry_without_writes(self):
        self.retry_state()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        value = dashboard_art.collect_art_snapshot(self.main, self.now)
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(value["phase"], "ART_REVIEW")
        self.assertEqual(value["status"], "waiting_provider")
        self.assertEqual(value["label"], "等待 Codex 额度恢复")
        self.assertEqual(value["provider_retry"], {"phase": "ART_REVIEW", "task_id": "ART-001", "attempt": 2,
            "reason": "codex_subscription_limit", "reason_label": "Codex额度已用完", "provider": "Codex",
            "next_retry_at": "2026-11-08T11:00:00+00:00", "status": "waiting"})
        self.assertEqual(value["last_error"], "")
        self.assertNotIn("PRIVATE_", json.dumps(value))
        self.assertIsNone(value["error"])
        self.assertEqual(value["requests"][0]["preview_kind"], "technical")
        self.assertFalse(value["requests"][0]["can_accept"])
        self.assertFalse(value["requests"][0]["can_resolve"])

    def test_art_retry_wait_respects_pause_blocked_expiry_and_real_retrying_state(self):
        state = self.retry_state()
        state["provider_retry"]["next_retry_at"] = "2000-01-01T00:00:00+00:00"
        path = self.art_runtime / "state.json"
        write_json(path, state)
        self.assertEqual(dashboard_art.collect_art_snapshot(self.main, self.now)["status"], "waiting_provider")
        (self.art_runtime / "PAUSE").touch()
        self.assertEqual(dashboard_art.collect_art_snapshot(self.main, self.now)["status"], "paused")
        (self.art_runtime / "PAUSE").unlink()
        state["provider_retry"]["status"] = "retrying"
        write_json(path, state)
        value = dashboard_art.collect_art_snapshot(self.main, self.now)
        self.assertEqual(value["status"], "art_review")
        self.assertEqual(value["provider_retry"]["status"], "retrying")
        state["phase"] = "BLOCKED"
        write_json(path, state)
        value = dashboard_art.collect_art_snapshot(self.main, self.now)
        self.assertEqual(value["status"], "blocked")
        self.assertIsNone(value["provider_retry"])

    def test_art_retry_rejects_foreign_task_non_art_review_and_wrong_provider(self):
        for variant in ("foreign_task", "non_art_task", "wrong_phase", "claude_review_quota", "codex_code_quota"):
            state = self.retry_state()
            if variant == "foreign_task":
                state["provider_retry"]["task_id"] = "ART-OTHER"
            elif variant == "non_art_task":
                state["task"]["kind"] = "code"
            elif variant == "wrong_phase":
                state["provider_retry"]["phase"] = "REVIEW"
            elif variant == "claude_review_quota":
                state["provider_retry"]["reason"] = "claude_subscription_limit"
            else:
                state["phase"] = state["provider_retry"]["phase"] = "CODE"
            write_json(self.art_runtime / "state.json", state)
            with self.subTest(variant=variant):
                value = dashboard_art.collect_art_snapshot(self.main, self.now)
                self.assertEqual(value["status"], "error")
                self.assertIsNone(value["provider_retry"])
                self.assertNotIn("PRIVATE_", json.dumps(value))

    def test_art_claude_code_quota_and_generic_review_wait_keep_their_own_labels(self):
        for phase, reason, label, provider in (
                ("CODE", "claude_subscription_limit", "等待 Claude 额度恢复", "Claude Code"),
                ("ART_REVIEW", "transport", "等待服务恢复", "Codex"),
                ("ART_REVIEW", "local_cli_unavailable", "等待本地 Codex 恢复", "Codex")):
            with self.subTest(phase=phase, reason=reason):
                self.retry_state(phase, reason)
                value = dashboard_art.collect_art_snapshot(self.main, self.now)
                self.assertEqual(value["status"], "waiting_provider")
                self.assertEqual(value["label"], label)
                self.assertEqual(value["provider_retry"]["provider"], provider)

    def test_active_technical_preview_uses_current_bound_state_and_never_allows_review(self):
        old_sha = self.review_sha
        self.package["revision"] = 2
        self.package["budget"] = {"budget": {**self.budget(), "breached": False}}
        write_json(self.package_path, self.package)
        new_sha = self.digest(self.package_path)
        self.item["status"] = "active"
        self.save_queue()
        for phase in ("ART_CHECK", "ART_REVIEW", "BLOCKED"):
            with self.subTest(phase=phase):
                self.save_active_task(phase)
                before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
                row = dashboard_art.collect_art_snapshot(self.main, self.now)["requests"][0]
                self.assertEqual(row["preview_kind"], "technical")
                self.assertEqual(row["preview_sha256"], new_sha)
                self.assertEqual(row["review_package_sha256"], old_sha)
                self.assertIn(new_sha, row["previews"][0]["url"])
                self.assertFalse(row["can_resolve"])
                self.assertFalse(row["reviewable"])
                self.assertFalse(row["can_accept"])
                self.assertIsNone(row["technical_review"])
                self.assertIsNone(row["preview_error"])
                self.assertIn("技术预览", row["accept_block_reason"])
                self.assertEqual(dashboard_art.preview_png(self.main, self.item["id"], new_sha, 0), self.png.read_bytes())
                with self.assertRaises(ValueError):
                    dashboard_art.preview_png(self.main, self.item["id"], old_sha, 0)
                after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
                self.assertEqual(before, after)

    def test_technical_preview_rejects_missing_mismatched_or_changed_state_evidence(self):
        self.item["status"] = "active"
        self.save_queue()
        cases = ("missing", "wrong_task", "wrong_kind", "wrong_artifacts", "wrong_package_path", "wrong_sha", "wrong_binding", "code")
        for case in cases:
            with self.subTest(case=case):
                state = self.save_active_task()
                task = state["task"]
                if case == "missing":
                    task.pop("art_review")
                elif case == "wrong_task":
                    task["id"] = "OTHER"
                elif case == "wrong_kind":
                    task["kind"] = "feature"
                elif case == "wrong_artifacts":
                    task["art_request"]["artifacts_dir"] = str(self.root)
                elif case == "wrong_package_path":
                    task["art_review"]["review_package"] = str(self.root / "other.json")
                elif case == "wrong_sha":
                    task["art_review"]["review_package_sha256"] = "f" * 64
                elif case == "wrong_binding":
                    state["binding"] = "f" * 64
                else:
                    state["phase"] = "CODE"
                write_json(self.art_runtime / "state.json", state)
                value = dashboard_art.collect_art_snapshot(self.main, self.now)
                if case == "wrong_binding":
                    self.assertEqual(value["status"], "error")
                else:
                    row = value["requests"][0]
                    self.assertEqual(row["previews"], [])
                    self.assertFalse(row["can_accept"])
                    self.assertTrue(row["preview_error"])
                with self.assertRaises(ValueError):
                    dashboard_art.preview_png(self.main, self.item["id"], self.review_sha, 0)

    def test_rework_code_without_current_evidence_is_pending_not_a_stale_package_error(self):
        self.item["status"] = "active"
        self.save_queue()
        state = self.save_active_task("CODE")
        state["task"].pop("art_review")
        write_json(self.art_runtime / "state.json", state)
        self.package["revision"] = "new_in_progress"
        write_json(self.package_path, self.package)
        with patch.object(dashboard_art, "_package", side_effect=AssertionError("Do not inspect stale queue package")):
            row = dashboard_art.collect_art_snapshot(self.main, self.now)["requests"][0]
            self.assertIsNone(row["preview_kind"])
            self.assertIsNone(row["preview_sha256"])
            self.assertIsNone(row["preview_error"])
            self.assertEqual(row["previews"], [])
            self.assertFalse(row["can_resolve"])
            self.assertFalse(row["can_accept"])
            self.assertIn("正在制作新版", row["accept_block_reason"])
            with self.assertRaisesRegex(ValueError, "正在制作新版"):
                dashboard_art.preview_png(self.main, self.item["id"], self.review_sha, 0)

    def test_technical_png_rechecks_current_package_and_image_hash_after_snapshot(self):
        self.item["status"] = "active"
        self.save_queue()
        self.save_active_task()
        row = dashboard_art.collect_art_snapshot(self.main, self.now)["requests"][0]
        self.assertEqual(row["preview_kind"], "technical")
        self.package["revision"] = 3
        write_json(self.package_path, self.package)
        with self.assertRaises(ValueError):
            dashboard_art.preview_png(self.main, self.item["id"], self.review_sha, 0)
        self.bind_package()
        self.save_active_task()
        self.png.write_bytes(self.png.read_bytes() + b"modified")
        with self.assertRaises(ValueError):
            dashboard_art.preview_png(self.main, self.item["id"], self.review_sha, 0)

    def test_normal_awaiting_approval_keeps_queue_version_and_review_permissions(self):
        self.package["budget"] = {"budget": {**self.budget(), "breached": False}}
        self.bind_package()
        row = dashboard_art.collect_art_snapshot(self.main, self.now)["requests"][0]
        self.assertEqual(row["preview_kind"], "review")
        self.assertEqual(row["preview_sha256"], self.review_sha)
        self.assertTrue(row["reviewable"])
        self.assertTrue(row["can_resolve"])
        self.assertTrue(row["can_accept"])

    def test_blocking_detail_is_bounded_and_summary_prefers_actual_defects(self):
        state = self.save_active_task()
        for marker in ("可观察缺陷：", "必须修复:", "阻塞及解除条件："):
            with self.subTest(marker=marker):
                state["last_error"] = "<think>可观察缺陷：PRIVATE_REASONING</think>" + "各项检查通过。" * 100 + marker + "屋顶穿插，需要修复。 token=private-secret " + "细节。" * 3000
                write_json(self.art_runtime / "state.json", state)
                value = dashboard_art.collect_art_snapshot(self.main, self.now)
                self.assertTrue(value["blocking_summary"].startswith(marker))
                self.assertIn("屋顶穿插", value["blocking_summary"])
                self.assertLessEqual(len(value["blocking_detail"]), 8000)
                self.assertLessEqual(len(value["blocking_summary"]), 600)
                self.assertNotIn("PRIVATE_REASONING", value["blocking_summary"])
                self.assertNotIn("private-secret", value["blocking_detail"])
        state.update(phase="ART_REVIEW", last_error="上一轮阻塞信息")
        write_json(self.art_runtime / "state.json", state)
        value = dashboard_art.collect_art_snapshot(self.main, self.now)
        self.assertIsNone(value["blocking_detail"])
        self.assertIsNone(value["blocking_summary"])

    def test_budget_and_technical_review_are_bound_allowlisted_and_read_only(self):
        self.package["budget"] = {"budget": self.budget(reserved=65, unknown=75, committed=400, remaining=2600),
                                  "private_ledger": str(self.root / "private-ledger-do-not-open.json")}
        self.bind_package()
        self.save_technical_review()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        original_open = Path.open
        def allowed_open(path, *args, **kwargs):
            self.assertNotIn("private-ledger", str(path))
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", allowed_open):
            value = dashboard_art.collect_art_snapshot(self.main, self.now)
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        row = value["requests"][0]
        self.assertEqual(row["budget"], self.budget(reserved=65, unknown=75, committed=400, remaining=2600))
        self.assertIsNone(row["budget_error"])
        self.assertEqual(row["technical_review"]["summary"], "技术检查通过，等待用户视觉验收。")
        self.assertEqual(row["technical_review"]["review_package_sha256"], self.review_sha)
        self.assertIsNone(row["decision"])
        self.assertTrue(row["can_resolve"])
        self.assertNotIn("private-", json.dumps(value))

    def test_malformed_budget_is_unavailable_without_hiding_valid_preview(self):
        invalid = [None, [], {}, {"budget": []}, {"budget": {"actual": 0}}]
        for field in ("actual", "reserved", "unknown", "committed", "remaining", "limit"):
            for amount in (None, True, "260", -1, float("nan"), float("inf"), 10 ** 400):
                invalid.append({"budget": self.budget(**{field: amount})})
        invalid.extend({"budget": values} for values in
                       (self.budget(limit=0), self.budget(committed=261), self.budget(remaining=2741),
                        self.budget(reserved=1), self.budget(unknown=1),
                        self.budget(actual=10 ** 308, reserved=10 ** 308)))
        for budget in invalid:
            with self.subTest(budget=budget):
                self.package["budget"] = budget
                self.bind_package()
                row = dashboard_art.collect_art_snapshot(self.main, self.now)["requests"][0]
                self.assertIsNone(row["budget"])
                self.assertTrue(row["budget_error"])
                self.assertTrue(row["can_resolve"])
                self.assertFalse(row["can_accept"])
                self.assertTrue(row["accept_block_reason"])
                self.assertIsNone(row["preview_error"])

    def test_over_budget_snapshot_retains_actual_amount_and_zero_remaining(self):
        self.package["budget"] = {"budget": self.budget(actual=3100, committed=3100, remaining=0)}
        self.bind_package()
        row = dashboard_art.collect_art_snapshot(self.main, self.now)["requests"][0]
        self.assertEqual(row["budget"]["actual"], 3100)
        self.assertEqual(row["budget"]["remaining"], 0)
        self.assertFalse(row["can_accept"])
        self.assertTrue(row["can_resolve"])
        self.assertIn("超限", row["accept_block_reason"])

    def test_can_accept_requires_settled_valid_budget_and_keeps_rework_available(self):
        for changes, allowed in (({}, True), ({"actual": 3000, "committed": 3000, "remaining": 0}, True),
                                 ({"reserved": 65, "committed": 325, "remaining": 2675}, False),
                                 ({"unknown": 65, "committed": 325, "remaining": 2675}, False),
                                 ({"limit": 4000, "remaining": 3740}, False), ({"breached": True}, False),
                                 ({"breached": None}, False), ({"breached": "false"}, False),
                                 ({"breached": 0}, False)):
            with self.subTest(changes=changes):
                self.package["budget"] = {"budget": {**self.budget(), "breached": False, **changes}}
                self.bind_package()
                row = dashboard_art.collect_art_snapshot(self.main, self.now)["requests"][0]
                self.assertEqual(row["can_accept"], allowed)
                self.assertEqual(row["accept_block_reason"] is None, allowed)
                self.assertTrue(row["can_resolve"])
                self.assertIsNotNone(row["budget"])
        del self.package["budget"]["budget"]["breached"]
        self.bind_package()
        self.assertFalse(dashboard_art.collect_art_snapshot(self.main, self.now)["requests"][0]["can_accept"])

    def test_missing_budget_or_existing_decision_blocks_acceptance(self):
        row = dashboard_art.collect_art_snapshot(self.main, self.now)["requests"][0]
        self.assertFalse(row["can_accept"])
        self.assertTrue(row["can_resolve"])
        self.assertTrue(row["accept_block_reason"])
        self.package["budget"] = {"budget": {**self.budget(), "breached": False}}
        for changes in ({"decision": {"decision": "rework", "feedback": "调整院门"}}, {"status": "accepted"}):
            with self.subTest(changes=changes):
                self.item.pop("decision", None)
                self.item.update(changes)
                self.bind_package()
                row = dashboard_art.collect_art_snapshot(self.main, self.now)["requests"][0]
                self.assertFalse(row["can_accept"])
                self.assertFalse(row["can_resolve"])
                self.assertTrue(row["accept_block_reason"])

    def test_spoofed_or_stale_package_cannot_supply_budget_or_technical_review(self):
        self.package["budget"] = {"budget": self.budget()}
        self.bind_package()
        self.item["review_package_sha256"] = "f" * 64
        self.save_queue()
        self.save_technical_review(sha="f" * 64)
        for stale in (False, True):
            with self.subTest(stale=stale):
                if stale:
                    self.bind_package()
                    self.save_technical_review()
                    self.package["budget"]["budget"]["actual"] = 0
                    write_json(self.package_path, self.package)
                row = dashboard_art.collect_art_snapshot(self.main, self.now)["requests"][0]
                self.assertIsNone(row["budget"])
                self.assertIsNone(row["technical_review"])
                self.assertTrue(row["preview_error"])
                self.assertFalse(row["can_resolve"])
                self.assertFalse(row["can_accept"])
                self.assertEqual(row["accept_block_reason"], row["preview_error"])

    def test_technical_review_matches_current_version_and_head_in_pending_or_history(self):
        self.package["budget"] = {"budget": self.budget()}
        self.bind_package()
        for mismatch in ({"sha": "b" * 64}, {"head": "b" * 40}):
            with self.subTest(mismatch=mismatch):
                self.save_technical_review(**mismatch)
                row = dashboard_art.collect_art_snapshot(self.main, self.now)["requests"][0]
                self.assertIsNone(row["technical_review"])
                self.assertEqual(row["budget"]["actual"], 260)
        self.save_technical_review(key="art_history")
        row = dashboard_art.collect_art_snapshot(self.main, self.now)["requests"][0]
        self.assertEqual(row["technical_review"]["decision"], "accepted")

    def test_polling_is_read_only_and_does_not_construct_engine_or_models(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        with patch("unattended.art_worker.ArtWorker", side_effect=AssertionError("No worker on GET")), \
             patch("unattended.providers.AgentProviders", side_effect=AssertionError("No provider on GET")):
            value = dashboard_state.collect_snapshot(self.main_path)
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(value["art"]["status"], "awaiting_approval")
        row = value["art"]["requests"][0]
        self.assertTrue(row["can_resolve"])
        self.assertIn(self.review_sha, row["previews"][0]["url"])
        self.assertNotIn("private-", json.dumps(value))

    def test_planned_backlog_is_sorted_read_only_and_never_enters_generation_queue(self):
        write_json(self.art_runtime / "art-backlog.json", {"version": 1, "batches": [
            {"id": "MARKET", "title": "市场与仓储", "priority": 2, "status": "waiting_budget", "definition_ids": ["building.market"]},
            {"id": "HOUSING", "title": "普通住宅其余等级", "priority": 1, "status": "waiting_sample_acceptance", "levels": ["L2", "L3"]}]})
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        value = dashboard_art.collect_art_snapshot(self.main, self.now)
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual([b["id"] for b in value["backlog"]], ["HOUSING", "MARKET"])
        self.assertTrue(all(b["dispatchable"] is False for b in value["backlog"]))
        self.assertEqual([r["id"] for r in value["requests"]], ["ART-001"])
        self.assertEqual(value["status"], "awaiting_approval")

    def test_invalid_backlog_is_isolated_from_active_queue_and_optional_when_missing(self):
        self.assertEqual(dashboard_art.collect_art_snapshot(self.main, self.now)["backlog"], [])
        write_json(self.art_runtime / "art-backlog.json", {"version": 1, "batches": [
            {"id": "MARKET", "title": "市场", "priority": 2, "status": "queued"}]})
        value = dashboard_art.collect_art_snapshot(self.main, self.now)
        self.assertTrue(value["backlog_error"])
        self.assertIsNone(value["error"])
        self.assertTrue(value["requests"][0]["can_resolve"])

    def test_preview_binds_package_and_image_and_does_not_allow_escape(self):
        self.assertEqual(dashboard_art.preview_png(self.main, "ART-001", self.review_sha, 0), self.png.read_bytes())
        with self.assertRaises(ValueError):
            dashboard_art.preview_png(self.main, "ART-001", "f" * 64, 0)
        self.png.write_bytes(self.png.read_bytes() + b"changed")
        with self.assertRaises(ValueError):
            dashboard_art.preview_png(self.main, "ART-001", self.review_sha, 0)
        outside = self.root / "secret.png"
        outside.write_bytes(self.png.read_bytes())
        self.package["capture"]["shots"][0].update(path=str(outside), sha256=self.digest(outside))
        write_json(self.package_path, self.package)
        self.item["review_package_sha256"] = self.digest(self.package_path)
        self.save_queue()
        with self.assertRaisesRegex(ValueError, "受管目录"):
            dashboard_art.preview_png(self.main, "ART-001", self.item["review_package_sha256"], 0)

    def test_changed_package_disables_approval_and_main_status_stays_readable(self):
        self.package["new"] = True
        write_json(self.package_path, self.package)
        value = dashboard_state.collect_snapshot(self.main_path)
        self.assertEqual(value["status"]["code"], "not_initialized")
        self.assertFalse(value["art"]["requests"][0]["can_resolve"])
        self.assertTrue(value["art"]["requests"][0]["preview_error"])

    def test_overlapping_worker_root_is_rejected_without_writing_main_state(self):
        config = json.loads(self.art_path.read_text())
        config["project_path"] = self.main["project_path"]
        write_json(self.art_path, config)
        value = dashboard_state.collect_snapshot(self.main_path)
        self.assertEqual(value["art"]["status"], "error")
        self.assertEqual(value["status"]["code"], "not_initialized")
        self.assertFalse(Path(self.main["runtime_dir"]).exists())

    def test_cache_action_routes_only_to_dedicated_worker_with_exact_sha(self):
        cache = dashboard.SnapshotCache(self.main_path)
        payload = {"task_id": "ART-001", "decision": "rework", "feedback": "调整院门", "review_package_sha256": self.review_sha}
        with patch("unattended.art_worker.ArtWorker.from_main_config") as route:
            route.return_value.resolve_art.return_value = {"status": "decision_queued"}
            self.assertEqual(cache.resolve_art(payload)["status"], "decision_queued")
        route.assert_called_once()
        route.return_value.resolve_art.assert_called_once_with("ART-001", "rework", "调整院门", self.review_sha)
        self.assertFalse(Path(self.main["runtime_dir"]).exists())

    def test_real_queue_rejects_competing_review_without_main_state_or_models(self):
        from concurrent.futures import ThreadPoolExecutor
        cache = dashboard.SnapshotCache(self.main_path)
        def submit(feedback):
            try:
                return cache.resolve_art({"task_id": "ART-001", "decision": "rework", "feedback": feedback,
                                          "review_package_sha256": self.review_sha})["status"]
            except (ValueError, RuntimeError, OSError):
                return "conflict"
        with patch("unattended.engine.AgentProviders") as providers, ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(submit, ("院门修改 A", "院门修改 B")))
        self.assertEqual(sorted(outcomes), ["conflict", "decision_queued"])
        queue = json.loads((self.art_runtime / "art-queue.json").read_text(encoding="utf-8"))
        self.assertIn(queue["requests"][0]["decision"]["feedback"], {"院门修改 A", "院门修改 B"})
        providers.return_value.code.assert_not_called()
        self.assertFalse(Path(self.main["runtime_dir"]).exists())

    def test_malformed_preview_metadata_is_a_controlled_value_error(self):
        write_json(self.art_runtime / "art-queue.json", {"version": 1, "requests": [None]})
        with self.assertRaises(ValueError):
            dashboard_art.preview_png(self.main, "ART-001", self.review_sha, 0)
        self.save_queue()
        self.package["capture"] = []
        write_json(self.package_path, self.package)
        self.item["review_package_sha256"] = self.digest(self.package_path)
        self.save_queue()
        with self.assertRaises(ValueError):
            dashboard_art.preview_png(self.main, "ART-001", self.item["review_package_sha256"], 0)

    def test_cli_routes_art_without_constructing_main_engine(self):
        with patch("unattended.art_worker.ArtWorker.from_main_config") as route, \
             patch("unattended.engine.Engine", side_effect=AssertionError("No main Engine")), redirect_stdout(io.StringIO()):
            route.return_value.resolve_art.return_value = {"status": "decision_queued"}
            code = cli.main(["art", "resolve", "--config", str(self.main_path), "--task", "ART-001", "--decision", "accepted",
                             "--feedback", "认可本版", "--review-package-sha256", self.review_sha])
        self.assertEqual(code, 0)
        route.return_value.resolve_art.assert_called_once_with("ART-001", "accepted", "认可本版", self.review_sha)


class ArtActionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.cache = Mock()
        self.cache.action_token = "one-instance-secret-token"
        self.cache.resolve_art.return_value = {"status": "decision_queued"}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(self.cache, {}))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.payload = {"task_id": "ART-001", "decision": "accepted", "feedback": "认可", "review_package_sha256": "a" * 64}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def post(self, payload=None, headers=None, method="POST"):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        valid = {"Origin": f"http://127.0.0.1:{self.server.server_port}", "Content-Type": "application/json",
                 "X-Art-Action-Token": self.cache.action_token}
        if headers is not None:
            valid.update(headers)
        connection.request(method, "/api/art/resolve", json.dumps(payload or self.payload), valid)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def test_explicit_json_review_requires_same_origin_and_instance_token(self):
        for headers in ({"Origin": "https://outside.invalid"}, {"X-Art-Action-Token": "stale"}, {"Origin": "null"}):
            self.assertEqual(self.post(headers=headers)[0], 403)
        self.cache.resolve_art.assert_not_called()
        self.assertEqual(self.post()[0], 200)
        self.cache.resolve_art.assert_called_once_with(self.payload)

    def test_invalid_actions_methods_and_changed_version_fail_closed(self):
        self.assertEqual(self.post({**self.payload, "decision": "publish"})[0], 409)
        self.assertEqual(self.post({**self.payload, "extra": "arbitrary-path"})[0], 409)
        self.assertEqual(self.post(method="PUT")[0], 405)
        self.cache.resolve_art.assert_not_called()
        self.cache.resolve_art.side_effect = ValueError("验收包已变化")
        status, body = self.post()
        self.assertEqual(status, 409)
        self.assertIn("验收包已变化", body.decode())


if __name__ == "__main__":
    unittest.main()
