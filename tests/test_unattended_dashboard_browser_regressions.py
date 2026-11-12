"""Opt-in UI interaction checks on an ephemeral server, never on a live worker."""
import base64
from copy import deepcopy
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import re
import threading
import unittest
from unittest.mock import Mock

from unattended.dashboard import make_handler
from unattended.dashboard_state import _empty


@unittest.skipUnless(os.environ.get("UNATTENDED_TEST_BROWSER") == "1", "opt-in local headless browser")
class DashboardBrowserRegressionTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright
        self.cache = Mock()
        self.cache.action_token = "isolated-browser-token"
        self.value = _empty(datetime.now(timezone.utc))
        self.value["status"].update(code="code", phase="CODE", label="主流程正在开发", actor="Claude", summary="功能开发与美术验收独立。")
        self.value["art"] = {"configured": True, "status": "awaiting_approval", "label": "等待视觉验收",
            "worker": {"label": "后台正常"}, "model": "claude-opus-5-5", "action_token": self.cache.action_token,
            "promotions": [], "backlog": [{"id": "MARKET", "title": "市场与仓储", "priority": 2,
                "status": "waiting_budget", "definition_ids": ["building.market"], "levels": []}],
            "requests": [self.request_item("ART-001", "a" * 64)]}
        self.cache.get.side_effect = lambda: deepcopy(self.value)
        self.cache.art_preview.return_value = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lS8AAAAASUVORK5CYII=")
        self.cache.resolve_art.side_effect = self.complete_decision
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.cache, {}))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(channel=os.environ.get("UNATTENDED_TEST_BROWSER_CHANNEL", "chrome"), headless=True)
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.addCleanup(self.close)
        self.url = f"http://127.0.0.1:{self.server.server_port}/"
        self.page.goto(self.url + "#art")
        self.page.get_by_role("button", name="接受本版", exact=True).wait_for()

    def close(self):
        self.browser.close()
        self.pw.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.assertFalse(self.errors, self.errors)

    @staticmethod
    def request_item(task, sha):
        return {"id": task, "title": "普通住宅三级样板", "status": "awaiting_approval", "batch_id": "HOUSE-001",
                "approved_levels": ["L1", "L4", "L8"], "review_package_sha256": sha,
                "can_resolve": True, "can_accept": True, "decision": None,
                "previews": [{"name": f"{i+1:02d}_院落视图", "url": f"/api/art/preview?task={task}&review={sha}&index={i}"} for i in range(8)]}

    def complete_decision(self, payload):
        item = next(item for item in self.value["art"]["requests"] if item["id"] == payload["task_id"])
        if payload["review_package_sha256"] != item["review_package_sha256"]:
            raise ValueError("验收包已变化，请刷新当前版本")
        item["decision"] = {"decision": payload["decision"], "feedback": payload["feedback"]}
        item["can_resolve"] = False
        return {"status": "decision_queued"}

    def card(self, task="ART-001"):
        return self.page.locator(".art-batch").filter(has_text=task)

    def refresh(self):
        # Programmatic activation models polling without moving keyboard focus.
        with self.page.expect_response(lambda response: response.url.endswith("/api/status")):
            self.page.locator("#refresh").evaluate("element => element.click()")
        self.page.wait_for_function("!document.getElementById('refresh').disabled")

    def open_view(self, view):
        from playwright.sync_api import expect
        self.page.locator(f'[data-open-view="{view}"]').first.click()
        expect(self.page.locator("#view-" + view)).to_be_visible()
        expect(self.page).to_have_url(re.compile(r"/#" + re.escape(view) + r"$"))

    def test_default_overview_six_entries_hashes_and_refresh_keep_selected_view(self):
        from playwright.sync_api import expect
        self.page.goto(self.url)
        expect(self.page.locator("#view-overview")).to_be_visible()
        for hidden in ("development", "art", "flows", "history", "settings"):
            expect(self.page.locator("#view-" + hidden)).to_be_hidden()
        for view in ("development", "art", "flows", "history", "settings", "overview"):
            with self.subTest(view=view):
                self.open_view(view)
                for other in {"overview", "development", "art", "flows", "history", "settings"} - {view}:
                    expect(self.page.locator("#view-" + other)).to_be_hidden()
                self.refresh()
                expect(self.page.locator("#view-" + view)).to_be_visible()
                expect(self.page).to_have_url(re.compile(r"/#" + view + r"$"))
        for fragment, view in (("art", "art"), ("development", "development"), ("flows", "flows"),
                               ("art-panel", "art"), ("workflow-panel", "development")):
            self.page.goto(self.url + "#" + fragment)
            expect(self.page.locator("#view-" + view)).to_be_visible()
        self.cache.resolve_art.assert_not_called()

    def test_same_version_draft_focus_and_selection_survive_other_batch_refresh(self):
        from playwright.sync_api import expect
        feedback = self.card().locator(".art-feedback")
        feedback.fill("请让茅屋材料更朴素，保留生活细节。")
        feedback.evaluate("element => { element.focus(); element.setSelectionRange(3, 7); }")
        self.value["art"]["requests"].append({"id": "ART-002", "title": "另一批资产", "status": "active", "can_resolve": False})
        self.value["art"]["requests"][0]["title"] = "普通住宅三级样板 · 当前版本"
        self.refresh()
        expect(self.card().locator(".art-feedback")).to_have_value("请让茅屋材料更朴素，保留生活细节。")
        expect(self.card().locator(".art-feedback")).to_be_focused()
        self.assertEqual(self.card().locator(".art-feedback").evaluate("element => [element.selectionStart, element.selectionEnd]"), [3, 7])
        self.cache.resolve_art.assert_not_called()

    def test_new_version_requires_fresh_feedback_and_submits_new_sha_and_token(self):
        from playwright.sync_api import expect
        self.card().locator(".art-feedback").fill("这是对旧版本的意见")
        self.value["art"]["requests"][0] = self.request_item("ART-001", "b" * 64)
        self.cache.action_token = self.value["art"]["action_token"] = "rotated-instance-token"
        self.refresh()
        expect(self.card().locator(".art-feedback")).to_have_value("")
        self.card().get_by_role("button", name="接受本版", exact=True).click()
        self.cache.resolve_art.assert_not_called()
        self.card().locator(".art-feedback").fill("确认本次新版本")
        self.card().get_by_role("button", name="接受本版", exact=True).click()
        expect(self.page.locator("#toast")).to_contain_text("已提交")
        self.cache.resolve_art.assert_called_once_with({"task_id": "ART-001", "decision": "accepted",
            "feedback": "确认本次新版本", "review_package_sha256": "b" * 64})

    def test_pending_action_stays_disabled_during_polling_and_cannot_double_submit(self):
        from playwright.sync_api import expect
        started, finish = threading.Event(), threading.Event()
        def delayed(payload):
            started.set()
            if not finish.wait(10):
                raise ValueError("offline test timed out")
            return self.complete_decision(payload)
        self.cache.resolve_art.side_effect = delayed
        self.card().locator(".art-feedback").fill("调整院门")
        try:
            self.card().get_by_role("button", name="退回修改", exact=True).click()
            self.assertTrue(started.wait(3))
            self.value["art"]["requests"][0]["title"] = "普通住宅三级样板 · 后台状态更新"
            self.refresh()
            button = self.card().get_by_role("button", name="退回修改", exact=True)
            expect(button).to_be_disabled()
            expect(self.card().get_by_role("button", name="接受本版", exact=True)).to_be_disabled()
            expect(self.card().locator(".art-feedback")).to_have_value("调整院门")
            button.evaluate("element => element.click()")
            self.assertEqual(self.cache.resolve_art.call_count, 1)
        finally:
            finish.set()
        expect(self.page.locator("#toast")).to_contain_text("已提交")

    def test_disconnected_snapshot_cannot_approve_and_recovers_without_losing_draft(self):
        from playwright.sync_api import expect
        self.card().locator(".art-feedback").fill("等待连接恢复后提交")
        self.page.route("**/api/status", lambda route: route.fulfill(status=503, body="offline test"))
        self.refresh()
        expect(self.page.locator("#connection-label")).to_contain_text("连接中断")
        button = self.card().get_by_role("button", name="接受本版", exact=True)
        expect(button).to_be_disabled()
        button.evaluate("element => element.click()")
        self.cache.resolve_art.assert_not_called()
        self.page.unroute("**/api/status")
        self.refresh()
        expect(self.page.locator("#connection-label")).to_contain_text("实时连接")
        expect(self.card().get_by_role("button", name="接受本版", exact=True)).to_be_enabled()
        expect(self.card().locator(".art-feedback")).to_have_value("等待连接恢复后提交")

    def test_thumbnail_keyboard_selection_budget_holds_and_technical_review_stay_separate(self):
        from playwright.sync_api import expect
        item = self.value["art"]["requests"][0]
        item["budget"] = {"actual": 260, "reserved": 0, "unknown": 0,
                          "committed": 260, "remaining": 2740, "limit": 3000}
        item["technical_review"] = {"decision": "accepted", "summary": "当前证据技术检查通过",
                                     "feedback": "", "review_package_sha256": "a" * 64}
        self.refresh()
        thumbnails = self.card().locator(".art-previews button")
        self.assertEqual(thumbnails.count(), 8)
        thumbnails.nth(3).focus()
        thumbnails.nth(3).press("Enter")
        expect(thumbnails.nth(3)).to_have_attribute("aria-pressed", "true")
        expect(self.card().locator(".art-gallery-main")).to_have_attribute("href", re.compile(r"index=3$"))
        expect(self.card().locator(".art-budget")).to_contain_text("260")
        expect(self.card().locator(".art-budget")).to_contain_text("2,740")
        expect(self.card().locator(".budget-meter")).to_have_attribute("value", "260")
        self.card().locator(".technical-review summary").click()
        expect(self.card().locator(".technical-review")).to_contain_text("技术检查不代替你的视觉验收")
        expect(self.card().get_by_role("button", name="接受本版", exact=True)).to_be_enabled()
        item["title"] += " · 状态更新"
        self.refresh()
        expect(self.card().locator(".art-previews button").nth(3)).to_have_attribute("aria-pressed", "true")
        expect(self.card().locator(".technical-review")).to_have_attribute("open", "")
        item.update(budget={"actual": 260, "reserved": 60, "unknown": 5, "committed": 325, "remaining": 2675, "limit": 3000},
                    can_accept=False, accept_block_reason="积分存在未结算占用，暂不能接受。")
        self.refresh()
        expect(self.card().locator(".art-budget")).to_contain_text("2,675")
        expect(self.card().locator(".art-budget")).to_contain_text("65")
        expect(self.card().locator(".budget-meter")).to_have_attribute("value", "325")
        expect(self.card().get_by_role("button", name="接受本版", exact=True)).to_be_disabled()
        item["budget"] = None
        item["budget_error"] = "预算待核实：当前版本证据已过期"
        self.refresh()
        expect(self.card().locator(".art-budget")).to_contain_text(item["budget_error"])
        self.assertEqual(self.card().locator(".budget-meter").count(), 0)
        self.cache.resolve_art.assert_not_called()

    def test_same_sha_rework_can_reenter_approval_only_with_fresh_feedback(self):
        from playwright.sync_api import expect
        self.card().locator(".art-feedback").fill("本轮需要调整院门")
        self.card().get_by_role("button", name="退回修改", exact=True).click()
        expect(self.card()).to_contain_text("已提交返工")
        self.assertEqual(self.cache.resolve_art.call_count, 1)
        item = self.value["art"]["requests"][0]
        item.update(status="awaiting_approval", decision=None, can_resolve=True)
        self.refresh()
        expect(self.card().locator(".art-feedback")).to_have_value("")
        expect(self.card().get_by_role("button", name="接受本版", exact=True)).to_be_enabled()
        expect(self.card().get_by_role("button", name="退回修改", exact=True)).to_be_enabled()
        self.card().get_by_role("button", name="接受本版", exact=True).click()
        self.assertEqual(self.cache.resolve_art.call_count, 1)
        self.card().locator(".art-feedback").fill("返工后重新核对，本次认可")
        with self.page.expect_response(lambda response: response.url.endswith("/api/art/resolve")):
            self.card().get_by_role("button", name="接受本版", exact=True).click()
        self.assertEqual(self.cache.resolve_art.call_count, 2)
        self.cache.resolve_art.assert_called_with({"task_id": "ART-001", "decision": "accepted",
            "feedback": "返工后重新核对，本次认可", "review_package_sha256": "a" * 64})

    def test_acceptance_block_disables_only_accept_and_keeps_rework_available(self):
        from playwright.sync_api import expect
        item = self.value["art"]["requests"][0]
        item.update(can_accept=False, accept_block_reason="当前验收包预算仍有未结算占用，请先核实。")
        self.refresh()
        accept = self.card().get_by_role("button", name="接受本版", exact=True)
        expect(accept).to_be_disabled()
        expect(self.card().get_by_role("button", name="退回修改", exact=True)).to_be_enabled()
        expect(self.card()).to_contain_text(item["accept_block_reason"])
        self.card().locator(".art-feedback").fill("退回核实本版预算与证据")
        accept.evaluate("element => element.click()")
        self.cache.resolve_art.assert_not_called()
        self.card().get_by_role("button", name="退回修改", exact=True).click()
        expect(self.page.locator("#toast")).to_contain_text("已提交")
        self.cache.resolve_art.assert_called_once_with({"task_id": "ART-001", "decision": "rework",
            "feedback": "退回核实本版预算与证据", "review_package_sha256": "a" * 64})

    def test_blocked_technical_preview_replaces_old_sha_and_preserves_full_defect_evidence(self):
        from playwright.sync_api import expect
        self.card().locator(".art-feedback").fill("旧验收版本的未提交草稿，不能移到新模型")
        summary = "技术审核未通过：门洞检查失败，近景记录与当前制作版本不一致。"
        detail = ("审核记录：必须检查当前模型的门洞与院落尺度。\n" * 100 +
                  "解除条件：修复全部明确缺陷，并重新生成当前版本的独立技术检查回执。")
        technical = self.request_item("ART-001", "b" * 64)
        technical.update(status="active", preview_kind="technical", preview_sha256="b" * 64,
                         review_package_sha256="a" * 64, can_resolve=False, can_accept=False,
                         preview_error=None, technical_review=None)
        self.value["art"].update(status="blocked", label="技术审核阻塞", phase="BLOCKED", blocked_from="ART_REVIEW",
                                 blocking_summary=summary, blocking_detail=detail, requests=[technical])
        self.refresh()
        expect(self.page.locator("#art-blocked")).to_be_visible()
        expect(self.page.locator("#art-blocking-summary")).to_have_text(summary)
        self.page.locator("#art-blocked summary").click()
        expect(self.page.locator("#art-blocking-detail")).to_be_visible()
        expect(self.page.locator("#art-blocking-detail")).to_have_text(detail)
        expect(self.card().locator(".art-technical-note")).to_contain_text("技术审核尚未通过")
        expect(self.card()).to_contain_text("b" * 10)
        self.assertNotIn("a" * 10, self.card().inner_text())
        self.assertEqual(self.card().locator(".art-previews img").count(), 8)
        for image in self.card().locator(".art-previews img").all():
            expect(image).to_have_attribute("src", re.compile("review=" + "b" * 64))
        self.assertEqual(self.card().locator(".art-feedback").count(), 0)
        self.assertEqual(self.card().get_by_role("button", name="接受本版", exact=True).count(), 0)
        self.assertEqual(self.card().get_by_role("button", name="退回修改", exact=True).count(), 0)
        self.refresh()
        self.cache.resolve_art.assert_not_called()
        # Technical evidence can later become a genuine review package; old
        # feedback never migrates across version/kind boundaries.
        current = self.request_item("ART-001", "b" * 64)
        current.update(preview_kind="review", preview_sha256="b" * 64)
        self.value["art"].update(status="awaiting_approval", label="等待视觉验收", phase="PLAN", blocked_from=None,
                                 blocking_summary=None, blocking_detail=None, requests=[current])
        self.refresh()
        expect(self.page.locator("#art-blocked")).to_be_hidden()
        expect(self.card().locator(".art-feedback")).to_have_value("")
        self.card().get_by_role("button", name="接受本版", exact=True).click()
        self.cache.resolve_art.assert_not_called()
        self.card().locator(".art-feedback").fill("重新查看当前版本后认可")
        self.card().get_by_role("button", name="接受本版", exact=True).click()
        expect(self.page.locator("#toast")).to_contain_text("已提交")
        self.cache.resolve_art.assert_called_once_with({"task_id": "ART-001", "decision": "accepted",
            "feedback": "重新查看当前版本后认可", "review_package_sha256": "b" * 64})

    def test_technical_kind_never_offers_approval_even_if_old_capabilities_arrive_with_it(self):
        from playwright.sync_api import expect
        technical = self.request_item("ART-001", "c" * 64)
        technical.update(preview_kind="technical", preview_sha256="c" * 64, review_package_sha256="a" * 64)
        # Deliberately retain the old awaiting/can_resolve/can_accept values to
        # check that technical-kind semantics independently forbid approval.
        self.value["art"].update(status="blocked", label="技术审核阻塞", requests=[technical],
                                 blocking_summary="技术证据尚未通过审核。", blocking_detail="当前模型必须修复后重新检查。")
        self.refresh()
        expect(self.card().locator(".art-technical-note")).to_be_visible()
        self.assertEqual(self.card().locator(".art-previews img").count(), 8)
        self.assertEqual(self.card().locator(".art-feedback,.art-actions button").count(), 0)
        expect(self.page.locator("#nav-art-count")).to_be_hidden()
        self.open_view("overview")
        expect(self.page.locator("#spotlight-description")).to_contain_text("技术证据尚未通过审核")
        self.assertNotIn("等待你验收", self.page.locator("#spotlight-state").inner_text())
        self.cache.resolve_art.assert_not_called()

    def test_long_planning_reason_does_not_overlap_rework_caption(self):
        self.value["workflow"] = {"current": "critical_plan", "route": "critical", "nodes": [], "edges": [],
            "route_reason": "跨系统规划必须核对住宅等级、人口、存档契约与后续升级条件。" * 20,
            "rework": {"active": True, "count": 2}}
        self.refresh()
        self.open_view("development")
        for width, height in ((1440, 1000), (390, 844)):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": height})
                caption = self.page.locator("#flow-branch-caption").bounding_box()
                rework = self.page.locator("#flow-rework").bounding_box()
                self.assertIsNotNone(caption)
                self.assertIsNotNone(rework)
                horizontal = max(0, min(caption["x"] + caption["width"], rework["x"] + rework["width"]) - max(caption["x"], rework["x"]))
                vertical = max(0, min(caption["y"] + caption["height"], rework["y"] + rework["height"]) - max(caption["y"], rework["y"]))
                self.assertEqual(horizontal * vertical, 0, "Long planning reason covers the rework caption")

    def test_workflow_keyboard_state_and_mobile_geometry_are_preserved(self):
        from playwright.sync_api import expect
        self.value["workflow"] = {"current": "code", "route": "routine", "nodes": [
            {"id": "code", "title": "开发与返工", "state": "ready", "current": True, "configured_model": "claude-sonnet-5-5"},
            {"id": "critical_plan", "title": "关键系统定稿", "state": "not_required", "current": False}], "edges": []}
        self.value["modules"] = [{"id": "developer", "title": "代码开发", "state": "ready", "current": True,
                                  "configured_model": "claude-sonnet-5-5", "responsibility": "实现功能", "summary": "等待开始", "activity": []}]
        self.refresh()
        self.open_view("development")
        flow = self.page.locator('[data-node="code"]')
        expect(flow).to_have_attribute("data-state", "ready")
        expect(flow).to_have_attribute("aria-current", "step")
        expect(self.page.locator('[data-node="critical_plan"]')).to_have_attribute("data-state", "not_required")
        flow.focus()
        flow.press("Enter")
        module = self.page.locator("#module-developer")
        expect(module).to_be_focused()
        expect(module.locator("details")).to_have_attribute("open", "")
        expect(module).to_contain_text("执行模型未记录")
        self.open_view("art")
        expect(self.page.locator("#art-status")).to_contain_text("等待视觉验收")
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 391)
        self.assertLessEqual(self.page.locator("#art-panel").bounding_box()["width"], 390)
        screenshots = os.environ.get("UNATTENDED_TEST_SCREENSHOT_DIR")
        if screenshots:
            directory = Path(screenshots)
            directory.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(directory / "dashboard-mobile.png"), full_page=True)


if __name__ == "__main__":
    unittest.main()
