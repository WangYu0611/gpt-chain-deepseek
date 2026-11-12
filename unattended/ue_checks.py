"""Run fixed UE automation for a committed task or milestone outside the agent sandbox.

The request is data: it can select safe project test names, two fixed modes,
and an optional fixed clean editor rebuild. It cannot supply executables,
arguments, environment variables, or commands. Milestone regression uses
the fixed project suite rather than a task request.
Run this script with the managed workspace as its current working directory.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import struct
import sys
import time
import uuid

# Also support: python /absolute/path/unattended/ue_checks.py ...
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from github_harness_lab.lab_harness.process import run_process, safe_environment
from unattended import gitops
from unattended.storage import write_json


TEST_TIMEOUT_SECONDS = 600
REGRESSION_TIMEOUT_SECONDS = 1800
BUILD_TIMEOUT_SECONDS = 1800
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_TESTS = 16
MAX_TEST_BATCHES = 16
MAX_CORE_TESTS = 5
_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}\Z")
_HEAD = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_TEST = re.compile(r"ZhuGuoCity\.[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*\Z")
_MILESTONE_TEST = re.compile(r"ZhuGuoCity\.M[0-9]+(?:\.[A-Za-z0-9_]+)+\Z")
# UE uses a literal beginning anchor. The receipt verifies numeric M suite
# names, while ZhuGuoCity.Local visual cases are outside the headless run.
_MILESTONE_SELECTOR = "^ZhuGuoCity.M"
_AUTOMATION_FAILURE = re.compile(r"Result=\{(?:Fail|Failed|Failure)\}|Fatal error", re.I)
_PNG = b"\x89PNG\r\n\x1a\n"
_WINDOWS_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z", re.I)


class ValidationError(ValueError):
    pass


def _is_reparse(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _safe_under(root: Path, relative: str | Path) -> Path:
    """Keep the lexical path and inspect every existing component before resolve."""
    parts = Path(relative).parts
    if Path(relative).is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ValidationError("A confined relative path is required")
    current = root
    if _is_reparse(current):
        raise ValidationError("Workspace/runtime roots cannot be symlinks or junctions")
    for part in parts:
        current = current / part
        if _is_reparse(current):
            raise ValidationError("Symlinks and junctions are forbidden in validation paths")
    if not current.resolve().is_relative_to(root.resolve()):
        raise ValidationError("Validation path escaped its root")
    return current


def _object(path: Path, maximum: int = 2 * 1024 * 1024) -> dict:
    if _is_reparse(path) or not path.is_file() or path.stat().st_size > maximum:
        raise ValidationError("Missing, linked, non-file, or oversized JSON evidence: " + str(path))

    def pairs(entries):
        value = {}
        for key, item in entries:
            if key in value:
                raise ValidationError("Duplicate JSON field: " + key)
            value[key] = item
        return value

    def constant(_):
        raise ValidationError("Non-finite JSON values are forbidden")

    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"),
                           object_pairs_hook=pairs, parse_constant=constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValidationError("Cannot read valid JSON evidence: " + str(path)) from error
    if type(value) is not dict:
        raise ValidationError("JSON evidence must be an object")
    return value


def _binding(state: dict, *, milestone_regression=False) -> tuple[str, str, str]:
    phase = state.get("phase")
    if milestone_regression:
        milestone, operation = state.get("milestone"), state.get("operation")
        if (phase != "MILESTONE_REVIEW" or state.get("task") is not None or type(milestone) is not dict or
                type(operation) is not dict or operation.get("kind") != "milestone_check" or
                operation.get("head_sha") != milestone.get("head_sha")):
            raise ValidationError("Full regression requires an active milestone check on the final milestone HEAD")
        task_id, head_sha = milestone.get("id"), milestone.get("head_sha")
        scope = "milestone_full_automation"
    elif phase == "CHECK":
        task = state.get("task")
        if type(task) is not dict or type(task.get("candidate")) is not dict:
            raise ValidationError("CHECK requires an active task and candidate")
        task_id, head_sha = task.get("id"), task["candidate"].get("head_sha")
        scope = "task"
    elif phase == "MILESTONE_REVIEW":
        milestone = state.get("milestone")
        if state.get("task") is not None or type(milestone) is not dict:
            raise ValidationError("MILESTONE_REVIEW requires a milestone and no active task")
        task_ids, records = milestone.get("task_ids"), milestone.get("accepted_tasks")
        if (type(task_ids) is not list or not task_ids or
                any(not isinstance(value, str) for value in task_ids) or len(task_ids) != len(set(task_ids)) or
                type(records) is not list):
            raise ValidationError("Milestone task identities are missing or ambiguous")
        task_id, head_sha = task_ids[-1], milestone.get("head_sha")
        selected = [record for record in records if type(record) is dict and record.get("id") == task_id]
        if len(selected) != 1 or selected[0].get("head_sha") != head_sha:
            raise ValidationError("Last accepted task must bind the exact final milestone HEAD")
        scope = "milestone_last_task_recheck"
    else:
        raise ValidationError("UE validation is only allowed during CHECK or MILESTONE_REVIEW")
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise ValidationError("Invalid task identity")
    if not isinstance(head_sha, str) or not _HEAD.fullmatch(head_sha):
        raise ValidationError("A complete candidate commit identity is required")
    return task_id, head_sha, scope


def task_validation_scope_sha256(task: dict, checks: list) -> str:
    """Same immutable task scope used by critical replanning; no model feedback."""
    scope = {key: task.get(key) for key in
             ("id", "title", "body", "acceptance_criteria", "core_tests", "base_sha", "coding_provider")}
    scope["checks"] = checks
    return hashlib.sha256(json.dumps(scope, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def verify_task_validation_policy(config: dict, task: dict, runtime: Path, workspace: Path) -> dict | None:
    """Reconcile a pinned completed failure without changing any raw PASS/FAIL.

    This is task-only. It never changes validation requests, UE receipts, or the
    milestone gate. A new candidate/receipt needs a new user-owned policy.
    """
    from unattended.config import validate_task_validation_policy

    value = config.get("task_validation_policy")
    if value is None:
        return None
    policy = validate_task_validation_policy(value)
    if task.get("id") != policy["task_id"] or task.get("kind") == "art":
        return None
    runtime, workspace = Path(runtime).absolute(), Path(workspace).absolute()
    if workspace != _safe_under(runtime, "workspace"):
        raise ValidationError("Task validation policy requires the managed workspace")
    head = (task.get("candidate") or {}).get("head_sha")
    if head != policy["head_sha"] or task_validation_scope_sha256(task, config["checks"]) != policy["scope_sha256"]:
        raise ValidationError("Task validation policy scope or candidate HEAD changed")

    fingerprints = {}

    def read_file(path: Path, root: Path, *, maximum=MAX_OUTPUT_BYTES) -> bytes:
        try:
            relative = path.absolute().relative_to(root.absolute())
        except ValueError as error:
            raise ValidationError("Task gate evidence escaped its trusted root") from error
        safe = _safe_under(root, relative)
        if not safe.is_file() or safe.stat().st_size > maximum:
            raise ValidationError("Missing or oversized task gate evidence")
        before = safe.stat()
        with safe.open("rb") as source:
            opened = os.fstat(source.fileno())
            raw = source.read(maximum + 1)
            finished = os.fstat(source.fileno())
        after = safe.stat()
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if any(identity(item) != identity(before) for item in (opened, finished, after)):
            raise ValidationError("Task gate evidence changed while reading")
        _safe_under(root, relative)
        if len(raw) > maximum:
            raise ValidationError("Task gate evidence exceeded its size bound")
        fingerprints[str(safe)] = hashlib.sha256(raw).hexdigest()
        return raw

    results = task.get("check_results")
    checks = config["checks"]
    if (not isinstance(results, list) or not checks or len(results) != len(checks)
            or any(row.get("id") != check["id"] or row.get("head_sha") != head for row, check in zip(results, checks))):
        raise ValidationError("Task gate requires the complete configured checks on the exact HEAD")
    failed = [row for row in results if row.get("passed") is not True]
    if len(failed) != 1 or failed[0].get("id") != "ue-task-validation":
        raise ValidationError("Only the pinned UE task validation check may be deferred")
    check = failed[0]
    if check.get("exit_code") != 1 or check.get("timed_out") is not False or check.get("output_limit_exceeded") is not False:
        raise ValidationError("Interrupted or crashed checks cannot be deferred")
    runs = _safe_under(runtime, "runs")
    check_stdout = Path(check["stdout_path"])
    raw_check = read_file(check_stdout.parent / "check.json", runs, maximum=2 * 1024 * 1024)
    if json.loads(raw_check.decode("utf-8-sig")) != check:
        raise ValidationError("Task check state does not match its original receipt")
    text = read_file(check_stdout, runs).decode("utf-8-sig")
    read_file(Path(check["stderr_path"]), runs)
    lines = text.strip().splitlines()
    if len(lines) != 2 or lines[0] != "UE_TASK_VALIDATION_FAILED":
        raise ValidationError("Missing unambiguous independent validation result")
    summary = json.loads(lines[1])
    parent = _safe_under(runtime, Path("tasks") / task["id"] / "validation")
    path = Path(summary["receipt_path"])
    raw = read_file(path, parent, maximum=16 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != policy["receipt_sha256"]:
        raise ValidationError("Pinned validation receipt bytes changed")
    receipt = json.loads(raw.decode("utf-8-sig"))
    name = policy["deferred_tests"][0]["name"]
    expected_error = {"type": "ValidationError", "message": "UE automation failed requested tests: " + name}
    for obj in (summary, receipt):
        if (obj.get("task_id") != task["id"] or obj.get("head_sha") != head or obj.get("scope") != "task"
                or obj.get("status") != "failed" or obj.get("error") != expected_error):
            raise ValidationError("Receipt is not the approved completed task test failure")
    if receipt.get("failed_tests") != [name] or Path(receipt.get("receipt_path", "")) != path:
        raise ValidationError("Validation receipt failure set or identity changed")
    request = validate_request(receipt["request"], task["id"], head)
    if request.get("artifacts") or (request.get("clean_build") and (receipt.get("clean_build") or {}).get("passed") is not True):
        raise ValidationError("Pending artifacts or a failed clean build cannot be deferred")
    tests = receipt.get("tests", [])
    wanted = [row["name"] for row in request["tests"]] + [item for batch in request.get("test_batches", []) for item in batch]
    if ([row.get("name") for row in tests] != wanted or [row["name"] for row in tests if row.get("passed") is not True] != [name]
            or name in (task.get("core_tests") or [])
            or any(core not in wanted for core in task.get("core_tests") or [])):
        raise ValidationError("Actual tests or mandatory core checks do not match the complete request")
    snapshot = snapshot_repository(workspace)
    for recorded in (receipt.get("before", {}), receipt.get("after", {})):
        if (recorded.get("head_sha") != head or recorded.get("clean") is not True or snapshot.get("clean") is not True
                or snapshot.get("head_sha") != head or recorded.get("tracked_inputs") != snapshot.get("tracked_inputs")
                or recorded.get("tracked_input_sha256") != snapshot.get("tracked_input_sha256")):
            raise ValidationError("Candidate source changed since independent validation")
    output_by_path = {}
    for test in tests:
        if (test.get("head_sha") != head or test.get("timed_out") is not False
                or test.get("output_limit_exceeded") is not False or test.get("exit_code") not in (0, 1)):
            raise ValidationError("Incomplete, timed out, or crashed automation cannot be deferred")
        stdout = str(test["stdout_path"])
        if stdout not in output_by_path:
            output = read_file(Path(stdout), path.parent).decode("utf-8-sig")
            error_output = read_file(Path(test["stderr_path"]), path.parent).decode("utf-8-sig")
            if "fatal error" in (output + error_output).lower():
                raise ValidationError("Fatal automation errors cannot be deferred")
            output_by_path[stdout] = output
        output = output_by_path[stdout]
        if "batch" in test:
            batch = request["test_batches"][test["batch"] - 1]
            facts = parse_batch_automation_output(output, batch)
            actual = facts["complete"] and test["name"] in facts["successful_paths"]
            if not facts["complete"] or (facts["failure_marker"] and not facts["failed_paths"]):
                raise ValidationError("Batch execution is incomplete or has unclassified errors")
        else:
            facts = parse_automation_output(output, test["name"])
            actual = facts["passed"]
        if actual != test["passed"]:
            raise ValidationError("Raw automation evidence disagrees with its receipt")
    deferred = policy["deferred_tests"][0]
    evidence = _safe_under(runtime, deferred["evidence_path"])
    evidence_raw = read_file(evidence, runtime)
    if hashlib.sha256(evidence_raw).hexdigest() != deferred["evidence_sha256"]:
        raise ValidationError("Pinned environment evidence bytes changed")
    uat = evidence_raw.decode("utf-8-sig")
    if "Unable to build while Live Coding is active." not in uat or "ExitCode=6" not in uat:
        raise ValidationError("Environment evidence does not prove the approved Live Coding failure")
    failed_test = next(test for test in tests if test["name"] == name)
    batch_output = output_by_path[str(failed_test["stdout_path"])].replace("\\", "/")
    events = re.search(r"BeginEvents: " + re.escape(name) + r"\s*(.*?)EndEvents: " + re.escape(name), batch_output, re.S)
    if not events or ("log=" + evidence.as_posix()) not in events[1] or "package integration failed:" not in events[1]:
        raise ValidationError("The failed test does not reference this environment evidence")
    # Recheck identity after reading all evidence; this gate grants no writes.
    if snapshot_repository(workspace) != snapshot:
        raise ValidationError("Candidate source changed while reconciling task gates")
    policy_sha = hashlib.sha256(json.dumps(policy, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return {"version": 1, "status": "policy_verified", "gate_passed": True, "raw_check_passed": False,
            "task_id": task["id"], "head_sha": head, "scope_sha256": policy["scope_sha256"],
            "policy_sha256": policy_sha, "policy": policy, "receipt_sha256": policy["receipt_sha256"],
            "deferred_tests": [{**deferred, "status": "deferred_nongating", "passed": False}],
            "evidence_sha256": fingerprints}


def _artifact_relative(value: object, task_id: str) -> str:
    if (not isinstance(value, str) or not value or any(ord(char) < 32 for char in value) or
            any(char in value for char in ':"<>|?*')):
        raise ValidationError("Artifact paths must be confined ordinary relative paths")
    if PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
        raise ValidationError("Absolute artifact paths and drives are forbidden")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if PurePosixPath(normalized).is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ValidationError("Artifact traversal or absolute paths are forbidden")
    if any(_WINDOWS_RESERVED.fullmatch(part) or part.endswith((" ", ".")) for part in parts):
        raise ValidationError("Reserved or ambiguous Windows artifact names are forbidden")
    prefixes = [("Saved", "Evidence", task_id), ("Docs", "Reports", "Evidence", task_id)]
    if not any(tuple(parts[:len(prefix)]) == prefix and len(parts) > len(prefix) for prefix in prefixes):
        raise ValidationError("Artifacts must stay under this task's Saved/Evidence or Docs/Reports/Evidence directory")
    return "/".join(parts)


def _validate_test_name(name: object) -> str:
    if not isinstance(name, str) or len(name) > 200 or not _TEST.fullmatch(name):
        raise ValidationError("Only safe, exact ZhuGuoCity automation test names are allowed")
    return name


def validate_core_tests(names: object) -> list[str]:
    if type(names) is not list or not 1 <= len(names) <= MAX_CORE_TESTS:
        raise ValidationError(f"Core tests require 1..{MAX_CORE_TESTS} exact test names")
    validated = [_validate_test_name(name) for name in names]
    if len({name.casefold() for name in validated}) != len(validated):
        raise ValidationError("Duplicate core test name")
    return validated


def validate_request(request: dict, task_id: str, head_sha: str) -> dict:
    required = {"version", "task_id", "head_sha", "tests"}
    if type(request) is not dict or not required <= set(request) or set(request) - required - {"artifacts", "clean_build", "test_batches"}:
        raise ValidationError("Validation request has missing or unknown fields")
    if "clean_build" in request and type(request["clean_build"]) is not bool:
        raise ValidationError("clean_build must be a boolean")
    if type(request["version"]) is not int or request["version"] != 1:
        raise ValidationError("Validation request version must be 1")
    if request["task_id"] != task_id or request["head_sha"] != head_sha:
        raise ValidationError("Validation request must bind the current task and exact candidate HEAD")
    tests = request["tests"]
    if type(tests) is not list or not 1 <= len(tests) <= MAX_TESTS:
        raise ValidationError("Validation request requires 1..16 tests")
    seen = set()
    for test in tests:
        if type(test) is not dict or set(test) != {"name", "mode"}:
            raise ValidationError("A test may contain only name and mode")
        name, mode = _validate_test_name(test["name"]), test["mode"]
        if not isinstance(mode, str) or mode not in {"nullrhi", "normal"}:
            raise ValidationError("Test mode must be nullrhi or normal")
        key = (name, mode)
        if key in seen:
            raise ValidationError("Duplicate test request")
        seen.add(key)
    if "test_batches" in request:
        batches = request["test_batches"]
        if type(batches) is not list or not 1 <= len(batches) <= MAX_TEST_BATCHES:
            raise ValidationError("test_batches requires 1..16 batches")
        batch_seen = {test["name"].casefold() for test in tests}
        for batch in batches:
            if type(batch) is not list or not 1 <= len(batch) <= MAX_TESTS:
                raise ValidationError("Each test batch requires 1..16 exact M2/M3 test names")
            for value in batch:
                name = _validate_test_name(value)
                if not name.startswith(("ZhuGuoCity.M2.", "ZhuGuoCity.M3.")):
                    raise ValidationError("Test batches require exact M2/M3 test names")
                if name.casefold() in batch_seen:
                    raise ValidationError("Duplicate test across request and batches")
                batch_seen.add(name.casefold())
    artifacts = request.get("artifacts", [])
    if type(artifacts) is not list:
        raise ValidationError("artifacts must be a list")
    normalized = [_artifact_relative(value, task_id) for value in artifacts]
    # Windows paths alias case-insensitively, including on a non-Windows test host.
    if len({value.casefold() for value in normalized}) != len(normalized):
        raise ValidationError("Duplicate artifact paths")
    return {**request, "artifacts": normalized}


def _fingerprint(path: Path) -> dict | None:
    if not path.exists():
        return None
    if _is_reparse(path) or not stat.S_ISREG(path.stat().st_mode):
        raise ValidationError("Evidence and tracked inputs must be ordinary files")
    before = path.stat()
    digest = hashlib.sha256()
    header = b""
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValidationError("Evidence changed while opening")
        while chunk := source.read(1024 * 1024):
            if len(header) < 24:
                header += chunk[:24 - len(header)]
            digest.update(chunk)
        finished = os.fstat(source.fileno())
    after = path.stat()
    identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
    if identity(before) != identity(finished) or identity(before) != identity(after) or _is_reparse(path):
        raise ValidationError("Evidence changed while hashing")
    value = {"sha256": digest.hexdigest(), "size": after.st_size, "mtime_ns": after.st_mtime_ns,
             "inode": after.st_ino, "device": after.st_dev}
    birth = getattr(after, "st_birthtime_ns", None)
    if birth is not None:
        value["birthtime_ns"] = birth
    if path.suffix.lower() == ".png":
        if len(header) < 24 or header[:8] != _PNG or header[12:16] != b"IHDR":
            raise ValidationError("PNG evidence has no valid PNG/IHDR header")
        width, height = struct.unpack(">II", header[16:24])
        if width == 0 or height == 0:
            raise ValidationError("PNG evidence has invalid dimensions")
        value["png_dimensions"] = {"width": width, "height": height}
    return value


def snapshot_repository(workspace: Path) -> dict:
    head_sha, clean = gitops.head(workspace), gitops.clean(workspace)
    paths = [path for path in gitops.git(workspace, "ls-files", "-z", "--", "Source", "Config", "Content", "*.uproject").split("\0") if path]
    inputs = {}
    for relative in sorted(paths):
        path = _safe_under(workspace, relative)
        fact = _fingerprint(path)
        if fact is None:
            raise ValidationError("A tracked input is missing: " + relative)
        inputs[relative] = {key: fact[key] for key in ("sha256", "size")}
    if gitops.head(workspace) != head_sha or gitops.clean(workspace) != clean:
        raise ValidationError("Git state changed while taking the input snapshot")
    summary = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"head_sha": head_sha, "clean": clean, "tracked_input_sha256": summary, "tracked_inputs": inputs}


def _test_selector(name: str) -> str:
    # UE 5.8 AutomationCommandline.cpp strips these two boundary markers;
    # AutomationControllerSettings.h matches the remaining *literal* full path
    # from both ends. This is not a regular expression: do not escape dots.
    if not _TEST.fullmatch(name):
        raise ValidationError("Unsafe automation test name")
    return "^" + name + "$"


def parse_automation_output(output: str, name: str) -> dict:
    selector = _test_selector(name)
    found = [int(count) for count in re.findall(
        r"\bFound\s+(\d+)\s+automation tests? based on ['\"]" + re.escape(selector) + r"['\"]", output)]
    success = re.findall(
        r"Test Completed\. Result=\{Success\}\s+Name=\{([^}]+)\}\s+Path=\{" + re.escape(name) + r"\}", output)
    failed = re.findall(
        r"Test Completed\. Result=\{(?:Fail|Failed|Failure)\}\s+Name=\{([^}]+)\}\s+Path=\{" + re.escape(name) + r"\}", output)
    performed = [int(count) for count in re.findall(r"\b(\d+)\s+tests?\s+performed\.", output)]
    failures = bool(_AUTOMATION_FAILURE.search(output))
    return {"selector": selector, "found_counts": found, "success_names": success, "failure_names": failed, "performed_counts": performed,
            "failure_marker": failures,
            "passed": found == [1] and success == [name.rsplit(".", 1)[-1]] and performed == [1] and not failures}


def parse_milestone_regression_output(output: str) -> dict:
    found = [int(count) for count in re.findall(
        r"\bFound\s+(\d+)\s+automation tests? based on ['\"]" + re.escape(_MILESTONE_SELECTOR) + r"['\"]", output)]
    discovered = re.findall(r"LogAutomationCommandLine: Display:[ \t]+(ZhuGuoCity\.[^\s]+)[ \t]*\r?$", output, re.M)
    successful = re.findall(r"Test Completed\. Result=\{Success\}\s+Name=\{[^}]+\}\s+Path=\{([^}]+)\}", output)
    performed = [int(count) for count in re.findall(r"\b(\d+)\s+tests?\s+performed\.", output)]
    failures = bool(_AUTOMATION_FAILURE.search(output))
    count = len(discovered)
    passed = (count > 0 and found == performed == [count] and len(set(discovered)) == count and
              all(_MILESTONE_TEST.fullmatch(name) for name in discovered) and
              sorted(successful) == sorted(discovered) and not failures)
    return {"selector": _MILESTONE_SELECTOR, "found_counts": found, "discovered_paths": discovered,
            "successful_paths": successful, "performed_counts": performed, "failure_marker": failures, "passed": passed}


def parse_batch_automation_output(output: str, names: list[str]) -> dict:
    selector = "+".join(_test_selector(name) for name in names)
    found = [int(count) for count in re.findall(
        r"\bFound\s+(\d+)\s+automation tests? based on ['\"]" + re.escape(selector) + r"['\"]", output)]
    discovered = re.findall(r"LogAutomationCommandLine: Display:[ \t]+(ZhuGuoCity\.[^\s]+)[ \t]*\r?$", output, re.M)
    successful = re.findall(r"Test Completed\. Result=\{Success\}\s+Name=\{[^}]+\}\s+Path=\{([^}]+)\}", output)
    failed = re.findall(r"Test Completed\. Result=\{(?:Fail|Failed|Failure)\}\s+Name=\{[^}]+\}\s+Path=\{([^}]+)\}", output)
    performed = [int(count) for count in re.findall(r"\b(\d+)\s+tests?\s+performed\.", output)]
    completed = successful + failed
    complete = (found == performed == [len(names)] and sorted(discovered) == sorted(names) and
                len(set(discovered)) == len(names) and sorted(completed) == sorted(names) and len(set(completed)) == len(names))
    failure_marker = bool(_AUTOMATION_FAILURE.search(output))
    return {"selector": selector, "found_counts": found, "discovered_paths": discovered,
            "successful_paths": successful, "failed_paths": failed, "performed_counts": performed,
            "complete": complete, "failure_marker": failure_marker,
            "passed": complete and not failed and not failure_marker}


def _binaries(engine: Path) -> Path:
    choices = [engine / "Engine/Binaries/Win64", engine / "Binaries/Win64"]
    for path in choices:
        if (path / "UnrealEditor-Cmd.exe").is_file() and (path / "UnrealEditor.exe").is_file():
            return path
    raise ValidationError("Engine must contain the native UnrealEditor and UnrealEditor-Cmd executables")


def _project(workspace: Path) -> Path:
    projects = sorted(workspace.glob("*.uproject"))
    if len(projects) != 1:
        raise ValidationError("Managed workspace must contain exactly one root .uproject")
    return _safe_under(workspace, projects[0].name)


def _command(binaries: Path, project: Path, test: dict, log: Path, *, milestone_regression=False, batch_names=None) -> list[str]:
    executable = "UnrealEditor-Cmd.exe" if test["mode"] == "nullrhi" else "UnrealEditor.exe"
    selector = _MILESTONE_SELECTOR if milestone_regression else _test_selector(test["name"])
    if batch_names is not None:
        selector = "+".join(_test_selector(name) for name in batch_names)
    argv = [str(binaries / executable), str(project),
            "-ExecCmds=Automation RunTests " + selector, "-unattended", "-nop4", "-nosplash",
            "-NoSound", "-DisablePlugins=Fab", "-stdout", "-FullStdOutLogOutput",
            "-TestExit=Automation Test Queue Empty", "-abslog=" + str(log)]
    if test["mode"] == "nullrhi":
        argv.append("-nullrhi")
    return argv


def _clean_build_command(engine: Path, project: Path, log: Path) -> list[str]:
    """One fixed UE 5.8 build action; no request-controlled paths or switches."""
    # --engine already accepts either the installation or its Engine directory.
    # Derive both tools from the same native-editor location, avoiding an extra
    # Engine component when the configured path is the latter form.
    engine_root = _binaries(engine).parent.parent
    dotnet = _safe_under(engine_root, "Binaries/ThirdParty/DotNet/10.0/win-x64/dotnet.exe")
    ubt = _safe_under(engine_root, "Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.dll")
    if not dotnet.is_file() or not ubt.is_file():
        raise ValidationError("Clean build requires the bundled UE 5.8 dotnet and UnrealBuildTool")
    return [str(dotnet), str(ubt), "ZhuGuoCityEditor", "Win64", "Development",
            "-project=" + str(project), "-Rebuild", "-ForceHeaderGeneration", "-NoUBTMakefiles",
            "-waitmutex", "-NoHotReloadFromIDE", "-log=" + str(log)]


def _backup_artifact(workspace: Path, relative: str, destination: Path, expected: dict) -> dict:
    source = _safe_under(workspace, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as input_file, destination.open("xb") as output_file:
        opened = os.fstat(input_file.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                expected["device"], expected["inode"], expected["size"], expected["mtime_ns"]):
            raise ValidationError("Artifact changed before archival")
        while chunk := input_file.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
            output_file.write(chunk)
        output_file.flush()
        os.fsync(output_file.fileno())
    _safe_under(workspace, relative)
    if digest.hexdigest() != expected["sha256"] or size != expected["size"] or _fingerprint(source) != expected:
        raise ValidationError("Artifact changed while archiving")
    return {"path": relative, **expected, "backup_path": str(destination), "backup_sha256": digest.hexdigest()}


def _write_results_mirror(workspace: Path, task_id: str, head_sha: str, receipt: dict) -> Path:
    relative = Path("Saved/Automation") / task_id / "validation-results.json"
    path = _safe_under(workspace, relative)
    if gitops.git(workspace, "ls-files", "--", relative.as_posix()):
        raise ValidationError("Validation results mirror cannot overwrite a tracked file")
    if not gitops.git(workspace, "check-ignore", "--", relative.as_posix()):
        raise ValidationError("Validation results mirror must remain Git-ignored")
    fields = ("name", "mode", "batch", "head_sha", "passed", "exit_code", "timed_out", "output_limit_exceeded",
              "automation", "stdout_path", "stderr_path")
    mirror = {key: receipt[key] for key in
              ("version", "status", "scope", "receipt_path", "finished_at", "error", "failed_tests", "core_tests") if key in receipt}
    mirror.update(task_id=task_id, head_sha=head_sha,
                  tests=[{key: test[key] for key in fields if key in test} for test in receipt["tests"]])
    write_json(path, mirror)
    _safe_under(workspace, relative)
    return path


def run_validation(runtime: Path, workspace: Path, engine: Path, *, runner=None, milestone_regression=False) -> dict:
    """Run once and retain a success/failure receipt; this never retries or pushes."""
    runtime, workspace, engine = (Path(value).expanduser().absolute() for value in (runtime, workspace, engine))
    receipt = {"version": 1, "status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
               "tests": [], "artifacts": [], "image_content_validated": False}
    run_dir = None
    task_id, head_sha = "_invalid-task", "unbound"
    process_runner = runner if runner is not None else run_process
    try:
        if not runtime.is_dir() or _is_reparse(runtime) or _is_reparse(workspace):
            raise ValidationError("Existing ordinary runtime and workspace directories are required")
        runtime, workspace, engine = runtime.resolve(), workspace.resolve(), engine.resolve()
        if workspace != runtime / "workspace" or Path.cwd().resolve() != workspace:
            raise ValidationError("cwd and --workspace must both be the runtime/workspace directory")
        state = _object(_safe_under(runtime, "state.json"), 32 * 1024 * 1024)
        task_id, head_sha, scope = _binding(state, milestone_regression=milestone_regression)
        parent = _safe_under(runtime, Path("milestones" if milestone_regression else "tasks") / task_id / "validation")
        run_dir = parent / (head_sha[:12] + "-" + uuid.uuid4().hex)
        run_dir.mkdir(parents=True, exist_ok=False)
        receipt["milestone_id" if milestone_regression else "task_id"] = task_id
        receipt.update(head_sha=head_sha, scope=scope, receipt_path=str(run_dir / "receipt.json"))
        before = snapshot_repository(workspace)
        receipt["before"] = before
        if before["head_sha"] != head_sha or not before["clean"]:
            raise ValidationError("Candidate HEAD must match and Git must be clean before validation")
        if milestone_regression:
            request = {"tests": [{"name": "MilestoneFullAutomation", "mode": "nullrhi"}], "artifacts": []}
        else:
            manifest = _safe_under(workspace, Path("Saved/Automation") / task_id / "validation-request.json")
            request = validate_request(_object(manifest), task_id, head_sha)
        receipt["request"] = request
        if scope == "task" and "core_tests" in state["task"]:
            core_tests = validate_core_tests(state["task"]["core_tests"])
            receipt["core_tests"] = core_tests
            requested = {test["name"] for test in request["tests"] if test["mode"] == "nullrhi"}
            missing = [name for name in core_tests if name not in requested]
            if missing:
                raise ValidationError("Validation request is missing planned NullRHI core tests: " + ", ".join(missing))
        artifact_before = {relative: _fingerprint(_safe_under(workspace, relative)) for relative in request["artifacts"]}
        receipt["artifact_before"] = artifact_before
        binaries, project = _binaries(engine), _project(workspace)
        write_json(run_dir / "receipt.json", receipt)
        if request.get("clean_build") is True:
            folder = run_dir / "00-clean-build"
            folder.mkdir()
            argv = _clean_build_command(engine, project, folder / "UnrealBuildTool.log")
            started = time.monotonic()
            process = process_runner(argv, cwd=workspace, output_dir=folder,
                                     timeout_seconds=BUILD_TIMEOUT_SECONDS, environment=safe_environment(),
                                     max_output_bytes=MAX_OUTPUT_BYTES)
            output = process.stdout_path.read_text(encoding="utf-8", errors="replace") + "\n" + process.stderr_path.read_text(encoding="utf-8", errors="replace")
            passed = (process.exit_code == 0 and not process.timed_out and not process.output_limit_exceeded and
                      "Result: Succeeded" in output and
                      not any(value in output for value in ("Result: Failed", "Fatal error:", "Unhandled exception:")))
            result = {"head_sha": head_sha, "argv": argv, "exit_code": process.exit_code,
                      "timed_out": process.timed_out, "output_limit_exceeded": process.output_limit_exceeded,
                      "elapsed_seconds": round(time.monotonic() - started, 3),
                      "stdout_path": str(process.stdout_path), "stderr_path": str(process.stderr_path),
                      "build_log_path": str(folder / "UnrealBuildTool.log"), "passed": passed}
            receipt["clean_build"] = result
            write_json(folder / "process.json", {key: result[key] for key in
                       ("head_sha", "argv", "exit_code", "timed_out", "output_limit_exceeded", "elapsed_seconds")})
            write_json(folder / "result.json", result)
            write_json(run_dir / "receipt.json", receipt)
            if not passed:
                raise ValidationError("Fixed clean UHT/editor rebuild failed; see archived build logs")
            built = snapshot_repository(workspace)
            receipt["after_clean_build"] = built
            if (built["head_sha"] != head_sha or not built["clean"] or
                    built["tracked_inputs"] != before["tracked_inputs"]):
                raise ValidationError("HEAD, tracked inputs, or Git cleanliness changed during clean build")
        runs = [{"tests": [test]} for test in request["tests"]]
        runs.extend({"tests": [{"name": name, "mode": "nullrhi"} for name in names], "batch": index}
                    for index, names in enumerate(request.get("test_batches", []), 1))
        for index, run in enumerate(runs, 1):
            test = run["tests"][0]
            batch_names = [item["name"] for item in run["tests"]] if "batch" in run else None
            name = "Batch" + f"{run['batch']:02d}" if batch_names is not None else test["name"]
            folder = run_dir / (f"{index:02d}-" + name.rsplit(".", 1)[-1][:32])
            folder.mkdir()
            argv = _command(binaries, project, test, folder / "Unreal.log", milestone_regression=milestone_regression,
                            batch_names=batch_names)
            started = time.monotonic()
            process = process_runner(argv, cwd=workspace, output_dir=folder,
                                     timeout_seconds=REGRESSION_TIMEOUT_SECONDS if milestone_regression else TEST_TIMEOUT_SECONDS,
                                     environment=safe_environment(),
                                     max_output_bytes=MAX_OUTPUT_BYTES)
            output = process.stdout_path.read_text(encoding="utf-8", errors="replace") + "\n" + process.stderr_path.read_text(encoding="utf-8", errors="replace")
            if batch_names is not None:
                facts = parse_batch_automation_output(output, batch_names)
            else:
                facts = parse_milestone_regression_output(output) if milestone_regression else parse_automation_output(output, name)
            passed = (process.exit_code == 0 and not process.timed_out and not process.output_limit_exceeded and facts["passed"])
            process_facts = {"argv": argv, "head_sha": head_sha, "exit_code": process.exit_code,
                             "timed_out": process.timed_out, "output_limit_exceeded": process.output_limit_exceeded,
                             "elapsed_seconds": round(time.monotonic() - started, 3),
                             "stdout_path": str(process.stdout_path), "stderr_path": str(process.stderr_path)}
            if batch_names is not None:
                normal_exit = process.exit_code == 0 or (process.exit_code == 1 and bool(facts["failed_paths"]))
                batch_valid = (normal_exit and not process.timed_out and not process.output_limit_exceeded and
                               facts["complete"] and "fatal error" not in output.lower() and
                               (not facts["failure_marker"] or bool(facts["failed_paths"])))
                results = [{**process_facts, "name": item["name"], "mode": "nullrhi", "batch": run["batch"],
                            "automation": {**facts, "test_path": item["name"],
                                           "test_result": "Success" if item["name"] in facts["successful_paths"] else "Fail" if item["name"] in facts["failed_paths"] else None},
                            "passed": batch_valid and item["name"] in facts["successful_paths"]}
                           for item in run["tests"]]
                result = {**process_facts, "batch": run["batch"], "automation": facts, "tests": results, "passed": passed}
                receipt.setdefault("batches", []).append(result)
                receipt["tests"].extend(results)
            else:
                result = {**process_facts, "name": name, "mode": test["mode"], "automation": facts, "passed": passed}
                receipt["tests"].append(result)
            write_json(folder / "result.json", result)
            write_json(run_dir / "receipt.json", receipt)
            # Every next test must use the same trusted source, including
            # after a test that reported success while mutating tracked inputs.
            current = snapshot_repository(workspace)
            if (current["head_sha"] != head_sha or not current["clean"] or
                    current["tracked_inputs"] != before["tracked_inputs"]):
                raise ValidationError("HEAD, tracked inputs, or Git cleanliness changed during validation")
            if not passed:
                if milestone_regression:
                    raise ValidationError("UE milestone regression did not prove a successful execution of every discovered test")
                if batch_names is not None:
                    completed_failure = batch_valid and bool(facts["failed_paths"])
                else:
                    completed_failure = (process.exit_code in (0, 1) and not process.timed_out and not process.output_limit_exceeded and
                                         facts["found_counts"] == facts["performed_counts"] == [1] and
                                         facts["failure_names"] == [name.rsplit(".", 1)[-1]] and "fatal error" not in output.lower())
                if not completed_failure:
                    raise ValidationError("UE automation did not prove one completed test execution: " + name)
        after = snapshot_repository(workspace)
        receipt["after"] = after
        if after["head_sha"] != head_sha or not after["clean"] or after["tracked_inputs"] != before["tracked_inputs"]:
            raise ValidationError("HEAD, tracked Source/Config/Content/project inputs, or Git cleanliness changed during validation")
        failed_tests = [test["name"] for test in receipt["tests"] if not test["passed"]]
        if failed_tests:
            receipt["failed_tests"] = failed_tests
            raise ValidationError("UE automation failed requested tests: " + ", ".join(failed_tests))
        for index, relative in enumerate(request["artifacts"], 1):
            source = _safe_under(workspace, relative)
            fact = _fingerprint(source)
            if fact is None:
                raise ValidationError("Requested artifact was not generated: " + relative)
            old = artifact_before[relative]
            keys = ("sha256", "size", "mtime_ns", "inode", "birthtime_ns")
            if old is not None and all(old.get(key) == fact.get(key) for key in keys):
                raise ValidationError("Requested artifact was not newly generated or rewritten in this validation: " + relative)
            destination = run_dir / "artifacts" / (f"{index:03d}-" + source.name)
            artifact = _backup_artifact(workspace, relative, destination, fact)
            artifact["freshness"] = "created" if old is None else "rewritten"
            receipt["artifacts"].append(artifact)
        # No screenshot-content claim is made by timestamps or PNG dimensions.
        receipt["normal_rhi_requested"] = any(test["mode"] == "normal" for test in request["tests"])
        receipt["visual_review_required"] = receipt["normal_rhi_requested"] and bool(receipt["artifacts"])
        receipt["evidence_scope"] = ("automation_with_fresh_artifacts_for_review" if receipt["artifacts"] else
                                     "automation_only_no_graphical_claim")
        receipt["status"] = "passed"
    except Exception as error:
        receipt["status"] = "failed"
        receipt["error"] = {"type": type(error).__name__, "message": str(error)}
        # If execution began, preserve a best-effort final snapshot on failure.
        if run_dir is not None and "before" in receipt and "after" not in receipt:
            try:
                receipt["after"] = snapshot_repository(workspace)
            except Exception as snapshot_error:
                receipt["after_error"] = type(snapshot_error).__name__ + ": " + str(snapshot_error)
        if run_dir is None and runtime.is_dir() and not _is_reparse(runtime):
            try:
                parent = _safe_under(runtime, Path("tasks") / "_invalid-task" / "validation")
                run_dir = parent / ("unbound-" + uuid.uuid4().hex)
                run_dir.mkdir(parents=True, exist_ok=False)
                receipt["receipt_path"] = str(run_dir / "receipt.json")
            except (OSError, ValidationError):
                run_dir = None
    receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
    if run_dir is not None:
        write_json(run_dir / "receipt.json", receipt)
        if not milestone_regression and _TASK_ID.fullmatch(task_id) and _HEAD.fullmatch(head_sha):
            try:
                receipt["results_mirror_path"] = str(_write_results_mirror(workspace, task_id, head_sha, receipt))
            except Exception as mirror_error:
                receipt["status"] = "failed"
                receipt["results_mirror_error"] = type(mirror_error).__name__ + ": " + str(mirror_error)
                receipt.setdefault("error", {"type": type(mirror_error).__name__, "message": str(mirror_error)})
            write_json(run_dir / "receipt.json", receipt)
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validate a committed ZhuGuoCity task using fixed native UE automation commands")
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--milestone-regression", action="store_true")
    arguments = parser.parse_args(argv)
    receipt = run_validation(arguments.runtime, arguments.workspace, arguments.engine,
                             milestone_regression=arguments.milestone_regression)
    summary = {key: receipt[key] for key in
               ("status", "task_id", "milestone_id", "head_sha", "scope", "receipt_path", "error") if key in receipt}
    if arguments.milestone_regression:
        automation = receipt["tests"][0]["automation"] if receipt["tests"] else {}
        summary.update(tests_discovered=len(automation.get("discovered_paths", [])),
                       tests_successful=len(automation.get("successful_paths", [])))
        marker = "UE_MILESTONE_REGRESSION"
    else:
        request = receipt.get("request", {})
        summary.update(tests_passed=sum(test["passed"] for test in receipt["tests"]),
                       tests_requested=len(request.get("tests", [])) + sum(len(batch) for batch in request.get("test_batches", [])),
                       artifacts_archived=len(receipt["artifacts"]))
        marker = "UE_TASK_VALIDATION"
    print(marker + ("_PASSED" if receipt["status"] == "passed" else "_FAILED"), flush=True)
    print(json.dumps(summary, ensure_ascii=True), flush=True)
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
