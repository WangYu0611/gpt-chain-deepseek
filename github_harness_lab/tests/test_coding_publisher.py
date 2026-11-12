"""Memory GitHub publication tests; no network, model, Runner or project code."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab_harness.coding_contract import (
    ARM_MARKER, TASK_MARKER, WORKFLOW_PATH, arm_record, canonical_digest, digest,
)
from lab_harness.coding_publisher import publish_task, verify_report_pr
from lab_harness.github import UncertainWrite
from tests.coding_fixtures import artifact_fixture, fixture
from tests.test_portable_publisher import BASE, MemoryGitHub, object_sha


class CodeMemoryGitHub(MemoryGitHub):
    def __init__(self, profile, activation):
        with patch("tests.test_portable_publisher.ARM_MARKER", ARM_MARKER), \
             patch("tests.test_portable_publisher.arm_record", arm_record):
            super().__init__(profile, activation)
        self.pr_reads = []
        blob = self.create_blob("old\n", "utf-8")
        tree = self.create_tree(self.commits[BASE]["tree"]["sha"], [
            {"path": "Source/Example/Widget.cpp", "mode": "100644", "type": "blob", "sha": blob["sha"]}])
        self.commits[BASE]["tree"]["sha"] = tree["sha"]
        self.writes.clear()

    def create_tree(self, base_tree, entries):
        tree = copy.deepcopy(self.trees[base_tree])
        for entry in entries:
            tree[entry["path"]] = {key: entry[key] for key in ("sha", "mode", "type")}
        sha = object_sha(tree)
        self.trees[sha] = tree
        return self._written("tree", {"sha": sha})

    def request(self, method, path, **kwargs):
        if method == "POST":
            assert path == "/git/refs"
            payload = kwargs["payload"]
            branch = payload["ref"].removeprefix("refs/heads/")
            assert branch.startswith("agent/code-") and branch not in self.refs
            self.refs[branch] = payload["sha"]
            return self._written("ref", self.get_ref(branch))
        return super().request(method, path, **kwargs)

    def get_pr(self, number):
        self.pr_reads.append(number)
        return super().get_pr(number)


def code_fixture(repository="Example/alpha"):
    profile, activation, event, env, _ = fixture(repository=repository)
    client = CodeMemoryGitHub(profile, activation)
    client.issues[event["issue"]["number"]] = copy.deepcopy(event["issue"])
    artifact = artifact_fixture(profile, activation, event, env)
    set_run(client, artifact)
    return profile, activation, client, artifact, env


def set_run(client, artifact):
    client.runs[artifact["run_id"]] = {"id": int(artifact["run_id"]), "run_attempt": 1,
        "head_sha": client.activation["workflow_sha"], "event": "issues", "path": WORKFLOW_PATH,
        "repository": {"full_name": client.repository}, "head_repository": {"full_name": client.repository},
        "actor": {"login": client.profile["trusted_actor"]},
        "triggering_actor": {"login": client.profile["trusted_actor"]}, "status": "in_progress", "conclusion": None}


def next_artifact(client, previous, number, *, transition="NEXT"):
    profile, activation = client.profile, client.activation
    _, _, event, env, _ = fixture(profile=profile)
    task = json.loads(event["issue"]["body"].split(TASK_MARKER, 1)[1])
    task.update(number=number, task_id=f"TASK-M2-{20 + number}", base_sha=previous["head_sha"],
                previous_pr=previous["pr"], previous_head=previous["head_sha"], transition=transition,
                goal="修复之前报告的问题，验证修改。")
    issue = {**event["issue"], "number": 5100 + number, "title": task["task_id"],
             "body": TASK_MARKER + json.dumps(task, ensure_ascii=False, separators=(",", ":"))}
    event["issue"] = issue
    env.update(HARNESS_ISSUE=str(issue["number"]), GITHUB_RUN_ID=str(8000 + number))
    client.issues[issue["number"]] = copy.deepcopy(issue)
    artifact = artifact_fixture(profile, activation, event, env)
    before = client.trees[client.commits[task["base_sha"]]["tree"]["sha"]]["Source/Example/Widget.cpp"]
    content = f"// Offline code round {number}\nint WidgetValue() {{ return {number}; }}\n"
    artifact["changes"][0].update(before_sha256=digest(client.blobs[before["sha"]]),
                                   content=content, content_sha256=digest(content))
    artifact["validated_change_set_sha256"] = canonical_digest(artifact["changes"])
    artifact["session_id"] = f"session-fixture-{number}"
    set_run(client, artifact)
    return artifact, env


class CodingPublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile, self.activation, self.client, self.artifact, self.env = code_fixture()

    def publish(self, artifact=None, env=None, client=None):
        client = client or self.client
        return publish_task(client, client.profile, client.activation, artifact or self.artifact,
                            env or self.env, journal_root=self.root)

    def complete(self, artifact=None):
        self.client.runs[(artifact or self.artifact)["run_id"]].update(status="completed", conclusion="success")

    def verify(self, result):
        return verify_report_pr(self.client, self.profile, self.activation, result["pr"],
                                expected_number=result["task_number"], expected_head=result["head_sha"])

    def test_two_commits_exact_code_then_report_and_no_main_write(self):
        result = self.publish()
        final = self.client.commits[result["head_sha"]]
        implementation = self.client.commits[result["implementation_sha"]]
        self.assertEqual(final["parents"], [{"sha": result["implementation_sha"]}])
        self.assertEqual(implementation["parents"], [{"sha": BASE}])
        self.assertEqual(final["message"], self.artifact["task"]["task_id"])
        self.assertEqual(implementation["message"], self.artifact["task"]["task_id"])
        self.assertEqual(self.client.refs["main"], BASE)
        entry = self.client.trees[final["tree"]["sha"]][result["report_path"]]
        text = self.client.blobs[entry["sha"]]
        self.assertIn("NOT_RUN", text)
        self.assertIn("user chooses", text)
        self.assertIn(result["implementation_sha"], text)
        self.assertNotIn(result["head_sha"], text)
        self.assertNotIn(self.artifact["changes"][0]["content"], self.client.prs[result["pr"]]["body"])
        self.complete()
        self.assertEqual(self.verify(result)["artifact_sha256"], result["artifact_sha256"])

    def test_four_rounds_cumulate_with_constant_depth_verification(self):
        previous = self.publish()
        self.complete()
        results = [previous]
        for number in range(2, 5):
            artifact, env = next_artifact(self.client, previous, number)
            previous = self.publish(artifact, env)
            self.complete(artifact)
            results.append(previous)
        self.assertEqual(len(self.client.prs), 4)
        self.assertEqual(self.client.refs["main"], BASE)
        for result in results[1:]:
            incremental = self.client.request("GET", f"/compare/{result['base_sha']}...{result['head_sha']}")
            self.assertEqual(len(incremental["files"]), 2)
            self.assertEqual(self.client.prs[result["pr"]]["base"]["sha"], BASE)
        cumulative = self.client.request("GET", f"/compare/{BASE}...{previous['head_sha']}")
        self.assertEqual(cumulative["total_commits"], 8)
        self.assertEqual(len(cumulative["files"]), 5)
        # Verifying a report checks only its own source Issue and evidence, not
        # every historical PR. The caller separately checks its next predecessor.
        self.client.pr_reads.clear()
        self.verify(previous)
        self.assertEqual(set(self.client.pr_reads), {previous["pr"]})

    def test_failed_check_report_publishes_but_next_requires_rework(self):
        self.artifact["checks"][0].update(status="FAIL", exit_code=1, reason="Synthetic build failure")
        self.artifact["check_status"] = "FAIL"
        first = self.publish()
        self.complete()
        self.assertEqual(self.verify(first)["check_status"], "FAIL")
        artifact, env = next_artifact(self.client, first, 2)
        writes = list(self.client.writes)
        with self.assertRaises(ValueError):
            self.publish(artifact, env)
        self.assertEqual(self.client.writes, writes)
        artifact, env = next_artifact(self.client, first, 2, transition="REWORK")
        second = self.publish(artifact, env)
        self.complete(artifact)
        self.assertEqual(self.verify(second)["check_status"], "PASS")

    def test_unknown_blob_is_not_replayed_and_unknown_visible_ref_or_pr_reconcile(self):
        for step in ("blob", "ref", "pr"):
            with self.subTest(step=step):
                profile, activation, client, artifact, env = code_fixture("Example/" + step)
                client.fail_after_write = step
                with self.assertRaises(UncertainWrite):
                    self.publish(artifact, env, client)
                before = list(client.writes)
                if step == "blob":
                    with self.assertRaises(UncertainWrite):
                        self.publish(artifact, env, client)
                    self.assertEqual(client.writes, before)
                else:
                    result = self.publish(artifact, env, client)
                    self.assertEqual(client.writes.count(step), 1)
                    self.assertEqual(result["task_number"], 1)

    def test_arm_or_issue_change_stops_before_next_write(self):
        for mutation in ("arm", "issue", "main"):
            profile, activation, client, artifact, env = code_fixture("Example/" + mutation)
            def mutate(_kind):
                if mutation == "arm":
                    client.comments[0]["body"] += " edited"
                elif mutation == "issue":
                    client.issues[artifact["issue"]]["body"] += " edited"
                else:
                    client.refs["main"] = "f" * 40
            client.after_write = mutate
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.publish(artifact, env, client)
            self.assertEqual(client.writes, ["blob"])
            self.assertEqual(client.prs, {})

    def test_predecessor_changed_between_writes_stops_successor(self):
        first = self.publish()
        self.complete()
        artifact, env = next_artifact(self.client, first, 2)
        before = len(self.client.writes)
        self.client.after_write = lambda _kind: self.client.prs[first["pr"]].update(body="changed")
        with self.assertRaises(ValueError):
            self.publish(artifact, env)
        self.assertEqual(len(self.client.writes), before + 1)
        self.assertEqual(len(self.client.prs), 1)

    def test_wrong_before_hash_and_check_misrepresentation_have_zero_writes(self):
        for mutation in ("hash", "checks", "content", "noop"):
            artifact = copy.deepcopy(self.artifact)
            if mutation == "hash":
                artifact["changes"][0]["before_sha256"] = "f" * 64
                artifact["validated_change_set_sha256"] = canonical_digest(artifact["changes"])
            elif mutation == "checks":
                artifact["checks"][0]["status"] = "FAIL"
            elif mutation == "noop":
                artifact["changes"][0].update(content="old\n", content_sha256=digest("old\n"))
                artifact["validated_change_set_sha256"] = canonical_digest(artifact["changes"])
            else:
                artifact["changes"][0]["content"] += "tampered"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.publish(artifact)
            self.assertEqual(self.client.writes, [])

    def test_report_body_head_and_commit_content_tampering_are_rejected(self):
        result = self.publish()
        self.complete()
        pr = self.client.prs[result["pr"]]
        original = copy.deepcopy(pr)
        for field in ("body", "head", "closed", "merged"):
            self.client.prs[result["pr"]] = copy.deepcopy(original)
            pr = self.client.prs[result["pr"]]
            if field == "body":
                pr["body"] += "\nmanual edit"
            elif field == "head":
                pr["head"]["sha"] = "f" * 40
            elif field == "closed":
                pr["state"] = "closed"
            else:
                pr["merged"] = True
            before = list(self.client.writes)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify(result)
            with self.assertRaises(ValueError):
                self.publish()
            self.assertEqual(self.client.writes, before)
        self.client.prs[result["pr"]] = original
        final_tree = self.client.trees[self.client.commits[result["head_sha"]]["tree"]["sha"]]
        blob = final_tree["Source/Example/Widget.cpp"]["sha"]
        self.client.blobs[blob] += "maliciously changed blob bytes"
        with self.assertRaises(ValueError):
            self.verify(result)

    def test_extra_tree_change_or_second_commit_mode_change_is_rejected(self):
        result = self.publish()
        self.complete()
        final_tree = self.client.trees[self.client.commits[result["head_sha"]]["tree"]["sha"]]
        original = copy.deepcopy(final_tree)
        final_tree["Source/Example/Widget.cpp"]["mode"] = "100755"
        with self.assertRaises(ValueError):
            self.verify(result)
        final_tree.clear()
        final_tree.update(original)
        final_tree["unrelated.txt"] = copy.deepcopy(final_tree["Source/Example/Widget.cpp"])
        with self.assertRaises(ValueError):
            self.verify(result)

    def test_provenance_and_complete_jobs_are_required_but_code_fail_is_separate(self):
        result = self.publish()
        with self.assertRaises(ValueError):
            self.verify(result)
        self.complete()
        run = self.client.runs[self.artifact["run_id"]]
        original = copy.deepcopy(run)
        for field, value in (("path", ".github/workflows/foreign.yml"), ("run_attempt", 2),
                             ("head_sha", "f" * 40), ("event", "workflow_dispatch")):
            run.clear()
            run.update(original)
            run[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify(result)
        run.clear()
        run.update(original)
        self.client.jobs_override = {"total_count": 3, "jobs": []}
        with self.assertRaises(ValueError):
            self.verify(result)
        self.client.jobs_override = {"total_count": 2, "jobs": [
            {"name": "generate", "status": "completed", "conclusion": "success"},
            {"name": "publish", "status": "completed", "conclusion": "failure"}]}
        with self.assertRaises(ValueError):
            self.verify(result)

    def test_large_code_and_new_file_publish_without_source_in_report(self):
        content = "// text\n" * 20000
        self.artifact["changes"][0].update(content=content, content_sha256=digest(content))
        path = "Docs/Reports/" + self.artifact["task"]["task_id"] + ".md"
        self.artifact["task"]["allowed_paths"].append(path)
        body = TASK_MARKER + json.dumps(self.artifact["task"], ensure_ascii=False, separators=(",", ":"))
        self.client.issues[self.artifact["issue"]]["body"] = body
        self.artifact["issue_body_sha256"] = digest(body)
        self.artifact["changes"].append({"path": path, "operation": "add", "before_sha256": None,
                                         "content": "Model notes\n", "content_sha256": digest("Model notes\n")})
        self.artifact["validated_change_set_sha256"] = canonical_digest(self.artifact["changes"])
        result = self.publish()
        self.complete()
        self.verify(result)
        self.assertLess(len(self.client.prs[result["pr"]]["body"]), 32768)


if __name__ == "__main__":
    unittest.main()
