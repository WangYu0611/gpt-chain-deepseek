"""Local-file handoff for a human or an already connected browser assistant.

This adapter starts no browser, reads no credentials, and sends no messages.
The bridge's existing run lock serializes request creation and state recovery.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from .browser import BrowserNeedsAttention, validate_conversation_url
from .protocol import ProtocolError, extract_block
from .storage import read_json, write_json


class ExternalBrowser:
    def __init__(self, config: dict, runtime_dir: Path):
        self.config = config
        self.runtime = Path(runtime_dir).resolve()
        self.url = validate_conversation_url(str(config.get("conversation_url", "")))
        try:
            self.timeout = float(config.get("timeout_seconds", 1800))
        except (TypeError, ValueError) as error:
            raise BrowserNeedsAttention("browser.timeout_seconds 必须为正数。") from error
        if not 0 < self.timeout < float("inf"):
            raise BrowserNeedsAttention("browser.timeout_seconds 必须为有限正数。")

    def close(self) -> None:
        pass

    def exchange(self, prompt: str, attachments: list[Path], relay_id: str, pending: bool = False) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise BrowserNeedsAttention("拒绝创建空传话请求。")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", relay_id):
            raise BrowserNeedsAttention("无效的外部传话编号。")
        self._check_pause()
        paths = [Path(path).resolve() for path in attachments]
        if any(not path.is_file() for path in paths):
            raise BrowserNeedsAttention("外部传话附件不存在，不能创建请求。")
        directory = self.runtime / "external" / relay_id
        request_path, reply_path = directory / "request.json", directory / "reply.txt"
        request = {
            "prompt": prompt, "attachments": [str(path) for path in paths],
            "relay_id": relay_id, "conversation_url": self.url, "reply_path": str(reply_path),
        }
        if request_path.exists():
            try:
                existing = read_json(request_path)
            except (OSError, ValueError) as error:
                raise BrowserNeedsAttention("既有外部请求损坏；已暂停，不会覆盖或重发。") from error
            if existing != request:
                raise BrowserNeedsAttention("同一编号的外部请求内容发生变化；已暂停，不会覆盖或重发。")
        elif pending:
            raise BrowserNeedsAttention("之前可能已发送，但找不到既有外部请求；已暂停，不会重建或重发。")
        else:
            if reply_path.exists():
                raise BrowserNeedsAttention("存在无法绑定到请求的回复文件，已暂停。")
            write_json(request_path, request)
        print(f"外部网页传话请求：{request_path}\n请核对编号后操作已登录的目标对话，将完整回复原子写入：{reply_path}", flush=True)
        deadline = time.monotonic() + self.timeout
        while True:
            self._check_pause()
            if reply_path.exists():
                try:
                    answer = reply_path.read_text(encoding="utf-8-sig")
                    extract_block(answer, "【交接】", "【交接结束】")
                except (OSError, UnicodeError, ProtocolError) as error:
                    raise BrowserNeedsAttention("外部回复文件不完整或不是有效 JSON 交接；已暂停，请核对原网页回复。") from error
                return answer
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserNeedsAttention("等待外部网页回复超时；请求已保留，不会重复创建。")
            time.sleep(min(1.0, remaining))

    def _check_pause(self) -> None:
        if (self.runtime / "PAUSE").exists():
            raise BrowserNeedsAttention("检测到暂停标记，外部网页传话已停止；既有请求保留。")
