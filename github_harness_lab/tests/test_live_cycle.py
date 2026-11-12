"""Offline control-plane fixtures plus real arithmetic tests, never a model call."""
import base64
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from lab_harness.cycle_policy import ISSUE, LOOP_ID, inject_fault
from lab_harness.live_cycle import check_context, check_remote, current_candidate, execute
from lab_harness.rework_protocol import SCHEMA, encode_record, ProtocolError
from lab_harness.smoke_policy import ACTOR, REPOSITORY, digest, validate_source


BASE = "a" * 40
HEADS = [str(i) * 40 for i in (1, 2, 3)]
ORIGINAL = "def add(left, right):\n    return left + right\n"
GOOD = validate_source(ORIGINAL + "\ndef multiply(left, right):\n    return left * right\n")
BODY = "OFFLINE FIXTURE: two reworks; no real authorizations or requests."
PR = 6


def fixture():
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    config = dict(repository=REPOSITORY, issue_number=ISSUE, loop_id=LOOP_ID, max_model_requests=3,
                  authorization="user-authorized-two-rework-drill", expires_at=expiry,
                  workflow_sha=BASE, base_sha=BASE, issue_body_sha256=digest(BODY),
                  original_source_sha256=digest(ORIGINAL), arm_comment_id=901, arm_nonce="offline-nonce",
                  reviewer_automation_id="OFFLINE-TASK", dsh={"fixture_only": True})
    arm = dict(schema="dsh-cycle-arm/v1", loop_id=LOOP_ID, issue=ISSUE, repository=REPOSITORY,
               authorized=True, max_model_requests=3, nonce=config["arm_nonce"], workflow_sha=BASE,
               issue_body_sha256=digest(BODY), runner_label="dsh-cycle-20261028", expires_at=expiry)
    arm_body = "<!-- dsh-cycle-arm:v1 -->\n" + json.dumps(arm)
    config["arm_body_sha256"] = digest(arm_body)
    manager = dict(schema="dsh-cycle-manager-start/v1", loop_id=LOOP_ID, issue=ISSUE,
                   arm_comment_id=901, arm_nonce=config["arm_nonce"], workflow_sha=BASE, trigger="schedule")
    comments = [make_comment(arm_body, 901, ISSUE),
                make_comment("<!-- dsh-cycle-manager-start:v1 -->\n" + json.dumps(manager), 902, ISSUE)]
    issue = dict(number=ISSUE, state="open", user={"login": ACTOR}, body=BODY,
                 labels=[{"name": "agent:cycle-ready"}])
    event = dict(repository={"full_name": REPOSITORY, "private": True}, sender={"login": ACTOR},
                 action="labeled", label={"name": "agent:cycle-ready"}, issue=copy.deepcopy(issue))
    env = dict(GITHUB_ACTIONS="true", GITHUB_REPOSITORY=REPOSITORY, GITHUB_EVENT_NAME="issues",
               GITHUB_ACTOR=ACTOR, GITHUB_WORKFLOW_SHA=BASE, GITHUB_SHA=BASE,
               GITHUB_RUN_ID="900000", GITHUB_RUN_ATTEMPT="1")
    return config, event, env, FakeClient(issue, comments)


def make_comment(body, cid, issue_or_pr=PR, actor=ACTOR):
    return dict(id=cid, body=body, user={"login": actor}, created_at="2026-10-28T00:00:00Z",
                updated_at="2026-10-28T00:00:00Z",
                html_url=f"https://github.com/{REPOSITORY}/{'issues' if issue_or_pr == ISSUE else 'pull'}/{issue_or_pr}#issuecomment-{cid}")


def rework_comments(revision=0, verdict="FAIL"):
    records = []
    previous = None
    for index in range(revision + 1):
        common = dict(schema=SCHEMA, loop_id=LOOP_ID, repository=REPOSITORY, issue=ISSUE, pr=PR,
                      base_sha=BASE, criteria_sha256=digest(BODY), revision=index, head_sha=HEADS[index])
        candidate = dict(common, kind="candidate", parent_sha=BASE if index == 0 else HEADS[index - 1],
                         previous_event_id=previous,
                         execution=dict(run_id=900000, run_attempt=1, workflow_sha=BASE,
                                        stage_key=f"generate-{index}", session_id=f"OFFLINE-{index}",
                                        source_sha256=digest(inject_fault(GOOD, index)[0]),
                                        artifact_sha256=str(index + 1) * 64, completed=True, model_requests=1),
                         ci=dict(run_id=900000, run_attempt=1, workflow_sha=BASE, stage_key=f"publish-{index}",
                                 tested_head_sha=HEADS[index], status="completed", conclusion="failure"))
        cid = 1001 + index * 2
        records.append(make_comment(encode_record(candidate), cid, actor="github-actions[bot]"))
        review = dict(common, kind="review", previous_event_id=cid, verdict=verdict,
                      ci_run_id=900000, ci_run_attempt=1, ci_stage_key=f"publish-{index}",
                      rework_instruction="Correct multiply; keep scope and verify the failing assertion.",
                      findings=[dict(criterion=criterion, status=verdict if criterion == "arithmetic-correctness" else "PASS",
                                     detail="Offline fixture only.", evidence_url=f"https://github.com/{REPOSITORY}/actions/runs/900000")
                                for criterion in ("arithmetic-correctness", "scope-safety", "execution-evidence")])
        records.append(make_comment(encode_record(review), cid + 1))
        previous = cid + 1
    return records


class FakeClient:
    def __init__(self, issue, comments):
        self.issue, self.comments = issue, comments
        self.base = BASE
        self.prs = []
        self.pr_comments = []
        self.sources = {BASE: ORIGINAL}

    def get_issue(self, number):
        assert number == ISSUE
        return copy.deepcopy(self.issue)

    def get_ref(self, branch):
        if branch != "main":
            from lab_harness.github import GitHubError
            raise GitHubError("Not found", status=404)
        return {"object": {"sha": self.base}}

    def list_comments(self, number):
        return copy.deepcopy(self.comments if number == ISSUE else self.pr_comments)

    def _list(self, path, params):
        assert path == "/pulls"
        assert params == {"state": "all", "head": ACTOR + f":agent/rework-002-issue-{ISSUE}"}
        return copy.deepcopy(self.prs)

    def request(self, method, path, *, params):
        assert (method, path) == ("GET", "/contents/src/arithmetic.py")
        return {"encoding": "base64", "type": "file",
                "content": base64.b64encode(self.sources[params["ref"]].encode()).decode()}

    def use_revision(self, revision):
        self.prs = [dict(number=PR, state="open", base={"sha": BASE, "ref": "main", "repo": {"full_name": REPOSITORY}},
                         head={"sha": HEADS[revision], "ref": f"agent/rework-002-issue-{ISSUE}", "repo": {"full_name": REPOSITORY}})]
        self.pr_comments = rework_comments(revision)
        self.sources[HEADS[revision]] = inject_fault(GOOD, revision)[0]


class FakeGenerate:
    def __init__(self, source=GOOD, after=None, error=False):
        self.calls = 0
        self.source, self.after, self.error = source, after, error

    def __call__(self, prompt, workspace, out, config):
        self.calls += 1
        assert config == {"fixture_only": True}
        assert workspace.is_dir()
        assert (workspace.parent / "claim.json").is_file(), "Claim must exist BEFORE billable request"
        assert len(prompt.encode()) <= 4096
        if self.error:
            raise RuntimeError("Offline uncertain model outcome")
        if self.after:
            self.after()
        return dict(final_text=self.source, session_id="OFFLINE-SESSION-NEW", model="OFFLINE-MODEL",
                    version="fixture", request_count=1, exit_code=0, usage={"input_tokens": 0, "output_tokens": 0})


class LiveCycleTests(unittest.TestCase):
    def test_review_from_different_workflow_cannot_rebill(self):
        config, event, env, client = fixture()
        client.use_revision(0)
        generate = FakeGenerate()
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(ValueError):
            execute(config, event, env | {"GITHUB_RUN_ID": "900001"}, revision=1,
                    root=Path(folder), client=client, generate=generate)
        self.assertEqual(generate.calls, 0)

    def test_existing_initial_pr_cannot_rebill_after_cache_loss(self):
        config, event, env, client = fixture()
        client.use_revision(0)
        generate = FakeGenerate()
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(ValueError):
            execute(config, event, env, revision=0, root=Path(folder), client=client, generate=generate)
        self.assertEqual(generate.calls, 0)

    def test_context_is_fixed_and_rejects_changed_workflow_actor_or_event(self):
        config, event, env, _ = fixture()
        check_context(config, event, env, 0)
        for key, value in (("GITHUB_REPOSITORY", "other/repo"), ("GITHUB_EVENT_NAME", "pull_request"),
                           ("GITHUB_ACTOR", "other"), ("GITHUB_WORKFLOW_SHA", "b" * 40),
                           ("GITHUB_SHA", "b" * 40), ("GITHUB_RUN_ATTEMPT", "2")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                check_context(config, event, env | {key: value}, 0)
        for rev in (-1, 3, True):
            with self.subTest(revision=rev), self.assertRaises(ValueError):
                check_context(config, event, env, rev)

    def test_expired_configuration_blocks_before_model(self):
        config, event, env, client = fixture()
        config["expires_at"] = "2000-01-01T00:00:00Z"
        generate = FakeGenerate()
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(ValueError):
            execute(config, event, env, revision=0, root=Path(folder), client=client, generate=generate)
        self.assertEqual(generate.calls, 0)

    def test_valid_remote_arm_and_single_manager_receipt(self):
        config, _, _, client = fixture()
        self.assertEqual(check_remote(config, client), 902)

    def test_arm_content_hash_author_and_immutable_timestamp_checked(self):
        for change in (lambda c: c.update(body=c["body"] + " "),
                       lambda c: c.update(user={"login": "other"}),
                       lambda c: c.update(updated_at="2026-10-28T00:01:00Z")):
            config, _, _, client = fixture()
            change(client.comments[0])
            with self.assertRaises(ValueError):
                check_remote(config, client)

    def test_expired_arm_even_with_updated_hash_is_rejected(self):
        config, _, _, client = fixture()
        arm = client.comments[0]
        marker, text = arm["body"].split("\n", 1)
        value = json.loads(text)
        value["expires_at"] = "2000-01-01T00:00:00Z"
        arm["body"] = marker + "\n" + json.dumps(value)
        config["arm_body_sha256"] = digest(arm["body"])
        with self.assertRaises(ValueError):
            check_remote(config, client)

    def test_missing_or_duplicate_manager_receipt_blocks(self):
        for duplicate in (False, True):
            config, _, _, client = fixture()
            if duplicate:
                duplicate_comment = copy.deepcopy(client.comments[1])
                duplicate_comment["id"] = 903
                client.comments.append(duplicate_comment)
            else:
                client.comments.pop()
            with self.subTest(duplicate=duplicate), self.assertRaises(ValueError):
                check_remote(config, client)

    def test_disarm_issue_edit_or_main_change_blocks(self):
        for change in (lambda c: c.issue.update(labels=[]), lambda c: c.issue.update(body=BODY + " edited"),
                       lambda c: c.issue.update(state="closed"), lambda c: setattr(c, "base", "b" * 40)):
            config, _, _, client = fixture()
            change(client)
            with self.assertRaises(ValueError):
                check_remote(config, client)

    def test_initial_execute_claim_precedes_one_fake_request_and_real_failure(self):
        config, event, env, client = fixture()
        generate = FakeGenerate()
        with tempfile.TemporaryDirectory() as folder:
            result = execute(config, event, env, revision=0, root=Path(folder), client=client, generate=generate)
            self.assertEqual((result["tests"]["exit_code"], result["model_tests"]["exit_code"]), (1, 0))
            self.assertTrue(result["fault_injection"]["disclosed"])
            self.assertEqual(result["manager_comment_id"], 902)
            self.assertTrue((Path(folder) / ".runtime/cycle-output" / LOOP_ID / "0/result.json").is_file())
            with self.assertRaises(FileExistsError):
                execute(config, event, env, revision=0, root=Path(folder), client=client, generate=generate)
            self.assertEqual(generate.calls, 1)

    def test_uncertain_model_outcome_cannot_rebill(self):
        config, event, env, client = fixture()
        generate = FakeGenerate(error=True)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(RuntimeError):
                execute(config, event, env, revision=0, root=Path(folder), client=client, generate=generate)
            with self.assertRaises(FileExistsError):
                execute(config, event, env, revision=0, root=Path(folder), client=client, generate=generate)
        self.assertEqual(generate.calls, 1)

    def test_incorrect_model_output_stops_before_fault_injection(self):
        config, event, env, client = fixture()
        generate = FakeGenerate(source=GOOD.replace("left * right", "left + right"))
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                execute(config, event, env, revision=0, root=Path(folder), client=client, generate=generate)
            stage = Path(folder) / ".runtime/cycle-output" / LOOP_ID / "0"
            self.assertTrue((stage / "model-failure.json").is_file())
            self.assertFalse((stage / "result.json").exists())
        self.assertEqual(generate.calls, 1)

    def test_issue_change_during_generation_prevents_publication_artifact(self):
        config, event, env, client = fixture()
        generate = FakeGenerate(after=lambda: client.issue.update(body=BODY + " changed"))
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                execute(config, event, env, revision=0, root=Path(folder), client=client, generate=generate)
            self.assertFalse((Path(folder) / ".runtime/cycle-output" / LOOP_ID / "0/result.json").exists())

    def test_current_rework_requires_exact_rejected_head(self):
        config, _, _, client = fixture()
        client.use_revision(0)
        head, review, number = current_candidate(config, client, 1)
        self.assertEqual((head, review["event_id"], number), (HEADS[0], 1002, PR))
        client.prs[0]["head"]["sha"] = HEADS[1]
        with self.assertRaises(ProtocolError):
            current_candidate(config, client, 1)

    def test_missing_review_or_blocked_review_cannot_dispatch(self):
        for blocked in (False, True):
            config, _, _, client = fixture()
            client.use_revision(0)
            client.pr_comments = rework_comments(0, "BLOCKED") if blocked else client.pr_comments[:1]
            with self.subTest(blocked=blocked), self.assertRaises(ValueError):
                current_candidate(config, client, 1)

    def test_two_rework_generation_stages_use_feedback_and_pass_final(self):
        config, event, env, client = fixture()
        with tempfile.TemporaryDirectory() as folder:
            results = []
            for revision in (1, 2):
                client.use_revision(revision - 1)
                generate = FakeGenerate()
                result = execute(config, event, env, revision=revision, root=Path(folder), client=client, generate=generate)
                self.assertEqual(generate.calls, 1)
                self.assertEqual(result["parent_sha"], HEADS[revision - 1])
                self.assertEqual(result["review_comment_id"], 1002 + (revision - 1) * 2)
                results.append(result["tests"]["exit_code"])
            self.assertEqual(results, [1, 0])

    def test_head_change_after_rework_generation_invalidates_artifact(self):
        config, event, env, client = fixture()
        client.use_revision(0)
        generate = FakeGenerate(after=lambda: client.prs[0]["head"].update(sha=HEADS[1]))
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(ProtocolError):
            execute(config, event, env, revision=1, root=Path(folder), client=client, generate=generate)


if __name__ == "__main__":
    unittest.main()
