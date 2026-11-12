"""Offline code-contract boundaries, including code-vs-document isolation."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from lab_harness import coding_contract as contract
from tests.coding_fixtures import fixture, make_profile, replace_task_record, artifact_fixture
from tests.portable_fixtures import PREVIOUS_HEAD


class CodingContractTests(unittest.TestCase):
    def test_same_contract_accepts_distinct_project_profiles(self):
        for repository, branch, actor in (("Example/alpha", "main", "Example"), ("Other/beta", "release/v1", "Other")):
            profile, activation, event, env, client = fixture(repository=repository, branch=branch, actor=actor)
            self.assertEqual(contract.validate_profile(profile), profile)
            self.assertEqual(contract.check_arm(client, profile, activation), contract.arm_record(profile, activation))
            self.assertEqual(contract.validate_event(profile, activation, event, env), event["issue"]["number"])
            issue, record, body_sha = contract.task_snapshot(client, profile, activation, event["issue"]["number"])
            self.assertEqual(record["task_id"], issue["title"])
            self.assertEqual(body_sha, contract.digest(issue["body"]))

    def test_profile_digest_is_canonical_and_changes_with_configuration(self):
        profile = make_profile()
        reordered = dict(reversed(list(profile.items())))
        self.assertEqual(contract.profile_digest(profile), contract.profile_digest(reordered))
        changed = copy.deepcopy(profile)
        changed["checks"][0]["argv"].append("--strict")
        self.assertNotEqual(contract.profile_digest(profile), contract.profile_digest(changed))
        self.assertEqual(contract.expand_paths(profile, "TASK_M0_1")[1], "Docs/Reports/TASK_M0_1.md")

    def test_profile_rejects_live_unbounded_and_cross_capability_data(self):
        for field, value in (("live_enabled", True), ("schema", "harness-project/v1"), ("runner_label", "self-hosted"),
                             ("source_repository", "relative"), ("goal", "x" * 8193), ("repository", "other?token=bad")):
            profile = make_profile()
            profile[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                contract.validate_profile(profile)
        for key in contract.LIMITS:
            profile = make_profile()
            profile["limits"][key] = 1 if profile["limits"][key] is None else profile["limits"][key] + 1
            with self.subTest(limit=key), self.assertRaises(ValueError):
                contract.validate_profile(profile)
        with self.assertRaises(ValueError):
            contract.validate_profile(make_profile() | {"token": "not allowed"})

    def test_exact_write_and_context_paths_reject_globs_traversal_and_sensitive_paths(self):
        paths = ["Source", "Source/*", "../a.cpp", "Source\\a.cpp", "Source/CON.cpp", "Source/a.cpp.",
                 ".github/workflows/run.yml", "AGENTS.md", "a/{command}.cpp", "Source/а.cpp", "Source/a.key",
                 "Docs/Reports/Harness/TASK-1.md"]
        for path in paths:
            profile = make_profile()
            profile["allowed_paths"][0] = path
            profile["checks"][0]["paths"][0] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                contract.validate_profile(profile)
        profile = make_profile()
        profile["allowed_paths"].append("source/example/widget.cpp")
        with self.assertRaises(ValueError):
            contract.validate_profile(profile)
        self.assertEqual(contract.expand_paths(make_profile(), "TASK-1", "materialize_paths")[0], "Source")

    def test_checks_are_reviewed_configuration_not_issue_commands(self):
        mutations = [lambda c: c.update(argv="fake.exe"), lambda c: c.update(argv=["{command}"]),
                     lambda c: c.update(argv=["{workspace!r}"]), lambda c: c.update(timeout_seconds=86401),
                     lambda c: c.update(success_patterns=["\x00"]), lambda c: c.update(paths=["Other.cpp"])]
        for mutate in mutations:
            profile = make_profile()
            mutate(profile["checks"][0])
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                contract.validate_profile(profile)
        profile = make_profile()
        profile["checks"][0]["paths"].pop()
        with self.assertRaisesRegex(ValueError, "Every writable"):
            contract.validate_profile(profile)

    def test_loading_rejects_duplicate_keys_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps(make_profile()), encoding="utf-8")
            self.assertEqual(contract.load_profile(path), make_profile())
            for text in ('{"schema":"one","schema":"two"}', '{"value":NaN}', "x" * 65537):
                path.write_text(text, encoding="utf-8")
                with self.subTest(text=text[:30]), self.assertRaises(ValueError):
                    contract.load_profile(path)

    def test_activation_binds_profile_and_revocable_continuous_limits(self):
        for field, value in (("schema", "harness-activation/v1"), ("max_tasks", 3), ("max_model_requests", True),
                             ("nonce", "invalid"), ("control_issue", True), ("enabled", False), ("workflow_sha", "bad")):
            profile, activation, *_ = fixture()
            activation[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                contract.validate_activation(profile, activation)
        profile, activation, *_ = fixture()
        profile["goal"] += " changed"
        with self.assertRaises(ValueError):
            contract.validate_activation(profile, activation)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        profile, activation, *_ = fixture()
        activation["issued_at"] = (now + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.assertRaises(ValueError):
            contract.validate_activation(profile, activation, now=now)
        activation["issued_at"] = "2020-01-01T00:00:00Z"
        contract.validate_activation(profile, activation, now=now)
        activation["expires_at"] = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.assertRaises(ValueError):
            contract.validate_activation(profile, activation, now=now)

    def test_arm_requires_fresh_immutable_unique_owner_approval_and_unchanged_main(self):
        for kind in ("duplicate", "edited", "revoked", "stale", "missing", "changed-main", "wrong-owner", "closed"):
            profile, activation, _, _, client = fixture()
            if kind == "duplicate": client.comments.append(copy.deepcopy(client.comments[0]))
            elif kind == "edited": client.comments[0]["updated_at"] = "2030-01-01T00:00:00Z"
            elif kind == "revoked": client.comments[0]["body"] += " revoked"
            elif kind == "stale": client.comments[0].update(created_at="2020-01-01T00:00:00Z", updated_at="2020-01-01T00:00:00Z")
            elif kind == "missing": client.comments.clear()
            elif kind == "changed-main": client.ref_sha = "e" * 40
            elif kind == "wrong-owner": client.comments[0]["user"]["login"] = "Other"
            else: client.control_issue["state"] = "closed"
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                contract.check_arm(client, profile, activation)
        profile, activation, _, _, _ = fixture()
        arm = contract.arm_record(profile, activation)
        self.assertNotIn("dsh", arm)
        self.assertNotIn("arm_comment_id", arm)
        self.assertEqual(arm["goal_sha256"], contract.digest(profile["goal"]))

    def test_event_rejects_wrong_workflow_actor_rerun_public_repo_and_issue(self):
        for key, value in (("GITHUB_RUN_ATTEMPT", "2"), ("GITHUB_EVENT_NAME", "workflow_dispatch"),
                           ("GITHUB_WORKFLOW_REF", "Example/alpha/.github/workflows/harness-documents.yml@refs/heads/main"),
                           ("RUNNER_OS", "Linux"), ("GITHUB_REPOSITORY", "Other/repo"), ("GITHUB_RUN_ID", "0")):
            profile, activation, event, env, _ = fixture()
            env[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                contract.validate_event(profile, activation, event, env)
        profile, activation, event, env, _ = fixture()
        event["issue"]["user"]["login"] = "stranger"
        with self.assertRaises(ValueError):
            contract.validate_event(profile, activation, event, env)

    def test_task_rejects_unapproved_scope_commands_and_invalid_sequence(self):
        bad = [{"number": 0}, {"number": 2**31}, {"number": True}, {"base_sha": PREVIOUS_HEAD}, {"previous_pr": 6101},
               {"argv": ["attack"]}, {"allowed_paths": ["Source"]}, {"allowed_paths": ["Other.cpp"]},
               {"task_id": "TASK-1\n"}, {"goal": "x" * 8193}, {"transition": "NEXT"},
               {"context": [{"path": "Config/Secret.ini", "start_line": 1, "end_line": 2}]},
               {"context": [{"path": "AGENTS.md", "start_line": 1, "end_line": 1201}]}]
        for change in bad:
            profile, activation, event, _, client = fixture()
            replace_task_record(client, **change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                contract.task_snapshot(client, profile, activation, event["issue"]["number"])

    def test_issue_snapshot_binds_title_author_and_body_hash(self):
        profile, activation, event, _, client = fixture()
        original = contract.digest(client.issue["body"])
        client.issue["body"] += "\nModified explanation"
        with self.assertRaisesRegex(ValueError, "changed during execution"):
            contract.task_snapshot(client, profile, activation, event["issue"]["number"], expected_body_sha=original)
        client.issue["title"] += " extra"
        with self.assertRaises(ValueError):
            contract.task_snapshot(client, profile, activation, event["issue"]["number"])

    def test_second_task_verifies_predecessor_and_next_requires_pass(self):
        for transition, state, allowed in (("NEXT", "PASS", True), ("NEXT", "FAIL", False),
                                            ("REWORK", "FAIL", True), ("REWORK", "PASS", True)):
            profile, activation, event, _, client = fixture(2)
            replace_task_record(client, transition=transition)
            verify = Mock(return_value={"check_status": state, "task_id": "TASK-M2-21"})
            fake_module = types.SimpleNamespace(verify_report_pr=verify)
            with patch.dict("sys.modules", {"lab_harness.coding_publisher": fake_module}):
                if allowed:
                    contract.task_snapshot(client, profile, activation, event["issue"]["number"])
                else:
                    with self.assertRaisesRegex(ValueError, "NEXT"):
                        contract.task_snapshot(client, profile, activation, event["issue"]["number"])
            verify.assert_called_once_with(client, profile, activation, client.previous, expected_number=1, expected_head=PREVIOUS_HEAD)
        profile, activation, event, _, client = fixture(2)
        replace_task_record(client, transition="REWORK", goal="新增文档")
        with self.assertRaisesRegex(ValueError, "explicitly"):
            contract.task_snapshot(client, profile, activation, event["issue"]["number"], verify_previous=False)

    def test_continuous_third_and_later_tasks_verify_only_immediate_predecessor(self):
        for number in (3, 100, 2**31 - 1):
            profile, activation, event, _, client = fixture(number)
            verify = Mock(return_value={"check_status": "PASS", "task_id": "TASK-PREVIOUS"})
            with patch.dict("sys.modules", {"lab_harness.coding_publisher": types.SimpleNamespace(verify_report_pr=verify)}):
                contract.task_snapshot(client, profile, activation, event["issue"]["number"])
            self.assertEqual(verify.call_args.kwargs["expected_number"], number - 1)

    def test_artifact_binds_exact_changes_and_single_provider_request(self):
        profile, activation, event, env, _ = fixture()
        artifact = artifact_fixture(profile, activation, event, env)
        self.assertEqual(contract.validate_artifact(profile, activation, artifact, env), artifact)
        for field, value in (("repository", "Other/repo"), ("model_requests", 2), ("run_attempt", True),
                             ("validated_change_set_sha256", "0" * 64), ("raw_model_output_sha256", "bad"),
                             ("check_status", "FAIL"), ("issue", True), ("usage", {"outputTokens": float("inf")})):
            changed = copy.deepcopy(artifact)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                contract.validate_artifact(profile, activation, changed, env)
        for field, value in (("path", "Other.cpp"), ("content", "changed"), ("operation", "delete"),
                             ("before_sha256", None), ("content", "\x00"), ("content", "x" * (4 * 1024 * 1024 + 1))):
            changed = copy.deepcopy(artifact)
            changed["changes"][0][field] = value
            changed["validated_change_set_sha256"] = contract.canonical_digest(changed["changes"])
            with self.subTest(field=field), self.assertRaises(ValueError):
                contract.validate_artifact(profile, activation, changed, env)

    def test_check_evidence_cannot_skip_triggered_check_or_forge_pass(self):
        profile, activation, event, env, _ = fixture()
        artifact = artifact_fixture(profile, activation, event, env)
        for field, value in (("status", "NOT_REQUIRED"), ("exit_code", 1), ("timed_out", True),
                             ("output_limit_exceeded", True), ("stdout_sha256", None),
                             ("duration_seconds", float("nan")), ("id", "other")):
            changed = copy.deepcopy(artifact)
            changed["checks"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                contract.validate_artifact(profile, activation, changed, env)
        failed = copy.deepcopy(artifact)
        failed["checks"][0].update(status="FAIL", exit_code=1, reason="check failed")
        failed["check_status"] = "FAIL"
        self.assertEqual(contract.validate_artifact(profile, activation, failed, env)["check_status"], "FAIL")
        with self.assertRaises(ValueError):
            contract.validate_artifact(profile, activation, artifact | {"checks": []}, env)

    def test_trusted_report_path_is_unique_per_activation_sequence_and_not_model_writable(self):
        profile, activation, event, env, _ = fixture()
        path = contract.report_path(activation, 3)
        self.assertTrue(path.endswith(activation["nonce"] + "-3.md"))
        with self.assertRaises(ValueError):
            contract.validate_path(path, writable=True)


if __name__ == "__main__":
    unittest.main()
