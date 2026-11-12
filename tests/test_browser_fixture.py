"""Browser integration tests against an isolated local page, never ChatGPT.

Install Playwright and a browser first.  Optionally set
RELAY_TEST_BROWSER_CHANNEL=chrome to use an installed Chrome instead of the
Playwright Chromium download.
"""

from __future__ import annotations

import importlib.util
import base64
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from relay.browser import BrowserNeedsAttention, ChatGPTBrowser, message_marker, validate_conversation_url


HTML = """<!doctype html><html><body>
<div id="messages">
 <div data-message-author-role="user">old question</div>
 <div data-message-author-role="assistant">【交接】{"task_id":"OLD"}【交接结束】</div>
</div>
<textarea id="prompt-textarea"></textarea>
<input type="file" multiple>
<button data-testid="send-button">Send</button>
<button data-testid="stop-button" hidden>Stop</button>
<script>
window.sendCount = 0;
window.fixtureMode = 'reply';
window.addMessage = (role, text) => {
 const el = document.createElement('div');
 el.setAttribute('data-message-author-role', role);
 el.textContent = text;
 document.querySelector('#messages').appendChild(el);
 return el;
};
document.querySelector('[data-testid="send-button"]').onclick = () => {
 window.sendCount++;
 const composer = document.querySelector('#prompt-textarea');
 window.addMessage('user', composer.value);
 composer.value = '';
 if (window.fixtureMode === 'no-reply') return;
 document.querySelector('[data-testid="stop-button"]').hidden = false;
 setTimeout(() => {
  window.addMessage('assistant', 'review result\\n【交接】' +
    JSON.stringify({action:'execute',task_id:'NEXT',task_path:'Docs/next.md'}) + '【交接结束】');
  if (window.fixtureMode !== 'keep-generating')
    document.querySelector('[data-testid="stop-button"]').hidden = true;
 }, 60);
};
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class BrowserValidationTests(unittest.TestCase):
    def test_only_pinned_chatgpt_conversations_are_allowed(self):
        self.assertEqual(
            validate_conversation_url("https://chatgpt.com/c/example"),
            "https://chatgpt.com/c/example",
        )
        self.assertEqual(
            validate_conversation_url("https://chatgpt.com/g/project/c/example"),
            "https://chatgpt.com/g/project/c/example",
        )
        for url in (
            "https://evil.example/c/example", "http://chatgpt.com/c/example",
            "https://chatgpt.com/", "https://chatgpt.com.evil.example/c/example",
            "https://name:password@chatgpt.com/c/example", "http://localhost/c/example",
        ):
            with self.subTest(url=url), self.assertRaises(BrowserNeedsAttention):
                validate_conversation_url(url)

    def test_relay_marker_is_unambiguous(self):
        self.assertEqual(message_marker("round-1:review"), "[relay-id:round-1:review]")
        for invalid in ("", "bad]\ntext", "a" * 161):
            with self.assertRaises(BrowserNeedsAttention):
                message_marker(invalid)


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is not installed")
class BrowserFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="relay-browser-fixture-")
        self.browser = ChatGPTBrowser(
            {
                "conversation_url": f"http://127.0.0.1:{self.server.server_port}/c/fixture",
                "test_mode": True,
                "headless": True,
                "channel": os.environ.get("RELAY_TEST_BROWSER_CHANNEL"),
                "timeout_seconds": 1.2,
                "stable_seconds": 0.1,
                "poll_seconds": 0.05,
                "action_timeout_seconds": 2,
                "history_timeout_seconds": 2,
            },
            Path(self.directory.name),
        )
        try:
            self.browser.ensure_open()
        except BrowserNeedsAttention as exc:
            self.browser.close()
            self.directory.cleanup()
            raise unittest.SkipTest(f"Local browser unavailable: {exc}") from exc

    def tearDown(self):
        self.browser.close()
        self.directory.cleanup()

    def work_fixture(self):
        self.browser.page.evaluate("""() => {
            let sequence = 0;
            const workAdd = (role, text, state='complete') => {
                const turn = document.createElement('section');
                turn.setAttribute('data-talvt-turn-state', state);
                const message = document.createElement('div');
                message.setAttribute('data-content-search-unit-key',
                    `changing-prefix-${++sequence}:${sequence * 3}:${role}`);
                message.textContent = text;
                turn.appendChild(message);
                document.querySelector('#messages').appendChild(turn);
                return message;
            };
            const oldMessages = [...document.querySelectorAll('[data-message-author-role]')];
            for (const message of oldMessages) {
                workAdd(message.getAttribute('data-message-author-role'), message.textContent);
                message.remove();
            }
            const input = document.createElement('div');
            input.setAttribute('role', 'textbox');
            input.setAttribute('contenteditable', 'true');
            input.setAttribute('aria-label', 'Work with ChatGPT');
            document.querySelector('#prompt-textarea').replaceWith(input);
            window.addMessage = workAdd;
            document.querySelector('[data-testid="send-button"]').onclick = () => {
                window.sendCount++;
                workAdd('user', input.innerText);
                input.textContent = '';
                setTimeout(() => workAdd('assistant',
                    '【交接】{"task_id":"NEXT-WORK"}【交接结束】'), 60);
            };
        }""")

    def test_work_dom_composer_and_dynamic_message_keys(self):
        self.work_fixture()
        self.assertEqual(self.browser.page.locator('[data-message-author-role]').count(), 0)
        self.assertEqual(self.browser.page.locator('#prompt-textarea').count(), 0)
        reply = self.browser.exchange('please review', [], 'work-new')
        self.assertIn('"task_id":"NEXT-WORK"', reply)
        self.assertNotIn('"task_id":"OLD"', reply)
        self.assertEqual(self.browser.page.evaluate('window.sendCount'), 1)

    def test_work_pending_turn_waits_for_its_own_reply(self):
        self.work_fixture()
        self.browser.page.evaluate("""marker => {
            window.addMessage('user', marker);
            setTimeout(() => window.addMessage('assistant',
                '【交接】{"task_id":"MATCHING-WORK"}【交接结束】'), 150);
        }""", message_marker('work-pending'))
        reply = self.browser.exchange('please review', [], 'work-pending', pending=True)
        self.assertIn('"task_id":"MATCHING-WORK"', reply)
        self.assertEqual(self.browser.page.evaluate('window.sendCount'), 0)

    def test_work_incomplete_turn_is_not_accepted_without_stop_button(self):
        self.work_fixture()
        self.browser.page.evaluate("""marker => {
            window.addMessage('user', marker);
            window.addMessage('assistant',
                '【交接】{"task_id":"UNFINISHED"}【交接结束】', 'streaming');
        }""", message_marker('work-incomplete'))
        with self.assertRaisesRegex(BrowserNeedsAttention, '超时'):
            self.browser.exchange('please review', [], 'work-incomplete', pending=True)

    def test_send_and_read_only_the_matching_new_response(self):
        reply = self.browser.exchange("please review", [], "new-review")
        self.assertIn('"task_id":"NEXT"', reply)
        self.assertNotIn('"task_id":"OLD"', reply)
        self.assertEqual(self.browser.page.evaluate("window.sendCount"), 1)

    def test_pending_message_is_not_sent_twice(self):
        self.browser.page.evaluate(
            """marker => {
                window.addMessage('user', 'review ' + marker);
                window.addMessage('assistant', '【交接】{"action":"stop"}【交接结束】');
            }""", message_marker("already-sent")
        )
        reply = self.browser.exchange("please review", [], "already-sent", pending=True)
        self.assertIn('"action":"stop"', reply)
        self.assertEqual(self.browser.page.evaluate("window.sendCount"), 0)

    def test_pending_user_without_reply_waits_for_its_later_response(self):
        self.browser.page.evaluate(
            """marker => {
                window.addMessage('user', marker);
                setTimeout(() => window.addMessage('assistant',
                    '【交接】{"action":"pause"}【交接结束】'), 150);
            }""", message_marker("waiting-for-reply")
        )
        reply = self.browser.exchange("please review", [], "waiting-for-reply", pending=True)
        self.assertIn('"action":"pause"', reply)
        self.assertEqual(self.browser.page.evaluate("window.sendCount"), 0)

    def test_missing_pending_marker_pauses_without_send(self):
        with self.assertRaisesRegex(BrowserNeedsAttention, "不会自动重发"):
            self.browser.exchange("please review", [], "uncertain", pending=True)
        self.assertEqual(self.browser.page.evaluate("window.sendCount"), 0)

    def test_old_handoff_does_not_complete_a_new_turn(self):
        self.browser.page.evaluate("window.fixtureMode = 'no-reply'")
        with self.assertRaisesRegex(BrowserNeedsAttention, "超时"):
            self.browser.exchange("please review", [], "without-reply")

    def test_existing_draft_is_preserved(self):
        self.browser.page.locator("#prompt-textarea").fill("human draft")
        with self.assertRaisesRegex(BrowserNeedsAttention, "草稿"):
            self.browser.exchange("please review", [], "draft")
        self.assertEqual(self.browser.page.locator("#prompt-textarea").input_value(), "human draft")
        self.assertEqual(self.browser.page.evaluate("window.sendCount"), 0)

    def test_complete_handoff_while_generating_is_not_accepted(self):
        self.browser.page.evaluate("window.fixtureMode = 'keep-generating'")
        with self.assertRaisesRegex(BrowserNeedsAttention, "超时"):
            self.browser.exchange("please review", [], "still-generating")

    def test_later_user_message_pauses_instead_of_using_another_reply(self):
        self.browser.page.evaluate(
            """marker => {
                window.addMessage('user', marker);
                window.addMessage('user', 'a different human question');
                window.addMessage('assistant', '【交接】{"action":"execute"}【交接结束】');
            }""", message_marker("interrupted")
        )
        with self.assertRaisesRegex(BrowserNeedsAttention, "另一条用户消息"):
            self.browser.exchange("please review", [], "interrupted", pending=True)

    def test_attachment_is_uploaded_before_send(self):
        attachment = Path(self.directory.name) / "evidence.png"
        attachment.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
        ))
        reply = self.browser.exchange("please review", [attachment], "with-attachment")
        self.assertIn('"task_id":"NEXT"', reply)
        self.assertEqual(
            self.browser.page.locator("input[type='file']").evaluate("e => e.files[0].name"),
            "evidence.png",
        )

    def test_multiple_attachments_use_multiple_input_after_first_single(self):
        self.browser.page.evaluate("""() => {
            const single = document.createElement('input');
            single.type = 'file';
            document.querySelector('input[type=file]').before(single);
        }""")
        paths = [Path(self.directory.name) / name for name in ('report.txt', 'changes.txt')]
        for path in paths:
            path.write_text('fixture evidence', encoding='utf-8')
        self.browser.exchange('please review', paths, 'multiple-input')
        counts = self.browser.page.locator('input[type=file]').evaluate_all('inputs => inputs.map(e => e.files.length)')
        self.assertEqual(counts, [0, 2])

    def test_multiple_attachments_without_multiple_input_pause(self):
        self.browser.page.locator('input[type=file]').evaluate("e => e.removeAttribute('multiple')")
        paths = [Path(self.directory.name) / name for name in ('report.txt', 'changes.txt')]
        for path in paths:
            path.write_text('fixture evidence', encoding='utf-8')
        with self.assertRaisesRegex(BrowserNeedsAttention, '单文件上传入口'):
            self.browser.exchange('please review', paths, 'single-input-only')
        self.assertEqual(self.browser.page.evaluate('window.sendCount'), 0)
        self.assertEqual(self.browser.page.locator('input[type=file]').evaluate('e => e.files.length'), 0)

    def test_send_aria_label_fallback(self):
        self.browser.page.locator('[data-testid=send-button]').evaluate("""e => {
            e.removeAttribute('data-testid');
            e.type = 'submit';
            e.setAttribute('aria-label', 'Send');
        }""")
        reply = self.browser.exchange('please review', [], 'aria-send')
        self.assertIn('"task_id":"NEXT"', reply)
        self.assertEqual(self.browser.page.evaluate('window.sendCount'), 1)

    def test_stop_aria_label_fallback_blocks_early_acceptance(self):
        self.browser.page.locator('[data-testid=stop-button]').evaluate("""e => {
            e.removeAttribute('data-testid');
            e.setAttribute('aria-label', 'Stop');
            e.hidden = false;
        }""")
        self.browser.page.evaluate("""marker => {
            window.addMessage('user', marker);
            window.addMessage('assistant', '【交接】{"task_id":"UNFINISHED"}【交接结束】');
        }""", message_marker('aria-stop'))
        with self.assertRaisesRegex(BrowserNeedsAttention, '超时'):
            self.browser.exchange('please review', [], 'aria-stop', pending=True)

    def test_attachment_entry_opens_upload_menu_before_dynamic_input(self):
        self.browser.page.evaluate("""() => {
            document.querySelector('input[type=file]').remove();
            const attach = document.createElement('button');
            attach.setAttribute('aria-label', 'Add files and more');
            attach.textContent = '+';
            window.attachClicks = 0;
            window.uploadClicks = 0;
            attach.onclick = () => {
                window.attachClicks++;
                const upload = document.createElement('button');
                upload.textContent = 'Add photos & files Upload from computer';
                upload.onclick = () => {
                    window.uploadClicks++;
                    const input = document.createElement('input');
                    input.type = 'file';
                    input.multiple = true;
                    document.body.appendChild(input);
                };
                document.body.appendChild(upload);
            };
            document.body.appendChild(attach);
        }""")
        paths = [Path(self.directory.name) / name for name in ('report.txt', 'changes.txt')]
        for path in paths:
            path.write_text('fixture evidence', encoding='utf-8')
        self.browser.exchange('please review', paths, 'upload-menu')
        self.assertEqual(self.browser.page.evaluate('[window.attachClicks, window.uploadClicks]'), [1, 1])
        self.assertEqual(self.browser.page.locator('input[type=file]').evaluate('e => e.files.length'), 2)

    def test_visible_page_error_pauses_without_send(self):
        self.browser.page.evaluate("""() => {
            const alert = document.createElement('div');
            alert.setAttribute('role', 'alert');
            alert.textContent = 'fixture error';
            document.body.appendChild(alert);
        }""")
        with self.assertRaisesRegex(BrowserNeedsAttention, "网页显示错误"):
            self.browser.exchange("please review", [], "page-error")
        self.assertEqual(self.browser.page.evaluate("window.sendCount"), 0)


if __name__ == "__main__":
    unittest.main()
