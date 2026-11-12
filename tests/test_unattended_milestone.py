"""Milestone gates and interrupted publication with real local Git, no network."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from tests import test_unattended_recovery as recovery
from unattended import gitops
from unattended.storage import read_json


class _AfterRemotePushCrash(BaseException):
    pass


class _MilestoneProviders(recovery.FakeProviders):
    def __init__(self, runtime):
        super().__init__()
        self.runtime = runtime
        self.milestone_decision = "accepted"
        self.milestone_head = None
        self.milestone_prompts = []

    def plan(self, prompt, cwd, out, timeout):
        state = read_json(self.runtime / "state.json")
        if state["accepted"]:
            self.plan_calls += 1
            return {"action": "complete", "title": "", "body": "", "acceptance_criteria": [],
                    "reason": "The full configured M2 milestone appears complete; independent stage checks are required."}
        return super().plan(prompt, cwd, out, timeout)

    def review(self, prompt, cwd, out, timeout):
        state = read_json(self.runtime / "state.json")
        if state["phase"] != "MILESTONE_REVIEW":
            return super().review(prompt, cwd, out, timeout)
        self.review_calls += 1
        self.milestone_prompts.append(prompt)
        return {"decision": self.milestone_decision, "summary": "Independent full milestone review",
                "feedback": "Fix milestone evidence before publication" if self.milestone_decision != "accepted" else "",
                "head_sha": self.milestone_head or gitops.head(cwd)}


class MilestoneTests(unittest.TestCase):
    def setUp(self):
        fixture = recovery.RecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.runtime = Path(fixture.config["runtime_dir"])
        fixture.config["publication"] = {
            "mode": "milestone", "milestone_id": "M2",
            "goal": "Complete all M2 behavior and independently verify its full runtime evidence; stop before M3.",
        }
        self.remote = fixture.root / "remote.git"
        self.remote.mkdir()
        gitops.git(self.remote, "init", "--bare")
        gitops.git(fixture.project, "remote", "add", "origin", str(self.remote))
        self.counter = self.runtime / "independent-check-count.txt"
        self.failure_flag = self.runtime / "fail-stage-check"
        script = (
            "from pathlib import Path\n"
            "import sys\n"
            "counter, failure = Path(sys.argv[1]), Path(sys.argv[2])\n"
            "counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))\n"
            "print('FAIL stage' if failure.exists() else 'PASS independent behavior')\n"
            "raise SystemExit(1 if failure.exists() else 0)\n"
        )
        fixture.config["checks"] = [{"id": "stage-behavior", "argv": [sys.executable, "-c", script,
            str(self.counter), str(self.failure_flag)], "timeout_seconds": 30,
            "success_patterns": ["PASS independent behavior"]}]
        fixture.providers = _MilestoneProviders(self.runtime)
        fixture.engine = fixture.restart()

    def _remote_head(self):
        return gitops.git(self.remote, "for-each-ref", "--format=%(objectname)",
                          "refs/heads/" + self.fixture.config["branch"])

    def _accept_task_locally(self):
        fixture = self.fixture
        fixture.plan()
        for expected in ("CHECK", "REVIEW", "PUBLISH", "PLAN"):
            self.assertEqual(fixture.restart().step()["phase"], expected)
        state = fixture.load()
        self.assertEqual(len(state["accepted"]), 1)
        self.assertEqual(int(self.counter.read_text()), 1)
        return state

    def _propose_milestone(self):
        self._accept_task_locally()
        self.assertEqual(self.fixture.restart().step()["phase"], "MILESTONE_REVIEW")
        return self.fixture.load()

    def _review_milestone(self):
        self._propose_milestone()
        self.assertEqual(self.fixture.restart().step()["phase"], "MILESTONE_REVIEW")
        self.assertEqual(int(self.counter.read_text()), 2)
        self.assertEqual(self.fixture.providers.review_calls, 1)
        return self.fixture.restart().step()

    def test_task_acceptance_stays_local_even_when_legacy_push_is_true(self):
        fixture = self.fixture
        fixture.config["push"] = True
        with patch.object(gitops, "publish", wraps=gitops.publish) as publish:
            state = self._accept_task_locally()
        publish.assert_not_called()
        self.assertTrue(state["accepted"][0]["publication"]["local_only"])
        self.assertEqual(self._remote_head(), "")

    def test_complete_proposal_requires_fresh_checks_and_distinct_review_before_push(self):
        fixture = self.fixture
        with patch.object(gitops, "publish", wraps=gitops.publish) as publish:
            state = self._propose_milestone()
            milestone = state["milestone"]
            self.assertEqual(milestone["id"], "M2")
            self.assertEqual(milestone["goal"], fixture.config["publication"]["goal"])
            self.assertEqual(milestone["head_sha"], gitops.head(fixture.work))
            self.assertEqual(milestone["task_ids"], ["TASK-AUTO-0001"])
            self.assertEqual(milestone["check_results"], [])
            self.assertEqual(int(self.counter.read_text()), 1)
            self.assertEqual(fixture.providers.review_calls, 1)
            publish.assert_not_called()
            self.assertEqual(fixture.restart().step()["phase"], "MILESTONE_REVIEW")
            self.assertEqual(int(self.counter.read_text()), 2)
            self.assertEqual(fixture.providers.review_calls, 1)
            self.assertEqual(fixture.restart().step()["phase"], "MILESTONE_PUBLISH")
            self.assertEqual(fixture.providers.review_calls, 2)
            self.assertIn(fixture.config["publication"]["goal"], fixture.providers.milestone_prompts[-1])
            self.assertIn("TASK-AUTO-0001", fixture.providers.milestone_prompts[-1])
            publish.assert_not_called()
            self.assertEqual(self._remote_head(), "")
            final = fixture.restart().step()
            self.assertEqual(final["phase"], "DONE")
            self.assertEqual(publish.call_count, 1)
        # Publication mode itself authorizes this final push despite push=false.
        self.assertFalse(fixture.config["push"])
        self.assertEqual(self._remote_head(), gitops.head(fixture.work))
        report = read_json(self.runtime / "milestones" / "M2" / "report.json")
        self.assertEqual(report["head_sha"], gitops.head(fixture.work))
        fixture.restart().step()
        self.assertEqual(fixture.providers.plan_calls, 2)
        self.assertEqual((fixture.project / "feature.txt").read_text(), "original\n")

    def test_milestone_rework_returns_to_planning_with_feedback_without_push(self):
        fixture = self.fixture
        fixture.providers.milestone_decision = "rework"
        with patch.object(gitops, "publish", wraps=gitops.publish) as publish:
            self.assertEqual(self._review_milestone()["phase"], "PLAN")
        publish.assert_not_called()
        state = fixture.load()
        self.assertIn("Fix milestone evidence", json.dumps(state["milestone_feedback"]))
        self.assertEqual(len(state["accepted"]), 1)
        self.assertEqual(self._remote_head(), "")
        # Resume a checkpoint already at its unchanged-HEAD rework limit. A
        # repeated complete proposal must stop before another check/review cycle.
        state["milestone"]["same_head_reworks"] = fixture.config["max_reworks"]
        fixture.save(state)
        check_count = int(self.counter.read_text())
        review_count = fixture.providers.review_calls
        with patch.object(gitops, "publish", wraps=gitops.publish) as publish:
            self.assertEqual(fixture.restart().step()["phase"], "BLOCKED")
        self.assertEqual(int(self.counter.read_text()), check_count)
        self.assertEqual(fixture.providers.review_calls, review_count)
        publish.assert_not_called()
        self.assertEqual(self._remote_head(), "")

    def test_failed_fresh_check_cannot_be_accepted_or_sent_to_reviewer(self):
        fixture = self.fixture
        self._propose_milestone()
        self.failure_flag.write_text("fail the independently rerun stage check", encoding="utf-8")
        with patch.object(gitops, "publish", wraps=gitops.publish) as publish:
            result = fixture.restart().step()
            self.assertEqual(fixture.restart().step()["phase"], "PLAN")
        self.assertNotIn(result["phase"], {"DONE", "MILESTONE_PUBLISH"})
        self.assertEqual(int(self.counter.read_text()), 2)
        self.assertEqual(fixture.providers.review_calls, 1)
        publish.assert_not_called()
        self.assertFalse(fixture.load()["milestone"]["check_results"][0]["passed"])
        self.assertEqual(self._remote_head(), "")

    def test_milestone_only_regression_is_required_for_stage_publication(self):
        fixture = self.fixture
        marker = self.runtime / "regression-ran"
        fixture.config["milestone_checks"] = [{"id": "full-regression", "timeout_seconds": 30,
            "argv": [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(marker)]}]
        proposal = self._propose_milestone()
        self.assertFalse(marker.exists())
        self.assertEqual(proposal["milestone"]["milestone_checks"], fixture.config["milestone_checks"])
        self.assertEqual(fixture.restart().step()["phase"], "MILESTONE_REVIEW")
        self.assertFalse(marker.exists())
        self.assertEqual(fixture.restart().step()["phase"], "MILESTONE_REVIEW")
        self.assertTrue(marker.exists())
        self.assertEqual(fixture.restart().step()["phase"], "MILESTONE_PUBLISH")
        ready = fixture.load()
        results = ready["milestone"]["check_results"]
        self.assertEqual([result["id"] for result in results], ["stage-behavior", "full-regression"])
        for invalid in ([], [dict(results[-1], head_sha="b" * 40)], [dict(results[-1], passed=False)]):
            with self.subTest(invalid=invalid):
                checkpoint = copy.deepcopy(ready)
                checkpoint["milestone"]["check_results"] = results[:-1] + invalid
                fixture.save(checkpoint)
                self.assertGreater(fixture.restart().step()["failures"], 0)
                self.assertEqual(self._remote_head(), "")
        fixture.save(ready)
        self.assertEqual(fixture.restart().step()["phase"], "DONE")

    def test_milestone_check_config_cannot_change_during_review(self):
        fixture = self.fixture
        extra = {"id": "full-regression", "argv": [sys.executable, "-c", "print('regression')"],
                 "timeout_seconds": 30}
        fixture.config["milestone_checks"] = [extra]
        self._propose_milestone()
        self.assertEqual(fixture.restart().step()["phase"], "MILESTONE_REVIEW")
        extra["timeout_seconds"] = 60
        result = fixture.restart().step()
        self.assertIn("阶段检查配置发生变化", result["last_error"])
        self.assertEqual(fixture.providers.review_calls, 1)
        self.assertEqual(self._remote_head(), "")

    def test_dirty_workspace_or_changed_head_cannot_start_stage_checks_or_review(self):
        fixture = self.fixture
        self._propose_milestone()
        unwanted = fixture.work / "untracked.txt"
        unwanted.write_text("outside edit", encoding="utf-8")
        with patch.object(gitops, "publish", wraps=gitops.publish) as publish:
            first = fixture.restart().step()
            self.assertGreater(first["failures"], 0)
            unwanted.unlink()
            fixture.providers.commit(fixture.work, "unexpected external commit\n")
            second = fixture.restart().step()
        self.assertGreater(second["failures"], 0)
        self.assertNotIn(second["phase"], {"DONE", "MILESTONE_PUBLISH"})
        self.assertEqual(int(self.counter.read_text()), 1)
        self.assertEqual(fixture.providers.review_calls, 1)
        publish.assert_not_called()
        self.assertEqual(self._remote_head(), "")

    def test_stage_review_for_another_head_cannot_publish(self):
        fixture = self.fixture
        fixture.providers.milestone_head = "b" * 40
        with patch.object(gitops, "publish", wraps=gitops.publish) as publish:
            result = self._review_milestone()
        self.assertGreater(result["failures"], 0)
        self.assertNotIn(result["phase"], {"DONE", "MILESTONE_PUBLISH"})
        publish.assert_not_called()
        self.assertEqual(self._remote_head(), "")

    def test_restart_after_actual_remote_push_reconciles_without_a_second_push(self):
        fixture = self.fixture
        self.assertEqual(self._review_milestone()["phase"], "MILESTONE_PUBLISH")
        expected = gitops.head(fixture.work)
        original_publish = gitops.publish

        def push_then_crash(*args, **kwargs):
            original_publish(*args, **kwargs)
            raise _AfterRemotePushCrash("Process disappeared after successful remote push")
        with patch.object(gitops, "publish", side_effect=push_then_crash):
            with self.assertRaises(_AfterRemotePushCrash):
                fixture.restart().step()
        self.assertEqual(self._remote_head(), expected)
        self.assertEqual(fixture.load()["phase"], "MILESTONE_PUBLISH")
        original_git = gitops.git
        pushes = []

        def observed_git(root, *args, **kwargs):
            if args and args[0] == "push":
                pushes.append(args)
            return original_git(root, *args, **kwargs)
        with patch.object(gitops, "git", side_effect=observed_git):
            self.assertEqual(fixture.restart().step()["phase"], "DONE")
        self.assertEqual(pushes, [])
        self.assertTrue(fixture.load()["milestone"]["publication"]["already_published"])
        self.assertEqual(self._remote_head(), expected)
        self.assertTrue((self.runtime / "milestones" / "M2" / "report.json").is_file())

    def test_publication_scope_is_bound_to_existing_checkpoint(self):
        fixture = self.fixture
        self._accept_task_locally()
        original = copy.deepcopy(fixture.config["publication"])
        for field, value in (("goal", "Different stage scope"), ("milestone_id", "M3")):
            fixture.config["publication"] = dict(original, **{field: value})
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "不匹配"):
                fixture.restart().status()

    def test_absent_publication_accepts_legacy_binding_but_new_policy_requires_migration(self):
        fixture = self.fixture
        publication = fixture.config.pop("publication")
        old_keys = ("project_path", "goal", "branch", "base_ref", "checks", "push", "remote", "task_prefix")
        old_values = {key: fixture.config.get(key) for key in old_keys}
        legacy_digest = hashlib.sha256(json.dumps(old_values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        engine = fixture.restart()
        self.assertEqual(engine._binding(), legacy_digest)
        old_state = engine._load()
        old_state["binding"] = legacy_digest
        engine._save(old_state)
        self.assertEqual(fixture.restart().status()["phase"], "PLAN")
        fixture.config["publication"] = publication
        with self.assertRaisesRegex(ValueError, "不匹配"):
            fixture.restart().status()


if __name__ == "__main__":
    unittest.main()
