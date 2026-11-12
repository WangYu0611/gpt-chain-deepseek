"""All full-flow notification delivery is mocked; no webhook is contacted."""
from copy import deepcopy
import json
import unittest

from tests import test_unattended_notifications as fixtures
from unattended import notifications as notify


class FullFlowNotificationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.NotificationTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.job = {"id": "a" * 32, "task_id": "TASK-1", "head_sha": "b" * 40, "status": "testing", "stage": "regression",
            "stage_label": "回归测试", "model": "claude-opus-5-5", "effort": "high", "summary": "正在执行独立全流程测试。",
            "notification_proof": {"test_native_started": True, "request_sha256": "c" * 64, "terminal_evidence": False}}
        self.f.snapshot["full_flow_test"] = self.job

    def test_actual_start_and_completed_with_failures_send_two_blue_cards_once_across_restart(self):
        self.f.monitor.tick(self.f.snapshot)
        self.f.monitor.tick(self.f.snapshot)
        self.assertEqual(self.f.opener.open.call_count, 1)
        self.job.update(status="completed", stage="analysis", stage_label="结果分析", summary="全流程已结束，仍有失败检查。",
                        retest={"summary": "补充复测单独保留，首轮失败未被覆盖。"})
        self.job["notification_proof"].update(terminal_evidence=True, outcome="checks_failed_or_not_run", failed_stages=["visual", "package"])
        self.f.monitor.tick(self.f.snapshot)
        self.f.monitor.tick(self.f.snapshot)
        notify.NotificationMonitor(self.f.root / "config.json", self.f.settings).tick(self.f.snapshot)
        self.assertEqual(self.f.opener.open.call_count, 2)
        for card in self.f.payloads():
            self.assertEqual(card["card"]["header"]["template"], "blue")
        first, last = [json.dumps(card, ensure_ascii=False) for card in self.f.payloads()]
        self.assertIn("实际测试已启动", first)
        self.assertIn("首轮回执含 FAIL 或未运行记录", last)
        self.assertIn("首轮失败未被覆盖", last)
        self.assertNotIn("通过 · 独立任务验收", last)
        self.assertEqual(len(self.f.snapshot["accepted"]), 1)

    def test_plan_preparing_configured_model_and_unverified_snapshot_do_not_notify(self):
        for value in ({"status": "planning", "stage": "plan"}, {"status": "preparing", "stage": "prepare"},
                      {"notification_proof": {}}, {"model": "claude-sonnet-5-5"}, {"effort": "medium"}):
            self.f.snapshot["full_flow_test"] = {**deepcopy(self.job), **value}
            self.f.monitor.tick(self.f.snapshot)
        self.f.opener.open.assert_not_called()

    def test_failed_test_needs_terminal_evidence_and_reports_failure_not_acceptance(self):
        self.f.monitor.tick(self.f.snapshot)
        self.job.update(status="failed", stage="analysis", summary="原生测试执行失败，报告未完成。")
        self.f.monitor.tick(self.f.snapshot)
        self.assertEqual(self.f.opener.open.call_count, 1)
        self.job["notification_proof"].update(terminal_evidence=True, outcome="execution_failed", failed_stages=["regression"])
        self.f.monitor.tick(self.f.snapshot)
        self.assertEqual(self.f.opener.open.call_count, 2)
        body = json.dumps(self.f.payloads()[-1], ensure_ascii=False)
        self.assertIn("执行失败或中断", body)
        self.assertNotIn("用户通过已提交", body)

    def test_existing_explicit_event_allowlist_is_respected_and_status_read_never_sends(self):
        self.f.options["events"] = ["window_start", "accepted", "blocked", "worker_exit", "window_end"]
        self.f.write_settings()
        self.f.monitor.status(); self.f.monitor.tick(self.f.snapshot)
        self.f.opener.open.assert_not_called()
        self.f.options["events"].extend(notify.FULL_FLOW_EVENTS); self.f.write_settings()
        self.f.monitor.tick(self.f.snapshot)
        self.assertEqual(self.f.opener.open.call_count, 1)

    def test_identity_and_head_are_the_dedup_key_and_main_read_error_does_not_hide_verified_test(self):
        self.f.snapshot["error"] = "main state temporarily unreadable"
        self.f.snapshot["status"]["code"] = "error"
        self.f.monitor.tick(self.f.snapshot)
        self.assertEqual(self.f.opener.open.call_count, 1)
        self.job["summary"] = "Only the summary changed"
        self.f.monitor.tick(self.f.snapshot)
        self.assertEqual(self.f.opener.open.call_count, 1)
        self.job["head_sha"] = "d" * 40
        self.job["notification_proof"]["request_sha256"] = "e" * 64
        self.f.monitor.tick(self.f.snapshot)
        self.assertEqual(self.f.opener.open.call_count, 2)

    def test_blue_card_omits_private_fields_and_red_art_cards_are_unchanged(self):
        self.job.update(summary="token=DO_NOT_LEAK https://private.example/report", native_prompt="NATIVE_PROMPT_DO_NOT_SEND")
        self.f.monitor.tick(self.f.snapshot)
        body = json.dumps(self.f.payloads()[0])
        self.assertNotIn("DO_NOT_LEAK", body); self.assertNotIn("private.example", body); self.assertNotIn("NATIVE_PROMPT", body)
        art = self.f.enable_art()
        self.assertEqual(notify.build_card(self.f.snapshot, "art_review_ready", art_request=art)["card"]["header"]["template"], "red")

    def test_analysis_continuation_does_not_reannounce_the_same_job_start(self):
        self.f.monitor.tick(self.f.snapshot)
        self.job.update(status="analyzing", stage="analysis", stage_label="结果分析", summary="同一任务继续使用 Monitor 等待已有测试。")
        self.f.monitor.tick(self.f.snapshot)
        notify.NotificationMonitor(self.f.root / "config.json", self.f.settings).tick(self.f.snapshot)
        self.assertEqual(self.f.opener.open.call_count, 1)
        self.job.update(status="completed")
        self.job["notification_proof"].update(terminal_evidence=True, outcome="checks_failed_or_not_run", failed_stages=["visual"])
        self.f.monitor.tick(self.f.snapshot)
        self.assertEqual(self.f.opener.open.call_count, 2)


if __name__ == "__main__":
    unittest.main()
