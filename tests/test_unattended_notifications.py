"""Notification delivery is entirely mocked; tests never contact Feishu."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from unattended import notifications as notify

WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/00000000-1111-2222-3333-444444444444"


class NotificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = self.root / "notifications.private.json"
        self.options = {"enabled": True, "webhook_url": WEBHOOK, "dashboard_url": "http://127.0.0.1:8765/"}
        self.write_settings()
        self.monitor = notify.NotificationMonitor(self.root / "config.json", self.settings)
        self.snapshot = {"generated_at": "2026-11-02T09:00:00+00:00", "project": {"name": "Demo", "milestone": "M2"},
                         "status": {"code": "code", "phase": "CODE", "label": "编写代码", "summary": "正在实现功能", "paused": False},
                         "schedule": {"allowed": False, "next_open": "2026-11-02T18:00:00+08:00", "closes_at": None},
                         "worker": {"alive": True, "healthy": True, "pid": 111},
                         "task": {"id": "T002", "title": "新增行为", "head_sha": "a" * 40, "reworks": 0},
                         "accepted": [self.record("T001")],
                         "publication": {"mode": "milestone", "milestone_id": "M2", "pending_tasks": 1,
                                         "summary": "里程碑独立验收通过后统一发布"}}
        self.now = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
        self.clock = patch.object(notify, "_now", side_effect=lambda: self.now).start()
        self.opener = MagicMock()
        self.response = self.opener.open.return_value.__enter__.return_value
        self.response.status = 200
        self.response.read.return_value = b'{"code":0,"msg":"success"}'
        self.factory = patch.object(notify.request, "build_opener", return_value=self.opener).start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def record(identity):
        return {"id": identity, "title": "行为已验证", "sha": "b" * 40,
                "review_summary": "独立行为检查通过，操作结果符合预期。", "accepted_at": "2026-11-02T18:10:00+08:00"}

    def write_settings(self):
        self.settings.write_text(json.dumps(self.options), encoding="utf-8")

    def test_enabled_notifications_can_read_the_webhook_from_local_secrets(self):
        self.options.pop("webhook_url")
        self.write_settings()
        (self.root / "secrets.local.json").write_text(json.dumps({"FEISHU_WEBHOOK_URL": WEBHOOK}), encoding="utf-8")
        status = self.monitor.status()
        self.assertTrue(status["enabled"])
        self.assertNotIn(WEBHOOK, json.dumps(status))
        self.opener.open.assert_not_called()

    def payloads(self):
        return [json.loads(call.args[0].data) for call in self.opener.open.call_args_list]

    def begin_window(self):
        self.snapshot["schedule"].update(allowed=True, closes_at="2026-11-03T08:00:00+08:00")

    def art_request(self, identity="ART-001", version="a" * 64):
        return {"id": identity, "title": "普通住宅三级样板", "status": "awaiting_approval",
                "review_package_sha256": version, "can_resolve": True, "can_accept": True,
                "preview_error": None, "previews": [{"name": "courtyard", "url": "/api/art/preview?private=omitted"}],
                "approved_levels": ["L1", "L4", "L8"], "budget": {"actual": 260, "limit": 3000},
                "technical_review": {"decision": "accepted", "summary": "技术证据足够，等待用户视觉验收。"}}

    def enable_art(self, item=None):
        self.snapshot["art"] = {"configured": True, "status": "awaiting_approval", "phase": "PLAN", "error": None,
                                "task_id": None, "requests": [item or self.art_request()]}
        return self.snapshot["art"]["requests"][0]

    def test_all_functional_events_are_blue_with_explicit_outcome_labels(self):
        for event in ("test", "window_start", "accepted", "blocked", "worker_exit", "window_end"):
            with self.subTest(event=event):
                payload = notify.build_card(self.snapshot, event, accepted=self.record("T002") if event == "accepted" else None)
                self.assertEqual(payload["card"]["header"]["template"], "blue")
                self.assertTrue(payload["card"]["header"]["title"]["content"].startswith("功能开发 · "))
                body = json.dumps(payload, ensure_ascii=False)
                if event == "accepted":
                    self.assertIn("通过 · 独立任务验收", body)
                elif event in {"blocked", "worker_exit"}:
                    self.assertIn("异常 ·", body)

    def test_all_art_events_are_red_and_keep_user_acceptance_distinct(self):
        item = self.enable_art()
        item["decision"] = {"decision": "accepted", "feedback": "认可本版"}
        for event in notify.ART_EVENTS:
            with self.subTest(event=event):
                payload = notify.build_card(self.snapshot, event, art_request=item)
                self.assertEqual(payload["card"]["header"]["template"], "red")
                self.assertTrue(payload["card"]["header"]["title"]["content"].startswith("模型验收 · "))
                self.assertIn(item["review_package_sha256"][:12], json.dumps(payload))
                self.assertLess(len(json.dumps(payload, ensure_ascii=False).encode()), 18000)
        ready = json.dumps(notify.build_card(self.snapshot, "art_review_ready", art_request=item), ensure_ascii=False)
        self.assertIn("待验收", ready)
        self.assertNotIn("用户通过已提交", ready)
        self.assertIn("260 / 3000", ready)

    def test_current_art_review_alerts_once_on_first_observation_and_restart(self):
        self.enable_art()
        self.monitor.tick(self.snapshot)
        self.monitor.tick(self.snapshot)
        notify.NotificationMonitor(self.root / "config.json", self.settings).tick(self.snapshot)
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertIn("模型验收 · 等待视觉验收", json.dumps(self.payloads()[0], ensure_ascii=False))

    def test_old_journal_adds_only_current_pending_art_not_historical_decisions(self):
        self.monitor.tick(self.snapshot)
        current = self.enable_art()
        old_accepted, old_rework = self.art_request("ART-OLD-A"), self.art_request("ART-OLD-R")
        old_accepted.update(status="accepted", can_resolve=False)
        old_rework.update(can_resolve=False, decision={"decision": "rework", "feedback": "历史意见"})
        self.snapshot["art"]["requests"] = [old_accepted, old_rework, current]
        for _ in range(4):
            self.monitor.tick(self.snapshot)
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertNotIn("ART-OLD", json.dumps(self.payloads()))

    def test_art_decision_types_each_notify_once_without_claiming_integrated(self):
        for decision, event in (("accepted", "art_accepted"), ("rework", "art_rework")):
            with self.subTest(decision=decision):
                item = self.enable_art(self.art_request("ART-" + decision))
                self.monitor.tick(self.snapshot)
                item.update(can_resolve=False, decision={"decision": decision, "feedback": "明确的用户意见"})
                result = self.monitor.tick(self.snapshot)
                self.assertEqual(result["last_event"], event)
                count = self.opener.open.call_count
                self.monitor.tick(self.snapshot)
                restarted = notify.NotificationMonitor(self.root / "config.json", self.settings)
                restarted.tick(self.snapshot)
                self.assertEqual(self.opener.open.call_count, count)
                body = json.dumps(self.payloads()[-1], ensure_ascii=False)
                self.assertIn("明确的用户意见", body)
                self.assertNotIn("已入库", body)
                if decision == "accepted":
                    item["status"] = "accepted"
                    self.monitor.tick(self.snapshot)
                    self.assertEqual(self.opener.open.call_count, count)

    def test_new_review_sha_not_title_change_triggers_next_art_alert(self):
        item = self.enable_art()
        self.monitor.tick(self.snapshot)
        item["title"] = "标题仅做校正"
        self.monitor.tick(self.snapshot)
        self.assertEqual(self.opener.open.call_count, 1)
        item["review_package_sha256"] = "b" * 64
        self.monitor.tick(self.snapshot)
        self.monitor.tick(self.snapshot)
        self.assertEqual(self.opener.open.call_count, 2)

    def test_stale_or_unverified_art_review_is_not_announced(self):
        item = self.enable_art()
        for changes in ({"review_package_sha256": "spoofed"}, {"preview_error": "版本已变化"},
                        {"previews": []}, {"can_resolve": False}, {"status": "in_progress"}):
            with self.subTest(changes=changes):
                self.snapshot["art"]["requests"] = [{**item, **changes}]
                self.monitor.tick(self.snapshot)
        self.opener.open.assert_not_called()

    def test_pending_superseded_review_is_cancelled_before_retry(self):
        item = self.enable_art()
        self.opener.open.side_effect = URLError("offline")
        self.monitor.tick(self.snapshot)
        item["review_package_sha256"] = "b" * 64
        self.monitor.tick(self.snapshot)
        self.opener.open.side_effect = None
        self.now += timedelta(seconds=60)
        self.monitor.tick(self.snapshot)
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertIn("b" * 12, json.dumps(self.payloads()[-1]))
        journal = json.loads(self.monitor.journal_path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(row["state"] for row in journal["events"].values()), ["sent", "superseded"])

    def test_art_blocked_is_an_entry_event_and_read_errors_do_not_rearm(self):
        self.enable_art()
        self.snapshot["art"]["requests"] = []
        self.monitor.tick(self.snapshot)
        self.snapshot["art"].update(status="blocked", phase="BLOCKED", task_id="ART-001", last_error="尺寸检查失败")
        self.monitor.tick(self.snapshot)
        self.monitor.tick(self.snapshot)
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertIn("异常 · 需要处理", json.dumps(self.payloads()[0], ensure_ascii=False))
        self.snapshot["art"]["error"] = "读取暂不可用"
        self.monitor.tick(self.snapshot)
        self.snapshot["art"]["error"] = None
        self.monitor.tick(self.snapshot)
        self.assertEqual(self.opener.open.call_count, 1)
        self.snapshot["art"].update(status="code", phase="CODE")
        self.monitor.tick(self.snapshot)
        self.snapshot["art"].update(status="blocked", phase="BLOCKED")
        self.monitor.tick(self.snapshot)
        self.assertEqual(self.opener.open.call_count, 2)

    def test_valid_art_can_notify_during_main_read_failure_without_false_functional_events(self):
        self.monitor.tick(self.snapshot)
        self.enable_art()
        self.snapshot.update(error="功能状态读取失败", worker={"alive": False})
        self.monitor.tick(self.snapshot)
        self.monitor.tick(self.snapshot)
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual(self.payloads()[0]["card"]["header"]["template"], "red")

    def test_explicit_old_event_allowlist_remains_unchanged_and_no_art_is_sent(self):
        self.options["events"] = ["window_start", "accepted", "blocked", "worker_exit", "window_end"]
        self.write_settings()
        before = self.settings.read_bytes()
        self.enable_art()
        self.monitor.tick(self.snapshot)
        self.assertEqual(self.settings.read_bytes(), before)
        self.opener.open.assert_not_called()

    def test_appending_art_events_after_legacy_baseline_alerts_pending_review_once(self):
        self.options["events"] = [event for event in notify.EVENTS if event not in notify.ART_EVENTS]
        self.write_settings()
        self.enable_art()
        self.monitor.tick(self.snapshot)
        self.opener.open.assert_not_called()
        self.options["events"].extend(notify.ART_EVENTS)
        self.write_settings()
        self.monitor.tick(self.snapshot)
        self.monitor.tick(self.snapshot)
        notify.NotificationMonitor(self.root / "config.json", self.settings).tick(self.snapshot)
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertIn("模型验收 · 等待视觉验收", json.dumps(self.payloads()[0], ensure_ascii=False))

    def test_art_card_excludes_private_sources_and_reasoning(self):
        item = self.enable_art()
        item.update(body="PRIVATE_PROMPT", preview_paths=["PRIVATE_FILE"], private_ledger="PRIVATE_LEDGER")
        item["technical_review"]["summary"] = "<think>PRIVATE_REASONING</think> api_key=sk-123456789 " + WEBHOOK + " 技术检查通过"
        payload = notify.build_card(self.snapshot, "art_review_ready", art_request=item)
        body = json.dumps(payload, ensure_ascii=False)
        for secret in ("PRIVATE_PROMPT", "PRIVATE_FILE", "PRIVATE_LEDGER", "PRIVATE_REASONING", "sk-123456789", WEBHOOK, "/api/art/"):
            self.assertNotIn(secret, body)

    def test_art_blocked_card_prefers_actionable_defect_over_passing_check_preamble(self):
        self.enable_art()
        self.snapshot["art"].update(last_error="所有文件哈希与独立检查通过。" * 60,
                                    blocking_summary="可观察缺陷：院墙孤立，门框与屋顶需要修改。")
        payload = notify.build_card(self.snapshot, "art_blocked")
        body = json.dumps(payload, ensure_ascii=False)
        self.assertIn("院墙孤立，门框与屋顶需要修改", body)
        self.assertNotIn("所有文件哈希与独立检查通过", body)

    def test_art_ready_note_asserts_technical_pass_only_for_accepted_review(self):
        item = self.enable_art()
        for review in (None, {}, {"decision": "rework"}, {"decision": "blocked"}, {"decision": "accepted"}):
            with self.subTest(review=review):
                item["technical_review"] = review
                payload = notify.build_card(self.snapshot, "art_review_ready", art_request=item)
                note = payload["card"]["elements"][-1]["elements"][0]["content"]
                if review and review.get("decision") == "accepted":
                    self.assertTrue(note.startswith("技术检查通过；"))
                else:
                    self.assertIn("摘要未提供", note)
                    self.assertIn("请核对本机证据", note)
                    self.assertFalse(note.startswith("技术检查通过；"))

    def test_legacy_queued_cards_migrate_only_type_header_and_preserve_retry_dedup(self):
        for event, old_color in (("accepted", "green"), ("blocked", "red")):
            with self.subTest(event=event):
                monitor = notify.NotificationMonitor(self.root / (event + "-config.json"), self.settings)
                snapshot = deepcopy(self.snapshot)
                monitor.tick(snapshot)
                if event == "accepted":
                    snapshot["accepted"].append(self.record("T002"))
                else:
                    snapshot["status"].update(code="blocked", phase="BLOCKED")
                with patch.object(monitor, "_deliver"):
                    monitor.tick(snapshot)
                journal = json.loads(monitor.journal_path.read_text(encoding="utf-8"))
                identity, row = next(iter(journal["events"].items()))
                row["card"]["card"]["header"].update(template=old_color, title={"tag": "plain_text", "content": "无人值守开发 · 原始事件标题"})
                row.update(attempts=1, next_at=self.now.timestamp() + 60)
                original = deepcopy(row["card"])
                before_seen = journal["seen"][:]
                monitor.journal_path.write_text(json.dumps(journal), encoding="utf-8")
                count = self.opener.open.call_count
                monitor.tick(snapshot)
                self.assertEqual(self.opener.open.call_count, count)
                self.now += timedelta(seconds=60)
                monitor.tick(snapshot)
                expected = deepcopy(original)
                expected["card"]["header"]["template"] = "blue"
                expected["card"]["header"]["title"]["content"] = "功能开发 · 原始事件标题"
                self.assertEqual(self.payloads()[-1], expected)
                after = json.loads(monitor.journal_path.read_text(encoding="utf-8"))
                self.assertEqual(after["events"][identity]["attempts"], 2)
                self.assertEqual(after["events"][identity]["state"], "sent")
                self.assertEqual(after["seen"], before_seen)
                monitor.tick(snapshot)
                self.assertEqual(self.opener.open.call_count, count + 1)

    def test_construct_status_and_baseline_never_send_or_replay_history(self):
        before = list(self.root.iterdir())
        self.assertEqual(self.monitor.status()["state"], "ready")
        self.assertEqual(before, list(self.root.iterdir()))
        self.monitor.tick(self.snapshot)
        self.monitor.tick(self.snapshot)
        self.opener.open.assert_not_called()
        journal = json.loads(self.monitor.journal_path.read_text(encoding="utf-8"))
        self.assertTrue(journal["initialized"])
        self.assertEqual(len(journal["seen"]), 1)

    def test_explicit_test_is_interactive_small_and_phone_safe(self):
        result = self.monitor.send_test(self.snapshot)
        self.assertTrue(result["ok"])
        payload = self.payloads()[0]
        self.assertEqual(payload["msg_type"], "interactive")
        self.assertIn("header", payload["card"])
        self.assertEqual({item["tag"] for item in payload["card"]["elements"]}, {"div", "note"})
        body = json.dumps(payload, ensure_ascii=False)
        self.assertIn("本机面板", body)
        self.assertIn("M2", body)
        self.assertIn("待整体发布", body)
        self.assertIn("本地提交", body)
        self.assertNotIn('"button"', body)
        self.assertNotIn(WEBHOOK, body)
        self.assertLess(len(body.encode()), 18000)
        call = self.opener.open.call_args
        self.assertEqual(call.kwargs["timeout"], 8)
        self.assertEqual(call.args[0].get_method(), "POST")
        self.assertIsInstance(self.factory.call_args.args[0], notify._NoRedirect)

    def test_new_accepted_summary_once_and_restart_dedup(self):
        self.monitor.tick(self.snapshot)
        self.snapshot["accepted"].append(self.record("T002"))
        self.monitor.tick(self.snapshot)
        self.monitor.tick(self.snapshot)
        notify.NotificationMonitor(self.root / "config.json", self.settings).tick(self.snapshot)
        self.assertEqual(self.opener.open.call_count, 1)
        body = json.dumps(self.payloads()[0], ensure_ascii=False)
        self.assertIn("任务验收通过", body)
        self.assertIn("T002", body)
        self.assertIn("独立行为检查通过", body)

    def test_task_commit_change_is_a_new_acceptance(self):
        self.monitor.tick(self.snapshot)
        self.snapshot["accepted"][0]["sha"] = "c" * 40
        self.monitor.tick(self.snapshot)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_window_start_end_and_count_survive_restart(self):
        self.monitor.tick(self.snapshot)
        self.begin_window()
        self.monitor.tick(self.snapshot)
        self.snapshot["accepted"].append(self.record("T002"))
        self.monitor.tick(self.snapshot)
        restarted = notify.NotificationMonitor(self.root / "config.json", self.settings)
        self.snapshot["schedule"].update(allowed=False, closes_at=None)
        restarted.tick(self.snapshot)
        restarted.tick(self.snapshot)
        self.assertEqual(self.opener.open.call_count, 3)
        bodies = [json.dumps(card, ensure_ascii=False) for card in self.payloads()]
        self.assertIn("工作窗口开始", bodies[0])
        self.assertIn("工作窗口结束", bodies[-1])
        self.assertIn("新增验收 1 项", bodies[-1])

    def test_skipped_closed_period_detects_next_window(self):
        self.begin_window()
        self.monitor.tick(self.snapshot)
        self.snapshot["schedule"]["closes_at"] = "2026-11-04T08:00:00+08:00"
        self.monitor.tick(self.snapshot)
        self.monitor.tick(self.snapshot)
        bodies = [json.dumps(card, ensure_ascii=False) for card in self.payloads()]
        self.assertEqual(len(bodies), 2)
        self.assertIn("工作窗口结束", bodies[0])
        self.assertIn("工作窗口开始", bodies[1])

    def test_acceptance_at_opening_edge_is_counted_in_new_window(self):
        self.monitor.tick(self.snapshot)
        self.begin_window()
        self.snapshot["accepted"].append(self.record("T002"))
        self.monitor.tick(self.snapshot)
        self.monitor.tick(self.snapshot)
        self.snapshot["schedule"].update(allowed=False, closes_at=None)
        self.monitor.tick(self.snapshot)
        self.assertIn("新增验收 1 项", json.dumps(self.payloads()[-1], ensure_ascii=False))

    def test_acceptances_across_missed_closed_period_use_acceptance_times(self):
        self.begin_window()
        self.monitor.tick(self.snapshot)
        old, new = self.record("T002"), self.record("T003")
        new["accepted_at"] = "2026-11-03T18:10:00+08:00"
        self.snapshot["accepted"].extend([old, new])
        self.snapshot["schedule"]["closes_at"] = "2026-11-04T08:00:00+08:00"
        for _ in range(4):
            self.monitor.tick(self.snapshot)
        self.snapshot["schedule"].update(allowed=False, closes_at=None)
        self.monitor.tick(self.snapshot)
        end_cards = [card for card in self.payloads() if "工作窗口结束" in json.dumps(card, ensure_ascii=False)]
        self.assertEqual(len(end_cards), 2)
        for card in end_cards:
            self.assertIn("新增验收 1 项", json.dumps(card, ensure_ascii=False))

    def test_blocked_only_on_entry_and_rework_quiet(self):
        self.monitor.tick(self.snapshot)
        self.snapshot["task"]["reworks"] = 1
        self.snapshot["status"]["failures"] = 1
        self.monitor.tick(self.snapshot)
        self.opener.open.assert_not_called()
        self.snapshot["status"].update(code="blocked", phase="BLOCKED", label="需要处理")
        self.monitor.tick(self.snapshot)
        self.monitor.tick(self.snapshot)
        self.assertEqual(self.opener.open.call_count, 1)
        self.snapshot["status"].update(code="code", phase="CODE")
        self.monitor.tick(self.snapshot)
        self.snapshot["status"].update(code="blocked", phase="BLOCKED")
        self.monitor.tick(self.snapshot)
        self.assertEqual(self.opener.open.call_count, 2)

    def test_worker_exit_is_transition_and_paused_done_do_not_alert(self):
        self.monitor.tick(self.snapshot)
        self.snapshot["worker"]["healthy"] = False
        self.monitor.tick(self.snapshot)
        self.opener.open.assert_not_called()
        self.snapshot["worker"]["alive"] = False
        self.monitor.tick(self.snapshot)
        self.monitor.tick(self.snapshot)
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertIn("后台意外退出", json.dumps(self.payloads()[0], ensure_ascii=False))
        for change in ({"paused": True}, {"paused": False, "phase": "DONE"}):
            self.snapshot["worker"]["alive"] = True
            self.monitor.tick(self.snapshot)
            self.snapshot["status"].update(change)
            self.snapshot["worker"]["alive"] = False
            self.monitor.tick(self.snapshot)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_read_error_does_not_generate_false_exit_or_window_end(self):
        self.begin_window()
        self.monitor.tick(self.snapshot)
        broken = deepcopy(self.snapshot)
        broken.update(error="read failed", accepted=[], worker={"alive": False}, schedule={"allowed": False})
        self.monitor.tick(broken)
        self.monitor.tick(self.snapshot)
        self.opener.open.assert_not_called()

    def test_failed_delivery_backoff_bounded_and_secret_never_logged(self):
        self.monitor.tick(self.snapshot)
        self.opener.open.side_effect = URLError("private secret " + WEBHOOK)
        self.snapshot["accepted"].append(self.record("T002"))
        result = self.monitor.tick(self.snapshot)
        self.assertEqual(result["last_error"], "delivery_failed")
        self.assertEqual(self.opener.open.call_count, 1)
        for seconds, expected in ((10, 1), (50, 2), (10, 2), (290, 3), (4000, 3)):
            self.now += timedelta(seconds=seconds)
            self.monitor = notify.NotificationMonitor(self.root / "config.json", self.settings)
            result = self.monitor.tick(self.snapshot)
            self.assertEqual(self.opener.open.call_count, expected)
        self.assertEqual(result["pending"], 0)
        public_and_journal = json.dumps(result) + json.dumps(self.monitor.status()) + self.monitor.journal_path.read_text(encoding="utf-8")
        self.assertNotIn(WEBHOOK, public_and_journal)
        self.assertNotIn("private secret", public_and_journal)

    def test_crashed_inflight_attempt_is_not_replayed(self):
        self.monitor.tick(self.snapshot)
        self.snapshot["accepted"].append(self.record("T002"))
        with patch.object(self.monitor, "_deliver"):
            self.monitor.tick(self.snapshot)
        journal = json.loads(self.monitor.journal_path.read_text(encoding="utf-8"))
        next(iter(journal["events"].values()))["state"] = "sending"
        self.monitor.journal_path.write_text(json.dumps(journal), encoding="utf-8")
        result = notify.NotificationMonitor(self.root / "config.json", self.settings).tick(self.snapshot)
        self.assertEqual(result["last_error"], "delivery_uncertain")
        self.opener.open.assert_not_called()

    def test_webhook_validation_rejects_unsafe_targets_without_exposing_them(self):
        for url in (WEBHOOK.replace("https:", "http:"), WEBHOOK.replace("open.feishu.cn", "evil.test"),
                    WEBHOOK.replace("open.feishu.cn", "open.feishu.cn.evil.test"), WEBHOOK + "?token=secret",
                    WEBHOOK + "#secret", WEBHOOK.replace("open.feishu.cn", "user@open.feishu.cn"),
                    WEBHOOK.replace("open.feishu.cn", "open.feishu.cn:443"), WEBHOOK + "/../../other", " " + WEBHOOK):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as caught:
                    notify.validate_webhook(url)
                self.assertNotIn(url, str(caught.exception))
                self.options["webhook_url"] = url
                self.write_settings()
                self.assertFalse(self.monitor.send_test(self.snapshot)["ok"])
        self.opener.open.assert_not_called()

    def test_redirect_handler_and_server_rejection_are_sanitized(self):
        redirect = notify._NoRedirect()
        self.assertIsNone(redirect.redirect_request(None, None, 302, "Moved", {}, "https://evil.test/"))
        self.opener.open.side_effect = HTTPError(WEBHOOK, 302, WEBHOOK, {}, None)
        result = self.monitor.send_test(self.snapshot)
        self.assertEqual(result["last_error"], "redirect_rejected")
        self.assertNotIn(WEBHOOK, json.dumps(result))
        self.opener.open.side_effect = None
        self.response.read.return_value = json.dumps({"code": 19024, "msg": WEBHOOK}).encode()
        self.assertEqual(self.monitor.send_test(self.snapshot)["last_error"], "bot_rejected")

    def test_invalid_server_body_cannot_report_success(self):
        for raw in (b'{}', b'[]', b'{"code":false}', b'{"code":"0"}', b'<html>no</html>', b'x' * 65537):
            self.response.read.return_value = raw
            self.assertFalse(self.monitor.send_test(self.snapshot)["ok"])

    def test_allowlist_only_selected_events_and_disable_is_quiet(self):
        self.options["events"] = ["accepted"]
        self.write_settings()
        self.monitor.tick(self.snapshot)
        self.begin_window()
        self.monitor.tick(self.snapshot)
        self.opener.open.assert_not_called()
        self.options["enabled"] = False
        self.write_settings()
        self.snapshot["accepted"].append(self.record("T002"))
        self.assertEqual(self.monitor.tick(self.snapshot)["state"], "disabled")
        self.assertFalse(self.monitor.send_test(self.snapshot)["ok"])
        self.opener.open.assert_not_called()

    def test_prompt_source_reasoning_and_private_url_not_in_card(self):
        self.snapshot["task"].update(body="FULL_PROMPT", acceptance_criteria="RAW_SOURCE", title="Normal")
        record = self.record("T002")
        record["review_summary"] = "<think>PRIVATE_REASONING</think>```python\nRAW_SOURCE\n``` api_key=sk-123456789 " + WEBHOOK + " 检查通过"
        payload = notify.build_card(self.snapshot, "accepted", accepted=record)
        body = json.dumps(payload, ensure_ascii=False)
        for secret in ("PRIVATE_REASONING", "RAW_SOURCE", "FULL_PROMPT", "sk-123456789", WEBHOOK):
            self.assertNotIn(secret, body)
        self.assertIn("检查通过", body)

    def test_corrupt_journal_fails_closed_and_does_not_send(self):
        self.monitor.journal_path.write_text("not JSON " + WEBHOOK, encoding="utf-8")
        result = self.monitor.tick(self.snapshot)
        self.assertEqual(result["last_error"], "notification_state_unavailable")
        self.assertNotIn(WEBHOOK, json.dumps(result))
        self.opener.open.assert_not_called()

    def test_failed_journal_reservation_prevents_network_send(self):
        with patch.object(notify, "write_json", side_effect=OSError("sensitive path " + WEBHOOK)):
            result = self.monitor.send_test(self.snapshot)
        self.assertFalse(result["ok"])
        self.assertNotIn(WEBHOOK, json.dumps(result))
        self.opener.open.assert_not_called()

    def test_missing_settings_is_disabled_and_does_not_create_journal(self):
        self.settings.unlink()
        self.assertEqual(self.monitor.status()["state"], "disabled")
        self.assertEqual(self.monitor.tick(self.snapshot)["state"], "disabled")
        self.assertFalse(self.monitor.journal_path.exists())
        self.opener.open.assert_not_called()

    def test_symlink_journal_rejected_without_touching_target(self):
        target = self.root / "other.json"
        target.write_text("untouched", encoding="utf-8")
        try:
            self.monitor.journal_path.symlink_to(target)
        except OSError:
            self.skipTest("Creating symlinks is not permitted on this Windows host")
        self.assertEqual(self.monitor.tick(self.snapshot)["state"], "error")
        self.assertEqual(target.read_text(), "untouched")
        self.opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
