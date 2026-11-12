"""Opt-in, isolated browser checks for live workflow nodes and evidence views."""
from copy import deepcopy
from http.server import ThreadingHTTPServer
import os
import threading
import unittest
from unittest.mock import Mock

from tests.test_unattended_dashboard_development_browser import development_snapshot
from unattended.dashboard import make_handler


def flow_node(lane, identity, title, *, state="idle", current=False, model=None, source=None, reason=None):
    labels = {"running": "正在执行", "idle": "待命", "waiting": "等待恢复", "blocked": "需要处理"}
    return {"id": identity, "title": title, "state": state, "state_label": labels[state],
            "current": current, "actor": "Blender" if identity == "blender" else "Claude",
            "model": model, "effort": "high" if model else None, "model_source": source,
            "summary": title + "的本轮状态", "blocked_reason": reason,
            "detail_url": f"/api/flow/detail?lane={lane}&node={identity}"}


def flow_snapshot():
    value = development_snapshot()
    value["flows"] = {
        "development": {"id": "development", "title": "功能开发", "current": "code", "summary": "正在开发",
            "nodes": [flow_node("development", "code", "开发实现", state="running", current=True,
                                model="claude-sonnet-5-5", source="recorded"),
                      flow_node("development", "review", "任务审核", model="gpt-6-astra", source="configured")],
            "edges": [{"from": "code", "to": "review", "label": "检查后审核"}]},
        "art": {"id": "art", "title": "美术制作", "current": "blender", "summary": "本地处理",
            "nodes": [flow_node("art", "blender", "Blender处理", state="running", current=True),
                      flow_node("art", "approval", "用户验收")],
            "edges": [{"from": "blender", "to": "approval", "label": "展示与审核后"}]},
        "operations": {"id": "operations", "title": "后台服务", "current": None, "summary": "后台在线",
            "nodes": [flow_node("operations", "notifications", "通知服务")], "edges": []}}
    return value


@unittest.skipUnless(os.environ.get("UNATTENDED_TEST_BROWSER") == "1", "opt-in local headless browser")
class WorkflowBrowserTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright
        self.value = flow_snapshot()
        self.event_text = '<script>window.flowInjected=true</script>\n本轮真实日志'
        self.cache = Mock()
        self.cache.get.side_effect = lambda: deepcopy(self.value)
        self.cache.flow_detail.side_effect = self.detail
        self.cache.flow_evidence.side_effect = self.evidence
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
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/#flows")
        self.page.locator('#view-flows [data-flow-node="code"]').wait_for(state="visible")

    def read_only(self, route):
        if route.request.method in {"GET", "HEAD"}:
            route.continue_()
        else:
            self.writes.append(route.request.method)
            route.abort()

    def close(self):
        self.browser.close()
        self.pw.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.assertFalse(self.errors, self.errors)
        self.assertFalse(self.writes, self.writes)
        self.cache.resolve_art.assert_not_called()

    def detail(self, lane, identity):
        item = next(n for n in self.value["flows"][lane]["nodes"] if n["id"] == identity)
        return {"lane": lane, "node": identity, "title": item["title"], "task_id": "TASK-FLOW-009",
                "scope_label": "本轮任务 TASK-FLOW-009", "summary": item["summary"],
                "model": item["model"], "effort": item["effort"], "model_source": item["model_source"],
                "blocked_reason": item["blocked_reason"],
                "logs": [{"id": "current-log", "title": "本轮执行日志", "kind": "text", "size_bytes": 180,
                          "url": f"/api/flow/evidence?lane={lane}&node={identity}&ref=log"}],
                "artifacts": [{"id": "manifest", "title": "处理清单", "kind": "text", "size_bytes": 60,
                               "url": f"/api/flow/evidence?lane={lane}&node={identity}&ref=manifest"}]}

    def evidence(self, lane, identity, reference):
        value = self.event_text if reference == "log" else '{"asset":"fixture","version":3}'
        return value.encode("utf-8"), "text/plain; charset=utf-8", None

    def test_node_opens_scoped_logs_and_artifacts_as_safe_text(self):
        from playwright.sync_api import expect
        button = self.page.locator('[data-flow-lane="development"][data-flow-node="code"]').first
        expect(button).to_have_attribute("data-state", "running")
        button.click()
        dialog = self.page.locator("#flow-detail-dialog")
        expect(dialog).to_be_visible()
        expect(self.page.locator("#flow-detail-title")).to_contain_text("开发实现")
        expect(dialog).to_contain_text("TASK-FLOW-009")
        dialog.get_by_role("button", name="本轮执行日志", exact=False).click()
        expect(self.page.locator("#flow-evidence-text")).to_contain_text("本轮真实日志")
        self.assertFalse(self.page.evaluate("Boolean(window.flowInjected)"))
        dialog.get_by_role("button", name="处理清单", exact=False).click()
        expect(self.page.locator("#flow-evidence-text")).to_contain_text('"version":3')
        self.cache.flow_detail.assert_any_call("development", "code")
        self.cache.flow_evidence.assert_any_call("development", "code", "manifest")
        self.page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        expect(button).to_be_focused()

    def test_waiting_state_and_blocking_reason_refresh_without_claiming_execution(self):
        from playwright.sync_api import expect
        item = self.value["flows"]["development"]["nodes"][0]
        item.update(state="waiting", state_label="等待恢复", blocked_reason="订阅额度等待恢复，21:30 自动重试")
        with self.page.expect_response(lambda r: r.url.endswith("/api/status")):
            self.page.locator("#refresh").click()
        button = self.page.locator('[data-flow-lane="development"][data-flow-node="code"]').first
        expect(button).to_have_attribute("data-state", "waiting")
        expect(button).to_contain_text("等待恢复")
        button.click()
        expect(self.page.locator("#flow-detail-dialog")).to_contain_text("21:30 自动重试")

    def test_mobile_flow_nodes_and_evidence_fit_the_screen(self):
        from playwright.sync_api import expect
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 390)
        self.page.locator('[data-flow-lane="art"][data-flow-node="blender"]').first.click()
        expect(self.page.locator("#flow-detail-dialog")).to_be_visible()
        self.assertLessEqual(self.page.locator("#flow-detail-dialog").bounding_box()["width"], 390)
        self.page.locator("#flow-detail-dialog").get_by_role("button", name="本轮执行日志", exact=False).click()
        expect(self.page.locator("#flow-evidence-text")).to_contain_text("本轮真实日志")
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 390)

    def test_selected_log_updates_while_detail_stays_open(self):
        from playwright.sync_api import expect
        self.page.locator('[data-flow-lane="development"][data-flow-node="code"]').first.click()
        dialog = self.page.locator("#flow-detail-dialog")
        dialog.get_by_role("button", name="本轮执行日志", exact=False).click()
        expect(self.page.locator("#flow-evidence-text")).to_contain_text("本轮真实日志")
        self.event_text += "\n持续追加的新执行记录"
        expect(self.page.locator("#flow-evidence-text")).to_contain_text("持续追加的新执行记录", timeout=10000)
        expect(dialog).to_be_visible()


if __name__ == "__main__":
    unittest.main()
