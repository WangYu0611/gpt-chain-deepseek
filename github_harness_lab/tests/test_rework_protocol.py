"""Offline protocol contract tests. No GitHub, ChatGPT or DSH requests occur."""
import copy
import dataclasses
import json
import unittest

from lab_harness.rework_protocol import (
    MARKER, SCHEMA, Policy, ProtocolError, VerifiedEvidence, encode_record, replay_journal,
)


REPO = "WangYu0611/chatgpt-deepseek-harness-test-20261028"
BASE, WORKFLOW, CRITERIA = "a" * 40, "b" * 40, "c" * 64
HEADS = [str(i) * 40 for i in range(1, 5)]


def policy(**changes):
    values = dict(repository=REPO, issue=99, pr=100, base_sha=BASE,
                  criteria_sha256=CRITERIA, workflow_sha=WORKFLOW,
                  publisher_login="github-actions[bot]", reviewer_login="WangYu0611",
                  automation_id="fixture-task-not-a-real-task",
                  required_criteria=("arithmetic-correctness", "scope-safety", "execution-evidence"))
    values.update(changes)
    return Policy(**values)


def common(kind, revision, previous):
    return dict(schema=SCHEMA, kind=kind, repository=REPO, issue=99, pr=100,
                base_sha=BASE, criteria_sha256=CRITERIA, head_sha=HEADS[revision],
                revision=revision, previous_event_id=previous)


def candidate(revision, previous):
    value = common("candidate", revision, previous)
    value.update(parent_sha=BASE if revision == 0 else HEADS[revision - 1],
                 execution=dict(run_id=1000 + revision, run_attempt=1, workflow_sha=WORKFLOW, stage_key=f"generate{revision}",
                                session_id=f"offline-dsh-{revision}", source_sha256=str(revision + 1) * 64,
                                artifact_sha256=str(revision + 4) * 64, completed=True, model_requests=1),
                 ci=dict(run_id=2000 + revision, run_attempt=1, workflow_sha=WORKFLOW, stage_key=f"publish{revision}",
                         tested_head_sha=HEADS[revision], status="completed",
                         conclusion="success" if revision >= 2 else "failure"))
    return value


def review(revision, previous, verdict):
    value = common("review", revision, previous)
    value.update(ci_run_id=2000 + revision, ci_run_attempt=1, ci_stage_key=f"publish{revision}", verdict=verdict,
                 provenance=dict(automation_id="fixture-task-not-a-real-task",
                                 run_id=f"offline-web-{revision}", trigger="github_pr_event"),
                 findings=[dict(criterion=key, status=verdict if key == "arithmetic-correctness" else "PASS",
                                detail="Offline fixture assertion; not live evidence.",
                                evidence_url=f"https://github.com/{REPO}/actions/runs/{2000 + revision}")
                           for key in ("arithmetic-correctness", "scope-safety", "execution-evidence")])
    if verdict == "FAIL":
        value["rework_instruction"] = "Repair the failing fixed arithmetic criterion and rerun exact-head CI."
    return value


def comment(record, event_id):
    return dict(id=event_id, body=encode_record(record),
                user={"login": "github-actions[bot]" if record["kind"] == "candidate" else "WangYu0611"},
                created_at="2026-10-28T05:00:00Z", updated_at="2026-10-28T05:00:00Z",
                html_url=f"https://github.com/{REPO}/pull/100#issuecomment-{event_id}")


def journal(verdicts=("FAIL", "FAIL", "PASS")):
    result, previous = [], None
    for revision, verdict in enumerate(verdicts):
        cid = 10 + revision * 2
        result.append(comment(candidate(revision, previous), cid))
        result.append(comment(review(revision, cid, verdict), cid + 1))
        previous = cid + 1
    return result


def mutate_event(items, index, change):
    clone = copy.deepcopy(items)
    record = json.loads(clone[index]["body"][len(MARKER):])
    change(record)
    clone[index]["body"] = encode_record(record)
    return clone


def simulated_verifier(record, observed):
    """A contract stub ONLY; never export this as a live verifier."""
    authority = "github" if record["kind"] == "candidate" else "chatgpt_web_scheduled_task"
    return VerifiedEvidence(authority, True)


class ReworkProtocolTests(unittest.TestCase):
    def offline(self, items=None, **kwargs):
        return replay_journal(policy(), journal() if items is None else items, mode="offline", **kwargs)

    def rejects(self, items):
        with self.assertRaises(ProtocolError):
            self.offline(items)

    def test_two_complete_reworks_have_three_distinct_heads(self):
        result = self.offline().report()
        self.assertEqual((result["status"], result["rejections"], result["reworks_completed"]), ("ACCEPTED", 2, 2))
        self.assertEqual(result["candidate_count"], 3)
        self.assertEqual([item["head_sha"] for item in result["events"]], HEADS[:3])
        self.assertEqual(result["two_automatic_reworks"], "NOT_VERIFIED")
        self.assertFalse(result["chatgpt_origin_verified"])

    def test_default_live_requires_external_verifier(self):
        with self.assertRaises(ProtocolError):
            replay_journal(policy(), journal(), observed_head_sha=HEADS[2])

    def test_live_contract_requires_current_head(self):
        with self.assertRaises(ProtocolError):
            replay_journal(policy(), journal(), verifier=simulated_verifier)

    def test_mock_live_verification_contract(self):
        # This assertion tests the verifier interface, NOT real automation.
        result = replay_journal(policy(), journal(), verifier=simulated_verifier,
                                observed_head_sha=HEADS[2]).report()
        self.assertEqual(result["two_automatic_reworks"], "VERIFIED")

    def test_operational_mode_allows_trusted_actor_without_origin_claim(self):
        items = journal()
        for i in (1, 3, 5):
            items = mutate_event(items, i, lambda r: r.pop("provenance"))
        result = replay_journal(policy(), items, mode="operational", observed_head_sha=HEADS[2]).report()
        self.assertEqual(result["status"], "ACCEPTED")
        self.assertEqual(result["two_automatic_reworks"], "NOT_VERIFIED")
        self.assertFalse(result["chatgpt_origin_verified"])

    def test_operational_mode_requires_current_head(self):
        with self.assertRaises(ProtocolError):
            replay_journal(policy(), journal(), mode="operational")

    def test_plain_boolean_verifier_cannot_attest_origin(self):
        with self.assertRaises(ProtocolError):
            replay_journal(policy(), journal(), verifier=lambda *_: True, observed_head_sha=HEADS[2])

    def test_wrong_verification_authority_is_rejected(self):
        with self.assertRaises(ProtocolError):
            replay_journal(policy(), journal(), verifier=lambda *_: VerifiedEvidence("github", True),
                           observed_head_sha=HEADS[2])

    def test_manual_intervention_prevents_automatic_claim(self):
        def verify(record, observed):
            proof = simulated_verifier(record, observed)
            return dataclasses.replace(proof, automatic=False) if observed["id"] == 13 else proof
        result = replay_journal(policy(), journal(), verifier=verify, observed_head_sha=HEADS[2]).report()
        self.assertTrue(result["chatgpt_origin_verified"])
        self.assertEqual(result["two_automatic_reworks"], "NOT_VERIFIED")

    def test_first_failure_dispatches_exactly_one_rework(self):
        state = self.offline(journal()[:2])
        self.assertEqual((state.status, state.revision, state.last_event_id), ("REWORK", 0, 11))

    def test_duplicate_delivery_is_idempotent(self):
        items = journal()
        duplicated = [item for item in items for _ in range(2)]
        self.assertEqual(self.offline(duplicated).report(), self.offline(items).report())

    def test_duplicate_post_does_not_change_canonical_predecessor(self):
        items = journal()
        duplicate = comment(json.loads(items[1]["body"][len(MARKER):]), 999)
        items.insert(2, duplicate)
        self.assertEqual(self.offline(items).last_event_id, 15)

    def test_duplicate_with_conflicting_body_is_not_idempotent(self):
        items = journal()[:2]
        altered = mutate_event(items, 1, lambda r: r.update(rework_instruction="Changed feedback"))[1]
        self.rejects(items + [altered])

    def test_old_review_after_new_head_rejected(self):
        items = journal()[:3]
        stale = review(0, 12, "FAIL")
        self.rejects(items + [comment(stale, 13)])

    def test_gap_and_fork_are_rejected(self):
        for previous in (None, 9, 999):
            with self.subTest(previous=previous):
                self.rejects(mutate_event(journal(), 2, lambda r: r.update(previous_event_id=previous)))

    def test_candidate_cannot_skip_a_rejection(self):
        first = journal()[0]
        self.rejects([first, comment(candidate(1, 10), 12)])

    def test_candidate_cannot_skip_a_revision(self):
        self.rejects(mutate_event(journal(), 2, lambda r: r.update(revision=2)))

    def test_single_workflow_dag_allows_distinct_round_stages(self):
        items = journal()
        for i in (0, 2, 4):
            items = mutate_event(items, i, lambda r: (r["execution"].update(run_id=5000), r["ci"].update(run_id=5000)))
        for i in (1, 3, 5):
            items = mutate_event(items, i, lambda r: r.update(ci_run_id=5000))
        self.assertEqual(self.offline(items).status, "ACCEPTED")
        invalid = mutate_event(items, 2, lambda r: r["execution"].update(stage_key="generate0"))
        self.rejects(invalid)

    def test_review_must_bind_current_ci_stage(self):
        self.rejects(mutate_event(journal(), 3, lambda r: r.update(ci_stage_key="publish0")))

    def test_missing_or_unsafe_stage_is_rejected(self):
        self.rejects(mutate_event(journal(), 0, lambda r: r["execution"].pop("stage_key")))
        self.rejects(mutate_event(journal(), 0, lambda r: r["ci"].update(stage_key="publish; script")))

    def test_job_id_can_be_used_without_stage_key(self):
        items = journal()
        for index in (0, 2, 4):
            def replace_stage(record):
                for obj in (record["execution"], record["ci"]):
                    obj.pop("stage_key")
                    obj["job_id"] = obj["run_id"] + 9000
            items = mutate_event(items, index, replace_stage)
        for index in (1, 3, 5):
            def bind_job(record):
                record.pop("ci_stage_key")
                record["ci_job_id"] = record["ci_run_id"] + 9000
            items = mutate_event(items, index, bind_job)
        self.assertEqual(self.offline(items).status, "ACCEPTED")

    def test_optional_loop_binding_prevents_cross_loop_receipts(self):
        items = journal()
        for index in range(len(items)):
            items = mutate_event(items, index, lambda r: r.update(loop_id="DSH-REWORK-002"))
        self.assertEqual(replay_journal(policy(loop_id="DSH-REWORK-002"), items, mode="offline").status, "ACCEPTED")
        with self.assertRaises(ProtocolError):
            replay_journal(policy(loop_id="other"), items, mode="offline")

    def test_issue_location_requires_explicit_policy(self):
        items = journal()
        for item in items:
            item["html_url"] = item["html_url"].replace("/pull/100#", "/issues/99#")
        self.rejects(items)
        self.assertEqual(replay_journal(policy(journal_location="issue"), items, mode="offline").status, "ACCEPTED")

    def test_candidate_must_descend_from_current_head(self):
        self.rejects(mutate_event(journal(), 2, lambda r: r.update(parent_sha=BASE)))

    def test_stale_ci_head_and_rerun_attempt_are_rejected(self):
        self.rejects(mutate_event(journal(), 2, lambda r: r["ci"].update(tested_head_sha=HEADS[0])))
        self.rejects(mutate_event(journal(), 3, lambda r: r.update(ci_run_attempt=2)))

    def test_pending_or_missing_ci_cannot_be_accepted(self):
        for conclusion in ("cancelled", "skipped", None):
            with self.subTest(conclusion=conclusion):
                self.rejects(mutate_event(journal(), 4, lambda r: r["ci"].update(conclusion=conclusion)))
        self.rejects(mutate_event(journal(), 4, lambda r: r["ci"].update(status="in_progress")))

    def test_actual_ci_failure_prevents_pass(self):
        self.rejects(mutate_event(journal(), 4, lambda r: r["ci"].update(conclusion="failure")))

    def test_changed_source_and_unique_dsh_and_actions_runs_required(self):
        for key, old in (("source_sha256", "1" * 64), ("session_id", "offline-dsh-0")):
            with self.subTest(key=key):
                self.rejects(mutate_event(journal(), 2, lambda r: r["execution"].update({key: old})))

    def test_unknown_execution_outcome_cannot_publish_candidate(self):
        self.rejects(mutate_event(journal(), 0, lambda r: r["execution"].update(completed=False)))
        self.rejects(mutate_event(journal(), 0, lambda r: r["execution"].update(model_requests=2)))

    def test_workflow_and_acceptance_policy_cannot_change(self):
        self.rejects(mutate_event(journal(), 2, lambda r: r["ci"].update(workflow_sha="d" * 40)))
        self.rejects(mutate_event(journal(), 3, lambda r: r.update(criteria_sha256="d" * 64)))

    def test_missing_criterion_cannot_pass(self):
        self.rejects(mutate_event(journal(), 5, lambda r: r["findings"].pop()))

    def test_duplicate_criterion_cannot_pass(self):
        self.rejects(mutate_event(journal(), 5, lambda r: r["findings"][1].update(criterion="arithmetic-correctness")))

    def test_rework_requires_actionable_failure(self):
        self.rejects(mutate_event(journal(), 1, lambda r: r.update(rework_instruction="")))
        self.rejects(mutate_event(journal(), 1, lambda r: r["findings"][0].update(status="BLOCKED")))

    def test_blocked_stops_without_spending_rework(self):
        items = [journal()[0], comment(review(0, 10, "BLOCKED"), 11)]
        result = self.offline(items).report()
        self.assertEqual((result["status"], result["rejections"], result["reworks_completed"]), ("BLOCKED", 0, 0))
        self.rejects(items + [comment(candidate(1, 11), 12)])

    def test_limit_allows_three_reworks_then_blocks(self):
        state = self.offline(journal(("FAIL", "FAIL", "FAIL", "FAIL")))
        self.assertEqual((state.status, state.revision, state.rejections), ("BLOCKED", 3, 4))
        self.assertEqual(state.report()["reworks_completed"], 3)

    def test_accepted_is_terminal(self):
        self.rejects(journal() + [comment(candidate(3, 15), 16)])

    def test_changed_remote_head_invalidates_acceptance(self):
        with self.assertRaises(ProtocolError):
            self.offline(observed_head_sha=HEADS[3])

    def test_untrusted_comment_author_and_wrong_issue_are_rejected(self):
        items = journal()
        for key, value in (("user", {"login": "attacker"}), ("html_url", "https://github.com/other/repo/issues/99#issuecomment-10")):
            changed = copy.deepcopy(items)
            changed[0][key] = value
            self.rejects(changed)

    def test_ordinary_comments_are_ignored(self):
        self.assertEqual(self.offline([{"body": "Ordinary discussion"}] + journal()).revision, 2)

    def test_edited_journal_and_duplicate_json_keys_are_rejected(self):
        changed = journal()
        changed[0]["updated_at"] = "2026-10-28T05:01:00Z"
        self.rejects(changed)
        changed = journal()
        changed[0]["body"] = changed[0]["body"].replace('"revision":0', '"revision":0,"revision":0')
        self.rejects(changed)

    def test_boolean_ids_and_oversized_comment_are_rejected(self):
        self.rejects(mutate_event(journal(), 0, lambda r: r.update(issue=True)))
        self.rejects(mutate_event(journal(), 0, lambda r: r["execution"].update(run_attempt=True)))
        changed = journal()
        changed[0]["body"] += " " * 32768
        self.rejects(changed)

    def test_live_origin_metadata_must_match_and_not_reuse_run(self):
        for index, change in ((1, lambda r: r["provenance"].update(automation_id="different")),
                              (3, lambda r: r["provenance"].update(run_id="offline-web-0"))):
            with self.subTest(index=index), self.assertRaises(ProtocolError):
                replay_journal(policy(), mutate_event(journal(), index, change),
                               verifier=simulated_verifier, observed_head_sha=HEADS[2])

    def test_policy_rejects_excessive_reworks_and_empty_criteria(self):
        for changes in ({"max_reworks": 4}, {"max_reworks": True}, {"required_criteria": ()}):
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                policy(**changes)

    def test_input_is_not_mutated_and_partial_snapshot_is_not_success(self):
        items = journal()
        before = copy.deepcopy(items)
        self.offline(items)
        self.assertEqual(items, before)
        self.assertEqual(self.offline(items[:4]).report()["two_automatic_reworks"], "NOT_VERIFIED")


if __name__ == "__main__":
    unittest.main()
