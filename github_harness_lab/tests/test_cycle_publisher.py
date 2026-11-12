"""Offline fake-GitHub integration tests; arithmetic subprocesses are real.

These tests never call a model or network and do not prove a hosted task ran.
"""
import base64
import copy
import hashlib
from functools import lru_cache
import json
import unittest

from lab_harness.cycle_policy import inject_fault, test_source
from lab_harness.github import GitHubError, UncertainWrite
from lab_harness.rework_protocol import MARKER, SCHEMA, encode_record
from lab_harness.smoke_policy import validate_source
from live_tools.publish_cycle import (CRITERIA, LOOP_ID, OWNER, PUBLISHER, REPOSITORY,
                                     STATUS_CONTEXT, branch_name, digest, independent_tests,
                                     inspect_review, publish, validate_envelope, wait_review)

BASE = "a" * 40
WORKFLOW = "b" * 40
BODY = "Disclosed offline test fixture; not a live Issue or task."
MODEL = validate_source("def add(left,right):\n return left+right\ndef multiply(left,right):\n return left*right\n")


@lru_cache(maxsize=4)
def tested(source):
    return test_source(source)


def fixture(revision=0, parent=BASE):
    source, fault = inject_fault(MODEL, revision)
    return {"schema": "bounded-dsh-cycle/v1", "repository": REPOSITORY, "issue": 5,
            "loop_id": LOOP_ID, "revision": revision, "base_sha": BASE, "parent_sha": parent,
            "workflow_sha": WORKFLOW, "issue_body_sha256": digest(BODY), "source_path": "src/arithmetic.py",
            "source_code": source, "source_sha256": digest(source), "model_source_code": MODEL,
            "model_source_sha256": digest(MODEL), "fault_injection": fault,
            "session_id": f"offline-session-{revision}", "model": "deepseek-flash", "version": "0.2.1-rc.3",
            "model_request_count": 1, "harness_status": "completed", "harness_exit_code": 0,
            "run_id": "100", "run_attempt": "1",
            "execution_stage": f"generate-{revision}", "ci_stage": f"publish-{revision}",
            "model_tests": copy.deepcopy(tested(MODEL)),
            "tests": copy.deepcopy(tested(source))}


def environment(data):
    return {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_ACTOR": OWNER, "GITHUB_EVENT_NAME": "issues", "GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_WORKFLOW_SHA": WORKFLOW, "GITHUB_JOB": f"publish-{data['revision']}",
            "CYCLE_ISSUE": "5", "CYCLE_LOOP_ID": LOOP_ID, "CYCLE_REVISION": str(data["revision"]),
            "CYCLE_BASE_SHA": BASE, "CYCLE_PARENT_SHA": data["parent_sha"], "CYCLE_ISSUE_BODY_SHA256": digest(BODY),
            "CYCLE_WORKFLOW_SHA": WORKFLOW, "CYCLE_AUTOMATION_ID": "offline-contract-task",
            "CYCLE_ARTIFACT_ID": str(200 + data["revision"])}


def artifact_sha(data):
    return digest(json.dumps(data, sort_keys=True))


class FakeGitHub:
    def __init__(self):
        self.issue = {"number": 5, "state": "open", "body": BODY, "user": {"login": OWNER}, "labels": [{"name": "agent:cycle-ready"}]}
        self.refs = {"main": BASE}
        self.commits = {BASE: {"sha": BASE, "tree": {"sha": "c" * 40}, "parents": [], "message": "base", "source": "initial"}}
        self.blobs = {}
        self.trees = {}
        self.prs = []
        self.comments = []
        self.statuses = {}
        self.writes = []
        self.unknown = None
        self.race_on_commit = False
        self.changed_exact_source = False

    def _write(self, kind):
        self.writes.append(kind)
        if self.unknown == kind:
            self.unknown = None
            raise UncertainWrite("offline uncertain result")

    def get_issue(self, number):
        assert number == 5
        return copy.deepcopy(self.issue)

    def get_ref(self, branch):
        if branch not in self.refs:
            raise GitHubError("not found", status=404)
        return {"object": {"sha": self.refs[branch]}}

    def request(self, method, path, **kwargs):
        if method == "GET":
            if path.startswith("/git/commits/"):
                return copy.deepcopy(self.commits[path.split("/")[-1]])
            if path.startswith("/compare/"):
                return {"total_commits": 1, "files": [{"filename": "src/arithmetic.py", "status": "modified"}]}
            if path == "/contents/src/arithmetic.py":
                source = self.commits[kwargs["params"]["ref"]]["source"]
                if self.changed_exact_source:
                    source = MODEL
                return {"type": "file", "encoding": "base64", "content": base64.b64encode(source.encode()).decode()}
        payload = kwargs.get("payload", {})
        if method == "POST" and path == "/git/refs":
            assert payload["ref"] == "refs/heads/" + branch_name(5)
            assert branch_name(5) not in self.refs
            self.refs[branch_name(5)] = payload["sha"]
            self._write("ref")
            return {"object": {"sha": payload["sha"]}}
        if method == "PATCH" and path == "/git/refs/heads/" + branch_name(5):
            assert payload["force"] is False
            new = self.commits[payload["sha"]]
            assert new["parents"] == [{"sha": self.refs[branch_name(5)]}]
            self.refs[branch_name(5)] = payload["sha"]
            self._write("update-ref")
            return {"object": {"sha": payload["sha"]}}
        if method == "POST" and path.startswith("/statuses/"):
            head = path.split("/")[-1]
            status = payload | {"id": 300 + len(self.statuses), "creator": {"login": PUBLISHER}}
            self.statuses.setdefault(head, []).insert(0, status)
            self._write("status")
            return copy.deepcopy(status)
        raise AssertionError((method, path, kwargs))

    def create_blob(self, source, encoding):
        assert encoding == "utf-8"
        sha = hashlib.sha1(source.encode()).hexdigest()
        self.blobs[sha] = source
        self._write("blob")
        return {"sha": sha}

    def create_tree(self, parent_tree, entries):
        assert len(entries) == 1 and entries[0]["path"] == "src/arithmetic.py" and entries[0]["mode"] == "100644"
        sha = hashlib.sha1((parent_tree + json.dumps(entries)).encode()).hexdigest()
        self.trees[sha] = self.blobs[entries[0]["sha"]]
        self._write("tree")
        return {"sha": sha}

    def create_commit(self, message, tree, parents):
        sha = hashlib.sha1((message + tree + str(parents)).encode()).hexdigest()
        self.commits[sha] = {"sha": sha, "tree": {"sha": tree}, "message": message,
                             "parents": [{"sha": p} for p in parents], "source": self.trees[tree]}
        self._write("commit")
        if self.race_on_commit:
            self.refs[branch_name(5)] = "f" * 40
        return {"sha": sha}

    def _list(self, path, params=None):
        if path == "/pulls":
            assert params["state"] == "all" and params["head"] == OWNER + ":" + branch_name(5)
            return [self.get_pr(p["number"]) for p in self.prs]
        if path.startswith("/commits/") and path.endswith("/statuses"):
            return copy.deepcopy(self.statuses.get(path.split("/")[2], []))
        raise AssertionError(path)

    def create_pr(self, title, body, branch, base, draft):
        assert branch == branch_name(5) and base == "main" and draft is True
        pr = {"number": 6, "html_url": f"https://github.com/{REPOSITORY}/pull/6", "body": body, "state": "open",
              "base": {"ref": "main"}, "head": {"ref": branch, "repo": {"full_name": REPOSITORY}}}
        self.prs.append(pr)
        self._write("pr")
        return self.get_pr(6)

    def get_pr(self, number):
        pr = copy.deepcopy(next(p for p in self.prs if p["number"] == number))
        pr["head"]["sha"] = self.refs[branch_name(5)]
        return pr

    def list_comments(self, number):
        assert number == 6
        return copy.deepcopy(self.comments)

    def comment(self, number, body, *, login=PUBLISHER):
        cid = 400 + len(self.comments)
        value = {"id": cid, "body": body, "user": {"login": login},
                 "created_at": "2026-10-28T10:00:00Z", "updated_at": "2026-10-28T10:00:00Z",
                 "html_url": f"https://github.com/{REPOSITORY}/pull/{number}#issuecomment-{cid}"}
        self.comments.append(value)
        self._write("comment")
        return copy.deepcopy(value)


def add_review(client, data, publication, verdict="FAIL", **changes):
    value = {"schema": SCHEMA, "kind": "review", "repository": REPOSITORY, "issue": 5, "pr": 6,
             "base_sha": BASE, "criteria_sha256": digest(BODY), "head_sha": publication["head_sha"],
             "revision": data["revision"], "previous_event_id": publication["journal_id"], "loop_id": LOOP_ID,
             "ci_run_id": 100, "ci_run_attempt": 1, "ci_stage_key": data["ci_stage"], "verdict": verdict,
             "findings": [{"criterion": c, "status": verdict if c == CRITERIA[0] else "PASS",
                           "detail": "Offline contract fixture, not a real hosted review.",
                           "evidence_url": f"https://github.com/{REPOSITORY}/actions/runs/100"} for c in CRITERIA]}
    if verdict == "FAIL":
        value["rework_instruction"] = "Repair multiply to return the arithmetic product."
    value.update(changes)
    return client.comment(6, encode_record(value), login=OWNER)


class CyclePublisherTests(unittest.TestCase):
    def test_wait_job_is_read_only_and_cannot_publish(self):
        data = fixture()
        env = environment(data) | {"GITHUB_JOB": "wait-0"}
        validate_envelope(data, env, read_only=True)
        with self.assertRaises(ValueError):
            validate_envelope(data, env)

    def setUp(self):
        self.client = FakeGitHub()
        self.data = fixture()
        self.env = environment(self.data)

    def publish(self, data=None, env=None):
        data = data or self.data
        return publish(self.client, data, env or environment(data), artifact_sha256=artifact_sha(data))

    def test_envelope_requires_every_trusted_binding(self):
        validate_envelope(self.data, self.env)
        for key in self.env:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_envelope(self.data, self.env | {key: "" if key == "CYCLE_AUTOMATION_ID" else "wrong"})

    def test_missing_context_rejected_before_any_write(self):
        broken = dict(self.env)
        del broken["CYCLE_AUTOMATION_ID"]
        with self.assertRaises(ValueError): self.publish(env=broken)
        self.assertEqual(self.client.writes, [])

    def test_real_assertions_fail_for_two_injections_and_pass_final(self):
        results = [independent_tests(fixture(i)["source_code"]) for i in range(3)]
        self.assertEqual([r["status"] for r in results], ["failure", "failure", "success"])
        self.assertTrue(all(r["count"] == 10 for r in results))
        self.assertTrue(results[0]["failed_cases"])
        self.assertEqual(results[2]["failed_cases"], [])

    def test_safe_business_failure_is_published_with_failure_status(self):
        result = self.publish()
        self.assertEqual(result["tests"]["exit_code"], 1)
        self.assertEqual(self.client.statuses[result["head_sha"]][0]["state"], "failure")
        self.assertEqual(result["journal"]["status"], "REVIEW")
        self.assertFalse(result["chatgpt_origin_verified"])
        self.assertEqual(self.client.refs["main"], BASE)

    def test_undisclosed_injection_or_false_model_receipt_rejected(self):
        for changes in ({"fault_injection": {"kind": "none", "disclosed": False}}, {"model_tests": {"exit_code": 1}}, {"source_sha256": "0" * 64}):
            with self.assertRaises(ValueError): self.publish(self.data | changes)
        self.assertEqual(self.client.writes, [])

    def test_changed_issue_or_main_stops_before_writes(self):
        for kind in ("issue", "main"):
            self.client = FakeGitHub()
            if kind == "issue": self.client.issue["body"] = "changed"
            else: self.client.refs["main"] = "e" * 40
            with self.assertRaises(ValueError): self.publish()
            self.assertEqual(self.client.writes, [])

    def test_repeated_publish_is_idempotent(self):
        first = self.publish()
        writes = list(self.client.writes)
        self.assertEqual(first, self.publish())
        self.assertEqual(writes, self.client.writes)

    def test_unknown_ref_pr_status_and_comment_outcomes_reconcile(self):
        for operation in ("ref", "pr", "status", "comment"):
            with self.subTest(operation=operation):
                self.client = FakeGitHub()
                self.client.unknown = operation
                with self.assertRaises(UncertainWrite): self.publish()
                self.assertEqual(self.client.writes.count(operation), 1)
                self.publish()
                self.assertEqual(self.client.writes.count(operation), 1)
                self.assertEqual(len(self.client.prs), 1)
                self.assertEqual(len(self.client.comments), 1)

    def test_conflicting_existing_branch_is_never_overwritten(self):
        self.client.refs[branch_name(5)] = BASE
        with self.assertRaises(ValueError): self.publish()
        self.assertEqual(self.client.writes, [])

    def test_branch_race_blocks_ref_write(self):
        self.client.race_on_commit = True
        with self.assertRaises(ValueError): self.publish()
        self.assertNotIn("ref", self.client.writes)
        self.assertNotIn("update-ref", self.client.writes)

    def test_exact_head_is_downloaded_and_mismatch_blocks(self):
        self.client.changed_exact_source = True
        with self.assertRaises(ValueError): self.publish()
        self.assertNotIn("pr", self.client.writes)
        self.assertNotIn("status", self.client.writes)

    def test_closed_pr_is_not_reopened(self):
        self.publish()
        self.client.prs[0]["state"] = "closed"
        before = list(self.client.writes)
        with self.assertRaises(ValueError): self.publish()
        self.assertEqual(before, self.client.writes)

    def test_no_rework_before_actual_failed_review(self):
        p0 = self.publish()
        before = list(self.client.writes)
        with self.assertRaises(ValueError): self.publish(fixture(1, p0["head_sha"]))
        self.assertEqual(before, self.client.writes)

    def test_three_revisions_reuse_one_pr_and_form_two_reworks(self):
        data = self.data
        heads = []
        for revision, verdict in enumerate(("FAIL", "FAIL", "PASS")):
            if revision: data = fixture(revision, heads[-1])
            publication = self.publish(data)
            heads.append(publication["head_sha"])
            add_review(self.client, data, publication, verdict)
            result = inspect_review(self.client, data, environment(data), publication)
            self.assertEqual(result["needs_rework"], revision < 2)
        self.assertEqual(len(set(heads)), 3)
        self.assertEqual(len(self.client.prs), 1)
        self.assertEqual(self.client.writes.count("update-ref"), 2)
        self.assertEqual(result["status"], "ACCEPTED")
        self.assertEqual(result["journal"]["reworks_completed"], 2)
        self.assertEqual(result["journal"]["two_automatic_reworks"], "NOT_VERIFIED")

    def test_unknown_rework_ref_then_journal_reconciles_same_candidate(self):
        p0 = self.publish()
        add_review(self.client, self.data, p0)
        data = fixture(1, p0["head_sha"])
        self.client.unknown = "update-ref"
        with self.assertRaises(UncertainWrite): self.publish(data)
        p1 = self.publish(data)
        before = list(self.client.writes)
        self.assertEqual(p1, self.publish(data))
        self.assertEqual(before, self.client.writes)
        self.assertEqual(self.client.writes.count("update-ref"), 1)

    def test_wrong_review_head_run_stage_and_previous_are_rejected(self):
        for changes in ({"head_sha": "e" * 40}, {"ci_run_id": 101}, {"ci_stage_key": "publish-1"}, {"previous_event_id": 999}):
            with self.subTest(changes=changes):
                self.client = FakeGitHub()
                publication = self.publish()
                add_review(self.client, self.data, publication, **changes)
                with self.assertRaises(ValueError): inspect_review(self.client, self.data, self.env, publication)

    def test_forged_author_or_edited_review_rejected(self):
        for kind in ("author", "edited"):
            self.client = FakeGitHub()
            publication = self.publish()
            add_review(self.client, self.data, publication)
            if kind == "author": self.client.comments[-1]["user"]["login"] = "untrusted"
            else: self.client.comments[-1]["updated_at"] = "later"
            with self.assertRaises(ValueError): inspect_review(self.client, self.data, self.env, publication)

    def test_pass_cannot_override_failed_exact_head_ci(self):
        publication = self.publish()
        add_review(self.client, self.data, publication, "PASS")
        with self.assertRaises(ValueError): inspect_review(self.client, self.data, self.env, publication)

    def test_superseded_status_invalidates_review(self):
        publication = self.publish()
        add_review(self.client, self.data, publication)
        self.client.statuses[publication["head_sha"]][0]["id"] = 999
        with self.assertRaises(ValueError): inspect_review(self.client, self.data, self.env, publication)

    def test_wait_timeout_does_not_publish_or_repair_a_review(self):
        publication = self.publish()
        before = list(self.client.writes)
        instant = [0.0]
        def sleep(seconds): instant[0] += seconds
        with self.assertRaises(TimeoutError):
            wait_review(self.client, self.data, self.env, publication, timeout_seconds=30,
                        clock=lambda: instant[0], sleep=sleep)
        self.assertEqual(before, self.client.writes)

    def test_wait_returns_real_eligible_comment_without_origin_claim(self):
        publication = self.publish()
        add_review(self.client, self.data, publication)
        result = wait_review(self.client, self.data, self.env, publication)
        self.assertTrue(result["needs_rework"])
        self.assertFalse(result["chatgpt_origin_verified"])


if __name__ == "__main__":
    unittest.main()
