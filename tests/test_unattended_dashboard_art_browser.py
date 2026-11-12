"""Opt-in isolated headless browser check; never accesses the running dashboard."""
import base64
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock

from unattended.dashboard import make_handler
from unattended.dashboard_state import _empty


@unittest.skipUnless(os.environ.get("UNATTENDED_TEST_BROWSER") == "1", "opt-in local headless browser")
class ArtDashboardBrowserTests(unittest.TestCase):
    def test_preview_draft_survives_polling_and_explicit_click_binds_sha(self):
        from playwright.sync_api import expect, sync_playwright
        cache = Mock()
        cache.action_token = "test-browser-token"
        value = _empty(datetime.now(timezone.utc))
        value["art"] = {"configured": True, "status": "awaiting_approval", "label": "等待视觉验收",
                        "worker": {"label": "后台正常"}, "model": "claude-opus-5-5", "action_token": cache.action_token,
                        "promotions": [{"id": "previous", "batch_id": "上一批", "status": "integrated"}],
                        "backlog": [{"id": "MARKET", "title": "市场与仓储", "priority": 2, "status": "waiting_budget", "definition_ids": ["building.market"], "levels": []}],
                        "requests": [{"id": "ART-001", "title": "普通住宅三级样板", "status": "awaiting_approval",
                                      "batch_id": "HOUSE-001", "approved_levels": ["L1", "L4", "L8"],
                                      "review_package_sha256": "a" * 64, "can_resolve": True, "can_accept": True, "decision": None,
                                      "previews": [{"name": f"{i+1:02d}_院落视图", "url": f"/api/art/preview?task=ART-001&review={'a'*64}&index={i}"} for i in range(8)]}]}
        cache.get.return_value = value
        image = os.environ.get("UNATTENDED_TEST_ART_SHOT")
        cache.art_preview.return_value = Path(image).read_bytes() if image else base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lS8AAAAASUVORK5CYII=")
        cache.resolve_art.return_value = {"status": "decision_queued"}
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cache, {}))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel=os.environ.get("UNATTENDED_TEST_BROWSER_CHANNEL", "chrome"), headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1100})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/#art")
                page.get_by_role("button", name="接受本版").wait_for()
                self.assertEqual(page.locator(".art-previews img").count(), 8)
                plan = page.locator("#art-backlog details")
                self.assertIsNone(plan.get_attribute("open"))
                expect(plan.locator("summary")).to_contain_text("待预算确认")
                plan.locator("summary").click()
                expect(plan.locator(".document")).to_be_visible()
                feedback = page.locator(".art-feedback")
                feedback.fill("请调整院门位置")
                page.get_by_role("button", name="刷新状态").click() if page.get_by_role("button", name="刷新状态").count() else page.locator("#refresh").click()
                page.wait_for_timeout(150)
                self.assertEqual(feedback.input_value(), "请调整院门位置")
                screenshot = os.environ.get("UNATTENDED_TEST_SCREENSHOT")
                if screenshot:
                    page.locator("#art-panel").screenshot(path=screenshot)
                page.get_by_role("button", name="退回修改").click()
                expect(page.locator("#toast")).to_contain_text("已提交")
                cache.resolve_art.assert_called_once_with({"task_id": "ART-001", "decision": "rework", "feedback": "请调整院门位置", "review_package_sha256": "a" * 64})
                page.set_viewport_size({"width": 390, "height": 844})
                self.assertLessEqual(page.locator("#art-panel").bounding_box()["width"], 390)
                self.assertFalse(errors, errors)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
