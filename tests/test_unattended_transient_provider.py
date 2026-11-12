import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from github_harness_lab.lab_harness.process import ProcessResult
from unattended import providers
from unattended.providers import AgentProviders, ProviderError, classify_transient_failure


def dsh_failure(error):
    return [
        {"type": "session", "sessionId": "session-safe"},
        {"type": "status", "phase": "turn_start", "turn": 2},
        {"type": "status", "phase": "turn_end", "turn": 2,
         "reason": {"kind": "error", "error": error}},
        {"type": "final", "sessionId": "session-safe", "text": "Unable to finish"},
    ]


class TransientProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)

    def fixture(self, events, *, kind="code", stderr="", **record):
        (self.out / "stdout.log").write_text(
            "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        (self.out / "stderr.log").write_text(stderr, encoding="utf-8")
        (self.out / "process.json").write_text(json.dumps({
            "kind": kind, "exit_code": 1, "timed_out": False,
            "output_limit_exceeded": False, **record}), encoding="utf-8")

    def subscription_fixture(self, events, *, kind="review", stderr="", **record):
        api = [{"type": "thread.started", "thread_id": "thread-api"}, {"type": "turn.started"},
               {"type": "turn.failed", "error": {"message": "Quota exceeded. Check your plan and billing details."}}]
        self.fixture(api, kind=kind, codex_auth_mode="api_key", codex_subscription_fallback=True)
        child = self.out / "codex-subscription"
        child.mkdir(exist_ok=True)
        (child / "stdout.log").write_text("\n".join(json.dumps(row) for row in events) + "\n", encoding="utf-8")
        (child / "stderr.log").write_text(stderr, encoding="utf-8")
        (child / "process.json").write_text(json.dumps({"kind": kind, "exit_code": 1, "timed_out": False,
            "output_limit_exceeded": False, "codex_auth_mode": "chatgpt", **record}), encoding="utf-8")
        return child

    @staticmethod
    def subscription_error(**fields):
        return {"message": "You’ve hit your usage limit. Visit https://chatgpt.com/settings/usage to purchase more credits or try again at Nov 11th, 2026 2:50 PM.", **fields}

    def test_codex_api_and_subscription_exhaustion_uses_nested_native_limit_reason(self):
        for kind in ("plan", "review"):
            error = self.subscription_error()
            rows = [{"type": "thread.started", "thread_id": "subscription-thread"}, {"type": "turn.started"},
                    {"type": "error", **error}, {"type": "turn.failed", "error": error}]
            child = self.subscription_fixture(rows, kind=kind)
            before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.out.rglob("*") if path.is_file()}
            self.assertEqual(classify_transient_failure(kind, self.out), {"reason": "codex_subscription_limit"})
            self.assertEqual(classify_transient_failure(kind, child), {"reason": "codex_subscription_limit"})
            after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.out.rglob("*") if path.is_file()}
            self.assertEqual(before, after)

    def test_codex_subscription_limit_preserves_structured_delay_but_never_parses_text_date(self):
        for fields, expected in (({}, None), ({"retry_after_seconds": 2700}, 2700),
                                 ({"headers": {"Retry-After": "1800"}}, 1800),
                                 ({"retry_after_seconds": True}, None), ({"retry_after_seconds": -1}, None),
                                 ({"retry_after_seconds": "Nov 11th, 2026 2:50 PM"}, None)):
            with self.subTest(fields=fields):
                error = self.subscription_error(**fields)
                rows = [{"type": "thread.started", "thread_id": "subscription-thread"}, {"type": "turn.started"},
                        {"type": "turn.failed", "error": error}]
                self.subscription_fixture(rows)
                classified = classify_transient_failure("review", self.out)
                self.assertEqual(classified["reason"], "codex_subscription_limit")
                self.assertEqual(classified.get("retry_after_seconds"), expected)

    def test_codex_native_top_level_quota_error_before_nonzero_exit_is_waitable(self):
        rows = [{"type": "thread.started", "thread_id": "subscription-thread"}, {"type": "turn.started"},
                {"type": "error", **self.subscription_error()}]
        self.subscription_fixture(rows)
        self.assertEqual(classify_transient_failure("review", self.out), {"reason": "codex_subscription_limit"})

    def test_codex_limit_prose_tools_stderr_and_unrelated_child_are_not_retry_evidence(self):
        error = self.subscription_error()
        opening = [{"type": "thread.started", "thread_id": "subscription-thread"}, {"type": "turn.started"}]
        for item in ({"type": "agent_message", "text": error["message"]},
                     {"type": "command_execution", "aggregated_output": error["message"], "error": error}):
            rows = opening + [{"type": "item.completed", "item": item},
                              {"type": "turn.failed", "error": {"message": "Unrelated failure"}}]
            self.subscription_fixture(rows, stderr=error["message"])
            self.assertIsNone(classify_transient_failure("review", self.out))
        rows = opening + [{"type": "error", **error}, {"type": "turn.failed", "error": error}]
        child = self.subscription_fixture(rows)
        primary = json.loads((self.out / "process.json").read_text(encoding="utf-8"))
        primary["codex_subscription_fallback"] = False
        (self.out / "process.json").write_text(json.dumps(primary), encoding="utf-8")
        self.assertIsNone(classify_transient_failure("review", self.out))
        self.assertIsNone(classify_transient_failure("review", child))

    def test_codex_limit_does_not_override_success_authentication_or_invalid_protocol(self):
        error = self.subscription_error()
        opening = [{"type": "thread.started", "thread_id": "subscription-thread"}, {"type": "turn.started"}]
        failed = {"type": "turn.failed", "error": error}
        rows = opening + [{"type": "error", **error}, failed]
        for receipt in ({"exit_code": 0}, {"timed_out": True}, {"output_limit_exceeded": True},
                        {"codex_auth_mode": "api_key"}, {"launch_or_capture_failed": True}):
            self.subscription_fixture(rows, **receipt)
            self.assertIsNone(classify_transient_failure("review", self.out))
        for invalid in (opening + [{"type": "error", "status": 401, "message": "unauthorized"}, failed],
                        opening + [{"type": "error", **error}, {"type": "turn.completed"}],
                        rows + [{"type": "error", **error}],
                        opening + [{"type": "turn.failed", "error": error, "truncated": True}]):
            self.subscription_fixture(invalid)
            self.assertIsNone(classify_transient_failure("review", self.out))

    def test_real_legacy_server_503_shape_is_transient(self):
        error = {"message": '503 {"error":{"message":"Service is too busy"}}',
                 "code": "SERVER", "status": 503}
        self.fixture(dsh_failure(error))
        self.assertEqual(classify_transient_failure("code", self.out), {"reason": "server_unavailable"})

    def test_rate_limit_and_server_statuses_preserve_only_numeric_retry_after(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.fixture(dsh_failure({"code": "SERVER", "status": status,
                                          "headers": {"Retry-After": "240", "Authorization": "private-value"}}))
                self.assertEqual(classify_transient_failure("code", self.out), {
                    "reason": "rate_limited" if status == 429 else "server_unavailable",
                    "retry_after_seconds": 240})
        for delay in (-1, True, "240 private-value", "99999999999999", 100000):
            with self.subTest(delay=delay):
                self.fixture(dsh_failure({"status": 429, "retry_after_seconds": delay}))
                self.assertEqual(classify_transient_failure("code", self.out), {"reason": "rate_limited"})

    def test_auth_quota_balance_context_permission_and_unknown_stay_permanent(self):
        errors = [{"code": "SERVER", "status": status} for status in (400, 401, 403, 404)]
        errors.append({"code": "INSUFFICIENT_QUOTA", "status": 429})
        errors += [{"status": 429, "message": message} for message in (
            "insufficient_quota", "quota exceeded", "insufficient balance", "credits exhausted",
            "context length exceeded", "permission denied", "invalid API key", "余额不足")]
        errors += [{"code": code, "status": 503} for code in (
            "INSUFFICIENT_QUOTA", "CONTEXT_LENGTH_EXCEEDED", "AUTHENTICATION", "UNKNOWN")]
        errors += [{"message": "Unknown failure"}, {"code": "UNKNOWN"}]
        for error in errors:
            with self.subTest(error=error):
                self.fixture(dsh_failure(error))
                self.assertIsNone(classify_transient_failure("code", self.out))

    def test_transport_error_codes_are_retryable_but_not_http_client_errors(self):
        for code in ("TRANSPORT", "ETIMEDOUT", "ECONNRESET", "ENETUNREACH"):
            with self.subTest(code=code):
                self.fixture(dsh_failure({"code": code, "message": "request transport failed"}))
                self.assertEqual(classify_transient_failure("code", self.out), {"reason": "transport"})
        self.fixture(dsh_failure({"code": "TRANSPORT", "status": 400, "message": "invalid request"}))
        self.assertIsNone(classify_transient_failure("code", self.out))

    def test_real_dsh_messages_stream_closed_is_retryable_transport(self):
        for code in ("STREAM_CLOSED", "stream_closed", "Stream_Closed"):
            with self.subTest(code=code):
                rows = dsh_failure({"code": code,
                                    "message": "DeepSeek Messages stream ended before message_stop"})
                # The observed 0.2.1-rc.3 invocation ends with this bare final.
                rows[-1] = {"type": "final"}
                self.fixture(rows)
                self.assertEqual(classify_transient_failure("code", self.out), {"reason": "transport"})

    def test_stream_closed_requires_known_metadata_and_permanent_errors_win(self):
        message = "DeepSeek Messages stream ended before message_stop"
        for error in (
                {"code": "STREAM_CLOSED", "message": "Unknown stream failure"},
                {"code": "STREAM_CLOSED", "message": message + ": unauthorized"},
                {"code": "STREAM_CLOSED", "message": message, "status": 400},
                {"code": "STREAM_CLOSED", "message": message, "status": 401},
                {"code": "STREAM_CLOSED", "message": message, "status": 403},
                {"code": "STREAM_CLOSED", "message": "insufficient quota"}):
            with self.subTest(error=error):
                self.fixture(dsh_failure(error))
                self.assertIsNone(classify_transient_failure("code", self.out))
        rows = dsh_failure({"code": "STREAM_CLOSED", "message": message})
        rows.insert(2, {"type": "error", "error": {"status": 401, "message": "unauthorized"}})
        self.fixture(rows)
        self.assertIsNone(classify_transient_failure("code", self.out))

    def test_stream_closed_prose_and_success_exit_do_not_trigger_retry(self):
        error = {"code": "STREAM_CLOSED",
                 "message": "DeepSeek Messages stream ended before message_stop"}
        rows = dsh_failure({"code": "UNKNOWN", "message": "Unknown error"})
        for event_type in ("thinking", "text", "tool_result"):
            rows.insert(2, {"type": event_type, "error": error, "text": error["message"]})
        rows[-1]["text"] = "STREAM_CLOSED: " + error["message"]
        self.fixture(rows, stderr="dsh: STREAM_CLOSED: " + error["message"])
        self.assertIsNone(classify_transient_failure("code", self.out))
        self.fixture(dsh_failure(error), exit_code=0)
        self.assertIsNone(classify_transient_failure("code", self.out))

    def test_shortened_tool_output_does_not_invalidate_known_stream_failure(self):
        error = {"code": "STREAM_CLOSED",
                 "message": "DeepSeek Messages stream ended before message_stop"}
        rows = dsh_failure(error)
        rows.insert(2, {"type": "tool_result", "callId": "safe-call", "status": "success",
                        "result": "Tool output shortened by DSH", "truncated": True})
        rows[-1] = {"type": "final"}
        self.fixture(rows)
        self.assertEqual(classify_transient_failure("code", self.out), {"reason": "transport"})
        # Tool-owned error fields cannot supply or replace terminal evidence.
        rows[2]["error"] = error
        rows[3]["reason"]["error"] = {"code": "UNKNOWN", "message": "Unknown error"}
        self.fixture(rows)
        self.assertIsNone(classify_transient_failure("code", self.out))

    def test_truncated_control_or_unknown_event_cannot_trigger_retry(self):
        for event_type in ("session", "status", "error", "final", "unknown-event"):
            with self.subTest(event_type=event_type):
                rows = dsh_failure({"code": "STREAM_CLOSED",
                                    "message": "DeepSeek Messages stream ended before message_stop"})
                if event_type == "unknown-event":
                    rows.insert(2, {"type": event_type, "truncated": True})
                elif event_type == "error":
                    rows.insert(2, {"type": event_type, "truncated": True,
                                    "error": {"code": "STREAM_CLOSED"}})
                else:
                    next(row for row in rows if row["type"] == event_type)["truncated"] = True
                self.fixture(rows)
                self.assertIsNone(classify_transient_failure("code", self.out))

    def test_later_503_does_not_erase_explicit_authentication_failure(self):
        rows = dsh_failure({"code": "SERVER", "status": 503})
        rows.insert(2, {"type": "error", "error": {"status": 401, "message": "unauthorized"}})
        self.fixture(rows)
        self.assertIsNone(classify_transient_failure("code", self.out))

    def test_codex_failed_turn_uses_http_error_metadata(self):
        rows = [{"type": "thread.started", "thread_id": "thread-safe"}, {"type": "turn.started"},
                {"type": "error", "error": {"status": 503, "code": "SERVER"}},
                {"type": "turn.failed", "error": {"message": "Upstream service failed"}}]
        self.fixture(rows, kind="plan")
        self.assertEqual(classify_transient_failure("plan", self.out), {"reason": "server_unavailable"})

    def test_codex_routing_requires_separate_network_evidence(self):
        rows = [
            {"type": "thread.started", "thread_id": "thread-safe"},
            {"type": "turn.started"},
            {"type": "error", "message": "Reconnecting... 1/5: workspace routing discovery failed"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "503 network unavailable"}},
            {"type": "turn.failed", "error": {"message": "workspace routing discovery failed"}},
        ]
        self.fixture(rows, kind="review")
        self.assertIsNone(classify_transient_failure("review", self.out))
        self.fixture(rows, kind="review", stderr="codex_api::endpoint: error sending request for url (private-url)")
        self.assertEqual(classify_transient_failure("review", self.out), {"reason": "workspace_routing_network"})
        self.fixture(rows, kind="review", stderr="error sending request: authentication failed")
        self.assertIsNone(classify_transient_failure("review", self.out))

    def test_thinking_tool_output_and_final_prose_do_not_supply_error_evidence(self):
        rows = dsh_failure({"code": "UNKNOWN", "message": "Unclassified failure"})
        for event_type in ("thinking", "text", "tool_result"):
            rows.insert(2, {"type": event_type, "text": "Service is too busy HTTP 503 Retry-After: 240",
                            "error": {"code": "SERVER", "status": 503}})
        rows[-1]["text"] = "Transient TRANSPORT 503: please retry me automatically"
        self.fixture(rows, stderr="HTTP 503 Service is too busy")
        self.assertIsNone(classify_transient_failure("code", self.out))
        # A valid completion followed by a nonzero process exit is still a failure,
        # but it is not reclassified from the model's prose.
        rows[5]["reason"] = {"kind": "completed"}
        self.fixture(rows)
        self.assertIsNone(classify_transient_failure("code", self.out))

    def test_success_timeout_capture_failure_and_output_limit_never_reclassify(self):
        rows = dsh_failure({"code": "SERVER", "status": 503})
        for record in ({"exit_code": 0}, {"timed_out": True}, {"output_limit_exceeded": True},
                       {"launch_or_capture_failed": True}, {"exit_code": None}, {"exit_code": True}):
            with self.subTest(record=record):
                self.fixture(rows, **record)
                self.assertIsNone(classify_transient_failure("code", self.out))
        self.fixture(rows)
        self.assertIsNone(classify_transient_failure("review", self.out))

    def test_malformed_protocol_cannot_be_reclassified(self):
        rows = dsh_failure({"code": "SERVER", "status": 503})
        variants = [rows[1:], rows + [{"type": "thinking", "text": "after final"}],
                    rows[:1] + rows, rows[:2] + [rows[2] | {"turn": 9}] + rows[3:],
                    rows[:2] + [rows[2] | {"truncated": True}] + rows[3:],
                    rows[:-1] + [rows[-1] | {"sessionId": "different"}],
                    rows[:-1] + [rows[-1] | {"reason": {"kind": "completed"}}]]
        for bad in variants:
            with self.subTest(events=bad):
                self.fixture(bad)
                self.assertIsNone(classify_transient_failure("code", self.out))
        self.fixture(rows)
        with (self.out / "stdout.log").open("a", encoding="utf-8") as output:
            output.write('{"type":"error","type":"status"}\n')
        self.assertIsNone(classify_transient_failure("code", self.out))

    def test_reader_is_bounded_and_does_not_parse_large_thinking_payload(self):
        rows = dsh_failure({"code": "SERVER", "status": 503})
        rows.insert(2, {"type": "thinking", "text": "THOUGHT-PRIVATE-" * 100000})
        self.fixture(rows)
        parsed = []
        strict = providers._strict_json

        def observe(raw):
            parsed.append(raw)
            return strict(raw)

        opened = Path.open
        reads = []

        class BoundedReader:
            def __init__(self, source, name):
                self.source, self.name = source, name

            def __enter__(self):
                self.source.__enter__()
                return self

            def __exit__(self, *args):
                return self.source.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self.source, name)

            def read(self, size=-1):
                self_check.assertGreater(size, 0)
                raw = self.source.read(size)
                reads.append((self.name, len(raw), size))
                return raw

            def readline(self, size=-1):
                self_check.assertGreater(size, 0)
                raw = self.source.readline(size)
                reads.append((self.name, len(raw), size))
                return raw

        self_check = self

        def bounded(path, *args, **kwargs):
            return BoundedReader(opened(path, *args, **kwargs), path.name)

        with patch.object(Path, "open", bounded), patch.object(providers, "_strict_json", observe):
            self.assertEqual(classify_transient_failure("code", self.out), {"reason": "server_unavailable"})
        self.assertFalse(any("THOUGHT-PRIVATE" in raw for raw in parsed))
        self.assertLessEqual(sum(length for name, length, _ in reads if name == "stdout.log"),
                             providers._FAILURE_STDOUT_LIMIT + providers._FAILURE_LINE_LIMIT + 1)
        self.assertLessEqual(max(size for _, _, size in reads), providers._FAILURE_STDOUT_LIMIT)

    @patch("unattended.providers.run_process")
    def test_failed_invocation_records_safe_metadata_and_recovery_never_rewrites_logs(self, launch):
        adapter = AgentProviders({})

        def failure(*args, **kwargs):
            out = kwargs["output_dir"]
            (out / "stdout.log").write_text("\n".join(json.dumps(event) for event in dsh_failure({
                "code": "SERVER", "status": 503, "message": "Service is too busy private-value",
                "headers": {"Retry-After": "240", "Authorization": "private-value"}})) + "\n", encoding="utf-8")
            (out / "stderr.log").write_text("", encoding="utf-8")
            return ProcessResult(1, False, False, out / "stdout.log", out / "stderr.log")

        launch.side_effect = failure
        with self.assertRaises(ProviderError) as caught:
            adapter._invoke("code", ["fake-native.exe"], "Prompt", self.out, self.out, 1, {})
        error = caught.exception
        self.assertTrue(error.transient)
        self.assertEqual((error.retry_reason, error.retry_after_seconds), ("server_unavailable", 240))
        self.assertEqual(error.session_id, "session-safe")
        record = json.loads((self.out / "process.json").read_text())
        self.assertEqual((record["transient"], record["retry_reason"], record["retry_after_seconds"]),
                         (True, "server_unavailable", 240))
        self.assertNotIn("private-value", json.dumps(record) + str(error))
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.out.iterdir()}
        self.assertIsNone(adapter.recover("code", self.out))
        self.assertEqual(classify_transient_failure("code", self.out),
                         {"reason": "server_unavailable", "retry_after_seconds": 240})
        self.assertEqual(before, {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.out.iterdir()})
        launch.assert_called_once()
        self.assertFalse((self.out / "result.json").exists())


if __name__ == "__main__":
    unittest.main()
