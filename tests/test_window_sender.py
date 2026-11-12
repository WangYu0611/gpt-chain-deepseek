from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from relay.storage import read_json
from windowrelay.sender import WindowSender
from windowrelay.windows import DesktopError, NativeDesktop, WindowInfo, _unicode_batches


class FakeDesktop:
    def __init__(self):
        self.actions = []
        self.window = WindowInfo(12, "GPT 目标", "chrome.exe", (1000, 700), 96)
        self.fail = None
        self.draft = ""
        self.submitted = []

    def find_window(self, spec):
        self.actions.append(("find", spec))
        return self.window

    def _act(self, name, *args):
        if self.fail == name:
            raise DesktopError("模拟前台窗口已改变")
        self.actions.append((name, *args))

    def activate(self, w): self._act("activate")
    def move(self, w, p): self._act("move", p)
    def click(self, w, p):
        self._act("click", p)
        if p == (220, 200):
            self.draft = ""
        if p == (900, 600):
            self.submitted.append(self.draft)
            self.draft = ""
    def wait(self, w, s): self._act("wait", s)
    def type_unicode(self, w, text):
        self._act("type", text)
        self.draft += text

    def enter(self, w):
        self._act("enter")
        self.submitted.append(self.draft)
        self.draft = ""


class FakeObserver:
    def __init__(self, backend):
        self.backend = backend
        self.calls = []
        self.changes = []

    def snapshot(self, target):
        self.calls.append(target)
        result = dict(available=True, busy=False, blocked=False,
                      editable_present=True, editable_text=self.backend.draft)
        if self.changes:
            change = self.changes.pop(0)
            if isinstance(change, Exception):
                raise change
            result.update(change)
        return SimpleNamespace(**result)


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)
        self.backend = FakeDesktop()
        self.observer = FakeObserver(self.backend)
        self.config = {
            "gpt": {"dedicated_input": True, "window": {"process_name": "chrome.exe", "title_contains": "GPT"},
                    "calibration": {"client_size": [1000, 700], "dpi": 96, "input": [400, 600], "send": None}},
            "dsh": {"project": "ue_game", "window": {"process_name": "DeepSeek Harness.exe", "title_contains": "DeepSeek"},
                    "calibration": {"client_size": [1000, 700], "dpi": 96, "project": "ue_game", "input": [400, 600], "send": [900, 600],
                                    "before_input": [{"action": "move", "point": [100, 200], "role": "project", "wait_seconds": 0.2},
                                                     {"action": "click", "point": [220, 200], "role": "new_task", "wait_seconds": 1}]}}
        }

    def tearDown(self): self.tmp.cleanup()

    def sender(self): return WindowSender(self.config, self.runtime, self.backend, self.observer)

    def test_gpt_types_only_then_one_enter(self):
        self.sender().send_gpt("DS做完了，请检查。\n任务 TASK-1", "a" * 40)
        self.assertEqual([x[0] for x in self.backend.actions].count("enter"), 1)
        self.assertEqual([x for x in self.backend.actions if x[0] == "type"], [("type", "DS做完了，请检查。\n任务 TASK-1")])
        self.assertNotIn("move", [x[0] for x in self.backend.actions])

    def test_dsh_new_card_sequence_and_send_button(self):
        self.sender().send_dsh("拉取最新的main，并完成指定任务书。", "b" * 40)
        actions = [(x[0], x[1] if len(x) > 1 else None) for x in self.backend.actions]
        self.assertLess(actions.index(("move", (100, 200))), actions.index(("click", (220, 200))))
        self.assertLess(actions.index(("click", (220, 200))), actions.index(("type", "拉取最新的main，并完成指定任务书。")))
        self.assertEqual([action for action in actions if action[0] == "click"][-1], ("click", (900, 600)))
        self.assertNotIn("enter", [x[0] for x in self.backend.actions])

    def test_missing_calibration_or_dedicated_input_never_acts(self):
        self.config["gpt"]["dedicated_input"] = False
        with self.assertRaises(DesktopError): self.sender().send_gpt("test", "x")
        self.assertEqual(self.backend.actions, [])
        self.config["gpt"]["dedicated_input"] = True
        self.config["gpt"]["calibration"]["input"] = None
        with self.assertRaises(DesktopError): self.sender().send_gpt("test", "x")
        self.assertEqual(self.backend.actions, [])

    def test_project_change_or_multiple_new_task_steps_stops(self):
        self.config["dsh"]["project"] = "different"
        with self.assertRaises(DesktopError): self.sender().send_dsh("test", "x")
        self.assertEqual(self.backend.actions, [])
        self.config["dsh"]["project"] = "ue_game"
        self.config["dsh"]["calibration"]["before_input"].append({"point": [50, 50], "role": "new_task"})
        with self.assertRaises(DesktopError): self.sender().send_dsh("test", "x")
        self.assertEqual(self.backend.actions, [])

    def test_size_or_dpi_change_stops_before_activation(self):
        self.backend.window = WindowInfo(12, "GPT 目标", "chrome.exe", (900, 700), 96)
        with self.assertRaises(DesktopError): self.sender().send_gpt("test", "x")
        self.assertEqual([x[0] for x in self.backend.actions], ["find"])
        self.backend.window = WindowInfo(12, "GPT 目标", "chrome.exe", (1000, 700), 144)
        with self.assertRaises(DesktopError): self.sender().send_gpt("test", "x")
        self.assertNotIn("activate", [x[0] for x in self.backend.actions])

    def test_doctor_validation_is_read_only(self):
        result = self.sender().validate("gpt")
        self.assertEqual(result, self.backend.window)
        self.assertEqual([x[0] for x in self.backend.actions], ["find"])
        self.assertFalse((self.runtime / "ui-receipts").exists())
        self.assertEqual(self.observer.calls, [])

    def test_default_verification_reads_back_before_and_after_one_send(self):
        self.sender().send_gpt("核对完整正文。\n下一行", "verified")
        record = read_json(next((self.runtime / "ui-receipts").glob("*.json")))
        self.assertEqual(self.observer.calls, ["gpt", "gpt"])
        self.assertTrue(record["input_verified"])
        self.assertTrue(record["submission_verified"])
        self.assertEqual(record["phase"], "send_verified")
        self.assertIsNone(record["server_received"])
        self.assertEqual(self.backend.submitted, ["核对完整正文。\n下一行"])

    def test_missing_or_mismatched_typed_text_never_presses_send(self):
        for index, change in enumerate((
            {"editable_text": ""}, {"editable_text": "only part"},
            {"available": False}, {"editable_present": False},
            {"busy": True}, {"blocked": True}, {"editable_text": None},
        )):
            with self.subTest(change=change):
                self.observer.changes = [change]
                before = len(self.backend.actions)
                self.backend.draft = ""
                with self.assertRaisesRegex(DesktopError, "未能在目标消息框核对输入内容"):
                    self.sender().send_gpt("complete message", f"mismatch-{index}")
                self.assertNotIn("enter", [action[0] for action in self.backend.actions[before:]])
                self.assertEqual(self.backend.submitted, [])

    def test_dsh_mismatched_input_does_not_click_its_send_button(self):
        self.observer.changes = [{"editable_text": "wrong box"}]
        with self.assertRaisesRegex(DesktopError, "输入点位"):
            self.sender().send_dsh("完成任务。", "dsh-mismatch")
        self.assertNotIn(("click", (900, 600)), self.backend.actions)
        self.assertEqual(self.backend.submitted, [])

    def test_readback_exception_stops_without_enter_or_retry(self):
        self.observer.changes = [RuntimeError("UIA unavailable")]
        with self.assertRaisesRegex(DesktopError, "输入点位"):
            self.sender().send_gpt("test", "unreadable")
        self.assertEqual(self.observer.calls, ["gpt"])
        self.assertNotIn("enter", [action[0] for action in self.backend.actions])
        record = read_json(next((self.runtime / "ui-receipts").glob("*.json")))
        self.assertEqual(record["status"], "uncertain")

    def test_unchanged_full_draft_after_send_is_uncertain_without_resend(self):
        self.observer.changes = [{}, {"editable_text": "test", "busy": True}]
        sender = self.sender()
        with self.assertRaisesRegex(DesktopError, "发送动作未生效"):
            sender.send_gpt("test", "not-submitted")
        self.assertEqual([action[0] for action in self.backend.actions].count("enter"), 1)
        self.assertEqual(self.observer.calls, ["gpt", "gpt"])
        record = read_json(next((self.runtime / "ui-receipts").glob("*.json")))
        self.assertEqual(record["status"], "uncertain")
        self.assertEqual(record["phase"], "send_requested")
        with self.assertRaisesRegex(DesktopError, "已经尝试过"):
            sender.send_gpt("test", "not-submitted")
        self.assertEqual([action[0] for action in self.backend.actions].count("enter"), 1)

    def test_new_busy_state_after_send_confirms_action_even_with_empty_placeholder(self):
        self.observer.changes = [{}, {"editable_text": "Work with ChatGPT\n", "busy": True}]
        self.sender().send_gpt("本轮完整正文", "working-after-send")
        record = read_json(next((self.runtime / "ui-receipts").glob("*.json")))
        self.assertEqual(record["submission_evidence"], "busy_after_send")
        self.assertEqual(record["status"], "send_action_completed")
        self.assertIsNone(record["server_received"])
        self.assertEqual(self.observer.calls, ["gpt", "gpt"])

    def test_unreadable_or_changed_nonempty_editor_after_send_is_uncertain(self):
        for index, change in enumerate(({"available": False}, {"editable_present": False}, {"editable_text": "unexpected draft"})):
            with self.subTest(change=change):
                self.backend.draft = ""
                self.observer.changes = [{}, change]
                with self.assertRaisesRegex(DesktopError, "发送后未能确认"):
                    self.sender().send_gpt("test", f"post-unknown-{index}")
        self.assertEqual(len(self.backend.submitted), 3)

    def test_line_endings_are_normalized_but_zero_width_content_is_preserved(self):
        self.observer.changes = [{"editable_text": "第一行\r\n第二行\r\n"}]
        self.sender().send_gpt("第一行\n第二行", "line-endings")
        self.backend.draft = ""
        self.observer.changes = [{"editable_text": "第一行\n第二行\u200b"}]
        with self.assertRaisesRegex(DesktopError, "输入点位"):
            self.sender().send_gpt("第一行\n第二行", "zero-width-mismatch")

    def test_auto_link_accessibility_whitespace_can_match_complete_message(self):
        message = "仓库：https://github.com/user/repo\n本轮识别码：abcd1234"
        self.observer.changes = [{"editable_text": "仓库：\nhttps://github.com/user/repo\n本轮识别码： abcd1234\n"}]
        self.sender().send_gpt(message, "auto-link-whitespace")
        record = read_json(next((self.runtime / "ui-receipts").glob("*.json")))
        self.assertEqual(record["match_mode"], "accessibility_whitespace")
        self.assertEqual(self.backend.submitted, [message])
        self.assertEqual([action[0] for action in self.backend.actions].count("enter"), 1)

    def test_missing_characters_wrong_nonce_and_changed_order_never_send(self):
        message = "仓库：https://github.com/user/repo\n本轮识别码：abcd1234"
        for index, actual in enumerate((
            "仓库：\nhttps://github.com/user/rep\n本轮识别码：abcd1234",
            "仓库：\nhttps://github.com/user/repo\n本轮识别码：abcd1235",
            "本轮识别码：abcd1234\n仓库：\nhttps://github.com/user/repo",
        )):
            with self.subTest(actual=actual):
                self.backend.draft = ""
                self.observer.changes = [{"editable_text": actual}]
                with self.assertRaisesRegex(DesktopError, "输入点位"):
                    self.sender().send_gpt(message, f"changed-content-{index}")
        self.assertNotIn("enter", [action[0] for action in self.backend.actions])
        self.assertEqual(self.backend.submitted, [])

    def test_unsubmitted_autolink_whitespace_still_counts_as_unchanged_full_draft(self):
        message = "仓库：https://github.com/user/repo"
        self.observer.changes = [{}, {"editable_text": "仓库：\nhttps://github.com/user/repo"}]
        with self.assertRaisesRegex(DesktopError, "发送动作未生效"):
            self.sender().send_gpt(message, "unsubmitted-autolink")
        self.assertEqual([action[0] for action in self.backend.actions].count("enter"), 1)

    def test_default_observer_is_lazy_and_is_not_skipped_for_fake_backend(self):
        with patch("windowrelay.observer.WindowObserver", return_value=self.observer) as factory:
            sender = WindowSender(self.config, self.runtime, self.backend)
            sender.validate("gpt")
            factory.assert_not_called()
            sender.send_gpt("test", "default-observer")
            factory.assert_called_once_with(self.config)
            self.assertEqual(self.observer.calls, ["gpt", "gpt"])

    def test_explicit_debug_disable_and_invalid_verification_config(self):
        self.config["gpt"]["input_verification"] = False
        self.sender().send_gpt("debug only", "disabled")
        self.assertEqual(self.observer.calls, [])
        self.config["gpt"]["input_verification"] = "false"
        before = len(self.backend.actions)
        with self.assertRaisesRegex(DesktopError, "input_verification"):
            self.sender().send_gpt("test", "invalid-verification")
        self.assertEqual(len(self.backend.actions), before)

    def test_new_task_role_requires_a_click(self):
        self.config["dsh"]["calibration"]["before_input"][1]["action"] = "move"
        with self.assertRaises(DesktopError): self.sender().send_dsh("test", "x")
        self.assertEqual(self.backend.actions, [])

    def test_uncertain_attempt_is_not_repeated(self):
        self.backend.fail = "enter"
        sender = self.sender()
        with self.assertRaises(DesktopError): sender.send_gpt("test", "repeat")
        record = read_json(next((self.runtime / "ui-receipts").glob("*.json")))
        self.assertEqual(record["status"], "uncertain")
        self.backend.fail = None
        count = len(self.backend.actions)
        with self.assertRaises(DesktopError): sender.send_gpt("test", "repeat")
        self.assertEqual(len(self.backend.actions), count + 1)  # identity read only

    def test_pause_does_not_initialize_backend(self):
        (self.runtime / "PAUSE").touch()
        with self.assertRaises(DesktopError): self.sender().send_gpt("test", "x")
        self.assertEqual(self.backend.actions, [])

    def test_control_characters_are_rejected_before_actions(self):
        with self.assertRaises(DesktopError): self.sender().send_gpt("test\x1b", "x")
        self.assertEqual(self.backend.actions, [])

    def test_unicode_and_linebreak_packets(self):
        batches = _unicode_batches("中😀\n文")
        scans = [e.value.ki.wScan for b in batches for e in b if e.value.ki.dwFlags == 4]
        self.assertEqual(scans, [0x4E2D, 0xD83D, 0xDE00, 0x6587])
        self.assertEqual([e.value.ki.wVk for e in batches[1]], [0x10, 0x0D, 0x0D, 0x10])


class NativeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.window = WindowInfo(12, "GPT 目标", "chrome.exe", (1000, 700), 96)
        self.desktop = object.__new__(NativeDesktop)
        self.desktop.abort = None
        self.desktop.is_desktop_available = lambda: True
        self.desktop.specs = {12: {"process_name": "chrome.exe", "title_contains": "GPT"}}
        self.user = SimpleNamespace(GetAsyncKeyState=lambda _: 0, GetForegroundWindow=lambda: 12,
                                    ClientToScreen=lambda *_: True, WindowFromPoint=lambda _: 120,
                                    GetAncestor=lambda *_: 12)
        self.desktop.api = SimpleNamespace(user=self.user)
        self.metadata = patch("windowrelay.windows._window_info", return_value=self.window)
        self.info = self.metadata.start()
        self.addCleanup(self.metadata.stop)

    def test_escape_quick_press_stops_before_identity_or_input(self):
        self.user.GetAsyncKeyState = lambda _: 1
        with self.assertRaisesRegex(DesktopError, "Esc"): self.desktop.check(self.window)
        self.info.assert_not_called()

    def test_foreground_changed_stops(self):
        self.user.GetForegroundWindow = lambda: 99
        with self.assertRaisesRegex(DesktopError, "前台"): self.desktop.check(self.window)

    def test_window_title_changed_stops(self):
        self.info.return_value = WindowInfo(12, "another conversation", "chrome.exe", (1000, 700), 96)
        with self.assertRaisesRegex(DesktopError, "标题"): self.desktop.check(self.window)

    def test_dpi_changed_during_input_stops(self):
        self.info.return_value = WindowInfo(12, "GPT 目标", "chrome.exe", (1000, 700), 144)
        with self.assertRaisesRegex(DesktopError, "DPI"): self.desktop.check(self.window)

    def test_other_window_covering_point_stops_before_mouse_move(self):
        self.user.GetAncestor = lambda *_: 99
        with self.assertRaisesRegex(DesktopError, "遮挡"): self.desktop.move(self.window, (400, 600))


if __name__ == "__main__": unittest.main()
