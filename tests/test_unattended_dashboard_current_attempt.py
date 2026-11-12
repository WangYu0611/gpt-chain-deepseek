"""A verified new invocation takes precedence over a prior persisted failure."""
from copy import deepcopy
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import Mock

from tests import test_unattended_dashboard_workflow as fixtures
from unattended import dashboard, dashboard_state


class CurrentCodeAttemptTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.DashboardWorkflowTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.base = self.f.fixture; self.state = self.f.state
        task = self.f.task()
        task.update(code_base="a" * 40, base_sha="a" * 40, session_id="550e8400-e29b-41d4-a716-446655440000",
                    session_owner_task_id="TASK-1", reworks=5)
        self.task = task
        self.error = "ProviderError: Provider exceeded its time limit"
        self.run = self.base.runtime / "runs" / ("code-" + "a" * 32); self.run.mkdir(parents=True)
        self.state.update(phase="CODE", expected_head="a" * 40, failures=2, last_error=self.error,
            operation={"kind": "code", "task_id": "TASK-1", "head_sha": "a" * 40,
                       "coding_provider": "claude", "output_dir": str(self.run), "started_at": "2026-11-02T11:00:00+00:00"})
        self.opening = {"type": "system", "subtype": "init", "session_id": task["session_id"], "model": "claude-sonnet-5-5"}
        (self.run / "stdout.log").write_text(json.dumps(self.opening) + "\n", encoding="utf-8")
        self.save()

    def save(self):
        self.base.write(self.base.runtime / "state.json", self.state)

    def snapshot(self):
        return dashboard_state.collect_snapshot(self.base.config_path)

    def test_current_native_invocation_is_running_and_previous_timeout_is_only_historical_without_writes(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.base.root.rglob("*") if p.is_file()}
        value = self.snapshot(); status = value["status"]
        self.assertEqual(status["code"], "code")
        self.assertIsNone(status["error"])
        self.assertEqual(status["previous_error"], self.error)
        self.assertIn("上次调用超时，当前已继续", status["summary"])
        self.assertEqual(status["failures"], 2)
        current = next(row for row in value["workflow"]["nodes"] if row["current"])
        self.assertEqual(current["state"], "running")
        self.assertNotIn("ProviderError", current["summary"])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.base.root.rglob("*") if p.is_file()})

    def test_current_failure_or_completed_receipt_does_not_claim_an_active_resumed_call(self):
        for receipt in ({"kind": "code", "exit_code": 1, "timed_out": True}, {"kind": "code", "exit_code": 0},
                        {"kind": "code", "launch_or_capture_failed": True}, {"kind": "review", "exit_code": 1}):
            with self.subTest(receipt=receipt):
                self.base.write(self.run / "process.json", receipt)
                status = self.snapshot()["status"]
                self.assertNotIn("previous_error", status)
                self.assertEqual(status["error"], self.error)
                self.assertNotIn("当前已继续", status["summary"])

    def test_foreign_task_head_session_or_missing_native_opening_cannot_hide_a_real_error(self):
        baseline = deepcopy(self.state)
        for variant in ("task", "head", "session", "owner", "opening"):
            with self.subTest(variant=variant):
                self.state = deepcopy(baseline)
                opening = dict(self.opening)
                if variant == "task": self.state["operation"]["task_id"] = "OTHER"
                elif variant == "head": self.state["operation"]["head_sha"] = "b" * 40
                elif variant == "session": opening["session_id"] = "550e8400-e29b-41d4-a716-446655440001"
                elif variant == "owner": self.state["task"]["session_owner_task_id"] = "OTHER"
                else: opening = {"type": "assistant", "text": "not a native opening"}
                (self.run / "stdout.log").write_text(json.dumps(opening) + "\n", encoding="utf-8")
                self.save(); status = self.snapshot()["status"]
                self.assertNotIn("previous_error", status)
                self.assertEqual(status["error"], self.error)

    def test_real_block_quota_and_stopped_worker_have_priority_over_resumed_evidence(self):
        self.state.update(phase="BLOCKED", blocked_from="CODE"); self.save()
        value = self.snapshot()
        self.assertEqual(value["status"]["code"], "blocked")
        self.assertEqual(value["status"]["error"], self.error)
        self.assertNotIn("previous_error", value["status"])
        self.state.update(phase="CODE", provider_retry={"phase": "CODE", "task_id": "TASK-1", "head_sha": "a" * 40,
            "attempt": 2, "reason": "claude_subscription_limit", "next_retry_at": "2026-11-02T11:10:00+00:00", "status": "waiting"})
        self.save(); status = self.snapshot()["status"]
        self.assertEqual(status["code"], "waiting_provider")
        self.assertIn("额度", status["summary"])
        self.assertNotIn("previous_error", status)
        self.state.pop("provider_retry"); self.save(); self.base.inspector.side_effect = OSError("stopped")
        status = self.snapshot()["status"]
        self.assertEqual(status["code"], "stopped")
        self.assertNotIn("previous_error", status)

    def maintenance(self, stage="waiting_boundary"):
        identity = "c" * 32
        pause = self.base.runtime / "PAUSE"; pause.write_bytes(("background-reload:" + identity + "\n").encode())
        fact = pause.stat()
        request = {"version": 1, "id": identity, "lane": "development", "status": stage,
            "config_file": str(self.base.config_path.resolve()), "runtime_dir": str(self.base.runtime),
            "config_sha256": hashlib.sha256(self.base.config_path.read_bytes()).hexdigest(),
            "source_identity": self.base.identity, "pause": {"mode": "owned", "token": pause.read_text(),
                "fingerprint": {"sha256": hashlib.sha256(pause.read_bytes()).hexdigest(), "mtime_ns": fact.st_mtime_ns, "size": fact.st_size}}}
        self.base.write(self.base.runtime / "background-reload-request.json", {"version": 1, "id": identity})
        path = self.base.runtime / "reload-history" / (identity + ".json")
        self.base.write(path, request)
        return request, path

    def test_owned_main_reload_explains_boundary_and_keeps_paused_gate(self):
        self.maintenance()
        value = self.snapshot(); status = value["status"]
        self.assertEqual(status["code"], "paused")
        self.assertIn("当前调用会自然结束", status["summary"])
        self.assertTrue(value["maintenance"]["current_call_active"])
        self.assertEqual(status["previous_error"], self.error)
        self.assertIsNone(status["error"])
        self.assertTrue(status["paused"])
        self.assertEqual(next(row for row in value["workflow"]["nodes"] if row["current"])["state"], "running")

    def test_real_user_pause_finished_call_and_unknown_model_cannot_be_maintenance_running(self):
        pause = self.base.runtime / "PAUSE"; pause.write_text("user requested pause")
        value = self.snapshot()
        self.assertIsNone(value["maintenance"])
        self.assertEqual(next(row for row in value["workflow"]["nodes"] if row["current"])["state"], "paused")
        self.maintenance()
        self.base.write(self.run / "process.json", {"kind": "code", "exit_code": 0})
        value = self.snapshot()
        self.assertFalse(value["maintenance"]["current_call_active"])
        self.assertEqual(next(row for row in value["workflow"]["nodes"] if row["current"])["state"], "paused")
        (self.run / "process.json").unlink()
        (self.run / "stdout.log").write_text(json.dumps({**self.opening, "model": "unknown"}) + "\n")
        value = self.snapshot()
        self.assertEqual(next(row for row in value["workflow"]["nodes"] if row["current"])["state"], "paused")

    def test_main_switch_stages_and_foreign_lane_never_claim_model_running(self):
        for stage in ("stopping", "starting"):
            with self.subTest(stage=stage):
                self.maintenance(stage)
                value = self.snapshot()
                self.assertEqual(value["status"]["code"], "paused")
                self.assertFalse(value["maintenance"]["current_call_active"])
                self.assertIn("等待后台接续", value["status"]["summary"])
        request, path = self.maintenance()
        request["lane"] = "art"; self.base.write(path, request)
        self.assertIsNone(self.snapshot()["maintenance"])


@unittest.skipUnless(os.environ.get("UNATTENDED_TEST_BROWSER") == "1", "opt-in isolated headless browser")
class CurrentAttemptBrowserTests(unittest.TestCase):
    def test_active_code_and_historical_timeout_are_presented_separately(self):
        from playwright.sync_api import expect, sync_playwright
        fixture = CurrentCodeAttemptTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        cache = Mock(); cache.get.side_effect = fixture.snapshot
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(cache, {}))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="chrome", headless=True)
                page = browser.new_page(viewport={"width": 390, "height": 844})
                writes, errors = [], []
                def read_only(route):
                    if route.request.method in {"GET", "HEAD"}: route.continue_()
                    else: writes.append(route.request.method); route.abort()
                page.route("**/*", read_only)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/#overview")
                expect(page.locator("#status-summary")).to_contain_text("上次调用超时，当前已继续")
                expect(page.locator("#status-error")).to_contain_text("上次调用记录")
                expect(page.locator("#status-error")).to_have_attribute("data-retrying", "true")
                page.locator('.links a[href="#development"]').click()
                expect(page.locator("#dev-task-summary")).to_contain_text("当前已继续")
                expect(page.locator('.dev-flow [data-node="code"]')).to_have_attribute("data-state", "running")
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
                self.assertFalse(writes); self.assertFalse(errors); cache.resolve_art.assert_not_called()
                browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


if __name__ == "__main__":
    unittest.main()
