"""Explicit one-shot messages to the configured GPT or DSH window."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from relay.storage import read_json, run_lock, write_json


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DirectRelay:
    """Persist a send attempt before touching a window; never retry it automatically."""

    def __init__(self, config: dict, runtime_dir: Path, sender: object):
        gpt = config.get("gpt", {})
        dsh = config.get("dsh", {})
        if not isinstance(gpt, dict) or not isinstance(dsh, dict):
            raise ValueError("gpt 和 dsh 配置必须为对象。")
        self.binding = {
            "gpt_url": gpt.get("url", ""),
            "dsh_project": dsh.get("project", ""),
            "dsh_project_path": dsh.get("project_path", ""),
        }
        if any(not isinstance(value, str) for value in self.binding.values()):
            raise ValueError("GPT 对话地址、DSH 项目名称和路径必须是文本。")
        if not self.binding["gpt_url"].strip() or not self.binding["dsh_project"].strip():
            raise ValueError("请填写 gpt.url 和 dsh.project。")
        self.binding = {key: value.strip() for key, value in self.binding.items()}
        self.runtime_dir = Path(runtime_dir)
        self.state_path = self.runtime_dir / "direct-state.json"
        self.sender = sender

    def _load(self) -> dict:
        if not self.state_path.exists():
            return {
                "version": 1, "binding": dict(self.binding),
                "pending": None, "last_action": None, "last_resolution": None,
            }
        state = read_json(self.state_path)
        if state.get("version") != 1:
            raise ValueError("直接传话状态版本不兼容，请使用新的运行目录。")
        if state.get("binding") != self.binding:
            raise ValueError("此运行目录已绑定其他 GPT 对话或 DSH 项目，请使用新的运行目录。")
        if state.get("pending") is not None and not isinstance(state["pending"], dict):
            raise ValueError("直接传话状态中的 pending 无效。")
        return state

    def _save(self, state: dict) -> None:
        state["updated_at"] = _now()
        write_json(self.state_path, state)

    def _paused(self) -> bool:
        return (self.runtime_dir / "PAUSE").exists()

    def send(self, target: str, message: str, execute: bool = False) -> dict:
        if target not in {"gpt", "dsh"}:
            raise ValueError("发送目标必须为 gpt 或 dsh。")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("发送消息不能为空，且必须是文本。")
        if not isinstance(execute, bool):
            raise ValueError("execute 必须明确指定 True 或 False。")
        if not execute:
            state = self._load()
            return {
                "status": "dry_run", "target": target, "message": message,
                "paused": self._paused(), "pending": state.get("pending"),
                "server_received": None,
            }
        # Share the controller lock with any earlier runner still using this runtime.
        with run_lock(self.runtime_dir / ".controller-lock"):
            state = self._load()
            if self._paused():
                return {"status": "paused", "reason": "检测到暂停标记，未操作窗口。", "server_received": None}
            if state.get("pending"):
                return {
                    "status": "uncertain", "pending": state["pending"],
                    "reason": "上次发送结果待人工确认，禁止继续发送或自动重试。",
                    "server_received": None,
                }
            # Calibration failure is known to precede input; do not reserve a send attempt.
            self.sender.validate(target)
            if self._paused():
                return {"status": "paused", "reason": "检测到暂停标记，未操作窗口。", "server_received": None}
            event_id = str(uuid.uuid4())
            action = {
                "event_id": event_id, "target": target, "message": message,
                "status": "pending", "created_at": _now(), "server_received": None,
            }
            state["pending"] = action
            self._save(state)
            try:
                getattr(self.sender, f"send_{target}")(message, event_id)
            except BaseException as error:
                action.update(status="uncertain", error=str(error) or type(error).__name__)
                self._save(state)
                if not isinstance(error, Exception):
                    raise
                return {
                    **action,
                    "reason": "发送动作结果不确定；请核对窗口后人工确认，脚本不会自动重发。",
                }
            action.update(status="send_action_completed", completed_at=_now())
            state["last_action"] = action
            state["pending"] = None
            self._save(state)
            return dict(action)

    def status(self) -> dict:
        """Read only the direct-send state, without creating a directory or taking a lock."""
        state = self._load() if self.state_path.exists() else None
        return {"runtime_dir": str(self.runtime_dir), "paused": self._paused(), "state": state}

    def resolve(self, sent: bool) -> dict:
        if not isinstance(sent, bool):
            raise ValueError("sent 必须明确指定 True（人工确认已发送）或 False（跳过）。")
        with run_lock(self.runtime_dir / ".controller-lock"):
            state = self._load()
            pending = state.get("pending")
            if not pending:
                return {"status": "no_pending", "server_received": None}
            result = {
                "status": "resolved_sent" if sent else "resolved_skipped",
                "event_id": pending["event_id"], "target": pending["target"],
                "human_confirmed_sent": sent, "server_received": None,
                "resolved_at": _now(),
            }
            state["last_action"] = {**pending, **result}
            state["last_resolution"] = result
            state["pending"] = None
            self._save(state)
            return dict(result)
