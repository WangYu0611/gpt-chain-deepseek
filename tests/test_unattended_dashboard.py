from __future__ import annotations

from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import Mock, patch

from unattended import dashboard
from unattended.cli import main


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.cache = Mock()
        self.cache.get.return_value = {"status": {"label": "正在编写代码"}, "task": {"title": "<script>not executable</script>"}}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(self.cache, {"service": dashboard.OWNER}))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, method="GET", headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_status_is_json_and_page_uses_safe_local_content_policy(self):
        status, headers, body = self.request("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), self.cache.get.return_value)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("无人值守工作台", body.decode())
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_foreign_host_and_origin_cannot_read_snapshot(self):
        for headers in ({"Host": "attacker.example"}, {"Origin": "https://attacker.example"}, {"Origin": "null"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/status", headers=headers)[0], 403)
        self.cache.get.assert_not_called()

    def test_read_only_routes_do_not_expose_files_or_accept_actions(self):
        for path in ("/../unattended.local.json", "/api/pause", "/.unattended/feishu.local.json"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request("/api/status", method="POST")[0], 405)
        self.cache.get.assert_not_called()

    def test_deferred_items_have_a_separate_text_only_panel_and_json_collection(self):
        self.cache.get.return_value.update(accepted=[], deferred=[{
            "id": "TASK-9", "title": "HUD <script>not executable</script>",
            "head_sha": "b" * 40, "base_sha": "a" * 40,
            "reason": "等待用户手动确认，不阻塞其他开发", "deferred_at": "2026-11-03T01:00:00+00:00",
            "status": "pending_manual_confirmation"}])
        status, _, body = self.request("/api/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["accepted"], [])
        self.assertEqual(data["deferred"][0]["status"], "pending_manual_confirmation")
        status, _, body = self.request("/")
        page = body.decode()
        self.assertEqual(status, 200)
        self.assertIn('id="deferred-panel"', page)
        self.assertIn('id="deferred"', page)
        self.assertIn("待手动确认（不阻塞开发）", page)
        self.assertIn("这些事项尚未验收通过", page)
        self.assertIn("node('div','event-text',d.reason)", page)
        self.assertIn("el.textContent=str(value)", page)
        self.assertNotIn(".innerHTML", page)
        self.assertNotIn("insertAdjacentHTML", page)

    def test_provider_wait_uses_iso_countdown_and_does_not_highlight_execution(self):
        self.cache.get.return_value.update(status={"code": "waiting_provider", "phase": "CODE", "actor": "系统"},
            provider_retry={"provider": "DSH", "reason_label": "模型服务暂时不可用", "attempt": 2,
                            "next_retry_at": "2026-11-02T11:10:00+00:00", "status": "waiting"})
        status, _, body = self.request("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["provider_retry"]["next_retry_at"], "2026-11-02T11:10:00+00:00")
        status, _, body = self.request("/")
        self.assertEqual(status, 200)
        page = body.decode()
        self.assertIn("parsed(retry?.next_retry_at)", page)
        self.assertIn("nextRetry.getTime()-Date.now()", page)
        self.assertIn("即将重试，等待执行器确认", page)
        self.assertIn("待重试 · 服务恢复后继续", page)
        self.assertIn("&&!providerWaiting", page)
        self.assertIn("classList.toggle('waiting',providerWaiting||filesystemWaiting)", page)


class DashboardIntegrationTests(unittest.TestCase):
    def test_snapshot_cache_only_exposes_allowlisted_notification_fields(self):
        with patch.object(dashboard, "collect_snapshot", return_value={"project": {"name": "test"}}):
            cache = dashboard.SnapshotCache("unused.json")
        cache.notification_status({"enabled": True, "last_success_at": "2026-11-02T10:00:00+00:00",
                                   "last_event": "accepted", "pending": 0, "webhook_url": "private"})
        first = cache.get()
        self.assertNotIn("private", json.dumps(first))
        self.assertEqual(first["notifications"]["last_sent_at"], "2026-11-02T10:00:00+00:00")
        self.assertEqual(first["notifications"]["last_event"], "任务验收通过")
        first["project"]["name"] = "changed by consumer"
        self.assertEqual(cache.get()["project"]["name"], "test")

    def test_foreign_scheduled_task_is_not_overwritten(self):
        manager = object.__new__(dashboard.DashboardManager)
        manager.name, manager.description = "task", "our-description"
        scheduler = Mock()
        scheduler.info.return_value = {"owner": "other", "description": "other", "state": 3}
        with self.assertRaisesRegex(RuntimeError, "拒绝覆盖"):
            manager._register(scheduler)
        scheduler.service.NewTask.assert_not_called()

    def test_cli_opens_panel_without_starting_or_resuming_engine(self):
        with patch.object(dashboard, "DashboardManager") as manager, patch("unattended.engine.Engine") as engine:
            manager.return_value.start.return_value = {"status": "running", "url": "http://127.0.0.1:8765/"}
            self.assertEqual(main(["dashboard", "--no-open"]), 0)
            manager.return_value.start.assert_called_once_with(open_browser=False)
            engine.assert_not_called()


if __name__ == "__main__":
    unittest.main()
