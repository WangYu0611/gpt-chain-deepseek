"""Explicit phase advancement retains pending manual evidence and old task ownership."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_unattended_engine as gate
from unattended.engine import Engine
from unattended.storage import read_json, write_json


class MilestoneAdvanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = gate.EngineGateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = self.fixture.config
        self.config["publication"] = {"mode": "milestone", "milestone_id": "M2", "goal": "Complete M2"}
        self.engine = self.fixture.engine
        self.head = "a" * 40
        accepted = [{"id": f"TASK-TEST-{index:04d}", "head_sha": self.head,
                     "publication": {"local_only": True, "deferred_to_milestone": "M2"}} for index in range(1, 67)]
        accepted[-1]["publication"].pop("deferred_to_milestone")
        deferred = [{"id": "MANUAL-1", "milestone_id": "M2", "head_sha": self.head,
                     "status": "pending_manual_confirmation", "reason": "HUD confirmation"}]
        review = {"decision": "blocked", "summary": "Manual verification remains", "feedback": "Confirm the HUD manually",
                  "head_sha": self.head}
        review_path = self.engine.runtime / "milestones/M2/review-1.json"
        write_json(review_path, review)
        milestone = {"id": "M2", "goal": "Complete M2", "head_sha": self.head, "base_sha": "b" * 40,
                     "proposal": {"action": "complete"}, "task_ids": [record["id"] for record in accepted],
                     "accepted_tasks": deepcopy(accepted), "deferred_tasks": deepcopy(deferred),
                     "check_results": [{"id": "behavior", "head_sha": self.head, "passed": True}],
                     "review": review, "review_path": str(review_path), "attempt": 1, "status": "candidate"}
        state = self.engine._load()
        state.update(phase="BLOCKED", blocked_from="MILESTONE_REVIEW", sequence=68, expected_head=self.head,
                     accepted=accepted, deferred=deferred, milestone=milestone, failures=1,
                     milestone_feedback="Pending manual checks", provider_retry={"old": "checkpoint"}, last_error="Manual verification remains")
        self.engine._save(state)
        self.original = read_json(self.engine.state_path)
        self.following = deepcopy(self.config)
        self.following.update(goal="Develop the authorized M3 milestone", publication={"mode": "milestone", "milestone_id": "M3", "goal": "Complete M3"})

    def state(self):
        return read_json(self.engine.state_path)

    def save(self, state):
        write_json(self.engine.state_path, state)

    def test_explicit_authorization_preserves_pending_stage_and_advances_only_planning(self):
        with patch("unattended.engine.gitops.publish") as publish:
            result = self.engine.advance_milestone(self.following, "用户授权保留人工待办并推进 M3")
        publish.assert_not_called()
        state = self.state()
        self.assertEqual((result["phase"], result["milestone_id"]), ("PLAN", "M3"))
        self.assertEqual(state["accepted"], self.original["accepted"])
        self.assertEqual(state["deferred"], self.original["deferred"])
        self.assertEqual((state["expected_head"], state["sequence"]), (self.head, 68))
        archive = state["milestone_history"][0]
        self.assertEqual(archive["milestone"], self.original["milestone"])
        self.assertEqual(archive["status"], "pending_manual_confirmation")
        self.assertEqual(read_json(Path(archive["transition"]["audit_path"])), archive)
        self.assertEqual(archive["milestone"]["review"]["decision"], "blocked")
        self.assertFalse(any(record.get("milestone_publication") for record in state["accepted"]))
        for key in ("milestone", "milestone_feedback", "blocked_from", "provider_retry"):
            self.assertNotIn(key, state)
        self.assertEqual(self.engine._pending_milestone_tasks(state), [])
        self.assertEqual(self.engine._deferred_tasks(state), [])
        fresh = Engine(self.following, self.fixture.providers, now=lambda: self.fixture.now)
        self.assertEqual(fresh.status()["phase"], "PLAN")
        self.fixture.providers.plan.assert_not_called()
        self.fixture.providers.review.assert_not_called()

    def test_blank_authorization_reason_and_invalid_target_do_not_change_checkpoint(self):
        for reason in (None, "", " "):
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                self.engine.advance_milestone(self.following, reason)
        for publication in ({"mode": "milestone", "milestone_id": "M2", "goal": "Same stage"},
                            {"mode": "milestone", "milestone_id": "M3", "goal": " "},
                            {"mode": "milestone", "milestone_id": "../M3", "goal": "Unsafe"}):
            target = deepcopy(self.following)
            target["publication"] = publication
            with self.subTest(publication=publication), self.assertRaises(ValueError):
                self.engine.advance_milestone(target, "Explicit authorization")
        self.assertEqual(self.state(), self.original)

    def test_active_task_operation_wrong_phase_dirty_or_changed_head_blocks_transition(self):
        for changes in ({"task": {"id": "TASK-ACTIVE"}}, {"operation": {}}, {"phase": "PLAN"}, {"blocked_from": "REVIEW"}):
            self.save({**deepcopy(self.original), **changes})
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.engine.advance_milestone(self.following, "Explicit authorization")
        self.save(deepcopy(self.original))
        with patch("unattended.engine.gitops.clean", return_value=False), self.assertRaises(RuntimeError):
            self.engine.advance_milestone(self.following, "Explicit authorization")
        with patch("unattended.engine.gitops.head", return_value="b" * 40), self.assertRaises(RuntimeError):
            self.engine.advance_milestone(self.following, "Explicit authorization")
        self.assertEqual(self.state(), self.original)

    def test_bound_configuration_fields_cannot_change(self):
        for key in ("runtime_dir", "project_path", "branch", "base_ref", "checks", "push", "remote", "task_prefix"):
            target = deepcopy(self.following)
            target[key] = [] if key == "checks" else not target[key] if key == "push" else str(target[key]) + "-changed"
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                self.engine.advance_milestone(target, "Explicit authorization")
        self.assertEqual(self.state(), self.original)

    def test_checks_and_review_must_be_complete_and_bound_to_current_head(self):
        variants = []
        for results in ([], [{"id": "behavior", "head_sha": self.head, "passed": False}],
                        [{"id": "behavior", "head_sha": "b" * 40, "passed": True}],
                        [{"id": "other", "head_sha": self.head, "passed": True}]):
            state = deepcopy(self.original)
            state["milestone"]["check_results"] = results
            variants.append(state)
        for review in ({"decision": "rework", "head_sha": self.head}, {"decision": "blocked", "head_sha": "b" * 40}):
            state = deepcopy(self.original)
            state["milestone"]["review"].update(review)
            variants.append(state)
        for state in variants:
            self.save(state)
            with self.subTest(milestone=state["milestone"]), self.assertRaises(ValueError):
                self.engine.advance_milestone(self.following, "Explicit authorization")
            self.assertEqual(self.state(), state)

    def test_original_review_receipt_must_match_and_accepted_review_keeps_manual_pending(self):
        path = Path(self.original["milestone"]["review_path"])
        altered = deepcopy(self.original["milestone"]["review"])
        altered["summary"] = "Different raw receipt"
        write_json(path, altered)
        with self.assertRaises(ValueError):
            self.engine.advance_milestone(self.following, "Explicit authorization")
        state = deepcopy(self.original)
        state["milestone"]["review"]["decision"] = "accepted"
        write_json(path, state["milestone"]["review"])
        self.save(state)
        self.engine.advance_milestone(self.following, "Keep manual checks pending and advance")
        self.assertEqual(self.state()["milestone_history"][0]["status"], "pending_manual_confirmation")
        self.assertEqual(self.state()["deferred"], self.original["deferred"])

    def test_current_stage_selection_excludes_old_tagged_and_legacy_records(self):
        self.assertEqual(len(self.engine._pending_milestone_tasks(self.original)), 66)
        self.engine.advance_milestone(self.following, "Explicit authorization")
        state = self.state()
        state["accepted"].extend([
            {"id": "TASK-M3", "publication": {"local_only": True, "deferred_to_milestone": "M3"}},
            {"id": "TASK-M4", "publication": {"local_only": True, "deferred_to_milestone": "M4"}},
            {"id": "TASK-LEGACY-NEW", "publication": {"local_only": True}},
        ])
        self.assertEqual([record["id"] for record in self.engine._pending_milestone_tasks(state)], ["TASK-M3", "TASK-LEGACY-NEW"])

    def test_failed_state_write_keeps_old_binding_and_does_not_duplicate_history_on_retry(self):
        with patch.object(self.engine, "_save", side_effect=OSError("Simulated checkpoint write failure")), self.assertRaises(OSError):
            self.engine.advance_milestone(self.following, "Explicit authorization")
        self.assertEqual(self.state(), self.original)
        self.assertEqual(self.engine.config["publication"]["milestone_id"], "M2")
        self.engine.advance_milestone(self.following, "Retry the same authorized transition")
        self.assertEqual(len(self.state()["milestone_history"]), 1)


if __name__ == "__main__":
    unittest.main()
