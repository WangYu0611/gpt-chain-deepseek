"""Shared engine tests using temporary directories and a synthetic provider only."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from lab_harness.portable_engine import generate_task
from lab_harness.portable_policy import PUBLIC_BINDING, validate_artifact
from lab_harness.project_profile import make_profile
from tests.portable_fixtures import document, fixture, make_generator, replace_task_record


class PortableEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def execute(self, values, generator):
        profile, activation, event, env, client = values
        return generate_task(profile, activation, event, env, root=self.root, client=client, generate=generator)

    def test_same_engine_runs_distinct_repository_configuration_without_source_execution(self):
        paths = []
        for repository, branch, actor in (("Example/alpha", "main", "Example"), ("Other/beta", "release/v1", "Other")):
            profile = make_profile(repository, branch, actor, source_repository=str(self.root / "absent-source-never-read"))
            profile["document_tasks"][0]["prompt"] += "本项目的独立配置。"
            values = fixture(profile=profile)
            generate = make_generator(profile)
            artifact, path = self.execute(values, generate)
            paths.append(path)
            generate.assert_called_once()
            self.assertEqual(artifact["repository"], repository)
            self.assertEqual(artifact["document"], document(profile))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), artifact)
            self.assertEqual(validate_artifact(profile, values[1], artifact, values[3]), artifact["document"])
            for key in PUBLIC_BINDING:
                self.assertEqual(artifact[key], values[1][key])
            prompt, workspace, output, config = generate.call_args.args
            self.assertEqual(prompt, profile["document_tasks"][0]["prompt"])
            self.assertEqual(list(Path(workspace).iterdir()), [])
            self.assertTrue(Path(output).is_relative_to(self.root))
            self.assertEqual(config, values[1]["dsh"])
            self.assertEqual(artifact["report_path"], profile["document_tasks"][0]["report_path"])
        self.assertNotEqual(paths[0].parent.parent.parent, paths[1].parent.parent.parent)

    def test_third_task_wrong_repo_profile_expiry_revocation_and_job_never_call_model(self):
        for kind in ("third", "repository", "profile", "expired", "revoked", "duplicate-arm", "workflow", "rerun", "remote-title", "task-path", "existing-report"):
            values = fixture(3 if kind == "third" else 1)
            profile, activation, event, env, client = values
            if kind == "repository": env["GITHUB_REPOSITORY"] = "Other/beta"
            elif kind == "profile": profile["document_tasks"][0]["prompt"] += "changed"
            elif kind == "expired": activation["expires_at"] = "2020-01-01T00:00:00Z"
            elif kind == "revoked": client.control_issue["state"] = "closed"
            elif kind == "duplicate-arm": client.comments.append(copy.deepcopy(client.comments[0]))
            elif kind == "workflow": env["GITHUB_WORKFLOW_REF"] = env["GITHUB_WORKFLOW_REF"].replace("harness-documents.yml", "other.yml")
            elif kind == "rerun": env["GITHUB_RUN_ATTEMPT"] = "2"
            elif kind == "remote-title": client.issue["title"] = "TASK-OTHER"
            elif kind == "task-path": replace_task_record(client, document_path="Other.md")
            elif kind == "existing-report": client.prs.append({"number": 6101})
            generate = Mock()
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.execute(values, generate)
            generate.assert_not_called()
        self.assertFalse(list(self.root.rglob("result.json")))

    def test_issue_changed_after_opened_event_is_rejected_before_model(self):
        values = fixture()
        values[4].issue["body"] += "\nEdited while job was queued"
        generate = Mock()
        with self.assertRaises(ValueError):
            self.execute(values, generate)
        generate.assert_not_called()

    def test_same_event_is_never_billed_twice_after_success(self):
        values = fixture()
        generate = make_generator(values[0])
        _, path = self.execute(values, generate)
        original = path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.execute(values, generate)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(list(self.root.rglob("lease.json")))

    def test_unknown_provider_outcome_keeps_permanent_claim_and_blocks_retry(self):
        values = fixture()
        first = Mock(side_effect=RuntimeError("synthetic provider outcome unknown"))
        with self.assertRaises(RuntimeError):
            self.execute(values, first)
        claims = list(self.root.rglob("claim.json"))
        self.assertEqual(len(claims), 1)
        claim = json.loads(claims[0].read_text(encoding="utf-8"))
        self.assertEqual(claim["state"], "BLOCKED")
        self.assertTrue(claim["model_request_may_have_started"])
        self.assertNotIn("synthetic provider outcome", claims[0].read_text(encoding="utf-8"))
        second = make_generator(values[0])
        with self.assertRaises(FileExistsError):
            self.execute(values, second)
        second.assert_not_called()
        self.assertFalse(list(self.root.rglob("result.json")))

    def test_remote_change_during_generation_prevents_artifact(self):
        for kind in ("body", "arm", "main"):
            with tempfile.TemporaryDirectory() as folder:
                values = fixture()
                profile, activation, event, env, client = values
                inner = make_generator(profile)
                def changed(*args):
                    result = inner(*args)
                    if kind == "body": client.issue["body"] += "\nChanged during model call"
                    elif kind == "arm": client.comments.clear()
                    else: client.ref_sha = "d" * 40
                    return result
                generate = Mock(side_effect=changed)
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    generate_task(profile, activation, event, env, root=Path(folder), client=client, generate=generate)
                generate.assert_called_once()
                self.assertFalse(list(Path(folder).rglob("result.json")))
                self.assertEqual(len(list(Path(folder).rglob("claim.json"))), 1)

    def test_guard_model_identity_and_usage_failures_keep_claim_without_artifact(self):
        for kind in ("missing-guard", "outside-guard", "bad-counter", "second-request", "tools", "violation", "model", "version", "provider", "document", "usage"):
            with tempfile.TemporaryDirectory() as folder:
                values = fixture()
                profile, activation, event, env, client = values
                inner = make_generator(profile)
                def changed(*args):
                    result = inner(*args)
                    guard = Path(result["guard_report_path"])
                    if kind == "missing-guard": guard.unlink()
                    elif kind == "outside-guard": result["guard_report_path"] = str(guard.parent.parent / "outside.json")
                    elif kind in ("bad-counter", "tools", "violation"):
                        data = json.loads(guard.read_text(encoding="utf-8"))
                        if kind == "bad-counter": data["request_count"] = True
                        elif kind == "tools": data["visible_tool_count"] = 1
                        else: data["violations"] = ["synthetic policy violation"]
                        guard.write_text(json.dumps(data), encoding="utf-8")
                    elif kind == "second-request": result["request_count"] = 2
                    elif kind in ("model", "version", "provider"): result[kind] = "different"
                    elif kind == "document": result["final_text"] += "https://example.test\n"
                    else: result["usage"] = {"private_log": "must not be published"}
                    return result
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    generate_task(profile, activation, event, env, root=Path(folder), client=client, generate=changed)
                inner.assert_called_once()
                self.assertFalse(list(Path(folder).rglob("result.json")))
                self.assertEqual(len(list(Path(folder).rglob("claim.json"))), 1)

    def test_new_profile_same_repository_cannot_reuse_same_run_task_claim(self):
        first = fixture()
        self.execute(first, make_generator(first[0]))
        changed = copy.deepcopy(first[0])
        changed["document_tasks"][0]["prompt"] += "new profile"
        second = fixture(profile=changed)
        generate = make_generator(changed)
        with self.assertRaises(FileExistsError):
            self.execute(second, generate)
        generate.assert_not_called()
        self.assertEqual(len(list(self.root.rglob("claim.json"))), 1)

    def test_repository_lease_rejects_concurrent_profile_without_second_model(self):
        first = fixture()
        profile2 = copy.deepcopy(first[0])
        profile2["document_tasks"][0]["prompt"] += "concurrent profile"
        second = fixture(profile=profile2)
        entered, release = threading.Event(), threading.Event()
        inner = make_generator(first[0])
        results, errors = [], []
        def held(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test synchronization timed out")
            return inner(*args)
        def run_first():
            try: results.append(self.execute(first, held))
            except BaseException as error: errors.append(error)
        thread = threading.Thread(target=run_first, daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(5), "First worker did not reach its fake model")
            other = make_generator(second[0])
            with self.assertRaisesRegex(ValueError, "lease"):
                self.execute(second, other)
            other.assert_not_called()
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        inner.assert_called_once()

    def test_second_task_requires_predecessor_verification_then_executes_once(self):
        values = fixture(2)
        generate = make_generator(values[0], 2)
        with patch("lab_harness.portable_publisher.verify_report_pr") as verify:
            artifact, _ = self.execute(values, generate)
        self.assertGreaterEqual(verify.call_count, 2)
        self.assertEqual(artifact["task_number"], 2)
        generate.assert_called_once()
        denied = fixture(2, repository="Other/beta", actor="Other")
        never = Mock()
        with patch("lab_harness.portable_publisher.verify_report_pr", side_effect=ValueError("prior report failed")):
            with self.assertRaisesRegex(ValueError, "prior report failed"):
                self.execute(denied, never)
        never.assert_not_called()


if __name__ == "__main__":
    unittest.main()
