"""Two real engine/publisher rounds using only synthetic providers and GitHub.

The protocol gates, filesystem claims, publication journal, commit verification,
and predecessor acceptance are the production implementations, not patched mocks.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab_harness.portable_engine import generate_task
from lab_harness.portable_policy import task_snapshot
from lab_harness.portable_publisher import publish_task, verify_report_pr
from tests.portable_fixtures import make_generator
from tests.test_portable_publisher import BASE, fixture, task_artifact


class PortableIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile, self.activation, self.client = fixture("Example/integration-project")
        self.activation["dsh"] = {"fixture_only": True}
        self.generators = []
        self.addCleanup(patch.stopall)
        patch("subprocess.Popen", side_effect=AssertionError("No process allowed in integration fixture")).start()
        patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No network allowed in integration fixture")).start()

    def prepare_task(self, number, previous=None):
        # Reuse the publisher's in-memory Issue and workflow-run records, then
        # discard its canned artifact: generate_task must produce the real one.
        fixture_artifact, env = task_artifact(self.client, number, previous)
        env["RUNNER_OS"] = "Windows"
        event = {"action": "opened", "sender": {"login": self.profile["trusted_actor"]},
                 "repository": {"full_name": self.profile["repository"], "private": True},
                 "issue": copy.deepcopy(self.client.issues[fixture_artifact["issue"]])}
        generator = make_generator(self.profile, number)
        fixture_generate = generator.side_effect

        def synthetic_provider(*args):
            result = fixture_generate(*args)
            result["session_id"] = f"offline-integration-session-{number}"
            return result

        generator.side_effect = synthetic_provider
        self.generators.append(generator)
        return event, env, generator

    def complete_round(self, number, previous=None):
        event, env, generator = self.prepare_task(number, previous)
        artifact, artifact_path = generate_task(
            self.profile, self.activation, event, env, root=self.root / "executor",
            client=self.client, generate=generator)
        self.assertEqual(json.loads(artifact_path.read_text(encoding="utf-8")), artifact)
        result = publish_task(self.client, self.profile, self.activation, artifact, env,
                              journal_root=self.root / "publisher")
        self.client.runs[artifact["run_id"]].update(status="completed", conclusion="success")
        report = verify_report_pr(self.client, self.profile, self.activation, result["pr"],
                                  expected_number=number, expected_head=result["head_sha"])
        generator.assert_called_once()
        self.assertEqual(report["session_id"], artifact["session_id"])
        self.assertEqual(report["document_sha256"], artifact["document_sha256"])
        self.assertEqual(report["checks"], "PASS")
        return artifact, artifact_path, result

    def test_two_rounds_generate_publish_and_accept_through_actual_interfaces(self):
        first_artifact, first_path, first = self.complete_round(1)
        second_artifact, second_path, second = self.complete_round(2, first)
        _, second_task, _ = task_snapshot(self.client, self.profile, self.activation,
                                          second_artifact["issue"],
                                          expected_body_sha=second_artifact["issue_body_sha256"])
        self.assertEqual(second_task["previous_pr"], first["pr"])
        self.assertEqual(second_task["previous_head"], first["head_sha"])
        self.assertEqual(sum(generator.call_count for generator in self.generators), 2)
        self.assertNotEqual(first_artifact["session_id"], second_artifact["session_id"])
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(len(self.client.prs), 2)
        self.assertEqual(self.client.refs["main"], BASE)
        for artifact, result in ((first_artifact, first), (second_artifact, second)):
            comparison = self.client.request("GET", "/compare/" + BASE + "..." + result["head_sha"])
            self.assertEqual(comparison["total_commits"], 2)
            self.assertEqual({entry["filename"] for entry in comparison["files"]},
                             {artifact["document_path"], artifact["report_path"]})
            verified = verify_report_pr(self.client, self.profile, self.activation, result["pr"],
                                        expected_number=artifact["task_number"], expected_head=result["head_sha"])
            self.assertEqual(verified["artifact_sha256"], result["artifact_sha256"])
        claims = [json.loads(path.read_text(encoding="utf-8")) for path in self.root.rglob("claim.json")]
        self.assertEqual(len(claims), 2)
        self.assertTrue(all(claim["state"] == "GENERATED" for claim in claims))
        self.assertFalse(list(self.root.rglob("lease.json")))

    def reject_changed_first_report(self, field):
        _, _, first = self.complete_round(1)
        event, env, generator = self.prepare_task(2, first)
        if field == "head":
            self.client.prs[first["pr"]]["head"]["sha"] = "f" * 40
        else:
            self.client.prs[first["pr"]]["body"] += "\nEdited after the first acceptance"
        before_writes = list(self.client.writes)
        with self.assertRaises(ValueError):
            generate_task(self.profile, self.activation, event, env, root=self.root / "executor",
                          client=self.client, generate=generator)
        generator.assert_not_called()
        self.assertEqual(sum(item.call_count for item in self.generators), 1)
        self.assertEqual(self.client.writes, before_writes)
        self.assertEqual(len(list(self.root.rglob("claim.json"))), 1)
        self.assertEqual(len(self.client.prs), 1)

    def test_changed_first_head_stops_second_round_before_model(self):
        self.reject_changed_first_report("head")

    def test_edited_first_report_stops_second_round_before_model(self):
        self.reject_changed_first_report("body")


if __name__ == "__main__":
    unittest.main()
