"""Calibrated mouse/keyboard macros; no browser profile, CLI agent or model API."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from relay.storage import write_json
from .windows import DesktopError, NativeDesktop, WindowInfo, _unicode_batches


class WindowSender:
    def __init__(self, config: dict, runtime_dir: Path, backend=None, observer=None):
        self.config = config
        self.runtime = Path(runtime_dir)
        self.backend = backend  # Lazy: preview never creates a Windows backend.
        self.observer = observer  # Lazy: doctor/calibration never reads app content.

    def _check_pause(self) -> None:
        if (self.runtime / "PAUSE").exists():
            raise DesktopError("检测到暂停文件，已停止窗口操作。")

    @staticmethod
    def _point(value, size: tuple[int, int], label: str) -> tuple[int, int]:
        if not isinstance(value, (list, tuple)) or len(value) != 2 or any(type(x) is not int for x in value):
            raise DesktopError(f"{label} 尚未校准。")
        point = tuple(value)
        if not (0 <= point[0] < size[0] and 0 <= point[1] < size[1]):
            raise DesktopError(f"{label} 超出窗口客户区，请重新校准。")
        return point

    def _plan(self, target: str):
        if target not in ("gpt", "dsh"):
            raise DesktopError("窗口目标必须为 gpt 或 dsh。")
        section = self.config.get(target, {})
        if not isinstance(section, dict):
            raise DesktopError(f"{target} 配置必须为对象。")
        verification = section.get("input_verification", self.config.get("input_verification", True))
        if not isinstance(verification, bool):
            raise DesktopError("input_verification 必须为 true 或 false。")
        calibration = section.get("calibration") or {}
        if not isinstance(calibration, dict):
            raise DesktopError(f"{target}.calibration 必须为对象。")
        size = calibration.get("client_size")
        if not isinstance(size, (list, tuple)) or len(size) != 2 or any(type(x) is not int or x <= 0 for x in size):
            raise DesktopError(f"请先校准 {target} 的窗口大小。")
        size = tuple(size)
        dpi = calibration.get("dpi")
        if type(dpi) is not int or dpi <= 0:
            raise DesktopError(f"请先校准 {target} 的 DPI。")
        input_point = self._point(calibration.get("input"), size, f"{target} 输入框")
        send_point = calibration.get("send")
        if send_point is not None:
            send_point = self._point(send_point, size, f"{target} 发送按钮")
        steps = calibration.get("before_input", [])
        if not isinstance(steps, list):
            raise DesktopError("before_input 必须为步骤数组。")
        validated = []
        for step in steps:
            if not isinstance(step, dict) or step.get("action", "click") not in ("click", "move"):
                raise DesktopError("校准步骤只允许 click 或 move。")
            wait = step.get("wait_seconds", 0.5)
            if isinstance(wait, bool) or not isinstance(wait, (float, int)) or not 0 <= wait <= 30:
                raise DesktopError("每个校准步骤的等待时间必须在 0–30 秒之间。")
            validated.append({**step, "action": step.get("action", "click"),
                              "point": self._point(step.get("point"), size, "步骤点击点"), "wait_seconds": float(wait)})
        if target == "gpt":
            if section.get("dedicated_input") is not True:
                raise DesktopError("请先清空 GPT 输入框并确认 dedicated_input=true；运行期间不要在该输入框写草稿。")
            if validated:
                raise DesktopError("GPT 只允许操作指定对话输入框，不执行 before_input 步骤。")
        else:
            if not section.get("project") or calibration.get("project") != section["project"]:
                raise DesktopError("DSH 项目与校准绑定不一致，请针对指定项目重新校准。")
            if sum(step.get("role") == "new_task" for step in validated) != 1:
                raise DesktopError("DSH 校准必须且只能包含一个 role=new_task 的新任务入口。")
            if any(step.get("role") == "new_task" and step["action"] != "click" for step in validated):
                raise DesktopError("DSH 新任务入口必须校准为 click 动作。")
        return section, size, dpi, input_point, send_point, validated

    def validate(self, target: str) -> WindowInfo:
        """Read-only doctor check; never focuses, clicks, types or reserves an event."""
        section, size, dpi, *_ = self._plan(target)
        if self.backend is None:
            self.backend = NativeDesktop(abort=self._check_pause)
        window = self.backend.find_window(section.get("window", {}))
        if window.client_size != size or window.dpi != dpi:
            raise DesktopError("实际窗口大小或 DPI 与校准记录不符，请重新校准。")
        return window

    def send_gpt(self, message: str, event_id: str) -> None:
        self._send("gpt", message, event_id)

    def send_dsh(self, message: str, event_id: str) -> None:
        self._send("dsh", message, event_id)

    @staticmethod
    def _normalized_text(text: str) -> str:
        # Rich editors may add a final paragraph newline. Preserve every other
        # character, including spaces, tabs and invisible characters.
        return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")

    @classmethod
    def _match_text(cls, actual: str, expected: str) -> str | None:
        if actual == expected:
            return "exact"
        if cls._normalized_text(actual) == cls._normalized_text(expected):
            return "line_endings"
        # Accessibility providers may put an auto-linked URL in its own range,
        # inserting whitespace absent from the actual draft. Require the entire
        # non-whitespace character sequence, preserving order, case and tokens.
        actual_characters = "".join(character for character in actual if not character.isspace())
        expected_characters = "".join(character for character in expected if not character.isspace())
        if actual_characters == expected_characters:
            return "accessibility_whitespace"
        return None

    @staticmethod
    def _field(observation, name: str, default=None):
        return observation.get(name, default) if isinstance(observation, dict) else getattr(observation, name, default)

    def _read_editor(self, target: str, *, after_send: bool):
        self._check_pause()
        if self.observer is None:
            from .observer import WindowObserver
            self.observer = WindowObserver(self.config)
        try:
            observed = self.observer.snapshot(target)
        except Exception as error:
            label = "发送后未能核对目标消息框状态" if after_send else "未能在目标消息框核对输入内容，请检查输入点位"
            raise DesktopError(label + "；已停止且不会重试。") from error
        self._check_pause()
        return observed

    def _verify_typed(self, target: str, message: str) -> str:
        observed = self._read_editor(target, after_send=False)
        actual = self._field(observed, "editable_text")
        match_mode = self._match_text(actual, message) if isinstance(actual, str) else None
        if not (
            self._field(observed, "available") is True
            and self._field(observed, "editable_present") is True
            and self._field(observed, "blocked", False) is False
            and self._field(observed, "busy") is False
            and match_mode is not None
        ):
            raise DesktopError("未能在目标消息框核对输入内容，请检查输入点位；未执行发送，也不会重试。")
        return match_mode

    def _verify_submitted(self, target: str, message: str) -> str:
        observed = self._read_editor(target, after_send=True)
        actual = self._field(observed, "editable_text")
        readable = (self._field(observed, "available") is True
                    and self._field(observed, "editable_present") is True and isinstance(actual, str))
        if isinstance(actual, str) and self._match_text(actual, message) is not None:
            raise DesktopError("发送动作未生效：完整原文仍在目标消息框内；已停止且不会重发。")
        # Generation can replace the composer with Stop, or expose its empty
        # placeholder again. It must be a new busy state: pre-send verification
        # already required busy=False and matched the entire typed message.
        if (self._field(observed, "available") is True and self._field(observed, "busy") is True
                and self._field(observed, "blocked", False) is False):
            return "busy_after_send"
        if not readable or self._normalized_text(actual) != "" or self._field(observed, "blocked", False) is True:
            raise DesktopError("发送后未能确认目标消息框已清空；请人工核对消息，脚本不会重发。")
        return "editor_cleared"

    def _send(self, target: str, message: str, event_id: str) -> None:
        self._check_pause()
        section, size, dpi, input_point, send_point, steps = self._plan(target)
        verify_input = section.get("input_verification", self.config.get("input_verification", True))
        _unicode_batches(message)  # Validate all text before any click/card creation.
        if not isinstance(event_id, str) or not event_id or len(event_id) > 200:
            raise DesktopError("窗口事件编号无效。")
        window: WindowInfo = self.validate(target)
        receipt = self.runtime / "ui-receipts" / (hashlib.sha256(event_id.encode()).hexdigest() + ".json")
        receipt.parent.mkdir(parents=True, exist_ok=True)
        record = {"event_id": event_id, "target": target, "window": window.hwnd,
                  "status": "started", "phase": "before_activation", "time": time.time(),
                  "input_verification": verify_input, "server_received": None}
        try:
            with receipt.open("x", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as error:
            raise DesktopError("这个事件已经尝试过窗口输入，请先核对界面；不会重复开卡或发送。") from error
        try:
            self.backend.activate(window)
            for index, step in enumerate(steps):
                self._check_pause()
                getattr(self.backend, step["action"])(window, step["point"])
                record["phase"] = f"before_input_{index}_{step.get('role', 'point')}"
                write_json(receipt, record)
                self.backend.wait(window, step["wait_seconds"])
            self._check_pause()
            self.backend.click(window, input_point)
            self.backend.wait(window, 0.15)
            self.backend.type_unicode(window, message)
            record["phase"] = "typed_unverified"
            write_json(receipt, record)
            self.backend.wait(window, 0.3)
            self._check_pause()
            if verify_input:
                match_mode = self._verify_typed(target, message)
                record.update(phase="input_verified", input_verified=True, match_mode=match_mode)
                write_json(receipt, record)
            if send_point is None:
                self.backend.enter(window)
            else:
                self.backend.click(window, send_point)
            record.update(phase="send_requested")
            write_json(receipt, record)
            if verify_input:
                self.backend.wait(window, 0.5)
                evidence = self._verify_submitted(target, message)
                record.update(phase="send_verified", submission_verified=True, submission_evidence=evidence)
            record.update(status="send_action_completed")
            write_json(receipt, record)
        except BaseException as error:
            record.update(status="uncertain", error=str(error) or type(error).__name__)
            write_json(receipt, record)
            raise
