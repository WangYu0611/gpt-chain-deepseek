"""Development UI evidence/state regressions on an isolated, read-only fixture."""
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


STAGES = (("routine_plan", "routine_planner", "常规规划"),
          ("critical_plan", "critical_planner", "关键规划"),
          ("code", "developer", "开发实现"), ("check", "checks", "独立检查"),
          ("review", "reviewer", "任务审核"), ("publish", "publisher", "任务登记"))


def development_snapshot():
    now = datetime.now(timezone.utc)
    value = _empty(now)
    value.update(coding_provider="claude", planning_provider="claude")
    value["project"].update(name="开发页面测试", milestone="M3", branch="unattended/browser-fixture")
    value["worker"].update(alive=True, healthy=True, label="后台在线", heartbeat_age_seconds=1)
    value["status"].update(code="check", phase="CHECK", label="独立检查", actor="UE/CHECK", summary="正在核对固定提交。",
                           started_at=now.isoformat(), elapsed_seconds=1)
    value["task"] = {"id": "TASK-FOCUS-009", "title": "住宅升级条件与真实检查证据", "body": "只展示已记录证据，不推测整体完成百分比。",
                     "reworks": 2, "base_sha": "a" * 40, "head_sha": "b" * 40,
                     "acceptance_criteria": ["独立检查完成", "审核固定提交"],
                     "planning": {"level": "routine", "model": "claude-sonnet-5-5", "effort": "high", "reason": "明确的本地功能范围。"}}
    value["check_progress"] = {"total": 5, "current_id": "ue-current", "evidence_scope": "current_attempt"}
    value["checks"] = [{"id": "ue-build", "passed": True, "exit_code": 0, "status": None},
                       {"id": "ue-layout", "passed": True, "exit_code": 0, "status": None}]
    value["publication"].update(mode="milestone", milestone_id="M3", pending_tasks=13, summary="阶段验收后统一发布")
    value["accepted"] = [{"id": f"TASK-HISTORY-{i}", "title": f"真实验收记录 {i}", "sha": str(i) * 40,
                          "accepted_at": f"2026-11-06T10:{i:02d}:00+00:00", "reworks": i % 2} for i in range(1, 9)]
    value["activity"] = [{"at": "2026-11-06T10:00:00+00:00", "actor": "CHECK", "text": "较早的活动"},
                         {"at": "2026-11-06T10:05:00+00:00", "actor": "CHECK", "text": "最新活动：正在核对本轮固定提交"}]
    states = {"routine_plan": "completed", "critical_plan": "not_required", "code": "completed", "check": "running",
              "review": "idle", "publish": "idle"}
    labels = {"completed": "已完成", "not_required": "本轮无需", "running": "正在执行", "idle": "待命"}
    nodes, modules = [], []
    for identity, module, title in STAGES:
        state = states[identity]
        current = identity == "check"
        configured = None if module in {"checks", "publisher"} else "claude-opus-5-5" if module == "critical_planner" else "gpt-6-astra" if module == "reviewer" else "claude-sonnet-5-5"
        common = {"state": state, "state_label": labels[state], "current": current, "model": None, "effort": "high",
                  "model_source": "unknown", "configured_model": configured}
        nodes.append({**common, "id": identity, "title": title, "module_id": module, "enabled": True,
                      "optional": identity == "critical_plan", "summary": title + "的真实记录"})
        modules.append({**common, "id": module, "title": title, "responsibility": title + "职责",
                        "summary": title + "的真实记录", "activity": value["activity"] if current else [],
                        "task_id": value["task"]["id"], "task_title": value["task"]["title"]})
    value["workflow"] = {"current": "check", "route": "routine", "nodes": nodes, "edges": [],
                         "route_reason": "明确的本地功能范围。", "summary": "当前独立检查", "rework": {"active": False, "count": 2, "reason": ""}}
    value["modules"] = modules
    return value


@unittest.skipUnless(os.environ.get("UNATTENDED_TEST_BROWSER") == "1", "opt-in local headless browser")
class DevelopmentDashboardBrowserTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright
        self.value = development_snapshot()
        self.cache = Mock()
        self.cache.get.side_effect = lambda: deepcopy(self.value)
        self.cache.resolve_art.side_effect = AssertionError("Development page must never submit any action")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.cache, {}))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(channel=os.environ.get("UNATTENDED_TEST_BROWSER_CHANNEL", "chrome"), headless=True)
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
        self.errors, self.writes = [], []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("**/*", self.read_only)
        self.addCleanup(self.close)
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/#development")
        self.page.locator("#dev-task-title").wait_for(state="visible")
        self.page.wait_for_function("document.getElementById('connection-label').textContent === '实时连接'")

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

    def refresh(self):
        with self.page.expect_response(lambda response: response.url.endswith("/api/status")):
            self.page.locator("#refresh").evaluate("element => element.click()")
        self.page.wait_for_function("!document.getElementById('refresh').disabled")

    def set_current(self, node_id, state, *, status_code=None):
        labels = {"ready": "等待执行", "running": "正在执行", "waiting": "等待恢复", "paused": "已暂停",
                  "stale": "状态待核实", "blocked": "需要处理"}
        current_module = dict((identity, module) for identity, module, _ in STAGES)[node_id]
        self.value["workflow"]["current"] = node_id
        for entry in self.value["workflow"]["nodes"]:
            if entry["current"]:
                entry.update(current=False, state="idle", state_label="待命")
            if entry["id"] == node_id:
                entry.update(current=True, state=state, state_label=labels[state])
        for entry in self.value["modules"]:
            if entry["current"]:
                entry.update(current=False, state="idle", state_label="待命")
            if entry["id"] == current_module:
                entry.update(current=True, state=state, state_label=labels[state])
        phase = {"check": "CHECK", "code": "CODE", "review": "REVIEW", "routine_plan": "PLAN", "critical_plan": "PLAN", "publish": "PUBLISH"}[node_id]
        self.value["status"].update(phase=phase, code=status_code or phase.lower(), label=labels[state], paused=state == "paused")

    def test_focus_stage_and_counts_are_grounded_in_snapshot_without_task_percentage(self):
        from playwright.sync_api import expect
        expect(self.page.locator("#dev-task-id")).to_have_text("TASK-FOCUS-009")
        expect(self.page.locator("#dev-task-title")).to_have_text("住宅升级条件与真实检查证据")
        expect(self.page.locator("#dev-state")).to_have_attribute("data-state", "running")
        expect(self.page.locator("#dev-stage-name")).to_contain_text("独立检查")
        self.assertEqual(self.page.locator(".dev-flow button.flow-node[data-node]").count(), 6)
        current = self.page.locator('.dev-flow [aria-current="step"]')
        self.assertEqual(current.count(), 1)
        expect(current).to_have_attribute("data-node", "check")
        expect(self.page.locator('.dev-flow [data-node="critical_plan"]')).to_have_attribute("data-state", "not_required")
        self.assertNotIn("%", self.page.locator("#dev-task-title").inner_text() + self.page.locator("#dev-state").inner_text())
        expect(self.page.locator("#dev-reworks")).to_contain_text("2")
        expect(self.page.locator("#dev-pending-publish")).to_contain_text("13")
        expect(self.page.locator("#dev-live-event")).to_contain_text("最新活动：正在核对本轮固定提交")

    def test_accepted_repair_is_shown_above_its_historical_failed_test(self):
        from playwright.sync_api import expect
        archived = self.value["accepted"][0]
        archived["review_summary"] = "已通过独立验收。"
        self.value["full_flow_test"] = {
            "id": "old-run", "task_id": archived["id"], "head_sha": "a" * 40,
            "historical": True, "summary": "首轮未通过。", "status_label": "本次测试已结束",
            "accepted_repair": {"task_id": archived["id"], "head_sha": archived["sha"],
                                "accepted_at": archived["accepted_at"], "model": "claude-opus-5-5",
                                "effort": "high", "passed_checks": 6, "total_checks": 6},
        }
        self.refresh()
        expect(self.page.locator("#user-repair-status")).to_have_text("验收通过")
        expect(self.page.locator("#user-repair-model")).to_have_text("Opus 5.5 · high")
        expect(self.page.locator("#user-repair-summary")).to_contain_text("6 / 6")
        expect(self.page.locator("#full-flow-title")).to_have_text("修复前的全流程测试")
        expect(self.page.locator("#full-flow-summary")).to_have_text("首轮未通过。")
        self.assertLess(self.page.locator("#user-repair-panel").bounding_box()["y"],
                        self.page.locator("#full-flow-test").bounding_box()["y"])
        self.page.locator("#user-repair-detail").click()
        expect(self.page.locator("#view-history")).to_be_visible()
        review = self.page.locator(f'#accepted details[data-task-id="{archived["id"]}"]')
        expect(review.locator("p")).to_be_visible()
        expect(review.locator("summary")).to_be_focused()

    def test_keyboard_selection_survives_polling_and_follow_current_restores_inspector(self):
        from playwright.sync_api import expect
        node = self.page.locator('.dev-flow [data-node="review"]')
        node.focus()
        node.press("Enter")
        selected = self.page.locator("#module-reviewer")
        expect(selected).to_be_visible()
        expect(selected).to_be_focused()
        expect(selected.locator("details")).to_have_attribute("open", "")
        self.assertEqual(self.page.locator("#modules-grid .role-card:visible").count(), 1)
        self.set_current("code", "ready")
        self.value["task"]["title"] += " · 已更新"
        self.refresh()
        expect(selected).to_be_visible()
        expect(selected.locator("details")).to_have_attribute("open", "")
        expect(self.page.locator("#module-developer")).to_be_hidden()
        self.page.locator("#dev-follow-current").click()
        expect(self.page.locator("#module-developer")).to_be_visible()
        expect(selected).to_be_hidden()
        self.set_current("check", "ready")
        self.refresh()
        expect(self.page.locator("#module-checks")).to_be_visible()

    def test_wait_pause_ready_stale_and_disconnection_do_not_claim_execution(self):
        from playwright.sync_api import expect
        for state, status in (("ready", "check"), ("waiting", "waiting_provider"), ("paused", "paused"), ("stale", "heartbeat_stale"), ("blocked", "blocked")):
            with self.subTest(state=state):
                self.set_current("check", state, status_code=status)
                self.refresh()
                expect(self.page.locator("#dev-state")).to_have_attribute("data-state", state)
                self.assertNotIn("正在执行", self.page.locator("#dev-state").inner_text())
                expect(self.page.locator('.dev-flow [data-node="check"]')).to_have_attribute("data-state", state)
        self.set_current("check", "running")
        self.refresh()
        expect(self.page.locator("#dev-state")).to_have_attribute("data-state", "running")
        self.page.route("**/api/status", lambda route: route.fulfill(status=503, body="isolated offline fixture"))
        self.refresh()
        expect(self.page.locator("#dev-state")).to_have_attribute("data-state", "stale")
        expect(self.page.locator("#flow-current")).to_contain_text("过期")
        expect(self.page.locator("#modules-grid")).to_contain_text("过期")
        self.assertNotIn("正在执行", self.page.locator("#dev-state").inner_text())
        self.page.unroute("**/api/status")
        self.refresh()
        expect(self.page.locator("#dev-state")).to_have_attribute("data-state", "running")

    def test_http_connection_does_not_misrepresent_stopped_worker_as_online(self):
        from playwright.sync_api import expect
        health = self.page.locator("#dev-worker-health")
        expect(health).to_have_text("后台在线")
        self.value["worker"].update(alive=False, healthy=False, label="后台已停止")
        self.set_current("check", "blocked", status_code="blocked")
        self.value["status"].update(attempt_finished=True, elapsed_seconds=50)
        self.refresh()
        expect(self.page.locator("#connection-label")).to_have_text("实时连接")
        expect(health).to_have_text("后台已停止")
        expect(health).to_have_class(re.compile(r"\bworker-offline\b"))
        expect(self.page.locator("#dev-state")).to_have_attribute("data-state", "blocked")
        self.assertNotIn("正在执行", self.page.locator("#dev-state").inner_text())
        self.page.route("**/api/status", lambda route: route.fulfill(status=503, body="isolated offline fixture"))
        self.refresh()
        expect(health).to_have_text("后台状态待同步")
        expect(health).not_to_have_class(re.compile(r"\bworker-offline\b"))
        self.page.unroute("**/api/status")
        self.value["worker"].update(alive=True, healthy=True, label="后台在线")
        self.set_current("check", "ready")
        self.refresh()
        expect(self.page.locator("#connection-label")).to_have_text("实时连接")
        expect(health).to_have_text("后台在线")
        expect(health).not_to_have_class(re.compile(r"\bworker-offline\b"))

    def test_check_matrix_counts_recorded_results_and_unrecorded_items_without_guessing(self):
        from playwright.sync_api import expect
        expect(self.page.locator("#checks")).to_contain_text("ue-build")
        expect(self.page.locator("#checks")).to_contain_text("ue-layout")
        expect(self.page.locator("#dev-check-summary")).to_contain_text("2")
        expect(self.page.locator("#dev-check-summary")).to_contain_text("5")
        expect(self.page.locator("#dev-check-progress")).to_have_attribute("aria-label", re.compile(r"2.*5|5.*2"))
        expect(self.page.locator("#dev-check-pending")).to_contain_text("3")
        self.assertNotIn("%", self.page.locator("#dev-check-summary").inner_text())
        self.set_current("code", "running")
        self.value["check_progress"].update(current_id=None, evidence_scope="previous_attempt")
        self.value["workflow"]["rework"].update(active=True, reason="修复上一轮问题")
        self.refresh()
        expect(self.page.locator("#dev-check-scope")).to_contain_text("上一轮")
        expect(self.page.locator("#dev-check-summary")).to_contain_text("0")
        expect(self.page.locator("#dev-check-pending")).to_contain_text("5")
        expect(self.page.locator("#checks")).to_contain_text("ue-build")
        expect(self.page.locator('.dev-flow [data-node="check"]')).to_have_attribute("data-state", "idle")

    def test_recent_six_records_are_real_and_do_not_complete_current_publication(self):
        from playwright.sync_api import expect
        recent = self.page.locator("#dev-recent-accepted")
        for index in range(3, 9):
            expect(recent).to_contain_text(f"真实验收记录 {index}")
        self.assertNotIn("真实验收记录 1", recent.inner_text())
        self.assertNotIn("真实验收记录 2", recent.inner_text())
        expect(self.page.locator('.dev-flow [data-node="publish"]')).to_have_attribute("data-state", "idle")
        self.value["accepted"] = []
        self.value["publication"]["pending_tasks"] = 0
        self.refresh()
        self.assertNotIn("真实验收记录", recent.inner_text())
        expect(self.page.locator("#dev-pending-publish")).to_contain_text("0")

    def test_mobile_flow_reflows_all_six_stages_without_horizontal_scroll(self):
        from playwright.sync_api import expect
        self.value["task"]["title"] = "包含多项升级条件、跨系统兼容、存档核对与界面说明的长任务标题" * 5
        self.refresh()
        self.page.set_viewport_size({"width": 390, "height": 844})
        flow = self.page.locator(".dev-flow")
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 391)
        self.assertLessEqual(flow.evaluate("element => element.scrollWidth - element.clientWidth"), 1)
        boxes = []
        for identity, module, _ in STAGES:
            node = flow.locator(f'[data-node="{identity}"]')
            box = node.bounding_box()
            self.assertGreaterEqual(box["x"], 0)
            self.assertLessEqual(box["x"] + box["width"], 391)
            boxes.append(box)
            node.click()
            expect(self.page.locator("#module-" + module)).to_be_visible()
        self.assertLessEqual(len({round(box["x"]) for box in boxes}), 2)
        self.assertGreaterEqual(len({round(box["y"]) for box in boxes}), 3)
        screenshots = os.environ.get("UNATTENDED_TEST_SCREENSHOT_DIR")
        if screenshots:
            directory = Path(screenshots)
            directory.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(directory / "development-mobile.png"), full_page=True)

    def test_missing_task_clears_old_focus_and_does_not_invent_running_work(self):
        from playwright.sync_api import expect
        self.value = _empty(datetime.now(timezone.utc))
        self.value["status"].update(code="not_initialized", label="尚未初始化")
        self.refresh()
        self.assertNotIn("TASK-FOCUS-009", self.page.locator("#dev-task-id").inner_text())
        self.assertNotIn("住宅升级条件与真实检查证据", self.page.locator("#dev-task-title").inner_text())
        self.assertNotEqual(self.page.locator("#dev-state").get_attribute("data-state"), "running")
        expect(self.page.locator("#dev-check-pending")).not_to_contain_text("3")


if __name__ == "__main__":
    unittest.main()
