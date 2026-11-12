from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from relay.storage import read_json
from windowrelay.uia_sender import ComAutomation, FocusedCharacterWriter, UiaNode, UiaSnapshot, UiaWindowSender, _matches, wire_message
from windowrelay.windows import DesktopError, WindowInfo


class StaleUiaError(Exception):
    hresult = -2147220991


class FakeDesktop:
    def __init__(self): self.actions = []; self.locked = False
    def is_desktop_available(self): return not self.locked
    def is_escape(self): return False
    def find_window(self, spec):
        return WindowInfo(12 if spec["process_name"] == "chrome.exe" else 13, "target", spec["process_name"], (1000, 700), 96)
    def activate(self, info): self.actions.append(("activate", info.hwnd))
    def check(self, info): self.actions.append(("check", info.hwnd))


class FakeAutomation:
    def __init__(self):
        self.stage = "old"
        self.value = ""
        self.placeholder = "Work with ChatGPT\n"
        self.address = "chatgpt.com/c/exact"
        self.noop_value = False
        self.no_ack = False
        self.wrong_project_after_new = False
        self.actions = []
        self.on_mutation = lambda: None
        self.complete = True
        self.focused = None
        self.hidden_duplicate = False
        self.unknown_boundary = False
        self.editor_class = "ProseMirror"
        self.focus_class_name = None
        self.access_mode = "工作区内修改"
        self.new_card_access_mode = None
        self.access_menu_open = False
        self.access_mode_missing = False
        self.access_mode_duplicate = False
        self.access_option_missing = False
        self.access_option_duplicate = False
        self.access_change_noop = False
        self.access_menu_stays_open = False
        self.access_mode_after_write = None
        self.invoke_error_before = set()
        self.invoke_error_after = set()
        self.confirm_required = False
        self.confirmation_open = False
        self.confirmation_name = "确认启用自动审查（实验）？"
        self.risk_state = 0
        self.toggle_noop = False
        self.toggle_error_after = False
        self.confirm_enable_noop = False
        self.focus_ready_after = 0
        self.focus_reads = 0
        self.snapshot_stale_after_new = 0
        self.card_generation = 0
        self.new_card_noop = False
        self.new_card_treeitem = False
        self.unselected_blank_marker = False
        self.extra_old_blank_marker = False
        self.full_confirmation_name = None

    def snapshot(self, hwnd):
        if self.stage == "blank" and self.snapshot_stale_after_new:
            self.snapshot_stale_after_new -= 1
            raise StaleUiaError("tree changed during read")
        if hwnd == 12:
            nodes = [UiaNode((1,), "Work with ChatGPT", "Edit", class_name=self.editor_class),
                     UiaNode((2,), "地址和搜索栏", "Edit", "view_1012", "OmniboxViewViews")]
            if self.hidden_duplicate:
                nodes.append(UiaNode((99,), "Work with ChatGPT", "Edit", class_name="ProseMirror", visible=False))
            if self.stage == "busy":
                nodes.append(UiaNode((9,), "Stop", "Button"))
            elif self.value not in ("", self.placeholder):
                nodes.append(UiaNode((8,), "Send", "Button"))
            else:
                nodes.extend([UiaNode((5,), "Start Voice", "Button"), UiaNode((6,), "Dictate", "Button")])
            return UiaSnapshot(tuple(nodes), self.complete)
        selected_order = (3,) if self.wrong_project_after_new and self.stage != "old" else (2,) if self.unknown_boundary else (1,)
        selected_name = "新会话" if self.new_card_treeitem and self.stage != "old" and not self.unselected_blank_marker else "current task"
        nodes = [UiaNode((3, self.card_generation), selected_name, "TreeItem", class_name="x_sessionRow x_selected", order=selected_order, parent_identity=(90,)),
                 UiaNode((4,), "默认工作区", "TreeItem", class_name="x_projectRow", order=(3,) if self.unknown_boundary else (2,), parent_identity=(90,)),
                 UiaNode((2,), "ue_game", "TreeItem", class_name="x_projectRow", order=(0,), parent_identity=(90,)),
                 UiaNode((10,), "新建会话", "Button", class_name="x_brand"),
                 UiaNode((11,), "新建会话", "Button", class_name="x_newSession"),
                 UiaNode((12,), "composer", "Edit", class_name="Composer")]
        if self.unknown_boundary:
            nodes.append(UiaNode((80,), "unknown workspace boundary", "TreeItem", class_name="future_workspace", order=(1,), parent_identity=(90,)))
        if self.stage != "old" and not self.new_card_treeitem:
            nodes.append(UiaNode((15, self.card_generation), "新会话", "Text"))
        if self.new_card_treeitem and self.unselected_blank_marker:
            nodes.append(UiaNode((15, self.card_generation), "新会话", "TreeItem", class_name="x_sessionRow", order=(0, 1), parent_identity=(90,)))
        if self.extra_old_blank_marker:
            nodes.append(UiaNode((99, 42), "新会话", "TreeItem", class_name="x_sessionRow", order=(0, 2), parent_identity=(90,)))
        if not self.access_mode_missing:
            nodes.append(UiaNode((20,), "访问模式，当前：" + self.access_mode, "Button"))
            if self.access_mode_duplicate:
                nodes.append(UiaNode((21,), "访问模式，当前：" + self.access_mode, "Button"))
        if self.access_menu_open and not self.access_option_missing:
            nodes.append(UiaNode((22,), "自动审查 EXP", "MenuItem", class_name="_item_4ub78_90"))
            nodes.append(UiaNode((24,), "完全权限", "MenuItem", class_name="_item_4ub78_90"))
            if self.access_option_duplicate:
                nodes.append(UiaNode((23,), "自动审查 EXP", "MenuItem", class_name="_item_4ub78_90"))
                nodes.append(UiaNode((25,), "完全权限", "MenuItem", class_name="_item_4ub78_90"))
        if self.confirmation_open:
            ancestor = ({"name": self.confirmation_name, "control_type": "Window",
                         "automation_id": "", "class_name": "_dialog_o6lrb_33 _confirmation_cjd8z_1"},)
            nodes.extend([
                UiaNode((30,), self.confirmation_name, "Window", class_name="_dialog_o6lrb_33 _confirmation_cjd8z_1"),
                UiaNode((31,), "我已了解这些风险，并愿意继续", "CheckBox", ancestors=ancestor),
                UiaNode((32,), "启用自动审查", "Button", enabled=self.risk_state == 1, ancestors=ancestor),
            ])
        if self.value: nodes.append(UiaNode((16,), "发送消息", "Button"))
        if self.stage == "busy": nodes.append(UiaNode((17,), "停止生成", "Button"))
        return UiaSnapshot(tuple(nodes), self.complete)

    def read_value(self, node): return self.address if node.identity == (2,) and node.control_type == "Edit" else self.value
    def read_pattern_text(self, node): return self.value
    def read_texts(self, node): return (node.name, self.read_value(node))
    def is_selected(self, node): return bool(node.identity) and node.identity[0] == 3
    def focus(self, node):
        self.on_mutation()
        self.actions.append(("focus", node.identity))
        self.focused = node.identity
        if self.focus_class_name:
            self.editor_class = self.focus_class_name
    def is_focused(self, node):
        self.focus_reads += 1
        return self.focused == node.identity and self.focus_reads > self.focus_ready_after
    def toggle_state(self, node): return self.risk_state
    def toggle(self, node):
        self.on_mutation()
        self.actions.append(("toggle", node.name, self.risk_state))
        if not self.toggle_noop:
            self.risk_state = 1
        if self.toggle_error_after:
            raise StaleUiaError("checkbox changed after Toggle")
    def set_value(self, node, message):
        self.on_mutation(); self.actions.append(("set", message))
        if not self.noop_value: self.value = message
        if self.access_mode_after_write:
            self.access_mode = self.access_mode_after_write
    def invoke(self, node):
        self.on_mutation(); self.actions.append(("invoke", node.name, node.class_name))
        if node.name in self.invoke_error_before:
            raise StaleUiaError("Invoke result is unknown")
        if node.class_name.endswith("_newSession"):
            if not self.new_card_noop:
                self.stage, self.value = "blank", ""
                self.card_generation += 1
                self.access_menu_open = False
                if self.new_card_access_mode is not None:
                    self.access_mode = self.new_card_access_mode
        elif node.name.startswith("访问模式，当前："):
            self.access_menu_open = not self.access_menu_open
        elif node.control_type == "MenuItem" and node.name == "自动审查 EXP":
            if self.confirm_required:
                self.confirmation_open = True
            elif not self.access_change_noop:
                self.access_mode = "自动审查"
            if not self.access_menu_stays_open:
                self.access_menu_open = False
        elif node.control_type == "MenuItem" and node.name == "完全权限":
            if not self.access_change_noop:
                self.access_mode = "完全权限"
            if self.full_confirmation_name is not None:
                self.confirmation_open = True
                self.confirmation_name = self.full_confirmation_name
            if not self.access_menu_stays_open:
                self.access_menu_open = False
        elif node.name == "启用自动审查":
            if self.risk_state == 1 and not self.confirm_enable_noop:
                self.confirmation_open = False
                self.access_mode = "自动审查"
        elif node.name in {"Send", "发送消息"} and not self.no_ack:
            self.stage = "busy"
            self.value = self.placeholder if node.name == "Send" else ""
        if node.name in self.invoke_error_after:
            raise StaleUiaError("old element retired after Invoke")


class FakeCharacters:
    def __init__(self, automation, suffix=""):
        self.automation, self.suffix = automation, suffix
        self.messages = []
    def write(self, info, editor, message, backend, automation):
        automation.on_mutation()
        self.messages.append(message)
        automation.value = message + self.suffix


class SemanticSenderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name) / "runtime"
        self.desktop = FakeDesktop()
        self.automation = FakeAutomation()
        self.config = {
            "gpt": {"url": "https://chatgpt.com/c/exact", "window": {"process_name": "chrome.exe", "title_contains": "target"},
                    "calibration": {"client_size": [1000, 700], "dpi": 96}, "uia": {"verification_timeout": 0.2}},
            "dsh": {"project": "ue_game", "window": {"process_name": "DeepSeek Harness.exe", "title_contains": "target"},
                    "calibration": {"client_size": [1000, 700], "dpi": 96},
                    "uia": {"project_mode": "sidebar_group", "new_card": {"name": "新会话", "control_type": "Text"},
                            "editor": {"name": "composer", "control_type": "Edit", "class_name": "Composer"}, "verification_timeout": 0.2}},
        }
        def persisted_before_mutation():
            receipts = list((self.runtime / "ui-receipts").glob("*.json"))
            self.assertEqual(len(receipts), 1)
            self.assertIn(read_json(receipts[0])["status"], ("started", "uncertain"))
        self.automation.on_mutation = persisted_before_mutation

    def sender(self, writer=None):
        return UiaWindowSender(self.config, self.runtime, self.desktop, self.automation, character_writer=writer)

    def receipt(self): return read_json(next((self.runtime / "ui-receipts").glob("*.json")))

    def test_dsh_semantic_new_card_and_verified_message_before_send(self):
        self.sender().send_dsh("拉取 main 并完成任务", "dsh-1")
        self.assertEqual(self.automation.actions, [("invoke", "新建会话", "x_newSession"),
                                                   ("set", "拉取 main 并完成任务"), ("invoke", "发送消息", "")])
        self.assertEqual(self.receipt()["status"], "send_action_completed")
        self.assertEqual(self.receipt()["acknowledgement"], "new_busy")
        self.assertIsNone(self.receipt()["server_received"])
        self.assertEqual(self.desktop.actions, [])  # No coordinate clicks or key injection.

    def test_new_card_auto_review_is_verified_before_any_text_input(self):
        self.config["dsh"]["uia"].update(access_mode="自动审查", write_mode="wm_char")
        self.automation.access_mode = "自动审查"  # The old task's setting is irrelevant.
        self.automation.new_card_access_mode = "工作区内修改"
        writer = FakeCharacters(self.automation)
        original = writer.write

        def checked_writer(*args):
            self.assertEqual(self.automation.access_mode, "自动审查")
            self.assertFalse(self.automation.access_menu_open)
            record = self.receipt()
            self.assertEqual(record["access_mode_verified"], "自动审查")
            self.assertEqual(record["phase"], "before_text")
            original(*args)

        writer.write = checked_writer
        self.sender(writer).send_dsh("拉取 main 完成任务", "auto-review-new-card")
        self.assertEqual([action[1] for action in self.automation.actions if action[0] == "invoke"],
                         ["新建会话", "访问模式，当前：工作区内修改", "自动审查 EXP", "发送消息"])
        self.assertEqual(writer.messages, ["拉取 main 完成任务"])
        self.assertEqual(self.receipt()["access_mode_requested"], "自动审查")
        self.assertEqual(self.receipt()["access_mode_verified"], "自动审查")

    def test_already_auto_review_does_not_reopen_or_reinvoke_access_mode(self):
        self.config["dsh"]["uia"]["access_mode"] = "自动审查"
        self.automation.new_card_access_mode = "自动审查"
        self.sender().send_dsh("task", "already-auto-review")
        self.assertEqual([action[1] for action in self.automation.actions if action[0] == "invoke"], ["新建会话", "发送消息"])
        self.assertEqual(self.receipt()["access_mode_verified"], "自动审查")

    def test_full_permissions_new_card_is_changed_to_requested_auto_review(self):
        self.config["dsh"]["uia"]["access_mode"] = "自动审查"
        self.automation.new_card_access_mode = "完全权限"
        self.sender().send_dsh("task", "full-to-auto-review")
        self.assertIn(("invoke", "访问模式，当前：完全权限", ""), self.automation.actions)
        self.assertEqual(self.automation.access_mode, "自动审查")

    def test_missing_ambiguous_or_unconfirmed_access_mode_never_types_or_sends(self):
        self.config["dsh"]["uia"].update(access_mode="自动审查", write_mode="wm_char")
        parent = self.runtime
        guard = self.automation.on_mutation
        for flag in ("access_mode_missing", "access_mode_duplicate", "access_option_missing", "access_option_duplicate",
                     "access_change_noop", "access_menu_stays_open"):
            with self.subTest(flag=flag):
                self.runtime = parent / flag
                self.automation = FakeAutomation()
                self.automation.on_mutation = guard
                setattr(self.automation, flag, True)
                writer = FakeCharacters(self.automation)
                with self.assertRaises(DesktopError):
                    self.sender(writer).send_dsh("task", flag)
                self.assertEqual(writer.messages, [])
                self.assertFalse(any(action[0] == "set" or action[:2] == ("invoke", "发送消息") for action in self.automation.actions))
                self.assertEqual(self.receipt()["status"], "uncertain")
                self.assertNotIn("access_mode_verified", self.receipt())

    def test_access_mode_changed_after_typing_prevents_send(self):
        self.config["dsh"]["uia"]["access_mode"] = "自动审查"
        self.automation.new_card_access_mode = "自动审查"
        self.automation.access_mode_after_write = "工作区内修改"
        with self.assertRaises(DesktopError):
            self.sender().send_dsh("task", "mode-changed-after-text")
        self.assertFalse(any(action[:2] == ("invoke", "发送消息") for action in self.automation.actions))
        self.assertEqual(self.receipt()["status"], "uncertain")

    def test_invalid_access_mode_is_rejected_before_new_card(self):
        self.config["dsh"]["uia"]["access_mode"] = "未知模式"
        with self.assertRaisesRegex(DesktopError, "access_mode"):
            self.sender().send_dsh("task", "invalid-access-mode")
        self.assertEqual(self.automation.actions, [])
        self.assertFalse(self.runtime.exists())

    def test_full_access_is_selected_and_verified_before_typing_without_auto_review(self):
        self.config["dsh"]["uia"].update(access_mode="完全权限", write_mode="wm_char")
        self.automation.new_card_access_mode = "工作区内修改"
        self.automation.confirm_required = True  # Applies only to Auto Review.
        writer = FakeCharacters(self.automation)
        original = writer.write

        def verified_writer(*args):
            self.assertEqual(self.automation.access_mode, "完全权限")
            self.assertFalse(self.automation.access_menu_open)
            self.assertFalse(self.automation.confirmation_open)
            self.assertEqual(self.receipt()["access_mode_verified"], "完全权限")
            original(*args)

        writer.write = verified_writer
        self.sender(writer).send_dsh("task", "full-access-new-card")
        self.assertEqual([action[1] for action in self.automation.actions if action[0] == "invoke"],
                         ["新建会话", "访问模式，当前：工作区内修改", "完全权限", "发送消息"])
        self.assertEqual(writer.messages, ["task"])
        self.assertFalse(any(action[0] == "toggle" for action in self.automation.actions))

    def test_already_full_access_does_not_open_menu_or_select_another_mode(self):
        self.config["dsh"]["uia"]["access_mode"] = "完全权限"
        self.automation.new_card_access_mode = "完全权限"
        self.sender().send_dsh("task", "already-full-access")
        self.assertEqual([action[1] for action in self.automation.actions if action[0] == "invoke"], ["新建会话", "发送消息"])
        self.assertEqual(self.receipt()["access_mode_verified"], "完全权限")

    def test_auto_review_new_card_is_changed_to_full_access_without_review_confirmation(self):
        self.config["dsh"]["uia"]["access_mode"] = "完全权限"
        self.automation.new_card_access_mode = "自动审查"
        self.sender().send_dsh("task", "auto-review-to-full-access")
        invoked = [action[1] for action in self.automation.actions if action[0] == "invoke"]
        self.assertEqual(invoked, ["新建会话", "访问模式，当前：自动审查", "完全权限", "发送消息"])
        self.assertNotIn("自动审查 EXP", invoked)
        self.assertNotIn("启用自动审查", invoked)

    def test_unconfirmed_full_access_never_inputs_or_sends(self):
        self.config["dsh"]["uia"].update(access_mode="完全权限", write_mode="wm_char")
        parent = self.runtime
        guard = self.automation.on_mutation
        for flag in ("access_change_noop", "access_option_missing", "access_option_duplicate", "access_menu_stays_open"):
            with self.subTest(flag=flag):
                self.runtime = parent / flag
                self.automation = FakeAutomation()
                self.automation.on_mutation = guard
                setattr(self.automation, flag, True)
                writer = FakeCharacters(self.automation)
                with self.assertRaises(DesktopError):
                    self.sender(writer).send_dsh("task", "unconfirmed-full-" + flag)
                self.assertEqual(writer.messages, [])
                self.assertFalse(any(action[0] == "toggle" or action[0] == "set" or action[:2] == ("invoke", "发送消息") for action in self.automation.actions))
                self.assertNotIn("access_mode_verified", self.receipt())

    def test_full_access_changed_after_typing_prevents_send(self):
        self.config["dsh"]["uia"]["access_mode"] = "完全权限"
        self.automation.new_card_access_mode = "完全权限"
        self.automation.access_mode_after_write = "自动审查"
        with self.assertRaises(DesktopError):
            self.sender().send_dsh("task", "full-access-changed-after-typing")
        self.assertFalse(any(action[:2] == ("invoke", "发送消息") for action in self.automation.actions))
        self.assertEqual(self.receipt()["status"], "uncertain")

    def test_full_access_never_confirms_auto_review_or_an_unobserved_dialog(self):
        self.config["dsh"]["uia"]["access_mode"] = "完全权限"
        parent = self.runtime
        guard = self.automation.on_mutation
        for index, name in enumerate(("确认启用自动审查（实验）？", "尚未验证的完全权限确认对话")):
            with self.subTest(name=name):
                self.runtime = parent / str(index)
                self.automation = FakeAutomation()
                self.automation.on_mutation = guard
                self.automation.full_confirmation_name = name
                with self.assertRaisesRegex(DesktopError, "确认对话"):
                    self.sender().send_dsh("task", "unexpected-dialog-" + str(index))
                self.assertFalse(any(action[0] in {"toggle", "set"} or action[:2] in {("invoke", "启用自动审查"), ("invoke", "发送消息")} for action in self.automation.actions))
                self.assertNotIn("access_mode_verified", self.receipt())

    def test_new_card_invoke_stale_after_effect_is_observed_without_another_new_card(self):
        self.automation.invoke_error_after.add("新建会话")
        self.automation.snapshot_stale_after_new = 2
        self.sender().send_dsh("task", "new-card-error-after-effect")
        self.assertEqual([action[1] for action in self.automation.actions if action[0] == "invoke"], ["新建会话", "发送消息"])
        self.assertTrue(self.receipt()["new_card_confirmed_after_invoke_error"])
        self.assertEqual(self.receipt()["status"], "send_action_completed")

    def test_unconfirmed_new_card_invoke_error_is_never_repeated(self):
        self.automation.invoke_error_before.add("新建会话")
        with self.assertRaises(DesktopError):
            self.sender().send_dsh("task", "new-card-error-before-effect")
        self.assertEqual([action[1] for action in self.automation.actions if action[0] == "invoke"], ["新建会话"])
        self.assertFalse(any(action[0] == "set" for action in self.automation.actions))
        self.assertEqual(self.receipt()["status"], "uncertain")

    def test_preexisting_blank_card_is_not_proof_that_failed_invoke_created_a_new_one(self):
        self.automation.stage = "blank"
        self.automation.invoke_error_before.add("新建会话")
        with self.assertRaises(DesktopError):
            self.sender().send_dsh("task", "old-blank-not-new")
        self.assertEqual(len(self.automation.actions), 1)
        self.assertEqual(self.automation.actions[0][:2], ("invoke", "新建会话"))
        self.assertNotIn("new_card_confirmed_after_invoke_error", self.receipt())

    def test_successful_noop_new_card_invoke_does_not_reuse_old_blank_marker(self):
        self.automation.stage = "blank"
        self.automation.new_card_noop = True
        with self.assertRaises(DesktopError):
            self.sender().send_dsh("task", "new-card-successful-noop")
        self.assertEqual(self.automation.actions, [("invoke", "新建会话", "x_newSession")])
        self.assertEqual(self.receipt()["status"], "uncertain")

    def test_unselected_blank_treeitem_does_not_prove_current_new_card(self):
        self.config["dsh"]["uia"]["new_card"] = {"name": "新会话", "control_type": "TreeItem", "class_suffix": "_sessionRow"}
        self.automation.new_card_treeitem = True
        self.automation.unselected_blank_marker = True
        with self.assertRaises(DesktopError):
            self.sender().send_dsh("task", "unselected-blank-treeitem")
        self.assertEqual(self.automation.actions, [("invoke", "新建会话", "x_newSession")])
        self.assertEqual(self.receipt()["status"], "uncertain")

    def test_selected_new_blank_treeitem_and_identity_are_both_confirmed(self):
        self.config["dsh"]["uia"]["new_card"] = {"name": "新会话", "control_type": "TreeItem", "class_suffix": "_sessionRow"}
        self.automation.new_card_treeitem = True
        self.sender().send_dsh("task", "selected-new-blank-treeitem")
        self.assertEqual(self.receipt()["status"], "send_action_completed")

    def test_old_unselected_blank_treeitem_does_not_make_new_selected_card_ambiguous(self):
        self.config["dsh"]["uia"]["new_card"] = {"name": "新会话", "control_type": "TreeItem", "class_suffix": "_sessionRow"}
        self.automation.new_card_treeitem = True
        self.automation.extra_old_blank_marker = True
        self.sender().send_dsh("task", "new-selected-plus-old-unselected")
        self.assertEqual(self.receipt()["status"], "send_action_completed")
        self.assertEqual([action[1] for action in self.automation.actions if action[0] == "invoke"], ["新建会话", "发送消息"])

    def test_access_mode_invoke_errors_after_effect_are_confirmed_without_repeating(self):
        self.config["dsh"]["uia"]["access_mode"] = "自动审查"
        self.automation.invoke_error_after.update({"访问模式，当前：工作区内修改", "自动审查 EXP"})
        self.sender().send_dsh("task", "mode-invoke-errors")
        self.assertEqual([action[1] for action in self.automation.actions if action[0] == "invoke"],
                         ["新建会话", "访问模式，当前：工作区内修改", "自动审查 EXP", "发送消息"])
        self.assertEqual(self.receipt()["access_mode_verified"], "自动审查")

    def test_dsh_send_invoke_error_after_effect_checks_ack_without_resending(self):
        self.automation.invoke_error_after.add("发送消息")
        self.sender().send_dsh("task", "send-error-after-effect")
        self.assertEqual([action[1] for action in self.automation.actions if action[0] == "invoke"], ["新建会话", "发送消息"])
        self.assertEqual(self.receipt()["acknowledgement"], "new_busy")
        self.assertIn("send_invoke_warning", self.receipt())

    def test_gpt_send_invoke_error_after_effect_checks_ack_without_resending(self):
        self.automation.value = self.automation.placeholder
        self.automation.invoke_error_after.add("Send")
        self.sender().send_gpt("task", "gpt-send-error-after-effect")
        self.assertEqual([action[1] for action in self.automation.actions if action[0] == "invoke"], ["Send"])
        self.assertEqual(self.receipt()["status"], "send_action_completed")
        self.assertIn("send_invoke_warning", self.receipt())

    def test_gpt_send_invoke_error_without_effect_is_uncertain_without_retry(self):
        self.automation.value = self.automation.placeholder
        self.automation.invoke_error_before.add("Send")
        with self.assertRaises(DesktopError):
            self.sender().send_gpt("task", "gpt-send-error-no-effect")
        self.assertEqual([action[1] for action in self.automation.actions if action[0] == "invoke"], ["Send"])
        self.assertEqual(self.automation.value, "task")
        self.assertEqual(self.receipt()["status"], "uncertain")

    def test_exact_auto_review_confirmation_is_checked_and_enabled_before_typing(self):
        self.config["dsh"]["uia"]["access_mode"] = "自动审查"
        self.automation.confirm_required = True
        original = self.automation.set_value

        def checked_write(*args):
            self.assertFalse(self.automation.confirmation_open)
            self.assertEqual(self.automation.access_mode, "自动审查")
            self.assertEqual(self.receipt()["access_mode_verified"], "自动审查")
            return original(*args)

        self.automation.set_value = checked_write
        self.sender().send_dsh("task", "review-confirmation")
        self.assertEqual([action[1] for action in self.automation.actions if action[0] == "invoke"],
                         ["新建会话", "访问模式，当前：工作区内修改", "自动审查 EXP", "启用自动审查", "发送消息"])
        self.assertEqual([action for action in self.automation.actions if action[0] == "toggle"],
                         [("toggle", "我已了解这些风险，并愿意继续", 0)])

    def test_auto_review_confirmation_already_checked_is_not_toggled_off(self):
        self.config["dsh"]["uia"]["access_mode"] = "自动审查"
        self.automation.confirm_required = True
        self.automation.risk_state = 1
        self.sender().send_dsh("task", "review-checkbox-already-on")
        self.assertFalse(any(action[0] == "toggle" for action in self.automation.actions))
        self.assertEqual(self.automation.risk_state, 1)

    def test_confirmation_stale_errors_after_effect_do_not_repeat_checkbox_or_enable(self):
        self.config["dsh"]["uia"]["access_mode"] = "自动审查"
        self.automation.confirm_required = True
        self.automation.toggle_error_after = True
        self.automation.invoke_error_after.add("启用自动审查")
        self.sender().send_dsh("task", "review-confirmation-stale")
        self.assertEqual(sum(action[0] == "toggle" for action in self.automation.actions), 1)
        self.assertEqual(sum(action[:2] == ("invoke", "启用自动审查") for action in self.automation.actions), 1)
        self.assertEqual(self.receipt()["status"], "send_action_completed")

    def test_unrelated_confirmation_and_unconfirmed_risk_never_type_or_send(self):
        self.config["dsh"]["uia"]["access_mode"] = "自动审查"
        parent = self.runtime
        guard = self.automation.on_mutation
        for label in ("wrong_dialog", "toggle_noop", "enable_noop", "indeterminate"):
            with self.subTest(label=label):
                self.runtime = parent / label
                self.automation = FakeAutomation()
                self.automation.on_mutation = guard
                self.automation.confirm_required = True
                if label == "wrong_dialog":
                    self.automation.confirmation_name = "其他权限确认"
                elif label == "toggle_noop":
                    self.automation.toggle_noop = True
                elif label == "enable_noop":
                    self.automation.confirm_enable_noop = True
                else:
                    self.automation.risk_state = 2
                with self.assertRaises(DesktopError):
                    self.sender().send_dsh("task", "unconfirmed-" + label)
                self.assertFalse(any(action[0] == "set" or action[:2] == ("invoke", "发送消息") for action in self.automation.actions))
                self.assertEqual(self.receipt()["status"], "uncertain")
                if label == "wrong_dialog":
                    self.assertFalse(any(action[0] == "toggle" or action[:2] == ("invoke", "启用自动审查") for action in self.automation.actions))

    def test_initial_focus_delay_is_read_only_then_characters_are_written_once(self):
        self.config["gpt"]["uia"]["write_mode"] = "wm_char"
        self.config["gpt"]["uia"]["verification_timeout"] = 0.5
        self.automation.value = self.automation.placeholder
        self.automation.focus_ready_after = 2
        writer = FakeCharacters(self.automation)
        elapsed = [0.0]
        with patch("windowrelay.uia_sender.time.monotonic", side_effect=lambda: elapsed[0]), \
                patch("windowrelay.uia_sender.time.sleep", side_effect=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)):
            self.sender(writer).send_gpt("task", "focus-delay")
        self.assertEqual(writer.messages, ["task"])
        self.assertEqual(sum(action[0] == "focus" for action in self.automation.actions), 1)
        self.assertGreaterEqual(self.automation.focus_reads, 3)

    def test_unconfirmed_initial_focus_never_writes_or_repeats_focus(self):
        self.config["gpt"]["uia"]["write_mode"] = "wm_char"
        self.automation.value = self.automation.placeholder
        self.automation.focus_ready_after = 100
        writer = FakeCharacters(self.automation)
        elapsed = [0.0]
        with patch("windowrelay.uia_sender.time.monotonic", side_effect=lambda: elapsed[0]), \
                patch("windowrelay.uia_sender.time.sleep", side_effect=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)):
            with self.assertRaisesRegex(DesktopError, "焦点在等待后仍未到位"):
                self.sender(writer).send_gpt("task", "focus-no-effect")
        self.assertEqual(writer.messages, [])
        self.assertEqual(sum(action[0] == "focus" for action in self.automation.actions), 1)
        self.assertFalse(any(action[0] == "invoke" for action in self.automation.actions))

    def test_draft_appearing_during_focus_wait_is_preserved_without_any_character(self):
        self.config["gpt"]["uia"]["write_mode"] = "wm_char"
        self.automation.value = self.automation.placeholder
        writer = FakeCharacters(self.automation)
        elapsed = [0.0]
        reads = [0]

        def focus_after_draft(node):
            reads[0] += 1
            if reads[0] == 1:
                return False
            self.automation.value = "human draft during focus wait"
            return True

        self.automation.is_focused = focus_after_draft
        with patch("windowrelay.uia_sender.time.monotonic", side_effect=lambda: elapsed[0]), \
                patch("windowrelay.uia_sender.time.sleep", side_effect=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)):
            with self.assertRaisesRegex(DesktopError, "首字输入前"):
                self.sender(writer).send_gpt("task", "focus-draft-race")
        self.assertEqual(writer.messages, [])
        self.assertEqual(self.automation.value, "human draft during focus wait")
        self.assertEqual(sum(action[0] == "focus" for action in self.automation.actions), 1)
        self.assertFalse(any(action[0] == "invoke" for action in self.automation.actions))

    def test_setvalue_success_without_effect_is_uncertain_and_never_sends(self):
        self.automation.noop_value = True
        with self.assertRaisesRegex(DesktopError, "回读"): self.sender().send_dsh("task", "noop")
        self.assertEqual(self.receipt()["status"], "uncertain")
        self.assertFalse(any(action[:2] == ("invoke", "发送消息") for action in self.automation.actions))

    def test_wrong_project_after_new_card_prevents_typing(self):
        self.automation.wrong_project_after_new = True
        with self.assertRaisesRegex(DesktopError, "项目"): self.sender().send_dsh("task", "wrong")
        self.assertEqual(self.receipt()["status"], "uncertain")
        self.assertFalse(any(action[0] == "set" for action in self.automation.actions))

    def test_preexisting_draft_is_not_overwritten_or_discarded(self):
        self.automation.value = "my draft"
        with self.assertRaisesRegex(DesktopError, "草稿"): self.sender().send_dsh("task", "draft")
        self.assertEqual(self.automation.value, "my draft")
        self.assertEqual(self.automation.actions, [])
        self.assertFalse(self.runtime.exists())

    def test_same_event_is_not_replayed_after_success(self):
        sender = self.sender()
        sender.send_dsh("task", "once")
        before = list(self.automation.actions)
        self.automation.stage = "old"
        with self.assertRaisesRegex(DesktopError, "已尝试"): sender.send_dsh("task", "once")
        self.assertEqual(self.automation.actions, before)

    def test_complete_tree_and_unlocked_desktop_are_required(self):
        self.automation.complete = False
        with self.assertRaises(DesktopError): self.sender().send_dsh("task", "partial")
        self.assertEqual(self.automation.actions, [])
        self.automation.complete = True
        self.desktop.locked = True
        with self.assertRaises(DesktopError): self.sender().send_dsh("task", "lock")
        self.assertEqual(self.automation.actions, [])

    def test_wm_char_transforms_newlines_upfront_and_verifies_exact_wire_message(self):
        self.config["dsh"]["uia"]["write_mode"] = "wm_char"
        writer = FakeCharacters(self.automation)
        self.sender(writer).send_dsh("地址 https://example.test/a\n下一项\r\n完成😀", "chars")
        self.assertEqual(writer.messages, ["地址 https://example.test/a 下一项 完成😀"])
        self.assertEqual(self.receipt()["status"], "send_action_completed")

    def test_other_controls_are_rejected_before_any_receipt_or_mutation(self):
        self.config["dsh"]["uia"]["write_mode"] = "wm_char"
        with self.assertRaises(DesktopError): self.sender().send_dsh("task\tsecond", "tab")
        self.assertEqual(self.automation.actions, [])
        self.assertFalse(self.runtime.exists())

    def test_gpt_exact_url_placeholder_and_hidden_duplicate_editor(self):
        self.config["gpt"]["uia"]["write_mode"] = "wm_char"
        self.automation.value = self.automation.placeholder
        self.automation.hidden_duplicate = True
        writer = FakeCharacters(self.automation, suffix="\n")
        self.sender(writer).send_gpt("审核 M220\n继续下一项", "gpt-ok")
        self.assertEqual(writer.messages, ["审核 M220 继续下一项"])
        self.assertEqual([action[1] for action in self.automation.actions if action[0] == "invoke"], ["Send"])
        self.assertEqual(self.receipt()["status"], "send_action_completed")

    def test_gpt_wrong_url_never_focuses_or_writes(self):
        self.automation.address = "chatgpt.com/c/different"
        with self.assertRaisesRegex(DesktopError, "地址"): self.sender().send_gpt("task", "url")
        self.assertEqual(self.automation.actions, [])
        self.assertFalse(self.runtime.exists())

    def test_gpt_real_draft_is_preserved(self):
        self.automation.value = "unsent human message"
        with self.assertRaises(DesktopError): self.sender().send_gpt("task", "human")
        self.assertEqual(self.automation.value, "unsent human message")
        self.assertEqual(self.automation.actions, [])

    def test_gpt_missing_or_altered_nonwhitespace_character_is_not_accepted(self):
        self.config["gpt"]["uia"]["write_mode"] = "wm_char"
        self.automation.value = self.automation.placeholder
        with self.assertRaisesRegex(DesktopError, "回读"):
            self.sender(FakeCharacters(self.automation, suffix="wrong-token")).send_gpt("task", "paragraph")
        self.assertFalse(any(action[0] == "invoke" for action in self.automation.actions))
        self.assertEqual(self.receipt()["status"], "uncertain")

    def test_gpt_auto_link_whitespace_matches_entire_message_and_records_mode(self):
        self.config["gpt"]["uia"]["write_mode"] = "wm_char"
        self.automation.value = self.automation.placeholder
        writer = FakeCharacters(self.automation)
        original = writer.write
        def split_url(*args):
            original(*args)
            self.automation.value = self.automation.value.replace("https://example.test/repo", "\nhttps://example.test/repo\n") + "\n"
        writer.write = split_url
        self.sender(writer).send_gpt("任务 nonceABC 仓库 https://example.test/repo 请审核", "url-split")
        self.assertEqual(self.receipt()["text_match_mode"], "accessibility_whitespace")
        self.assertEqual(self.receipt()["status"], "send_action_completed")

    def test_delayed_wm_char_value_converges_before_one_send_without_retyping(self):
        self.config["gpt"]["uia"]["write_mode"] = "wm_char"
        self.config["gpt"]["uia"]["verification_timeout"] = 0.5
        self.automation.value = self.automation.placeholder
        writer = FakeCharacters(self.automation)
        elapsed = [0.0]
        reads = []
        original = self.automation.read_value

        def delayed_value(node):
            if node.identity == (1,) and writer.messages and self.automation.stage != "busy":
                reads.append(elapsed[0])
                if elapsed[0] < 0.3:
                    return "partially refreshed UIA value"
            return original(node)

        self.automation.read_value = delayed_value
        with patch("windowrelay.uia_sender.time.monotonic", side_effect=lambda: elapsed[0]), \
                patch("windowrelay.uia_sender.time.sleep", side_effect=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)):
            self.sender(writer).send_gpt("本轮识别码 nonceABC 请审核完整交付", "delayed-uia-value")
        self.assertGreaterEqual(len(reads), 4)
        self.assertEqual(writer.messages, ["本轮识别码 nonceABC 请审核完整交付"])
        self.assertEqual([action[1] for action in self.automation.actions if action[0] == "invoke"], ["Send"])
        self.assertGreaterEqual(self.receipt()["text_readback_attempts"], 4)
        self.assertEqual(self.receipt()["status"], "send_action_completed")

    def test_nonconverging_value_times_out_without_send_or_retyping(self):
        self.config["gpt"]["uia"]["write_mode"] = "wm_char"
        self.automation.value = self.automation.placeholder
        writer = FakeCharacters(self.automation)
        elapsed = [0.0]
        reads = []
        original = self.automation.read_value

        def stale_value(node):
            if node.identity == (1,) and writer.messages:
                reads.append(elapsed[0])
                return "wrong nonce or incomplete input"
            return original(node)

        self.automation.read_value = stale_value
        with patch("windowrelay.uia_sender.time.monotonic", side_effect=lambda: elapsed[0]), \
                patch("windowrelay.uia_sender.time.sleep", side_effect=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)):
            with self.assertRaisesRegex(DesktopError, "等待后仍不一致"):
                self.sender(writer).send_gpt("本轮识别码 nonceABC 请审核完整交付", "never-converges")
        self.assertGreaterEqual(len(reads), 2)
        self.assertLessEqual(elapsed[0], 0.2)
        self.assertEqual(len(writer.messages), 1)
        self.assertFalse(any(action[0] == "invoke" for action in self.automation.actions))
        self.assertEqual(self.receipt()["status"], "uncertain")

    def test_gpt_default_class_token_accepts_already_focused_placeholder(self):
        self.automation.editor_class = "ProseMirror ProseMirror-focused"
        self.automation.value = self.automation.placeholder
        self.sender().send_gpt("核对当前交付", "already-focused-editor")
        self.assertEqual(self.receipt()["status"], "send_action_completed")

    def test_gpt_class_token_survives_focus_change_between_write_and_send(self):
        self.config["gpt"]["uia"]["write_mode"] = "wm_char"
        self.automation.value = self.automation.placeholder
        self.automation.focus_class_name = "ProseMirror ProseMirror-focused"
        writer = FakeCharacters(self.automation)
        self.sender(writer).send_gpt("核对当前交付", "focus-toggles-class")
        self.assertEqual(self.automation.editor_class, "ProseMirror ProseMirror-focused")
        self.assertEqual(len(writer.messages), 1)
        self.assertEqual([action[1] for action in self.automation.actions if action[0] == "invoke"], ["Send"])
        self.assertEqual(self.receipt()["status"], "send_action_completed")

    def test_unknown_sidebar_boundary_never_inherits_previous_project(self):
        self.automation.unknown_boundary = True
        with self.assertRaisesRegex(DesktopError, "项目"): self.sender().send_dsh("task", "boundary")
        self.assertEqual(self.automation.actions, [])
        self.assertFalse(self.runtime.exists())

    def test_successful_invoke_without_clear_or_new_busy_stays_uncertain(self):
        self.automation.no_ack = True
        with self.assertRaisesRegex(DesktopError, "确认"): self.sender().send_dsh("task", "noack")
        self.assertEqual(self.receipt()["status"], "uncertain")

    def test_wire_message_never_glues_urls_or_sends_return(self):
        self.assertEqual(wire_message("https://example.test/a\nhttps://example.test/b", "wm_char"),
                         "https://example.test/a https://example.test/b")


class LowLevelSemanticGuards(unittest.TestCase):
    def test_stale_snapshot_is_discarded_and_rebuilt_read_only(self):
        automation = object.__new__(ComAutomation)
        fresh = UiaSnapshot((UiaNode((2,), "fresh", "Text"),))
        automation._snapshot_once = Mock(side_effect=[StaleUiaError("node retired"), fresh])
        with patch("windowrelay.uia_sender.time.sleep") as sleep:
            self.assertEqual(automation.snapshot(12), fresh)
        self.assertEqual(automation._snapshot_once.call_count, 2)
        self.assertEqual(automation._snapshot_once.call_args_list[0].args[1], automation._snapshot_once.call_args_list[1].args[1])
        sleep.assert_called_once()

    def test_snapshot_stale_retries_are_bounded_and_unrelated_errors_are_not_retried(self):
        automation = object.__new__(ComAutomation)
        automation._snapshot_once = Mock(side_effect=StaleUiaError("node retired"))
        with patch("windowrelay.uia_sender.time.sleep"), self.assertRaises(StaleUiaError):
            automation.snapshot(12)
        self.assertEqual(automation._snapshot_once.call_count, 3)
        automation._snapshot_once = Mock(side_effect=RuntimeError("unexpected provider failure"))
        with self.assertRaises(RuntimeError):
            automation.snapshot(12)
        self.assertEqual(automation._snapshot_once.call_count, 1)

    def test_checkbox_toggle_fresh_guard_never_turns_an_already_on_box_off(self):
        automation = object.__new__(ComAutomation)
        automation.UIA = SimpleNamespace(UIA_CheckBoxControlTypeId=50002)
        action = Mock()
        pattern = SimpleNamespace(CurrentToggleState=1, Toggle=action)
        automation._pattern = Mock(return_value=pattern)
        live = SimpleNamespace(CurrentName="我已了解这些风险，并愿意继续", CurrentControlType=50002,
                               CurrentIsEnabled=True, CurrentIsOffscreen=False)
        node = UiaNode((1,), live.CurrentName, "CheckBox", handle=live)
        with self.assertRaisesRegex(DesktopError, "不是 Off"):
            automation.toggle(node)
        action.assert_not_called()
        pattern.CurrentToggleState = 0
        automation.toggle(node)
        action.assert_called_once_with()

    def test_menu_item_invoke_requires_same_current_name_type_and_availability(self):
        automation = object.__new__(ComAutomation)
        automation.UIA = SimpleNamespace(UIA_ButtonControlTypeId=50000, UIA_MenuItemControlTypeId=50011)
        invoke = Mock()
        automation._pattern = Mock(return_value=SimpleNamespace(Invoke=invoke))
        live = {"CurrentName": "自动审查 EXP", "CurrentControlType": 50011,
                "CurrentIsEnabled": True, "CurrentIsOffscreen": False}
        node = UiaNode((1,), "自动审查 EXP", "MenuItem", handle=SimpleNamespace(**live))
        automation.invoke(node)
        invoke.assert_called_once_with()
        invoke.reset_mock()
        for change in ({"CurrentName": "完全权限"}, {"CurrentControlType": 50000},
                       {"CurrentIsEnabled": False}, {"CurrentIsOffscreen": True}):
            changed = {**live, **change}
            node = UiaNode((1,), "自动审查 EXP", "MenuItem", handle=SimpleNamespace(**changed))
            with self.subTest(change=change), self.assertRaises(DesktopError):
                automation.invoke(node)
        invoke.assert_not_called()

    def test_class_token_matches_whole_tokens_only_and_validates_single_token(self):
        selector = {"control_type": "Edit", "class_token": "ProseMirror"}
        for value in ("ProseMirror", "ProseMirror ProseMirror-focused", "before ProseMirror after"):
            with self.subTest(value=value):
                self.assertTrue(_matches(UiaNode((1,), "editor", "Edit", class_name=value), selector, project=""))
        for value in ("NotProseMirror", "ProseMirror-focused", "prosemirror"):
            with self.subTest(value=value):
                self.assertFalse(_matches(UiaNode((1,), "editor", "Edit", class_name=value), selector, project=""))
        UiaWindowSender._validate_selector(selector, "editor")
        with self.assertRaisesRegex(DesktopError, "单个完整类名"):
            UiaWindowSender._validate_selector({"class_token": "ProseMirror ProseMirror-focused"}, "editor")

    def test_invoke_rechecks_send_label_enabled_and_visible(self):
        automation = object.__new__(ComAutomation)
        automation.UIA = SimpleNamespace(UIA_ButtonControlTypeId=50000)
        invoke = Mock()
        automation._pattern = Mock(return_value=SimpleNamespace(Invoke=invoke))
        for changes in ({"CurrentName": "Stop"}, {"CurrentControlType": 50004},
                        {"CurrentIsEnabled": False}, {"CurrentIsOffscreen": True}):
            live = {"CurrentName": "Send", "CurrentControlType": 50000, "CurrentIsEnabled": True, "CurrentIsOffscreen": False}
            live.update(changes)
            node = UiaNode((1,), "Send", "Button", handle=SimpleNamespace(**live))
            with self.subTest(changes=changes), self.assertRaises(DesktopError): automation.invoke(node)
        invoke.assert_not_called()

    def test_character_writer_requires_visible_focus_and_emits_utf16_without_enter(self):
        class Call:
            def __init__(self, function): self.function = function
            def __call__(self, *args): return self.function(*args)
        sent = []
        visible = [True]
        def gui_info(thread, pointer):
            pointer._obj.hwndFocus = 55
            return True
        def class_name(hwnd, buffer, length):
            buffer.value = "Chrome_RenderWidgetHostHWND"
            return len(buffer.value)
        user = SimpleNamespace(
            GetGUIThreadInfo=Call(gui_info), GetClassNameW=Call(class_name),
            IsWindowUnicode=Call(lambda _: True), GetWindowThreadProcessId=Call(lambda *_: 7),
            GetAncestor=Call(lambda *_: 12), IsWindow=Call(lambda _: True), IsWindowVisible=Call(lambda _: visible[0]),
            SendMessageTimeoutW=Call(lambda hwnd, kind, code, *rest: sent.append((hwnd, kind, code)) or 1),
        )
        desktop = SimpleNamespace(api=SimpleNamespace(user=user), check=Mock())
        automation = SimpleNamespace(is_focused=Mock(return_value=True))
        info = WindowInfo(12, "target", "chrome.exe", (1000, 700), 96)
        editor = UiaNode((1,), "editor", "Edit")
        writer = FocusedCharacterWriter()
        writer.write(info, editor, "中😀", desktop, automation)
        self.assertEqual(sent, [(55, 0x102, 0x4E2D), (55, 0x102, 0xD83D), (55, 0x102, 0xDE00)])
        sent.clear()
        visible[0] = False
        with self.assertRaises(DesktopError): writer.write(info, editor, "A", desktop, automation)
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
