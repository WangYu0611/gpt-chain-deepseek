"""Offline code generation/publication integration with actual protocol validation.

Only process/provider/materialization boundaries are injected. Git objects,
Issue/arm gates, text patches, artifact checks and PR verification remain real.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from lab_harness.coding_contract import TASK_MARKER, digest, strict_json, validate_artifact
from lab_harness.coding_engine import generate_task
from lab_harness.coding_publisher import publish_task, verify_report_pr
from lab_harness.project_profile import repository_namespace
from tests.coding_fixtures import fixture
from tests.test_coding_publisher import CodeMemoryGitHub, set_run
from tests.test_portable_publisher import BASE


SOURCE = "Source/Example/Widget.cpp"


class EngineMemoryGitHub(CodeMemoryGitHub):
    """Add the complete tree metadata returned by GitHub to the memory fixture."""
    def __init__(self, profile, activation):
        super().__init__(profile, activation)
        instructions = self.create_blob("# Project instructions\nOnly implement the specified task.\n", "utf-8")
        config = self.create_blob("[Project]\nName=Offline\n", "utf-8")
        tree = self.create_tree(self.commits[BASE]["tree"]["sha"], [
            {"path": "AGENTS.md", "mode": "100644", "type": "blob", "sha": instructions["sha"]},
            {"path": "Config/Default.ini", "mode": "100644", "type": "blob", "sha": config["sha"]},
        ])
        self.commits[BASE]["tree"]["sha"] = tree["sha"]
        self.writes.clear()

    def request(self, method, path, **kwargs):
        result = super().request(method, path, **kwargs)
        if method == "GET" and path.startswith("/git/trees/"):
            result["sha"] = path.rsplit("/", 1)[1]
            for entry in result["tree"]:
                if entry["type"] == "blob":
                    entry["size"] = len(self.blobs[entry["sha"]].encode("utf-8"))
        return result


def fake_prepare(profile, task, activation, root, client):
    workspace = (root / ".runtime" / "coding" / repository_namespace(profile["repository"])
                 / activation["nonce"] / str(task["number"]) / "workspace")
    workspace.mkdir(parents=True, exist_ok=False)
    tree = client.trees[client.commits[task["base_sha"]]["tree"]["sha"]]
    scopes = [value.replace("{task_id}", task["task_id"]) for value in profile["materialize_paths"]]
    for path, entry in tree.items():
        if any(path == scope or path.startswith(scope + "/") for scope in scopes):
            target = workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(client.blobs[entry["sha"]].encode("utf-8"))
    return workspace


def make_provider(*, change=None, mutate=None, guard_mutation=None, error=None):
    def generate(prompt, workspace, output, config):
        if error:
            raise error
        request = strict_json(prompt.split("\n", 1)[1])
        task_id = request["task_id"]
        before = next(item["before_sha256"] for item in request["allowed_files"] if item["path"] == SOURCE)
        original = (workspace / SOURCE).read_bytes().decode("utf-8")
        patch = {"schema": "harness-code-change/v1", "repository": request["repository"],
                 "base_sha": request["base_sha"], "task_id": task_id,
                 "changes": [{"path": SOURCE, "operation": "replace", "before_sha256": before,
                              "edits": [{"old": original, "new": f"// {task_id}\nint WidgetValue() {{ return 7; }}\n"}]}]}
        if change:
            change(patch)
        guard = {"protocol": "bounded-code-dsh/v1", "request_count": 1, "visible_tool_count": 0,
                 "violations": [], "max_tokens": 8192, "max_input_bytes": 524288, "tool_calls_blocked": 0}
        if guard_mutation:
            guard_mutation(guard)
        output.mkdir(parents=True, exist_ok=False)
        guard_path = output / "guard-report.json"
        guard_path.write_text(json.dumps(guard), encoding="utf-8")
        if mutate:
            mutate()
        return {"guard_report_path": str(guard_path), "final_text": json.dumps(patch, ensure_ascii=False),
                "exit_code": 0, "request_count": 1, "session_id": "offline-" + task_id,
                "model": "deepseek-flash", "version": "0.2.1-rc.3",
                "mode": "bounded-tool-free-code-headless", "provider": "deepseek-account",
                "model_called": True, "usage": {"inputTokens": 100, "outputTokens": 50}}
    return Mock(side_effect=generate)


def make_checks(*, fail=False, mutate=None):
    def check(profile, task, activation, workspace, changed_paths, output):
        output.mkdir(parents=True, exist_ok=False)
        summaries = []
        for spec in profile["checks"]:
            triggered = bool(set(changed_paths) & {path.replace("{task_id}", task["task_id"]) for path in spec["paths"]})
            if triggered:
                stdout = "FAILED\n" if fail else "PASS\n"
                stderr = ""
                folder = output / spec["id"]
                folder.mkdir()
                (folder / "stdout.log").write_bytes(stdout.encode("utf-8"))
                (folder / "stderr.log").write_bytes(stderr.encode("utf-8"))
                summary = {"id": spec["id"], "status": "FAIL" if fail else "PASS", "exit_code": 1 if fail else 0,
                           "timed_out": False, "output_limit_exceeded": False, "stdout_sha256": digest(stdout),
                           "stderr_sha256": digest(stderr), "duration_seconds": 0.1,
                           "reason": "Offline failing check" if fail else "Offline successful check"}
            else:
                summary = {"id": spec["id"], "status": "NOT_REQUIRED", "exit_code": None,
                           "timed_out": False, "output_limit_exceeded": False, "stdout_sha256": None,
                           "stderr_sha256": None, "duration_seconds": 0, "reason": "No changed path triggers this check"}
            summaries.append(summary)
        if mutate:
            mutate(workspace)
        return summaries
    return Mock(side_effect=check)


class CodingEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harness-code-engine-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.setup_project()

    def setup_project(self, repository="Example/alpha", actor="Example"):
        self.profile, self.activation, self.event, self.env, _ = fixture(repository=repository, actor=actor)
        self.client = EngineMemoryGitHub(self.profile, self.activation)
        self.client.issues[self.event["issue"]["number"]] = copy.deepcopy(self.event["issue"])

    def generate(self, provider=None, checks=None, prepare=fake_prepare):
        return generate_task(self.profile, self.activation, self.event, self.env, root=self.root,
                             client=self.client, generate=provider or make_provider(),
                             prepare=prepare, checks=checks or make_checks())

    def state(self):
        return self.root / ".runtime" / "coding" / repository_namespace(self.profile["repository"])

    def task_folder(self, number=1):
        return self.state() / self.activation["nonce"] / str(number)

    def next_issue(self, previous, number, transition):
        task = json.loads(self.event["issue"]["body"].split(TASK_MARKER, 1)[1])
        task.update(number=number, task_id=f"TASK-M2-{20 + number}", base_sha=previous["head_sha"],
                    previous_pr=previous["pr"], previous_head=previous["head_sha"], transition=transition,
                    goal="修复前一轮问题并完成本项任务，保持其他行为。")
        issue = {"number": 5100 + number, "state": "open", "title": task["task_id"],
                 "user": {"login": self.profile["trusted_actor"]},
                 "body": TASK_MARKER + json.dumps(task, ensure_ascii=False, separators=(",", ":"))}
        self.event["issue"] = copy.deepcopy(issue)
        self.client.issues[issue["number"]] = copy.deepcopy(issue)
        self.env.update(HARNESS_ISSUE=str(issue["number"]), GITHUB_RUN_ID=str(8000 + number))

    def publish_and_verify(self, artifact):
        set_run(self.client, artifact)
        published = publish_task(self.client, self.profile, self.activation, artifact, self.env,
                                 journal_root=self.root / "publisher")
        self.client.runs[artifact["run_id"]].update(status="completed", conclusion="success")
        report = verify_report_pr(self.client, self.profile, self.activation, published["pr"],
                                  expected_number=artifact["task"]["number"], expected_head=published["head_sha"])
        self.assertEqual(report["check_status"], artifact["check_status"])
        return published

    def test_valid_code_patch_produces_pass_artifact_and_single_request_evidence(self):
        provider, checks = make_provider(), make_checks()
        artifact, path = self.generate(provider, checks)
        self.assertEqual(path, self.task_folder() / "result.json")
        self.assertEqual(strict_json(path.read_text(encoding="utf-8")), artifact)
        self.assertEqual(validate_artifact(self.profile, self.activation, artifact, self.env), artifact)
        self.assertEqual(artifact["check_status"], "PASS")
        self.assertEqual(artifact["model_requests"], 1)
        self.assertEqual(artifact["changes"][0]["operation"], "modify")
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(checks.call_count, 1)
        self.assertEqual(self.client.writes, [])
        self.assertEqual(strict_json((self.task_folder() / "claim.json").read_text())["state"], "GENERATED")
        self.assertFalse((self.state() / "lease.json").exists())

    def test_failed_check_still_produces_explicit_fail_artifact_for_rework(self):
        provider = make_provider()
        artifact, _ = self.generate(provider, make_checks(fail=True))
        self.assertEqual(artifact["check_status"], "FAIL")
        self.assertEqual(artifact["checks"][0]["exit_code"], 1)
        self.assertEqual(provider.call_count, 1)
        report = self.publish_and_verify(artifact)
        self.assertEqual(report["check_status"], "FAIL")

    def test_duplicate_slot_and_new_run_id_cannot_bill_twice(self):
        provider = make_provider()
        self.generate(provider)
        self.env["GITHUB_RUN_ID"] = "8099"
        with self.assertRaises((ValueError, FileExistsError)):
            self.generate(provider)
        self.assertEqual(provider.call_count, 1)
        self.assertFalse((self.state() / "lease.json").exists())

    def test_provider_exception_retains_claim_and_cannot_repeat_request(self):
        provider = make_provider(error=RuntimeError("Offline provider lost response"))
        with self.assertRaises(RuntimeError):
            self.generate(provider)
        claim = strict_json((self.task_folder() / "claim.json").read_text())
        self.assertEqual(claim["state"], "BLOCKED")
        self.assertTrue(claim["model_request_may_have_started"])
        with self.assertRaises((ValueError, FileExistsError)):
            self.generate(provider)
        self.assertEqual(provider.call_count, 1)
        self.assertFalse((self.task_folder() / "result.json").exists())

    def test_changed_existing_source_or_unlisted_source_created_by_check_blocks_artifact(self):
        for kind in ("changed", "created"):
            with self.subTest(kind=kind):
                self.setup_project("Example/" + kind)
                def mutate(workspace):
                    target = workspace / (SOURCE if kind == "changed" else "Source/Example/Unexpected.cpp")
                    target.write_text("unapproved change\n", encoding="utf-8")
                provider = make_provider()
                with self.assertRaises(ValueError):
                    self.generate(provider, make_checks(mutate=mutate))
                self.assertEqual(provider.call_count, 1)
                self.assertFalse((self.task_folder() / "result.json").exists())

    def test_stale_out_of_scope_and_invalid_model_patches_stop_before_checks(self):
        mutations = {
            "stale": lambda p: p["changes"][0].update(before_sha256="f" * 64),
            "outside": lambda p: p["changes"][0].update(path="Other.cpp"),
            "base": lambda p: p.update(base_sha="f" * 40),
            "empty": lambda p: p.update(changes=[]),
        }
        for kind, mutation in mutations.items():
            with self.subTest(kind=kind):
                self.setup_project("Example/" + kind)
                provider, checks = make_provider(change=mutation), make_checks()
                with self.assertRaises(ValueError):
                    self.generate(provider, checks)
                self.assertEqual(provider.call_count, 1)
                checks.assert_not_called()
                self.assertFalse((self.task_folder() / "result.json").exists())

    def test_owner_arm_issue_and_stop_gates_reject_before_model_call(self):
        for kind in ("arm", "issue", "stop", "lease", "workflow", "rerun"):
            with self.subTest(kind=kind):
                self.setup_project("Example/" + kind)
                if kind == "arm": self.client.comments[0]["body"] += "revoked"
                elif kind == "issue": self.client.issues[self.event["issue"]["number"]]["body"] += "changed"
                elif kind in {"stop", "lease"}:
                    self.state().mkdir(parents=True)
                    (self.state() / ("STOP" if kind == "stop" else "lease.json")).write_text("{}")
                elif kind == "workflow": self.env["GITHUB_WORKFLOW_REF"] = "Example/arm/.github/workflows/foreign.yml@refs/heads/main"
                else: self.env["GITHUB_RUN_ATTEMPT"] = "2"
                provider = make_provider()
                with self.assertRaises(ValueError):
                    self.generate(provider)
                provider.assert_not_called()

    def test_revocation_during_preparation_stops_before_request(self):
        def prepare(*args):
            workspace = fake_prepare(*args)
            self.client.comments[0]["body"] += " revoked during preparation"
            return workspace
        provider = make_provider()
        with self.assertRaises(ValueError):
            self.generate(provider, prepare=prepare)
        provider.assert_not_called()

    def test_revocation_during_model_call_does_not_apply_or_publish_patch(self):
        provider = make_provider(mutate=lambda: self.client.comments[0].update(body="revoked"))
        checks = make_checks()
        with self.assertRaises(ValueError):
            self.generate(provider, checks)
        self.assertEqual(provider.call_count, 1)
        checks.assert_not_called()
        self.assertEqual((self.task_folder() / "workspace" / SOURCE).read_bytes(), b"old\n")
        self.assertFalse((self.task_folder() / "result.json").exists())

    def test_materialized_source_and_provider_guard_must_match_reviewed_inputs(self):
        def prepare(*args):
            workspace = fake_prepare(*args)
            (workspace / SOURCE).write_text("stale snapshot", encoding="utf-8")
            return workspace
        provider = make_provider()
        with self.assertRaises(ValueError):
            self.generate(provider, prepare=prepare)
        provider.assert_not_called()
        self.setup_project("Example/guard")
        provider = make_provider(guard_mutation=lambda g: g.update(visible_tool_count=1))
        checks = make_checks()
        with self.assertRaises(ValueError):
            self.generate(provider, checks)
        self.assertEqual(provider.call_count, 1)
        checks.assert_not_called()

    def test_same_engine_supports_another_repository_and_distinct_namespace(self):
        first, first_path = self.generate()
        self.setup_project("Other/beta", actor="Other")
        second, second_path = self.generate()
        self.assertNotEqual(first_path, second_path)
        self.assertNotEqual(first["repository"], second["repository"])
        self.assertEqual(second["check_status"], "PASS")

    def test_three_real_protocol_rounds_fail_rework_pass_then_next_without_two_round_cap(self):
        providers, artifacts, publications = [], [], []
        for number in (1, 2, 3):
            if number > 1:
                self.next_issue(publications[-1], number, "REWORK" if number == 2 else "NEXT")
            provider = make_provider()
            providers.append(provider)
            artifact, path = self.generate(provider, make_checks(fail=number == 1))
            artifacts.append(artifact)
            publications.append(self.publish_and_verify(artifact))
            self.assertEqual(path.parent.name, str(number))
        self.assertEqual([item["check_status"] for item in artifacts], ["FAIL", "PASS", "PASS"])
        self.assertEqual([item["task"]["transition"] for item in artifacts], ["INITIAL", "REWORK", "NEXT"])
        self.assertEqual([provider.call_count for provider in providers], [1, 1, 1])
        self.assertEqual(len(self.client.prs), 3)
        self.assertEqual(self.client.refs["main"], BASE)
        self.assertEqual(artifacts[1]["task"]["base_sha"], publications[0]["head_sha"])
        self.assertEqual(artifacts[2]["task"]["base_sha"], publications[1]["head_sha"])
        self.assertTrue(all(item["model_requests"] == 1 for item in artifacts))
        # A report already published is a second independent no-billing gate.
        with self.assertRaisesRegex(ValueError, "already has a report"):
            self.generate(providers[-1])
        self.assertEqual(providers[-1].call_count, 1)


if __name__ == "__main__":
    unittest.main()
