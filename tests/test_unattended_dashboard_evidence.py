from __future__ import annotations

from copy import deepcopy
import hashlib
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from unattended import dashboard, dashboard_evidence as e


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def binding(config):
    keys = ("project_path", "goal", "branch", "base_ref", "checks", "push", "remote", "task_prefix")
    return hashlib.sha256(json.dumps({k: config.get(k) for k in keys}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class NodeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "dev"
        self.config = {"project_path": str(self.root / "source"), "runtime_dir": str(self.runtime), "branch": "main"}
        self.config_path = self.root / "main.json"
        self.load = patch.object(e, "load_config", return_value=self.config); self.load.start(); self.addCleanup(self.load.stop)
        self.run = self.runtime / "runs" / ("code-" + "a" * 32)
        self.run.mkdir(parents=True)
        self.state = {"version": 1, "binding": binding(self.config), "phase": "CODE", "expected_head": "a" * 40,
                      "task": {"id": "TASK-1", "code_base": "a" * 40, "candidate": {"head_sha": "a" * 40}, "reworks": 0},
                      "operation": {"kind": "code", "task_id": "TASK-1", "head_sha": "a" * 40, "output_dir": str(self.run)}}
        self.save()

    def save(self): write(self.runtime / "state.json", self.state)
    def detail(self, lane="development", node="code"): return e.collect_node_detail(self.config_path, lane, node)
    def evidence(self, ref, lane="development", node="code"): return e.read_node_evidence(self.config_path, lane, node, ref)

    def test_native_chat_never_exposed_and_live_log_append_keeps_reference(self):
        stdout = self.run / "stdout.log"
        stdout.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "FULL_PRIVATE_CONVERSATION"},
            {"type": "tool_use", "name": "Read", "input": {"path": "secret"}}]}}) + "\n", encoding="utf-8")
        detail = self.detail(); ref = detail["logs"][0]["id"]
        with stdout.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "result", "subtype": "success", "is_error": False}) + "\n")
        data, mime, filename = self.evidence(ref)
        self.assertIn("读取文件", data.decode()); self.assertIn("本轮执行结束", data.decode())
        self.assertNotIn(b"FULL_PRIVATE_CONVERSATION", data)
        self.assertNotIn(str(self.run), json.dumps(detail))
        self.assertIsNone(filename); self.assertTrue(mime.startswith("text/plain"))

    def test_old_task_or_operation_reference_cannot_be_reused(self):
        (self.run / "stdout.log").write_text('{"type":"turn.started"}\n')
        ref = self.detail()["logs"][0]["id"]
        self.state["task"]["id"] = "TASK-2"; self.state["operation"]["task_id"] = "TASK-2"; self.save()
        with self.assertRaisesRegex(ValueError, "范围已变化"):
            self.evidence(ref)

    def test_unknown_node_and_arbitrary_ref_are_rejected(self):
        for lane, node in (("private", "code"), ("development", "../../config")):
            with self.assertRaises(ValueError): self.detail(lane, node)
        with self.assertRaises(ValueError): self.evidence(str(self.root / "secret.txt"))

    def test_check_log_tail_is_bounded_and_redacts_credentials_and_urls(self):
        self.state["phase"] = "CHECK"; self.state["operation"]["kind"] = "check"; self.save()
        (self.run / "stdout.log").write_text("old prefix\n" * 20000 +
            "api_key=DO_NOT_LEAK https://files.example/model?signature=LEAK\n<script>alert(1)</script>\n", encoding="utf-8")
        ref = self.detail(node="check")["logs"][0]["id"]
        data, mime, _ = self.evidence(ref, node="check")
        self.assertLessEqual(len(data), e.MAX_TEXT * 3)
        self.assertNotIn(b"DO_NOT_LEAK", data); self.assertNotIn(b"signature", data)
        self.assertIn(b"<script>", data)  # text/plain + nosniff, never HTML rendering.
        self.assertTrue(mime.startswith("text/plain"))

    def test_unrelated_check_result_head_or_run_path_is_not_served(self):
        self.state["operation"] = None
        self.state["task"]["check_results"] = [{"id": "check", "head_sha": "b" * 40,
                                                  "stdout_path": str(self.run / "stdout.log")}]
        self.save()
        self.assertFalse(self.detail(node="check")["logs"])
        self.assertFalse(self.detail(node="check")["artifacts"])

    def test_file_replacement_invalidates_live_reference(self):
        path = self.run / "stdout.log"; path.write_text('{"type":"turn.started"}\n')
        ref = self.detail()["logs"][0]["id"]
        replaced = self.run / "new.log"; replaced.write_text("replacement")
        replaced.replace(path)
        with self.assertRaises(ValueError): self.evidence(ref)

    def test_symlink_escape_is_refused(self):
        secret = self.root / "secret.txt"; secret.write_text("DO_NOT_LEAK")
        try: (self.run / "stdout.log").symlink_to(secret)
        except OSError: self.skipTest("symlink unavailable")
        with self.assertRaises(ValueError): self.detail()

    def art(self):
        runtime = self.root / "art"
        public = runtime / "batches/BATCH"
        workspace = runtime / "workspace"
        recipe = workspace / "recipe.json"; job = workspace / "job.py"
        write(recipe, {"assets": [{"asset_id": "house_l1", "revision": "v003"}]})
        job.write_text("def run_job(context): pass\n")
        batch_path = self.root / "approved/batch.json"
        write(batch_path, {"batch_id": "BATCH", "recipe_file": str(recipe), "blender_job_file": str(job)})
        config = {"project_path": str(self.root / "art-source"), "runtime_dir": str(runtime), "branch": "art",
                  "art_pipeline": {"config_file": str(batch_path), "artifacts_dir": str(public), "task_ids": ["TASK-ART-1"]}}
        request = {"config_file": str(batch_path), "artifacts_dir": str(public), "config_sha256": e._digest(batch_path)}
        state = {"version": 1, "binding": binding(config), "phase": "ART_REVIEW", "operation": None,
                 "task": {"id": "TASK-ART-1", "kind": "art", "art_workspace_head": "a" * 40, "reworks": 0, "art_request": request}}
        patcher = patch.object(e, "worker_config", return_value=config); patcher.start(); self.addCleanup(patcher.stop)
        shot = public / "screenshots/version/shot.png"; shot.parent.mkdir(parents=True); shot.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        package = {"capture": {"shots": [{"path": str(shot), "sha256": e._digest(shot), "name": "front"}]}}
        write(public / "review-package.json", package)
        state["task"]["art_review"] = {"review_package_sha256": e._digest(public / "review-package.json")}
        write(runtime / "state.json", state)
        return runtime, public, state, shot

    def test_art_png_is_package_bound_and_changed_bytes_fail(self):
        _, _, _, shot = self.art()
        detail = self.detail("art", "ue_capture")
        png = next(a for a in detail["artifacts"] if a["kind"] == "png")
        data, mime, _ = self.evidence(png["id"], "art", "ue_capture")
        self.assertEqual(mime, "image/png"); self.assertEqual(data, shot.read_bytes())
        shot.write_bytes(b"\x89PNG\r\n\x1a\ntampered")
        with self.assertRaisesRegex(ValueError, "SHA"):
            self.evidence(png["id"], "art", "ue_capture")

    def test_art_rework_does_not_serve_previous_package(self):
        runtime, _, state, _ = self.art()
        old = next(a for a in self.detail("art", "ue_capture")["artifacts"] if a["kind"] == "png")
        state["phase"] = "CODE"; state["task"].pop("art_review"); state["task"]["reworks"] += 1
        write(runtime / "state.json", state)
        self.assertFalse(self.detail("art", "ue_capture")["artifacts"])
        with self.assertRaises(ValueError): self.evidence(old["id"], "art", "ue_capture")

    def test_active_capture_log_is_derived_from_current_import_without_old_package(self):
        from unattended.art_pipeline import capture_fingerprint
        runtime, public, state, _ = self.art()
        state["phase"] = "CODE"; state["task"].pop("art_review")
        write(runtime / "state.json", state)
        batch = json.loads((self.root / "approved/batch.json").read_text())
        source = {"batch_id": "BATCH", "recipe": {"sha256": e._digest(Path(batch["recipe_file"]))},
                  "blender_job": {"sha256": e._digest(Path(batch["blender_job_file"]))}}
        write(public / "production-input.json", source)
        run = "a" * 12
        manifest = public / "processed" / run / "assets_manifest.json"
        write(manifest, {"input_manifest_sha256": e._digest(public / "production-input.json")})
        write(public / "pipeline-state.json", {"stage": "ue_capture", "recipe": run, "manifest": str(manifest)})
        receipt = public / "receipts" / (run + "-import.json")
        write(receipt, {"status": "passed", "run": run, "manifest_sha256": e._digest(manifest)})
        capture = capture_fingerprint(run, receipt, Path(e.__file__).resolve().parent.parent / "art_tools/ue/capture_preview.py")
        log = public / "logs" / (capture + "-ue-capture.log"); log.parent.mkdir(); log.write_text("rendering frame\n")
        detail = self.detail("art", "ue_capture")
        self.assertEqual(len(detail["logs"]), 1); self.assertFalse(detail["artifacts"])
        self.assertIn(b"rendering frame", self.evidence(detail["logs"][0]["id"], "art", "ue_capture")[0])
        write(receipt, {"status": "passed", "run": run, "manifest_sha256": "b" * 64})
        self.assertFalse(self.detail("art", "ue_capture")["logs"])

    def test_verified_subscription_fallback_exposes_both_attempts_without_chat(self):
        directory = self.runtime / "runs" / ("review-" + "b" * 32); directory.mkdir()
        child = directory / "codex-subscription"; child.mkdir()
        self.state["phase"] = "REVIEW"
        self.state["operation"].update(kind="review", output_dir=str(directory)); self.save()
        write(directory / "process.json", {"kind": "review", "codex_auth_mode": "api_key", "exit_code": 1,
              "codex_subscription_fallback": True, "timed_out": False, "output_limit_exceeded": False})
        opening = '{"type":"thread.started","thread_id":"thread-api"}\n{"type":"turn.started"}\n'
        (directory / "stdout.log").write_text(opening + json.dumps({"type": "turn.failed", "error": {
            "message": "Quota exceeded. Check your plan and billing details."}}) + "\n")
        write(child / "process.json", {"kind": "review", "codex_auth_mode": "chatgpt", "exit_code": 0})
        (child / "stdout.log").write_text('{"type":"turn.completed"}\n')
        detail = self.detail(node="review")
        self.assertTrue(any("订阅尝试" in row["title"] for row in detail["logs"]))
        (directory / "stdout.log").write_text('{"type":"turn.failed","error":{"message":"unrelated error"}}\n')
        self.assertFalse(any("订阅尝试" in row["title"] for row in self.detail(node="review")["logs"]))

    def test_service_scope_does_not_claim_current_task_or_expire_when_task_changes(self):
        path = self.runtime / "background.log"; path.write_text('service running\n')
        detail = self.detail("operations", "development_worker")
        self.assertIsNone(detail["task_id"])
        ref = detail["logs"][0]["id"]
        self.state["task"]["id"] = "TASK-2"; self.state["expected_head"] = "b" * 40; self.save()
        self.assertIn(b"service running", self.evidence(ref, "operations", "development_worker")[0])

    def test_old_approved_promotion_uses_its_history_not_new_active_task(self):
        runtime, _, state, _ = self.art()
        approved = json.loads(json.dumps(state["task"]))
        approved["id"] = "TASK-ART-OLD"
        package_sha = "c" * 64
        state["art_history"] = [{"id": approved["id"], "status": "accepted", "task": approved,
                                 "review_package_sha256": package_sha,
                                 "visual_decision": {"decision": "accepted"}}]
        write(runtime / "state.json", state)
        request = {"id": "art-promotion-old", "source_task_id": approved["id"], "status": "blocked",
                   "batch_id": "OLD-BATCH", "review_package_sha256": package_sha,
                   "message": "target editor open"}
        write(runtime / "promotion-requests.json", {"requests": [request]})
        detail = self.detail("art", "promotion")
        self.assertEqual(detail["task_id"], "TASK-ART-OLD")
        self.assertIn("已批准", detail["scope_label"])
        self.assertEqual(len(detail["artifacts"]), 1)
        ref = detail["artifacts"][0]["id"]
        self.assertIn(b"OLD-BATCH", self.evidence(ref, "art", "promotion")[0])
        request["review_package_sha256"] = "d" * 64
        write(runtime / "promotion-requests.json", {"requests": [request]})
        self.assertIsNone(self.detail("art", "promotion")["task_id"])
        with self.assertRaises(ValueError): self.evidence(ref, "art", "promotion")


class EvidenceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.cache = Mock()
        self.cache.flow_detail.return_value = {"lane": "art", "node": "blender", "logs": [], "artifacts": []}
        self.cache.flow_evidence.return_value = (b"safe", "application/octet-stream", "asset.blend")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(self.cache, {}))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def request(self, path, headers=None):
        c = HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            c.request("GET", path, headers=headers or {})
            r = c.getresponse(); return r.status, dict(r.getheaders()), r.read()
        finally: c.close()

    def test_exact_read_only_queries_and_safe_download_headers(self):
        self.assertEqual(self.request("/api/flow/detail?lane=art&node=blender")[0], 200)
        self.cache.flow_detail.assert_called_once_with("art", "blender")
        for query in ("lane=art&node=blender&path=secret", "lane=art&lane=development&node=blender", "lane=art"):
            self.assertEqual(self.request("/api/flow/detail?" + query)[0], 409)
        status, headers, data = self.request("/api/flow/evidence?lane=art&node=blender&ref=" + "a" * 64)
        self.assertEqual(status, 200); self.assertEqual(data, b"safe")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.request("/api/flow/detail?lane=art&node=blender", {"Origin": "https://foreign.example"})[0], 403)

    def test_flows_failure_does_not_break_snapshot_or_notifications(self):
        with patch.object(dashboard, "collect_snapshot", return_value={"project": {"name": "fixture"}}):
            cache = dashboard.SnapshotCache("unused.json")
        cache.notification_status({"enabled": True})
        with patch("unattended.dashboard_flows.collect_flows", side_effect=ValueError("unavailable")):
            value = cache.get()
        self.assertEqual(value["project"]["name"], "fixture")
        self.assertTrue(value["notifications"]["enabled"])
        self.assertIn("error", value["flows"])


if __name__ == "__main__": unittest.main()
