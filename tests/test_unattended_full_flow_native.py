"""Native background protocol fixtures; no model, UE or production writes."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unattended import full_flow_native as native

SESSION = "99db06e4-336d-4d8e-927d-1db262334adf"
OTHER = "89db06e4-336d-4d8e-927d-1db262334adf"


def event(kind, **fields):
    return {"type": kind, "session_id": SESSION, **fields}


def init(model=native.MODEL):
    return event("system", subtype="init", model=model)


def result(answer="answer", *, origin=None, index=0, **fields):
    return event("result", subtype="success", is_error=False, result=answer,
                 modelUsage={native.MODEL: {}, "claude-sonnet-5-5": {}}, origin=origin,
                 result_index=index, **fields)


def tool_progress(**fields):
    return event("tool_progress", uuid="228cad03-4367-48aa-a244-53250d49bc36",
                 parent_tool_use_id="toolu_01THVQmAyVELnksEJ2iN5Drm") | fields


class NativeBackgroundTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "stdout.log"

    def parse(self, events, requested_session=SESSION):
        self.path.write_text("\n".join(json.dumps(value) for value in events) + "\n", encoding="utf8")
        original = self.path.read_bytes()
        value = native.parse_full_flow_native(self.path, requested_session)
        self.assertEqual(original, self.path.read_bytes())
        self.assertEqual(value[-1]["raw_stdout_sha256"], hashlib.sha256(original).hexdigest())
        return value

    def background(self):
        return [event("system", subtype="task_notification"), event("system", subtype="task_notification"),
                init(), result("", origin={"kind": "task-notification"}), init(),
                event("assistant", message={"content": []}),
                event("rate_limit_event", rate_limit_info={"status": "allowed"}), result("foreground", index=1),
                event("system", subtype="background_tasks_changed"), event("system", subtype="task_notification"),
                init(), event("system", subtype="thinking_tokens"),
                event("system", subtype="task_notification"),
                result("final summary", index=2, origin={"kind": "task-notification", "producer": "session-task", "runId": "owned-run-1"})]

    def test_actual_protocol_shape_selects_last_background_answer_without_losing_origin(self):
        session, terminal, errors, rate, model = self.parse(self.background())
        self.assertEqual(session, SESSION); self.assertEqual(terminal["result"], "final summary")
        self.assertEqual(terminal["origin"]["kind"], "task-notification")
        self.assertEqual(model["selected_origin"], terminal["origin"])
        self.assertEqual(model["selected_turn_index"], 2); self.assertEqual(model["selected_result_index"], 2)
        self.assertEqual(model["turn_count"], 3); self.assertEqual(model["notification_count"], 4)
        self.assertEqual(errors, []); self.assertEqual(rate["status"], "allowed")
        self.assertTrue(model["model_verified"])

    def test_single_fork_session_is_bound_from_notifications_and_init(self):
        value = self.parse(self.background(), None)
        self.assertEqual(value[0], SESSION)
        events = self.background(); events[4]["session_id"] = OTHER
        with self.assertRaisesRegex(ValueError, "session"):
            self.parse(events, None)

    def test_foreign_or_missing_session_anywhere_is_rejected(self):
        for index in (0, 2, 3, 5, 6, 13):
            for identity in (OTHER, None):
                events = self.background(); events[index]["session_id"] = identity
                with self.subTest(index=index, identity=identity), self.assertRaisesRegex(ValueError, "session"):
                    self.parse(events)

    def test_incomplete_prefix_turn_or_suffix_cannot_reuse_an_earlier_answer(self):
        for events in ([event("system", subtype="task_notification")], [init()],
                       [init(), result(), init()],
                       [init(), result(), event("system", subtype="task_notification")],
                       [init(), result(), event("system", subtype="background_tasks_changed")]):
            with self.subTest(events=events), self.assertRaisesRegex(ValueError, "Incomplete"):
                self.parse(events)

    def test_nested_init_or_uninitialized_result_is_rejected(self):
        for events in ([init(), init(), result()], [result()], [event("assistant"), init(), result()]):
            with self.assertRaises(ValueError):
                self.parse(events)

    def test_only_native_task_notification_origin_is_allowed(self):
        for origin in ("task-notification", {}, {"kind": "agent"}, {"kind": "task-notification", "producer": "untrusted"},
                       {"kind": "task-notification", "extra": "not accepted"}):
            with self.subTest(origin=origin), self.assertRaisesRegex(ValueError, "origin"):
                self.parse([init(), result(origin=origin)])

    def test_duplicate_native_result_or_run_identity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "order"):
            self.parse([init(), result(), init(), result()])
        origin = {"kind": "task-notification", "producer": "session-task", "runId": "same"}
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.parse([init(), result(origin=origin), init(), result(origin=origin, index=1)])

    def test_later_error_is_preserved_even_when_earlier_answer_exists(self):
        failed = result("failed", index=1); failed.update(is_error=True, subtype="error_during_execution")
        value = self.parse([init(), result(), init(), failed])
        self.assertEqual(value[1]["result"], "answer")
        self.assertIn("native_terminal_error", value[2])
        self.assertTrue(value[-1]["native_turns"][-1]["is_error"])

    def test_api_errors_and_rejected_quota_cannot_be_hidden_by_later_allowed_event(self):
        events = [init(), event("assistant", error="rate_limit", parent_tool_use_id=None),
                  event("rate_limit_event", rate_limit_info={"status": "rejected"}),
                  event("rate_limit_event", rate_limit_info={"status": "allowed"}), result()]
        value = self.parse(events)
        self.assertEqual(value[2], ["rate_limit"]); self.assertEqual(value[3]["status"], "rejected")

    def test_wrong_model_in_any_turn_is_not_reported_verified(self):
        value = self.parse([init(), result(), init("claude-sonnet-5-5"), result("other", index=1)])
        self.assertFalse(value[-1]["model_verified"])
        self.assertEqual(value[-1]["selected_turn_index"], 0)
        wrong = result(); wrong["modelUsage"] = {"claude-sonnet-5-5": {}}
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            self.parse([init(), wrong])

    def test_empty_notification_ack_is_not_an_invented_answer(self):
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            self.parse([init(), result("", origin={"kind": "task-notification"})])
        value = self.parse([init(), result("foreground"), init(), result("", index=1, origin={"kind": "task-notification"})])
        self.assertEqual(value[1]["result"], "foreground")
        self.assertEqual(value[-1]["turn_count"], 2)

    def test_invalid_json_duplicate_keys_and_wrong_error_flag_fail(self):
        for raw in ('{"type":"system","type":"result"}\n', '{"type":\n'):
            self.path.write_text(raw, encoding="utf8")
            with self.assertRaises(ValueError):
                native.parse_full_flow_native(self.path, SESSION)
        invalid = result(); invalid["is_error"] = 0
        with self.assertRaisesRegex(ValueError, "error flag"):
            self.parse([init(), invalid])

    def test_linked_and_oversized_output_is_rejected(self):
        self.path.write_text("{}", encoding="utf8")
        real = native.ue_checks._is_reparse
        with patch.object(native.ue_checks, "_is_reparse", side_effect=lambda path: path == self.path or real(path)):
            with self.assertRaisesRegex(ValueError, "junctions"):
                native.parse_full_flow_native(self.path, SESSION)
        with patch.object(native, "MAX_STDOUT_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "oversized"):
                native.parse_full_flow_native(self.path, SESSION)

    def test_plain_foreground_with_ui_preamble_remains_supported(self):
        value = self.parse([event("system", subtype="ui_invalidate"), init(), result()])
        self.assertTrue(value[-1]["model_verified"])
        self.assertIsNone(value[-1]["selected_origin"])

    def test_sanitized_tool_progress_is_liveness_only_inside_a_native_turn(self):
        for parent in (None, "toolu_01THVQmAyVELnksEJ2iN5Drm"):
            with self.subTest(parent=parent):
                value = self.parse([init(), tool_progress(parent_tool_use_id=parent), result()])
                self.assertEqual(value[1]["result"], "answer")
                self.assertEqual(value[2], [])
                self.assertEqual(value[-1]["turn_count"], 1)
                self.assertEqual(value[-1]["event_count"], 3)
                self.assertTrue(value[-1]["model_verified"])

    def test_tool_progress_requires_exact_sanitized_shape_and_same_session(self):
        invalid = [tool_progress(uuid="not-a-uuid"), tool_progress(uuid=None),
                   tool_progress(parent_tool_use_id=""), tool_progress(parent_tool_use_id=7),
                   tool_progress(parent_tool_use_id="unsafe\nidentity"),
                   tool_progress(result="not a terminal"), tool_progress(error="rate_limit"),
                   tool_progress(session_id=OTHER), tool_progress(session_id=None)]
        missing = tool_progress(); del missing["parent_tool_use_id"]
        invalid.append(missing)
        for progress in invalid:
            with self.subTest(progress=progress), self.assertRaises(ValueError):
                self.parse([init(), progress, result()])

    def test_tool_progress_cannot_open_or_complete_a_turn(self):
        for events in ([tool_progress(), init(), result()],
                       [init(), result(), tool_progress()],
                       [init(), tool_progress()],
                       [init(), result(), init(), tool_progress()]):
            with self.subTest(events=events), self.assertRaises(ValueError):
                self.parse(events)

    def test_tool_progress_does_not_mask_later_error_quota_or_model_mismatch(self):
        failed = result("failed", index=1); failed.update(is_error=True, subtype="error_during_execution")
        value = self.parse([init(), tool_progress(), result(), init("claude-sonnet-5-5"),
                            tool_progress(), event("assistant", error="rate_limit", parent_tool_use_id=None),
                            event("rate_limit_event", rate_limit_info={"status": "rejected"}), failed])
        self.assertEqual(value[1]["result"], "answer")
        self.assertEqual(value[2], ["rate_limit", "native_terminal_error"])
        self.assertEqual(value[3]["status"], "rejected")
        self.assertFalse(value[-1]["model_verified"])


if __name__ == "__main__":
    unittest.main()
