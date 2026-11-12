"""One bounded, exact-name retry of failures from this isolated test run.

Names and modes come only from verified first-pass receipts, never a model
argument. First-pass results are immutable and remain failures in their facts.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import struct
import time

from . import ue_checks
from .storage import lock, write_json

TOTAL_SECONDS = 10800
ITEM_SECONDS = 600


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _evidence_output(flow, row, run):
    records = row.get("evidence", [])
    if not records:
        raise ValueError("First-pass failure has no hashed raw evidence")
    flow._check_files(records, run)
    paths = {str(Path(record["path"])) for record in records}
    process = row.get("process") or {}
    stdout = Path(process.get("stdout_path", ""))
    stderr = Path(process.get("stderr_path", ""))
    if str(stdout) not in paths or str(stderr) not in paths:
        raise ValueError("First-pass process logs are not bound to their receipt")
    unreal = stdout.parent / "Unreal.log"
    selected = unreal if str(unreal) in paths else stdout
    return flow._log_text(selected)


def _first_pass(flow, request, source_sha):
    run = Path(request["run_dir"])
    selected, evidence, unresolved, reconciled = [], [], [], []
    for stage in ("regression", "visual"):
        path = ue_checks._safe_under(run, Path("checks") / stage / "receipt.json")
        receipt = ue_checks._object(path, maximum=16 * 1024 * 1024)
        if (any(receipt.get(key) != request[key] for key in ("id", "task_id", "head_sha", "request_sha256"))
                or receipt.get("stage") != stage or receipt.get("source_manifest_sha256") != source_sha
                or receipt.get("status") not in {"passed", "failed"} or not receipt.get("finished_at")):
            raise ValueError("Retest requires completed first-pass receipts from this exact source/run")
        evidence.append(flow._file_record(path))
        flow._check_files(receipt.get("evidence", []), run)
        evidence.extend(receipt.get("evidence", []))
        if stage == "regression":
            if not receipt.get("process"):
                unresolved.append({"stage": stage, "reason": "first_pass_process_not_run"})
                continue
            output = _evidence_output(flow, receipt, run)
            recorded = receipt.get("automation") or {}
            actual = flow.parse_regression(output, recorded.get("expected_paths"))
            failures = recorded.get("failed_paths")
            if type(failures) is not list or failures != actual["failed_paths"] or len(set(failures)) != len(failures):
                raise ValueError("First-pass failure names disagree with raw automation")
            if "-nullrhi" not in receipt["process"].get("argv", []):
                raise ValueError("Regression first-pass mode is not the fixed NullRHI mode")
            for name in failures:
                if not re.fullmatch(r"ZhuGuoCity\.M[0-9]+(?:\.[A-Za-z0-9_]+)+", name):
                    raise ValueError("Regression failure is not an exact implemented M-suite test")
                if name not in actual["discovered_paths"] or name not in actual["expected_paths"]:
                    raise ValueError("Failed test was not discovered in this first pass")
                selected.append({"name": name, "mode": "nullrhi", "source_stage": stage})
            if (not actual.get("complete") or recorded.get("not_run_paths") or
                    (receipt["status"] == "failed" and not failures)):
                unresolved.append({"stage": stage, "reason": "first_pass_coverage_or_infrastructure_incomplete"})
        else:
            cases = receipt.get("cases")
            if not isinstance(cases, list) or len({row.get("name") for row in cases}) != len(cases):
                raise ValueError("Visual first-pass case identities are missing or duplicate")
            for case_index, case in enumerate(cases, 1):
                if case.get("status") in {"FALSE_PASS", "NOT_RUN"}:
                    unresolved.append({"stage": stage, "name": case.get("name"), "reason": case["status"]})
                    continue
                if case.get("status") == "PASS" and case.get("passed") is True:
                    continue
                if case.get("status") == "FAIL" and not case.get("process"):
                    unresolved.append({"stage": stage, "name": case.get("name"), "reason": "first_pass_process_not_run"})
                    continue
                if case.get("status") != "FAIL" or case.get("passed") is not False:
                    raise ValueError("Unclassified visual first-pass result")
                name = ue_checks._validate_test_name(case.get("name"))
                if not re.fullmatch(r"ZhuGuoCity\.(?:M[0-9]+|Local)(?:\.[A-Za-z0-9_]+)+", name):
                    raise ValueError("Visual failure is not an exact allowed project test")
                output = _evidence_output(flow, case, run)
                evidence.extend(case["evidence"])
                command = case["process"].get("argv", [])
                if (case.get("mode") != "normal" or "-nullrhi" in command or
                        "-ExecCmds=Automation RunTests ^" + name + "$" not in command):
                    raise ValueError("Visual first-pass case mode/name does not match its fixed command")
                actual = ue_checks.parse_automation_output(output, name)
                recorded = case.get("automation") or {}
                # Visual evidence can fail after an automation Success (missing
                # images, RHI, or timeout), so retain that original distinction.
                for key in ("found_counts", "performed_counts", "success_names", "failure_names"):
                    if key in recorded and recorded[key] != actual[key]:
                        raise ValueError("Visual first-pass result disagrees with its raw log")
                correction = _forced_rhi_correction(flow, request, receipt, path, case, case_index, output, actual, source_sha)
                if correction:
                    reconciled.append(correction)
                    evidence.extend(correction["evidence"])
                    continue
                selected.append({"name": name, "mode": "normal", "source_stage": stage})
            if not cases and receipt["status"] == "failed":
                unresolved.append({"stage": stage, "reason": "first_pass_visual_not_run"})
    if len({(row["name"], row["mode"]) for row in selected}) != len(selected):
        raise ValueError("Duplicate first-pass retry identity")
    return selected, list({row["path"]: row for row in evidence}.values()), unresolved, reconciled


def _forced_rhi_correction(flow, request, stage, stage_path, case, index, raw, actual, source_sha):
    """Derive a parser correction only when RHI spelling was the sole failure.

    Original FAIL, process logs and screenshot snapshots stay immutable. A
    missing proof simply leaves the case in the ordinary single-retest path.
    """
    process = case["process"]
    if (case.get("rhi") != {"normal_rhi_proven": False, "expected": "D3D12"} or
            case.get("missing_images") != [] or case.get("skip_lines") != [] or case.get("error") or
            actual.get("passed") is not True or (case.get("automation") or {}).get("passed") is not True or
            process.get("completed") is not True or type(process.get("exit_code")) is not int or process["exit_code"] != 0 or
            process.get("timed_out") is not False or process.get("output_limit_exceeded") is not False or
            not re.search(r"LogRHI: Using Forced RHI: D3D12\b", raw) or not flow._normal_rhi(raw) or
            re.search(r"\bskipped:|(?:LogAutomation\w+|\btest\b|\bautomation\b)[^\r\n]*"
                      r"(?:\bskipp(?:ed|ing)\b|\bnot (?:run|executed)\b)", raw, re.I)):
        return None
    run = Path(request["run_dir"])
    if stage.get("attempt") != 1:
        return None
    folder = ue_checks._safe_under(run, f"checks/visual/attempt-1/case-{index:03d}")
    case_path = ue_checks._safe_under(folder, "receipt.json")
    stage_records = {row["path"]: row for row in stage.get("evidence", [])}
    if (not case_path.is_file() or str(case_path) not in stage_records or
            flow._file_record(case_path) != stage_records[str(case_path)] or
            ue_checks._object(case_path, maximum=16 * 1024 * 1024) != case):
        return None
    command = ue_checks._command(ue_checks._binaries(Path(request["engine_dir"])),
        Path(request["project_dir"]) / "ZhuGuoCity.uproject", {"name": case["name"], "mode": "normal"}, folder / "Unreal.log")
    command.extend(["-d3d12", "-windowed", "-ResX=1280", "-ResY=720"])
    process_path = ue_checks._safe_under(folder, "process.json")
    records = {row["path"]: row for row in case["evidence"]}
    if (process.get("argv") != command or str(process_path) not in records or
            ue_checks._object(process_path) != process):
        return None
    inventory = flow._inventory(request)
    expected = inventory["visual"].get(case["name"])
    images = case.get("images")
    if not isinstance(images, list):
        return None
    if expected is None:
        if case["name"] not in flow.VISUAL_TESTS or images:
            return None
    else:
        if len(images) != len(expected["images"]):
            return None
        for filename, image in zip(expected["images"], images):
            image_path = ue_checks._safe_under(folder, Path("images") / filename)
            if (image.get("path") != str(image_path) or records.get(str(image_path)) != image or
                    image.get("source_relative_path") != (Path("project") / expected["directory"] / filename).as_posix()):
                return None
            with image_path.open("rb") as stream:
                header = stream.read(24)
            if (len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR" or
                    min(struct.unpack(">II", header[16:24])) <= 0 or
                    image.get("dimensions") != list(struct.unpack(">II", header[16:24]))):
                return None
    return {**{key: request[key] for key in ("id", "task_id", "head_sha", "request_sha256")},
            "source_manifest_sha256": source_sha, "name": case["name"], "mode": "normal", "source_stage": "visual",
            "case_index": index, "status": "reconciled_tool_false_failure", "original_status": "FAIL",
            "execution": "not_reexecuted", "passed_after_parser_correction": True,
            "reason": flow.RUNNER_MIGRATION_REASON, "normal_rhi_proven": True,
            "original_visual_receipt": flow._file_record(stage_path), "original_case_receipt": flow._file_record(case_path),
            "automation": actual, "images": images, "evidence": [flow._file_record(case_path), *case["evidence"]]}


def _nullrhi_case(flow, request, item, folder, timeout):
    project = Path(request["project_dir"])
    log = folder / "Unreal.log"
    command = ue_checks._command(ue_checks._binaries(Path(request["engine_dir"])),
                                project / "ZhuGuoCity.uproject", item, log)
    process = flow._execute(command, project, folder, timeout)
    raw = flow._log_text(log) if log.is_file() else flow._log_text(process["stdout_path"])
    facts = ue_checks.parse_automation_output(raw, item["name"])
    passed = process["completed"] and process["exit_code"] == 0 and facts["passed"]
    return {"name": item["name"], "mode": item["mode"], "passed": passed,
            "status": "PASS" if passed else "FAIL", "process": process, "automation": facts,
            "evidence": [flow._file_record(folder / name) for name in
                         ("stdout.log", "stderr.log", "process.json", "Unreal.log") if (folder / name).is_file()]}


def run(request: dict, folder: Path, started_epoch: float) -> dict:
    from . import full_flow_checks as flow

    if not isinstance(started_epoch, (int, float)) or not math.isfinite(started_epoch):
        raise ValueError("Retest start time must be finite")
    run_dir = Path(request["run_dir"])
    if flow.load_request(run_dir / "request.json") != request:
        raise ValueError("Retest request differs from the immutable operator request")
    expected_folder = ue_checks._safe_under(run_dir, "checks/retest/attempt-1")
    if Path(folder).absolute() != expected_folder.absolute() or not expected_folder.is_dir():
        raise ValueError("Retest output must be this run's first fixed retest attempt")
    started = time.monotonic()
    initial_budget = max(0.0, min(TOTAL_SECONDS, started_epoch + TOTAL_SECONDS - time.time()))
    with lock(expected_folder / "retest.lock"):
        source_sha = flow._verify_inputs(request)
        selected, original_evidence, unresolved, reconciled = _first_pass(flow, request, source_sha)
        origin_sha = _digest(original_evidence)
        binding = {key: request[key] for key in ("id", "task_id", "head_sha", "request_sha256")}
        binding.update(source_manifest_sha256=source_sha, first_pass_evidence_sha256=origin_sha)
        rows, evidence = [], list(original_evidence)
        for correction in reconciled:
            path = ue_checks._safe_under(expected_folder, f"reconciliations/case-{correction['case_index']:03d}.json")
            value = {**binding, **correction, "parser_runner_sha256": flow._sha(flow.__file__),
                     "retest_runner_sha256": flow._sha(__file__)}
            if path.exists():
                if ue_checks._object(path, maximum=16 * 1024 * 1024) != value:
                    raise ValueError("Existing parser correction has different evidence or source")
            else:
                write_json(path, value)
            correction["derived_receipt"] = flow._file_record(path)
            evidence.append(correction["derived_receipt"])
        for index, item in enumerate(selected, 1):
            case_folder = ue_checks._safe_under(expected_folder, f"case-{index:03d}")
            path = ue_checks._safe_under(case_folder, "receipt.json")
            identity = {**binding, **item, "attempt": 1}
            if path.exists():
                cached = ue_checks._object(path, maximum=16 * 1024 * 1024)
                if any(cached.get(key) != value for key, value in identity.items()):
                    raise ValueError("Existing single-test retest has a different source or identity")
                flow._check_files(cached.get("evidence", []), run_dir)
                if cached.get("status") == "RUNNING":
                    cached.update(status="NOT_RUN", passed=False, reason="interrupted_retest_not_repeated", finished_at=flow._now())
                    write_json(path, cached)
                elif cached.get("status") not in {"PASS", "FAIL", "NOT_RUN", "FALSE_PASS"} or not cached.get("finished_at"):
                    raise ValueError("Existing retest has no valid terminal status")
                rows.append(cached)
                evidence.extend(cached.get("evidence", [])); evidence.append(flow._file_record(path))
                continue
            case_folder.mkdir(parents=True, exist_ok=False)
            remaining = initial_budget - (time.monotonic() - started)
            if remaining < 1:
                row = {**identity, "status": "NOT_RUN", "passed": False, "reason": "total_budget_exhausted",
                       "finished_at": flow._now(), "evidence": []}
            else:
                if flow._verify_inputs(request) != source_sha:
                    raise ValueError("Isolated source changed before the single-test retry")
                flow._check_files(original_evidence, run_dir)
                row = {**identity, "status": "RUNNING", "passed": False, "started_at": flow._now(), "evidence": []}
                write_json(path, row)  # Never implicitly repeat a possibly submitted process.
                try:
                    timeout = min(ITEM_SECONDS, remaining)
                    result = (flow.run_visual_case(request, item["name"], case_folder, timeout)
                              if item["mode"] == "normal" else _nullrhi_case(flow, request, item, case_folder, timeout))
                    if (result.get("name") != item["name"] or result.get("mode") != item["mode"] or
                            result.get("status") not in {"PASS", "FAIL", "FALSE_PASS", "NOT_RUN"}):
                        raise ValueError("Single-test runner returned another test or an invalid status")
                    row.update(result)
                except Exception as error:
                    process_path = ue_checks._safe_under(case_folder, "process.json")
                    if process_path.is_file():
                        row["process"] = ue_checks._object(process_path)
                    row.update(status="FAIL" if row.get("process") else "NOT_RUN", passed=False,
                               error=f"{type(error).__name__}: {error}")
                    row["evidence"] = [flow._file_record(case_folder / name) for name in
                                       ("stdout.log", "stderr.log", "process.json", "Unreal.log") if (case_folder / name).is_file()]
                row["finished_at"] = flow._now()
            write_json(path, row)
            rows.append(row)
            evidence.extend(row.get("evidence", [])); evidence.append(flow._file_record(path))
            if flow._verify_inputs(request) != source_sha:
                raise ValueError("Isolated source changed during retest; remaining retries were not started")
        flow._check_files(original_evidence, run_dir)
        return {"passed": not unresolved and all(row.get("passed") is True for row in rows),
                "rechecked": rows, "planned_count": len(selected),
                "reconciled_tool_false_failure": reconciled,
                "attempted_count": sum(bool(row.get("process")) for row in rows),
                "no_failures": not selected and not unresolved and not reconciled, "first_pass_unchanged": True,
                "first_pass_evidence_sha256": origin_sha, "unresolved_first_pass": unresolved,
                "still_failed": [row["name"] for row in rows if row["status"] in {"FAIL", "FALSE_PASS"}],
                "newly_passed": [row["name"] for row in rows if row["passed"] is True],
                "not_run": [row["name"] for row in rows if row["status"] == "NOT_RUN"],
                "evidence": list({row["path"]: row for row in evidence}.values()),
                "scope_note": "Retest outcomes do not replace any first-pass failure or prove unrun coverage."}
