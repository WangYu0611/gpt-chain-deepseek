"""Regression for the ProseMirror metadata supplied in the user's probe."""
from __future__ import annotations

import copy
import unittest
from unittest.mock import Mock

from windowrelay.observer import WindowObserver
from windowrelay.windows import WindowInfo


class PlaceholderRegressionTests(unittest.TestCase):
    def setUp(self):
        self.config = {target: {
            "window": {"process_name": "chrome.exe", "title_contains": "M2 18A"},
            "calibration": {"client_size": [1278, 1379], "dpi": 120},
        } for target in ("gpt", "dsh")}
        self.backend = Mock()
        self.backend.find_window.return_value = WindowInfo(42, "M2 18A", "chrome.exe", (1278, 1379), 120)
        # Candidate/value/button names are from the user's diagnostic. A complete
        # collector payload additionally supplies explicit enabled idle controls.
        self.payload = {
            "text": "Previous assistant response", "truncated": False,
            "buttons": ["Start Voice", "Dictate", "Add files and more", "Regenerate response"],
            "enabled_buttons": ["Start Voice", "Dictate", "Add files and more"],
            "editable_text": "Work with ChatGPT\n", "editable_present": True,
            "editable_readable": True, "editor_source": "fallback", "editor_candidates": [{
                "read_source": "value", "result": "readable",
                "text_preview": "Work with ChatGPT\n", "value_preview": "Work with ChatGPT\n",
                "parent_name": "", "read_pattern": "ValuePattern", "fallback": True,
                "automation_id": "", "type": "ControlType.Edit", "value_length": 18,
                "class_name": "ProseMirror", "value_readonly": False, "text_length": None,
                "bounding_rect": [476, 1243, 755, 33], "named": False,
                "name": "Work with ChatGPT", "has_value": True,
            }],
        }

    def observe(self, target="gpt"):
        collector = Mock(return_value=self.payload)
        return WindowObserver(self.config, self.backend, collector).snapshot(target)

    def test_exact_empty_composer_is_idle_and_keeps_raw_diagnostics(self):
        before = copy.deepcopy(self.payload)
        result = self.observe()
        self.assertTrue(result.available)
        self.assertTrue(result.idle)
        self.assertFalse(result.draft_present)
        self.assertEqual(result.editable_text, "")
        self.assertEqual(result.draft_preview, "Work with ChatGPT\n")
        self.assertEqual(result.editor_state, "placeholder_confirmed")
        self.assertEqual(result.reason_code, "idle_placeholder")
        self.assertEqual(self.payload, before)
        self.backend.activate.assert_not_called()

    def test_same_text_with_visible_send_is_a_real_draft_even_if_send_disabled(self):
        for label in ("Send", "Send message", "Send prompt", "发送", "发送消息", "Submit"):
            with self.subTest(label=label):
                self.payload["buttons"].append(label)
                result = self.observe()
                self.assertTrue(result.draft_present)
                self.assertFalse(result.idle)
                self.assertEqual(result.editable_text, "Work with ChatGPT\n")
                self.payload["buttons"].pop()

    def test_focused_composer_placeholder_remains_empty_and_busy_is_linked(self):
        self.payload["editor_candidates"][0]["class_name"] = "ProseMirror ProseMirror-focused"
        result = self.observe()
        self.assertTrue(result.idle)
        self.assertFalse(result.draft_present)
        self.payload["buttons"].append("Stop")
        self.payload["enabled_buttons"] = []
        result = self.observe()
        self.assertTrue(result.busy)
        self.assertFalse(result.draft_present)
        self.assertEqual(result.editable_text, "")

    def test_class_token_does_not_accept_similar_names_or_hide_real_drafts(self):
        for name in ("ProseMirror-focused", "NotProseMirror", "ProseMirrorOther"):
            self.payload["editor_candidates"][0]["class_name"] = name
            self.assertTrue(self.observe().draft_present)
        self.payload["editor_candidates"][0]["class_name"] = "ProseMirror ProseMirror-focused"
        self.payload["buttons"].append("Send")
        self.assertTrue(self.observe().draft_present)

    def test_missing_or_disabled_voice_controls_do_not_prove_empty(self):
        for enabled in ([], ["Start Voice"], ["Dictate"]):
            with self.subTest(enabled=enabled):
                self.payload["enabled_buttons"] = enabled
                result = self.observe()
                self.assertFalse(result.available)
                self.assertFalse(result.draft_present)
                self.assertFalse(result.idle)
                self.assertEqual(result.reason_code, "placeholder_unconfirmed")
                self.assertEqual(result.editable_text, "Work with ChatGPT\n")
                self.assertEqual(result.draft_preview, "Work with ChatGPT\n")

    def test_busy_while_placeholder_is_not_reported_as_human_draft(self):
        self.payload["buttons"].append("Stop generating")
        self.payload["enabled_buttons"] = []
        result = self.observe()
        self.assertTrue(result.available)
        self.assertTrue(result.busy)
        self.assertFalse(result.idle)
        self.assertFalse(result.draft_present)
        self.assertEqual(result.reason_code, "busy")
        self.assertEqual(result.editor_state, "placeholder_busy")
        self.assertEqual(result.draft_preview, "Work with ChatGPT\n")

    def test_voice_button_rendering_sequence_never_claims_a_human_draft(self):
        self.payload["text"] = "本轮输出 [[RELAY_DONE:unique-test]]"
        sequence = (["Start Voice", "Dictate"], [], ["Start Voice"], ["Start Voice", "Dictate"], ["Dictate"])
        results = []
        for enabled in sequence:
            self.payload["enabled_buttons"] = enabled
            results.append(self.observe())
        self.assertEqual([item.reason_code for item in results], [
            "idle_placeholder", "placeholder_unconfirmed", "placeholder_unconfirmed",
            "idle_placeholder", "placeholder_unconfirmed",
        ])
        self.assertFalse(any(item.draft_present for item in results))
        self.assertFalse(any(item.blocked for item in results))
        for item in results:
            self.assertEqual(item.draft_preview, "Work with ChatGPT\n")
            if item.reason_code == "placeholder_unconfirmed":
                self.assertFalse(item.available)
                self.assertFalse(item.idle)

    def test_send_control_still_protects_draft_during_voice_and_busy_transitions(self):
        self.payload["buttons"].extend(["Stop generating", "Send prompt"])
        self.payload["enabled_buttons"] = []
        result = self.observe()
        self.assertTrue(result.draft_present)
        self.assertTrue(result.busy)
        self.assertFalse(result.idle)
        self.assertEqual(result.editable_text, "Work with ChatGPT\n")

    def test_custom_send_control_also_preserves_same_text_draft(self):
        self.config["gpt"]["completion"] = {"idle_regex": "^Send to model$"}
        self.payload["buttons"].append("Send to model")
        self.assertTrue(self.observe().draft_present)
        self.assertFalse(self.observe().idle)

    def test_different_actual_text_whitespace_or_zero_width_is_preserved(self):
        for draft in ("Please inspect my project", " Work with ChatGPT\n", "Work with ChatGPT \n", "Work with ChatGPT\u200b\n"):
            with self.subTest(draft=draft):
                self.payload["editable_text"] = draft
                self.payload["editor_candidates"][0].update(value_preview=draft, value_length=len(draft), text_preview=draft)
                result = self.observe()
                self.assertEqual(result.editable_text, draft)
                self.assertTrue(result.draft_present)
                self.assertFalse(result.idle)

    def test_other_app_class_or_second_candidate_is_not_normalized(self):
        self.assertTrue(self.observe("dsh").draft_present)
        self.payload["editor_candidates"][0]["class_name"] = "CodeMirror"
        self.assertTrue(self.observe().draft_present)
        self.payload["editor_candidates"][0]["class_name"] = "ProseMirror"
        self.payload["editor_candidates"].append(copy.deepcopy(self.payload["editor_candidates"][0]))
        self.assertTrue(self.observe().draft_present)

    def test_conflicting_patterns_or_truncated_diagnostics_are_not_empty(self):
        self.payload["editor_candidates"][0]["text_preview"] = "actual unsent task text"
        self.assertFalse(self.observe().idle)
        self.payload["editor_candidates"][0]["text_preview"] = "Work with ChatGPT\n"
        self.payload["editor_candidates"][0]["value_length"] = 1000
        self.assertFalse(self.observe().idle)
        self.payload["truncated"] = True
        self.assertFalse(self.observe().available)

    def test_text_preview_at_character_limit_does_not_prove_full_pattern_agrees(self):
        self.payload["editor_candidates"][0]["text_preview"] = "Work with ChatGPT" + "\n" * 143
        self.assertTrue(self.observe().draft_present)
        self.assertFalse(self.observe().idle)

    def test_stop_and_approval_still_prevent_handoff(self):
        self.payload["buttons"].append("Stop generating")
        self.assertTrue(self.observe().busy)
        self.assertFalse(self.observe().idle)
        self.payload["buttons"].remove("Stop generating")
        self.payload["buttons"].append("Approve once")
        self.payload["enabled_buttons"].append("Approve once")
        result = self.observe()
        self.assertTrue(result.blocked)
        self.assertFalse(result.idle)


if __name__ == "__main__":
    unittest.main()
