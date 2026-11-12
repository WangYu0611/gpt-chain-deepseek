"""Finish an interrupted retest with one explicitly authorized killed-case restart.

The immutable first pass and completed single-test receipts are never changed.
Only one explicitly identified killed case may restart, in a separate attempt.
"""
from __future__ import annotations

import argparse
import hashlib
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from unattended import full_flow_checks as flow, full_flow_retest as retest, ue_checks
from unattended.storage import lock, write_json

RECOVERY_SECONDS = 1800
REASON = "native_background_timeout_after_completed_prefix"
_IDENTITY = ("id", "task_id", "head_sha", "request_sha256")


def _killed_event(run, record):
    if type(record) is not dict or set(record) != {"path", "sha256", "size_bytes", "task_id", "session_id"}:
        raise ValueError("Native killed event requires exact identity and raw evidence")
    path = Path(record["path"])
    if not path.is_relative_to(run / "native"):
        raise ValueError("Native killed evidence must be archived under this run's native directory")
    path = ue_checks._safe_under(run, path.relative_to(run))
    flow._check_files([record], run)
    value = ue_checks._object(path, maximum=1024 * 1024)
    session = value.get("sessionId", value.get("session_id"))
    if session != record["session_id"] or not isinstance(session, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", session):
        raise ValueError("Native killed event belongs to another session")
    message = value.get("message") or {}
    content = message.get("content")
    if (value.get("type") != "user" or message.get("role") != "user" or value.get("promptSource") != "system" or
            value.get("turnOrigin") != "task_notification" or
            (value.get("origin") or {}).get("kind") != "task-notification"):
        raise ValueError("Expected the native task-notification user event")
    texts = [content] if isinstance(content, str) else [item.get("text", "") for item in content
            if type(item) is dict and item.get("type") == "text"] if isinstance(content, list) else []
    matches = re.findall(r"<task-notification>.*?</task-notification>", "\n".join(texts), re.S)
    if len(matches) != 1:
        raise ValueError("Expected exactly one native task-notification")
    xml = ET.fromstring(matches[0])
    summary = xml.findtext("summary") or ""
    if (xml.findtext("task-id") != record["task_id"] or xml.findtext("status") != "killed" or
            not re.search(r"background", summary, re.I) or
            not re.search(r"timeout|time limit|timed out", summary, re.I)):
        raise ValueError("Native evidence does not prove this task's background timeout")
    return flow._file_record(path)


def _policy(request, run):
    path = ue_checks._safe_under(run, "operator-retest-recovery.json")
    value = ue_checks._object(path, maximum=65536)
    required = {"version", *_IDENTITY, "master_sha256", "completed_cases", "interrupted_case", "resume_runner_sha256", "native_killed_event", "reason"}
    if (set(value) != required or type(value.get("version")) is not int or value["version"] != 1 or
            any(value.get(key) != request[key] for key in _IDENTITY) or value["reason"] != REASON or
            value["resume_runner_sha256"] != flow._sha(__file__) or
            not isinstance(value["master_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["master_sha256"])):
        raise ValueError("Recovery policy does not bind this exact request and runner")
    cases = value["completed_cases"]
    if type(cases) is not list or not cases:
        raise ValueError("A completed prefix must be explicitly authorized")
    for index, case in enumerate(cases, 1):
        if (type(case) is not dict or set(case) != {"index", "receipt_sha256"} or
                type(case["index"]) is not int or case["index"] != index or
                not isinstance(case["receipt_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", case["receipt_sha256"])):
            raise ValueError("Recovery completed prefix is not exact and contiguous")
    interrupted = value["interrupted_case"]
    if (type(interrupted) is not dict or set(interrupted) != {"index", "name", "receipt_sha256", "action"} or
            type(interrupted["index"]) is not int or interrupted["index"] != len(cases) + 1 or
            interrupted["action"] != "restart_once_after_native_kill" or
            not isinstance(interrupted["receipt_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", interrupted["receipt_sha256"])):
        raise ValueError("Recovery must bind the single interrupted case immediately after the completed prefix")
    return value, flow._file_record(path), _killed_event(run, value["native_killed_event"])


def _no_active_case(folder):
    """Read-only Windows process query; never stop a user or test process."""
    if os.name != "nt":
        raise ValueError("Native case liveness proof requires the configured Windows host")
    shell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = ('[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); '
               'ConvertTo-Json -Compress -InputObject @(Get-CimInstance Win32_Process '
               '-Filter "Name = \'UnrealEditor-Cmd.exe\' OR Name = \'UnrealEditor.exe\'" '
               '-ErrorAction Stop | Select-Object ProcessId,CommandLine)')
    result = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, timeout=30, encoding="utf-8", errors="strict",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode or not result.stdout.strip():
        raise ValueError("Could not prove the interrupted native case is no longer active")
    values = json.loads(result.stdout.lstrip("\ufeff"))
    if not isinstance(values, list) or any(not isinstance(row, dict) or not isinstance(row.get("CommandLine"), str) for row in values):
        raise ValueError("Native process liveness evidence is incomplete")
    needle = str(folder).replace("\\", "/").casefold()
    if any(needle in row["CommandLine"].replace("\\", "/").casefold() for row in values):
        raise ValueError("Interrupted case still has an active native process")
    return {"checked_at": flow._now(), "query": "UnrealEditor/UnrealEditor-Cmd exact case directory", "matching_processes": 0}


def _complete_case(request, folder, identity, expected_sha=None, recovery=None):
    path = ue_checks._safe_under(folder, "receipt.json")
    if not path.is_file() or (expected_sha is not None and flow._sha(path) != expected_sha):
        raise ValueError("Completed case receipt is missing or changed")
    row = ue_checks._object(path, maximum=16 * 1024 * 1024)
    if (any(row.get(key) != value for key, value in identity.items()) or
            row.get("status") not in {"PASS", "FAIL", "FALSE_PASS", "NOT_RUN"} or not row.get("finished_at") or
            type(row.get("passed")) is not bool or (row["passed"] != (row["status"] == "PASS"))):
        raise ValueError("Existing case is incomplete or belongs to another test; refusing resubmission")
    if recovery is not None and row.get("recovery") != recovery:
        raise ValueError("Additional completed case was not submitted by this exact recovery")
    flow._check_files(row.get("evidence", []), Path(request["run_dir"]))
    if row["status"] != "NOT_RUN":
        raw = retest._evidence_output(flow, row, Path(request["run_dir"]))
        actual = ue_checks.parse_automation_output(raw, row["name"])
        process = row.get("process") or {}
        if (process.get("argv", []).count("-ExecCmds=Automation RunTests ^" + row["name"] + "$") != 1 or
                ("-nullrhi" in process.get("argv", [])) != (row["mode"] == "nullrhi")):
            raise ValueError("Completed case command differs from its exact original mode/name")
        recorded = row.get("automation") or {}
        for key in ("found_counts", "performed_counts", "success_names", "failure_names"):
            if key in recorded and recorded[key] != actual[key]:
                raise ValueError("Completed case automation differs from raw logs")
        if row["passed"] and (not actual["passed"] or process.get("completed") is not True or
                process.get("exit_code") != 0 or process.get("timed_out") is not False or
                process.get("output_limit_exceeded") is not False):
            raise ValueError("Completed PASS is not backed by successful native execution")
        if row["passed"] and row["mode"] == "normal" and (not flow._normal_rhi(raw) or row.get("skip_lines") or row.get("missing_images")):
            raise ValueError("Completed visual PASS lacks normal RHI or contains skipped/missing evidence")
    return row, flow._file_record(path)


def _archive_checkpoint(path, raw):
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("Interrupted checkpoint archive changed")
        return
    with path.open("xb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())


def run(request_path):
    deadline = time.monotonic() + RECOVERY_SECONDS
    request_path = Path(request_path)
    request = flow.load_request(request_path)
    run_dir = Path(request["run_dir"])
    folder = ue_checks._safe_under(run_dir, "checks/retest/attempt-1")
    resumed_folder = ue_checks._safe_under(run_dir, "checks/retest/attempt-2")
    master_path = ue_checks._safe_under(run_dir, "checks/retest/receipt.json")
    archive_path = ue_checks._safe_under(folder, "interrupted-checkpoint.json")
    with lock(ue_checks._safe_under(run_dir, "checks.lock")), lock(ue_checks._safe_under(folder, "retest.lock")):
        policy, policy_record, killed = _policy(request, run_dir)
        current_binding = flow._bind(request, request_path)
        source_sha = flow._verify_inputs(request)
        selected, originals, unresolved, reconciled = retest._first_pass(flow, request, source_sha)
        origin_sha = retest._digest(originals)
        case_binding = {**{key: request[key] for key in _IDENTITY}, "source_manifest_sha256": source_sha,
                        "first_pass_evidence_sha256": origin_sha}
        prefix = policy["completed_cases"]
        if not 1 <= len(selected) - len(prefix) <= 3:
            raise ValueError("Recovery is limited to the interrupted case and at most two never-started cases")
        recovery = {"policy_sha256": policy_record["sha256"], "runner_sha256": flow._sha(__file__),
                    "interrupted_master_sha256": policy["master_sha256"]}
        master = ue_checks._object(master_path, maximum=16 * 1024 * 1024)
        master_raw = master_path.read_bytes()
        already_final = master.get("status") in {"passed", "failed"}
        if already_final:
            if master.get("recovery") != recovery or not archive_path.is_file():
                raise ValueError("A previous terminal retest result cannot be overwritten")
            original_raw = archive_path.read_bytes()
        else:
            original_raw = master_raw
        if hashlib.sha256(original_raw).hexdigest() != policy["master_sha256"]:
            raise ValueError("Running master bytes differ from the operator checkpoint")
        checkpoint = json.loads(original_raw.decode("utf-8-sig"))
        if (any(checkpoint.get(key) != request[key] for key in _IDENTITY) or checkpoint.get("stage") != "retest" or
                checkpoint.get("attempt") != 1 or checkpoint.get("status") != "running" or checkpoint.get("finished_at") or
                any(checkpoint.get(key) != value for key, value in current_binding.items())):
            raise ValueError("Only this exact unfinished retest checkpoint can be completed")
        rows, evidence = [], [*originals, policy_record, killed]
        for correction in reconciled:
            path = ue_checks._safe_under(folder, f"reconciliations/case-{correction['case_index']:03d}.json")
            expected = {**case_binding, **correction, "parser_runner_sha256": flow._sha(flow.__file__),
                        "retest_runner_sha256": flow._sha(retest.__file__)}
            if ue_checks._object(path, maximum=16 * 1024 * 1024) != expected:
                raise ValueError("Existing parser reconciliation changed")
            correction["derived_receipt"] = flow._file_record(path)
            evidence.append(correction["derived_receipt"])
        known_names = {f"case-{index:03d}" for index in range(1, len(selected) + 1)}
        if any(path.name.startswith("case-") and path.name not in known_names for path in folder.iterdir()):
            raise ValueError("Unexpected retest case directory")
        interrupted = policy["interrupted_case"]
        interrupted_folder = ue_checks._safe_under(folder, f"case-{interrupted['index']:03d}")
        interrupted_path = interrupted_folder / "receipt.json"
        interrupted_row = ue_checks._object(interrupted_path)
        item = selected[interrupted["index"] - 1]
        expected_identity = {**case_binding, **item, "attempt": 1}
        if (flow._sha(interrupted_path) != interrupted["receipt_sha256"] or item["name"] != interrupted["name"] or
                any(interrupted_row.get(key) != value for key, value in expected_identity.items()) or
                interrupted_row.get("status") != "RUNNING" or interrupted_row.get("finished_at") or
                interrupted_row.get("process") or interrupted_row.get("evidence") or interrupted_row.get("images") or
                (interrupted_folder / "process.json").exists() or (interrupted_folder / "images").exists()):
            raise ValueError("Only the explicitly bound unfinished native-killed case may restart once")
        interrupted_evidence = []
        for path in interrupted_folder.iterdir():
            if path.name not in {"receipt.json", "stdout.log", "stderr.log", "Unreal.log"} or not path.is_file():
                raise ValueError("Unexpected artifact in interrupted case")
            interrupted_evidence.append(flow._file_record(ue_checks._safe_under(interrupted_folder, path.name)))
        evidence.extend(interrupted_evidence)
        recovery_names = {f"case-{index:03d}" for index in range(len(prefix) + 1, len(selected) + 1)}
        if resumed_folder.exists() and any(path.name not in recovery_names for path in resumed_folder.iterdir() if path.is_dir()):
            raise ValueError("Unexpected directory in recovery attempt")
        liveness = None if already_final else _no_active_case(interrupted_folder)
        pending = []
        for index, item in enumerate(selected, 1):
            original_folder = ue_checks._safe_under(folder, f"case-{index:03d}")
            if index > interrupted["index"] and original_folder.exists():
                raise ValueError("A supposedly never-started suffix already has artifacts")
            case_folder = original_folder if index <= len(prefix) else ue_checks._safe_under(resumed_folder, f"case-{index:03d}")
            identity = {**case_binding, **item, "attempt": 1 if index <= len(prefix) else 2}
            if index <= len(prefix) or case_folder.exists():
                if pending:
                    raise ValueError("Recovery case receipts are not a completed contiguous prefix")
                row, record = _complete_case(request, case_folder, identity,
                    prefix[index - 1]["receipt_sha256"] if index <= len(prefix) else None,
                    None if index <= len(prefix) else recovery)
                rows.append(row); evidence.extend(row.get("evidence", [])); evidence.append(record)
            else:
                pending.append((index, item, identity, case_folder))
        if already_final:
            if pending:
                raise ValueError("Final recovery is missing required case receipts")
            flow._check_files(master.get("evidence", []), run_dir)
            return master
        # All existing cases are terminal before any archive, launch or mutation.
        _archive_checkpoint(archive_path, original_raw)
        evidence.append(flow._file_record(archive_path))
        resumed_folder.mkdir(exist_ok=True)
        for index, item, identity, case_folder in pending:
            if flow._verify_inputs(request) != source_sha:
                raise ValueError("Source changed before recovery launch")
            flow._check_files([*originals, *interrupted_evidence], run_dir)
            case_folder.mkdir(exist_ok=False)
            path = case_folder / "receipt.json"
            row = {**identity, "recovery": recovery, "status": "RUNNING", "passed": False,
                   "started_at": flow._now(), "evidence": []}
            write_json(path, row)
            remaining = deadline - time.monotonic()
            if remaining < 1:
                row.update(status="NOT_RUN", reason="recovery_budget_exhausted")
            else:
                try:
                    timeout = min(retest.ITEM_SECONDS, remaining)
                    value = (flow.run_visual_case(request, item["name"], case_folder, timeout) if item["mode"] == "normal"
                             else retest._nullrhi_case(flow, request, item, case_folder, timeout))
                    if value.get("name") != item["name"] or value.get("mode") != item["mode"]:
                        raise ValueError("Recovery returned another case")
                    row.update(value)
                except Exception as error:
                    process_path = case_folder / "process.json"
                    if process_path.is_file():
                        row["process"] = ue_checks._object(process_path)
                    row.update(status="FAIL" if row.get("process") else "NOT_RUN", passed=False,
                               error=f"{type(error).__name__}: {error}")
                    row["evidence"] = [flow._file_record(case_folder / name) for name in
                        ("stdout.log", "stderr.log", "process.json", "Unreal.log") if (case_folder / name).is_file()]
            row["finished_at"] = flow._now(); write_json(path, row)
            row, record = _complete_case(request, case_folder, identity, recovery=recovery)
            rows.append(row); evidence.extend(row.get("evidence", [])); evidence.append(record)
        if flow._verify_inputs(request) != source_sha:
            raise ValueError("Source changed during recovery")
        flow._check_files(evidence, run_dir)
        passed = not unresolved and all(row["passed"] for row in rows)
        terminal = {**checkpoint, "recovery": recovery, "recovery_policy": policy_record,
            "recovery_runner_sha256": flow._sha(__file__), "interrupted_checkpoint": flow._file_record(archive_path),
            "native_killed_evidence": killed, "source_manifest_sha256": source_sha,
            "output_dir": str(resumed_folder),
            "attempt": 2, "composite_execution": True, "interrupted_case": interrupted,
            "original_interrupted_evidence": interrupted_evidence, "interrupted_case_liveness": liveness,
            "status": "passed" if passed else "failed", "passed": passed, "finished_at": flow._now(),
            "rechecked": rows, "planned_count": len(selected), "attempted_count": sum(bool(row.get("process")) for row in rows),
            "resumed_count": len(selected) - len(prefix), "submitted_this_invocation_count": len(pending), "completed_prefix_count": len(prefix),
            "reconciled_tool_false_failure": reconciled, "no_failures": False, "first_pass_unchanged": True,
            "first_pass_evidence_sha256": origin_sha, "unresolved_first_pass": unresolved,
            "still_failed": [row["name"] for row in rows if row["status"] in {"FAIL", "FALSE_PASS"}],
            "newly_passed": [row["name"] for row in rows if row["passed"]],
            "not_run": [row["name"] for row in rows if row["status"] == "NOT_RUN"],
            "evidence": list({row["path"]: row for row in evidence}.values()),
            "scope_note": "One explicitly authorized native-killed case restarted once in attempt-2; prior completed cases and original RUNNING checkpoint remain unchanged."}
        terminal["updated_at"] = terminal["finished_at"]
        write_json(resumed_folder / "receipt.json", terminal)
        write_json(master_path, terminal)
        write_json(run_dir / "facts.json", flow.status(request))
        return terminal


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run(args.request)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if result["passed"] else 1
    except Exception as error:
        print(json.dumps({"passed": False, "error": f"{type(error).__name__}: {error}"}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
