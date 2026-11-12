"""Opt-in isolated art rework history UI checks; never contact a real worker."""
import base64
from copy import deepcopy
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
import os
import threading
import unittest
from unittest.mock import Mock

from unattended.dashboard import make_handler
from unattended.dashboard_state import _empty


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lS8AAAAASUVORK5CYII=")
OLD_SHA = "5" * 64
CURRENT_SHA = "a" * 64


def history_fixture():
    complete_reason = "需修复：屋面交接存在局部扭曲。\n" + "完整原始审核说明。" * 420 + "\n审核全文末尾标记：未要求完全平屋顶。"
    delivery = "Claude声明：院墙已修复，屋顶局部仍需处理。\n<script>window.reworkInjected=true</script>\n交付全文末尾标记。"
    images = [{"id": f"r5-image-{i}", "title": f"第5轮视图{i + 1}", "kind": "png", "size_bytes": len(PNG),
               "url": f"/api/flow/evidence?lane=art&node=review&ref=r5-{OLD_SHA[:12]}-shot-{i}"} for i in range(8)]
    return {"rounds": [
        {"index": 0, "label": "初次技术审核（非返工）", "decision": "accepted", "summary": "初审完成，等待用户意见",
         "feedback": "初审记录", "delivery_text": "初次交付", "review_source": "Codex技术审核", "delivery_source": "Claude交付声明",
         "reviewed_at": "2026-11-06T09:00:00+00:00", "time_source": "review_file_mtime", "production_mode": "recipe_only",
         "checks": [], "checks_status": "unavailable", "sections": [], "evidence_images": [
             {"id": "r0-image-0", "title": "初审独立版本视图", "kind": "png", "size_bytes": len(PNG),
              "url": "/api/flow/evidence?lane=art&node=review&ref=r0-999999999999-shot-0"}], "logs": [
             {"id": "r0-log", "title": "初审执行日志", "kind": "text", "size_bytes": 40,
              "url": "/api/flow/evidence?lane=art&node=review&ref=r0-log"}], "artifacts": [],
         "evidence_status": "historical_capture_receipt", "evidence_note": "初审历史截图，不是当前验收版本。"},
        {"index": 3, "label": "第3次返工：旧权限阻塞", "decision": "blocked", "summary": "仅有配方权限，无法修改院落程序",
         "feedback": "旧权限阶段：只能编辑 recipe，不能修院落程序；未产生新模型。", "delivery_text": "本轮无新模型和新提交。",
         "review_source": "Codex技术审核", "delivery_source": "Claude交付声明", "reviewed_at": "2026-11-06T15:00:00+00:00",
         "time_source": "review_file_mtime", "production_mode": "recipe_only", "checks": [], "checks_status": "unavailable",
         "sections": [{"title": "阻塞及解除条件", "kind": "required", "text": "旧权限不足，需要开放受限 Blender 制作入口。"}],
         "evidence_images": [], "logs": [], "artifacts": [], "evidence_status": "unavailable", "evidence_note": "旧版本图片未归档，不能用当前图替代。"},
        {"index": 5, "label": "第5次返工技术审核", "decision": "rework", "summary": "院墙已修复；屋顶局部扭曲仍待处理",
         "feedback": complete_reason, "delivery_text": delivery, "review_source": "Codex技术审核", "delivery_source": "Claude交付声明",
         "reviewed_at": "2026-11-07T13:45:00+00:00", "time_source": "review_file_mtime", "production_mode": "claude_blender_job",
         "checks": [{"id": "art-pipeline-verify", "passed": True, "head_sha": "b" * 40, "exit_code": 0, "timed_out": False}],
         "checks_status": "matched_attempt", "sections": [
             {"title": "已完成部分", "kind": "completed", "text": "L1孤立半墙已删除，L4/L8院墙和院门已重建。"},
             {"title": "必须继续修复", "kind": "required", "text": "屋面交接存在局部折痕。"},
             {"title": "证据限制", "kind": "limitations", "text": "通行净空尚未做碰撞验证。"}],
         "evidence_images": images, "logs": [{"id": "r5-log", "title": "第5轮执行日志", "kind": "text", "size_bytes": 40,
                                               "url": "/api/flow/evidence?lane=art&node=review&ref=r5-log"}], "artifacts": [
             {"id": "review", "title": "本轮审核原文", "kind": "text", "size_bytes": 8000,
              "url": "/api/flow/evidence?lane=art&node=review&ref=r5-review"}],
         "evidence_status": "historical_capture_receipt", "evidence_note": "该轮引用截图回执，旧完整验收包未归档/无法核实；不是当前待接受版本。"},
        {"index": 6, "label": "第6次返工进行中", "decision": "in_progress", "summary": "Claude正在处理上轮反馈",
         "feedback": "", "delivery_text": "", "review_source": "Codex技术审核", "delivery_source": "Claude交付声明",
         "reviewed_at": None, "time_source": None, "production_mode": "claude_blender_job", "checks": [], "checks_status": "unavailable",
         "sections": [], "evidence_images": [], "logs": [], "artifacts": [], "evidence_status": "unavailable", "evidence_note": "本轮尚未完成。"}],
        "user_feedback": [{"source": "用户", "decision": "rework", "feedback": "用户原话：半墙是BUG，院落细节不足；没有要求完全平屋顶。",
                           "recorded_at": "2026-11-06T13:21:12+00:00", "review_package_sha256": "9" * 64}],
        "summary": {"reworks": 6, "current_index": 6, "initial_review_count": 1, "completed_job_rounds": 2,
                    "current_job_round": 6, "job_capability_at": "2026-11-07T12:03:15+00:00",
                    "text": "历史返工计数6，含旧权限阻塞；Claude可编辑Blender后已完成2轮，当前第6轮进行中。"}}


@unittest.skipUnless(os.environ.get("UNATTENDED_TEST_BROWSER") == "1", "opt-in local headless browser")
class ArtReworksBrowserTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright
        self.history = history_fixture()
        self.value = _empty(datetime.now(timezone.utc))
        self.value["art"] = {"configured": True, "status": "code", "phase": "CODE", "label": "Claude制作中", "task_id": "ART-001",
            "reworks": 6, "model": "claude-opus-5-5", "effort": "high", "worker": {"alive": True, "healthy": True, "label": "后台在线"},
            "action_token": "isolated-rework-token", "promotions": [], "backlog": [],
            "requests": [{"id": "ART-001", "title": "三级住宅返工", "status": "active", "batch_id": "HOUSE-001",
                "review_package_sha256": OLD_SHA, "can_resolve": False, "can_accept": False, "decision": None,
                "previews": [], "preview_kind": None, "preview_sha256": None, "preview_error": None}]}
        self.detail_error = False
        self.image_error = False
        self.cache = Mock()
        self.cache.action_token = self.value["art"]["action_token"]
        self.cache.get.side_effect = lambda: deepcopy(self.value)
        self.cache.flow_detail.side_effect = self.detail
        self.cache.flow_evidence.side_effect = self.evidence
        self.cache.art_preview.return_value = PNG
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.cache, {}))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(channel=os.environ.get("UNATTENDED_TEST_BROWSER_CHANNEL", "chrome"), headless=True)
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
        self.errors, self.writes = [], []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("**/*", self.read_only)
        self.addCleanup(self.close)
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/#art")
        self.page.locator('#art-rework-rounds details[data-rework-round="5"]').wait_for(state="attached")

    def read_only(self, route):
        if route.request.method in {"GET", "HEAD"}:
            route.continue_()
        else:
            self.writes.append(route.request.method)
            route.abort()

    def detail(self, lane, node):
        if self.detail_error:
            raise ValueError("返工记录版本已变化，请刷新；旧图不能作为当前验收证据。")
        return {"lane": lane, "node": node, "title": "Codex技术审核", "task_id": "ART-001",
                "scope_label": "当前任务 ART-001", "summary": "第6次返工进行中", "logs": [], "artifacts": [],
                "rework_history": deepcopy(self.history)}

    def evidence(self, lane, node, ref):
        if self.image_error:
            raise ValueError("该轮截图校验失败，历史证据不可用。")
        if ref.startswith(("r0-", "r5-")) and "-shot-" in ref:
            return PNG, "image/png", None
        if ref in {"r0-log", "r5-log"}:
            return ("初审日志正文" if ref == "r0-log" else "第5轮日志正文").encode("utf-8"), "text/plain; charset=utf-8", None
        return b"isolated historical review text", "text/plain; charset=utf-8", None

    def close(self):
        self.browser.close()
        self.pw.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.assertFalse(self.errors, self.errors)
        self.assertFalse(self.writes, self.writes)
        self.cache.resolve_art.assert_not_called()

    def round(self, index):
        return self.page.locator(f'#art-rework-rounds details[data-rework-round="{index}"]')

    def open_history(self):
        from playwright.sync_api import expect
        details = self.page.locator("#art-rework-details")
        if details.get_attribute("open") is None:
            details.locator(":scope > summary").click()
        expect(details).to_have_attribute("open", "")

    def open_round(self, index):
        self.open_history()
        if self.round(index).get_attribute("open") is None:
            self.round(index).locator(":scope > summary").click()

    def refresh(self):
        from playwright.sync_api import expect
        # A new task checkpoint also triggers history refresh without waiting for its poll interval.
        self.value["art"]["reworks"] += 1
        with self.page.expect_response(lambda response: "/api/flow/detail?" in response.url and "node=review" in response.url):
            self.page.locator("#refresh").evaluate("element => element.click()")
        expect(self.page.locator("#refresh")).to_be_enabled()

    def test_default_is_short_reason_two_current_round_images_and_collapsed_history(self):
        from playwright.sync_api import expect
        details = self.page.locator("#art-rework-details")
        expect(details).not_to_have_attribute("open", "")
        expect(self.page.locator("#art-rework-count-detail")).to_be_hidden()
        expect(self.page.locator("#art-rework-user-feedback")).to_be_hidden()
        expect(self.round(5).locator("[data-delivery-text]")).to_be_hidden()
        reason = self.page.locator("#art-rework-latest-summary")
        expect(reason).to_contain_text("屋顶局部扭曲仍待处理")
        self.assertLessEqual(len(reason.inner_text()), 160)
        self.assertNotIn("屋面交接存在局部折痕", reason.inner_text())  # Do not append full required sections.
        self.assertNotIn(self.history["summary"]["text"], self.page.locator("#art-rework-summary").inner_text())
        gallery = self.page.locator("#art-rework-latest-images")
        expect(gallery).to_be_visible()
        self.assertEqual(gallery.locator("button[data-rework-image]").count(), 2)
        allowed = {row["url"] for row in self.history["rounds"][2]["evidence_images"]}
        for image in gallery.locator("img").all():
            self.assertIn(image.get_attribute("src"), allowed)
            expect(image).to_have_js_property("naturalWidth", 1)
        selected_url = gallery.locator("img").first.get_attribute("src")
        gallery.locator("button[data-rework-image]").first.click()
        expect(self.page.locator("#art-rework-image-dialog")).to_be_visible()
        expect(self.page.locator("#art-rework-image")).to_have_attribute("src", selected_url)
        self.page.locator("#art-rework-image-close").click()
        self.history["rounds"][2]["summary"] = "本轮仍需修复屋面接缝。" * 35
        self.refresh()
        expect(reason).to_contain_text("本轮仍需修复屋面接缝")
        self.assertLessEqual(len(reason.inner_text()), 160)
        expect(details).not_to_have_attribute("open", "")

    def test_no_images_hides_featured_gallery_instead_of_borrowing_another_round(self):
        from playwright.sync_api import expect
        self.history["rounds"][2]["evidence_images"] = []
        self.refresh()
        gallery = self.page.locator("#art-rework-latest-images")
        expect(gallery).to_be_hidden()
        self.assertEqual(gallery.locator("img, button[data-rework-image]").count(), 0)
        expect(self.page.locator("#art-rework-latest-summary")).to_contain_text("屋顶局部扭曲仍待处理")
        self.open_round(0)
        self.assertEqual(self.round(0).locator("button[data-rework-image]").count(), 1)
        self.assertEqual(self.page.get_by_role("button", name="接受本版", exact=True).count(), 0)

    def test_code_rework_keeps_last_completed_full_reason_fixed_items_and_claude_statement(self):
        from playwright.sync_api import expect
        self.open_round(5)
        expect(self.round(5)).to_have_attribute("open", "")
        expect(self.round(6)).not_to_have_attribute("open", "")
        expect(self.page.locator("#art-rework-latest-summary")).to_contain_text("屋顶局部扭曲仍待处理")
        self.round(5).get_by_text("原始审核全文与原因", exact=True).click()
        expect(self.round(5)).to_contain_text("审核全文末尾标记：未要求完全平屋顶。")
        expect(self.round(5).get_by_text("审核全文末尾标记：未要求完全平屋顶。", exact=False)).to_be_visible()
        expect(self.round(5).locator('[data-rework-section="completed"]')).to_contain_text("L1孤立半墙已删除")
        expect(self.round(5).locator("[data-delivery-text]")).to_contain_text("交付全文末尾标记。")
        expect(self.round(5).locator("[data-review-source]")).to_contain_text("Codex")
        expect(self.round(5).locator("[data-delivery-source]")).to_contain_text("Claude")
        self.assertEqual(self.page.get_by_role("button", name="接受本版", exact=True).count(), 0)
        self.assertEqual(self.page.get_by_role("button", name="退回修改", exact=True).count(), 0)

    def test_user_feedback_and_old_permission_history_do_not_become_new_model_iterations(self):
        from playwright.sync_api import expect
        self.open_history()
        expect(self.page.locator("#art-rework-user-feedback")).to_contain_text("用户原话：半墙是BUG")
        expect(self.page.locator("#art-rework-count-detail")).to_contain_text(self.history["summary"]["text"])
        self.assertEqual(self.page.locator("#art-rework-rounds > details[data-rework-round]").count(), 4)
        self.assertEqual(self.round(4).count(), 0)  # Missing records are not invented from the counter.
        expect(self.round(0).locator("summary")).to_contain_text("非返工")
        expect(self.round(0).locator("summary")).to_contain_text("技术审核通过")
        self.assertNotIn("用户接受", self.round(0).locator("summary").inner_text())
        self.assertNotIn("已接受", self.round(0).locator("summary").inner_text())
        self.round(3).locator("summary").click()
        expect(self.round(3)).to_contain_text("旧权限")
        expect(self.round(3)).to_contain_text("未产生新模型")
        expect(self.round(3).locator("[data-evidence-note]")).to_contain_text("不能用当前图替代")
        self.assertEqual(self.round(3).locator("button[data-rework-image]").count(), 0)
        self.assertNotIn("已完成6轮模型制作", self.page.locator("#art-rework-panel").inner_text())

    def test_historical_eight_images_zoom_only_the_bound_version_and_never_enable_accept(self):
        from playwright.sync_api import expect
        self.open_round(5)
        buttons = self.round(5).locator("button[data-rework-image]")
        self.assertEqual(buttons.count(), 8)
        buttons.nth(6).click()
        dialog = self.page.locator("#art-rework-image-dialog")
        expect(dialog).to_be_visible()
        image = self.page.locator("#art-rework-image")
        expect(image).to_have_attribute("src", self.history["rounds"][2]["evidence_images"][6]["url"])
        expect(image).to_have_js_property("naturalWidth", 1)
        self.cache.flow_evidence.assert_any_call("art", "review", f"r5-{OLD_SHA[:12]}-shot-6")
        expect(self.round(5).locator("[data-evidence-note]")).to_contain_text("旧完整验收包未归档")
        self.assertEqual(dialog.get_by_role("button", name="接受本版", exact=True).count(), 0)
        self.page.locator("#art-rework-image-close").click()
        expect(dialog).not_to_be_visible()
        self.round(0).locator("summary").click()
        self.round(0).locator("button[data-rework-image]").click()
        expect(dialog).to_be_visible()
        expect(image).to_have_attribute("src", self.history["rounds"][0]["evidence_images"][0]["url"])
        expect(self.page.locator("#art-rework-image-title")).to_contain_text("初审")
        expect(image).to_have_js_property("naturalWidth", 1)
        self.cache.flow_evidence.assert_any_call("art", "review", "r0-999999999999-shot-0")
        self.assertNotIn(OLD_SHA[:12], image.get_attribute("src"))
        self.page.locator("#art-rework-image-close").click()
        for index, title, expected_text, unexpected_text in (
                (5, "第5轮执行日志", "第5轮日志正文", "初审日志正文"),
                (0, "初审执行日志", "初审日志正文", "第5轮日志正文")):
            with self.page.expect_popup() as opened:
                self.round(index).get_by_role("link", name=title, exact=False).click()
            popup = opened.value
            expect(popup.locator("body")).to_contain_text(expected_text)
            self.assertNotIn(unexpected_text, popup.locator("body").inner_text())
            popup.close()

    def test_untrusted_history_text_is_literal_and_cannot_execute_html(self):
        from playwright.sync_api import expect
        self.open_round(5)
        expect(self.round(5).locator("[data-delivery-text]")).to_contain_text("<script>window.reworkInjected=true</script>")
        self.assertFalse(self.page.evaluate("Boolean(window.reworkInjected)"))
        self.assertEqual(self.page.locator("#art-rework-panel script").count(), 0)
        self.assertGreater(self.round(5).locator("a[data-rework-artifact]").count(), 0)
        for href in self.round(5).locator("a[data-rework-artifact]").evaluate_all("nodes => nodes.map(node => node.getAttribute('href'))"):
            self.assertTrue(href.startswith("/api/flow/evidence?"), href)

    def test_refresh_preserves_history_expansion_and_current_other_version_feedback_draft(self):
        from playwright.sync_api import expect
        self.open_history()
        self.value["art"]["requests"].append({"id": "ART-002", "title": "另一个当前待验收批次", "status": "awaiting_approval",
            "review_package_sha256": CURRENT_SHA, "preview_kind": "review", "preview_sha256": CURRENT_SHA,
            "can_resolve": True, "can_accept": True, "decision": None,
            "previews": [{"name": "当前批次展示", "url": f"/api/art/preview?task=ART-002&review={CURRENT_SHA}&index=0"}]})
        self.refresh()
        feedback = self.page.locator(".art-batch").filter(has_text="ART-002").locator(".art-feedback")
        feedback.fill("这是对当前版本的意见，查看旧轮次时不要丢失。")
        if self.round(5).get_attribute("open") is not None:
            self.round(5).locator("summary").click()
        self.round(3).locator("summary").click()
        self.history["rounds"][2]["feedback"] += "\n新增记录仍不应改变展开状态。"
        self.refresh()
        expect(self.page.locator("#art-rework-details")).to_have_attribute("open", "")
        expect(self.round(3)).to_have_attribute("open", "")
        expect(self.round(5)).not_to_have_attribute("open", "")
        expect(feedback).to_have_value("这是对当前版本的意见，查看旧轮次时不要丢失。")
        self.assertEqual(self.page.locator("#art-rework-panel .art-feedback").count(), 0)
        self.assertEqual(self.page.locator("#art-rework-panel").get_by_role("button", name="接受本版", exact=True).count(), 0)

    def test_history_refresh_failure_is_visible_and_never_enables_old_version_actions(self):
        from playwright.sync_api import expect
        self.open_history()
        self.detail_error = True
        self.refresh()
        expect(self.page.locator("#art-rework-status")).to_contain_text("版本已变化")
        self.assertEqual(self.page.get_by_role("button", name="接受本版", exact=True).count(), 0)
        self.detail_error = False
        self.refresh()
        expect(self.round(5)).to_contain_text("审核全文末尾标记")

    def test_390px_history_long_text_and_image_dialog_do_not_overflow(self):
        from playwright.sync_api import expect
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 390)
        expect(self.page.locator("#art-rework-latest-images")).to_be_visible()
        self.open_round(5)
        self.history["rounds"][2]["feedback"] += "\n" + "unbroken-evidence-id-" * 100
        self.refresh()
        expect(self.round(5)).to_have_attribute("open", "")
        self.round(5).get_by_text("原始审核全文与原因", exact=True).click()
        expect(self.round(5).locator("[data-rework-raw-text]")).to_be_visible()
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 390)
        self.round(5).locator("button[data-rework-image]").first.click()
        expect(self.page.locator("#art-rework-image-dialog")).to_be_visible()
        self.assertLessEqual(self.page.locator("#art-rework-image-dialog").bounding_box()["width"], 390)
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 390)


if __name__ == "__main__":
    unittest.main()
