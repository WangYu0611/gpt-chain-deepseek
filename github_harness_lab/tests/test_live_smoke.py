"""Offline controller tests. No GitHub transport or model request is used.

The end-to-end fixture runs the real trusted arithmetic test process in a
TemporaryDirectory. Its model/session/runner names explicitly identify fiction;
no result from this test suite is an integration acceptance artifact.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from lab_harness.live_smoke import check_context, execute
from lab_harness.smoke_policy import ACTOR, REPOSITORY, SmokeError, digest, validate_result, validate_source


BASE = "a" * 40
ORIGINAL = "def add(left, right):\n    return left + right\n"
GOOD_SOURCE = ORIGINAL + "\ndef multiply(left, right):\n    return left * right\n"
BODY = "OFFLINE UNIT TEST ONLY: add multiply and preserve add."


def fixture_context():
    issue = {"number": 3, "state": "open", "user": {"login": ACTOR},
             "body": BODY, "labels": [{"name": "agent:ready"}]}
    config = {"repository": REPOSITORY, "issue_number": 3,
              "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
              "usage_authorization": "user-authorized-single-request",
              "issue_body_sha256": digest(BODY), "base_sha": BASE,
              "original_source_sha256": digest(ORIGINAL), "dsh": {"fixture_only": True}}
    event = {"repository": {"full_name": REPOSITORY, "private": True},
             "sender": {"login": ACTOR}, "action": "labeled",
             "label": {"name": "agent:ready"}, "issue": copy.deepcopy(issue)}
    environment = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": REPOSITORY,
                   "GITHUB_EVENT_NAME": "issues", "GITHUB_ACTOR": ACTOR,
                   "GITHUB_RUN_ID": "900000001", "GITHUB_RUN_ATTEMPT": "1",
                   "RUNNER_NAME": "UNIT-TEST-NO-REAL-RUNNER"}
    return config, event, environment, issue


class FakeReadOnlyClient:
    def __init__(self, issue):
        self.issue = copy.deepcopy(issue)
        self.base = BASE
        self.issue_reads = 0
        self.change_after_generation = False

    def get_issue(self, number):
        if number != self.issue["number"]:
            raise AssertionError("The controller read an unrelated Issue")
        self.issue_reads += 1
        result = copy.deepcopy(self.issue)
        if self.change_after_generation and self.issue_reads > 1:
            result["body"] += " changed"
        return result

    def get_ref(self, branch):
        if branch != "main":
            raise AssertionError("The controller read an unexpected branch")
        return {"object": {"sha": self.base}}

    def request(self, method, path, *, params):
        if (method, path, params) != ("GET", "/contents/src/arithmetic.py", {"ref": self.base}):
            raise AssertionError("Only the fixed read-only fixture request is allowed")
        return {"encoding": "base64", "content": base64.b64encode(ORIGINAL.encode()).decode()}


class FakeGenerate:
    def __init__(self, source=GOOD_SOURCE, *, fail=False):
        self.source = source
        self.fail = fail
        self.calls = 0

    def __call__(self, prompt, workspace, output_dir, config):
        self.calls += 1
        if len(prompt.encode("utf-8")) > 2048 or not workspace.is_dir():
            raise AssertionError("Unexpected prompt/workspace passed to model fixture")
        if config != {"fixture_only": True}:
            raise AssertionError("A real provider configuration reached the fixture")
        if self.fail:
            raise SmokeError("OFFLINE fixture simulates an interrupted model request")
        return {"final_text": self.source, "session_id": "OFFLINE-UNIT-TEST-SESSION",
                "model": "OFFLINE-FAKE-MODEL", "version": "fixture-no-cli",
                "request_count": 1}


class SourcePolicyTests(unittest.TestCase):
    def test_accepts_and_canonicalizes_only_the_two_task_functions(self):
        canonical = validate_source(GOOD_SOURCE)
        self.assertEqual(canonical, validate_source(canonical))
        self.assertEqual(canonical, validate_source("```python\n" + GOOD_SOURCE + "```"))

    def test_rejects_module_imports_and_executable_statements(self):
        for payload in ("import os\n", "from os import system\n", "answer = 42\n", "print('fixture')\n"):
            with self.subTest(payload=payload), self.assertRaises(SmokeError):
                validate_source(payload + GOOD_SOURCE)

    def test_rejects_call_attribute_and_other_non_arithmetic_expressions(self):
        expressions = ["abs(left)", "left.__class__", "__import__('os')", "left[0]",
                       "[left for right in []]", "left ** right", "left / right",
                       "left and right", "lambda: left", "100", "'code'"]
        for expression in expressions:
            with self.subTest(expression=expression), self.assertRaises(SmokeError):
                validate_source(ORIGINAL + "\ndef multiply(left, right):\n    return " + expression + "\n")

    def test_rejects_decorators_defaults_annotations_and_extra_statements(self):
        functions = ["@decorator\ndef multiply(left, right):\n    return left * right\n",
                     "def multiply(left, right=1):\n    return left * right\n",
                     "def multiply(left: int, right):\n    return left * right\n",
                     "def multiply(left, right) -> int:\n    return left * right\n",
                     "def multiply(left, right):\n    pass\n    return left * right\n",
                     "async def multiply(left, right):\n    return left * right\n"]
        for function in functions:
            with self.subTest(function=function), self.assertRaises(SmokeError):
                validate_source(ORIGINAL + "\n" + function)

    def test_existing_add_cannot_change_and_functions_must_be_distinct(self):
        for source in (GOOD_SOURCE.replace("left + right", "left - right"),
                       GOOD_SOURCE.replace("left + right", "right + left"),
                       GOOD_SOURCE.replace("def multiply", "def add"),
                       GOOD_SOURCE.replace("def add", "def subtract")):
            with self.subTest(source=source), self.assertRaises(SmokeError):
                validate_source(source)

    def test_source_size_and_depth_are_bounded(self):
        for source in (GOOD_SOURCE + "#" * 4097,
                       ORIGINAL + "\ndef multiply(left, right):\n    return -(-(-(-(-left))))\n"):
            with self.subTest(source=source[:50]), self.assertRaises(SmokeError):
                validate_source(source)


class ContextPolicyTests(unittest.TestCase):
    def test_valid_fixed_context_is_accepted(self):
        config, event, environment, _ = fixture_context()
        self.assertEqual(check_context(config, event, environment), 3)

    def test_rejects_wrong_actor_at_each_trust_boundary(self):
        for target in ("environment", "sender", "author"):
            config, event, environment, _ = fixture_context()
            if target == "environment": environment["GITHUB_ACTOR"] = "stranger"
            elif target == "sender": event["sender"]["login"] = "stranger"
            else: event["issue"]["user"]["login"] = "stranger"
            with self.subTest(target=target), self.assertRaises(SmokeError):
                check_context(config, event, environment)

    def test_rejects_wrong_repository_or_public_repository(self):
        for target in ("config", "environment", "event", "public"):
            config, event, environment, _ = fixture_context()
            if target == "config": config["repository"] = "someone/else"
            elif target == "environment": environment["GITHUB_REPOSITORY"] = "someone/else"
            elif target == "event": event["repository"]["full_name"] = "someone/else"
            else: event["repository"]["private"] = False
            with self.subTest(target=target), self.assertRaises(SmokeError):
                check_context(config, event, environment)

    def test_rejects_wrong_issue_body_state_or_label(self):
        for field, value in (("number", 4), ("body", BODY + " changed"), ("state", "closed"),
                             ("labels", []), ("pull_request", {"url": "not-an-issue"})):
            config, event, environment, _ = fixture_context()
            event["issue"][field] = value
            with self.subTest(field=field), self.assertRaises(SmokeError):
                check_context(config, event, environment)

    def test_rejects_expired_or_invalid_configuration(self):
        for expiry in ("2000-01-01T00:00:00Z", "not-a-dateZ", "2099-01-01T00:00:00+00:00"):
            config, event, environment, _ = fixture_context()
            config["expires_at"] = expiry
            with self.subTest(expiry=expiry), self.assertRaises(SmokeError):
                check_context(config, event, environment)

    def test_rejects_unapproved_usage_and_non_actions_execution(self):
        config, event, environment, _ = fixture_context()
        config["usage_authorization"] = "not-authorized"
        with self.assertRaises(SmokeError): check_context(config, event, environment)
        config, event, environment, _ = fixture_context()
        environment["GITHUB_ACTIONS"] = "false"
        with self.assertRaises(SmokeError): check_context(config, event, environment)


class OfflineControllerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="offline-smoke-unittest-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config, self.event, self.environment, issue = fixture_context()
        self.client = FakeReadOnlyClient(issue)
        self.generate = FakeGenerate()

    @property
    def output(self):
        return self.root / ".runtime" / "smoke-output" / "3"

    def run_fixture(self):
        return execute(self.config, self.event, self.environment, root=self.root,
                       client=self.client, generate=self.generate)

    def test_fake_model_runs_seven_real_tests_and_writes_bound_artifact(self):
        result = self.run_fixture()
        persisted = json.loads((self.output / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted, result)
        self.assertEqual(result["session_id"], "OFFLINE-UNIT-TEST-SESSION")
        self.assertEqual(result["model"], "OFFLINE-FAKE-MODEL")
        self.assertEqual(result["tests"]["count"], 7)
        self.assertEqual(result["tests"]["exit_code"], 0)
        self.assertEqual(result["source_sha256"], digest(result["source_code"]))
        self.assertEqual(result["base_sha"], BASE)
        self.assertEqual(self.generate.calls, 1)
        self.assertEqual(validate_result(result), result)
        claim = json.loads((self.output / "claim.json").read_text(encoding="utf-8"))
        self.assertEqual(claim["phase"], "completed")

    def test_duplicate_claim_never_generates_twice(self):
        self.run_fixture()
        self.environment["GITHUB_RUN_ATTEMPT"] = "2"
        with self.assertRaisesRegex(SmokeError, "already claimed"):
            self.run_fixture()
        self.assertEqual(self.generate.calls, 1)

    def test_interrupted_generation_remains_claimed_without_an_artifact(self):
        self.generate = FakeGenerate(fail=True)
        with self.assertRaisesRegex(SmokeError, "interrupted"):
            self.run_fixture()
        with self.assertRaisesRegex(SmokeError, "already claimed"):
            self.run_fixture()
        self.assertEqual(self.generate.calls, 1)
        self.assertFalse((self.output / "result.json").exists())

    def test_wrong_multiply_fails_real_tests_and_cannot_publish(self):
        self.generate = FakeGenerate(GOOD_SOURCE.replace("left * right", "left + right"))
        with self.assertRaisesRegex(SmokeError, "tests did not pass"):
            self.run_fixture()
        self.assertEqual(self.generate.calls, 1)
        self.assertFalse((self.output / "result.json").exists())

    def test_live_source_or_issue_mismatch_prevents_model_call(self):
        for changed in ("source", "issue", "base"):
            with self.subTest(changed=changed):
                saved_config = copy.deepcopy(self.config)
                saved_issue = copy.deepcopy(self.client.issue)
                saved_base = self.client.base
                if changed == "source": self.config["original_source_sha256"] = "0" * 64
                elif changed == "issue": self.client.issue["body"] += " changed"
                else: self.client.base = "b" * 40
                with self.assertRaises(SmokeError): self.run_fixture()
                self.assertEqual(self.generate.calls, 0)
                self.config, self.client.issue, self.client.base = saved_config, saved_issue, saved_base

    def test_task_change_during_generation_prevents_artifact(self):
        self.client.change_after_generation = True
        with self.assertRaisesRegex(SmokeError, "Issue no longer matches"):
            self.run_fixture()
        self.assertEqual(self.generate.calls, 1)
        self.assertFalse((self.output / "result.json").exists())

    def test_result_digest_and_machine_test_status_cannot_be_forged(self):
        evidence = self.run_fixture()
        mutations = [("source_sha256", "0" * 64), ("session_id", ""),
                     ("model_request_count", 2), ("harness_status", "failed")]
        for field, value in mutations:
            altered = copy.deepcopy(evidence)
            altered[field] = value
            with self.subTest(field=field), self.assertRaises(SmokeError): validate_result(altered)
        altered = copy.deepcopy(evidence)
        altered["tests"]["exit_code"] = 1
        with self.assertRaises(SmokeError): validate_result(altered)


if __name__ == "__main__":
    unittest.main()
