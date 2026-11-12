"""Paid-job recovery and accounting tests. All network responses are local fakes."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

from unattended.art_tripo import BatchBusy, BudgetExceeded, RequestConflict, TripoClient, TripoError


class Response(io.BytesIO):
    def __init__(self, payload=b"model-content", url="https://cdn.example/model.glb?auth_key=private", expected=None):
        super().__init__(payload)
        self.url = url
        self.headers = {"Content-Length": str(len(payload) if expected is None else expected)}

    def geturl(self):
        return self.url


def accepted(task_id="task-1"):
    return {"code": 0, "data": {"task_id": task_id}}


def finished(status="success", cost=60, url="https://cdn.example/model.glb?auth_key=private"):
    data = {"status": status, "output": {"model_url": url} if status == "success" else {}}
    if cost is not None:
        data["credits_consumed"] = cost
    return {"code": 0, "data": data}


class TripoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.key = self.root / "external.key"
        self.key.write_text("tsk_test_not_a_real_credential", encoding="utf-8")
        self.client = TripoClient(self.root / "batch", self.key, 3000)
        self.api = Mock(return_value=accepted())
        self.client._api_json = self.api
        self.body = {"input": "concept-task", "model": "v3.1-20260211"}

    def submit(self, tag="house-low-model", estimate=60, body=None, **kwargs):
        return self.client.submit(tag, "generation/image-to-model", body or self.body, estimate, **kwargs)

    def download(self, response=None):
        opener = Mock()
        opener.open.return_value = response or Response()
        return patch("unattended.art_tripo.urllib.request.build_opener", return_value=opener)

    def test_json_key_file_sends_only_the_tripo_credential(self):
        settings = self.root / "secrets.local.json"
        settings.write_text(json.dumps({"TRIPO_API_KEY": "synthetic-tripo-secret",
                                        "CODEX_API_KEY": "synthetic-codex-secret"}), encoding="utf-8")
        client = TripoClient(self.root / "batch", settings)
        with self.download(Response(b'{"code":0}')) as factory:
            self.assertEqual(client._api_json("GET", "/task/example"), {"code": 0})
        request = factory.return_value.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-tripo-secret")

    def test_exact_cap_accounts_for_actual_reserved_and_unknown(self):
        self.submit("unknown", estimate=1000)
        self.api.side_effect = TimeoutError("signed url and key must not be logged")
        unknown = self.submit("lost", estimate=900)
        self.assertEqual(unknown["submission"], "unknown")
        self.api.side_effect = None
        self.api.return_value = finished(cost=800)
        with self.download():
            self.client.refresh("unknown")
        self.api.return_value = accepted("task-3")
        boundary = self.submit("last", estimate=1300)
        self.assertEqual(boundary["budget"], {"limit": 3000.0, "actual": 800.0, "reserved": 1300.0,
                                           "unknown": 900.0, "committed": 3000.0, "remaining": 0.0,
                                           "breached": False})
        calls = self.api.call_count
        with self.assertRaises(BudgetExceeded):
            self.submit("over", estimate=0.1)
        self.assertEqual(self.api.call_count, calls)

    def test_same_tag_is_idempotent_and_changed_body_is_rejected(self):
        first = self.submit()
        again = self.submit()
        self.assertEqual(first["task_id"], again["task_id"])
        self.api.assert_called_once()
        with self.assertRaises(RequestConflict):
            self.submit(body={"input": "different-concept"})
        with self.assertRaises(RequestConflict):
            self.submit(required_outputs=["model_url"])
        self.api.assert_called_once()

    def test_timeout_and_5xx_hold_budget_and_never_retry_post(self):
        for error in (TimeoutError("secret https://cdn/x?auth_key=private"),
                      urllib.error.HTTPError("https://api/x?secret=private", 503, "private", {}, None)):
            with self.subTest(error=type(error).__name__):
                client = TripoClient(self.root / type(error).__name__, self.key)
                api = Mock(side_effect=error)
                client._api_json = api
                first = client.run("asset", "generation/image-to-model", self.body, 60, poll_seconds=0)
                second = client.run("asset", "generation/image-to-model", self.body, 60, poll_seconds=0)
                client.refresh("asset")
                api.assert_called_once()
                self.assertEqual(first["submission"], "unknown")
                self.assertEqual(second["budget"]["unknown"], 60)
                self.assertNotIn("private", json.dumps(second))

    def test_process_dies_after_post_intent_and_restart_does_not_submit_again(self):
        script = """
import os, sys
from unattended.art_tripo import TripoClient
class Crash(TripoClient):
    def _api_json(self, method, path, body=None):
        os._exit(23)
Crash(sys.argv[1], sys.argv[2]).submit('house-low-model', 'generation/image-to-model',
    {'input':'concept-task','model':'v3.1-20260211'}, 60)
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.client.root), str(self.key)],
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 23)
        recovered = self.submit()
        self.assertEqual(recovered["submission"], "unknown")
        self.assertEqual(recovered["budget"]["unknown"], 60)
        self.api.assert_not_called()

    def test_real_cross_process_lock_prevents_double_submission_and_budget_race(self):
        marker, release, counter = (self.root / name for name in ("locked", "release", "counter"))
        script = """
import pathlib, sys, time
from unattended.art_tripo import TripoClient
class Hold(TripoClient):
    def _api_json(self, method, path, body=None):
        pathlib.Path(sys.argv[5]).write_text('one POST')
        pathlib.Path(sys.argv[3]).write_text('locked')
        while not pathlib.Path(sys.argv[4]).exists(): time.sleep(.01)
        return {'code':0,'data':{'task_id':'task-1'}}
Hold(sys.argv[1], sys.argv[2]).submit('house-low-model', 'generation/image-to-model',
    {'input':'concept-task','model':'v3.1-20260211'}, 60)
"""
        process = subprocess.Popen([sys.executable, "-c", script, str(self.client.root), str(self.key),
                                    str(marker), str(release), str(counter)], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, creationflags=0x08000000 if os.name == "nt" else 0)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(marker.exists(), "child did not acquire batch lock")
            with self.assertRaises(BatchBusy):
                self.submit()
            self.api.assert_not_called()
        finally:
            release.write_text("go")
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
        self.assertEqual(counter.read_text(), "one POST")
        self.assertEqual(self.submit()["budget"]["committed"], 60)
        self.api.assert_not_called()

    def test_failed_terminal_without_confirmed_cost_keeps_reservation(self):
        self.submit()
        self.api.return_value = finished("failed", cost=None)
        result = self.client.refresh("house-low-model")
        self.assertFalse(result["ready"])
        self.assertEqual(result["budget"]["unknown"], 60)
        self.api.return_value = finished("failed", cost=7)
        result = self.client.refresh("house-low-model")
        self.assertEqual(result["budget"]["actual"], 7)
        self.assertEqual(result["budget"]["committed"], 7)
        self.assertEqual(result["reserved_credits"], 0)

    def test_success_without_cost_or_download_is_not_ready(self):
        self.submit()
        self.api.return_value = finished(cost=None)
        with self.download():
            result = self.client.refresh("house-low-model")
        self.assertEqual(result["download_status"], "complete")
        self.assertFalse(result["ready"])
        self.assertEqual(result["budget"]["unknown"], 60)

    def test_partial_download_is_not_published_and_resume_refreshes_expired_url(self):
        self.submit()
        self.api.return_value = finished()
        with self.download(Response(b"short", expected=100)):
            broken = self.client.refresh("house-low-model")
        self.assertEqual(broken["api_status"], "success")
        self.assertEqual(broken["download_status"], "failed")
        self.assertFalse(broken["ready"])
        self.assertEqual(list((self.client.root / "artifacts").rglob("*.part")), [])
        self.assertEqual(list((self.client.root / "artifacts").rglob("*.glb")), [])
        refreshed = "https://cdn.example/model.glb?auth_key=refreshed-private"
        self.api.return_value = finished(url=refreshed)
        with self.download(Response(url=refreshed)) as opener:
            resumed = self.client.run("house-low-model", "generation/image-to-model", self.body, 60)
        self.assertTrue(resumed["ready"])
        self.assertEqual(sum(call.args[0] == "POST" for call in self.api.call_args_list), 1)
        self.assertEqual(self.api.call_args_list[-1].args[0], "GET")
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, refreshed)
        metadata = resumed["files"]["model_url"]
        self.assertEqual(metadata["sha256"], hashlib.sha256(b"model-content").hexdigest())
        self.assertEqual(metadata["size"], len(b"model-content"))
        self.assertNotIn("auth_key", json.dumps(resumed))

    def test_existing_valid_download_is_reused_but_corruption_is_repaired(self):
        self.submit()
        self.api.return_value = finished()
        with self.download():
            result = self.client.refresh("house-low-model")
        with patch.object(self.client, "_download_one", side_effect=AssertionError("must reuse")):
            self.assertTrue(self.client.refresh("house-low-model")["ready"])
        Path(result["files"]["model_url"]["path"]).write_bytes(b"corrupt")
        with self.download(Response(b"repaired")):
            result = self.client.refresh("house-low-model")
        self.assertEqual(result["files"]["model_url"]["sha256"], hashlib.sha256(b"repaired").hexdigest())
        self.assertTrue(result["ready"])

    def test_success_without_model_and_non_https_output_are_not_ready(self):
        self.submit(required_outputs=["model_url"])
        for output in ({}, {"model_url": "http://cdn.example/unsafe.glb"}):
            self.api.return_value = {"code": 0, "data": {"status": "success", "credits_consumed": 60, "output": output}}
            with patch.object(self.client, "_download_one") as download:
                result = self.client.refresh("house-low-model")
            self.assertFalse(result["ready"])
            self.assertEqual(result["download_status"], "failed")
            download.assert_not_called()

    def test_dependencies_resolve_task_ids_and_keep_signed_urls_out_of_reports(self):
        self.submit("concept", estimate=5)
        self.api.return_value = finished(cost=5)
        with self.download():
            self.client.refresh("concept")
        self.api.return_value = accepted("task-2")
        result = self.submit("model", body={"inputs": [{"task_id": "@concept"}], "image": "@concept:model_url"})
        submitted_body = self.api.call_args.args[2]
        self.assertEqual(submitted_body["inputs"], [{"task_id": "task-1"}])
        self.assertIn("auth_key=", submitted_body["image"])
        report = json.dumps(self.client.status())
        self.assertNotIn("auth_key", report)
        self.assertNotIn("tsk_test", report)
        self.assertNotIn("https://", report)
        self.assertFalse(result["ready"])

    def test_budget_configuration_cannot_be_reset_or_raised_on_existing_batch(self):
        self.submit()
        other = TripoClient(self.client.root, self.key, 2000)
        with self.assertRaises(TripoError):
            other.status()
        with self.assertRaises(ValueError):
            TripoClient(self.root / "too-large", self.key, 3001)
        self.client.ledger_path.write_text("corrupt", encoding="utf-8")
        with self.assertRaises(TripoError):
            self.submit("second")
        self.api.assert_called_once()

    def test_unexpected_cost_overrun_is_reported_and_freezes_new_work(self):
        self.submit(estimate=3000)
        self.api.return_value = finished(cost=3001)
        with self.download():
            result = self.client.refresh("house-low-model")
        self.assertTrue(result["budget"]["breached"])
        self.assertFalse(result["ready"])
        with self.assertRaises(BudgetExceeded):
            self.submit("second", estimate=1)

    def test_expired_cdn_url_is_refreshed_with_get_without_another_paid_request(self):
        self.submit()
        self.api.return_value = finished()
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError("https://cdn.example/model.glb?auth_key=private", 403, "expired", {}, None)
        with patch("unattended.art_tripo.urllib.request.build_opener", return_value=opener):
            result = self.client.refresh("house-low-model")
        self.assertEqual(result["error"], "artifact_download_failed_http_403")
        self.assertFalse(result["ready"])
        fresh = "https://cdn.example/model.glb?auth_key=new-private"
        self.api.return_value = finished(url=fresh)
        with self.download(Response(url=fresh)) as opener:
            result = self.client.run("house-low-model", "generation/image-to-model", self.body, 60)
        self.assertTrue(result["ready"])
        self.assertEqual(opener.return_value.open.call_args.args[0].full_url, fresh)
        self.assertEqual([call.args[0] for call in self.api.call_args_list], ["POST", "GET", "GET"])

    def test_later_status_failure_cannot_claim_fresh_verification_or_erase_actual_cost(self):
        self.submit()
        self.api.return_value = finished()
        with self.download():
            self.assertTrue(self.client.refresh("house-low-model")["ready"])
        self.api.side_effect = TimeoutError("secret")
        result = self.client.refresh("house-low-model")
        self.assertFalse(result["ready"])
        self.assertEqual(result["budget"]["actual"], 60)
        self.api.side_effect = None
        self.api.return_value = finished(cost=0)
        result = self.client.refresh("house-low-model")
        self.assertEqual(result["budget"]["actual"], 60)
        self.assertTrue(result["ready"])

    def test_invalid_reservations_never_reach_api(self):
        for cost in (True, -1, 0, float("nan"), float("inf")):
            with self.subTest(cost=cost), self.assertRaises(ValueError):
                self.submit(estimate=cost)
        self.api.assert_not_called()

    def test_generation_cannot_bypass_artifact_gate_with_empty_required_outputs(self):
        with self.assertRaises(ValueError):
            self.submit(required_outputs=[])
        self.api.assert_not_called()
        self.assertFalse(self.client.ledger_path.exists())
        self.client.submit("rig-check", "animations/rig-check", {"input": "model-task"}, 0,
                           required_outputs=[])
        self.api.return_value = {"code": 0, "data": {"status": "success", "credits_consumed": 0, "output": {}}}
        result = self.client.refresh("rig-check")
        self.assertTrue(result["ready"])
        self.assertTrue(result["local_artifacts_verified"])
        self.assertEqual(result["files"], {})

    def test_status_rehashes_missing_or_modified_files_without_network_or_ledger_writes(self):
        self.submit(required_outputs=["model_url"])
        self.api.return_value = finished()
        with self.download():
            completed = self.client.refresh("house-low-model")
        path = Path(completed["files"]["model_url"]["path"])
        original = path.read_bytes()
        ledger_before = self.client.ledger_path.read_bytes()
        calls_before = self.api.call_count
        self.assertTrue(self.client.status("house-low-model")["ready"])
        path.write_bytes(b"x" * len(original))  # Same size; only the digest catches this.
        modified = self.client.status("house-low-model")
        self.assertFalse(modified["ready"])
        self.assertFalse(modified["local_artifacts_verified"])
        self.assertEqual(modified["error"], "local_artifact_verification_failed")
        path.unlink()
        missing = self.client.status()["tasks"]["house-low-model"]
        self.assertFalse(missing["ready"])
        self.assertFalse(missing["local_artifacts_verified"])
        self.assertEqual(self.api.call_count, calls_before)
        self.assertEqual(self.client.ledger_path.read_bytes(), ledger_before)
        path.write_bytes(original)
        self.assertTrue(self.client.status("house-low-model")["ready"])
        self.assertEqual(self.api.call_count, calls_before)

    def test_key_is_loaded_only_for_actual_api_request_and_never_sent_to_download(self):
        client = TripoClient(self.root / "fresh", self.key)
        client.status()
        self.key.write_text("tsk_rotated_test_credential", encoding="utf-8")
        with self.download(Response(json.dumps(accepted()).encode())) as opener:
            result = client.submit("one", "generation/image-to-model", self.body, 60)
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer tsk_rotated_test_credential")
        self.assertNotIn("tsk_rotated", client.ledger_path.read_text(encoding="utf-8"))
        client._api_json = Mock(return_value=finished())
        with self.download() as opener:
            result = client.refresh("one")
        request = opener.return_value.open.call_args.args[0]
        self.assertIsNone(request.get_header("Authorization"))
        self.assertTrue(result["ready"])


if __name__ == "__main__":
    unittest.main()
