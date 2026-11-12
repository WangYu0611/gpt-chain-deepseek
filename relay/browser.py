"""Small, conservative browser adapter for one existing ChatGPT conversation.

Playwright is imported only when a browser is opened.  This module never uses
private ChatGPT endpoints or reads browser cookies/authentication files.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class BrowserNeedsAttention(RuntimeError):
    """The bridge must pause rather than guess or repeat a possible send."""


DEFAULT_SELECTORS = {
    "composer": "#prompt-textarea, textarea[data-id='root'], [role='textbox'][contenteditable='true']",
    "send_button": "button[data-testid='send-button'], button[type='submit'][aria-label='Send']",
    "stop_button": (
        "button[data-testid='stop-button'], "
        "button[aria-label='Stop streaming'], button[aria-label='停止生成'], button[aria-label='Stop']"
    ),
    "assistant_messages": "[data-message-author-role='assistant'], [data-content-search-unit-key$=':assistant']",
    "user_messages": "[data-message-author-role='user'], [data-content-search-unit-key$=':user']",
    "file_input": "input[type='file']",
    "attach_button": "button[data-testid='composer-plus-btn'], button[aria-label='Add files and more']",
    "attach_file_menu_item": "button:has-text('Add photos & files')",
    "uploading": "[data-testid='file-upload-progress'], [aria-label='Uploading']",
    "error": "[data-testid='conversation-turn-error'], [role='alert']",
    "history_ready": "[data-message-author-role], [data-content-search-unit-key$=':user'], [data-content-search-unit-key$=':assistant']",
}


def validate_conversation_url(url: str, *, test_mode: bool = False) -> str:
    """Allow a pinned conversation, never a login/redirect/arbitrary website."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise BrowserNeedsAttention("GPT 对话网址格式无效。") from exc
    local_test = test_mode and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    production = (
        parsed.scheme == "https"
        and parsed.hostname == "chatgpt.com"
        and port in {None, 443}
    )
    if parsed.username or parsed.password or not (production or local_test):
        raise BrowserNeedsAttention("GPT 对话网址必须是 https://chatgpt.com 的具体对话链接。")
    if local_test and parsed.scheme not in {"http", "https"}:
        raise BrowserNeedsAttention("本地测试网址必须使用 HTTP 或 HTTPS。")
    if not re.search(r"/c/[^/]+/?$", parsed.path):
        raise BrowserNeedsAttention("请配置具体 GPT 对话网址（路径中应有 /c/对话编号），而不是首页。")
    return url


def message_marker(relay_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", relay_id):
        raise BrowserNeedsAttention("relay_id 只能包含字母、数字、下划线、点、冒号或短横线。")
    return f"[relay-id:{relay_id}]"


class ChatGPTBrowser:
    """Send once, then wait for the matching assistant's complete handoff.

    ``pending=True`` means the caller has a possibly-sent message on disk.  If
    its marker is not in the loaded conversation, this adapter stops; it never
    retries an uncertain send.  Persist that intent *before* calling exchange.
    """

    def __init__(self, config: dict[str, Any], runtime_dir: Path):
        self.config = dict(config)
        self.runtime_dir = Path(runtime_dir).resolve()
        self.url = validate_conversation_url(
            str(config.get("conversation_url", "")),
            test_mode=bool(config.get("test_mode", False)),
        )
        self.selectors = {**DEFAULT_SELECTORS, **config.get("selectors", {})}
        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None
        self._attached = bool(config.get("cdp_url"))

    def __enter__(self) -> "ChatGPTBrowser":
        self.ensure_open()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def ensure_open(self) -> None:
        if self.page is not None and not self.page.is_closed():
            self._assert_expected_page()
            return
        if self._playwright is not None:
            self.close()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserNeedsAttention(
                "缺少 Playwright。请安装 requirements.txt，并安装浏览器或配置 channel。"
            ) from exc
        try:
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            self._playwright = sync_playwright().start()
            cdp_url = self.config.get("cdp_url")
            if cdp_url:
                parsed = urlsplit(str(cdp_url))
                if (
                    parsed.scheme not in {"http", "https", "ws", "wss"}
                    or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                    or parsed.username
                    or parsed.password
                ):
                    raise BrowserNeedsAttention("CDP 连接只允许本机地址，且不能在网址中放入凭据。")
                self._browser = self._playwright.chromium.connect_over_cdp(str(cdp_url))
                contexts = self._browser.contexts
                if not contexts:
                    raise BrowserNeedsAttention("CDP 浏览器没有可用窗口。")
                self._context = contexts[0]
            else:
                configured_profile = self.config.get("profile_dir")
                profile = (
                    Path(configured_profile).expanduser().resolve()
                    if configured_profile
                    else self.runtime_dir / "browser-profile"
                )
                options: dict[str, Any] = {
                    "headless": bool(self.config.get("headless", False)),
                    "accept_downloads": False,
                }
                if self.config.get("channel"):
                    options["channel"] = self.config["channel"]
                self._context = self._playwright.chromium.launch_persistent_context(
                    str(profile), **options
                )
            self._context.set_default_timeout(
                float(self.config.get("action_timeout_seconds", 30)) * 1000
            )
            self.page = next(
                (p for p in self._context.pages if self._same_conversation(p.url)), None
            )
            if self.page is None:
                self.page = self._context.new_page()
                self.page.goto(
                    self.url,
                    wait_until="domcontentloaded",
                    timeout=float(self.config.get("navigation_timeout_seconds", 60)) * 1000,
                )
            self.page.bring_to_front()
        except BrowserNeedsAttention:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise BrowserNeedsAttention(
                "无法打开传话浏览器。检查浏览器是否已安装、独立 profile 是否被占用，"
                "或 CDP 是否开启。"
            ) from exc

    def login(self, wait_for_enter: bool = True) -> None:
        """Open the dedicated profile; the human signs in in the visible browser."""
        if self.page is None or self.page.is_closed():
            self.ensure_open()
        if wait_for_enter:
            input("请在浏览器中完成登录并打开目标对话，然后在这里按 Enter：")
        self._assert_expected_page()
        self._wait_for_composer()

    def close(self) -> None:
        # Never call browser.close/context.close on an attached user's browser.
        # Stopping our Playwright driver simply disconnects that CDP client.
        try:
            if self._context is not None and not self._attached:
                self._context.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()
            self.page = None
            self._context = None
            self._browser = None
            self._playwright = None

    def exchange(
        self,
        prompt: str,
        attachments: list[Path],
        relay_id: str,
        pending: bool = False,
    ) -> str:
        """Return the full matching response, or raise to pause the workflow."""
        marker = message_marker(relay_id)
        if not prompt.strip():
            raise BrowserNeedsAttention("拒绝发送空消息。")
        try:
            self.ensure_open()
            self._assert_expected_page()
            self._wait_for_composer()
            self._wait_for_history()
            thread = self._read_thread(marker)
            if not thread["found"]:
                if pending:
                    raise BrowserNeedsAttention(
                        "这条消息之前可能已发送，但当前加载的对话中找不到交接编号。"
                        "已暂停，需人工核对；不会自动重发。"
                    )
                self._send_once(prompt, attachments, marker)
            return self._wait_for_reply(marker)
        except BrowserNeedsAttention as exc:
            screenshot = self._capture_failure()
            suffix = f" 截图：{screenshot}" if screenshot else ""
            raise BrowserNeedsAttention(str(exc) + suffix) from exc
        except Exception as exc:
            screenshot = self._capture_failure()
            suffix = f" 截图：{screenshot}" if screenshot else ""
            raise BrowserNeedsAttention(
                "网页操作中断；消息可能已经发送。已暂停，恢复时应核对当前交接编号，"
                "不要直接创建新一轮。" + suffix
            ) from exc

    def _same_conversation(self, url: str) -> bool:
        current, expected = urlsplit(url), urlsplit(self.url)
        return (
            current.scheme == expected.scheme
            and current.netloc == expected.netloc
            and current.path.rstrip("/") == expected.path.rstrip("/")
        )

    def _assert_expected_page(self) -> None:
        if self.page is None or self.page.is_closed():
            raise BrowserNeedsAttention("传话浏览器已关闭。")
        if not self._same_conversation(self.page.url):
            raise BrowserNeedsAttention("浏览器不在指定 GPT 对话中；请完成登录并回到该对话。")

    def _locator(self, name: str):
        selector = str(self.selectors.get(name, ""))
        if not selector:
            return None
        return self.page.locator(selector)

    def _visible(self, name: str) -> bool:
        locator = self._locator(name)
        return bool(locator and any(locator.nth(i).is_visible() for i in range(locator.count())))

    def _raise_page_error(self) -> None:
        if self._visible("error"):
            raise BrowserNeedsAttention("网页显示错误或提示，需要人工处理后继续。")

    def _wait_for_composer(self) -> None:
        composer = self._locator("composer")
        if composer is None:
            raise BrowserNeedsAttention("未配置 GPT 输入框选择器。")
        try:
            composer.first.wait_for(state="visible")
        except Exception as exc:
            raise BrowserNeedsAttention("找不到 GPT 输入框；请检查登录状态或更新网页选择器。") from exc
        self._raise_page_error()

    def _wait_for_history(self) -> None:
        history = self._locator("history_ready")
        if history is None:
            return
        try:
            history.first.wait_for(
                state="attached",
                timeout=float(self.config.get("history_timeout_seconds", 20)) * 1000,
            )
        except Exception as exc:
            raise BrowserNeedsAttention("目标对话的历史消息尚未加载，已暂停以避免重复发送。") from exc

    def _read_thread(self, marker: str) -> dict[str, Any]:
        # DOM order binds the reply to our user message, rather than accepting
        # an old complete handoff elsewhere in a long conversation.
        result = self.page.evaluate(
            """({userSelector, assistantSelector, marker}) => {
                const users = [...document.querySelectorAll(userSelector)];
                const matching = users.filter(e => e.textContent.includes(marker));
                if (matching.length !== 1) return {found:false, matches:matching.length};
                const user = matching[0];
                const nextUser = users.find(e => e !== user &&
                    (user.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING));
                const assistants = [...document.querySelectorAll(assistantSelector)].filter(e =>
                    (user.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING) &&
                    (!nextUser || (e.compareDocumentPosition(nextUser) & Node.DOCUMENT_POSITION_FOLLOWING))
                );
                const assistant = assistants.length ? assistants[assistants.length - 1] : null;
                const turn = assistant && assistant.closest('[data-talvt-turn-state]');
                return {found:true, matches:1, next_user:!!nextUser,
                    text: assistant ? assistant.innerText : '',
                    turn_complete: turn ? turn.getAttribute('data-talvt-turn-state') === 'complete' : null};
            }""",
            {
                "userSelector": self.selectors["user_messages"],
                "assistantSelector": self.selectors["assistant_messages"],
                "marker": marker,
            },
        )
        if result.get("matches", 0) > 1:
            raise BrowserNeedsAttention("对话中出现重复的交接编号，已暂停等待人工核对。")
        return result

    def _send_once(self, prompt: str, attachments: list[Path], marker: str) -> None:
        self._raise_page_error()
        if self._visible("stop_button"):
            raise BrowserNeedsAttention("GPT 正在处理其他消息，等待该轮结束后再恢复。")
        composer = self._locator("composer").first
        draft = composer.evaluate("e => String('value' in e ? e.value : e.innerText || '').trim()")
        if draft:
            raise BrowserNeedsAttention("GPT 输入框中已有草稿，已暂停以免覆盖。")
        paths = [Path(path).resolve() for path in attachments]
        for path in paths:
            if not path.is_file():
                raise BrowserNeedsAttention(f"附件不存在：{path}")
        if paths:
            self._attach_files(paths)
        content = prompt if marker in prompt else f"{prompt.rstrip()}\n\n{marker}"
        composer.fill(content)
        send = self._locator("send_button")
        if send is None:
            raise BrowserNeedsAttention("未配置 GPT 发送按钮选择器。")
        deadline = time.monotonic() + float(self.config.get("upload_timeout_seconds", 180))
        ready_since = None
        while time.monotonic() < deadline:
            self._assert_expected_page()
            self._raise_page_error()
            ready = (
                send.first.is_visible()
                and send.first.is_enabled()
                and not self._visible("uploading")
            )
            if ready:
                ready_since = ready_since or time.monotonic()
                if time.monotonic() - ready_since >= (1.5 if paths else 0):
                    break
            else:
                ready_since = None
            self.page.wait_for_timeout(250)
        else:
            raise BrowserNeedsAttention("等待附件上传或发送按钮超时，消息尚未确认发送。")
        # Exactly one call; an uncertain click is never retried here.
        send.first.click()
        deadline = time.monotonic() + float(self.config.get("action_timeout_seconds", 30))
        while time.monotonic() < deadline:
            self._assert_expected_page()
            if self._read_thread(marker)["found"]:
                return
            self.page.wait_for_timeout(250)
        raise BrowserNeedsAttention("点击发送后未能确认消息出现；已暂停，不会重发。")

    def _attach_files(self, paths: list[Path]) -> None:
        file_input = self._locator("file_input")
        if file_input is None:
            raise BrowserNeedsAttention("未配置网页附件输入框选择器。")
        if not file_input.count():
            attach = self._locator("attach_button")
            if attach is None or not attach.count():
                raise BrowserNeedsAttention("找不到网页附件入口；请更新附件选择器。")
            attach.first.click()
            menu_item = self._locator("attach_file_menu_item")
            if menu_item is not None:
                menu_item.first.click()
            file_input.first.wait_for(state="attached")
        multiple = [file_input.nth(i) for i in range(file_input.count())
                    if file_input.nth(i).evaluate("e => Boolean(e.multiple)")]
        if len(paths) > 1 and not multiple:
            raise BrowserNeedsAttention("网页只有单文件上传入口，无法一次发送多份证据；请更新附件选择器。")
        target = multiple[0] if multiple else file_input.first
        target.set_input_files([str(path) for path in paths])

    def _wait_for_reply(self, marker: str) -> str:
        deadline = time.monotonic() + float(self.config.get("timeout_seconds", 1800))
        stable_seconds = max(0.1, float(self.config.get("stable_seconds", 3)))
        poll_ms = max(50, float(self.config.get("poll_seconds", 1)) * 1000)
        previous = None
        stable_since = time.monotonic()
        while time.monotonic() < deadline:
            self._assert_expected_page()
            self._raise_page_error()
            thread = self._read_thread(marker)
            if not thread["found"]:
                raise BrowserNeedsAttention("当前交接消息从页面中消失，已暂停等待人工核对。")
            if thread.get("next_user"):
                raise BrowserNeedsAttention("当前交接后出现另一条用户消息，已暂停以免串轮。")
            text = str(thread.get("text", "")).strip()
            generating = self._visible("stop_button") or thread.get("turn_complete") is False
            if text != previous or generating:
                previous, stable_since = text, time.monotonic()
            complete = "【交接】" in text and "【交接结束】" in text
            if complete and not generating and time.monotonic() - stable_since >= stable_seconds:
                return text
            self.page.wait_for_timeout(poll_ms)
        raise BrowserNeedsAttention("等待 GPT 完整交接回复超时，已暂停；不会发送下一任务。")

    def _capture_failure(self) -> Path | None:
        if self.page is None or self.page.is_closed():
            return None
        try:
            directory = self.runtime_dir / "browser-errors"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{time.time_ns()}.png"
            self.page.screenshot(path=str(path), full_page=False)
            return path
        except Exception:
            return None
