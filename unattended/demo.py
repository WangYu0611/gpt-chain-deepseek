"""Inspectable offline demo: fake agents, real Git, real checks, and recovery.

No Codex/DSH process, credential, remote repository, or network is used. This
exercise validates the local orchestration contract, not real-model capability.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys

from .calendar import BEIJING
from .config import load_config
from .engine import Engine
from . import gitops
from .storage import read_json, write_json, write_text


class _DemoCrash(BaseException):
    """Simulate process loss after Git commit but before provider/state flush."""


_ADD_INITIAL = "def add(a, b):\n    return a + b\n"
_ADD_CHECKED = (
    "def add(a, b):\n"
    "    if type(a) not in (int, float) or type(b) not in (int, float):\n"
    "        raise TypeError('numeric arguments required')\n"
    "    return a + b\n"
)
_MULTIPLY = (
    "\n\ndef multiply(a, b):\n"
    "    if type(a) not in (int, float) or type(b) not in (int, float):\n"
    "        raise TypeError('numeric arguments required')\n"
    "    return a * b\n"
)
_BEHAVIOR_CHECK = '''"""Independent behavior checks for the offline orchestration demo."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("candidate", Path.cwd() / "calculator.py")
calculator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(calculator)


class BehaviorTests(unittest.TestCase):
    def test_add_handles_positive_negative_zero_and_fractional_numbers(self):
        for a, b, expected in [(2, 3, 5), (-4, 7, 3), (0, 0, 0), (1.25, 2.5, 3.75)]:
            with self.subTest(a=a, b=b):
                self.assertEqual(calculator.add(a, b), expected)

    def test_add_rejects_non_numeric_arguments(self):
        for a, b in [("2", "3"), ([], []), (None, 2), (True, 2)]:
            with self.subTest(a=a, b=b), self.assertRaises(TypeError):
                calculator.add(a, b)

    def test_multiply_when_the_second_feature_has_been_added(self):
        if not hasattr(calculator, "multiply"):
            self.skipTest("multiply belongs to the second task")
        for a, b, expected in [(2, 3, 6), (-4, 7, -28), (0, 9, 0), (1.5, 2, 3.0)]:
            with self.subTest(a=a, b=b):
                self.assertEqual(calculator.multiply(a, b), expected)
        for a, b in [("2", 3), (None, 2), (True, 2)]:
            with self.subTest(a=a, b=b), self.assertRaises(TypeError):
                calculator.multiply(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
'''


class _DemoProviders:
    """Persist fake-agent call counts separately from the engine checkpoint."""

    def __init__(self, runtime: Path, stats_path: Path):
        self.runtime, self.stats_path = runtime, stats_path

    def _state(self):
        return read_json(self.runtime / "state.json")

    def _record(self, kind: str, out: Path):
        stats = read_json(self.stats_path) if self.stats_path.exists() else {"plan": 0, "code": 0, "review": 0}
        stats[kind] += 1
        write_json(self.stats_path, stats)
        out.mkdir(parents=True, exist_ok=True)
        write_text(out / "fake-agent.txt", "Offline fixture; no model was called.\n")
        return stats

    @staticmethod
    def _save(out: Path, result: dict):
        write_json(out / "fake-result.json", result)
        return result

    def recover(self, kind: str, out: Path):
        path = out / "fake-result.json"
        return read_json(path) if path.exists() else None

    def plan(self, prompt: str, cwd: Path, out: Path, timeout: float):
        self._record("plan", out)
        accepted = len(self._state()["accepted"])
        if accepted >= 2:
            return self._save(out, {"action": "complete", "title": "", "body": "",
                                    "acceptance_criteria": [],
                                    "reason": "Both numeric operations passed independent behavior checks."})
        feature = "add" if accepted == 0 else "multiply"
        return self._save(out, {
            "action": "task", "title": "Implement numeric " + feature,
            "body": "Implement " + feature + "(a, b) in calculator.py. Support int and float arguments, "
                    "reject strings, containers, None and bool using TypeError; commit using the assigned task ID.",
            "acceptance_criteria": ["Positive, negative, zero and fractional numbers have correct results.",
                                    "Invalid argument types raise TypeError.",
                                    "Independent behavior command passes; all changes are committed."],
            "reason": "Next bounded feature in the offline fixture.",
        })

    def code(self, prompt: str, cwd: Path, out: Path, timeout: float, *, session_id=None):
        stats = self._record("code", out)
        state = self._state()
        task = state["task"]
        first_task = len(state["accepted"]) == 0
        source = _ADD_INITIAL if first_task and task["reworks"] == 0 else _ADD_CHECKED
        if not first_task:
            source += _MULTIPLY
        write_text(cwd / "calculator.py", source)
        gitops.git(cwd, "add", "--", "calculator.py")
        gitops.git(cwd, "commit", "-m", task["id"])
        if stats["code"] == 1:
            # No result receipt is written: the restarted Engine must reconcile
            # the actual task commit, rather than invoke this fake coder again.
            raise _DemoCrash("Simulated process loss immediately after the first code commit")
        return self._save(out, {
            "completed": True, "session_id": session_id or "offline-demo-session-" + task["id"],
            "text": "Offline fake coder committed " + gitops.head(cwd) + ". Independent checks follow.",
        })

    def review(self, prompt: str, cwd: Path, out: Path, timeout: float):
        self._record("review", out)
        task = self._state()["task"]
        passed = bool(task.get("check_results")) and all(item["passed"] for item in task["check_results"])
        decision = "accepted" if passed else "rework"
        return self._save(out, {
            "decision": decision, "head_sha": gitops.head(cwd),
            "summary": "Behavior checks passed." if passed else "The independent behavior test found missing input validation.",
            "feedback": "" if passed else "Reject string, container, None and bool arguments; retain correct numeric behavior.",
        })


def run_demo(output: Path) -> dict:
    """Create and run a fresh, wholly local two-task recovery demonstration."""
    output = Path(output).expanduser()
    if output.is_symlink():
        raise ValueError("Demo output must not be a symbolic link")
    output = output.resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Demo output must be an empty or nonexistent directory; existing files are preserved")
    output.mkdir(parents=True, exist_ok=True)
    # Exclusive ownership also prevents concurrent demos from sharing one target.
    with (output / ".demo-owner").open("x", encoding="utf-8") as owner:
        owner.write("Offline orchestration demo; this directory is owned by this run.\n")
    project, runtime = output / "repo", output / "runtime"
    project.mkdir()
    gitops.git(project, "init", "--initial-branch=main")
    gitops.git(project, "config", "user.name", "Offline Harness Demo")
    gitops.git(project, "config", "user.email", "offline-demo@example.invalid")
    write_text(project / ".gitignore", "__pycache__/\n*.pyc\n")
    write_text(project / "README.md", "# Offline fixture\n\nAdd two numeric operations through serial task handoffs.\n")
    write_text(project / "calculator.py", "def add(a, b):\n    raise NotImplementedError('first task')\n")
    gitops.git(project, "add", ".")
    gitops.git(project, "commit", "-m", "Initial offline fixture")
    initial_head = gitops.head(project)
    check_path = output / "verify_behavior.py"
    write_text(check_path, _BEHAVIOR_CHECK)
    calendar = output / "calendar.json"
    write_json(calendar, {"years": [2026], "holidays": [], "workdays": [], "overrides": {},
                          "notes": "Offline demo coverage only; the fixed demo date is a normal weekday."})
    config_path = output / "config.json"
    write_json(config_path, {
        "version": 1, "project_path": str(project), "runtime_dir": str(runtime),
        "goal": "Implement add and multiply with strict numeric argument handling; pass independent behavior checks.",
        "branch": "unattended/offline-demo", "base_ref": "HEAD", "task_prefix": "TASK-DEMO",
        "schedule": {"timezone": "Asia/Shanghai", "weekday_start": "18:00", "weekday_end": "08:00",
                     "weekends_all_day": True, "holidays_file": str(calendar)},
        "checks": [{"id": "behavior", "argv": [sys.executable, str(check_path)], "timeout_seconds": 30}],
        "close_policy": "finish_step", "step_timeout_seconds": 30, "max_reworks": 3, "max_failures": 3,
        "poll_seconds": 1, "push": False, "remote": "origin", "codex": {}, "dsh": {},
    })
    config = load_config(config_path)
    now = lambda: datetime(2026, 11, 2, 19, 0, tzinfo=BEIJING)
    stats_path = output / "fake-agent-calls.json"
    engine = Engine(config, _DemoProviders(runtime, stats_path), now=now)
    transitions = [engine.step()]
    before_crash = read_json(runtime / "state.json")
    if before_crash["phase"] != "CODE":
        raise RuntimeError("Demo planner did not produce the expected first task")
    try:
        engine.step()
    except _DemoCrash:
        pass
    else:
        raise RuntimeError("Demo did not reach the simulated commit interruption")
    interrupted = read_json(runtime / "state.json")
    interrupted_head = gitops.head(runtime / "workspace")
    # A new Engine and provider instance stand in for a restarted controller.
    engine = Engine(config, _DemoProviders(runtime, stats_path), now=now)
    transitions.append(engine.step())
    recovered = read_json(runtime / "state.json")
    recovery = {
        "interrupted_phase": interrupted["phase"], "recovered_phase": recovered["phase"],
        "task_id_unchanged": interrupted["task"]["id"] == recovered["task"]["id"],
        "head_unchanged": gitops.head(runtime / "workspace") == interrupted_head,
        "recovered_from_git": recovered["task"].get("candidate", {}).get("recovered_from_git") is True,
        "code_calls_after_recovery": read_json(stats_path)["code"],
    }
    write_json(output / "recovery.json", recovery)
    if not (recovery["recovered_phase"] == "CHECK" and recovery["task_id_unchanged"] and
            recovery["head_unchanged"] and recovery["recovered_from_git"] and recovery["code_calls_after_recovery"] == 1):
        raise RuntimeError("Recovery duplicated or failed to reconcile the committed task")
    for _ in range(60):
        snapshot = engine.step()
        transitions.append(snapshot)
        if snapshot["status"] in {"done", "blocked"}:
            break
    final = read_json(runtime / "state.json")
    workspace = runtime / "workspace"
    stats = read_json(stats_path)
    behavior_receipts = [read_json(path) for path in (runtime / "runs").glob("check-*/check.json")]
    checks = {
        "completed": final["phase"] == "DONE", "accepted_two_tasks": len(final["accepted"]) == 2,
        "first_task_reworked_once": read_json(runtime / "tasks/TASK-DEMO-0001/review-0.json")["decision"] == "rework" and
                                    read_json(runtime / "tasks/TASK-DEMO-0001/review-1.json")["decision"] == "accepted" and
                                    not (runtime / "tasks/TASK-DEMO-0001/review-2.json").exists(),
        "exactly_three_code_calls": stats["code"] == 3,
        "exactly_three_task_commits": int(gitops.git(workspace, "rev-list", "--count", initial_head + "..HEAD")) == 3,
        "source_unchanged": gitops.head(project) == initial_head and gitops.clean(project),
        "workspace_clean": gitops.clean(workspace),
        "all_accepted_heads_have_passing_behavior_evidence": all(
            any(receipt["id"] == "behavior" and receipt["head_sha"] == record["head_sha"] and
                receipt["passed"] and receipt["exit_code"] == 0 and
                not receipt["timed_out"] and not receipt["output_limit_exceeded"]
                for receipt in behavior_receipts)
            for record in final["accepted"]),
    }
    summary = {"status": "passed" if all(checks.values()) else "failed", "mode": "offline_fake_agents_real_git",
               "real_models_called": False, "fixed_beijing_time": now().isoformat(),
               "output": str(output), "project": str(project), "workspace": str(workspace),
               "config": str(config_path), "runtime": str(runtime), "checks": checks,
               "recovery": recovery, "fake_agent_calls": stats, "accepted": final["accepted"],
               "transitions": transitions}
    write_json(output / "summary.json", summary)
    if summary["status"] != "passed":
        raise RuntimeError("Offline demo failed; inspect " + str(output / "summary.json"))
    return summary
