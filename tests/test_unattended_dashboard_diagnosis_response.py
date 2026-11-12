"""Explicit diagnosis replies remain separate from hash-bound asset acceptance."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import Mock, patch

from tests import test_unattended_dashboard_art as art_fixture
from unattended import dashboard, dashboard_art
from unattended.dashboard_state import _empty
from unattended.storage import write_json


class DiagnosisResponseHTTPTests(unittest.TestCase):
    def setUp(self):
        self.cache = Mock()
        self.cache.action_token = "diagnosis-instance-token"
        self.cache.respond_art_diagnosis.return_value = {"status": "response_queued"}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(self.cache, {}))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.payload = {"task_id": "ART-001", "receipt_sha256": "a" * 64, "feedback": " 保持原预算。\n请重新分析。 "}

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def post(self, payload=None, headers=None, path="/api/art/diagnosis/respond"):
        values = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.server.server_port}",
                  "X-Art-Action-Token": self.cache.action_token}
        values.update(headers or {})
        connection = HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            connection.request("POST", path, json.dumps(self.payload if payload is None else payload), values)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_explicit_reply_uses_its_own_route_with_exact_sha_and_unmodified_feedback(self):
        self.assertEqual(self.post()[0], 200)
        self.cache.respond_art_diagnosis.assert_called_once_with(self.payload)
        self.cache.resolve_art.assert_not_called()

    def test_local_origin_and_instance_token_are_required(self):
        for headers in ({"Origin": "https://foreign.example"}, {"Origin": ""}, {"X-Art-Action-Token": ""}):
            with self.subTest(headers=headers):
                self.assertEqual(self.post(headers=headers)[0], 403)
        self.cache.respond_art_diagnosis.assert_not_called()

    def test_malformed_or_ambiguous_reply_is_not_dispatched(self):
        for change in ({"feedback": " "}, {"feedback": "a" * 4001}, {"feedback": "a" * 17000}, {"receipt_sha256": "bad"},
                       {"task_id": "../private"}, {"decision": "accepted"}, {"feedback": None}):
            with self.subTest(change=change):
                self.assertEqual(self.post({**self.payload, **change})[0], 409)
        self.assertEqual(self.post(headers={"Content-Type": "text/plain"})[0], 409)
        self.assertEqual(self.post(path="/api/art/diagnosis/respond?approve=all")[0], 405)
        self.cache.respond_art_diagnosis.assert_not_called()
        self.cache.resolve_art.assert_not_called()

    def test_stale_receipt_returns_refresh_error_without_asset_acceptance(self):
        self.cache.respond_art_diagnosis.side_effect = ValueError("诊断回执已变化，请刷新后重新回复。")
        status, value = self.post()
        self.assertEqual(status, 409)
        self.assertIn("刷新", value["error"])
        self.cache.resolve_art.assert_not_called()


class DiagnosisResponseSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.f = art_fixture.ArtDashboardTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.state = self.f.save_active_task("BLOCKED")
        self.source = {"head_sha": "a" * 40, "editable_files": {"job": "f" * 64}}
        self.result = {"action": "needs_user", "diagnosis": "需要确认授权边界。", "plan": "用户回应后重新分析。",
                       "reason": "需要预算决定。", "required_approvals": ["budget"], "questions": ["是否调整预算？"],
                       "additional_tripo_credits": 100, "head_sha": "a" * 40}
        self.path = self.f.art_runtime / "tasks/ART-001/diagnosis-1.json"
        self.receipt = {"task_id": "ART-001", "head_sha": "a" * 40, "round": 1,
                        "source_fingerprint": self.source, "result": self.result, "execution_decision": "waiting",
                        "controller_reason": "Claude需要确认预算。", "private_native": "DO_NOT_PROJECT_NATIVE"}
        write_json(self.path, self.receipt)
        self.state.update(blocked_from="ART_DIAGNOSE", operation=None)
        self.state["task"].update(art_workspace_head="a" * 40, reworks=6,
            art_diagnosis_history=[deepcopy(self.receipt)],
            art_diagnosis={"status": "waiting_user", "round": 1, "fingerprint": self.source, "receipt_path": str(self.path)})
        self.save()

    def save(self):
        write_json(self.f.art_runtime / "state.json", self.state)

    def test_waiting_user_projection_binds_exact_receipt_bytes_and_never_constructs_worker(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.f.root.rglob("*") if p.is_file()}
        with patch("unattended.art_worker.ArtWorker.from_main_config", side_effect=AssertionError("GET must be read-only")):
            value = dashboard_art.collect_art_snapshot(self.f.main, self.f.now)
        public = value["diagnosis_response"]
        self.assertEqual(public["receipt_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(public["task_id"], "ART-001")
        self.assertTrue(public["can_respond"])
        self.assertEqual(public["questions"], self.result["questions"])
        self.assertNotIn("DO_NOT_PROJECT_NATIVE", json.dumps(public))
        self.assertNotIn(str(self.path), json.dumps(public))
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.f.root.rglob("*") if p.is_file()})

    def test_normal_production_never_exposes_a_decision_form(self):
        self.state["phase"] = "CODE"; self.save()
        self.assertIsNone(dashboard_art.collect_art_snapshot(self.f.main, self.f.now)["diagnosis_response"])

    def test_queued_response_is_visible_as_pending_without_exposing_queue_feedback_or_source(self):
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        write_json(self.f.art_runtime / "art-diagnosis-responses.json", {"version": 1, "responses": [
            {"id": "queued-reply", "task_id": "ART-001", "receipt_sha256": digest, "status": "queued",
             "source_fingerprint": {"private": "DO_NOT_PROJECT_SOURCE"}, "feedback": "DO_NOT_PROJECT_QUEUED_FEEDBACK"}]})
        public = dashboard_art.collect_art_snapshot(self.f.main, self.f.now)["diagnosis_response"]
        self.assertFalse(public["can_respond"])
        self.assertEqual(public["response_status"], "queued")
        self.assertNotIn("DO_NOT_PROJECT", json.dumps(public))

    def test_changed_task_head_source_or_fixed_receipt_path_is_not_replyable(self):
        baseline = deepcopy(self.state)
        for variant in ("head", "source", "path", "task", "history", "epoch"):
            with self.subTest(variant=variant):
                self.state = deepcopy(baseline)
                if variant == "head": self.state["task"]["art_workspace_head"] = "b" * 40
                elif variant == "source": self.state["task"]["art_diagnosis"]["fingerprint"] = {"head_sha": "b" * 40}
                elif variant == "path": self.state["task"]["art_diagnosis"]["receipt_path"] = str(self.f.root / "private.json")
                elif variant == "task": self.state["task"]["id"] = "ART-OTHER"
                elif variant == "history": self.state["task"]["art_diagnosis_history"] = []
                else: self.state["task"]["art_autonomy_epoch"] = 1
                self.save()
                value = dashboard_art.collect_art_snapshot(self.f.main, self.f.now)
                self.assertIsNone(value["diagnosis_response"])
                self.assertTrue(value["diagnosis_response_error"])

    def test_cache_dispatches_only_to_diagnosis_response_method(self):
        cache = dashboard.SnapshotCache(self.f.main_path)
        payload = {"task_id": "ART-001", "receipt_sha256": "f" * 64, "feedback": " 请重新分析。 "}
        with patch("unattended.art_worker.ArtWorker.from_main_config") as route:
            route.return_value.respond_art_diagnosis.return_value = {"status": "response_queued"}
            self.assertEqual(cache.respond_art_diagnosis(payload)["status"], "response_queued")
        route.return_value.respond_art_diagnosis.assert_called_once_with("ART-001", "f" * 64, " 请重新分析。 ")
        route.return_value.resolve_art.assert_not_called()


@unittest.skipUnless(os.environ.get("UNATTENDED_TEST_BROWSER") == "1", "opt-in local headless browser")
class DiagnosisResponseBrowserTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright
        self.value = _empty(datetime.now(timezone.utc))
        self.value["art"] = {"configured": True, "phase": "BLOCKED", "status": "blocked", "requests": [],
            "action_token": "reply-browser-token", "diagnosis_response": {"task_id": "ART-001", "receipt_sha256": "a" * 64,
                "status": "waiting_user", "can_respond": True, "summary": "需要你确认预算。", "plan": "等待回复后重新分析。",
                "reason": "现有授权边界需确认。", "questions": ["是否允许调整预算？<script>window.replyInjected=true</script>"]}}
        self.cache = Mock(); self.cache.action_token = "reply-browser-token"
        self.cache.get.side_effect = lambda: deepcopy(self.value)
        self.cache.flow_detail.return_value = {"lane": "art", "node": "review", "task_id": "ART-001", "rework_history": {"rounds": [], "user_feedback": [], "summary": {"text": "暂无审核历史"}}}
        self.cache.respond_art_diagnosis.return_value = {"status": "response_queued"}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(self.cache, {}))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.pw = sync_playwright().start(); self.browser = self.pw.chromium.launch(channel="chrome", headless=True)
        self.page = self.browser.new_page(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
        self.errors = []; self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.addCleanup(self.close)
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/#art")
        self.page.locator("#art-diagnosis-response").wait_for(state="visible")

    def close(self):
        self.browser.close(); self.pw.stop(); self.server.shutdown(); self.server.server_close(); self.thread.join(2)
        self.assertFalse(self.errors, self.errors); self.cache.resolve_art.assert_not_called()

    def refresh(self):
        with self.page.expect_response(lambda response: response.url.endswith("/api/status")):
            self.page.locator("#refresh").click()

    def test_only_explicit_reply_sends_exact_receipt_and_original_text_without_asset_approval(self):
        from playwright.sync_api import expect
        feedback = self.page.locator("#art-diagnosis-feedback")
        expect(feedback).to_have_value(""); self.cache.respond_art_diagnosis.assert_not_called()
        original = " 保持预算。\n请自行调整方案后重新分析。 "
        feedback.fill(original); self.refresh(); expect(feedback).to_have_value(original)
        self.page.locator("#art-diagnosis-send").click()
        expect(self.page.locator("#art-diagnosis-message")).to_contain_text("回复已提交")
        self.cache.respond_art_diagnosis.assert_called_once_with({"task_id": "ART-001", "receipt_sha256": "a" * 64, "feedback": original})
        expect(self.page.locator("#art-diagnosis-send")).to_be_disabled()
        self.assertFalse(self.page.evaluate("Boolean(window.replyInjected)"))
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 390)

    def test_stale_reply_keeps_draft_and_requests_refresh(self):
        from playwright.sync_api import expect
        self.cache.respond_art_diagnosis.side_effect = ValueError("诊断回执已变化，请刷新后重新回复。")
        self.page.locator("#art-diagnosis-feedback").fill("请先解释新增预算。")
        self.page.locator("#art-diagnosis-send").click()
        expect(self.page.locator("#art-diagnosis-message")).to_contain_text("刷新")
        expect(self.page.locator("#art-diagnosis-feedback")).to_have_value("请先解释新增预算。")

    def test_new_receipt_has_no_prefilled_approval_and_normal_code_hides_reply(self):
        from playwright.sync_api import expect
        self.page.locator("#art-diagnosis-feedback").fill("旧诊断草稿")
        self.value["art"]["diagnosis_response"]["receipt_sha256"] = "b" * 64
        self.refresh(); expect(self.page.locator("#art-diagnosis-feedback")).to_have_value("")
        self.value["art"]["phase"] = "CODE"
        self.refresh(); expect(self.page.locator("#art-diagnosis-response")).to_be_hidden()
        self.cache.respond_art_diagnosis.assert_not_called()

    def test_existing_queued_reply_disables_the_form_without_another_submission(self):
        from playwright.sync_api import expect
        self.value["art"]["diagnosis_response"]["can_respond"] = False
        self.value["art"]["diagnosis_response"]["response_status"] = "queued"
        self.refresh()
        expect(self.page.locator("#art-diagnosis-send")).to_be_disabled()
        expect(self.page.locator("#art-diagnosis-message")).to_contain_text("回复已排队")
        self.cache.respond_art_diagnosis.assert_not_called()


if __name__ == "__main__":
    unittest.main()
