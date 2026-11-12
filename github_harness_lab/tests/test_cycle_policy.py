"""Actual bounded arithmetic subprocess tests; no network or model calls."""
import copy
import unittest

from lab_harness.cycle_policy import (
    FAULTS, ISSUE, LOOP_ID, SCHEMA, inject_fault, test_source, validate_cycle_artifact,
)
from lab_harness.smoke_policy import REPOSITORY, SmokeError, digest, validate_source


GOOD = validate_source("def add(left, right):\n    return left + right\n\ndef multiply(left, right):\n    return left * right\n")


def artifact(revision=0):
    source, fault = inject_fault(GOOD, revision)
    return dict(schema=SCHEMA, repository=REPOSITORY, issue=ISSUE, loop_id=LOOP_ID,
                revision=revision, base_sha="a" * 40, parent_sha="a" * 40, workflow_sha="a" * 40,
                issue_body_sha256="b" * 64, source_path="src/arithmetic.py", source_code=source,
                source_sha256=digest(source), model_source_code=GOOD, model_source_sha256=digest(GOOD),
                fault_injection=fault, run_id="100", run_attempt="1", execution_stage=f"generate-{revision}",
                ci_stage=f"publish-{revision}", session_id="OFFLINE-SESSION", model="OFFLINE-MODEL", version="fixture",
                model_request_count=1, harness_status="completed", harness_exit_code=0,
                tests=test_source(source), model_tests=test_source(GOOD))


class CyclePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.artifacts = [artifact(i) for i in range(3)]

    def test_disclosed_faults_cause_real_fail_fail_pass(self):
        self.assertEqual([item["tests"]["exit_code"] for item in self.artifacts], [1, 1, 0])
        self.assertEqual([item["fault_injection"]["kind"] for item in self.artifacts], list(FAULTS))
        self.assertEqual(len({item["source_sha256"] for item in self.artifacts}), 3)
        for item in self.artifacts:
            self.assertEqual(item["tests"]["count"], 10)
            self.assertIn("Ran 10 tests", item["tests"]["log"])
            self.assertEqual(item["tests"]["log_sha256"], digest(item["tests"]["log"]))
            self.assertEqual(item["model_tests"]["exit_code"], 0)

    def test_faults_preserve_add_and_ast_scope(self):
        for item in self.artifacts:
            self.assertEqual(item["source_code"], validate_source(item["source_code"]))
            self.assertIn("def add(left, right):\n    return left + right", item["source_code"])
            self.assertTrue(item["fault_injection"]["disclosed"])
        self.assertEqual(self.artifacts[2]["source_code"], GOOD)

    def test_invalid_revisions_rejected_before_transformation(self):
        for revision in (-1, 3, True, 1.0, "1"):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                inject_fault(GOOD, revision)

    def test_arbitrary_code_never_reaches_subprocess(self):
        for source in ("import os\n" + GOOD, GOOD.replace("left * right", "__import__('os').getcwd()")):
            with self.subTest(source=source), self.assertRaises(SmokeError):
                test_source(source)

    def test_valid_artifacts_include_failed_product_tests(self):
        for value in self.artifacts:
            self.assertIs(validate_cycle_artifact(value), value)

    def test_source_and_injection_tampering_are_rejected(self):
        cases = [("source_code", GOOD), ("source_sha256", "d" * 64),
                 ("model_source_sha256", "d" * 64), ("fault_injection", {"kind": "none", "disclosed": True}),
                 ("source_path", "runner.py"), ("loop_id", "other"), ("issue", 3)]
        for key, changed in cases:
            value = copy.deepcopy(self.artifacts[0])
            value[key] = changed
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_cycle_artifact(value)

    def test_model_failure_or_incomplete_execution_cannot_publish(self):
        cases = [("model_tests", {"exit_code": 1}), ("model_request_count", True),
                 ("model_request_count", 2), ("harness_status", "unknown"),
                 ("execution_stage", "generate-2")]
        for key, changed in cases:
            value = copy.deepcopy(self.artifacts[0])
            value[key] = changed
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_cycle_artifact(value)

    def test_timeout_or_incomplete_test_receipts_rejected(self):
        for key, changed in (("timed_out", True), ("output_limit_exceeded", True), ("count", 9), ("exit_code", 2)):
            value = copy.deepcopy(self.artifacts[0])
            value["tests"][key] = changed
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_cycle_artifact(value)

    def test_output_is_bounded_and_no_credentials_are_in_test_logs(self):
        for item in self.artifacts:
            self.assertLess(len(item["tests"]["log"].encode()), 65536)
            self.assertNotIn("GH_CYCLE_READ_TOKEN", item["tests"]["log"])


if __name__ == "__main__":
    unittest.main()
