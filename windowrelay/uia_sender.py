"""Shared, opt-in UIA sender with the WindowSender interface.

UiaWindowSender verifies the configured GPT URL and DSH selected-project group,
uses exact visible controls, and reserves a receipt before any UI mutation.
ValuePattern writes must take effect. Explicit wm_char mode focuses and verifies
the visible Chromium child, replaces line breaks with spaces, and sends no Enter.
The full readback must match every ordered non-whitespace character before Invoke
Send; successful submission also requires a new busy state or configured echo.

DSH selectors live in dsh.uia; GPT has observed defaults overridable in gpt.uia.
class_token matches a whole CSS class token across focus-state class changes.
No coordinate clicks, clipboard, application storage, model APIs or Git calls.
Importing this module never connects to or changes an application.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import time
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from relay.storage import run_lock, write_json
from .windows import DesktopError, DesktopUnavailableError, NativeDesktop, WindowInfo


def _is_stale_uia_error(error: BaseException) -> bool:
    # UIA_E_ELEMENTNOTAVAILABLE shares a generic COM error description. It does
    # not prove whether a preceding Invoke already changed the application.
    value = getattr(error, "hresult", None)
    if value is None and error.args and type(error.args[0]) is int:
        value = error.args[0]
    return type(value) is int and value & 0xFFFFFFFF == 0x80040201


@dataclass(frozen=True)
class UiaNode:
    identity: tuple
    name: str
    control_type: str
    automation_id: str = ""
    class_name: str = ""
    visible: bool = True
    enabled: bool = True
    ancestors: tuple[dict, ...] = ()
    handle: object = field(default=None, repr=False, compare=False)
    order: tuple[int, ...] = ()
    parent_identity: tuple = ()


@dataclass(frozen=True)
class UiaSnapshot:
    nodes: tuple[UiaNode, ...]
    complete: bool = True
    reason: str = ""


def _matches(node, selector: dict, *, project: str, message: str = "") -> bool:
    def expand(value):
        return value.replace("{project}", project).replace("{message}", message)
    if not node.visible:
        return False
    names = selector.get("names", [selector["name"]] if "name" in selector else None)
    if names is not None and node.name not in [expand(value) for value in names]:
        return False
    for key in ("control_type", "automation_id", "class_name"):
        if key in selector and getattr(node, key) != expand(selector[key]):
            return False
    if "class_token" in selector and expand(selector["class_token"]) not in node.class_name.split():
        return False
    if "class_suffix" in selector and not any(token.endswith(selector["class_suffix"]) for token in node.class_name.split()):
        return False
    ancestor = selector.get("ancestor")
    if ancestor and not any(_matches(UiaNode((), **item), ancestor, project=project, message=message) for item in node.ancestors):
        return False
    return True


class ComAutomation:
    """Bounded visible UIA reads and explicit patterns. No coordinate fallbacks."""
    def __init__(self):
        if os.name != "nt":
            raise DesktopError("UIA 语义发送仅支持 Windows。")
        import comtypes.client
        self.COMError = comtypes.COMError
        try:
            from comtypes.gen import UIAutomationClient as UIA
        except ImportError:
            UIA = comtypes.client.GetModule("UIAutomationCore.dll")
        self.UIA = UIA
        self.client = comtypes.client.CreateObject(getattr(UIA, "CUIAutomation8", UIA.CUIAutomation), interface=UIA.IUIAutomation)
        try:
            bounded = self.client.QueryInterface(UIA.IUIAutomation2)
            bounded.ConnectionTimeout = 2000
            bounded.TransactionTimeout = 3000
        except (AttributeError, OSError, comtypes.COMError):
            raise DesktopError("当前 UIA 客户端不支持有界事务，未启用语义发送。")

    def snapshot(self, hwnd: int) -> UiaSnapshot:
        deadline = time.monotonic() + 5
        for attempt in range(3):
            try:
                return self._snapshot_once(hwnd, deadline)
            except Exception as error:
                if not _is_stale_uia_error(error) or attempt == 2 or time.monotonic() >= deadline:
                    raise
                # Discard the entire torn tree. Never infer project/idle state
                # from a partial snapshot or reuse its stale element handles.
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    def _snapshot_once(self, hwnd: int, deadline: float) -> UiaSnapshot:
        UIA = self.UIA
        root = self.client.ElementFromHandle(hwnd)
        walker = self.client.ControlViewWalker
        queue = deque([(root, (), (), ())])
        nodes, seen = [], set()
        types = {getattr(UIA, f"UIA_{name}ControlTypeId"): name
                 for name in ("Button", "MenuItem", "CheckBox", "Window", "Edit", "Text", "Document", "Custom", "Pane", "TreeItem", "ListItem")}
        while queue:
            if len(nodes) >= 1800 or time.monotonic() >= deadline:
                return UiaSnapshot(tuple(nodes), False, "UIA 可见树读取超限。")
            element, ancestors, order, parent_identity = queue.popleft()
            if element.CurrentIsOffscreen:
                continue
            identity = tuple(element.GetRuntimeId())
            if identity in seen:
                continue
            seen.add(identity)
            node = UiaNode(identity, str(element.CurrentName or ""), types.get(element.CurrentControlType, str(element.CurrentControlType)),
                           str(element.CurrentAutomationId or ""), str(element.CurrentClassName or ""), True,
                           bool(element.CurrentIsEnabled), ancestors, element, order, parent_identity)
            nodes.append(node)
            parent = {"name": node.name, "control_type": node.control_type, "automation_id": node.automation_id, "class_name": node.class_name}
            child = walker.GetLastChildElement(element)
            reverse_index = 0
            while child:
                if len(queue) + len(nodes) >= 1800 or time.monotonic() >= deadline:
                    return UiaSnapshot(tuple(nodes), False, "UIA 可见树读取超限。")
                if not child.CurrentIsOffscreen:
                    queue.append((child, ancestors + (parent,), order + (reverse_index,), identity))
                reverse_index -= 1
                child = walker.GetPreviousSiblingElement(child)
        return UiaSnapshot(tuple(nodes))

    def _pattern(self, node: UiaNode, kind: str):
        return node.handle.GetCurrentPattern(getattr(self.UIA, f"UIA_{kind}PatternId")).QueryInterface(getattr(self.UIA, f"IUIAutomation{kind}Pattern"))

    def read_value(self, node: UiaNode) -> str:
        value = self._pattern(node, "Value")
        if value.CurrentIsReadOnly:
            raise DesktopError("消息框只读，未写入。")
        return str(value.CurrentValue or "")

    def read_texts(self, node: UiaNode) -> tuple[str, ...]:
        texts = [node.name]
        for kind, field_name in (("Value", "CurrentValue"), ("Text", None), ("LegacyIAccessible", "CurrentValue"), ("LegacyIAccessible", "CurrentDescription")):
            try:
                pattern = self._pattern(node, kind)
                texts.append(str(getattr(pattern, field_name) or "") if field_name else str(pattern.DocumentRange.GetText(20001)))
            except (OSError, AttributeError, self.COMError):
                pass  # Unsupported read pattern; never substitutes a write path.
        return tuple(texts)

    def set_value(self, node: UiaNode, message: str) -> None:
        value = self._pattern(node, "Value")
        if value.CurrentIsReadOnly:
            raise DesktopError("消息框只读，未写入。")
        value.SetValue(message)

    def invoke(self, node: UiaNode) -> None:
        current = node.handle
        expected_type = {"Button": "UIA_ButtonControlTypeId", "MenuItem": "UIA_MenuItemControlTypeId"}.get(node.control_type)
        expected_type = getattr(self.UIA, expected_type, None) if expected_type else None
        if (expected_type is None or not node.visible or not node.enabled
                or str(current.CurrentName or "") != node.name
                or current.CurrentControlType != expected_type
                or not current.CurrentIsEnabled or current.CurrentIsOffscreen):
            raise DesktopError("控件在执行前改变名称、类型或可用状态，未调用 Invoke。")
        self._pattern(node, "Invoke").Invoke()

    def select(self, node: UiaNode) -> None:
        self._pattern(node, "SelectionItem").Select()

    def toggle_state(self, node: UiaNode) -> int:
        return int(self._pattern(node, "Toggle").CurrentToggleState)

    def toggle(self, node: UiaNode) -> None:
        current = node.handle
        if (node.control_type != "CheckBox" or not node.visible or not node.enabled
                or current.CurrentControlType != self.UIA.UIA_CheckBoxControlTypeId
                or str(current.CurrentName or "") != node.name
                or not current.CurrentIsEnabled or current.CurrentIsOffscreen):
            raise DesktopError("自动审查确认框的勾选控件已改变，未调用 Toggle。")
        if self.toggle_state(node) != 0:
            raise DesktopError("勾选控件已不是 Off，未重复 Toggle。")
        self._pattern(node, "Toggle").Toggle()

    def is_selected(self, node: UiaNode) -> bool:
        value = node.handle.GetCurrentPropertyValue(self.UIA.UIA_SelectionItemIsSelectedPropertyId)
        return isinstance(value, (bool, int)) and value == True

    def read_pattern_text(self, node: UiaNode) -> str:
        return str(self._pattern(node, "Text").DocumentRange.GetText(20001))

    def focus(self, node: UiaNode) -> None:
        node.handle.SetFocus()

    def is_focused(self, node: UiaNode) -> bool:
        return bool(self.client.CompareElements(self.client.GetFocusedElement(), node.handle))


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND), ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


class FocusedCharacterWriter:
    """Explicit single-line WM_CHAR path; never sends Return or a shortcut."""
    def write(self, window: WindowInfo, editor: UiaNode, message: str, backend, automation) -> None:
        if any(ord(char) < 32 for char in message):
            raise DesktopError("WM_CHAR 模式仅接受不含换行、Tab 或控制字符的单段消息。")
        user = backend.api.user
        user.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GuiThreadInfo)]
        user.GetGUIThreadInfo.restype = wintypes.BOOL
        user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.GetClassNameW.restype = ctypes.c_int
        user.IsWindowUnicode.argtypes = [wintypes.HWND]
        user.IsWindowUnicode.restype = wintypes.BOOL
        user.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                             wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        user.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        target_thread = user.GetWindowThreadProcessId(window.hwnd, None)
        expected_child = None
        raw = message.encode("utf-16-le")
        for index in range(0, len(raw), 2):
            backend.check(window)
            if not automation.is_focused(editor):
                raise DesktopError("消息框焦点改变，停止 WM_CHAR 输入。")
            info = _GuiThreadInfo()
            info.cbSize = ctypes.sizeof(info)
            if not user.GetGUIThreadInfo(target_thread, ctypes.byref(info)):
                raise DesktopError("无法核对消息框实际焦点，停止输入。")
            child = int(info.hwndFocus or 0)
            name = ctypes.create_unicode_buffer(256)
            if not child or not user.IsWindow(child) or not user.IsWindowVisible(child) or not user.GetClassNameW(child, name, len(name)) or name.value != "Chrome_RenderWidgetHostHWND":
                raise DesktopError("焦点不在已识别的 Chromium 输入窗口，停止输入。")
            if int(user.GetAncestor(child, 2) or 0) != window.hwnd or not user.IsWindowUnicode(child):
                raise DesktopError("消息框焦点窗口归属不符，停止输入。")
            if expected_child is not None and child != expected_child:
                raise DesktopError("输入中 Chromium 子窗口改变，停止输入。")
            expected_child = child
            result = ctypes.c_size_t()
            code = int.from_bytes(raw[index:index + 2], "little")
            if not user.SendMessageTimeoutW(child, 0x0102, code, 1, 0x0002, 1000, ctypes.byref(result)):
                raise DesktopError("WM_CHAR 输入超时或被拒绝；已停止，不自动重试。")


def wire_message(message: str, mode: str) -> str:
    if not isinstance(message, str) or not message.strip() or len(message) > 20000:
        raise DesktopError("消息须为非空且不超过 20000 字符。")
    # WM_CHAR Return can submit a composer. Flatten intentional line breaks to
    # spaces before writing, then verify this exact wire text without inference.
    result = re.sub(r"(?:\r\n|\r|\n)+", " ", message) if mode == "wm_char" else message
    if any(ord(char) < 32 and (mode == "wm_char" or char not in "\r\n") for char in result):
        raise DesktopError("消息包含不支持的控制字符；未开始输入。")
    result.encode("utf-16-le")  # Reject invalid lone surrogates before mutation.
    return result


class SemanticInput:
    """Reusable GPT/DSH input plus Invoke; always verify full text first."""
    def __init__(self, backend, automation, guard, character_writer=None, paragraph_newline=False,
                 readback_timeout=3.0):
        self.backend, self.automation, self.guard = backend, automation, guard
        self.character_writer = character_writer or FocusedCharacterWriter()
        self.paragraph_newline = paragraph_newline
        self.match_mode = None
        if type(readback_timeout) not in (int, float) or not 0.2 <= readback_timeout <= 5:
            raise DesktopError("输入回读等待必须为 0.2 到 5 秒。")
        self.readback_timeout = float(readback_timeout)
        self.readback_attempts = 0

    def _equal(self, actual, expected):
        from .sender import WindowSender
        self.match_mode = WindowSender._match_text(actual, expected)
        return self.match_mode is not None

    def _wait_for_text(self, editor, wire):
        # WM_CHAR completion only means the host accepted the character packet.
        # Chromium's accessibility Value may still lag the actual draft. Wait
        # for that read-only view to converge; never type or invoke again here.
        deadline = time.monotonic() + self.readback_timeout
        while True:
            self.guard()
            self.readback_attempts += 1
            try:
                if self._equal(self.automation.read_value(editor), wire):
                    return
            except Exception as error:
                if not _is_stale_uia_error(error):
                    raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DesktopError("消息框完整回读在等待后仍不一致；未调用发送，不自动重试。")
            time.sleep(min(0.1, remaining))

    def _wait_for_focus(self, editor):
        deadline = time.monotonic() + self.readback_timeout
        while True:
            self.guard()
            try:
                if self.automation.is_focused(editor):
                    return
            except Exception as error:
                if not _is_stale_uia_error(error):
                    raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DesktopError("消息框焦点在等待后仍未到位；尚未输入文字，不重复 SetFocus。")
            time.sleep(min(0.1, remaining))

    def write_verified(self, info, editor, message, mode="value", expected_empty="") -> str:
        wire = wire_message(message, mode)
        self.guard()
        if not editor.visible or not editor.enabled or self.automation.read_value(editor) != expected_empty:
            raise DesktopError("消息框现值与已核实空态不符，保留原文。")
        if mode == "value":
            try:
                self.automation.set_value(editor, wire)
            except Exception as error:
                if not _is_stale_uia_error(error):
                    raise
                # SetValue may have taken effect before the old element was
                # retired. Verify its result; never issue a second write.
        elif mode == "wm_char":
            self.backend.activate(info)
            self.guard()
            try:
                self.automation.focus(editor)
            except Exception as error:
                if not _is_stale_uia_error(error):
                    raise
            self._wait_for_focus(editor)
            self.guard()
            if self.automation.read_value(editor) != expected_empty:
                raise DesktopError("首字输入前消息框已出现其他内容，保留草稿，尚未写入文字。")
            self.character_writer.write(info, editor, wire, self.backend, self.automation)
        else:
            raise DesktopError("未知输入模式。")
        self._wait_for_text(editor, wire)
        return wire

    def invoke_verified(self, editor, button, wire, allowed_names):
        self.guard()
        if not button.visible or not button.enabled or button.control_type != "Button" or button.name not in allowed_names:
            raise DesktopError("发送控件并非精确匹配的可见可用按钮。")
        if not self._equal(self.automation.read_value(editor), wire):
            raise DesktopError("发送前完整文字发生变化，未调用发送。")
        try:
            self.automation.invoke(button)
        except Exception as error:
            if not _is_stale_uia_error(error):
                raise
            return error  # Caller must confirm the postcondition without retry.
        return None


class UiaDshSender:
    def __init__(self, config: dict, runtime_dir: Path, backend=None, automation=None, gpt_sender=None, character_writer=None):
        self.config, self.runtime = config, Path(runtime_dir)
        self.backend, self.automation = backend, automation
        self.gpt_sender, self.character_writer = gpt_sender, character_writer

    def _settings(self):
        section = self.config.get("dsh", {})
        project, settings = section.get("project"), section.get("uia")
        if not isinstance(project, str) or not project.strip() or not isinstance(settings, dict):
            raise DesktopError("请配置 dsh.project 与 dsh.uia 语义选择器。")
        selectors = {
            "new_task": {"names": ["新建会话", "New conversation"], "class_suffix": "_newSession", "control_type": "Button"},
            "send": {"names": ["发送消息", "Send message", "Send"], "control_type": "Button"},
            "busy": {"names": ["停止生成", "Stop generating", "Stop response"], "control_type": "Button"},
        }
        selectors.update({key: settings[key] for key in ("project_indicator", "project_select", "new_card", "editor", "new_task", "send", "busy", "user_echo") if key in settings})
        for key in (("new_card", "editor") if settings.get("project_mode") == "sidebar_group" else ("project_indicator", "new_card", "editor")):
            if key not in selectors:
                raise DesktopError(f"缺少 dsh.uia.{key}，无法证明目标项目/新卡/消息框。")
        for key, selector in selectors.items():
            self._validate_selector(selector, key)
        if "project_indicator" in selectors:
            indicator = selectors["project_indicator"]
            if not any(indicator.get(key) for key in ("automation_id", "class_name", "class_token", "class_suffix", "ancestor")):
                raise DesktopError("project_indicator 必须限定工作区指示控件，不能只凭侧栏项目名称可见。")
        for key in ("new_task", "send", "busy"):
            if selectors[key].get("control_type") != "Button" or not (selectors[key].get("names") or selectors[key].get("name")):
                raise DesktopError(f"{key} 必须限定精确可见 Button 名称。")
        mode = settings.get("write_mode", "value")
        if mode not in {"value", "wm_char"}:
            raise DesktopError("UIA write_mode 仅允许 value 或显式 wm_char。")
        timeout = settings.get("verification_timeout", 5)
        if type(timeout) not in (int, float) or not 0.2 <= timeout <= 15:
            raise DesktopError("verification_timeout 必须为 0.2 到 15 秒。")
        if settings.get("access_mode") not in (None, "自动审查", "完全权限"):
            raise DesktopError("dsh.uia.access_mode 仅支持“完全权限”、“自动审查”或 null。")
        return section, project, settings, selectors, mode, float(timeout)

    @classmethod
    def _validate_selector(cls, value, label):
        if not isinstance(value, dict) or not any(value.get(key) for key in ("name", "names", "class_name", "class_token", "class_suffix", "automation_id")):
            raise DesktopError(f"{label} 必须填写精确语义选择器。")
        if any(key not in {"name", "names", "class_name", "class_token", "class_suffix", "automation_id", "control_type", "ancestor"} for key in value):
            raise DesktopError(f"{label} 包含不支持的选择器字段。")
        for key, item in value.items():
            if key == "names":
                if not isinstance(item, list) or not item or any(not isinstance(name, str) or not name for name in item):
                    raise DesktopError(f"{label}.names 必须是精确名称数组。")
            elif key == "ancestor":
                cls._validate_selector(item, label + ".ancestor")
            elif not isinstance(item, str) or not item:
                raise DesktopError(f"{label}.{key} 必须是非空文字。")
            elif key == "class_token" and (len(item.split()) != 1 or item != item.strip()):
                raise DesktopError(f"{label}.class_token 必须是单个完整类名 token。")

    def _guard(self, expected=None, target="dsh"):
        if (self.runtime / "PAUSE").exists():
            raise DesktopError("检测到暂停标记，未继续 UIA 操作。")
        if self.backend is None:
            self.backend = NativeDesktop()
        if not self.backend.is_desktop_available():
            raise DesktopUnavailableError("桌面已锁定，未继续 UIA 操作。")
        if self.backend.is_escape():
            raise DesktopError("检测到 Esc，停止 UIA 操作。")
        section = self.config[target]
        info = self.backend.find_window(section.get("window", {}))
        calibration = section.get("calibration") or {}
        if list(info.client_size) != calibration.get("client_size") or info.dpi != calibration.get("dpi"):
            raise DesktopError("窗口尺寸/DPI 与校准不符。")
        if expected and (info.hwnd, info.process_name, info.client_size, info.dpi) != (expected.hwnd, expected.process_name, expected.client_size, expected.dpi):
            raise DesktopError("语义操作中窗口身份发生变化。")
        return info

    def _tree(self, info, target="dsh"):
        self._guard(info, target)
        if self.automation is None:
            self.automation = ComAutomation()
        tree = self.automation.snapshot(info.hwnd)
        if not isinstance(tree, UiaSnapshot) or not tree.complete:
            raise DesktopError("UIA 读取不完整，不能操作。")
        return tree.nodes

    @staticmethod
    def _find(nodes, selector, project, message="", *, required=True, enabled=False):
        found = [node for node in nodes if _matches(node, selector, project=project, message=message) and (node.enabled or not enabled)]
        if len(found) != 1:
            if not found and not required:
                return None
            raise DesktopError(f"语义控件匹配到 {len(found)} 个，禁止猜测或坐标回退。")
        return found[0]

    def _project(self, nodes, selectors, project):
        settings = self.config["dsh"]["uia"]
        if settings.get("project_mode") == "sidebar_group":
            group = settings.get("sidebar_group", {})
            project_suffix = group.get("project_class_suffix", "_projectRow")
            session_suffix = group.get("session_class_suffix", "_sessionRow")
            selected_suffix = group.get("selected_class_suffix", "_selected")
            if any(not isinstance(value, str) or not value for value in (project_suffix, session_suffix, selected_suffix)):
                raise DesktopError("侧栏分组类名后缀配置无效。")
            def token(node, suffix): return any(part.endswith(suffix) for part in node.class_name.split())
            rows = [node for node in nodes if node.visible and node.control_type == "TreeItem"]
            selected = [node for node in rows if token(node, session_suffix) and token(node, selected_suffix) and self.automation.is_selected(node)]
            if len(selected) != 1:
                raise DesktopError("侧栏当前选中任务不唯一，无法核实项目。")
            selected = selected[0]
            if not selected.parent_identity or not selected.order:
                raise DesktopError("侧栏缺少可靠顺序/父节点信息，不能猜测项目。")
            current = None
            for row in sorted((row for row in rows if row.parent_identity == selected.parent_identity), key=lambda row: row.order):
                if token(row, project_suffix): current = row.name
                elif token(row, session_suffix):
                    if row.identity == selected.identity:
                        if current == project: return selected
                        break
                else:
                    current = None  # Never carry a project across an unknown workspace boundary.
            raise DesktopError("当前选中任务不在配置项目的侧栏分组中。")
        indicator = self._find(nodes, selectors["project_indicator"], project)
        if project not in self.automation.read_texts(indicator):
            raise DesktopError("当前卡片工作区无法精确核实为配置项目；侧栏可见不算项目证明。")

    def _ready(self, nodes, selectors, project):
        busy = [node for node in nodes if _matches(node, selectors["busy"], project=project)]
        if busy:
            raise DesktopError("当前 DSH 仍在运行，未创建或发送新任务。")

    def _invoke_once(self, node):
        try:
            self.automation.invoke(node)
        except Exception as error:
            if not _is_stale_uia_error(error):
                raise
            return error
        return None

    @staticmethod
    def _auto_review_confirmation():
        return {"control_type": "Window", "name": "确认启用自动审查（实验）？"}

    @staticmethod
    def _access_menu_items():
        return {"control_type": "MenuItem", "names": ["完全权限", "自动审查 EXP", "工作区内修改", "仅可查看"]}

    @staticmethod
    def _access_mode_option(desired):
        return {"control_type": "MenuItem", "name": {"自动审查": "自动审查 EXP", "完全权限": "完全权限"}[desired]}

    def _access_confirmation(self, nodes, project, desired):
        # The first node is the scoped application root, not a confirmation.
        dialogs = [node for node in nodes if node.visible and node.control_type == "Window"
                   and (not nodes or node.identity != nodes[0].identity)]
        expected = self._find(nodes, self._auto_review_confirmation(), project, required=False)
        if desired == "自动审查" and expected is not None and len(dialogs) == 1:
            return True
        if dialogs or expected is not None:
            raise DesktopError("发现尚未验证的访问模式确认对话，未输入或发送任务。")
        return False

    def _confirm_auto_review(self, info, timeout, selectors, project, phase):
        """Handle only the specific confirmation authorized by access_mode."""
        dialog = self._auto_review_confirmation()
        checkbox = {"control_type": "CheckBox", "name": "我已了解这些风险，并愿意继续", "ancestor": dialog}
        enable = {"control_type": "Button", "name": "启用自动审查", "ancestor": dialog}
        def risk_control(nodes):
            self._project(nodes, selectors, project)
            self._ready(nodes, selectors, project)
            self._find(nodes, dialog, project)
            check = self._find(nodes, checkbox, project, enabled=True)
            return check, self.automation.toggle_state(check)

        check, state = self._wait(info, timeout, risk_control)
        if state not in (0, 1):
            raise DesktopError("自动审查风险确认勾选状态不明确，未继续。")
        if state == 0:
            phase("before_auto_review_risk_confirmation")
            self._guard(info)
            try:
                self.automation.toggle(check)
            except Exception as error:
                if not _is_stale_uia_error(error):
                    raise
                # Toggle may have changed the state before its node retired.
                # Read the resulting state; never toggle again on an error.

        def enabled_confirmation(current):
            self._project(current, selectors, project)
            self._ready(current, selectors, project)
            self._find(current, dialog, project)
            check = self._find(current, checkbox, project, enabled=True)
            if self.automation.toggle_state(check) != 1:
                return False
            return self._find(current, enable, project, required=False, enabled=True)

        button = self._wait(info, timeout, enabled_confirmation)
        phase("before_auto_review_enable")
        self._guard(info)
        self._invoke_once(button)  # Caller confirms modal disappearance + mode.

    def _ensure_access_mode(self, info, timeout, selectors, project, desired, phase):
        """Apply the user-requested new-card mode using observed exact controls."""
        desired_label = "访问模式，当前：" + desired
        modes = {"control_type": "Button", "names": [
            "访问模式，当前：工作区内修改", "访问模式，当前：完全权限", "访问模式，当前：自动审查",
        ]}
        option_selector = self._access_mode_option(desired)
        menu_items = self._access_menu_items()

        def mode_button(nodes, *, required=True):
            self._project(nodes, selectors, project)
            self._ready(nodes, selectors, project)
            return self._find(nodes, modes, project, required=required, enabled=True)

        def confirmed(nodes):
            self._project(nodes, selectors, project)
            self._ready(nodes, selectors, project)
            if self._access_confirmation(nodes, project, desired):
                return False
            button = self._find(nodes, modes, project, required=False, enabled=True)
            if any(_matches(node, menu_items, project=project) for node in nodes):
                return False
            return button is not None and button.name == desired_label

        nodes = self._tree(info)
        if self._access_confirmation(nodes, project, desired):
            self._confirm_auto_review(info, timeout, selectors, project, phase)
            self._wait(info, timeout, confirmed)
            return
        button = mode_button(nodes)
        if button.name == desired_label:
            if any(_matches(node, menu_items, project=project) for node in nodes):
                raise DesktopError("访问模式菜单仍展开，未输入任务；请先关闭菜单。")
            return

        phase("before_access_mode_menu")
        self._guard(info)
        self._invoke_once(button)

        def menu_item(nodes):
            mode_button(nodes)
            return self._find(nodes, option_selector, project, required=False, enabled=True)

        item = self._wait(info, timeout, menu_item)
        phase("before_access_mode_selection")
        self._guard(info)
        self._invoke_once(item)

        def selected_or_confirmation(nodes):
            self._project(nodes, selectors, project)
            self._ready(nodes, selectors, project)
            if self._access_confirmation(nodes, project, desired):
                return "confirmation"
            return "confirmed" if confirmed(nodes) else False

        if self._wait(info, timeout, selected_or_confirmation) == "confirmation":
            self._confirm_auto_review(info, timeout, selectors, project, phase)
        self._wait(info, timeout, confirmed)

    def _wait(self, info, timeout, condition):
        deadline = time.monotonic() + timeout
        while True:
            try:
                nodes = self._tree(info)
                result = condition(nodes)
            except Exception as error:
                if not _is_stale_uia_error(error):
                    raise
                result = False  # Read-only condition will use a fresh full tree.
            if result:
                return result
            if time.monotonic() >= deadline:
                raise DesktopError("UIA 操作结果未得到完整界面确认；已停止，不自动重试。")
            time.sleep(0.1)

    def validate(self, target="dsh") -> WindowInfo:
        if target != "dsh":
            return self._gpt().validate(target)
        _, project, _, selectors, _, _ = self._settings()
        info = self._guard()
        nodes = self._tree(info)
        self._find(nodes, selectors["new_task"], project, enabled=True)
        self._find(nodes, selectors["editor"], project, enabled=True)
        if "project_select" not in selectors:
            self._project(nodes, selectors, project)
        return info

    def _gpt(self):
        if self.gpt_sender is None:
            from .sender import WindowSender
            self.gpt_sender = WindowSender(self.config, self.runtime, self.backend)
        return self.gpt_sender

    def send_gpt(self, message: str, event_id: str) -> None:
        self._gpt().send_gpt(message, event_id)

    def send_dsh(self, message: str, event_id: str) -> None:
        _, project, settings, selectors, mode, timeout = self._settings()
        wire = wire_message(message, mode)
        if not isinstance(event_id, str) or not event_id or len(event_id) > 200:
            raise DesktopError("事件编号无效。")
        info = self.validate()
        nodes = self._tree(info)
        self._ready(nodes, selectors, project)
        editor = self._find(nodes, selectors["editor"], project, enabled=True)
        if self.automation.read_value(editor) != "":
            raise DesktopError("已有未发送草稿，保留原文，不创建新卡。")
        with run_lock(self.runtime / ".uia-sender-lock"):
            receipt = self.runtime / "ui-receipts" / (hashlib.sha256(event_id.encode()).hexdigest() + ".json")
            receipt.parent.mkdir(parents=True, exist_ok=True)
            record = {"event_id": event_id, "target": "dsh", "mode": "uia", "window": info.hwnd,
                      "project": project, "status": "started", "phase": "before_mutation", "server_received": None,
                      "wire_sha256": hashlib.sha256(wire.encode()).hexdigest()}
            try:
                with receipt.open("x", encoding="utf-8") as stream:
                    json.dump(record, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError as error:
                raise DesktopError("本事件已尝试过，禁止重复创建卡片或发送。") from error
            def phase(value):
                record["phase"] = value
                write_json(receipt, record)
            try:
                if "project_select" in selectors:
                    nodes = self._tree(info)
                    item = self._find(nodes, selectors["project_select"], project, enabled=True)
                    phase("before_project_selection")
                    self._guard(info)
                    try:
                        self.automation.select(item)
                    except Exception as error:
                        if not _is_stale_uia_error(error):
                            raise
                    self._wait(info, timeout, lambda current: self.automation.is_selected(self._find(current, selectors["project_select"], project)))
                nodes = self._tree(info)
                self._project(nodes, selectors, project)
                self._ready(nodes, selectors, project)
                button = self._find(nodes, selectors["new_task"], project, enabled=True)
                old_blank_markers = {node.identity for node in nodes if _matches(node, selectors["new_card"], project=project)}
                phase("before_new_card")
                self._guard(info)
                new_card_error = self._invoke_once(button)
                if new_card_error is not None:
                    record["new_card_invoke_warning"] = str(new_card_error)
                    write_json(receipt, record)
                def blank(current):
                    matches = [node for node in current if _matches(node, selectors["new_card"], project=project)
                               and (node.control_type != "TreeItem" or self.automation.is_selected(node))]
                    if len(matches) > 1:
                        raise DesktopError("新空卡标记匹配到多个当前控件，禁止猜测。")
                    marker = matches[0] if matches else None
                    if marker is None or marker.identity in old_blank_markers:
                        return False
                    selected = self._project(current, selectors, project)
                    if marker.control_type == "TreeItem" and (
                        not self.automation.is_selected(marker)
                        or (selected is not None and selected.identity != marker.identity)
                    ):
                        return False
                    self._ready(current, selectors, project)
                    element = self._find(current, selectors["editor"], project, enabled=True)
                    if self.automation.read_value(element) != "":
                        raise DesktopError("新卡消息框不为空，禁止覆盖。")
                    return element
                editor = self._wait(info, timeout, blank)
                if new_card_error is not None:
                    record["new_card_confirmed_after_invoke_error"] = True
                access_mode = settings.get("access_mode")
                if access_mode is not None:
                    record["access_mode_requested"] = access_mode
                    phase("before_access_mode")
                    self._ensure_access_mode(info, timeout, selectors, project, access_mode, phase)
                    record["access_mode_verified"] = access_mode
                    phase("access_mode_verified")
                nodes = self._tree(info)
                self._project(nodes, selectors, project)
                self._ready(nodes, selectors, project)
                editor = self._find(nodes, selectors["editor"], project, enabled=True)
                if self.automation.read_value(editor) != "":
                    raise DesktopError("访问模式核验后消息框不为空，未输入任务。")
                baseline = {node.identity for node in nodes}
                phase("before_text")
                self._guard(info)
                inputs = SemanticInput(self.backend, self.automation, lambda: self._guard(info), self.character_writer,
                                       readback_timeout=min(timeout, 5))
                wire = inputs.write_verified(info, editor, message, mode)
                record["text_match_mode"] = inputs.match_mode
                record["text_readback_attempts"] = inputs.readback_attempts
                phase("typed_verified")
                def send_button(current):
                    self._project(current, selectors, project)
                    self._ready(current, selectors, project)
                    if access_mode is not None:
                        self._find(current, {"control_type": "Button", "name": "访问模式，当前：" + access_mode}, project, enabled=True)
                        if any(_matches(node, self._access_menu_items(), project=project) for node in current):
                            raise DesktopError("发送前访问模式菜单仍展开，未发送任务。")
                        if self._access_confirmation(current, project, access_mode):
                            raise DesktopError("访问模式确认尚未完成，未发送任务。")
                    field = self._find(current, selectors["editor"], project, enabled=True)
                    if not inputs._equal(self.automation.read_value(field), wire):
                        raise DesktopError("发送前消息内容发生变化，已停止。")
                    return self._find(current, selectors["send"], project, required=False, enabled=True)
                button = self._wait(info, timeout, send_button)
                phase("before_send")
                self._guard(info)
                allowed = selectors["send"].get("names", [selectors["send"].get("name")])
                send_error = inputs.invoke_verified(self._find(self._tree(info), selectors["editor"], project, enabled=True), button, wire, allowed)
                if send_error is not None:
                    record["send_invoke_warning"] = str(send_error)
                    write_json(receipt, record)
                def acknowledged(current):
                    self._project(current, selectors, project)
                    field = self._find(current, selectors["editor"], project, enabled=True)
                    if self.automation.read_value(field) != "":
                        return False
                    if any(_matches(node, selectors["busy"], project=project) for node in current):
                        return "new_busy"
                    if "user_echo" in selectors:
                        for node in current:
                            if node.identity not in baseline and node.identity != editor.identity and _matches(node, selectors["user_echo"], project=project, message=wire):
                                if wire in self.automation.read_texts(node):
                                    return "new_user_echo"
                    return False
                acknowledgement = self._wait(info, timeout, acknowledged)
                record.update(status="send_action_completed", phase="acknowledged", acknowledgement=acknowledgement)
                write_json(receipt, record)
            except BaseException as error:
                record.update(status="uncertain", error=str(error) or type(error).__name__)
                write_json(receipt, record)
                raise


class UiaWindowSender(UiaDshSender):
    """Shared sender facade: semantic DSH cards and a URL-verified GPT composer."""
    def _gpt_settings(self):
        section = self.config.get("gpt", {})
        settings = section.get("uia", {})
        if not isinstance(settings, dict):
            raise DesktopError("gpt.uia 必须为对象。")
        selectors = {
            "editor": {"name": "Work with ChatGPT", "class_token": "ProseMirror", "control_type": "Edit"},
            "omnibox": {"class_name": "OmniboxViewViews", "automation_id": "view_1012", "control_type": "Edit"},
            "send": {"names": ["Send"], "control_type": "Button"},
            "busy": {"names": ["Stop generating", "Stop response", "Stop", "停止生成", "停止"], "control_type": "Button"},
        }
        for key in tuple(selectors) + ("user_echo",):
            if key in settings:
                selectors[key] = settings[key]
        for key, selector in selectors.items():
            self._validate_selector(selector, key)
        for key in ("send", "busy"):
            if selectors[key].get("control_type") != "Button" or not (selectors[key].get("names") or selectors[key].get("name")):
                raise DesktopError("GPT 发送/停止按钮必须限定精确可见 Button 名称。")
        mode = settings.get("write_mode", "value")
        timeout = settings.get("verification_timeout", 5)
        if mode not in {"value", "wm_char"} or type(timeout) not in (int, float) or not 0.2 <= timeout <= 15:
            raise DesktopError("GPT 输入模式或核验超时配置无效。")
        return selectors, mode, float(timeout)

    def _gpt_url(self, nodes, selectors):
        address = self._find(nodes, selectors["omnibox"], "")
        actual = self.automation.read_value(address).strip()
        actual = urlparse(actual if "://" in actual else "https://" + actual)
        wanted = urlparse(str(self.config["gpt"].get("url", "")).strip())
        if wanted.scheme != "https" or wanted.hostname not in {"chatgpt.com", "chat.openai.com"} or not wanted.path.startswith("/c/"):
            raise DesktopError("GPT 配置必须为指定对话 HTTPS 地址。")
        if (actual.scheme, actual.netloc.casefold(), actual.path, actual.query, actual.fragment) != (wanted.scheme, wanted.netloc.casefold(), wanted.path, wanted.query, wanted.fragment):
            raise DesktopError("当前 Chrome 地址并非配置的 GPT 对话，未输入。")

    def _gpt_empty(self, nodes, selectors, editor, *, allow_busy=False):
        value = self.automation.read_value(editor)
        if value == "":
            return True
        if editor.control_type != "Edit" or "ProseMirror" not in editor.class_name.split() or editor.name != "Work with ChatGPT" or value not in ("Work with ChatGPT\n", "Work with ChatGPT\r\n", "Work with ChatGPT"):
            return False
        if self.automation.read_pattern_text(editor) != value:
            return False
        if any(_matches(node, selectors["send"], project="") for node in nodes):
            return False
        enabled = {node.name for node in nodes if node.visible and node.enabled and node.control_type == "Button"}
        return {"Start Voice", "Dictate"}.issubset(enabled) or (allow_busy and any(_matches(node, selectors["busy"], project="") for node in nodes))

    def validate(self, target="dsh") -> WindowInfo:
        if target != "gpt":
            return super().validate(target)
        selectors, _, _ = self._gpt_settings()
        info = self._guard(target="gpt")
        nodes = self._tree(info, "gpt")
        self._gpt_url(nodes, selectors)
        self._find(nodes, selectors["editor"], "", enabled=True)
        return info

    def _gpt_wait(self, info, timeout, condition):
        deadline = time.monotonic() + timeout
        while True:
            try:
                result = condition(self._tree(info, "gpt"))
            except Exception as error:
                if not _is_stale_uia_error(error):
                    raise
                result = False
            if result:
                return result
            if time.monotonic() >= deadline:
                raise DesktopError("GPT 语义发送未获得明确界面回执；已停止，不自动重试。")
            time.sleep(0.1)

    def send_gpt(self, message: str, event_id: str) -> None:
        selectors, mode, timeout = self._gpt_settings()
        wire = wire_message(message, mode)
        if not isinstance(event_id, str) or not event_id or len(event_id) > 200:
            raise DesktopError("事件编号无效。")
        info = self.validate("gpt")
        nodes = self._tree(info, "gpt")
        editor = self._find(nodes, selectors["editor"], "", enabled=True)
        if any(_matches(node, selectors["busy"], project="") for node in nodes) or not self._gpt_empty(nodes, selectors, editor):
            raise DesktopError("GPT 正在回复或存在未发送内容，保留界面，不输入。")
        raw_empty = self.automation.read_value(editor)
        baseline = {node.identity for node in nodes}
        with run_lock(self.runtime / ".uia-sender-lock"):
            receipt = self.runtime / "ui-receipts" / (hashlib.sha256(event_id.encode()).hexdigest() + ".json")
            receipt.parent.mkdir(parents=True, exist_ok=True)
            record = {"event_id": event_id, "target": "gpt", "mode": "uia", "window": info.hwnd,
                      "status": "started", "phase": "before_text", "server_received": None,
                      "wire_sha256": hashlib.sha256(wire.encode()).hexdigest()}
            try:
                with receipt.open("x", encoding="utf-8") as stream:
                    json.dump(record, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError as error:
                raise DesktopError("本事件已尝试过，禁止重复发送。") from error
            try:
                inputs = SemanticInput(self.backend, self.automation, lambda: self._guard(info, "gpt"), self.character_writer,
                                       paragraph_newline=True, readback_timeout=min(timeout, 5))
                wire = inputs.write_verified(info, editor, message, mode, expected_empty=raw_empty)
                record["text_match_mode"] = inputs.match_mode
                record["text_readback_attempts"] = inputs.readback_attempts
                record["phase"] = "typed_verified"
                write_json(receipt, record)
                def send_button(current):
                    self._gpt_url(current, selectors)
                    if any(_matches(node, selectors["busy"], project="") for node in current):
                        raise DesktopError("GPT 在发送前开始生成，停止操作。")
                    return self._find(current, selectors["send"], "", required=False, enabled=True)
                button = self._gpt_wait(info, timeout, send_button)
                current = self._tree(info, "gpt")
                self._gpt_url(current, selectors)
                editor = self._find(current, selectors["editor"], "", enabled=True)
                record["phase"] = "before_send"
                write_json(receipt, record)
                send_error = inputs.invoke_verified(editor, button, wire, selectors["send"].get("names", [selectors["send"].get("name")]))
                if send_error is not None:
                    record["send_invoke_warning"] = str(send_error)
                    write_json(receipt, record)
                def acknowledged(current):
                    self._gpt_url(current, selectors)
                    field = self._find(current, selectors["editor"], "", enabled=True)
                    if not self._gpt_empty(current, selectors, field, allow_busy=True):
                        return False
                    if any(_matches(node, selectors["busy"], project="") for node in current):
                        return "new_busy"
                    if "user_echo" in selectors:
                        for node in current:
                            if node.identity not in baseline and _matches(node, selectors["user_echo"], project="", message=wire) and wire in self.automation.read_texts(node):
                                return "new_user_echo"
                    return False
                acknowledgement = self._gpt_wait(info, timeout, acknowledged)
                record.update(status="send_action_completed", phase="acknowledged", acknowledgement=acknowledgement)
                write_json(receipt, record)
            except BaseException as error:
                record.update(status="uncertain", error=str(error) or type(error).__name__)
                write_json(receipt, record)
                raise
