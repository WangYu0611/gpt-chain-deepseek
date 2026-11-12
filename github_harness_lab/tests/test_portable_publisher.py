"""Offline publication/recovery tests with an in-memory GitHub implementation."""
import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab_harness.github import GitHubError, UncertainWrite
from lab_harness.portable_policy import (
    ARM_MARKER, PUBLIC_BINDING, TASK_MARKER, arm_record, digest,
)
from lab_harness.portable_publisher import (
    WORKFLOW_PATH, publish_task, verify_report_pr,
)
from lab_harness.project_profile import make_profile, profile_digest, task_spec


BASE = "a" * 40


def object_sha(value):
    return hashlib.sha1(json.dumps(value, sort_keys=True).encode()).hexdigest()


class MemoryGitHub:
    """Only in-memory API records; no requests, credentials or model execution."""
    def __init__(self, profile, activation):
        self.repository = profile["repository"]
        self.profile, self.activation = profile, activation
        self.blobs, self.trees, self.commits, self.refs, self.prs = {}, {}, {}, {}, {}
        self.issues, self.runs = {}, {}
        self.writes, self.reads = [], []
        self.fail_after_write = None
        self.after_write = None
        self.compare_override = None
        self.tree_override = None
        self.jobs_override = None
        empty_tree = object_sha({})
        self.trees[empty_tree] = {}
        self.commits[BASE] = {"sha": BASE, "tree": {"sha": empty_tree}, "parents": [], "message": "initial"}
        self.refs[profile["default_branch"]] = BASE
        self.issues[activation["control_issue"]] = {
            "number": activation["control_issue"], "state": "open", "user": {"login": profile["trusted_actor"]}}
        body = ARM_MARKER + json.dumps(arm_record(profile, activation))
        activation["arm_body_sha256"] = digest(body)
        self.comments = [{"id": activation["arm_comment_id"], "user": {"login": profile["trusted_actor"]},
                          "body": body, "created_at": activation["issued_at"], "updated_at": activation["issued_at"]}]

    def _written(self, kind, result):
        self.writes.append(kind)
        if self.after_write:
            self.after_write(kind)
        if self.fail_after_write == kind:
            self.fail_after_write = None
            raise UncertainWrite("response lost after accepted write")
        return copy.deepcopy(result)

    def get_issue(self, number):
        return copy.deepcopy(self.issues[number])

    def list_comments(self, number):
        assert number == self.activation["control_issue"]
        return copy.deepcopy(self.comments)

    def get_ref(self, branch):
        if branch not in self.refs:
            raise GitHubError("not found", status=404)
        return {"object": {"sha": self.refs[branch]}}

    def get_pr(self, number):
        return copy.deepcopy(self.prs[number])

    def _list(self, path, params):
        assert path == "/pulls" and params["state"] == "all"
        branch = params["head"].split(":", 1)[1]
        return copy.deepcopy([pr for pr in self.prs.values() if pr["head"]["ref"] == branch])

    def request(self, method, path, **kwargs):
        if method == "POST":
            assert path == "/git/refs"
            payload = kwargs["payload"]
            branch = payload["ref"].removeprefix("refs/heads/")
            assert branch.startswith("agent/harness-") and branch not in self.refs
            self.refs[branch] = payload["sha"]
            return self._written("ref", self.get_ref(branch))
        assert method == "GET"
        self.reads.append(path)
        if path.startswith("/git/commits/"):
            return copy.deepcopy(self.commits[path.rsplit("/", 1)[1]])
        if path.startswith("/git/blobs/"):
            sha = path.rsplit("/", 1)[1]
            raw = self.blobs[sha].encode()
            return {"sha": sha, "size": len(raw), "encoding": "base64", "content": base64.b64encode(raw).decode()}
        if path.startswith("/git/trees/"):
            if self.tree_override is not None:
                return copy.deepcopy(self.tree_override)
            entries = self.trees[path.rsplit("/", 1)[1]]
            return {"truncated": False, "tree": [{"path": name, **entry} for name, entry in entries.items()]}
        if path.startswith("/compare/"):
            if self.compare_override is not None:
                return copy.deepcopy(self.compare_override)
            base, head = path.removeprefix("/compare/").split("...")
            before = self.trees[self.commits[base]["tree"]["sha"]]
            after = self.trees[self.commits[head]["tree"]["sha"]]
            files = [{"filename": name, "sha": after[name]["sha"], "status": "added" if name not in before else "modified"}
                     for name in after if before.get(name) != after[name]]
            files.extend({"filename": name, "sha": before[name]["sha"], "status": "removed"}
                         for name in before if name not in after)
            count, current = 0, head
            while current != base and count < 10:
                count += 1
                parents = self.commits[current]["parents"]
                if not parents:
                    break
                current = parents[0]["sha"]
            return {"status": "ahead" if current == base else "diverged", "total_commits": count, "files": files}
        if path.endswith("/jobs"):
            if self.jobs_override is not None:
                return copy.deepcopy(self.jobs_override)
            return {"total_count": 2, "jobs": [{"name": name, "status": "completed", "conclusion": "success"}
                                                for name in ("generate", "publish")]}
        if path.startswith("/actions/runs/"):
            return copy.deepcopy(self.runs[path.rsplit("/", 1)[1]])
        raise AssertionError(path)

    def create_blob(self, content, encoding):
        assert encoding == "utf-8"
        raw = content.encode()
        sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        self.blobs[sha] = content
        return self._written("blob", {"sha": sha})

    def create_tree(self, base_tree, entries):
        tree = copy.deepcopy(self.trees[base_tree])
        for entry in entries:
            assert entry["path"] not in tree
            tree[entry["path"]] = {key: entry[key] for key in ("sha", "mode", "type")}
        sha = object_sha(tree)
        self.trees[sha] = tree
        return self._written("tree", {"sha": sha})

    def create_commit(self, message, tree, parents):
        commit = {"message": message, "tree": {"sha": tree}, "parents": [{"sha": parent} for parent in parents]}
        sha = object_sha(commit)
        self.commits[sha] = {"sha": sha, **commit}
        return self._written("commit", {"sha": sha})

    def create_pr(self, title, body, head, base, draft):
        number = 200 + len(self.prs)
        pr = {"number": number, "state": "open", "merged": False, "merged_at": None,
              "title": title, "body": body, "user": {"login": "github-actions[bot]"},
              "head": {"ref": head, "sha": self.refs[head], "repo": {"full_name": self.repository}},
              "base": {"ref": base, "sha": self.refs[base], "repo": {"full_name": self.repository}}}
        self.prs[number] = pr
        return self._written("pr", pr)


def fixture(repository="Example/first-project"):
    profile = make_profile(repository, "main", "Example")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = lambda value: value.isoformat().replace("+00:00", "Z")
    activation = {"schema": "harness-activation/v1", "enabled": True,
                  "repository": repository, "profile_sha256": profile_digest(profile),
                  "workflow_sha": BASE, "controller_sha256": "b" * 64,
                  "control_issue": 17, "arm_comment_id": 35, "arm_body_sha256": "c" * 64,
                  "nonce": "3f559ed3-156b-4f47-bd24-a049f251cd02", "issued_at": stamp(now - timedelta(minutes=1)),
                  "expires_at": stamp(now + timedelta(minutes=40)), "max_model_requests": 2,
                  "runner_label": profile["runner_label"]}
    return profile, activation, MemoryGitHub(profile, activation)


def task_artifact(client, number=1, previous=None):
    profile, activation = client.profile, client.activation
    spec = task_spec(profile, number)
    issue = 100 + number
    record = {"schema": "harness-task/v1", "repository": profile["repository"],
              "profile_sha256": profile_digest(profile), "nonce": activation["nonce"],
              "control_issue": activation["control_issue"], "number": number, "task_id": spec["task_id"],
              "dispatch": True, "previous_pr": previous["pr"] if previous else None,
              "previous_head": previous["head_sha"] if previous else None,
              "document_path": spec["document_path"], "report_path": spec["report_path"]}
    body = TASK_MARKER + json.dumps(record)
    client.issues[issue] = {"number": issue, "state": "open", "title": spec["task_id"], "body": body,
                            "user": {"login": profile["trusted_actor"]}}
    lines = [spec["validation"]["first_line"], "", *spec["validation"]["required_headings"],
             "本次只验证文档任务交接及真实报告，不代表项目功能已经完成。" * 3]
    if spec["validation"]["bullet_count"] is not None:
        lines.extend("- 核对任务真实执行证据与对应文档内容。" for _ in range(spec["validation"]["bullet_count"]))
    document = "\n".join(lines) + "\n"
    artifact = {key: activation[key] for key in PUBLIC_BINDING}
    artifact.update(schema="harness-document/v1", issue=issue, task_number=number, task_id=spec["task_id"],
                    issue_body_sha256=digest(body), document_path=spec["document_path"], report_path=spec["report_path"],
                    document=document, document_sha256=digest(document), session_id=f"offline-session-{number}",
                    model="deepseek-flash", version="0.2.1-rc.3", model_requests=1, exit_code=0,
                    guard={"request_count": 1, "visible_tool_count": 0, "violations": []}, usage={"totalTokens": 120},
                    run_id=str(900 + number), run_attempt=1)
    env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": profile["repository"], "GITHUB_EVENT_NAME": "issues",
           "GITHUB_ACTOR": profile["trusted_actor"], "GITHUB_WORKFLOW_SHA": BASE,
           "GITHUB_WORKFLOW_REF": profile["repository"] + "/" + WORKFLOW_PATH + "@refs/heads/main",
           "GITHUB_RUN_ID": artifact["run_id"], "GITHUB_RUN_ATTEMPT": "1", "HARNESS_ISSUE": str(issue)}
    client.runs[artifact["run_id"]] = {"id": int(artifact["run_id"]), "run_attempt": 1, "head_sha": BASE,
        "event": "issues", "path": WORKFLOW_PATH, "repository": {"full_name": profile["repository"]},
        "head_repository": {"full_name": profile["repository"]}, "actor": {"login": profile["trusted_actor"]},
        "triggering_actor": {"login": profile["trusted_actor"]}, "status": "in_progress", "conclusion": None}
    return artifact, env


class PortablePublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile, self.activation, self.client = fixture()
        self.artifact, self.env = task_artifact(self.client)

    def publish(self, artifact=None, env=None, client=None):
        return publish_task(client or self.client, (client or self.client).profile, (client or self.client).activation,
                            artifact or self.artifact, env or self.env, journal_root=self.root)

    def completed(self, artifact=None):
        self.client.runs[(artifact or self.artifact)["run_id"]].update(status="completed", conclusion="success")

    def verify(self, result):
        return verify_report_pr(self.client, self.profile, self.activation, result["pr"],
                                expected_number=result["task_number"], expected_head=result["head_sha"])

    def test_exact_two_commits_two_added_files_and_no_report_self_reference(self):
        result = self.publish()
        self.assertEqual(self.client.refs["main"], BASE)
        self.assertEqual(self.client.writes, ["blob", "tree", "commit", "blob", "tree", "commit", "ref", "pr"])
        final = self.client.commits[result["head_sha"]]
        first = self.client.commits[final["parents"][0]["sha"]]
        self.assertEqual(first["parents"], [{"sha": BASE}])
        self.assertEqual(final["message"], self.artifact["task_id"])
        self.assertEqual(first["message"], self.artifact["task_id"])
        report_entry = self.client.trees[final["tree"]["sha"]][self.artifact["report_path"]]
        text = self.client.blobs[report_entry["sha"]]
        self.assertIn(result["implementation_sha"], text)
        self.assertNotIn(result["head_sha"], text)
        self.completed()
        self.assertEqual(self.verify(result)["artifact_sha256"], result["artifact_sha256"])

    def test_second_pr_has_independent_base_and_only_its_two_paths(self):
        first = self.publish()
        self.completed()
        second_artifact, second_env = task_artifact(self.client, 2, first)
        second = self.publish(second_artifact, second_env)
        self.completed(second_artifact)
        self.verify(second)
        comparison = self.client.request("GET", "/compare/" + BASE + "..." + second["head_sha"])
        self.assertEqual({item["filename"] for item in comparison["files"]},
                         {second_artifact["document_path"], second_artifact["report_path"]})
        self.assertEqual(len(self.client.prs), 2)

    def test_same_engine_publishes_different_repository_without_source_edits(self):
        self.publish()
        _, _, client = fixture("Other/second-project")
        artifact, env = task_artifact(client)
        result = self.publish(artifact, env, client)
        self.assertEqual(result["repository"], "Other/second-project")
        self.assertNotEqual(next(iter(self.client.prs.values()))["head"]["ref"], next(iter(client.prs.values()))["head"]["ref"])

    def test_repeat_is_read_only_and_closed_pr_is_never_reopened(self):
        first = self.publish()
        before = list(self.client.writes)
        self.assertEqual(self.publish(), first)
        self.assertEqual(self.client.writes, before)
        self.client.prs[first["pr"]]["state"] = "closed"
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.client.writes, before)

    def test_lost_pr_and_branch_responses_reconcile_without_repeating_write(self):
        for kind in ("ref", "pr"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                self.profile, self.activation, self.client = fixture()
                self.artifact, self.env = task_artifact(self.client)
                self.root = Path(directory)
                self.client.fail_after_write = kind
                with self.assertRaises(UncertainWrite):
                    self.publish()
                result = self.publish()
                self.assertEqual(result["pr"], 200)
                self.assertEqual(self.client.writes.count(kind), 1)

    def test_unknown_unreachable_object_write_stays_blocked_across_restart(self):
        self.client.fail_after_write = "blob"
        with self.assertRaises(UncertainWrite):
            self.publish()
        with self.assertRaises(UncertainWrite):
            self.publish()
        self.assertEqual(self.client.writes, ["blob"])
        journal = next(self.root.rglob("publisher.json"))
        self.assertEqual(json.loads(journal.read_text())["steps"]["document_blob"]["status"], "pending")

    def test_actions_rerun_is_rejected_even_after_journal_loss(self):
        self.client.fail_after_write = "blob"
        with self.assertRaises(UncertainWrite):
            self.publish()
        next(self.root.rglob("publisher.json")).unlink()
        before = list(self.client.writes)
        with self.assertRaises(ValueError):
            self.publish(env={**self.env, "GITHUB_RUN_ATTEMPT": "2"})
        self.assertEqual(self.client.writes, before)

    def test_report_commit_cannot_rewrite_document_mode(self):
        result = self.publish()
        self.completed()
        implementation = self.client.commits[result["implementation_sha"]]
        self.client.trees[implementation["tree"]["sha"]][self.artifact["document_path"]]["mode"] = "120000"
        with self.assertRaisesRegex(ValueError, "Final commit must only add"):
            self.verify(result)

    def test_issue_change_or_revocation_is_checked_before_the_next_write(self):
        for kind in ("issue", "arm"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                self.profile, self.activation, self.client = fixture()
                self.artifact, self.env = task_artifact(self.client)
                self.root = Path(directory)
                def mutate(_):
                    if kind == "issue":
                        self.client.issues[self.artifact["issue"]]["body"] += "changed"
                    else:
                        self.client.comments.clear()
                self.client.after_write = mutate
                with self.assertRaises(ValueError):
                    self.publish()
                self.assertEqual(self.client.writes, ["blob"])

    def test_pending_jobs_other_workflow_or_partial_job_list_never_accepts(self):
        result = self.publish()
        with self.assertRaises(ValueError):
            self.verify(result)
        self.completed()
        original = copy.deepcopy(self.client.runs[self.artifact["run_id"]])
        self.client.runs[self.artifact["run_id"]]["path"] = ".github/workflows/unrelated.yml"
        with self.assertRaises(ValueError):
            self.verify(result)
        self.client.runs[self.artifact["run_id"]] = original
        self.client.jobs_override = {"total_count": 3, "jobs": [{"name": "generate", "status": "completed", "conclusion": "success"}]}
        with self.assertRaises(ValueError):
            self.verify(result)

    def test_extra_files_symlinks_report_edits_and_changed_head_are_rejected(self):
        result = self.publish()
        self.completed()
        current = copy.deepcopy(self.client.prs[result["pr"]])
        for change in ("body", "head", "author"):
            pr = self.client.prs[result["pr"]]
            if change == "body": pr["body"] += "edited"
            elif change == "head": pr["head"]["sha"] = "f" * 40
            else: pr["user"]["login"] = "someone-else"
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify(result)
            self.client.prs[result["pr"]] = copy.deepcopy(current)
        self.client.tree_override = {"truncated": False, "tree": []}
        with self.assertRaises(ValueError):
            self.verify(result)
        self.client.tree_override = None
        comparison = self.client.request("GET", "/compare/" + BASE + "..." + result["head_sha"])
        comparison["files"].append({"filename": "Source/hidden.cpp", "status": "added", "sha": "f" * 40})
        self.client.compare_override = comparison
        with self.assertRaises(ValueError):
            self.verify(result)

    def test_no_subprocess_network_or_model_is_used_by_publication(self):
        with patch("subprocess.Popen", side_effect=AssertionError("no process")), \
                patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("no HTTP")):
            result = self.publish()
            self.completed()
            self.verify(result)

    def test_artifact_tampering_or_cross_artifact_journal_reuse_never_writes(self):
        for field, value in (("document_sha256", "f" * 64), ("task_number", 3), ("model_requests", 2)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.publish({**self.artifact, field: value})
        self.assertEqual(self.client.writes, [])
        self.publish()
        before = list(self.client.writes)
        changed = {**self.artifact, "session_id": "different-session"}
        with self.assertRaises(ValueError):
            self.publish(changed)
        self.assertEqual(self.client.writes, before)


if __name__ == "__main__":
    unittest.main()
