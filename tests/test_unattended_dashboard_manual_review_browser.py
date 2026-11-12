"""Manual-review discovery is visible while all version and budget gates remain intact."""
import base64
from copy import deepcopy
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import Mock

from unattended.dashboard import make_handler
from unattended.dashboard_state import _empty


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lS8AAAAASUVORK5CYII=")


@unittest.skipUnless(os.environ.get("UNATTENDED_TEST_BROWSER") == "1", "opt-in isolated headless browser")
class ManualReviewBrowserTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright
        self.value = _empty(datetime.now(timezone.utc))
        self.item = {"id": "ART-001", "title": "阶段示意样板", "status": "awaiting_approval", "preview_kind": "review",
                     "preview_sha256": "a" * 64, "review_package_sha256": "a" * 64, "decision": None,
                     "can_resolve": True, "can_accept": True, "accept_block_reason": None,
                     "previews": [{"name": "当前样板", "url": "/api/art/preview?task=ART-001&review=" + "a" * 64 + "&index=0"}]}
        self.value["art"] = {"configured": True, "status": "awaiting_approval", "phase": "PLAN", "task_id": "",
                             "worker": {"label": "后台正常"}, "action_token": "isolated-manual-token", "requests": [self.item]}
        self.cache = Mock(); self.cache.action_token = "isolated-manual-token"
        self.cache.get.side_effect = lambda: deepcopy(self.value)
        self.cache.art_preview.return_value = PNG
        self.cache.flow_detail.return_value = {"lane": "art", "node": "review", "rework_history": {
            "rounds": [], "user_feedback": [], "summary": {"text": "暂无返工历史"}}}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.cache, {}))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(channel="chrome", headless=True)
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
        self.errors, self.writes = [], []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("**/*", self.read_only)
        self.addCleanup(self.close)
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/#art")
        self.page.locator('[data-art-manual-review="ART-001"]').wait_for(state="visible")

    def read_only(self, route):
        if route.request.method in {"GET", "HEAD"}:
            route.continue_()
        else:
            self.writes.append(route.request.method); route.abort()

    def close(self):
        self.browser.close(); self.pw.stop(); self.server.shutdown(); self.server.server_close(); self.thread.join(2)
        self.assertFalse(self.errors, self.errors); self.assertFalse(self.writes, self.writes)
        self.cache.resolve_art.assert_not_called()

    def refresh(self):
        with self.page.expect_response(lambda response: response.url.endswith("/api/status")):
            self.page.locator("#refresh").click()

    def form(self, identity="ART-001"):
        return self.page.locator(f'[data-art-manual-review="{identity}"]')

    def test_valid_pending_has_clear_manual_entry_empty_opinion_and_explicit_actions(self):
        from playwright.sync_api import expect
        expect(self.page.locator("#art-manual-entry-heading")).to_have_text("人工验收")
        expect(self.form().locator(".manual-review-heading")).to_contain_text("人工验收")
        expect(self.form().locator(".art-feedback")).to_have_value("")
        expect(self.form().get_by_role("button", name="接受本版", exact=True)).to_be_enabled()
        expect(self.form().get_by_role("button", name="退回修改", exact=True)).to_be_enabled()
        self.page.locator('[data-art-review-entry="ART-001"]').get_by_role("button").click()
        expect(self.form().locator(".manual-review-heading")).to_be_focused()
        self.refresh()
        expect(self.form().locator(".art-feedback")).to_have_value("")
        self.cache.resolve_art.assert_not_called()

    def test_gate_error_keeps_disabled_confirmation_and_reason_visible_and_draft_survives_recovery(self):
        from playwright.sync_api import expect
        self.item.update(preview_kind=None, preview_sha256=None, previews=[], can_resolve=False, can_accept=False,
                         preview_error="验收清单超过当前读取上限，暂时无法核对。", accept_block_reason="验收清单超过当前读取上限，暂时无法核对。")
        self.refresh()
        expect(self.page.locator("#art-manual-entry")).to_contain_text("暂不可确认")
        expect(self.form().locator(".art-manual-unavailable")).to_contain_text("读取上限")
        for name in ("接受本版", "退回修改"):
            button = self.form().get_by_role("button", name=name, exact=True)
            expect(button).to_be_visible(); expect(button).to_be_disabled()
            button.evaluate("element => element.click()")
        opinion = "本版仅作当前阶段示意，后续仍需调整。"
        self.form().locator(".art-feedback").fill(opinion)
        self.item.update(preview_kind="review", preview_sha256="a" * 64, can_resolve=True, can_accept=True,
                         preview_error=None, accept_block_reason=None)
        self.refresh()
        expect(self.form().locator(".art-feedback")).to_have_value(opinion)
        expect(self.form().get_by_role("button", name="接受本版", exact=True)).to_be_enabled()
        self.cache.resolve_art.assert_not_called()

    def test_budget_gate_only_disables_acceptance_and_does_not_disguise_rework_as_approval(self):
        from playwright.sync_api import expect
        self.item.update(can_accept=False, accept_block_reason="本批费用仍有未结算记录，请先核实。")
        self.refresh()
        expect(self.form().get_by_role("button", name="接受本版", exact=True)).to_be_disabled()
        expect(self.form().get_by_role("button", name="退回修改", exact=True)).to_be_enabled()
        expect(self.page.locator("#art-manual-entry")).to_contain_text("接受暂不可用")
        expect(self.form()).to_contain_text(self.item["accept_block_reason"])

    def test_multiple_pending_are_addressable_on_desktop_and_mobile_without_actions_for_technical_or_history(self):
        from playwright.sync_api import expect
        second = {**deepcopy(self.item), "id": "ART-002", "title": "另一待验收批次"}
        technical = {**deepcopy(self.item), "id": "ART-TECH", "title": "技术制作证据", "preview_kind": "technical"}
        historical = {**deepcopy(self.item), "id": "ART-OLD", "title": "已入库历史版本", "status": "accepted"}
        self.value["art"]["requests"] = [self.item, second, technical, historical]
        self.refresh()
        expect(self.page.locator("[data-art-review-entry]")).to_have_count(2)
        expect(self.page.locator("[data-art-manual-review]")).to_have_count(2)
        for identity in ("ART-TECH", "ART-OLD"):
            card = self.page.locator(".art-batch").filter(has_text=identity)
            self.assertEqual(card.locator(".art-actions button,.art-feedback").count(), 0)
        for width in (1440, 390):
            self.page.set_viewport_size({"width": width, "height": 1000 if width == 1440 else 844})
            self.page.locator('[data-art-review-entry="ART-002"]').get_by_role("button").click()
            expect(self.form("ART-002").locator(".manual-review-heading")).to_be_focused()
            expect(self.form("ART-002").get_by_role("button", name="接受本版", exact=True)).to_be_visible()
            self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), width)

    def test_asset_verification_can_exceed_fifteen_seconds_without_timeout_or_duplicate_submission(self):
        from playwright.sync_api import expect
        pending = []
        self.page.route("**/api/art/resolve", lambda route: pending.append(route))
        self.page.clock.install()
        opinion = "仅认可作为当前阶段示意，后续仍需调整。"
        self.form().locator(".art-feedback").fill(opinion)
        self.form().get_by_role("button", name="接受本版", exact=True).click()
        expect(self.form().locator("[data-art-message]")).to_contain_text("正在核验资产并提交")
        self.assertEqual(len(pending), 1)
        self.assertEqual(json.loads(pending[0].request.post_data), {"task_id": "ART-001", "decision": "accepted",
                         "feedback": opinion, "review_package_sha256": "a" * 64})
        self.assertEqual(pending[0].request.headers["x-art-action-token"], self.cache.action_token)
        self.page.clock.fast_forward(16000)
        expect(self.form().locator("[data-art-message]")).to_contain_text("正在核验资产并提交")
        expect(self.form().get_by_role("button", name="接受本版", exact=True)).to_be_disabled()
        expect(self.form().get_by_role("button", name="退回修改", exact=True)).to_be_disabled()
        self.form().get_by_role("button", name="接受本版", exact=True).evaluate("element => element.click()")
        self.assertEqual(len(pending), 1)
        pending[0].fulfill(status=200, content_type="application/json", body='{"status":"decision_queued"}')
        expect(self.page.locator("#toast")).to_contain_text("已提交")
        self.cache.resolve_art.assert_not_called()


if __name__ == "__main__":
    unittest.main()
