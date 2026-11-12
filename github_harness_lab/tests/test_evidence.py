import copy
import unittest

from lab_harness.evidence import validate_manifest


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {"schema_version": "1.0", "mode": "offline-demo", "issue": {"repository": "OWNER/REPOSITORY", "number": 1, "task_id": "T1"}, "pr": None, "base_commit_sha": "a" * 40, "result_commit_sha": "b" * 40, "harness": {"session_id": "fixture", "model": "offline-fixture", "version": "1", "exit_code": 0, "final_kind": "completed"}, "started_at": "2026-10-28T01:00:00Z", "finished_at": "2026-10-28T01:01:00Z", "changed_files": ["src/calc.py"], "tests": [{"id": "unit", "command": ["python", "test.py"], "exit_code": 0, "log": "tests/stdout.log", "timed_out": False, "output_limit_exceeded": False}], "ci": {"run_url": None, "head_sha": "b" * 40, "status": "NOT_RUN"}, "artifacts": [], "blocked_checks": ["Real integration NOT_RUN"], "status": "REVIEW"}

    def test_review_requires_base_and_result_commit(self):
        for name in ("base_commit_sha", "result_commit_sha"):
            value = self.manifest | {name: None}
            with self.assertRaises(ValueError):
                validate_manifest(value)

    def test_timeout_output_limit_and_nonzero_are_independent(self):
        for change in ({"exit_code": 1}, {"exit_code": False}, {"timed_out": True}, {"output_limit_exceeded": True}):
            value = copy.deepcopy(self.manifest)
            value["tests"][0].update(change)
            with self.assertRaises(ValueError):
                validate_manifest(value)

    def test_harness_and_tests_both_need_completion(self):
        for change in ({"exit_code": 1}, {"final_kind": "error"}, {"session_id": None}):
            value = copy.deepcopy(self.manifest)
            value["harness"].update(change)
            with self.assertRaises(ValueError):
                validate_manifest(value)

    def test_invalid_modes_statuses_and_timestamps_are_rejected(self):
        for change in ({"mode": "real-ish"}, {"status": "ACCEPTED"}, {"finished_at": "2026-10-27T01:01:00Z"}):
            with self.assertRaises(ValueError):
                validate_manifest(self.manifest | change)

    def test_local_review_does_not_imply_real_ci_or_pr(self):
        validate_manifest(self.manifest)
        self.assertEqual(self.manifest["mode"], "offline-demo")
        self.assertIsNone(self.manifest["pr"])
        self.assertEqual(self.manifest["ci"]["status"], "NOT_RUN")


if __name__ == "__main__":
    unittest.main()
