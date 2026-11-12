"""Explicit user deferral carries checked code, never an accepted-task receipt."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_unattended_recovery as recovery
from tests.test_unattended_burst import _SerialProviders
from unattended import gitops
from unattended.providers import ProviderError
from unattended.storage import read_json, write_json


class DeferTests(unittest.TestCase):
    def setUp(self):
        self.f = recovery.RecoveryTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.runtime = Path(self.f.config["runtime_dir"])

    def blocked(self, *, milestone=False):
        if milestone:
            self.f.config["publication"] = {"mode": "milestone", "milestone_id": "M2", "goal": "All required evidence, including HUD behavior"}
            self.f.engine = self.f.restart()
        self.f.plan()
        self.assertEqual(self.f.engine.step()["phase"], "CHECK")
        self.assertEqual(self.f.engine.step()["phase"], "REVIEW")
        self.f.providers.review_decision = "blocked"
        self.assertEqual(self.f.engine.step()["phase"], "BLOCKED")
        state = self.f.load()
        path = self.runtime / "tasks" / state["task"]["id"] / "review-0.json"
        return state, path

    def test_defer_preserves_original_evidence_without_acceptance_push_or_model_and_next_task_is_fresh(self):
        before, path = self.blocked()
        old_review = path.read_bytes()
        task_path = path.parent / "TASK.md"
        old_task = task_path.read_bytes()
        self.f.providers = _SerialProviders(self.runtime)
        self.f.engine = self.f.restart()
        with patch.object(gitops, "publish", side_effect=AssertionError("Defer must not push")):
            result = self.f.engine.defer("User did not observe flicker; keep it for manual confirmation")
        self.assertEqual(result["phase"], "PLAN")
        state = self.f.load()
        self.assertEqual(state["accepted"], before["accepted"])
        self.assertEqual(state["sequence"], before["sequence"] + 1)
        self.assertEqual(state["expected_head"], before["task"]["candidate"]["head_sha"])
        self.assertIsNone(state["task"])
        self.assertIsNone(state["operation"])
        self.assertEqual(state["failures"], 0)
        self.assertEqual(path.read_bytes(), old_review)
        self.assertEqual(task_path.read_bytes(), old_task)
        receipt = read_json(path.parent / "deferred-task.json")
        self.assertEqual(receipt["task"]["id"], before["task"]["id"])
        self.assertEqual(receipt["review"], read_json(path))
        self.assertEqual(receipt["status"], "pending_manual_confirmation")
        self.assertFalse((path.parent / "accepted.json").exists())
        self.assertFalse(self.f.providers.plan_calls or self.f.providers.code_calls or self.f.providers.review_calls)
        with self.assertRaises(ValueError):
            self.f.engine.defer("repeat")
        with patch.object(self.f.providers, "plan", wraps=self.f.providers.plan) as plan:
            self.assertEqual(self.f.engine.step()["phase"], "CODE")
        self.assertIn(state["deferred"][0]["reason"], plan.call_args.args[0])
        self.assertEqual(self.f.load()["task"]["id"], "TASK-AUTO-0002")
        self.assertIsNone(self.f.load()["task"]["session_id"])
        self.assertEqual(self.f.engine.step()["phase"], "CHECK")
        self.assertIsNone(self.f.providers.code_calls[0]["session_id"])
        self.assertNotEqual(self.f.load()["task"]["session_id"], before["task"]["session_id"])

    def test_defer_outside_window_only_records_explicit_decision_then_waits(self):
        _, _ = self.blocked()
        calls = (self.f.providers.plan_calls, len(self.f.providers.code_calls), self.f.providers.review_calls)
        self.f.clock[0] = self.f.clock[0].replace(hour=10)
        self.assertEqual(self.f.engine.defer("Confirm manually later")["phase"], "PLAN")
        self.assertEqual(self.f.restart().step()["status"], "waiting_window")
        self.assertEqual((self.f.providers.plan_calls, len(self.f.providers.code_calls), self.f.providers.review_calls), calls)

    def test_defer_rejects_wrong_state_receipt_head_round_and_operation(self):
        baseline, path = self.blocked()
        original_review = read_json(path)
        mutations = [
            lambda s, r: s.update(phase="REVIEW"),
            lambda s, r: s.update(blocked_from="CODE"),
            lambda s, r: s["task"]["candidate"].update(head_sha="0" * 40),
            lambda s, r: r.update(head_sha="0" * 40),
            lambda s, r: r.update(decision="accepted"),
            lambda s, r: s["task"].update(reworks=1),
            lambda s, r: r.pop("feedback"),
            lambda s, r: s.update(operation={"kind": "review", "task_id": "TASK-OTHER", "head_sha": r["head_sha"]}),
            lambda s, r: s.update(operation={"kind": "code", "task_id": s["task"]["id"], "head_sha": r["head_sha"]}),
            lambda s, r: s.update(operation={"kind": "review", "task_id": s["task"]["id"], "head_sha": "0" * 40}),
        ]
        for mutate in mutations:
            state, review = deepcopy(baseline), dict(original_review)
            mutate(state, review)
            self.f.save(state)
            write_json(path, review)
            with self.subTest(mutate=mutate), self.assertRaises((ValueError, ProviderError)):
                self.f.restart().defer("Pending manual confirmation")
            self.assertFalse((path.parent / "deferred-task.json").exists())
            self.assertNotIn("deferred", self.f.load())
        self.f.save(baseline)
        write_json(path, original_review)
        path.unlink()
        with self.assertRaisesRegex(ValueError, "回执"):
            self.f.restart().defer("Missing review")

    def test_defer_rejects_dirty_workspace_and_failed_missing_or_stale_checks(self):
        baseline, path = self.blocked()
        variants = [[], baseline["task"]["check_results"] * 2]
        for field, value in (("id", "unconfigured-check"), ("passed", False), ("passed", 1), ("head_sha", "0" * 40)):
            checks = deepcopy(baseline["task"]["check_results"])
            checks[0][field] = value
            variants.append(checks)
        for checks in variants:
            state = deepcopy(baseline)
            state["task"]["check_results"] = checks
            self.f.save(state)
            with self.subTest(checks=checks), self.assertRaisesRegex(ValueError, "检查"):
                self.f.restart().defer("Not sufficiently checked")
        self.f.save(baseline)
        (self.f.work / "feature.txt").write_text("dirty", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "HEAD"):
            self.f.restart().defer("Dirty")
        self.assertFalse((path.parent / "deferred-task.json").exists())

    def test_defer_accepts_rework_receipt_but_requires_reason_and_cannot_repeat_saved_record(self):
        baseline, path = self.blocked()
        for reason in (None, "", "  "):
            with self.assertRaises(ValueError):
                self.f.engine.defer(reason)
        review = read_json(path)
        review["decision"] = "rework"
        write_json(path, review)
        self.f.engine.defer("Explicitly defer this requested rework")
        self.assertEqual(self.f.load()["deferred"][0]["status"], "pending_manual_confirmation")
        self.f.save(baseline)
        with self.assertRaisesRegex(ValueError, "回执"):
            self.f.restart().defer("Cannot duplicate the durable receipt")

    def test_defer_recovers_receipt_written_before_state_without_rewriting_or_model_calls(self):
        before, review_path = self.blocked()
        reason = "Explicit user deferral pending manual confirmation"
        calls = (self.f.providers.plan_calls, len(self.f.providers.code_calls), self.f.providers.review_calls)
        with patch.object(self.f.engine, "_save", side_effect=RuntimeError("crash after receipt")):
            with self.assertRaisesRegex(RuntimeError, "crash after receipt"):
                self.f.engine.defer(reason)
        self.assertEqual(self.f.load(), before)
        path = review_path.parent / "deferred-task.json"
        original_bytes = path.read_bytes()
        original_time = read_json(path)["deferred_at"]
        # Legacy checkpoints may not yet have the owner field even though the
        # durable receipt has it. Claiming the same session normalizes this.
        legacy = self.f.load()
        legacy["task"].pop("session_owner_task_id", None)
        self.f.save(legacy)
        self.f.clock[0] = self.f.clock[0].replace(hour=20)
        self.assertEqual(self.f.restart().defer(reason)["phase"], "PLAN")
        state = self.f.load()
        self.assertEqual(len(state["deferred"]), 1)
        self.assertEqual(state["deferred"][0]["deferred_at"], original_time)
        self.assertEqual(state["accepted"], before["accepted"])
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual((self.f.providers.plan_calls, len(self.f.providers.code_calls), self.f.providers.review_calls), calls)
        with self.assertRaises(ValueError):
            self.f.restart().defer(reason)

    def test_completion_proposal_cannot_hide_deferred_task_without_milestone(self):
        self.blocked()
        self.f.engine.defer("Await manual confirmation")
        with patch.object(self.f.providers, "plan", return_value={"action": "complete", "reason": "Everything else complete"}):
            self.assertEqual(self.f.engine.step()["phase"], "BLOCKED")
        self.assertEqual(self.f.load()["blocked_from"], "PLAN")
        self.assertNotIn("completion_reason", self.f.load())

    def test_milestone_candidate_evidence_review_and_publish_gate_keep_deferrals_separate(self):
        before, _ = self.blocked(milestone=True)
        goal = self.f.config["publication"]["goal"]
        self.f.engine.defer("HUD flicker awaits user confirmation, other development continues")
        checkpoint = self.f.load()
        current_deferred = deepcopy(checkpoint["deferred"])
        historical = {**current_deferred[0], "id": "TASK-AUTO-0000", "milestone_id": "M1",
                      "reason": "Historical HUD review remains open"}
        archive = {"milestone": {"id": "M1", "review_path": str(self.runtime / "milestones/M1/review-2.json")},
                   "status": "pending_manual_confirmation",
                   "transition": {"audit_path": str(self.runtime / "milestones/M1/advance-to-M2.json")}}
        checkpoint["deferred"].append(historical)
        checkpoint["milestone_history"] = [archive]
        self.f.save(checkpoint)
        with patch.object(self.f.providers, "plan", return_value={"action": "complete", "reason": "Other work is finished"}) as plan:
            self.assertEqual(self.f.engine.step()["phase"], "MILESTONE_REVIEW")
        self.assertIn("人工待办可继续保留", plan.call_args.args[0])
        state = self.f.load()
        milestone = state["milestone"]
        self.assertEqual(milestone["goal"], goal)
        self.assertEqual(milestone["accepted_tasks"], [])
        self.assertEqual(milestone["task_ids"], [])
        self.assertEqual(milestone["deferred_tasks"], current_deferred)
        evidence = self.f.engine._milestone_evidence(milestone)
        self.assertIn("deferred", evidence[0])
        self.assertNotIn("accepted", evidence[0])
        self.assertTrue(Path(evidence[0]["deferred_task_path"]).exists())
        self.assertEqual(self.f.engine.step()["phase"], "MILESTONE_REVIEW")
        blocked_review = {"decision": "blocked", "summary": "External system verification is unavailable",
                          "feedback": "An independent system dependency is blocked alongside manual follow-up.",
                          "head_sha": milestone["head_sha"]}
        with patch.object(self.f.providers, "review", return_value=blocked_review), patch.object(gitops, "publish") as publish:
            self.assertEqual(self.f.engine.step()["phase"], "BLOCKED")
            publish.assert_not_called()
        with patch.object(gitops, "publish", side_effect=AssertionError("A blocked system review cannot publish")):
            with self.assertRaisesRegex(RuntimeError, "独立验收"):
                self.f.engine._milestone_publish(self.f.load(), self.f.work)
        self.assertEqual(self.f.engine.retry()["phase"], "MILESTONE_REVIEW")
        self.f.providers.review_decision = "accepted"
        with patch.object(self.f.providers, "review", wraps=self.f.providers.review) as review:
            self.assertEqual(self.f.engine.step()["phase"], "MILESTONE_PUBLISH")
        self.assertIn(state["deferred"][0]["reason"], review.call_args.args[0])
        self.assertIn("系统部分有完整证据且通过时返回 accepted", review.call_args.args[0])
        ready = self.f.load()
        changed = deepcopy(ready)
        changed["deferred"][0]["reason"] = "changed candidate evidence"
        with self.assertRaisesRegex(RuntimeError, "集合"):
            self.f.engine._milestone_candidate(changed, self.f.work)
        with patch.object(gitops, "publish", return_value={"head_sha": milestone["head_sha"]}) as publish:
            self.assertEqual(self.f.engine.step()["phase"], "DONE")
            publish.assert_called_once_with(self.f.work, self.f.config, milestone["head_sha"])
        published = self.f.load()
        self.assertEqual(published["accepted"], before["accepted"])
        self.assertEqual(published["deferred"], state["deferred"])
        self.assertEqual(published["milestone_history"], [archive])
        self.assertEqual(published["milestone"]["status"], "published")
        report = read_json(self.runtime / "milestones" / "M2" / "report.json")
        self.assertEqual(report["system_verification"], {"status": "passed", "head_sha": milestone["head_sha"]})
        self.assertEqual(report["manual_acceptance"], {"status": "pending_manual_confirmation",
                                                      "pending_task_ids": [state["deferred"][0]["id"]]})
        self.assertEqual(report["deferred_tasks"], current_deferred)
        self.assertEqual(report["historical_deferred_tasks"], [historical])
        self.assertEqual(report["historical_pending_milestones"], [{
            "id": "M1", "status": "pending_manual_confirmation", "review_path": archive["milestone"]["review_path"],
            "advance_audit_path": archive["transition"]["audit_path"],
        }])
        markdown = (self.runtime / "milestones" / "M2" / "REPORT.md").read_text(encoding="utf-8")
        for expected in (historical["reason"], archive["milestone"]["review_path"], archive["transition"]["audit_path"]):
            self.assertIn(expected, markdown)


if __name__ == "__main__":
    unittest.main()
