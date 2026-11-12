"""Read-only completion observations from the calibrated desktop windows."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .windows import DesktopError, WindowInfo, find_window


DEFAULT_BUSY = [
    r"^\s*(?:stop(?:\s+(?:generating|generation|response))?|停止(?:生成|回复|响应)?)(?:\s*[（(].*[）)])?\s*$",
    r"^\s*[0-9]+\s*个后台任务运行中\s*$",
    r"^\s*(?:排队发送|插话发送)\s*$",
]
DEFAULT_IDLE = [r"^\s*(?:send(?:\s+(?:message|prompt))?|发送(?:消息)?|submit)\s*$"]
DEFAULT_BLOCKED = [r"^\s*(?:approve(?:\s+once)?|allow once|批准(?:一次)?|等待(?:用户)?回答)\s*$"]


@dataclass(frozen=True)
class Observation:
    target: str
    available: bool
    busy: bool | None
    text: str
    reason: str
    fingerprint: str
    idle: bool = False
    blocked: bool = False
    buttons: tuple[str, ...] = ()
    editable_text: str = ""
    editable_present: bool = False
    draft_present: bool = False
    blocked_controls: tuple[str, ...] = ()
    editor_source: str = ""
    draft_preview: str = ""
    reason_code: str = ""
    editor_candidates: tuple[dict, ...] = ()
    editor_state: str = ""


def _gpt_placeholder_structure(target: str, editable: str, candidates: tuple[dict, ...]) -> bool:
    """Recognize only the exact, fully read ProseMirror metadata from the probe."""
    placeholder = "Work with ChatGPT"
    if target != "gpt" or len(candidates) != 1 or editable.rstrip("\r\n") != placeholder:
        return False
    candidate = candidates[0]
    if not (
        candidate.get("type") == "ControlType.Edit"
        and isinstance(candidate.get("class_name"), str)
        and "ProseMirror" in candidate["class_name"].split()
        and candidate.get("name") == placeholder
        and candidate.get("has_value") is True
        and candidate.get("value_readonly") is False
        and candidate.get("read_pattern") == "ValuePattern"
        and candidate.get("result") == "readable"
        and candidate.get("value_length") == len(editable)
        and candidate.get("value_preview") == editable
        and isinstance(candidate.get("text_preview"), str)
        and len(candidate["text_preview"]) < 160
        and candidate["text_preview"].rstrip("\r\n") == placeholder
    ):
        return False
    return True


def _gpt_placeholder_is_empty(target: str, editable: str, candidates: tuple[dict, ...],
                              buttons: tuple[str, ...], enabled: tuple[str, ...],
                              send_patterns: list[re.Pattern] | None = None) -> bool:
    """Require separate idle controls to corroborate the known provider quirk."""
    if not _gpt_placeholder_structure(target, editable, candidates):
        return False
    if not {"Start Voice", "Dictate"}.issubset(enabled):
        return False
    # Check all visible send controls, including disabled ones. Do not infer
    # emptiness merely because sending is temporarily disabled.
    return not _matches(buttons, _patterns(None, DEFAULT_IDLE, "idle_regex") + (send_patterns or []))


def _patterns(value, defaults: list[str], name: str) -> list[re.Pattern]:
    value = defaults if value is None else value
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"completion.{name} 必须是正则字符串或非空正则字符串数组。")
    return [re.compile(item, re.IGNORECASE) for item in value]


def _matches(labels: tuple[str, ...], patterns: list[re.Pattern]) -> bool:
    return any(pattern.search(label) for pattern in patterns for label in labels)


def collect_ui(hwnd: int, *, editor_regex: str | None = None) -> dict:
    """A bounded, read-only UIA process; never invokes app controls or changes policy."""
    if os.name != "nt":
        raise DesktopError("窗口状态读取仅支持 Windows。")
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass",
               "-File", str(Path(__file__).with_name("read_ui.ps1")), "-WindowHandle", str(hwnd)]
    if editor_regex is not None:
        if not isinstance(editor_regex, str) or len(editor_regex) > 2000:
            raise ValueError("completion.editor_regex 必须是不超过 2000 字符的正则字符串。")
        command.extend(["-EditorRegex", editor_regex])
    result = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace",
                            timeout=12, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise DesktopError("UIAutomation 读取失败：" + (result.stderr.strip() or f"退出码 {result.returncode}")[:400])
    payload = json.loads(result.stdout.lstrip("\ufeff"))
    if not isinstance(payload, dict):
        raise DesktopError("UIAutomation 返回的内容不是对象。")
    return payload


class WindowObserver:
    def __init__(self, config: dict, backend=None, collector=None):
        self.config = config
        self.backend = backend
        self.collector = collector

    def _find(self, spec: dict) -> WindowInfo:
        return self.backend.find_window(spec) if self.backend is not None else find_window(spec)

    @staticmethod
    def _unavailable(target: str, reason: str, *, text: str = "", buttons=(), editable_text="", editable_present=False,
                     editor_source="", editor_candidates=(), reason_code="unavailable") -> Observation:
        return Observation(target, False, None, text, reason, "", buttons=tuple(buttons),
                           editable_text=editable_text, editable_present=editable_present,
                           editor_source=editor_source, draft_preview=editable_text[:160],
                           editor_candidates=tuple(editor_candidates), reason_code=reason_code)

    def snapshot(self, target: str) -> Observation:
        """Never focuses, clicks or types. Unknown/partial UI state cannot mean idle."""
        try:
            if target not in {"gpt", "dsh"}:
                raise ValueError("观察目标必须为 gpt 或 dsh。")
            section = self.config.get(target, {})
            if not isinstance(section, dict):
                raise ValueError(f"{target} 配置必须为对象。")
            calibration = section.get("calibration") or {}
            if not isinstance(calibration, dict):
                raise ValueError("窗口校准必须为对象。")
            size, dpi = calibration.get("client_size"), calibration.get("dpi")
            if not isinstance(size, (list, tuple)) or len(size) != 2 or any(type(n) is not int or n <= 0 for n in size) or type(dpi) is not int or dpi <= 0:
                raise ValueError(f"请先校准 {target} 的窗口大小和 DPI。")
            completion = section.get("completion") or {}
            if not isinstance(completion, dict):
                raise ValueError("completion 必须为对象。")
            busy_patterns = _patterns(completion.get("busy_regex"), DEFAULT_BUSY, "busy_regex")
            idle_patterns = _patterns(completion.get("idle_regex"), DEFAULT_IDLE, "idle_regex")
            blocked_patterns = _patterns(completion.get("blocked_regex"), DEFAULT_BLOCKED, "blocked_regex")
            spec = section.get("window", {})
            window = self._find(spec)
            if window.client_size != tuple(size) or window.dpi != dpi:
                raise DesktopError("窗口大小或 DPI 与校准不符，请重新校准。")
            payload = self.collector(window.hwnd) if self.collector is not None else collect_ui(window.hwnd, editor_regex=completion.get("editor_regex"))
            # Recheck identity and geometry after the out-of-process read as well.
            after = self._find(spec)
            if after.hwnd != window.hwnd or after.process_name != window.process_name or after.client_size != window.client_size or after.dpi != window.dpi:
                raise DesktopError("读取过程中目标窗口发生变化。")
            if not isinstance(payload, dict):
                raise ValueError("UIAutomation 返回的内容不是对象。")
            if payload.get("error"):
                raise DesktopError(str(payload["error"]))
            if payload.get("truncated") is not False:
                raise DesktopError("UIAutomation 读取超时、截断或未标明完整，不能判断完成。")
            text = payload.get("text")
            buttons = payload.get("buttons", [])
            enabled = payload.get("enabled_buttons", [])
            editable = payload.get("editable_text", "")
            editable_present = payload.get("editable_present", False)
            editor_source = payload.get("editor_source", "")
            candidates = payload.get("editor_candidates", [])
            if not isinstance(editor_source, str) or not isinstance(candidates, list) or any(not isinstance(item, dict) for item in candidates):
                raise ValueError("UIAutomation 编辑器诊断格式无效。")
            candidates = tuple({key: (value[:160] if isinstance(value, str) else value)
                                for key, value in item.items() if isinstance(value, (str, bool, int, float)) or value is None
                                or (key == "bounding_rect" and isinstance(value, list) and len(value) == 4 and all(isinstance(n, (int, float)) for n in value))}
                               for item in candidates[:8])
            if not isinstance(text, str) or not isinstance(editable, str) or not isinstance(editable_present, bool):
                raise ValueError("UIAutomation 文本或输入框信息无效。")
            if not isinstance(buttons, list) or not isinstance(enabled, list) or any(not isinstance(label, str) for label in buttons + enabled):
                raise ValueError("UIAutomation 按钮标签无效。")
            buttons = tuple(dict.fromkeys(label.strip() for label in buttons if label.strip()))
            enabled = tuple(label.strip() for label in enabled if label.strip())
            details = {"text": text, "buttons": buttons, "editable_text": editable,
                       "editable_present": editable_present, "editor_source": editor_source, "editor_candidates": candidates}
            if payload.get("editor_ambiguous") is True:
                return self._unavailable(target, "输入框文字与占位提示相同，无法区分真实草稿；保留原文等待诊断。",
                                         reason_code="editor_ambiguous", **details)
            if editable_present and (payload.get("editable_readable") is not True or "editable_text" not in payload):
                return self._unavailable(target, "当前消息输入框的草稿不可读，不能判断它是否为空。",
                                         reason_code="editor_unreadable", **details)
            if not text.strip():
                return self._unavailable(target, "未读取到可见对话文字。", reason_code="empty_text", **details)
            blocked_controls = tuple(label for label in enabled if _matches((label,), blocked_patterns))
            blocked = bool(blocked_controls)
            raw_editable = editable
            busy = _matches(buttons, busy_patterns)
            send_visible = _matches(buttons, _patterns(None, DEFAULT_IDLE, "idle_regex") + idle_patterns)
            placeholder_structure = not blocked and _gpt_placeholder_structure(target, editable, candidates)
            placeholder_busy = placeholder_structure and not send_visible and busy
            placeholder_confirmed = not blocked and not busy and _gpt_placeholder_is_empty(target, editable, candidates, buttons, enabled, idle_patterns)
            if placeholder_structure and not send_visible and not busy and not placeholder_confirmed:
                return self._unavailable(target, "识别到精确占位提示，但语音控件空态佐证不足；保留原文等待下一次读取。",
                                         reason_code="placeholder_unconfirmed", **details)
            if placeholder_confirmed or placeholder_busy:
                editable = ""
            draft_present = bool(editable.strip())
            # An enabled Send button can coexist with a running DSH response and
            # a draft. Only a positively readable, empty message editor proves
            # the input is clear; missing Stop alone does not establish idle.
            idle = not blocked and not busy and editable_present and not draft_present
            send_available = _matches(enabled, idle_patterns)
            fingerprint = hashlib.sha256(json.dumps({"text": text, "busy": busy, "idle": idle, "blocked": blocked,
                                                    "editable": editable}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            if blocked:
                reason = "检测到可用审批控件：" + "、".join(label[:80] for label in blocked_controls[:3])
                reason_code = "approval_controls"
            elif draft_present:
                reason = "检测到未发送文字，保留等待；不会覆盖或发送现有内容。"
                reason_code = "draft_present"
            elif busy:
                reason = "检测到正在生成或后台任务运行。"
                reason_code = "busy"
            elif idle:
                if placeholder_confirmed:
                    reason = "已确认 Work with ChatGPT 是 ProseMirror 空输入提示，未检测到真实草稿；仍须核对本轮完成标记。"
                    reason_code = "idle_placeholder"
                else:
                    reason = ("检测到可读空白消息框和可用发送按钮；" if send_available else "检测到可读空白消息框；") + "仍须由循环核对本轮完成标记。"
                    reason_code = "idle"
            else:
                return self._unavailable(target, "未识别到生成状态或空闲消息控件。", reason_code="editor_not_found", **details)
            return Observation(target, True, busy, text, reason, fingerprint, idle, blocked,
                               buttons, editable, editable_present, draft_present, blocked_controls, editor_source,
                               raw_editable[:160], reason_code, candidates,
                               "placeholder_busy" if placeholder_busy else "placeholder_confirmed" if placeholder_confirmed else "")
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, re.error, subprocess.SubprocessError) as error:
            return self._unavailable(target, f"无法可靠读取 {target}：{error}")
