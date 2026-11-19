"""The HTTP reviewer binds actual evidence and validates restart-safe API receipts."""
from io import BytesIO
import http.client
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from unattended import api_review as api
from unattended.providers import ProviderError


HEAD = "a" * 40
RESULT = {"decision": "accepted", "summary": "Verified", "feedback": "", "head_sha": HEAD}


def response(number, output, **changes):
    return {"object": "response", "id": f"resp_test_{number}", "status": "completed",
            "error": None, "incomplete_details": None, "model": "gpt-test", "output": output, **changes}


def call(name, ident, arguments=None):
    return {"type": "function_call", "id": "fc_" + ident, "call_id": ident, "name": name,
            "arguments": json.dumps(arguments or {}), "status": "completed"}


def answer(result=None):
    return {"type": "message", "id": "msg_final", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": json.dumps(result or RESULT), "annotations": []}]}


class FakeEvidence:
    tools = [{"type": "function", "name": "git_diff", "strict": True,
              "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}]
    instructions = "Use fixed test evidence"
    head_sha = HEAD

    def __init__(self, *args, **kwargs):
        self.calls = []

    def run(self, name, arguments):
        self.calls.append((name, arguments))
        return {"content": "Actual candidate evidence"}

    def verify(self):
        return None


class ApiReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.out = self.root / "review"
        self.config = {"codex": {"model": "gpt-test", "reasoning_effort": "high"}}
        self.requests = []
        self.addCleanup(patch.stopall)
        self.evidence = FakeEvidence()
        patch.object(api, "ReviewEvidence", return_value=self.evidence).start()
        patch.object(api, "_codex_api_key", return_value="private-api-credential").start()
        patch.object(api, "read_secret", return_value=None).start()
        patch("unattended.secrets.secret_values", return_value=["private-backup-credential"]).start()
        patch.object(api, "REQUEST_ATTEMPTS", 1).start()

    def invoke(self, responses, prompt="Inspect this task", *, art_directory=None, resume_from=None):
        def post(payload, key, endpoint, timeout, **kwargs):
            self.requests.append((json.loads(payload), key, endpoint, timeout))
            item = responses.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item, api._hash(api._bytes(item))
        with patch.object(api, "_post", side_effect=post):
            return api.review(self.config, prompt, self.root, self.out, 30, art_directory=art_directory, resume_from=resume_from)

    def exchange(self, final=None):
        return [response(1, [call("git_diff", "call_diff"), call("read_file", "call_file")]),
                response(2, [answer(final)])]

    def test_stateless_strict_review_replays_reasoning_and_recovers_offline(self):
        reasoning = {"type": "reasoning", "id": "rs_test", "summary": [], "encrypted_content": "opaque-reasoning"}
        replies = self.exchange()
        replies[0]["output"].insert(0, reasoning)
        self.assertEqual(self.invoke(replies), RESULT)
        first = self.requests[0][0]
        self.assertEqual(self.requests[0][2], "https://api.openai.com/v1/responses")
        self.assertFalse(first["store"])
        self.assertEqual(first["reasoning"], {"effort": "high"})
        self.assertTrue(first["text"]["format"]["strict"])
        self.assertEqual(first["text"]["format"]["schema"], api.REVIEW_SCHEMA)
        self.assertEqual(first["include"], ["reasoning.encrypted_content"])
        second = self.requests[1][0]
        self.assertIn(reasoning, second["input"])
        self.assertNotIn("previous_response_id", second)
        self.assertEqual([item["call_id"] for item in second["input"] if item.get("type") == "function_call_output"],
                         ["call_diff", "call_file"])
        receipt = json.loads((self.out / "process.json").read_text())
        self.assertEqual(receipt["protocol"], api.PROTOCOL)
        self.assertEqual(receipt["final_response_id"], "resp_test_2")
        self.assertEqual(receipt["head_sha"], HEAD)
        with patch.object(api, "_post", side_effect=AssertionError("Recovery must stay offline")):
            self.assertEqual(api.recover(self.out), RESULT)
            with self.assertRaisesRegex(ProviderError, "already used"):
                api.review(self.config, "Review again", self.root, self.out, 30)

    def test_all_known_credentials_are_redacted_from_requests_and_saved_artifacts(self):
        self.evidence.run = lambda *args: {"content": "private-backup-credential private-api-credential"}
        replies = self.exchange({**RESULT, "summary": "private-api-credential private-backup-credential"})
        result = self.invoke(replies, prompt="Do not echo private-api-credential or private-backup-credential")
        self.assertEqual(result["summary"], "[REDACTED] [REDACTED]")
        for request, _, _, _ in self.requests:
            self.assertNotIn("private-api-credential", json.dumps(request))
            self.assertNotIn("private-backup-credential", json.dumps(request))
        for path in self.out.rglob("*.json"):
            self.assertNotIn("private-api-credential", path.read_text())
            self.assertNotIn("private-backup-credential", path.read_text())

    def test_image_is_sent_as_multimodal_input_and_saved_as_a_hashed_marker(self):
        image = "data:image/png;base64,aGVsbG8="
        self.evidence.run = lambda name, args: ({"image_url": image, "path": "shot.png"}
                                                if name == "read_image" else {"content": "patch"})
        replies = [response(1, [call("git_diff", "diff"), call("read_file", "file"), call("read_image", "image")]),
                   response(2, [answer()])]
        self.assertEqual(self.invoke(replies), RESULT)
        content = [part for item in self.requests[1][0]["input"] for part in item.get("content", [])
                   if isinstance(part, dict) and part.get("type") == "input_image"]
        self.assertEqual(content, [{"type": "input_image", "image_url": image, "detail": "high"}])
        stored = (self.out / "responses/turn-0001.tools.json").read_text()
        self.assertNotIn(image, stored)
        self.assertIn(api._hash(image.encode()), stored)
        self.assertEqual(api.recover(self.out), RESULT)

    def test_final_requires_diff_and_actual_file_inspection(self):
        with self.assertRaisesRegex(ProviderError, "actual diff and files"):
            self.invoke([response(1, [answer()])])
        self.assertIsNone(api.recover(self.out))

    def test_art_review_requires_actual_image_evidence(self):
        with self.assertRaisesRegex(ProviderError, "actual diff and files"):
            self.invoke(self.exchange(), art_directory=self.root / "art")
        self.assertIsNone(api.recover(self.out))
        self.out = self.root / "with-image"
        self.evidence.run = lambda name, args: ({"image_url": "data:image/png;base64,aGVsbG8=", "path": "preview.png"}
                                                if name == "read_image" else {"content": "source evidence"})
        replies = self.exchange()
        replies[0]["output"].append(call("read_image", "shot"))
        self.assertEqual(self.invoke(replies, art_directory=self.root / "art"), RESULT)
        self.assertEqual(api.recover(self.out), RESULT)

    def test_returned_model_must_match_configured_model_or_dated_snapshot(self):
        replies = self.exchange()
        replies[-1]["model"] = "another-model"
        with self.assertRaisesRegex(ProviderError, "different model"):
            self.invoke(replies)
        self.assertIsNone(api.recover(self.out))
        self.out = self.root / "snapshot"
        replies = self.exchange()
        for item in replies:
            item["model"] = "gpt-test-2026-11-19"
        self.assertEqual(self.invoke(replies), RESULT)
        self.assertEqual(api.recover(self.out), RESULT)
        receipt = json.loads((self.out / "process.json").read_text())
        self.assertEqual(receipt["observed_model"], "gpt-test-2026-11-19")

    def test_wrong_head_refusal_and_incomplete_response_cannot_complete_review(self):
        for mode in ("wrong_head", "refusal", "incomplete"):
            self.out = self.root / mode
            replies = self.exchange()
            if mode == "wrong_head":
                replies[-1] = response(2, [answer({**RESULT, "head_sha": "b" * 40})])
            elif mode == "refusal":
                replies[-1]["output"][0]["content"] = [{"type": "refusal", "refusal": "Cannot review"}]
            else:
                replies[-1]["status"] = "incomplete"
            with self.subTest(mode=mode), self.assertRaises(ProviderError):
                self.invoke(replies)
            self.assertIsNone(api.recover(self.out))

    def test_recovery_rejects_corrupted_response_tools_and_sidecars(self):
        self.invoke(self.exchange())
        for relative in ("responses/turn-0002.json", "responses/turn-0001.tools.json", "final.json", "result.json"):
            path = self.out / relative
            before = path.read_bytes()
            path.write_bytes(before + b" ")
            with self.subTest(relative=relative):
                self.assertIsNone(api.recover(self.out))
            path.write_bytes(before)
        self.assertEqual(api.recover(self.out), RESULT)

    def test_http_failures_are_durable_and_never_automatically_replayed(self):
        for reason, status, delay in (("rate_limited", 429, 12), ("server_unavailable", 503, None), ("transport", None, None)):
            self.out = self.root / reason
            with self.subTest(reason=reason), self.assertRaises(ProviderError) as caught:
                self.invoke([api._RequestFailure(reason=reason, status=status, retry_after=delay)])
            self.assertTrue(caught.exception.transient)
            self.assertEqual(caught.exception.retry_reason, reason)
            expected = {"reason": reason}
            if delay:
                expected["retry_after_seconds"] = delay
            self.assertEqual(api.classify_failure(self.out), expected)
            self.assertIsNone(api.recover(self.out))
        self.out = self.root / "quota"
        with self.assertRaises(ProviderError) as caught:
            self.invoke([api._RequestFailure(status=429, code="insufficient_quota")])
        self.assertFalse(caught.exception.transient)
        self.assertIsNone(api.classify_failure(self.out))

    def test_round_limit_fails_without_final_result(self):
        with patch.object(api, "MAX_ROUNDS", 1), self.assertRaisesRegex(ProviderError, "round limit"):
            self.invoke(self.exchange())
        self.assertIsNone(api.recover(self.out))

    def test_deadline_after_http_response_preserves_failed_receipt_without_acceptance(self):
        with patch.object(api.time, "monotonic", side_effect=[100, 100, 100, 131]), self.assertRaises(ProviderError) as caught:
            self.invoke(self.exchange())
        self.assertTrue(caught.exception.interrupted)
        receipt = json.loads((self.out / "process.json").read_text())
        self.assertTrue(receipt["timed_out"])
        self.assertEqual(receipt["status"], "failed")
        self.assertIsNone(api.recover(self.out))

    def test_response_size_limit_and_forged_transient_quota_receipts_are_rejected(self):
        with patch.object(api, "_post", side_effect=api._OutputLimit("API response exceeded limit")):
            with self.assertRaises(ProviderError):
                api.review(self.config, "Review", self.root, self.out, 30)
        receipt = json.loads((self.out / "process.json").read_text())
        self.assertTrue(receipt["output_limit_exceeded"])
        self.assertIsNone(api.classify_failure(self.out))
        receipt.update(status="failed", exit_code=1, transient=True, output_limit_exceeded=False,
                       retry_reason="rate_limited", http_status=429, api_error_code="insufficient_quota")
        (self.out / "process.json").write_text(json.dumps(receipt))
        self.assertIsNone(api.classify_failure(self.out))

    def test_changed_evidence_fails_before_http_without_final_result(self):
        self.evidence.verify = lambda: (_ for _ in ()).throw(ValueError("Changed evidence"))
        with self.assertRaisesRegex(ProviderError, "evidence or local receipt"):
            self.invoke(self.exchange())
        self.assertIsNone(api.recover(self.out))

    def checkpoint(self, *, image=False):
        first = self.exchange()[0]
        if image:
            first["output"].append(call("read_image", "call_image"))
        with self.assertRaises(ProviderError):
            self.invoke([first, api._RequestFailure(reason="transport")])
        return self.out

    def test_transient_request_retries_use_identical_payload_and_execute_tools_once(self):
        replies = self.exchange()
        sequence = [api._RequestFailure(reason="transport", transport={"exception_type": "URLError", "cause_type": "SSLError"}),
                    replies[0], api._RequestFailure(status=503, reason="server_unavailable"), replies[1]]
        with patch.object(api, "REQUEST_ATTEMPTS", 4), patch.object(api.time, "sleep") as sleep:
            self.assertEqual(self.invoke(sequence), RESULT)
        self.assertEqual(self.requests[0][0], self.requests[1][0])
        self.assertEqual(self.requests[2][0], self.requests[3][0])
        self.assertEqual(len(self.evidence.calls), 2)
        self.assertEqual([row.args[0] for row in sleep.call_args_list], [1, 1])
        receipt = json.loads((self.out / "process.json").read_text())
        self.assertEqual(len(receipt["request_failures"]), 2)
        self.assertEqual(receipt["request_failures"][0]["transport_exception"]["cause_type"], "SSLError")
        self.assertEqual(api.recover(self.out), RESULT)

    def test_retries_are_bounded_and_auth_credit_or_long_retry_after_do_not_sleep(self):
        with patch.object(api, "REQUEST_ATTEMPTS", 3), patch.object(api.time, "sleep") as sleep:
            with self.assertRaises(ProviderError):
                self.invoke([api._RequestFailure(reason="transport") for _ in range(3)])
        self.assertEqual(len(self.requests), 3)
        self.assertEqual([row.args[0] for row in sleep.call_args_list], [1, 2])
        for label, failure in (("auth", api._RequestFailure(status=401, code="invalid_api_key")),
                               ("quota", api._RequestFailure(status=429, code="insufficient_quota")),
                               ("long-delay", api._RequestFailure(status=429, reason="rate_limited", retry_after=60))):
            self.out = self.root / label
            before = len(self.requests)
            with patch.object(api, "REQUEST_ATTEMPTS", 4), patch.object(api.time, "sleep") as sleep:
                with self.subTest(label=label), self.assertRaises(ProviderError):
                    self.invoke([failure])
            self.assertEqual(len(self.requests), before + 1)
            sleep.assert_not_called()

    def test_resume_revalidates_and_copies_completed_turns_without_changing_source(self):
        source = self.checkpoint()
        originals = {path.relative_to(source).as_posix(): path.read_bytes() for path in source.rglob("*") if path.is_file()}
        pending_payload = self.requests[-1][0]
        self.out = self.root / "continued"
        self.assertEqual(self.invoke([response(2, [answer()])], resume_from=source), RESULT)
        self.assertEqual(self.requests[-1][0], pending_payload)
        self.assertEqual(len(self.evidence.calls), 4)
        receipt = json.loads((self.out / "process.json").read_text())
        self.assertEqual(receipt["resumed_rounds"], 1)
        self.assertEqual(receipt["resume_from"], str(source.resolve()))
        self.assertEqual(receipt["resume_request_sha256"], api._hash(originals["request.json"]))
        self.assertEqual((self.out / "responses/turn-0001.json").read_bytes(), originals["responses/turn-0001.json"])
        for relative, raw in originals.items():
            self.assertEqual((source / relative).read_bytes(), raw)
        self.assertEqual(api.recover(self.out), RESULT)

    def test_resume_refuses_corrupt_response_tools_context_and_changed_evidence_before_http(self):
        source = self.checkpoint()
        for relative in ("responses/turn-0001.json", "responses/turn-0001.tools.json", "request.json"):
            path = source / relative
            original = path.read_bytes()
            if relative == "request.json":
                value = json.loads(original)
                value["instructions"] = "Changed instructions"
                path.write_text(json.dumps(value))
            else:
                path.write_bytes(original + b" ")
            self.out = self.root / ("invalid-" + path.name)
            before = len(self.requests)
            with self.subTest(relative=relative), self.assertRaises(ProviderError):
                self.invoke([], resume_from=source)
            self.assertEqual(len(self.requests), before)
            path.write_bytes(original)
        self.out = self.root / "changed-evidence"
        self.evidence.run = lambda *args: {"content": "Changed evidence"}
        before = len(self.requests)
        with self.assertRaises(ProviderError):
            self.invoke([], resume_from=source)
        self.assertEqual(len(self.requests), before)

    def test_resume_recreates_image_input_and_rejects_changed_image_bytes(self):
        def read(name, arguments):
            return {"image_url": "data:image/png;base64,aGVsbG8=", "path": "preview.png"} if name == "read_image" else {"content": "source"}
        self.evidence.run = read
        source = self.checkpoint(image=True)
        pending_payload = self.requests[-1][0]
        self.out = self.root / "image-continued"
        self.assertEqual(self.invoke([response(2, [answer()])], resume_from=source), RESULT)
        self.assertEqual(self.requests[-1][0], pending_payload)
        self.out = self.root / "changed-image"
        self.evidence.run = lambda name, arguments: ({"image_url": "data:image/png;base64,Y2hhbmdlZA==", "path": "preview.png"}
                                                     if name == "read_image" else {"content": "source"})
        before = len(self.requests)
        with self.assertRaises(ProviderError):
            self.invoke([], resume_from=source)
        self.assertEqual(len(self.requests), before)

    def test_resume_preserves_total_round_budget(self):
        source = self.checkpoint()
        self.out = self.root / "budget"
        before = len(self.requests)
        with patch.object(api, "MAX_ROUNDS", 2), self.assertRaisesRegex(ProviderError, "round limit"):
            self.invoke([response(2, [call("read_file", "more")])], resume_from=source)
        self.assertEqual(len(self.requests), before + 1)

    def test_another_transient_failure_after_resume_retains_all_verified_progress_for_the_next_attempt(self):
        first = self.checkpoint()
        self.out = self.root / "second-attempt"
        with self.assertRaises(ProviderError):
            self.invoke([response(2, [call("read_file", "call_more")]), api._RequestFailure(reason="transport")],
                        resume_from=first)
        second = self.out
        self.assertEqual(len(json.loads((second / "process.json").read_text())["turns"]), 2)
        pending_payload = self.requests[-1][0]
        self.out = self.root / "third-attempt"
        self.assertEqual(self.invoke([response(3, [answer()])], resume_from=second), RESULT)
        self.assertEqual(self.requests[-1][0], pending_payload)
        receipt = json.loads((self.out / "process.json").read_text())
        self.assertEqual(receipt["resumed_rounds"], 2)
        self.assertEqual([turn["number"] for turn in receipt["turns"]], [1, 2, 3])
        self.assertEqual(api.recover(self.out), RESULT)

    def test_short_retry_after_is_honored_before_repeating_the_same_request(self):
        replies = self.exchange()
        with patch.object(api, "REQUEST_ATTEMPTS", 4), patch.object(api.time, "sleep") as sleep:
            self.assertEqual(self.invoke([api._RequestFailure(status=429, reason="rate_limited", retry_after=3), *replies]), RESULT)
        sleep.assert_called_once_with(3)
        self.assertEqual(self.requests[0][0], self.requests[1][0])

    def test_truncated_http_diagnostics_record_types_without_raw_partial_body(self):
        with patch.object(api, "build_opener") as build:
            build.return_value.open.side_effect = http.client.IncompleteRead(b"private-api-credential", 500)
            with self.assertRaises(api._RequestFailure) as caught:
                api._post(b"{}", "private-api-credential", "https://api.openai.com/v1/responses", 10)
        self.assertEqual(caught.exception.reason, "transport")
        self.assertEqual(caught.exception.transport, {"exception_type": "IncompleteRead", "cause_type": "IncompleteRead"})
        self.assertNotIn("private-api-credential", json.dumps(caught.exception.transport))

    def test_transport_uses_bearer_header_and_classifies_http_errors_without_error_text(self):
        payload = b'{"model":"test"}'
        for status, code, expected in ((429, "rate_limit_exceeded", "rate_limited"),
                                       (503, "server_error", "server_unavailable"),
                                       (429, "insufficient_quota", None), (401, "invalid_api_key", None)):
            body = BytesIO(json.dumps({"error": {"code": code, "message": "private-api-credential"}}).encode())
            error = HTTPError("https://api.openai.com/v1/responses", status, "error", {"Retry-After": "15"}, body)
            with patch.object(api, "build_opener") as build, self.subTest(status=status, code=code):
                build.return_value.open.side_effect = error
                with self.assertRaises(api._RequestFailure) as caught:
                    api._post(payload, "private-api-credential", "https://api.openai.com/v1/responses", 10)
                self.assertEqual(caught.exception.reason, expected)
                request = build.return_value.open.call_args.args[0]
                self.assertEqual(request.get_header("Authorization"), "Bearer private-api-credential")
                self.assertEqual(request.get_method(), "POST")
                self.assertNotIn("private-api-credential", str(caught.exception))
        with patch.object(api, "build_opener") as build:
            build.return_value.open.side_effect = URLError("private-api-credential")
            with self.assertRaises(api._RequestFailure) as caught:
                api._post(payload, "private-api-credential", "https://api.openai.com/v1/responses", 10)
            self.assertEqual(caught.exception.reason, "transport")
            self.assertNotIn("private-api-credential", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
