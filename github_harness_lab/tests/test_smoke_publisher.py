"""No network or model requests: exercise publisher reconciliation and bounds."""
import base64
import copy
import unittest

from lab_harness.github import GitHubError, UncertainWrite
from lab_harness.smoke_policy import SmokeError, validate_source
from live_tools.publish_smoke import REPOSITORY, check_current, digest, independent_tests, publish, validate_envelope


def fixture():
    source = validate_source("def add(left, right):\n    return left + right\n\ndef multiply(left, right):\n    return left * right\n")
    return {"schema": "bounded-dsh-smoke/v1", "repository": REPOSITORY,
            "issue": 3, "issue_body_sha256": digest("fixed task"), "base_sha": "a" * 40,
            "source_path": "src/arithmetic.py", "source_code": source, "source_sha256": digest(source),
            "session_id": "test-session", "model": "deepseek-flash", "version": "0.2.1-rc.3",
            "model_request_count": 1, "harness_status": "completed",
            "tests": {"exit_code": 0, "count": 7, "timed_out": False, "output_limit_exceeded": False},
            "run_id": "100", "run_attempt": "1", "runner_name": "test-runner",
            "started_at": "2026-10-28T00:00:00Z", "finished_at": "2026-10-28T00:00:02Z"}


class FakeClient:
    def __init__(self, data):
        self.data = data
        self.issue = {"number": 3, "state": "open", "body": "fixed task", "user": {"login": "WangYu0611"}, "labels": [{"name": "agent:ready"}]}
        self.base = data["base_sha"]
        self.branch = None
        self.message = None
        self.prs = []
        self.comments = []
        self.writes = []
        self.pr_uncertain = False

    def get_issue(self, number): return self.issue
    def get_ref(self, branch):
        if branch == "main": return {"object": {"sha": self.base}}
        if self.branch is None: raise GitHubError("Not found", status=404)
        return {"object": {"sha": self.branch}}
    def request(self, method, path, **kwargs):
        assert method == "GET"
        if path == "/git/commits/" + self.base: return {"tree": {"sha": "c" * 40}}
        if path.startswith("/git/commits/"): return {"message": self.message, "parents": [{"sha": self.base}]}
        if path.startswith("/compare/"): return {"total_commits": 1, "files": [{"filename": "src/arithmetic.py", "status": "modified"}]}
        if path == "/contents/src/arithmetic.py": return {"type": "file", "encoding": "base64", "content": base64.b64encode(self.data["source_code"].encode()).decode()}
        raise AssertionError(path)
    def create_blob(self, content, encoding):
        self.writes.append("blob")
        assert content == self.data["source_code"] and encoding == "utf-8"
        return {"sha": "d" * 40}
    def create_tree(self, base_tree, entries):
        self.writes.append("tree")
        assert entries == [{"path": "src/arithmetic.py", "mode": "100644", "type": "blob", "sha": "d" * 40}]
        return {"sha": "e" * 40}
    def create_commit(self, message, tree, parents):
        self.writes.append("commit")
        self.message = message
        assert parents == [self.base]
        return {"sha": "b" * 40}
    def create_ref(self, branch, head):
        self.writes.append("ref:" + branch)
        assert branch == "agent/issue-3"
        self.branch = head
    def _list(self, path, params):
        assert path == "/pulls" and params["state"] == "all"
        return self.prs
    def create_pr(self, title, body, branch, base, draft):
        self.writes.append("pr")
        pr = {"number": 4, "html_url": "https://github.com/" + REPOSITORY + "/pull/4", "body": body, "state": "open",
              "head": {"sha": self.branch, "repo": {"full_name": REPOSITORY}}, "base": {"ref": base}}
        self.prs.append(pr)
        if self.pr_uncertain:
            self.pr_uncertain = False
            raise UncertainWrite("unknown")
        return pr
    def list_comments(self, number): return self.comments
    def comment(self, number, body):
        self.writes.append("comment")
        self.comments.append({"body": body})


class SmokePublisherTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture()
        self.env = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1", "SMOKE_ISSUE": "3",
                    "GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "issues", "GITHUB_ACTOR": "WangYu0611"}
        self.cloud_tests = {"count": 10, "exit_code": 0}

    def test_artifact_is_bound_to_exact_run_attempt_issue_and_repo(self):
        validate_envelope(self.data, self.env)
        for key in self.env:
            wrong = self.env | {key: "wrong"}
            with self.assertRaises(ValueError): validate_envelope(self.data, wrong)

    def test_source_digest_and_machine_completion_required(self):
        for key, wrong in (("source_sha256", "0" * 64), ("model_request_count", 2), ("harness_status", "failed")):
            with self.assertRaises((SmokeError, ValueError)):
                validate_envelope(self.data | {key: wrong}, self.env)

    def test_independent_tests_execute_real_candidate(self):
        self.assertEqual(independent_tests(self.data["source_code"])["count"], 10)
        wrong = self.data["source_code"].replace("left * right", "left + right")
        with self.assertRaises(ValueError): independent_tests(wrong)

    def test_changed_issue_or_main_stops_before_writes(self):
        for kind in ("issue", "main"):
            client = FakeClient(self.data)
            if kind == "issue": client.issue["body"] = "changed"
            else: client.base = "f" * 40
            with self.assertRaises(ValueError): publish(client, self.data, cloud_tests=self.cloud_tests)
            self.assertEqual(client.writes, [])

    def test_first_publication_writes_only_task_branch_and_one_pr(self):
        client = FakeClient(self.data)
        result = publish(client, self.data, cloud_tests=self.cloud_tests)
        self.assertEqual(result["pr"], 4)
        self.assertEqual(client.writes, ["blob", "tree", "commit", "ref:agent/issue-3", "pr", "comment"])
        self.assertEqual(result["chatgpt_web_acceptance"], "NOT_RUN")
        self.assertFalse(result["merged"])

    def test_same_artifact_reconciles_without_duplicate_writes(self):
        client = FakeClient(self.data)
        first = publish(client, self.data, cloud_tests=self.cloud_tests)
        before = list(client.writes)
        second = publish(client, self.data, cloud_tests=self.cloud_tests)
        self.assertEqual(first, second)
        self.assertEqual(before, client.writes)

    def test_closed_pr_is_not_reopened_or_duplicated(self):
        client = FakeClient(self.data)
        publish(client, self.data, cloud_tests=self.cloud_tests)
        client.prs[0]["state"] = "closed"
        before = list(client.writes)
        self.assertEqual(publish(client, self.data, cloud_tests=self.cloud_tests)["state"], "closed")
        self.assertEqual(before, client.writes)

    def test_unknown_pr_write_is_not_retried_and_later_reconciles(self):
        client = FakeClient(self.data)
        client.pr_uncertain = True
        with self.assertRaises(UncertainWrite): publish(client, self.data, cloud_tests=self.cloud_tests)
        self.assertEqual(client.writes.count("pr"), 1)
        self.assertEqual(publish(client, self.data, cloud_tests=self.cloud_tests)["pr"], 4)
        self.assertEqual(client.writes.count("pr"), 1)

    def test_foreign_existing_task_commit_is_never_overwritten(self):
        client = FakeClient(self.data)
        client.branch = "f" * 40
        client.message = "someone else's work"
        with self.assertRaises(ValueError): publish(client, self.data, cloud_tests=self.cloud_tests)
        self.assertEqual(client.writes, [])

    def test_multiple_prs_require_reconciliation(self):
        client = FakeClient(self.data)
        publish(client, self.data, cloud_tests=self.cloud_tests)
        client.prs.append(copy.deepcopy(client.prs[0]))
        before = list(client.writes)
        with self.assertRaises(ValueError): publish(client, self.data, cloud_tests=self.cloud_tests)
        self.assertEqual(before, client.writes)


if __name__ == "__main__": unittest.main()
