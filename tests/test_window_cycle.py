from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from relay.storage import write_json
from windowrelay.cycle import AutoRelay


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def observation(**changes):
    fields = dict(available=True, busy=False, idle=True, text="old idle reply",
                  fingerprint="old-idle", editable_text="", blocked=False)
    fields.update(changes)
    return SimpleNamespace(**fields)


class FakeObserver:
    def __init__(self):
        self.values = {"gpt": observation(), "dsh": observation()}
        self.calls = []

    def snapshot(self, target):
        self.calls.append(target)
        value = self.values[target]
        if isinstance(value, BaseException):
            raise value
        return value


class FakeSender:
    def __init__(self):
        self.calls = []
        self.validated = []
        self.validation_error = None
        self.error = None
        self.before_send = None

    def validate(self, target):
        self.validated.append(target)
        if self.validation_error:
            raise self.validation_error

    def send_gpt(self, message, event_id):
        self._send("gpt", message, event_id)

    def send_dsh(self, message, event_id):
        self._send("dsh", message, event_id)

    def _send(self, target, message, event_id):
        if self.before_send:
            self.before_send(target, message, event_id)
        self.calls.append((target, message, event_id))
        if self.error:
            raise self.error


class AutoRelayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name) / "isolated-cycle"
        self.config = {
            "gpt": {"url": "https://chatgpt.com/c/auto-test"},
            "dsh": {"project": "auto-test-project"},
            "messages": {"gpt": "DS做完了，请检查。", "dsh": "完成指定任务书。"},
        }
        self.clock, self.sender, self.observer = FakeClock(), FakeSender(), FakeObserver()
        self.relay = self.new_relay()

    def new_relay(self):
        return AutoRelay(self.config, self.runtime, self.sender, self.observer, self.clock)

    def state(self):
        return json.loads((self.runtime / "cycle-state.json").read_text(encoding="utf-8"))

    def send_first(self, target="gpt"):
        result = self.relay.step(execute=True, start_with=target)
        self.assertEqual((result["status"], result["target"]), ("awaiting", target))
        return self.state()["current"]

    def show_done(self, target=None, **changes):
        current = self.state()["current"]
        target = target or current["target"]
        marker = "[[RELAY_DONE:" + current["token"] + "]]"
        self.observer.values[target] = observation(text="本轮工作已完成。\n" + marker,
                                                  fingerprint="finished-" + current["token"], **changes)
        return marker

    def finish_current(self):
        self.show_done()
        self.assertEqual(self.relay.step(execute=True)["reason_code"], "settling")
        self.clock.advance(3)
        self.assertEqual(self.relay.step(execute=True)["reason_code"], "settling")
        self.clock.advance(3)
        return self.relay.step(execute=True)

    def test_dry_run_and_empty_status_have_no_file_sender_or_observer_side_effects(self):
        self.assertIsNone(self.relay.status()["state"])
        result = self.relay.step()
        self.assertEqual((result["status"], result["target"]), ("dry_run", "gpt"))
        self.assertIn("DS做完了，请检查。", result["message"])
        self.assertFalse(self.runtime.exists())
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(self.sender.validated, [])
        self.assertEqual(self.observer.calls, [])

    def test_task_titles_start_with_number_and_commit_titles_contain_only_number(self):
        for target in ("gpt", "dsh"):
            message = self.relay.step(execute=False, start_with=target)["message"]
            self.assertIn("正文第一行", message)
            self.assertIn("commit 标题", message)
            self.assertIn("commit 标题必须仅为实际任务编号本身", message)
            self.assertIn("不附带 task+、说明文字或其他前后缀", message)
            self.assertIn("直接以", message)
            self.assertIn("实际任务编号开头", message)
            self.assertIn("TASK-M2-20", message)
            self.assertIn("不要添加 task+ 或其他额外前缀", message)
            self.assertNotIn("task+实际任务编号", message)
            self.assertNotIn("task+TASK-", message)
        gpt = self.relay._prompt("gpt", "format-check")
        self.assertIn("TASK-M2-20 下一任务：具体内容", gpt)
        self.assertIn("新任务书和修正任务书的标题文字", gpt)
        self.assertIn("不得照抄示例编号", gpt)

    def test_obsolete_task_prefix_setting_cannot_prepend_any_task_prefix(self):
        self.config["auto"] = {"task_label_prefix": "job+"}
        for target in ("gpt", "dsh"):
            message = self.new_relay()._prompt(target, "same-nonce")
            self.assertNotIn("job+", message)
            self.assertNotIn("task+TASK-", message)
            self.assertIn("本轮识别码：same-nonce", message)
            self.assertIn("RELAY_DONE:", message)
            self.assertIn("RELAY_BLOCKED:", message)
            self.assertNotIn("[[RELAY_DONE:same-nonce]]", message)
            self.assertNotIn("[[RELAY_BLOCKED:same-nonce]]", message)

    def test_role_prompts_split_markers_and_pending_is_durable_before_send(self):
        def verify(target, message, event_id):
            pending = self.state()["pending"]
            self.assertEqual((pending["target"], pending["event_id"], pending["message"]),
                             (target, event_id, message))
            self.assertEqual(self.state()["phase"], "sending")
            self.assertNotIn("[[RELAY_DONE:" + pending["token"] + "]]", message)
            self.assertNotIn("[[RELAY_BLOCKED:" + pending["token"] + "]]", message)
            self.assertIn("左双中括号", message)
            self.assertIn("RELAY_DONE:", message)
            self.assertIn("项目已经全部完成", message)
            self.assertIn("发布到项目仓库", message)
            self.assertIn("执行方拉取任务书后必须能立即开始", message)

        self.sender.before_send = verify
        self.send_first()
        self.assertEqual(self.sender.validated, ["gpt"])
        self.assertIsNone(self.state()["pending"])
        self.assertIsNone(self.state()["current"]["server_received"])

    def test_full_alternation_waits_for_own_turn_marker_and_defers_next_send(self):
        self.send_first()
        result = self.finish_current()
        self.assertEqual((result["status"], result["next_target"]), ("completed", "dsh"))
        self.assertEqual([call[0] for call in self.sender.calls], ["gpt"])
        self.assertTrue(all(target == "gpt" for target in self.observer.calls))
        self.assertEqual(self.relay.step(execute=True)["status"], "awaiting")
        self.assertIn("全部上传到项目仓库", self.sender.calls[1][1])
        self.assertIn("不要自行开始下一任务", self.sender.calls[1][1])
        second_result = self.finish_current()
        self.assertEqual((second_result["status"], second_result["next_target"]), ("completed", "gpt"))
        self.assertEqual([call[0] for call in self.sender.calls], ["gpt", "dsh"])
        self.assertNotEqual(self.sender.calls[0][2], self.sender.calls[1][2])
        self.assertEqual(self.state()["completed_turns"], 2)

    def test_old_idle_reply_and_action_success_never_count_as_completion(self):
        self.send_first()
        self.clock.advance(20)
        for _ in range(4):
            result = self.relay.step(execute=True)
            self.assertEqual((result["status"], result["reason_code"]), ("waiting", "completion_missing"))
        self.assertEqual(self.state()["completed_turns"], 0)
        self.assertEqual([call[0] for call in self.sender.calls], ["gpt"])

    def test_send_action_and_ui_request_echo_are_reported_separately(self):
        result = self.relay.step(execute=True)
        self.assertEqual((result["status"], result["request_status"]), ("awaiting", "not_yet_observed"))
        current = self.state()["current"]
        self.observer.values["gpt"] = observation(text="You said: 本轮识别码：" + current["token"], fingerprint="user-echo")
        result = self.relay.step(execute=True)
        self.assertEqual((result["status"], result["request_status"]), ("waiting", "ui_echo_observed"))
        self.assertFalse(result["response_seen"])
        self.assertIsNone(result["server_received"])
        self.assertEqual(self.state()["current"]["ui_request_echo_seen_at"], self.clock.now)
        self.assertEqual(len(self.sender.calls), 1)

    def test_request_echo_and_busy_evidence_survive_restart_and_disappearing_user_bubble(self):
        current = self.send_first()
        self.observer.values["gpt"] = observation(
            text="You said: " + current["token"], busy=True, idle=False, fingerprint="request-working")
        result = self.relay.step(execute=True)
        self.assertEqual((result["reason_code"], result["request_status"]), ("busy", "ui_echo_observed"))
        observed = self.state()["current"]
        self.assertEqual(observed["busy_seen_at"], self.clock.now)
        restarted = self.new_relay()
        self.observer.values["gpt"] = observation(text="visible reply without user bubble")
        self.clock.advance(61)
        result = restarted.step(execute=True)
        self.assertEqual((result["reason_code"], result["request_status"]), ("completion_missing", "ui_echo_observed"))
        self.assertEqual(self.state()["current"]["ui_request_echo_seen_at"], observed["ui_request_echo_seen_at"])
        self.assertEqual(len(self.sender.calls), 1)

    def test_old_idle_reply_without_echo_warns_but_never_resends_or_pauses(self):
        self.send_first()
        self.clock.advance(60)
        for _ in range(3):
            result = self.relay.step(execute=True)
            self.assertEqual((result["status"], result["reason_code"], result["request_status"]),
                             ("waiting", "request_not_confirmed", "not_yet_observed"))
            self.assertIn("核对", result["reason"])
            self.assertEqual(self.state()["phase"], "awaiting")
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(self.state()["completed_turns"], 0)

    def test_busy_without_current_nonce_is_only_unlinked_busy_evidence(self):
        self.send_first()
        self.observer.values["gpt"] = observation(busy=True, idle=False, text="old job still working")
        result = self.relay.step(execute=True)
        self.assertEqual((result["reason_code"], result["request_status"]), ("busy_unlinked", "not_yet_observed"))
        self.assertEqual(self.state()["current"]["busy_seen_at"], self.clock.now)
        self.assertIsNone(self.state()["current"]["ui_request_echo_seen_at"])
        self.assertEqual(len(self.sender.calls), 1)

    def test_nonce_in_draft_or_unavailable_text_is_not_request_echo(self):
        current = self.send_first()
        for changes in ({"editable_text": current["token"], "draft_present": True, "idle": False},
                        {"available": False}):
            self.observer.values["gpt"] = observation(text=current["token"], **changes)
            result = self.relay.step(execute=True)
            self.assertEqual(result["request_status"], "not_yet_observed")
            self.assertIsNone(self.state()["current"]["ui_request_echo_seen_at"])
        self.assertEqual(len(self.sender.calls), 1)

    def test_legacy_current_without_ack_fields_remains_compatible_and_can_confirm_echo(self):
        current = self.send_first()
        state = self.state()
        for key in ("ui_request_echo_seen_at", "busy_seen_at", "response_seen_at", "response_evidence"):
            state["current"].pop(key, None)
        write_json(self.runtime / "cycle-state.json", state)
        self.observer.values["gpt"] = observation(text="本轮识别码：" + current["token"])
        result = self.new_relay().step(execute=True)
        self.assertEqual(result["request_status"], "ui_echo_observed")
        self.assertEqual(len(self.sender.calls), 1)

    def test_done_and_blocked_markers_are_persisted_as_response_evidence(self):
        self.send_first()
        self.show_done()
        result = self.relay.step(execute=True)
        self.assertTrue(result["response_seen"])
        self.assertEqual(self.state()["current"]["response_evidence"], "done_marker")
        current = self.state()["current"]
        self.observer.values["gpt"] = observation(text="[[RELAY_BLOCKED:" + current["token"] + "]]")
        result = self.relay.step(execute=True)
        self.assertEqual((result["status"], result["reason_code"]), ("paused", "blocked_marker"))
        self.assertTrue(result["response_seen"])
        self.assertEqual(self.state()["current"]["response_evidence"], "blocked_marker")

    def test_busy_unknown_drafts_and_unconfirmed_idle_prevent_initial_input(self):
        for fields, code in (({"busy": True}, "busy"), ({"busy": None}, "busy_unknown"),
                             ({"idle": False}, "not_idle"), ({"editable_text": "user draft"}, "draft_present")):
            with self.subTest(fields=fields):
                self.observer.values["gpt"] = observation(**fields)
                result = self.relay.step(execute=True)
                self.assertEqual((result["status"], result["reason_code"]), ("waiting", code))
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(self.sender.validated, [])
        self.assertIsNone(self.state()["pending"])

    def test_busy_unknown_draft_or_nonidle_resets_completion_stability(self):
        self.send_first()
        marker = self.show_done()
        self.relay.step(execute=True)
        self.assertEqual(self.state()["stability"]["samples"], 1)
        for changes, code in (({"busy": True}, "busy"), ({"busy": None}, "busy_unknown"),
                              ({"idle": False}, "not_idle"), ({"editable_text": "draft"}, "draft_present")):
            self.observer.values["gpt"] = observation(text=marker, fingerprint="done", **changes)
            self.assertEqual(self.relay.step(execute=True)["reason_code"], code)
            self.assertEqual(self.state()["stability"]["samples"], 0)
        self.assertEqual(len(self.sender.calls), 1)

    def test_wrong_nonce_or_inline_marker_never_completes(self):
        current = self.send_first()
        marker = "[[RELAY_DONE:" + current["token"] + "]]"
        for text in ("[[RELAY_DONE:old-turn]]", "quoted " + marker, marker + " extra"):
            with self.subTest(text=text):
                self.observer.values["gpt"] = observation(text=text, fingerprint="same")
                self.clock.advance(20)
                self.assertEqual(self.relay.step(execute=True)["reason_code"], "completion_missing")
        self.assertEqual(len(self.sender.calls), 1)

    def test_uia_parent_and_child_representations_of_same_marker_can_complete(self):
        current = self.send_first()
        marker = "[[RELAY_DONE:" + current["token"] + "]]"
        # One response can be exposed both through Document.TextPattern and a child Text.Name.
        text = "Document visible range: 本轮已完成。\n" + marker + "\nText child:\n" + marker
        self.observer.values["gpt"] = observation(text=text, fingerprint="stable-parent-and-child")
        self.assertEqual(self.relay.step(execute=True)["reason_code"], "settling")
        self.clock.advance(3)
        self.assertEqual(self.relay.step(execute=True)["reason_code"], "settling")
        self.clock.advance(3)
        self.assertEqual(self.relay.step(execute=True)["status"], "completed")
        self.assertEqual(len(self.sender.calls), 1)

    def test_observer_reason_is_logged_without_response_text(self):
        self.observer.values["gpt"] = observation(
            available=False, reason="UIA editor is not exposed", text="private raw response content")
        result = self.relay.step(execute=True)
        self.assertIn("UIA editor is not exposed", result["reason"])
        self.assertEqual(result["observer_reason"], "UIA editor is not exposed")
        self.assertNotIn("private raw response content", json.dumps(result))
        self.observer.values["gpt"] = RuntimeError("reader disconnected")
        self.assertIn("reader disconnected", self.relay.step(execute=True)["reason"])

    def test_changed_fingerprint_resets_sample_count_and_settle_timer(self):
        self.send_first()
        self.show_done()
        self.relay.step(execute=True)
        self.clock.advance(3)
        self.relay.step(execute=True)
        self.observer.values["gpt"].fingerprint = "new-response-revision"
        self.clock.advance(3)
        self.assertEqual(self.relay.step(execute=True)["reason_code"], "settling")
        self.assertEqual(self.state()["stability"]["samples"], 1)
        self.clock.advance(3)
        self.assertEqual(self.relay.step(execute=True)["reason_code"], "settling")
        self.clock.advance(3)
        self.assertEqual(self.relay.step(execute=True)["status"], "completed")

    def test_sample_count_alone_cannot_bypass_settle_seconds(self):
        self.send_first()
        self.show_done()
        for _ in range(4):
            self.assertEqual(self.relay.step(execute=True)["reason_code"], "settling")
        self.assertEqual(self.state()["stability"]["samples"], 4)
        self.clock.advance(5)
        self.assertEqual(self.relay.step(execute=True)["status"], "completed")
        self.assertEqual(len(self.sender.calls), 1)

    def test_readability_recovery_resets_unavailable_timeout(self):
        self.send_first()
        self.observer.values["gpt"] = observation(available=False)
        self.relay.step(execute=True)
        self.clock.advance(59)
        self.observer.values["gpt"] = observation()
        self.assertEqual(self.relay.step(execute=True)["reason_code"], "completion_missing")
        self.assertIsNone(self.state()["unavailable_since"])
        self.observer.values["gpt"] = observation(available=False)
        self.relay.step(execute=True)
        self.clock.advance(59)
        self.assertEqual(self.relay.step(execute=True)["reason_code"], "unavailable")

    def test_restarting_awaiting_keeps_token_without_resend_and_resets_stability(self):
        current = self.send_first()
        self.show_done()
        self.relay.step(execute=True)
        self.clock.advance(3)
        self.relay.step(execute=True)
        restarted = self.new_relay()
        self.clock.advance(3)
        self.assertEqual(restarted.step(execute=True)["reason_code"], "settling")
        self.assertEqual(self.state()["stability"]["samples"], 1)
        self.assertEqual(self.state()["current"]["token"], current["token"])
        self.clock.advance(3)
        restarted.step(execute=True)
        self.clock.advance(3)
        self.assertEqual(restarted.step(execute=True)["status"], "completed")
        self.assertEqual(len(self.sender.calls), 1)

    def test_phase_timeout_also_applies_before_initial_send(self):
        self.config["auto"] = {"phase_timeout_seconds": 10}
        relay = self.new_relay()
        self.observer.values["gpt"] = observation(busy=True)
        relay.step(execute=True)
        self.clock.advance(10)
        result = relay.step(execute=True)
        self.assertEqual((result["status"], result["reason_code"]), ("paused", "phase_timeout"))
        self.assertEqual(self.sender.calls, [])

    def test_unavailable_timeout_and_observer_exception_pause_without_handoff(self):
        self.send_first()
        self.observer.values["gpt"] = RuntimeError("reader disconnected")
        self.assertEqual(self.relay.step(execute=True)["reason_code"], "unavailable")
        self.clock.advance(59)
        self.assertEqual(self.relay.step(execute=True)["status"], "waiting")
        self.clock.advance(1)
        result = self.relay.step(execute=True)
        self.assertEqual((result["status"], result["reason_code"]), ("paused", "unavailable_timeout"))
        self.assertEqual(len(self.sender.calls), 1)

    def test_blocked_marker_or_approval_pauses_and_never_hands_off(self):
        current = self.send_first()
        self.observer.values["gpt"] = observation(text="[[RELAY_BLOCKED:" + current["token"] + "]]")
        result = self.relay.step(execute=True)
        self.assertEqual((result["status"], result["reason_code"]), ("paused", "blocked_marker"))
        self.assertEqual(self.relay.step(execute=True)["status"], "paused")
        self.relay.reset()
        self.observer.values["gpt"] = observation(blocked=True)
        result = self.relay.step(execute=True)
        self.assertEqual((result["status"], result["reason_code"]), ("paused", "approval_required"))
        self.assertEqual(len(self.sender.calls), 1)

    def test_real_draft_before_first_send_waits_without_permanent_pause(self):
        draft = "private unsent user draft"
        self.observer.values["gpt"] = observation(
            available=True, busy=False, idle=False, editable_text=draft, draft_present=True,
            blocked=False, blocked_controls=(), reason="消息编辑框含草稿。", reason_code="draft_present",
            editor_source="ValuePattern")
        for _ in range(3):
            result = self.relay.step(execute=True)
            self.assertEqual((result["status"], result["reason_code"], result["phase"]),
                             ("waiting", "draft_present", "ready"))
            self.assertEqual(result["observer_reason_code"], "draft_present")
            self.assertEqual(result["editor_source"], "ValuePattern")
            self.assertTrue(result["draft_present"])
            self.assertEqual(result["blocked_controls"], [])
            self.assertNotIn(draft, json.dumps(result, ensure_ascii=False))
        self.assertIsNone(self.state()["pending"])
        self.assertIsNone(self.state()["current"])
        self.assertEqual(self.sender.calls, [])
        self.observer.values["gpt"] = observation()
        self.assertEqual(self.relay.step(execute=True)["status"], "awaiting")
        self.assertEqual(len(self.sender.calls), 1)

    def test_approval_pause_retains_exact_reason_and_controls_after_restart(self):
        reason = "匹配到已启用的审批控件：批准一次"
        self.observer.values["gpt"] = observation(
            blocked=True, blocked_controls=("批准一次",), reason=reason,
            reason_code="approval_required", text="private response text", editor_source="TextPattern")
        result = self.relay.step(execute=True)
        self.assertEqual((result["status"], result["reason_code"]), ("paused", "approval_required"))
        self.assertEqual(result["observer_reason"], reason)
        self.assertEqual(result["blocked_controls"], ["批准一次"])
        self.assertEqual(result["observer_reason_code"], "approval_required")
        self.assertNotIn("private response text", json.dumps(result))
        paused = self.state()
        self.assertEqual(paused["pause_observer_reason"], reason)
        self.assertEqual(paused["pause_diagnostic"]["blocked_controls"], ["批准一次"])
        restarted = self.new_relay()
        result = restarted.step(execute=True)
        self.assertEqual(result["observer_reason"], reason)
        self.assertEqual(result["blocked_controls"], ["批准一次"])
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(self.observer.calls, ["gpt"])

    def test_old_ambiguous_approval_pause_does_not_automatically_continue(self):
        self.observer.values["gpt"] = observation(busy=True)
        self.relay.step(execute=True)
        state = self.state()
        state.update(phase="paused", pause_code="approval_or_blocked", pause_reason="old ambiguous pause")
        write_json(self.runtime / "cycle-state.json", state)
        self.observer.values["gpt"] = observation()
        result = self.relay.step(execute=True)
        self.assertEqual((result["status"], result["reason_code"]), ("paused", "approval_or_blocked"))
        with self.assertRaisesRegex(ValueError, "reset-cycle"):
            self.relay.resume()
        self.assertEqual(self.sender.calls, [])

    def test_validation_failure_creates_no_pending_or_attempt(self):
        self.sender.validation_error = RuntimeError("calibration required")
        with self.assertRaisesRegex(RuntimeError, "calibration required"):
            self.relay.step(execute=True)
        self.assertFalse((self.runtime / "cycle-state.json").exists())
        self.assertEqual(self.sender.calls, [])

    def test_send_failure_and_keyboard_interrupt_are_durable_and_never_retried(self):
        for error in (RuntimeError("send clicked but window vanished"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                runtime = self.runtime / type(error).__name__
                sender = FakeSender()
                sender.error = error
                relay = AutoRelay(self.config, runtime, sender, self.observer, self.clock)
                if isinstance(error, KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        relay.step(execute=True)
                else:
                    self.assertEqual(relay.step(execute=True)["status"], "uncertain")
                restarted = AutoRelay(self.config, runtime, sender, self.observer, self.clock)
                self.assertEqual(restarted.step(execute=True)["status"], "uncertain")
                self.assertEqual(len(sender.calls), 1)
                with self.assertRaisesRegex(ValueError, "resolve"):
                    restarted.reset()

    def test_crash_pending_is_uncertain_and_resolve_sent_waits_original_target(self):
        current = self.send_first("dsh")
        state = self.state()
        state.update(phase="sending", pending=dict(current))
        write_json(self.runtime / "cycle-state.json", state)
        restarted = self.new_relay()
        self.assertEqual(restarted.status()["state"]["phase"], "uncertain")
        self.assertEqual(restarted.step(execute=True)["status"], "uncertain")
        result = restarted.resolve(True)
        self.assertEqual((result["status"], result["target"]), ("awaiting", "dsh"))
        self.assertEqual(self.state()["current"]["token"], current["token"])
        self.assertEqual(len(self.sender.calls), 1)

    def test_resolve_skip_pauses_until_explicit_reset_and_records_previous_turn(self):
        self.sender.error = RuntimeError("uncertain")
        self.relay.step(execute=True)
        token = self.state()["current"]["token"]
        self.assertEqual(self.relay.resolve(False)["status"], "paused")
        self.assertEqual(self.relay.step(execute=True)["status"], "paused")
        self.assertEqual(len(self.sender.calls), 1)
        result = self.relay.reset("dsh")
        self.assertEqual((result["status"], result["target"]), ("ready", "dsh"))
        self.assertEqual(self.state()["last_reset"]["previous_token"], token)
        self.assertEqual(self.state()["last_reset"]["previous_phase"], "paused")
        self.assertEqual(len(self.sender.calls), 1)

    def test_pause_file_stops_input_and_status_preview_do_not_modify_saved_state(self):
        self.send_first()
        original = (self.runtime / "cycle-state.json").read_bytes()
        self.relay.status()
        self.relay.step(execute=False)
        self.assertEqual((self.runtime / "cycle-state.json").read_bytes(), original)
        (self.runtime / "PAUSE").touch()
        self.assertEqual(self.relay.step(execute=True)["status"], "paused")
        self.assertEqual(len(self.sender.calls), 1)

    def test_resume_after_step_catches_user_pause_preserves_nonce_and_does_not_resend(self):
        current = self.send_first()
        self.show_done()
        self.relay.step(execute=True)
        self.clock.advance(3)
        self.relay.step(execute=True)
        original_start = self.state()["phase_started_at"]
        (self.runtime / "PAUSE").touch()
        self.assertEqual(self.relay.step(execute=True)["status"], "paused")
        paused = self.state()
        self.assertEqual(paused["paused_from"], "awaiting")
        self.assertEqual(paused["paused_at"], self.clock.now)
        self.clock.advance(22000)
        # Repeated checks do not replace the instant at which this pause began.
        self.assertEqual(self.relay.step(execute=True)["status"], "paused")
        self.assertEqual(self.state()["paused_at"], paused["paused_at"])
        (self.runtime / "PAUSE").unlink()
        result = self.relay.resume()
        self.assertEqual((result["status"], result["phase"]), ("resumed", "awaiting"))
        self.assertEqual(self.state()["current"]["token"], current["token"])
        self.assertEqual(self.state()["current"]["event_id"], current["event_id"])
        self.assertEqual(self.state()["phase_started_at"], original_start + 22000)
        self.assertEqual(self.state()["stability"]["samples"], 0)
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(self.relay.step(execute=True)["reason_code"], "settling")
        self.clock.advance(3)
        self.relay.step(execute=True)
        self.clock.advance(3)
        self.assertEqual(self.relay.step(execute=True)["status"], "completed")
        self.assertEqual(len(self.sender.calls), 1)

    def test_resume_user_pause_before_initial_send_returns_to_ready(self):
        self.observer.values["gpt"] = observation(busy=True)
        self.relay.step(execute=True)
        (self.runtime / "PAUSE").touch()
        self.relay.step(execute=True)
        self.assertEqual(self.state()["paused_from"], "ready")
        (self.runtime / "PAUSE").unlink()
        self.assertEqual(self.relay.resume()["phase"], "ready")
        self.assertEqual(self.sender.calls, [])
        self.observer.values["gpt"] = observation()
        self.assertEqual(self.relay.step(execute=True)["status"], "awaiting")
        self.assertEqual(len(self.sender.calls), 1)

    def test_resume_rejects_fault_pause_and_unresolved_send(self):
        self.observer.values["gpt"] = observation(blocked=True)
        self.relay.step(execute=True)
        with self.assertRaisesRegex(ValueError, "reset-cycle"):
            self.relay.resume()
        self.relay.reset()
        self.observer.values["gpt"] = observation()
        self.sender.error = RuntimeError("send uncertain")
        self.relay.step(execute=True)
        with self.assertRaisesRegex(ValueError, "resolve"):
            self.relay.resume()
        self.assertEqual(len(self.sender.calls), 1)

    def test_resume_when_not_paused_is_a_read_only_noop(self):
        self.send_first()
        self.show_done()
        self.relay.step(execute=True)
        original = (self.runtime / "cycle-state.json").read_bytes()
        self.assertTrue(self.relay.resume()["noop"])
        self.assertEqual((self.runtime / "cycle-state.json").read_bytes(), original)
        self.assertEqual(len(self.sender.calls), 1)

    def test_cycle_state_does_not_read_other_state_files_or_share_project_binding(self):
        self.runtime.mkdir(parents=True)
        for name in ("state.json", "direct-state.json"):
            (self.runtime / name).write_text("deliberately invalid old JSON", encoding="utf-8")
        self.send_first()
        self.config["dsh"]["project"] = "another-project"
        with self.assertRaisesRegex(ValueError, "绑定"):
            self.new_relay().step(execute=True)
        self.assertEqual(len(self.sender.calls), 1)


if __name__ == "__main__":
    unittest.main()
