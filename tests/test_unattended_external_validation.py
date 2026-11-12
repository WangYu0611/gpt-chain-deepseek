"""Recover a real candidate/check handoff without inventing a code commit."""
from copy import deepcopy
from pathlib import Path
import unittest

from tests import test_unattended_recovery as recovery
from unattended import gitops
from unattended.storage import read_json, write_json


class ExternalValidationTests(unittest.TestCase):
    def setUp(self):
        self.f = recovery.RecoveryTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.config["checks"][0]["id"] = "ue-task-validation"
        self.f.engine = self.f.restart()
        self.f.plan()
        self.f.engine.step()
        self.f.engine.step()
        self.f.providers.review_decision = "rework"
        self.f.engine.step()
        self.f.providers.code_callback = lambda *args: {
            "completed": True, "session_id": "session-recovery", "text": "Waiting for external evidence."}
        for _ in range(3):
            self.f.engine.step()
        self.before = self.f.load()
        self.assertEqual(self.before["phase"], "BLOCKED")
        self.runtime = Path(self.f.config["runtime_dir"])
        self.task = self.before["task"]
        self.task["core_tests"] = ["ZhuGuoCity.M2.Save"]
        self.f.save(self.before)
        self.request_path = self.f.work / "Saved/Automation" / self.task["id"] / "validation-request.json"
        exclude = gitops.common_dir(self.f.work) / "info" / "exclude"
        with exclude.open("a", encoding="utf-8") as stream:
            stream.write("\nSaved/\n")
        self.request = {"version": 1, "task_id": self.task["id"],
                        "head_sha": self.task["candidate"]["head_sha"],
                        "tests": [{"name": "ZhuGuoCity.M2.Save", "mode": "nullrhi"}]}
        write_json(self.request_path, self.request)

    def test_recovery_preserves_candidate_session_budget_and_reviews(self):
        review_path = self.runtime / "tasks" / self.task["id"] / "review-0.json"
        original_review = review_path.read_bytes()
        calls = len(self.f.providers.code_calls)
        self.assertEqual(self.f.restart().retry(external_validation=True)["phase"], "CHECK")
        after = self.f.load()
        for key in ("candidate", "session_id", "code_base", "reworks", "code_failures"):
            self.assertEqual(after["task"][key], self.task[key])
        self.assertNotIn("manual_rework_allowance", after["task"])
        self.assertEqual(after["failures"], 0)
        self.assertEqual(after["task"]["check_results"], [])
        self.assertEqual(len(after["task"]["external_validation_recoveries"]), 1)
        self.assertEqual(review_path.read_bytes(), original_review)
        self.assertEqual(len(self.f.providers.code_calls), calls)
        # The candidate still goes through checks and independent review.
        self.assertEqual(self.f.restart().step()["phase"], "REVIEW")
        self.assertEqual(self.f.restart().step()["phase"], "CODE")
        self.assertEqual(self.f.providers.review_calls, 2)

    def test_wrong_phase_candidate_failure_or_review_cannot_resume_checks(self):
        failure_path = Path(self.task["code_failures"][-1])
        failure = read_json(failure_path)
        review_path = self.runtime / "tasks" / self.task["id"] / "review-0.json"
        review = read_json(review_path)
        for kind in ("phase", "candidate", "failure", "review", "active_operation"):
            with self.subTest(kind=kind):
                state = deepcopy(self.before)
                bad_failure, bad_review = dict(failure), dict(review)
                if kind == "phase": state["blocked_from"] = "REVIEW"
                elif kind == "candidate": state["task"]["code_base"] = "0" * 40
                elif kind == "failure": bad_failure["task_id"] = "OTHER-TASK"
                elif kind == "review": bad_review["decision"] = "accepted"
                else: state["operation"] = {"kind": "code"}
                self.f.save(state)
                write_json(failure_path, bad_failure)
                write_json(review_path, bad_review)
                with self.assertRaises(ValueError):
                    self.f.restart().retry(external_validation=True)
                self.assertEqual(self.f.load()["phase"], "BLOCKED")

    def test_dirty_work_or_stale_request_cannot_resume_checks(self):
        write_json(self.request_path, self.request | {"head_sha": "0" * 40})
        with self.assertRaises(ValueError):
            self.f.restart().retry(external_validation=True)
        write_json(self.request_path, self.request | {"tests": [{"name": "ZhuGuoCity.M2.Other", "mode": "nullrhi"}]})
        with self.assertRaises(ValueError):
            self.f.restart().retry(external_validation=True)
        write_json(self.request_path, self.request)
        (self.f.work / "feature.txt").write_text("Uncommitted user changes", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.f.restart().retry(external_validation=True)
        self.assertEqual(self.f.load()["phase"], "BLOCKED")

    def test_identical_recovery_cannot_be_repeated_or_combined_with_rework(self):
        with self.assertRaises(ValueError):
            self.f.restart().retry(rework=True, external_validation=True)
        self.f.restart().retry(external_validation=True)
        state = self.f.load()
        state.update(phase="BLOCKED", blocked_from="CODE")
        self.f.save(state)
        with self.assertRaisesRegex(ValueError, "相同补证请求"):
            self.f.restart().retry(external_validation=True)
        self.assertEqual(len(self.f.load()["task"]["external_validation_recoveries"]), 1)


if __name__ == "__main__":
    unittest.main()

