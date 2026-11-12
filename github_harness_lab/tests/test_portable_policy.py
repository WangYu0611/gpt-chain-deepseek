"""Offline authorization, event and evidence invariants for the portable engine."""
import copy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from lab_harness.portable_policy import (
    ARM_MARKER, TASK_MARKER, arm_record, check_arm, digest, parse_record,
    task_snapshot, validate_activation, validate_artifact, validate_event,
)
from tests.portable_fixtures import (
    PREVIOUS_HEAD, artifact_fixture, fixture, replace_task_record,
)


class PortablePolicyTests(unittest.TestCase):
    def test_two_repositories_accept_their_own_fresh_bindings(self):
        for repository, branch, actor in (("Example/alpha", "main", "Example"), ("Other/beta", "release/v1", "Other")):
            profile, activation, event, env, client = fixture(repository=repository, branch=branch, actor=actor)
            self.assertEqual(check_arm(client, profile, activation), arm_record(profile, activation))
            self.assertEqual(validate_event(profile, activation, event, env), event["issue"]["number"])
            issue, record, body_sha = task_snapshot(client, profile, activation, event["issue"]["number"])
            self.assertEqual(record["task_id"], issue["title"])
            self.assertEqual(body_sha, digest(issue["body"]))

    def test_changed_profile_repository_and_activation_limits_fail_closed(self):
        for name, value in (("enabled", False), ("repository", "Other/beta"), ("profile_sha256", "0" * 64),
                            ("workflow_sha", "bad"), ("controller_sha256", "bad"), ("arm_body_sha256", "bad"),
                            ("control_issue", True), ("arm_comment_id", 0), ("nonce", "not-a-uuid"),
                            ("runner_label", "self-hosted"), ("max_model_requests", True), ("max_model_requests", 3)):
            profile, activation, *_ = fixture()
            activation[name] = value
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                validate_activation(profile, activation)
        profile, activation, *_ = fixture()
        profile["document_tasks"][0]["prompt"] += "调整需求"
        with self.assertRaisesRegex(ValueError, "different project profile"):
            validate_activation(profile, activation)

    def test_expired_future_and_oversized_windows_are_rejected(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        for start, end in ((now - timedelta(hours=1), now), (now + timedelta(seconds=1), now + timedelta(minutes=30)),
                           (now - timedelta(minutes=1), now + timedelta(minutes=90))):
            profile, activation, *_ = fixture()
            activation.update(issued_at=start.strftime("%Y-%m-%dT%H:%M:%SZ"), expires_at=end.strftime("%Y-%m-%dT%H:%M:%SZ"))
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                validate_activation(profile, activation, now=now)

    def test_owner_arm_must_be_unique_immutable_and_not_revoked(self):
        for kind in ("revoked", "edited", "duplicate", "extra-owner-record", "missing", "foreign", "changed-branch", "closed-control", "wrong-owner"):
            profile, activation, _, _, client = fixture()
            if kind == "revoked":
                record = arm_record(profile, activation) | {"authorized": False}
                client.comments[0]["body"] = ARM_MARKER + json.dumps(record)
            elif kind == "edited":
                client.comments[0]["updated_at"] = activation["expires_at"]
            elif kind == "duplicate":
                client.comments.append(copy.deepcopy(client.comments[0]))
            elif kind == "extra-owner-record":
                extra = copy.deepcopy(client.comments[0])
                extra["id"] += 1
                client.comments.append(extra)
            elif kind == "missing":
                client.comments.clear()
            elif kind == "foreign":
                client.repository = "Other/beta"
            elif kind == "changed-branch":
                client.ref_sha = "d" * 40
            elif kind == "closed-control":
                client.control_issue["state"] = "closed"
            else:
                client.control_issue["user"]["login"] = "stranger"
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                check_arm(client, profile, activation)

    def test_another_live_nonce_conflicts_but_expired_history_does_not(self):
        for expired in (False, True):
            profile, activation, _, _, client = fixture()
            extra = copy.deepcopy(client.comments[0])
            extra["id"] += 1
            record = arm_record(profile, activation) | {"nonce": "22345678-1234-4234-8234-123456789abc"}
            if expired:
                record["expires_at"] = "2020-01-01T00:00:00Z"
            extra["body"] = ARM_MARKER + json.dumps(record)
            client.comments.append(extra)
            if expired:
                check_arm(client, profile, activation)
            else:
                with self.assertRaisesRegex(ValueError, "conflicts"):
                    check_arm(client, profile, activation)

    def test_event_requires_exact_repo_actor_job_identity_and_workflow(self):
        bad_env = {"GITHUB_REPOSITORY": "Other/beta", "GITHUB_ACTIONS": "false", "GITHUB_EVENT_NAME": "workflow_dispatch",
                   "GITHUB_ACTOR": "stranger", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_RUN_ID": "0",
                   "GITHUB_WORKFLOW_SHA": "d" * 40, "RUNNER_OS": "Linux", "HARNESS_ISSUE": "9999",
                   "GITHUB_WORKFLOW_REF": "Example/alpha/.github/workflows/another.yml@refs/heads/main"}
        for key, value in bad_env.items():
            profile, activation, event, env, _ = fixture()
            env[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_event(profile, activation, event, env)
        for kind in ("action", "sender", "public", "repository", "number"):
            profile, activation, event, env, _ = fixture()
            if kind == "action": event["action"] = "edited"
            elif kind == "sender": event["sender"]["login"] = "stranger"
            elif kind == "public": event["repository"]["private"] = False
            elif kind == "repository": event["repository"]["full_name"] = "Other/beta"
            else: event["issue"]["number"] = True
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                validate_event(profile, activation, event, env)

    def test_third_task_and_changed_task_scope_are_rejected(self):
        profile, activation, event, _, client = fixture(3)
        with self.assertRaises(ValueError):
            task_snapshot(client, profile, activation, event["issue"]["number"])
        for changes in ({"dispatch": False}, {"dispatch": 1}, {"repository": "Other/beta"},
                        {"profile_sha256": "0" * 64}, {"document_path": "Other.md"}, {"report_path": "Other.md"},
                        {"previous_pr": 6101}, {"number": True}, {"extra_instruction": "ignored?"}):
            profile, activation, event, _, client = fixture()
            replace_task_record(client, **changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                task_snapshot(client, profile, activation, event["issue"]["number"])

    def test_second_task_calls_actual_predecessor_verifier(self):
        profile, activation, event, _, client = fixture(2)
        with patch("lab_harness.portable_publisher.verify_report_pr") as verify:
            task_snapshot(client, profile, activation, event["issue"]["number"])
        verify.assert_called_once_with(client, profile, activation, client.previous,
                                       expected_number=1, expected_head=PREVIOUS_HEAD)
        with patch("lab_harness.portable_publisher.verify_report_pr", side_effect=ValueError("bad predecessor")):
            with self.assertRaisesRegex(ValueError, "bad predecessor"):
                task_snapshot(client, profile, activation, event["issue"]["number"])

    def test_task_body_digest_binds_changes_even_when_json_remains_valid(self):
        profile, activation, event, _, client = fixture()
        original = digest(client.issue["body"])
        client.issue["body"] += "\nnew prose"
        with self.assertRaisesRegex(ValueError, "changed during execution"):
            task_snapshot(client, profile, activation, event["issue"]["number"], expected_body_sha=original)

    def test_machine_records_reject_duplicates_nonobjects_oversize_and_invalid_json(self):
        bad = ["", TASK_MARKER + "[]", TASK_MARKER + '{"a":1,"a":2}', TASK_MARKER + '{"a":NaN}',
               TASK_MARKER + "{}" + TASK_MARKER + "{}", TASK_MARKER + "{" + "x" * 32768]
        for body in bad:
            with self.subTest(body=body[:40]), self.assertRaises(ValueError):
                parse_record(body, TASK_MARKER)

    def test_artifact_validates_bindings_scope_document_and_machine_evidence(self):
        profile, activation, event, env, _ = fixture()
        artifact = artifact_fixture(profile, activation, event, env)
        self.assertEqual(validate_artifact(profile, activation, artifact, env), artifact["document"])
        for field, value in (("repository", "Other/beta"), ("profile_sha256", "0" * 64),
                             ("document_sha256", "0" * 64), ("document_path", "Other.md"),
                             ("report_path", "Other.md"), ("run_attempt", True), ("issue", True),
                             ("model_requests", 2), ("exit_code", True), ("session_id", ""),
                             ("guard", {"request_count": 1, "visible_tool_count": 1, "violations": []})):
            changed = copy.deepcopy(artifact)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_artifact(profile, activation, changed, env)
        with self.assertRaises(ValueError):
            validate_artifact(profile, activation, artifact | {"document": artifact["document"] + "\n"}, env)


if __name__ == "__main__":
    unittest.main()
