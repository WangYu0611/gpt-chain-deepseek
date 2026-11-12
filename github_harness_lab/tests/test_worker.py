import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from lab_harness import gitops
from lab_harness.demo import FixtureHarness, create_source, example_event
from lab_harness.evidence import sha256, validate_manifest
from lab_harness.policy import PolicyError
from lab_harness.worker import Worker


class SimulatedPowerLoss(BaseException):
    pass


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        create_source(self.source)
        self.config = {"repository": "OWNER/REPOSITORY", "trusted_actors": ["trusted-planner"], "source_repository": str(self.source), "runtime_dir": str(self.root / "runtime"), "live_enabled": False, "limits": {"run_timeout_seconds": 60, "max_budget_usd": 1}, "test_commands": [{"id": "unit", "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests"], "timeout_seconds": 10}]}
        self.worker = Worker(self.config)
        self.event = example_event()

    def test_real_local_tests_commit_and_evidence_match(self):
        manifest = self.worker.run(self.event, FixtureHarness(), offline_demo=True)
        validate_manifest(manifest)
        self.assertEqual(manifest["status"], "REVIEW")
        self.assertEqual(manifest["mode"], "offline-demo")
        self.assertIsNone(manifest["pr"])
        self.assertEqual(manifest["ci"]["status"], "NOT_RUN")
        run = self.root / "runtime/runs/issue-1-attempt-1"
        self.assertEqual(gitops.git(run / "workspace", "rev-parse", "HEAD"), manifest["result_commit_sha"])
        self.assertEqual(gitops.git(run / "workspace", "remote"), "")
        outbox = json.loads((run / "outbox.json").read_text())
        self.assertFalse(outbox["sent"])
        self.assertEqual(outbox["evidence_sha256"], sha256(run / "evidence.json"))
        for artifact in manifest["artifacts"]:
            self.assertEqual(artifact["sha256"], sha256(run / artifact["path"]))

    def test_prose_does_not_override_real_test_failure(self):
        manifest = self.worker.run(self.event, FixtureHarness("def add(a,b):\n    return -999\n"), offline_demo=True)
        self.assertEqual(manifest["status"], "FAILED")
        self.assertNotEqual(manifest["tests"][0]["exit_code"], 0)
        self.assertIsNone(manifest["result_commit_sha"])
        self.assertFalse(list((self.root / "runtime").rglob("outbox.json")))

    def test_structured_harness_failure_cannot_create_review(self):
        class FailedHarness(FixtureHarness):
            def run(self, *args, **kwargs):
                return super().run(*args, **kwargs) | {"final_kind": "tool-error"}
        result = self.worker.run(self.event, FailedHarness(), offline_demo=True)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["tests"])

    def test_duplicate_event_does_not_run_harness_or_tests_again(self):
        harness = FixtureHarness()
        first = self.worker.run(self.event, harness, offline_demo=True)
        second = Worker(self.config).run(self.event, harness, offline_demo=True)
        self.assertEqual(first["status"], "REVIEW")
        self.assertEqual(second["status"], "SKIPPED")
        self.assertEqual(harness.calls, 1)

    def test_repository_change_cannot_reuse_ledger(self):
        with self.assertRaises(PolicyError):
            Worker(self.config | {"repository": "OTHER/REPO"})

    def test_unaccepted_dependency_does_not_claim(self):
        event = copy.deepcopy(self.event)
        task = json.loads(event["issue"]["body"].split("```agent-task\n")[1].split("\n```")[0])
        task["depends_on"] = [99]
        event = example_event(task)
        with self.assertRaises(PolicyError):
            self.worker.run(event, FixtureHarness(), offline_demo=True)
        self.assertIsNone(self.worker.store.snapshot(1))

    def test_test_mutating_candidate_is_not_committed(self):
        self.config["test_commands"][0]["argv"] = [sys.executable, "-c", "from pathlib import Path; Path('src/calc.py').write_text('changed by test')"]
        result = self.worker.run(self.event, FixtureHarness(), offline_demo=True)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(any("tests changed" in x for x in result["blocked_checks"]))

    def test_timeout_cannot_succeed_even_if_windows_exit_code_is_zero(self):
        self.config["test_commands"][0].update(argv=[sys.executable, "-c", "import time; time.sleep(10)"], timeout_seconds=0.1)
        result = self.worker.run(self.event, FixtureHarness(), offline_demo=True)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(result["tests"][0]["timed_out"])

    def test_commit_crash_recovers_same_attempt_without_repeating_agent(self):
        harness = FixtureHarness()
        real_commit = gitops.commit
        commits = []

        def power_loss(*args, **kwargs):
            commits.append(real_commit(*args, **kwargs))
            raise SimulatedPowerLoss()

        with patch("lab_harness.worker.gitops.commit", side_effect=power_loss):
            with self.assertRaises(SimulatedPowerLoss):
                self.worker.run(self.event, harness, offline_demo=True)
        self.assertEqual(self.worker.store.snapshot(1)["status"], "RUNNING")
        # Explicit test-only time advance: simulate lease expiration following a dead process.
        with closing(sqlite3.connect(self.worker.store.path)) as db, db:
            db.execute("UPDATE tasks SET lease_until=0 WHERE issue='1'")
        result = Worker(self.config).run(self.event, harness, offline_demo=True)
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["result_commit_sha"], commits[0])
        self.assertEqual(self.worker.store.snapshot(1)["attempt"], 1)
        self.assertEqual(harness.calls, 1)
        clone = self.root / "runtime/runs/issue-1-attempt-1/workspace"
        self.assertEqual(gitops.git(clone, "rev-list", "--count", "HEAD"), "2")

    def test_modified_candidate_after_crash_requires_new_tests(self):
        harness = FixtureHarness()
        real_commit = gitops.commit

        def power_loss(*args, **kwargs):
            real_commit(*args, **kwargs)
            raise SimulatedPowerLoss()

        with patch("lab_harness.worker.gitops.commit", side_effect=power_loss):
            with self.assertRaises(SimulatedPowerLoss):
                self.worker.run(self.event, harness, offline_demo=True)
        clone = self.root / "runtime/runs/issue-1-attempt-1/workspace"
        (clone / "src/calc.py").write_text("def add(a,b): return 123\n")
        with closing(sqlite3.connect(self.worker.store.path)) as db, db:
            db.execute("UPDATE tasks SET lease_until=0 WHERE issue='1'")
        result = Worker(self.config).run(self.event, harness, offline_demo=True)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(any("changed since" in x for x in result["blocked_checks"]))

    def test_paused_store_does_not_call_harness(self):
        self.worker.store.pause()
        harness = FixtureHarness()
        self.assertEqual(self.worker.run(self.event, harness, offline_demo=True)["status"], "SKIPPED")
        self.assertEqual(harness.calls, 0)

    def test_incremental_rework_keeps_previous_changes_and_commit_parent(self):
        first = self.worker.run(self.event, FixtureHarness(), offline_demo=True)
        from lab_harness.policy import canonical_digest
        event = copy.deepcopy(self.event)
        event["label"]["name"] = "agent:rework"
        event["issue"]["labels"] = [{"name": "agent:rework"}]
        self.worker.store.request_rework(1, canonical_digest(event), first["result_commit_sha"])

        class IncrementalHarness(FixtureHarness):
            def run(self, prompt, cwd, output_dir, session_id=None):
                self.calls += 1
                (cwd / "src/notes.txt").write_text("incremental addition\n")
                return {"session_id": "offline-incremental", "model": "offline-fixture", "version": "1", "exit_code": 0, "final_kind": "completed"}

        result = self.worker.run(event, IncrementalHarness(), offline_demo=True)
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["base_commit_sha"], first["result_commit_sha"])
        clone = self.root / "runtime/runs/issue-1-attempt-2/workspace"
        self.assertIn("return a + b", (clone / "src/calc.py").read_text())
        self.assertEqual(gitops.git(clone, "rev-parse", "HEAD^"), first["result_commit_sha"])
        self.assertEqual(result["changed_files"], ["src/notes.txt"])


if __name__ == "__main__":
    unittest.main()
