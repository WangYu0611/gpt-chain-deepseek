"""The dashboard reads only local evidence, with bounded and filtered logs."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unattended import dashboard_state as dashboard
from unattended.background import OWNER
from unattended.config import load_config


class DashboardStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.config_path = self.root / "config.json"
        self.config = {"version": 1, "project_path": str(self.root / "project"), "runtime_dir": str(self.runtime),
                       "goal": "A test", "branch": "unattended/test", "codex": {"model": "codex-test"}, "dsh": {},
                       "schedule": {}, "checks": [{"id": "ue-behavior", "argv": ["python", "check.py"]}]}
        self.write(self.config_path, self.config)
        config = load_config(self.config_path)
        values = {key: config.get(key) for key in ("project_path", "goal", "branch", "base_ref", "checks", "push", "remote", "task_prefix")}
        self.state = {"version": 1, "binding": hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                      "phase": "PLAN", "task": None, "operation": None, "accepted": [], "failures": 0,
                      "updated_at": "2026-11-02T11:00:00+00:00"}
        self.now = datetime(2026, 11, 2, 11, 5, tzinfo=timezone.utc)
        self.identity = {"pid": 123, "process_created": 456, "image": "pythonw.exe"}
        self.health = {"owner": OWNER, "config": str(self.config_path.resolve()), **self.identity,
                       "heartbeat_epoch": self.now.timestamp(), "status": "running"}
        self.write(self.runtime / "background-health.json", self.health)
        self.addCleanup(patch.stopall)
        patch.object(dashboard, "_now", return_value=self.now).start()
        self.inspector = patch.object(dashboard, "process_identity", return_value=self.identity).start()
        patch("unattended.background.TaskScheduler", side_effect=AssertionError("Dashboard must not query COM")).start()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def snapshot(self):
        self.write(self.runtime / "state.json", self.state)
        return dashboard.collect_snapshot(self.config_path)

    def logs(self, rows, kind="plan"):
        directory = self.runtime / "runs" / "one"
        directory.mkdir(parents=True, exist_ok=True)
        self.state["operation"] = {"kind": kind, "output_dir": str(directory), "started_at": "2026-11-02T11:00:00+00:00"}
        (directory / "stdout.log").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        return directory

    def test_plan_without_task_and_snapshot_is_read_only(self):
        self.write(self.runtime / "state.json", self.state)
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.root.rglob("*") if path.is_file()}
        result = dashboard.collect_snapshot(self.config_path)
        after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertIsNone(result["task"])
        self.assertEqual(result["status"]["actor"], "Codex")
        self.assertEqual(result["status"]["code"], "plan")
        self.assertTrue(result["worker"]["healthy"])
        self.assertIsNone(result["error"])

    def test_codex_auth_requires_evidence_and_follows_live_subscription_transcript(self):
        self.config["codex"].update(auth_mode="api_key", subscription_fallback=True)
        self.write(self.config_path, self.config)
        opening = [{"type": "thread.started", "thread_id": "thread-api"}, {"type": "turn.started"}]
        quota = {"type": "turn.failed", "error": {"message": "Quota exceeded. Check your plan and billing details."}}
        directory = self.logs([*opening, quota])
        child = directory / "codex-subscription"
        child.mkdir()
        # A fallback schema is created before the login check, so it cannot
        # establish which route actually launched.
        self.write(child / "schema.json", {})
        auth = self.snapshot()["codex_auth"]
        self.assertEqual(auth, {"policy": "api_then_subscription", "current": None, "fallback": False})

        api = {"kind": "plan", "codex_auth_mode": "api_key", "codex_subscription_fallback": True,
               "exit_code": 1, "timed_out": False, "output_limit_exceeded": False}
        api_receipt = directory / "process.json"
        self.write(api_receipt, api)
        observed_at = self.now.timestamp()
        os.utime(api_receipt, (observed_at, observed_at))
        pending = self.snapshot()
        self.assertEqual(pending["codex_auth"]["current"], "api_key")
        self.assertFalse(pending["status"]["attempt_finished"])

        stdout = child / "stdout.log"
        stdout.write_text('{"type":"thread.started","thread_id":"thread-subscription"}\n{"type":"turn.started"}', encoding="utf-8")
        self.assertEqual(self.snapshot()["codex_auth"]["current"], "api_key")
        with stdout.open("a", encoding="utf-8") as stream:
            stream.write('\n{"type":"item.completed","item":{"type":"agent_message","text":"订阅派工进度"}}\n')
        result = self.snapshot()
        self.assertEqual(result["codex_auth"], {"policy": "api_then_subscription", "current": "chatgpt", "fallback": True})
        self.assertEqual([event["text"] for event in result["activity"]], ["开始本轮执行", "订阅派工进度"])
        self.assertFalse(result["status"]["attempt_finished"])

        child_receipt = child / "process.json"
        self.write(child_receipt, {"kind": "plan", "codex_auth_mode": "chatgpt", "exit_code": 0})
        os.utime(child_receipt, (observed_at, observed_at))
        self.assertTrue(self.snapshot()["status"]["attempt_finished"])

        self.logs([*opening, {"type": "turn.failed", "error": {"code": "rate_limit_exceeded"}}])
        result = self.snapshot()
        self.assertEqual(result["codex_auth"]["current"], "api_key")
        self.assertNotIn("订阅派工进度", json.dumps(result, ensure_ascii=False))

    def test_failed_attempt_stops_its_clock_while_checkpoint_awaits_retry(self):
        self.state.update(phase="CODE", task={"id": "TASK-1"}, failures=1, last_error="ProviderError: failed")
        directory = self.logs([], kind="code")
        receipt = directory / "process.json"
        self.write(receipt, {"kind": "code", "exit_code": 1})
        finished = self.now.timestamp() - 30
        os.utime(receipt, (finished, finished))
        status = self.snapshot()["status"]
        self.assertEqual(status["code"], "code")
        self.assertTrue(status["attempt_finished"])
        self.assertEqual(status["elapsed_seconds"], 270)

        self.write(receipt, {"kind": "review", "exit_code": 1})
        status = self.snapshot()["status"]
        self.assertFalse(status["attempt_finished"])
        self.assertEqual(status["elapsed_seconds"], 300)

    def test_blocked_review_freezes_at_final_subscription_receipt_after_worker_exit(self):
        self.config["codex"].update(auth_mode="api_key", subscription_fallback=True)
        self.write(self.config_path, self.config)
        self.state.update(phase="BLOCKED", blocked_from="REVIEW", task={"id": "TASK-1"},
                          updated_at="2026-11-02T11:00:51+00:00")
        directory = self.logs([
            {"type": "thread.started", "thread_id": "thread-api"}, {"type": "turn.started"},
            {"type": "turn.failed", "error": {"message": "Quota exceeded. Check your plan and billing details."}},
        ], kind="review")
        self.state["operation"]["task_id"] = "TASK-1"
        primary = directory / "process.json"
        final = directory / "codex-subscription" / "process.json"
        self.write(primary, {"kind": "review", "codex_auth_mode": "api_key", "codex_subscription_fallback": True,
                             "exit_code": 1, "timed_out": False, "output_limit_exceeded": False})
        self.write(final, {"kind": "review", "codex_auth_mode": "chatgpt", "exit_code": 1})
        started = datetime.fromisoformat(self.state["operation"]["started_at"]).timestamp()
        os.utime(primary, (started + 30, started + 30))
        os.utime(final, (started + 50, started + 50))
        self.inspector.side_effect = OSError("gone")
        for later in (self.now, self.now + timedelta(hours=17)):
            with self.subTest(now=later), patch.object(dashboard, "_now", return_value=later):
                result = self.snapshot()
            self.assertEqual(result["status"]["code"], "blocked")
            self.assertFalse(result["worker"]["alive"])
            self.assertEqual(result["codex_auth"]["current"], "chatgpt")
            self.assertTrue(result["status"]["attempt_finished"])
            self.assertEqual(result["status"]["elapsed_seconds"], 50)

    def test_blocked_clock_requires_current_operation_phase_task_and_receipt(self):
        self.state.update(phase="BLOCKED", blocked_from="CODE", task={"id": "TASK-1"})
        directory = self.logs([], kind="code")
        self.state["operation"]["task_id"] = "TASK-1"
        receipt = directory / "process.json"
        self.write(receipt, {"kind": "code", "exit_code": 1})
        finished = self.now.timestamp() - 30
        os.utime(receipt, (finished, finished))
        status = self.snapshot()["status"]
        self.assertTrue(status["attempt_finished"])
        self.assertEqual(status["elapsed_seconds"], 270)
        for invalid in ("wrong_phase", "foreign_task", "wrong_kind", "old_receipt", "other_run", "missing_phase"):
            with self.subTest(invalid=invalid):
                self.state["blocked_from"] = "REVIEW" if invalid == "wrong_phase" else "CODE"
                if invalid == "missing_phase":
                    self.state.pop("blocked_from")
                self.state["operation"]["task_id"] = "OTHER" if invalid == "foreign_task" else "TASK-1"
                self.state["operation"]["output_dir"] = str(directory / "another-run" if invalid == "other_run" else directory)
                self.write(receipt, {"kind": "review" if invalid == "wrong_kind" else "code", "exit_code": 1})
                end = finished - 600 if invalid == "old_receipt" else finished
                os.utime(receipt, (end, end))
                status = self.snapshot()["status"]
                self.assertEqual(status["code"], "blocked")
                self.assertFalse(status["attempt_finished"])
                self.assertIsNone(status["elapsed_seconds"])

    def test_blocked_check_uses_matching_check_and_head_evidence(self):
        for phase, kind in (("CHECK", "check"), ("MILESTONE_REVIEW", "milestone_check")):
            with self.subTest(phase=phase):
                self.state.update(phase="BLOCKED", blocked_from=phase, task={"id": "TASK-1", "check_results": []},
                                  milestone={"check_results": []})
                directory = self.logs([], kind=kind)
                self.state["operation"].update(task_id="TASK-1", head_sha="a" * 40)
                receipt = directory / "check.json"
                self.write(receipt, {"id": "ue-behavior", "head_sha": "a" * 40, "passed": False})
                finished = self.now.timestamp() - 30
                os.utime(receipt, (finished, finished))
                status = self.snapshot()["status"]
                self.assertTrue(status["attempt_finished"])
                self.assertEqual(status["elapsed_seconds"], 270)
                self.state["operation"]["head_sha"] = "b" * 40
                status = self.snapshot()["status"]
                self.assertFalse(status["attempt_finished"])
                self.assertIsNone(status["elapsed_seconds"])

    def test_stopped_operation_without_terminal_evidence_has_unknown_elapsed(self):
        self.logs([], kind="plan")
        self.inspector.side_effect = OSError("gone")
        for later in (self.now, self.now + timedelta(hours=17)):
            with self.subTest(now=later), patch.object(dashboard, "_now", return_value=later):
                status = self.snapshot()["status"]
            self.assertEqual(status["code"], "stopped")
            self.assertFalse(status["attempt_finished"])
            self.assertIsNone(status["elapsed_seconds"])

    def test_pause_blocked_done_and_dead_process(self):
        self.inspector.side_effect = OSError("gone")
        stopped = self.snapshot()["status"]
        self.assertEqual(stopped["code"], "stopped")
        self.assertEqual(stopped["actor"], "系统")
        self.assertEqual(stopped["phase"], "PLAN")
        for phase in ("BLOCKED", "DONE"):
            self.state["phase"] = phase
            self.assertEqual(self.snapshot()["status"]["code"], phase.lower())
        (self.runtime / "PAUSE").touch()
        self.assertEqual(self.snapshot()["status"]["code"], "paused")

    def test_process_identity_reuse_and_unowned_health(self):
        self.inspector.return_value = {**self.identity, "process_created": 999}
        self.assertFalse(self.snapshot()["worker"]["alive"])
        self.health["owner"] = "other"
        self.write(self.runtime / "background-health.json", self.health)
        self.inspector.reset_mock()
        self.snapshot()
        self.inspector.assert_not_called()

    def test_silent_log_does_not_mark_worker_dead_and_stale_heartbeat_does_not_either(self):
        self.logs([])
        result = self.snapshot()
        self.assertTrue(result["worker"]["alive"])
        self.assertEqual(result["status"]["code"], "plan")
        self.health["heartbeat_epoch"] -= 3600
        self.write(self.runtime / "background-health.json", self.health)
        result = self.snapshot()
        self.assertTrue(result["worker"]["alive"])
        self.assertEqual(result["status"]["code"], "heartbeat_stale")
        self.assertEqual(result["status"]["actor"], "系统")
        self.assertNotIn("正在规划", result["status"]["summary"])

    def filesystem_recovery(self):
        self.health.update(status="recovering", recovery={"kind": "filesystem_sharing", "attempt": 2,
            "next_retry_at": "2026-11-02T11:05:30+00:00", "path": "PRIVATE_PATH",
            "exception": "PRIVATE_EXCEPTION", "analysis": "PRIVATE_REASONING"}, last_error="PRIVATE_ERROR")
        self.write(self.runtime / "background-health.json", self.health)

    def test_filesystem_recovery_is_safe_waiting_without_model_execution(self):
        self.filesystem_recovery()
        self.state.update(phase="REVIEW", task={"id": "TASK-15", "reworks": 1}, failures=0,
                          last_error="PRIVATE_OLD_ERROR")
        result = self.snapshot()
        self.assertEqual(result["status"]["code"], "waiting_filesystem")
        self.assertEqual(result["status"]["label"], "等待状态文件恢复")
        self.assertEqual(result["status"]["actor"], "系统")
        self.assertIsNone(result["status"]["provider_actor"])
        self.assertIsNone(result["status"]["error"])
        self.assertTrue(result["worker"]["healthy"])
        self.assertEqual(result["worker"]["label"], "后台等待状态文件恢复")
        self.assertEqual(set(result["worker"]["recovery"]), {"kind", "attempt", "next_retry_at"})
        self.assertEqual(result["task"]["reworks"], 1)
        self.assertEqual(result["status"]["failures"], 0)
        self.assertIn("文件暂被占用，自动重试", result["status"]["summary"])
        self.assertIn("19:05:30（北京时间）", result["status"]["summary"])
        self.assertNotIn("PRIVATE_", json.dumps(result))
        self.health["recovery"]["next_retry_at"] = "2026-11-02T10:00:00+00:00"
        self.write(self.runtime / "background-health.json", self.health)
        self.assertEqual(self.snapshot()["status"]["code"], "waiting_filesystem")
        self.health.update(status="running")
        self.health.pop("recovery")
        self.write(self.runtime / "background-health.json", self.health)
        self.assertEqual(self.snapshot()["status"]["code"], "review")

    def test_filesystem_recovery_obeys_pause_terminal_worker_and_window_priority(self):
        self.filesystem_recovery()
        self.retry_record("REVIEW")
        self.assertEqual(self.snapshot()["status"]["code"], "waiting_filesystem")
        (self.runtime / "PAUSE").touch()
        self.assertEqual(self.snapshot()["status"]["code"], "paused")
        (self.runtime / "PAUSE").unlink()
        for phase in ("DONE", "BLOCKED"):
            self.state["phase"] = phase
            self.assertEqual(self.snapshot()["status"]["code"], phase.lower())
        self.state["phase"] = "REVIEW"
        self.inspector.side_effect = OSError("gone")
        self.assertEqual(self.snapshot()["status"]["code"], "stopped")
        self.inspector.side_effect = None
        self.health["heartbeat_epoch"] -= 100
        self.write(self.runtime / "background-health.json", self.health)
        self.assertEqual(self.snapshot()["status"]["code"], "heartbeat_stale")
        self.health["heartbeat_epoch"] += 100
        self.write(self.runtime / "background-health.json", self.health)
        with patch.object(dashboard, "_now", return_value=datetime(2026, 11, 2, 4, tzinfo=timezone.utc)):
            self.assertEqual(self.snapshot()["status"]["code"], "waiting_window")

    def test_filesystem_recovery_rejects_unsafe_metadata_without_exposing_details(self):
        self.filesystem_recovery()
        original = self.health["recovery"].copy()
        for key, value in (("kind", "PRIVATE_RAW_ERROR"), ("attempt", True), ("attempt", 0),
                           ("attempt", 10**12), ("next_retry_at", "2026-11-02T11:05:30"),
                           ("next_retry_at", "PRIVATE_PATH")):
            with self.subTest(key=key):
                self.health["recovery"] = {**original, key: value}
                self.write(self.runtime / "background-health.json", self.health)
                result = self.snapshot()
                self.assertIsNone(result["worker"]["recovery"])
                self.assertEqual(result["status"]["code"], "heartbeat_stale")
                self.assertNotIn("PRIVATE_", json.dumps(result))
        self.health.update(owner="other", recovery=original)
        self.write(self.runtime / "background-health.json", self.health)
        self.assertIsNone(self.snapshot()["worker"]["recovery"])

    def test_work_window_and_actor_for_each_phase(self):
        with patch.object(dashboard, "_now", return_value=datetime(2026, 11, 2, 4, tzinfo=timezone.utc)):
            result = self.snapshot()
        self.assertEqual(result["status"]["code"], "waiting_window")
        self.assertFalse(result["schedule"]["allowed"])
        self.assertEqual(result["status"]["actor"], "系统")
        self.assertEqual(result["status"]["phase"], "PLAN")
        self.assertIn(result["schedule"]["next_open"], result["status"]["summary"])
        self.assertIn("原排期", result["status"]["summary"])
        self.assertNotIn("正在规划", result["status"]["summary"])
        for phase, actor in (("CODE", "DSH"), ("CHECK", "UE/CHECK"), ("REVIEW", "Codex")):
            self.state["phase"] = phase
            self.assertEqual(self.snapshot()["status"]["actor"], actor)

    def test_pause_preserves_uncertainty_and_missing_titles_have_fallbacks(self):
        (self.runtime / "PAUSE").touch()
        self.state["task"] = {"id": "TASK-1"}
        self.state["accepted"] = [{"id": "TASK-0"}, {}]
        result = self.snapshot()
        self.assertEqual(result["status"]["actor"], "系统")
        self.assertIn("当前操作可能尚未结束", result["status"]["summary"])
        self.assertEqual(result["task"]["title"], "TASK-1")
        self.assertEqual(result["accepted"][0]["title"], "TASK-0")
        self.assertEqual(result["accepted"][1]["title"], "未命名任务")

    def test_whitelisted_events_never_expose_reasoning_or_tool_arguments(self):
        self.logs([
            {"type": "thinking", "text": "PRIVATE_REASONING"},
            {"type": "text", "channel": "analysis", "text": "PRIVATE_REASONING"},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "PRIVATE_REASONING"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "公开进度 <think>PRIVATE_REASONING</think> API_KEY=abc-secret"}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "echo COMMAND_SECRET", "aggregated_output": "RAW_OUTPUT", "exit_code": 0}},
            {"type": "tool_call", "tool": "pwsh", "input": "COMMAND_SECRET"},
            {"type": "text", "text": "DSH progress sk-abcdefgh12345"},
            {"type": "status", "phase": "step_start", "message": "PRIVATE_REASONING"},
            {"type": "final", "text": '{"large":"RAW_JSON"}'},
        ])
        result = self.snapshot()
        text = json.dumps(result["activity"], ensure_ascii=False)
        for secret in ("PRIVATE_REASONING", "COMMAND_SECRET", "RAW_OUTPUT", "abc-secret", "sk-abcdefgh12345", "RAW_JSON"):
            self.assertNotIn(secret, text)
        self.assertIn("公开进度", text)
        self.assertIn("pwsh", text)
        self.assertIn("退出码 0", text)
        self.assertEqual(len(result["activity"]), 4)

    def test_tool_summaries_show_basename_and_safe_description_only(self):
        self.logs([
            {"type": "tool_call", "tool": "read", "input": {"file_path": "C:\\PRIVATE_DIRECTORY\\ZgWorld.cpp", "content": "PRIVATE_SOURCE"}},
            {"type": "tool_call", "tool": "edit_file", "arguments": json.dumps({"file_path": "/PRIVATE_DIRECTORY/ZgSave.cpp", "description": "核对存档计时 API_KEY=PRIVATE_KEY <think>PRIVATE_REASONING</think>", "old_string": "PRIVATE_SOURCE", "new_string": "PRIVATE_SOURCE"})},
            {"type": "tool_call", "tool": "write", "input": {"file_path": "/PRIVATE_DIRECTORY/ZgTime.cpp", "content": "PRIVATE_SOURCE"}},
            {"type": "tool_call", "tool": "pwsh", "input": {"description": "核对存档计时", "command": "PRIVATE_COMMAND"}},
            {"type": "status", "phase": "step_start"}, {"type": "status", "phase": "step_end"},
            {"type": "status", "phase": "turn_start"}, {"type": "status", "phase": "turn_end"},
        ])
        result = self.snapshot()
        texts = [row["text"] for row in result["activity"]]
        self.assertEqual(texts[0], "读取文件：ZgWorld.cpp")
        self.assertTrue(texts[1].startswith("修改文件：ZgSave.cpp · 核对存档计时"))
        self.assertEqual(texts[2], "写入文件：ZgTime.cpp")
        self.assertEqual(texts[3], "调用工具：pwsh · 核对存档计时")
        self.assertEqual(texts[4:], ["开始本轮执行", "本轮执行结束"])
        for secret in ("PRIVATE_DIRECTORY", "PRIVATE_SOURCE", "PRIVATE_COMMAND", "PRIVATE_KEY", "PRIVATE_REASONING"):
            self.assertNotIn(secret, json.dumps(result["activity"]))

    def test_tool_descriptions_are_bounded_and_unstructured_arguments_ignored(self):
        self.logs([
            {"type": "tool_call", "tool": "pwsh", "input": {"description": "a" * 1000}},
            {"type": "tool_call", "tool": "pwsh", "arguments": "PRIVATE_COMMAND"},
            {"type": "tool_call", "tool": "read", "input": ["PRIVATE_SOURCE"]},
        ])
        texts = [row["text"] for row in self.snapshot()["activity"]]
        self.assertLess(len(texts[0]), 190)
        self.assertEqual(texts[1:], ["调用工具：pwsh", "调用工具：read"])

    def test_structured_messages_are_summarized_and_prefixed_credentials_redacted(self):
        self.logs([
            {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"title": "A useful task", "body": "PRIVATE_BIG_JSON"})}},
            {"type": "text", "text": "DEEPSEEK_API_KEY=MY_SECRET Authorization: Bearer ANOTHER_SECRET"},
            {"type": ["malformed"], "text": "MALFORMED"},
        ])
        result = self.snapshot()
        self.assertIsNone(result["error"])
        text = json.dumps(result["activity"])
        self.assertIn("A useful task", text)
        for secret in ("PRIVATE_BIG_JSON", "MY_SECRET", "ANOTHER_SECRET", "MALFORMED"):
            self.assertNotIn(secret, text)

    def test_accepted_details_and_burst_project_into_contract(self):
        self.state["accepted"] = [{"id": "TASK-1", "title": "Task", "head_sha": "abc", "accepted_at": self.now.isoformat(), "publication": {"local_only": True}}]
        self.write(self.runtime / "tasks/TASK-1/completed-task.json", {"reworks": 2})
        self.write(self.runtime / "tasks/TASK-1/review-2.json", {"summary": "Checks passed"})
        self.write(self.runtime / "burst.json", {"status": "completed", "completed_tasks": 3, "requested_tasks": 3, "remaining_tasks": 0})
        result = self.snapshot()
        self.assertEqual(result["accepted"][0]["review_summary"], "Checks passed")
        self.assertEqual(result["accepted"][0]["reworks"], 2)
        self.assertEqual(result["burst"]["completed_tasks"], 3)
        self.assertEqual(result["publication"]["pending_tasks"], 1)

    def test_new_stage_pending_count_preserves_old_tasks_and_manual_list(self):
        self.config["publication"] = {"mode": "milestone", "milestone_id": "M3", "goal": "M3 scope"}
        self.write(self.config_path, self.config)
        config = load_config(self.config_path)
        values = {key: config.get(key) for key in
                  ("project_path", "goal", "branch", "base_ref", "checks", "push", "remote", "task_prefix", "publication")}
        self.state["binding"] = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.state["accepted"] = [
            {"id": "OLD", "head_sha": "a" * 40, "publication": {"local_only": True, "deferred_to_milestone": "M2"}},
            {"id": "LEGACY-OLD", "head_sha": "b" * 40, "publication": {"local_only": True}},
            {"id": "NEW", "head_sha": "c" * 40, "publication": {"local_only": True, "deferred_to_milestone": "M3"}},
        ]
        self.state["milestone_history"] = [{"accepted_task_ids": ["OLD", "LEGACY-OLD"]}]
        self.state["deferred"] = [{"id": "MANUAL-M2", "status": "pending_manual_confirmation", "milestone_id": "M2"}]
        result = self.snapshot()
        self.assertIsNone(result["error"])
        self.assertEqual(result["publication"]["milestone_id"], "M3")
        self.assertEqual(result["publication"]["pending_tasks"], 1)
        self.assertEqual(len(result["accepted"]), 3)
        self.assertEqual(result["deferred"][0]["id"], "MANUAL-M2")

    def test_tail_discards_partial_json_and_limits_read(self):
        directory = self.logs([])
        path = directory / "stdout.log"
        path.write_bytes(b"x" * (dashboard.TAIL_BYTES * 3) + b'\n{"type":"text","text":"recent"}\n{"type":"text","text":"partial')
        original_open = Path.open
        reads = []
        class Reader:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def __getattr__(self, key):
                return getattr(self.stream, key)
            def read(self, count=-1):
                reads.append(count)
                return self.stream.read(count)
        def tracked_open(target, *args, **kwargs):
            opened = original_open(target, *args, **kwargs)
            return Reader(opened) if target == path else opened
        with patch.object(Path, "open", tracked_open):
            result = self.snapshot()
        self.assertEqual(reads, [dashboard.TAIL_BYTES])
        self.assertEqual([row["text"] for row in result["activity"]], ["recent"])

    def test_task_session_ownership_and_legacy_fields(self):
        self.state["task"] = {"id": "TASK-1", "session_id": "dsh-current"}
        for fields in ({}, {"session_owner": "TASK-1"},
                       {"session_owner_task_id": "TASK-1", "session_owner": "TASK-0"}):
            self.state["task"] = {"id": "TASK-1", "session_id": "dsh-current", **fields}
            task = self.snapshot()["task"]
            self.assertEqual(task["session_id"], "dsh-current")
            self.assertEqual(task["session_owner_task_id"], "TASK-1")
        self.state["task"]["session_owner_task_id"] = "TASK-0"
        self.assertIsNone(self.snapshot()["task"]["session_id"])
        self.state["task"] = {"id": "TASK-2", "session_history": [{"session_id": "dsh-old"}]}
        self.state["session_id"] = "dsh-old"
        self.assertIsNone(self.snapshot()["task"]["session_id"])

    def select_claude(self):
        cli = self.root / "claude.exe"
        cli.touch()
        self.config.update(coding_provider="claude", claude={"command": [str(cli)]})
        self.write(self.config_path, self.config)

    def test_claude_activity_is_bounded_and_session_comes_from_owned_init_record(self):
        self.select_claude()
        session = "18d2529d-649c-4ac1-9d45-3c9377291694"
        self.state.update(phase="CODE", task={"id": "TASK-1", "coding_provider": "claude"})
        self.logs([
            {"type": "system", "subtype": "init", "session_id": session, "model": "PRIVATE_MODEL", "cwd": "PRIVATE_DIRECTORY"},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "PRIVATE_REASONING"},
                {"type": "text", "text": "PRIVATE_TEXT"}, {"type": "tool_use", "name": "Read",
                  "input": {"file_path": "PRIVATE_PATH", "description": "PRIVATE_DESCRIPTION"}}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "PRIVATE_TOOL"}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "PRIVATE_SOURCE"}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "PRIVATE_RESULT"},
        ], kind="code")
        self.state["operation"].update(task_id="TASK-1", coding_provider="claude")
        result = self.snapshot()
        self.assertIsNone(result["error"])
        self.assertEqual(result["status"]["actor"], "Claude Code")
        self.assertEqual(set(result["models"]), {"codex", "claude"})
        self.assertEqual(result["task"]["session_provider"], "Claude Code")
        self.assertEqual(result["task"]["session_id"], session)
        self.assertEqual([row["text"] for row in result["activity"]],
                         ["开始本轮执行", "工具调用 · 读取文件", "调用工具", "收到工具结果", "本轮执行结束"])
        self.assertNotIn("PRIVATE_", json.dumps(result))
        self.state["operation"]["task_id"] = "OTHER-TASK"
        self.assertIsNone(self.snapshot()["task"]["session_id"])
        self.state["task"]["session_id"] = "session-retired-dsh-id"
        self.assertIsNone(self.snapshot()["task"]["session_id"])

    def test_active_session_reads_only_bounded_opening_record(self):
        self.state.update(phase="CODE", task={"id": "TASK-1", "session_id": None})
        directory = self.logs([{"type": "session",
                                "sessionId": "session-5ccb292f-39ab-41fb-b242-42c02de92693",
                                "cwd": "C:\\codex-dsh-work\\zhuguo-city\\workspace",
                                "extra": "PRIVATE_PROTOCOL_METADATA"}], kind="code")
        self.state["operation"]["task_id"] = "TASK-1"
        path = directory / "stdout.log"
        with path.open("ab") as stream:
            stream.write(b'{"type":"thinking","text":"PRIVATE_REASONING"}\n' * 10000)
        original_open = Path.open
        reads = []
        class Reader:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def read(self, count=-1):
                reads.append(count)
                return self.stream.read(count)
        def tracked_open(target, *args, **kwargs):
            opened = original_open(target, *args, **kwargs)
            return Reader(opened) if target == path else opened
        with patch.object(Path, "open", tracked_open), patch.object(dashboard, "_activity", return_value=([], None)):
            result = self.snapshot()
        self.assertEqual(reads, [8192])
        self.assertEqual(result["task"]["session_id"], "session-5ccb292f-39ab-41fb-b242-42c02de92693")
        self.assertEqual(result["task"]["session_owner_task_id"], "TASK-1")
        self.assertNotIn("PRIVATE_REASONING", json.dumps(result))
        self.assertNotIn("PRIVATE_PROTOCOL_METADATA", json.dumps(result))
        self.assertNotIn("codex-dsh-work", json.dumps(result))

    def test_session_header_requires_current_operation_and_complete_first_record(self):
        self.state.update(phase="CODE", task={"id": "TASK-1"})
        directory = self.logs([{"sessionId": "dsh-live", "type": "session"}], kind="code")
        for owner in (None, "TASK-0"):
            self.state["operation"]["task_id"] = owner
            self.assertIsNone(self.snapshot()["task"]["session_id"])
        self.state["operation"]["task_id"] = "TASK-1"
        self.assertEqual(self.snapshot()["task"]["session_id"], "dsh-live")
        for header in (b'{"type":"session","sessionId":"partial"}',
                       b'\n' * 8192 + b'{"type":"session","sessionId":"too-late"}\n',
                       b'{"type":"thinking","text":"private"}\n{"type":"session","sessionId":"later"}\n'):
            (directory / "stdout.log").write_bytes(header)
            self.assertIsNone(self.snapshot()["task"]["session_id"])
        self.logs([{"type": "session", "sessionId": "dsh-live"}], kind="review")
        self.state["operation"]["task_id"] = "TASK-1"
        self.assertIsNone(self.snapshot()["task"]["session_id"])

    def test_session_header_cannot_escape_managed_runs(self):
        self.state.update(phase="CODE", task={"id": "TASK-1"})
        self.state["operation"] = {"kind": "code", "task_id": "TASK-1", "output_dir": str(self.root)}
        (self.root / "stdout.log").write_text('{"type":"session","sessionId":"outside-session"}\n')
        result = self.snapshot()
        self.assertEqual(result["status"]["code"], "error")
        self.assertNotIn("outside-session", json.dumps(result))

    def test_path_escape_and_symlink_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "stdout.log").write_text('{"type":"text","text":"OUTSIDE_SECRET"}\n')
        self.state["operation"] = {"kind": "plan", "output_dir": str(outside)}
        result = self.snapshot()
        self.assertEqual(result["status"]["code"], "error")
        self.assertNotIn("OUTSIDE_SECRET", json.dumps(result))
        runs = self.runtime / "runs"
        runs.mkdir()
        link = runs / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            # Windows without symlink privilege: exercise reparse rejection explicitly.
            with patch.object(dashboard, "_linked", side_effect=lambda path: path == link):
                self.state["operation"]["output_dir"] = str(link)
                self.assertEqual(self.snapshot()["status"]["code"], "error")
        else:
            self.state["operation"]["output_dir"] = str(link)
            self.assertEqual(self.snapshot()["status"]["code"], "error")

    def test_check_log_has_only_summary(self):
        directory = self.logs([], kind="check")
        self.state["phase"] = "CHECK"
        (directory / "stdout.log").write_text("LogNoise: UE_INTERNAL_DATA\n" * 10000, encoding="utf-8")
        result = self.snapshot()
        self.assertEqual(len(result["activity"]), 1)
        self.assertIn("ue-behavior", result["activity"][0]["text"])
        self.assertNotIn("UE_INTERNAL_DATA", json.dumps(result))

    def test_check_progress_reports_active_operation_and_build_gate_skip(self):
        self.state.update(phase="CHECK", task={"id": "TASK-1", "check_results": []})
        self.assertEqual(self.snapshot()["check_progress"], {"total": 1, "current_id": None})
        directory = self.logs([], kind="check")
        self.assertEqual(self.snapshot()["check_progress"], {"total": 1, "current_id": "ue-behavior"})
        self.state["operation"]["head_sha"] = "a" * 40
        receipt = directory / "check.json"
        self.write(receipt, {"id": "ue-behavior", "head_sha": "a" * 40, "passed": True})
        observed_at = self.now.timestamp()
        os.utime(receipt, (observed_at, observed_at))
        self.assertTrue(self.snapshot()["status"]["attempt_finished"])
        self.state["operation"]["task_id"] = "TASK-OTHER"
        unrelated = self.snapshot()
        self.assertIsNone(unrelated["check_progress"]["current_id"])
        self.assertFalse(unrelated["status"]["attempt_finished"])

        self.state.update(phase="REVIEW", operation=None)
        self.state["task"]["check_results"] = [
            {"id": "ue-behavior", "passed": False, "status": "NOT_RUN", "reason": "editor_build_failed"},
        ]
        result = self.snapshot()
        self.assertEqual(result["check_progress"], {"total": 1, "current_id": None})
        self.assertEqual(result["checks"], [{"id": "ue-behavior", "passed": False, "exit_code": None, "status": "not_run"}])

    def test_corrupt_state_binding_and_missing_state(self):
        result = dashboard.collect_snapshot(self.config_path)
        self.assertEqual(result["status"]["code"], "not_initialized")
        (self.runtime / "state.json").write_text("{broken")
        result = dashboard.collect_snapshot(self.config_path)
        self.assertEqual(result["status"]["code"], "error")
        self.assertIn("project", result)
        self.state["binding"] = "wrong"
        self.assertEqual(self.snapshot()["status"]["code"], "error")

    def test_deferred_is_separate_sanitized_and_does_not_block_other_development(self):
        self.state["accepted"] = [{"id": "TASK-8", "title": "已完成事项", "head_sha": "a" * 40,
                                   "publication": {"local_only": True}}]
        self.state["deferred"] = [{"id": "TASK-9", "title": "HUD 闪动 <think>PRIVATE_TITLE</think>",
            "head_sha": "b" * 40, "base_sha": "a" * 40, "deferred_at": self.now.isoformat(),
            "reason": "等待用户手动确认；继续其他 M2 开发。 API_KEY=PRIVATE_KEY <analysis>PRIVATE_REASONING</analysis>",
            "status": "pending_manual_confirmation", "receipt_path": "PRIVATE_RECEIPT_PATH", "task": {"body": "PRIVATE_TASK"},
            "review": {"analysis": "PRIVATE_REVIEW"}, "milestone_id": "M2"}]
        result = self.snapshot()
        self.assertIsNone(result["error"])
        self.assertEqual(result["status"]["code"], "plan")
        self.assertEqual(result["accepted"][0]["id"], "TASK-8")
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(result["publication"]["pending_tasks"], 1)
        item = result["deferred"][0]
        self.assertEqual(set(item), {"id", "title", "head_sha", "base_sha", "reason", "deferred_at", "status"})
        self.assertEqual(item["status"], "pending_manual_confirmation")
        self.assertIn("等待用户手动确认", item["reason"])
        self.assertEqual(item["deferred_at"], self.now.isoformat())
        self.assertNotIn("PRIVATE_", json.dumps(result, ensure_ascii=False))
        with patch.object(dashboard, "_now", return_value=datetime(2026, 11, 2, 4, tzinfo=timezone.utc)):
            waiting = self.snapshot()
        self.assertEqual(waiting["status"]["code"], "waiting_window")
        self.assertEqual(len(waiting["deferred"]), 1)

    def test_deferred_handles_legacy_missing_and_malformed_state(self):
        self.assertEqual(self.snapshot()["deferred"], [])
        self.state["deferred"] = [{"id": "TASK-9", "status": "PRIVATE_STATUS", "deferred_at": "invalid"}]
        item = self.snapshot()["deferred"][0]
        self.assertEqual(item["title"], "TASK-9")
        self.assertEqual(item["status"], "unknown")
        self.assertIsNone(item["deferred_at"])
        self.state["deferred"] = {"bad": "record"}
        self.assertEqual(self.snapshot()["status"]["code"], "error")

    def retry_record(self, phase="CODE"):
        self.state.update(phase=phase, task={"id": "TASK-13", "reworks": 1, "code_base": "b" * 40}
                          if phase in {"CODE", "REVIEW"} else None)
        self.state["provider_retry"] = {"phase": phase, "task_id": "TASK-13" if self.state["task"] else None,
            "head_sha": "a" * 40, "attempt": 2, "reason": "server_unavailable",
            "next_retry_at": "2026-11-02T11:10:00+00:00", "status": "waiting",
            "operation_output_dir": "PRIVATE_PATH", "raw_error": "PRIVATE_ERROR", "thinking": "PRIVATE_THINKING"}
        return self.state["provider_retry"]

    def test_provider_retry_projects_safe_details_without_failure_or_rework_changes(self):
        self.retry_record()
        self.state["last_error"] = "PRIVATE_OLD_ERROR"
        result = self.snapshot()
        self.assertIsNone(result["error"])
        self.assertIsNone(result["status"]["error"])
        self.assertEqual(result["status"]["code"], "waiting_provider")
        self.assertEqual(result["status"]["label"], "等待服务恢复")
        self.assertEqual(result["status"]["actor"], "系统")
        self.assertEqual(result["status"]["provider_actor"], "DSH")
        self.assertEqual(result["status"]["phase"], "CODE")
        self.assertEqual(result["status"]["failures"], 0)
        self.assertEqual(result["task"]["reworks"], 1)
        self.assertTrue(result["worker"]["healthy"])
        self.assertIn("第 2 次", result["status"]["summary"])
        self.assertIn("19:10:00（北京时间）", result["status"]["summary"])
        self.assertEqual(set(result["provider_retry"]), {"phase", "task_id", "attempt", "reason", "reason_label", "next_retry_at", "provider", "status"})
        self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_claude_subscription_retry_shows_exact_reset_without_exposing_error(self):
        self.select_claude()
        record = self.retry_record()
        record.update(reason="claude_subscription_limit", next_retry_at="2026-11-02T21:00:00+00:00")
        self.state["task"]["coding_provider"] = "claude"
        result = self.snapshot()
        self.assertIsNone(result["error"])
        self.assertEqual(result["status"]["code"], "waiting_provider")
        self.assertEqual(result["status"]["label"], "等待 Claude 额度恢复")
        self.assertEqual(result["status"]["provider_actor"], "Claude Code")
        self.assertEqual(result["provider_retry"]["reason_label"], "Claude Pro 额度已用完")
        self.assertIn("11-03 05:00:00", result["status"]["summary"])
        self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_codex_subscription_retry_projects_waiting_for_codex_roles(self):
        for phase in ("PLAN", "REVIEW", "MILESTONE_REVIEW"):
            with self.subTest(phase=phase):
                record = self.retry_record(phase)
                record.update(reason="codex_subscription_limit", next_retry_at="2026-11-02T21:00:00+00:00")
                result = self.snapshot()
                self.assertIsNone(result["error"])
                self.assertEqual(result["status"]["code"], "waiting_provider")
                self.assertEqual(result["status"]["label"], "等待 Codex 额度恢复")
                self.assertEqual(result["status"]["provider_actor"], "Codex")
                self.assertEqual(result["provider_retry"]["reason_label"], "Codex额度已用完")
                self.assertIn("11-03 05:00:00", result["status"]["summary"])
                self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_codex_subscription_retry_rejects_non_codex_code_and_plan(self):
        self.retry_record()["reason"] = "codex_subscription_limit"
        self.assertEqual(self.snapshot()["status"]["code"], "error")
        self.select_claude()
        for phase in ("CODE", "PLAN"):
            with self.subTest(phase=phase):
                self.retry_record(phase)["reason"] = "codex_subscription_limit"
                if phase == "CODE":
                    self.state["task"]["coding_provider"] = "claude"
                else:
                    self.logs([], kind="plan")
                    self.state["operation"]["planning_provider"] = "claude"
                result = self.snapshot()
                self.assertEqual(result["status"]["code"], "error")
                self.assertIsNone(result["provider_retry"])
                self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_provider_retry_respects_pause_terminal_worker_and_calendar_gates(self):
        self.retry_record()
        (self.runtime / "PAUSE").touch()
        self.assertEqual(self.snapshot()["status"]["code"], "paused")
        (self.runtime / "PAUSE").unlink()
        for phase in ("BLOCKED", "DONE"):
            self.state["phase"] = phase
            self.assertEqual(self.snapshot()["status"]["code"], phase.lower())
        self.state["phase"] = "CODE"
        self.inspector.side_effect = OSError("gone")
        self.assertEqual(self.snapshot()["status"]["code"], "stopped")
        self.inspector.side_effect = None
        self.health["heartbeat_epoch"] -= 100
        self.write(self.runtime / "background-health.json", self.health)
        self.assertEqual(self.snapshot()["status"]["code"], "heartbeat_stale")
        self.health["heartbeat_epoch"] += 100
        self.write(self.runtime / "background-health.json", self.health)
        with patch.object(dashboard, "_now", return_value=datetime(2026, 11, 2, 4, tzinfo=timezone.utc)):
            result = self.snapshot()
        self.assertEqual(result["status"]["code"], "waiting_window")

    def test_provider_retry_validates_reason_timestamp_counter_head_and_task_ownership(self):
        original = self.retry_record().copy()
        for key, value in (("reason", "PRIVATE_RAW_ERROR API_KEY=PRIVATE_SECRET"), ("next_retry_at", "2026-11-02T12:00:00"),
                           ("attempt", True), ("attempt", 0), ("attempt", 10**12), ("task_id", "TASK-12"),
                           ("head_sha", "not-a-commit"), ("phase", "PLAN"), ("status", "PRIVATE_STATUS")):
            with self.subTest(key=key, value=value):
                self.state["provider_retry"] = {**original, key: value}
                result = self.snapshot()
                self.assertIsNone(result["provider_retry"])
                self.assertEqual(result["status"]["code"], "error")
                self.assertNotIn("PRIVATE_", json.dumps(result))
        (self.runtime / "PAUSE").touch()
        self.assertEqual(self.snapshot()["status"]["code"], "paused")

    def test_provider_retry_expiry_does_not_invent_execution_and_retrying_can_resume(self):
        record = self.retry_record("PLAN")
        record["next_retry_at"] = "2026-11-02T10:00:00+00:00"
        result = self.snapshot()
        self.assertEqual(result["status"]["code"], "waiting_provider")
        self.assertEqual(result["provider_retry"]["provider"], "Codex")
        self.assertNotIn("正在", result["status"]["summary"])
        record["status"] = "retrying"
        self.assertEqual(self.snapshot()["status"]["code"], "plan")
        del self.state["provider_retry"]
        result = self.snapshot()
        self.assertIsNone(result["provider_retry"])
        self.assertEqual(result["status"]["code"], "plan")

    def test_provider_retry_supports_review_roles_and_each_safe_reason(self):
        for phase in ("PLAN", "CODE", "REVIEW", "MILESTONE_REVIEW"):
            for reason in ("rate_limited", "server_unavailable", "transport", "workspace_routing_network", "local_cli_unavailable"):
                with self.subTest(phase=phase, reason=reason):
                    record = self.retry_record(phase)
                    record["reason"] = reason
                    result = self.snapshot()
                    self.assertEqual(result["status"]["code"], "waiting_provider")
                    self.assertEqual(result["provider_retry"]["provider"], "DSH" if phase == "CODE" else "Codex")

    def test_local_cli_retry_has_safe_label_and_does_not_mask_pause_or_stopped_worker(self):
        for phase in ("PLAN", "REVIEW", "MILESTONE_REVIEW"):
            with self.subTest(phase=phase):
                record = self.retry_record(phase)
                record["reason"] = "local_cli_unavailable"
                result = self.snapshot()
                self.assertEqual(result["status"]["code"], "waiting_provider")
                self.assertEqual(result["status"]["label"], "等待本地 Codex 恢复")
                self.assertEqual(result["provider_retry"]["provider"], "Codex")
                self.assertEqual(result["provider_retry"]["reason_label"], "本地 Codex 正在更新或暂不可用")
                self.assertEqual(set(result["provider_retry"]), {"phase", "task_id", "attempt", "reason", "reason_label", "next_retry_at", "provider", "status"})
                self.assertNotIn("PRIVATE_", json.dumps(result))
                (self.runtime / "PAUSE").touch()
                self.assertEqual(self.snapshot()["status"]["code"], "paused")
                (self.runtime / "PAUSE").unlink()
                self.inspector.side_effect = OSError("gone")
                self.assertEqual(self.snapshot()["status"]["code"], "stopped")
                self.inspector.side_effect = None


if __name__ == "__main__":
    unittest.main()
