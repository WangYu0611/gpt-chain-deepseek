from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from relay.browser import BrowserNeedsAttention
from relay.external_browser import ExternalBrowser
from relay.storage import read_json, write_text


class ExternalBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="relay-external-test-")
        self.runtime = Path(self.temporary.name)
        self.config = {"conversation_url": "https://chatgpt.com/c/fixture", "timeout_seconds": 0.03}
        self.browser = ExternalBrowser(self.config, self.runtime)
        self.attachment = self.runtime / "proof.txt"
        self.attachment.write_text("evidence", encoding="utf-8")
        self.request_path = self.runtime / "external" / "review-1" / "request.json"
        self.reply_path = self.request_path.with_name("reply.txt")

    def tearDown(self):
        self.temporary.cleanup()

    def create_request(self):
        with self.assertRaisesRegex(BrowserNeedsAttention, "超时"):
            self.browser.exchange("review prompt", [self.attachment], "review-1")

    def test_request_survives_timeout_with_absolute_paths(self):
        self.create_request()
        request = read_json(self.request_path)
        self.assertEqual(request["prompt"], "review prompt")
        self.assertEqual(request["attachments"], [str(self.attachment.resolve())])
        self.assertEqual(request["reply_path"], str(self.reply_path.resolve()))
        self.assertEqual(request["conversation_url"], self.config["conversation_url"])

    def test_pending_existing_request_is_not_rewritten(self):
        self.create_request()
        before = self.request_path.stat().st_mtime_ns
        with self.assertRaisesRegex(BrowserNeedsAttention, "超时"):
            self.browser.exchange("review prompt", [self.attachment], "review-1", pending=True)
        self.assertEqual(self.request_path.stat().st_mtime_ns, before)

    def test_changed_request_is_rejected_without_overwrite(self):
        self.create_request()
        before = self.request_path.read_bytes()
        with self.assertRaisesRegex(BrowserNeedsAttention, "内容发生变化"):
            self.browser.exchange("different prompt", [self.attachment], "review-1", pending=True)
        self.assertEqual(self.request_path.read_bytes(), before)

    def test_pending_without_request_never_creates_one(self):
        with self.assertRaisesRegex(BrowserNeedsAttention, "不会重建或重发"):
            self.browser.exchange("review prompt", [], "review-1", pending=True)
        self.assertFalse(self.request_path.exists())

    def test_existing_complete_reply_is_returned(self):
        self.create_request()
        reply = 'review text\n【交接】{"action":"pause"}【交接结束】'
        write_text(self.reply_path, reply)
        self.assertEqual(self.browser.exchange("review prompt", [self.attachment], "review-1", pending=True), reply)

    def test_reply_written_during_wait_is_returned(self):
        self.create_request()
        self.browser.timeout = 2
        reply = '【交接】{"action":"done"}【交接结束】'
        writer = threading.Thread(target=lambda: (time.sleep(0.03), write_text(self.reply_path, reply)))
        writer.start()
        try:
            self.assertEqual(self.browser.exchange("review prompt", [self.attachment], "review-1", pending=True), reply)
        finally:
            writer.join()

    def test_incomplete_reply_pauses_without_overwrite(self):
        self.create_request()
        write_text(self.reply_path, "【交接】{partial")
        with self.assertRaisesRegex(BrowserNeedsAttention, "不完整"):
            self.browser.exchange("review prompt", [self.attachment], "review-1", pending=True)

    def test_pause_stops_before_request_creation(self):
        (self.runtime / "PAUSE").touch()
        with self.assertRaisesRegex(BrowserNeedsAttention, "暂停标记"):
            self.browser.exchange("review prompt", [], "review-1")
        self.assertFalse(self.request_path.exists())

    def test_pause_is_checked_while_waiting(self):
        self.create_request()
        self.browser.timeout = 2
        writer = threading.Thread(target=lambda: (time.sleep(0.03), (self.runtime / "PAUSE").touch()))
        writer.start()
        try:
            with self.assertRaisesRegex(BrowserNeedsAttention, "暂停标记"):
                self.browser.exchange("review prompt", [self.attachment], "review-1", pending=True)
        finally:
            writer.join()

    def test_missing_attachment_and_invalid_timeouts_are_rejected(self):
        with self.assertRaisesRegex(BrowserNeedsAttention, "附件不存在"):
            self.browser.exchange("review prompt", [self.runtime / "missing.png"], "review-1")
        for timeout in (0, -1, "no", float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(BrowserNeedsAttention):
                ExternalBrowser({**self.config, "timeout_seconds": timeout}, self.runtime)


if __name__ == "__main__":
    unittest.main()
