"""Explicitly simulated orchestration, with actual local Git commits and test processes."""
from __future__ import annotations

import copy
import json
import sys
import uuid
from pathlib import Path

from .evidence import write_json
from .gitops import git
from .policy import PolicyError, check_changes
from .state import StateStore
from .worker import Worker


def example_task() -> dict:
    return {"task_id": "LAB-001", "depends_on": [], "allowed_paths": ["src"], "forbidden_paths": ["secrets"], "requirements": ["Implement add(a, b) correctly"], "acceptance": [{"id": "AC-1", "criterion": "add(2,3) returns 5"}], "evidence": [{"id": "EV-1", "requirement": "Actual unittest exit code zero at result commit"}], "test_ids": ["unit"], "risk": "Offline fixture only", "rollback": "Discard the dedicated local clone"}


def example_event(task: dict | None = None, number: int = 1) -> dict:
    return {"action": "labeled", "label": {"name": "agent:ready"}, "repository": {"full_name": "OWNER/REPOSITORY"}, "sender": {"login": "trusted-planner"}, "issue": {"number": number, "state": "open", "user": {"login": "trusted-planner"}, "labels": [{"name": "agent:ready"}], "body": "Offline demo task\n\n```agent-task\n" + json.dumps(task or example_task(), ensure_ascii=False, indent=2) + "\n```"}}


class FixtureHarness:
    offline_fixture = True

    def __init__(self, content: str = "def add(a, b):\n    return a + b\n"):
        self.content = content
        self.calls = 0

    def run(self, prompt, cwd, output_dir, session_id=None):
        self.calls += 1
        (cwd / "src" / "calc.py").write_text(self.content, encoding="utf-8")
        return {"session_id": "offline-fixture-" + str(self.calls), "model": "offline-fixture", "version": "1", "exit_code": 0, "final_kind": "completed"}


def create_source(path: Path):
    path.mkdir(parents=True)
    (path / "src").mkdir()
    (path / "tests").mkdir()
    (path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (path / "src" / "calc.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    (path / "tests" / "test_calc.py").write_text("import unittest\nfrom src.calc import add\nclass TestCalc(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n        self.assertEqual(add(-2, 2), 0)\n", encoding="utf-8")
    (path / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    git(path, "init", "-b", "main")
    git(path, "add", ".")
    git(path, "commit", "-m", "Offline fixture baseline")


def run_demo(output: Path, scenario: str = "all") -> dict:
    root = output.resolve() / ("demo-" + uuid.uuid4().hex[:12])
    source = root / "source"
    create_source(source)
    config = {"version": 1, "repository": "OWNER/REPOSITORY", "trusted_actors": ["trusted-planner"], "source_repository": str(source), "runtime_dir": str(root / "runtime"), "live_enabled": False, "limits": {"run_timeout_seconds": 120, "max_budget_usd": 1}, "test_commands": [{"id": "unit", "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], "timeout_seconds": 30}]}
    worker = Worker(config)
    results = {}
    selected = set("ABCDEF") if scenario == "all" else {scenario}
    if selected & {"A", "C"}:
        harness = FixtureHarness()
        manifest = worker.run(example_event(), harness, offline_demo=True)
        assert manifest["status"] == "REVIEW", manifest
        worker.store.accept(1, "fixture-accept-1", manifest["result_commit_sha"])
        if "A" in selected:
            results["A"] = {"result": "PASS_LOCAL_ONLY", "evidence_status": manifest["status"], "commit": manifest["result_commit_sha"], "real_github_deepseek_chatgpt": "NOT_RUN"}
        if "C" in selected:
            duplicate = worker.run(example_event(), harness, offline_demo=True)
            assert duplicate["status"] == "SKIPPED" and harness.calls == 1
            results["C"] = {"result": "PASS_LOCAL_ONLY", "harness_calls": harness.calls, "duplicate": duplicate["status"]}
    if "B" in selected:
        first_event = example_event(number=2)
        first = worker.run(first_event, FixtureHarness("def add(a,b):\n    return a + b + 0\n"), offline_demo=True)
        assert first["status"] == "REVIEW"
        second_event = copy.deepcopy(first_event)
        second_event["label"]["name"] = "agent:rework"
        second_event["issue"]["labels"] = [{"name": "agent:rework"}]
        second_event["issue"]["body"] += "\nFixture reviewer requires a smaller implementation."
        from .policy import canonical_digest
        worker.store.request_rework(2, canonical_digest(second_event), first["result_commit_sha"])
        second = worker.run(second_event, FixtureHarness(), offline_demo=True)
        assert second["status"] == "REVIEW" and second["result_commit_sha"] != first["result_commit_sha"]
        worker.store.accept(2, "fixture-accept-2", second["result_commit_sha"])
        results["B"] = {"result": "PASS_LOCAL_ONLY", "reworks": worker.store.snapshot(2)["reworks"], "reviewer": "fixture, not ChatGPT", "new_commit": second["result_commit_sha"]}
    if "D" in selected:
        # Simulate a killed controller with an expired lease and persisted checkpoint.
        store = StateStore(root / "interruption.sqlite3")
        old = store.claim(4, "event-D", now=0, lease_seconds=10)
        store.checkpoint(4, old["owner"], {"stage": "harness_completed", "session_id": "fixture-D"}, now=1)
        recovered = StateStore(root / "interruption.sqlite3").claim(4, "event-D", now=11)
        assert old["attempt"] == recovered["attempt"] and old["owner"] != recovered["owner"] and recovered["result"]["session_id"] == "fixture-D"
        results["D"] = {"result": "PASS_LOCAL_ONLY", "same_attempt": recovered["attempt"], "process_kill_integration": "NOT_RUN; lease/checkpoint simulation only"}
    if "E" in selected:
        store = StateStore(root / "fuse.sqlite3")
        for n in range(3):
            claimed = store.claim(5, "fail-" + str(n))
            store.finish(5, claimed["owner"], "FAILED")
        assert store.snapshot(5)["status"] == "BLOCKED" and store.claim(5, "fail-4") is None
        results["E"] = {"result": "PASS_LOCAL_ONLY", "status": "BLOCKED", "failures": 3}
    if "F" in selected:
        prevented = []
        for file in (".github/workflows/evil.yml", "secrets/.env", "../escape", "src/.env"):
            try:
                check_changes(source, [file], example_task())
            except PolicyError:
                prevented.append(file)
        assert len(prevented) == 4
        results["F"] = {"result": "PASS_POLICY_ONLY", "prevented_paths": prevented, "os_isolation_secret_exfiltration_branch_protection": "NOT_VERIFIED"}
    summary = {"mode": "offline-demo", "external_sends": 0, "root": str(root), "scenarios": results, "true_end_to_end": "NOT_RUN: no real Issue, DSH, PR, CI or ChatGPT acceptance", "state": worker.store.export_snapshot()}
    write_json(root / "summary.json", summary)
    return summary
