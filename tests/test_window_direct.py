from __future__ import annotations

import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from relay.storage import run_lock
from windowrelay.direct import DirectRelay


class FakeSender:
    def __init__(self):
        self.validated = []
        self.calls = []
        self.validation_error = None
        self.error = None
        self.on_validate = None
        self.before_send = None

    def validate(self, target):
        self.validated.append(target)
        if self.validation_error:
            raise self.validation_error
        if self.on_validate:
            self.on_validate()

    def send_gpt(self, message, event_id):
        self._send("gpt", message, event_id)

    def send_dsh(self, message, event_id):
        self._send("dsh", message, event_id)

    def _send(self, target, message, event_id):
        if self.before_send:
            self.before_send(target, message, event_id)
        self.calls.append((target, message, event_id))
        if self.error:
            raise self.error


class DirectRelayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name) / "fresh-direct-runtime"
        self.config = {
            "gpt": {"url": "https://chatgpt.com/c/test-conversation"},
            "dsh": {"project": "test-project", "project_path": "C:/test/project"},
        }
        self.sender = FakeSender()
        self.relay = DirectRelay(self.config, self.runtime, self.sender)

    def read_state(self):
        return json.loads((self.runtime / "direct-state.json").read_text(encoding="utf-8"))

    def test_default_preview_never_creates_files_or_calls_sender(self):
        for target, message in (("gpt", "DS做完了，请检查。"), ("dsh", "执行指定任务书。")):
            result = self.relay.send(target, message)
            self.assertEqual(result["status"], "dry_run")
            self.assertEqual((result["target"], result["message"]), (target, message))
            self.assertIsNone(result["server_received"])
        self.assertFalse(self.runtime.exists())
        self.assertEqual(self.sender.validated, [])
        self.assertEqual(self.sender.calls, [])

    def test_real_send_persists_pending_before_sender_and_records_only_action_completion(self):
        def check_pending(target, message, event_id):
            pending = self.read_state()["pending"]
            self.assertEqual((pending["target"], pending["message"], pending["event_id"]),
                             (target, message, event_id))
            self.assertEqual(pending["status"], "pending")
            self.assertIsNone(pending["server_received"])
            uuid.UUID(event_id)

        self.sender.before_send = check_pending
        result = self.relay.send("gpt", "DS做完了，请检查。", execute=True)
        self.assertEqual(result["status"], "send_action_completed")
        self.assertIsNone(result["server_received"])
        self.assertEqual(self.sender.validated, ["gpt"])
        self.assertEqual(len(self.sender.calls), 1)
        state = self.read_state()
        self.assertIsNone(state["pending"])
        self.assertEqual(state["last_action"], result)
        self.assertFalse((self.runtime / "ui-receipts").exists())

    def test_every_explicit_send_gets_a_fresh_uuid_even_for_identical_text(self):
        first = self.relay.send("dsh", "执行指定任务书。", execute=True)
        second = self.relay.send("dsh", "执行指定任务书。", execute=True)
        self.assertEqual([call[0] for call in self.sender.calls], ["dsh", "dsh"])
        self.assertNotEqual(first["event_id"], second["event_id"])
        uuid.UUID(first["event_id"])
        uuid.UUID(second["event_id"])

    def test_validation_failure_does_not_reserve_pending(self):
        self.sender.validation_error = RuntimeError("please calibrate")
        with self.assertRaisesRegex(RuntimeError, "please calibrate"):
            self.relay.send("dsh", "执行任务", execute=True)
        self.assertFalse((self.runtime / "direct-state.json").exists())
        self.assertEqual(self.sender.calls, [])

    def test_failed_send_is_uncertain_and_blocks_all_following_attempts_after_restart(self):
        self.sender.error = RuntimeError("window disappeared after click")
        result = self.relay.send("gpt", "检查交付", execute=True)
        self.assertEqual(result["status"], "uncertain")
        self.assertIsNone(result["server_received"])
        original = (self.runtime / "direct-state.json").read_bytes()
        fresh_sender = FakeSender()
        restarted = DirectRelay(self.config, self.runtime, fresh_sender)
        self.assertEqual(restarted.send("dsh", "下一个动作", execute=True)["status"], "uncertain")
        self.assertEqual(fresh_sender.validated, [])
        self.assertEqual(fresh_sender.calls, [])
        self.assertEqual((self.runtime / "direct-state.json").read_bytes(), original)

    def test_keyboard_interrupt_is_saved_as_uncertain_and_reraised(self):
        self.sender.error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.relay.send("dsh", "执行任务", execute=True)
        pending = self.read_state()["pending"]
        self.assertEqual(pending["status"], "uncertain")
        self.assertEqual(pending["error"], "KeyboardInterrupt")
        self.assertEqual(self.relay.send("gpt", "检查", execute=True)["status"], "uncertain")
        self.assertEqual(len(self.sender.calls), 1)

    def test_resolve_confirms_or_skips_without_sending_and_allows_a_new_explicit_action(self):
        for sent in (True, False):
            with self.subTest(sent=sent):
                runtime = self.runtime / str(sent)
                sender = FakeSender()
                sender.error = RuntimeError("uncertain")
                relay = DirectRelay(self.config, runtime, sender)
                failed = relay.send("gpt", "检查", execute=True)
                result = relay.resolve(sent)
                self.assertEqual(result["status"], "resolved_sent" if sent else "resolved_skipped")
                self.assertEqual(result["human_confirmed_sent"], sent)
                self.assertEqual(result["event_id"], failed["event_id"])
                self.assertIsNone(result["server_received"])
                self.assertEqual(len(sender.calls), 1)
                self.assertIsNone(relay.status()["state"]["pending"])
                sender.error = None
                next_action = relay.send("dsh", "执行", execute=True)
                self.assertEqual(next_action["status"], "send_action_completed")
                self.assertNotEqual(failed["event_id"], next_action["event_id"])

    def test_status_is_read_only_and_resolve_requires_a_boolean(self):
        self.assertIsNone(self.relay.status()["state"])
        self.assertFalse(self.runtime.exists())
        for value in (None, "false", 0, 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.relay.resolve(value)
        self.assertFalse(self.runtime.exists())
        self.assertEqual(self.relay.resolve(False)["status"], "no_pending")
        self.assertFalse((self.runtime / "direct-state.json").exists())

    def test_preview_and_status_do_not_modify_existing_state(self):
        self.relay.send("gpt", "检查", execute=True)
        before = (self.runtime / "direct-state.json").read_bytes()
        self.relay.status()
        self.assertEqual(self.relay.send("dsh", "预览")["status"], "dry_run")
        self.assertEqual((self.runtime / "direct-state.json").read_bytes(), before)
        self.assertEqual(self.sender.validated, ["gpt"])
        self.assertEqual(len(self.sender.calls), 1)

    def test_pause_blocks_real_input_and_can_be_observed_without_sending(self):
        self.runtime.mkdir(parents=True)
        (self.runtime / "PAUSE").touch()
        self.assertEqual(self.relay.send("gpt", "检查", execute=True)["status"], "paused")
        self.assertTrue(self.relay.status()["paused"])
        self.assertTrue(self.relay.send("gpt", "预览")["paused"])
        self.assertEqual(self.sender.validated, [])
        self.assertEqual(self.sender.calls, [])
        self.assertFalse((self.runtime / "direct-state.json").exists())

    def test_pause_arriving_during_validation_still_prevents_pending_and_input(self):
        self.sender.on_validate = lambda: (self.runtime / "PAUSE").touch()
        self.assertEqual(self.relay.send("gpt", "检查", execute=True)["status"], "paused")
        self.assertEqual(self.sender.validated, ["gpt"])
        self.assertEqual(self.sender.calls, [])
        self.assertFalse((self.runtime / "direct-state.json").exists())

    def test_same_controller_lock_prevents_concurrent_window_operations(self):
        with run_lock(self.runtime / ".controller-lock"):
            # Windows may reject the locked byte read before the helper's friendly error.
            with self.assertRaises((RuntimeError, OSError)):
                self.relay.send("gpt", "检查", execute=True)
        self.assertEqual(self.sender.validated, [])
        self.assertEqual(self.sender.calls, [])
        self.assertFalse((self.runtime / "direct-state.json").exists())

    def test_runtime_binding_rejects_changed_conversation_project_or_path(self):
        self.relay.send("gpt", "检查", execute=True)
        for section, key, value in (
            ("gpt", "url", "https://chatgpt.com/c/other"),
            ("dsh", "project", "another-project"),
            ("dsh", "project_path", "C:/test/other"),
        ):
            with self.subTest(section=section, key=key):
                config = copy.deepcopy(self.config)
                config[section][key] = value
                other = DirectRelay(config, self.runtime, self.sender)
                with self.assertRaisesRegex(ValueError, "绑定"):
                    other.send("gpt", "检查", execute=True)
                with self.assertRaisesRegex(ValueError, "绑定"):
                    other.status()

    def test_old_state_is_never_read_or_modified(self):
        self.runtime.mkdir(parents=True)
        old_state = self.runtime / "state.json"
        old_state.write_text("old format deliberately not JSON", encoding="utf-8")
        self.assertIsNone(self.relay.status()["state"])
        self.assertEqual(self.relay.send("gpt", "检查", execute=True)["status"], "send_action_completed")
        self.assertEqual(old_state.read_text(encoding="utf-8"), "old format deliberately not JSON")
        self.assertEqual(set(self.read_state()["binding"]), {"gpt_url", "dsh_project", "dsh_project_path"})

    def test_invalid_target_message_or_execute_value_fails_before_any_side_effect(self):
        cases = (("other", "text", False), ("gpt", " ", True), ("gpt", None, True), ("gpt", "text", "false"))
        for target, message, execute in cases:
            with self.subTest(target=target, message=message), self.assertRaises(ValueError):
                self.relay.send(target, message, execute)
        self.assertFalse(self.runtime.exists())
        self.assertEqual(self.sender.validated, [])
        self.assertEqual(self.sender.calls, [])


if __name__ == "__main__":
    unittest.main()
