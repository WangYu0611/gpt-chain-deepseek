"""Fixed complete checks for one explicitly authorized repair, never a model-made command."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from unattended import full_flow_checks as flow, gitops, ue_checks
from unattended.storage import lock, write_json
from unattended.user_repair import load_repair_request, validate_repair_profile

CHECK_ID = "ue-full-flow-repair"
BUDGET = 21600
STAGE_BUDGETS = {"build": 1800, "regression": 10800, "visual": 5400, "package": 3600}
PACKAGE = "ZhuGuoCity.M2.PackageIntegration"
SCOPE = "user_repair_check"
SELF_SCOPE = "user_repair_selftest"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _safe(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    if not path.is_relative_to(root):
        raise ValueError("Evidence escaped its fixed directory")
    return ue_checks._safe_under(root, path.relative_to(root))


def _object(path, limit=32 * 1024 * 1024):
    return ue_checks._object(Path(path), maximum=limit)


def _snapshot(workspace):
    """Also cover Tools and new source files; generated ignored outputs are not inputs."""
    head = gitops.head(workspace)
    status = gitops.git(workspace, "status", "--porcelain=v1", "-z")
    names = sorted(set(filter(None, gitops.git(workspace, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0"))))
    if len(names) > 100000:
        raise ValueError("Source snapshot exceeds its file-count bound")
    inputs = {}
    for name in names:
        path = ue_checks._safe_under(workspace, name)
        if not path.is_file():
            inputs[name] = {"missing": True}
            continue
        fact = ue_checks._fingerprint(path)
        inputs[name] = {"sha256": fact["sha256"], "size_bytes": fact["size"]}
    if gitops.head(workspace) != head or gitops.git(workspace, "status", "--porcelain=v1", "-z") != status:
        raise ValueError("Git inputs changed while being snapshotted")
    return {"head_sha": head, "clean": not bool(status), "status_sha256": hashlib.sha256(status.encode()).hexdigest(),
            "inputs": inputs, "inputs_sha256": _digest(inputs)}


def _runners():
    return {path.name: _sha(path) for path in (Path(__file__), Path(flow.__file__), Path(ue_checks.__file__))}


def _inventory(request, workspace):
    row = next((row for row in request["evidence"] if row["role"] == "source_inventory"), None)
    if row is None:
        raise ValueError("Full repair requires the authorized original inventory")
    value = _object(row["path"])
    minimum = value.get("automation_inventory", value)
    original = _object(Path(request["source_full_flow_run"]) / "source-manifest.json")["automation_inventory"]
    if minimum != original:
        raise ValueError("Authorized inventory differs from the original full-flow source manifest")
    current = flow.source_inventory(workspace)
    for kind in ("regression", "visual"):
        old, new = set(minimum[kind]), set(current[kind])
        if not old or not old <= new:
            raise ValueError("Current source removed or renamed required " + kind + " tests: " + ", ".join(sorted(old - new)))
    for name, expectation in minimum["visual"].items():
        if len(current["visual"][name]["images"]) < len(expectation["images"]):
            raise ValueError("Current visual reduced its original screenshot coverage: " + name)
    if PACKAGE not in current["regression"]:
        raise ValueError("Formal PackageIntegration is missing")
    return current


def _context(runtime, workspace, engine, self_test):
    runtime, workspace, engine = map(lambda p: Path(p).absolute(), (runtime, workspace, engine))
    if workspace != runtime / "workspace" or any(ue_checks._is_reparse(p) for p in (runtime, *runtime.parents)):
        raise ValueError("A managed non-linked workspace is required")
    _safe(runtime, workspace)
    state = _object(_safe(runtime, runtime / "state.json"))
    task, op = state.get("task"), state.get("operation")
    if not isinstance(task, dict) or not isinstance(op, dict):
        raise ValueError("An active task and operation are required")
    profile = validate_repair_profile(task, runtime)
    if profile is None:
        raise ValueError("Full repair is not authorized for this task")
    request = load_repair_request(Path(profile["request_path"]), runtime)
    full = _object(Path(request["source_full_flow_run"]) / "request.json")
    if ue_checks._binaries(engine) != ue_checks._binaries(Path(full["engine_dir"])):
        raise ValueError("Engine differs from the authorized full-flow engine")
    head = gitops.head(workspace)
    if op.get("task_id") != task["id"] or not op.get("output_dir"):
        raise ValueError("Current operation belongs to another task")
    out = _safe(runtime, Path(op["output_dir"]))
    if out.parent != runtime / "runs":
        raise ValueError("Current operation output is outside managed runs")
    if self_test:
        if (state["phase"] != "CODE" or op.get("kind") != "code" or
                op.get("user_repair_id") != profile["id"] or op.get("user_repair_request_sha256") != profile["request_sha256"] or
                op.get("requested_session_id") != task.get("session_id")):
            raise ValueError("Self-test requires this authorized CODE operation")
    elif (state["phase"] != "CHECK" or op.get("kind") != "check" or op.get("check_id") != CHECK_ID or
          op.get("head_sha") != head or task.get("candidate", {}).get("head_sha") != head or not gitops.clean(workspace)):
        raise ValueError("Independent repair check requires its exact clean candidate CHECK operation")
    return runtime, workspace, engine, task, op, profile, request, head


def _record(path, run):
    path = _safe(run, path)
    return {"relative_path": path.relative_to(run).as_posix(), "sha256": _sha(path), "size_bytes": path.stat().st_size}


def _copy_artifact(workspace, run, source, destination, started, *, image=False):
    source, destination = _safe(workspace, source), _safe(run, destination)
    if not source.is_file() or source.stat().st_mtime < started:
        raise ValueError("Artifact is missing or stale: " + str(source))
    fingerprint = ue_checks._fingerprint(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    record = _record(destination, run)
    if record["sha256"] != fingerprint["sha256"]:
        raise ValueError("Artifact changed during archival")
    record.update(source_relative_path=source.relative_to(workspace).as_posix(), source_mtime_ns=fingerprint["mtime_ns"])
    if image:
        dimensions = fingerprint.get("png_dimensions")
        if dimensions is None:
            raise ValueError("Expected an actual PNG")
        record["dimensions"] = [dimensions["width"], dimensions["height"]]
    return record


def _argv(stage, engine, workspace, folder, name=None):
    project = ue_checks._project(workspace)
    if stage == "build":
        return ue_checks._clean_build_command(engine, project, folder / "UnrealBuildTool.log")
    mode = "normal" if stage == "visual" else "nullrhi"
    argv = ue_checks._command(ue_checks._binaries(engine), project,
             {"name": name or PACKAGE, "mode": mode}, folder / "Unreal.log", milestone_regression=stage == "regression")
    if mode == "normal":
        argv += ["-d3d12", "-windowed", "-ResX=1280", "-ResY=720"]
    return argv


def _raw(folder, stage):
    log = folder / ("UnrealBuildTool.log" if stage == "build" else "Unreal.log")
    return flow._log_text(log if log.is_file() else folder / "stdout.log")


def _process_pass(process):
    return (type(process.get("exit_code")) is int and process["exit_code"] == 0 and process.get("completed") is True
            and process.get("timed_out") is False and process.get("output_limit_exceeded") is False)


def _parse(stage, raw, process, inventory, name=None):
    value = {"passed": _process_pass(process)}
    if stage == "build":
        value["build_success_marker"] = "Result: Succeeded" in raw
        value["passed"] &= value["build_success_marker"]
    elif stage == "regression":
        facts = flow.parse_regression(raw, inventory["regression"])
        # Keep the genuine prefix skip visible. Only formal package success
        # can close this one deferred coverage item in the overall result.
        other_skips = [line for line in _test_skips(raw)
                       if "skipped: package integration runs only when its exact test is selected" not in line]
        value.update(automation=facts, deferred_to_package=PACKAGE if facts["package_skipped"] else None, unexpected_skip_lines=other_skips)
        value["passed"] &= facts["complete"] and not facts["failed_paths"] and not facts["not_run_paths"] and not other_skips
        value["passed"] &= not re.search(r"Fatal error|Result=\{(?:Fail|Failed|Failure)\}", raw, re.I)
    else:
        facts = ue_checks.parse_automation_output(raw, name or PACKAGE)
        skips = _test_skips(raw)
        value.update(automation=facts, skip_lines=skips)
        value["passed"] &= facts["passed"] and not skips
        if stage == "visual":
            value["normal_rhi_proven"] = flow._normal_rhi(raw)
            value["passed"] &= value["normal_rhi_proven"]
        else:
            value["formal_package_marker"] = "package integration PASS: exit=0" in raw
            value["passed"] &= value["formal_package_marker"]
    value["passed"] = bool(value["passed"])
    return value


def _test_skips(raw):
    # Shader/Python/RHI initialization legitimately skips caches or optional
    # capabilities. Only automation skip evidence describes test coverage.
    return [line.strip() for line in raw.splitlines()
            if re.search(r"\b(?:skipped|skipping|not run|not executed)\b", line, re.I)
            and ("LogAutomation" in line or re.match(r"\s*skipped:", line, re.I))]


def _versions(workspace):
    header = (workspace / "Source/ZhuGuoCity/ZgSave.h").read_text(encoding="utf8")
    tools = (workspace / "Tools/package_verification.py").read_text(encoding="utf8")
    version = re.findall(r"\bCurrentStructureVersion\s*=\s*(\d+)\s*;", header)
    tool_version = re.findall(r"(?m)^EXPECTED_STRUCTURE_VERSION\s*=\s*(\d+)\s*$", tools)
    tool_content = re.findall(r'''(?m)^EXPECTED_CONTENT_VERSION\s*=\s*["']([^"']+)["']\s*$''', tools)
    content = _object(workspace / "Content/Data/ZgDefinitions/resources.json").get("contentVersion")
    if len(version) != 1 or tool_version != version or tool_content != [content] or not isinstance(content, str) or not content:
        raise ValueError("Current save/definitions/package contracts do not agree")
    return {"structureVersion": int(version[0]), "contentVersion": content}


def _package_archive(workspace, run, folder, head, started):
    root = folder / "package-evidence"
    summary = _copy_artifact(workspace, run, workspace / "Saved/Evidence/TASK-LOCAL-0066/package-summary.json",
                             root / "package-summary.json", started)
    data = _object(run / summary["relative_path"])
    if data.get("status") != "passed" or data.get("head_sha") != head or Path(data.get("workspace", "")).absolute() != workspace:
        raise ValueError("Formal package summary differs from this workspace/HEAD")
    images, artifacts = [], [summary]
    for name in ("plain", "loop"):
        if data["runs"][name].get("passed") is not True:
            raise ValueError("Formal packaged scenario did not pass")
        facts = data["runs"][name]["facts"]
        image = _copy_artifact(workspace, run, Path(facts["screenshot"]["path"]), root / (name + ".png"), started, image=True)
        if image["sha256"] != facts["screenshot"]["sha256"] or name == "plain" and image["dimensions"] != [1280, 720]:
            raise ValueError("Formal package screenshot differs from summary or dimensions")
        images.append(image)
        # Tools already archives the external packaged-run log back into its
        # workspace evidence and binds that copy by SHA. Read that exact copy;
        # never broaden this checker to an arbitrary runtime path in facts.log.
        log_record = data["runs"][name]["game_log_copy"]
        log = _copy_artifact(workspace, run, Path(log_record["path"]), root / (name + ".log"), started)
        if log["sha256"] != log_record["sha256"]:
            raise ValueError("Packaged log copy differs from the formal summary")
        artifacts.append(log)
    loop = data["runs"]["loop"]["facts"]
    source = Path(loop["save"]["path"])
    if source.name not in {"Slot97.zgs", "loop-Slot97.zgs"}:
        raise ValueError("Packaged save is not Slot97")
    save = _copy_artifact(workspace, run, source, root / "loop-Slot97.zgs", started)
    if save["sha256"] != loop["save"]["sha256"]:
        raise ValueError("Formal packaged save differs from summary")
    artifacts.append(save)
    result = {"summary": summary, "images": images, "artifacts": artifacts}
    _verify_package_archive(workspace, run, result, head)
    return result


def _verify_package_archive(workspace, run, package, head):
    root = run / "package/package-evidence"
    data = _object(root / "package-summary.json")
    if data.get("status") != "passed" or data.get("head_sha") != head or Path(data.get("workspace", "")).absolute() != workspace:
        raise ValueError("Archived package summary is not this clean HEAD")
    for name in ("plain", "loop"):
        if _sha(root / (name + ".png")) != data["runs"][name]["facts"]["screenshot"]["sha256"]:
            raise ValueError("Archived package image differs from summary")
        if data["runs"][name].get("passed") is not True or _sha(root / (name + ".log")) != data["runs"][name]["game_log_copy"]["sha256"]:
            raise ValueError("Archived packaged log/scenario differs from summary")
    if ue_checks._fingerprint(root / "plain.png")["png_dimensions"] != {"width": 1280, "height": 720}:
        raise ValueError("Archived plain screenshot dimensions differ")
    loop = data["runs"]["loop"]["facts"]
    compared = re.search(r"\bcompared=(\d+)", loop.get("summary", ""))
    if not compared or int(compared[1]) < 300:
        raise ValueError("Packaged comparison has fewer than 300 steps")
    loop_log = flow._log_text(root / "loop.log")
    # The game's verifier preserves the CR captured before a Windows newline
    # in its JSON fact, while text-mode log reads normalize CRLF to LF.
    summary_line = loop["summary"].rstrip("\r\n")
    if ("[ZgPackageLoopDemo] SUMMARY PASS " + summary_line not in loop_log or
            "[ZgPackageLoopDemo] SUMMARY FAIL" in loop_log):
        raise ValueError("Packaged loop summary is not proved by its archived native log")
    save = root / "loop-Slot97.zgs"
    if _sha(save) != loop["save"]["sha256"]:
        raise ValueError("Archived Slot97 differs from summary")
    with save.open(encoding="utf-8-sig") as stream:
        magic, line = stream.readline(100), stream.readline(65536)
    header = json.loads(line)
    if magic.strip() != "ZGSAVE1" or any(header.get(key) != value for key, value in _versions(workspace).items()):
        raise ValueError("Archived Slot97 does not satisfy the current save/definitions contract")
    package.update(passed=True, save_header=header, slot=97, compared_steps=int(compared[1]))


def _execute_stage(stage, workspace, engine, run, folder, inventory, timeout, name=None):
    folder.mkdir(parents=True, exist_ok=False)
    started = time.time()
    row = {"stage": stage, "name": name, "status": "running", "passed": False, "started_at": _now(), "images": []}
    write_json(folder / "receipt.json", row)
    try:
        process = flow._execute(_argv(stage, engine, workspace, folder, name), workspace, folder, timeout)
        row.update(process=process, **_parse(stage, _raw(folder, stage), process, inventory, name))
        if stage == "visual":
            expected = inventory["visual"][name]
            for filename in expected["images"]:
                row["images"].append(_copy_artifact(workspace, run,
                    ue_checks._safe_under(workspace, Path(expected["directory"]) / filename),
                    folder / "images" / filename, started, image=True))
        elif stage == "package" and row["passed"]:
            row["package"] = _package_archive(workspace, run, folder, gitops.head(workspace), started)
            row["images"] = row["package"]["images"]
    except Exception as error:
        row.update(passed=False, error=f"{type(error).__name__}: {error}")
    if stage == "package" and not row["passed"]:
        row["failure_artifacts"] = []
        for name in ("package-summary.json", "package-uat.log"):
            source = workspace / "Saved/Evidence/TASK-LOCAL-0066" / name
            try:
                row["failure_artifacts"].append(_copy_artifact(workspace, run, source,
                    folder / "package-evidence" / ("raw-" + name), started))
            except (OSError, ValueError):
                pass  # No fresh file means no invented failure evidence.
    row.update(status="passed" if row["passed"] else "failed", finished_at=_now())
    write_json(folder / "receipt.json", row)
    return row


def _mirror(workspace, task_id, self_test):
    return ue_checks._safe_under(workspace, Path("Saved/Automation") / task_id /
                                ("selftest-results.json" if self_test else "full-repair-results.json"))


def run(runtime, workspace, engine, *, self_test=False):
    ctx = _context(runtime, workspace, engine, self_test)
    runtime, workspace, engine, task, op, profile, request, head = ctx
    parent = _safe(runtime, runtime / "tasks" / task["id"] / "full-repair-checks")
    with lock(parent / "runner.lock"):
        # Holding the checker lock never acquires the worker's orchestration lock.
        ctx = _context(runtime, workspace, engine, self_test)
        if ctx[4] != op or ctx[5] != profile:
            raise ValueError("Current repair operation changed before launch")
        ident = uuid.uuid4().hex
        run_dir = _safe(parent, parent / ident); run_dir.mkdir(parents=True, exist_ok=False)
        receipt_path = run_dir / "receipt.json"
        receipt = {"version": 1, "id": ident, "task_id": task["id"], "authorization_id": profile["id"],
                   "request_sha256": profile["request_sha256"], "head_sha": head, "scope": SELF_SCOPE if self_test else SCOPE,
                   "engine_dir": str(engine), "workspace": str(workspace), "operation": op,
                   "status": "running", "passed": False, "started_at": _now(), "stages": {}, "evidence": [], "images": [],
                   "runner_sha256": _runners()}
        write_json(receipt_path, receipt)
        mirror = _mirror(workspace, task["id"], self_test)
        def publish():
            receipt["updated_at"] = _now()
            write_json(receipt_path, receipt)
            write_json(mirror, {key: receipt[key] for key in ("version", "id", "task_id", "authorization_id", "request_sha256",
                       "head_sha", "scope", "status", "passed", "updated_at")} | {"receipt_path": str(receipt_path), "receipt_sha256": _sha(receipt_path)})
        publish()
        deadline = time.monotonic() + BUDGET
        try:
            before = _snapshot(workspace)
            if before["head_sha"] != head or not self_test and not before["clean"]:
                raise ValueError("Source differs from the candidate before execution")
            inventory = _inventory(request, workspace)
            receipt["inventory"] = inventory
            write_json(run_dir / "source-before.json", before)
            receipt["source_before"] = {key: before[key] for key in ("head_sha", "clean", "inputs_sha256", "status_sha256")}
            publish()
            for stage in ("build", "regression", "visual", "package"):
                print(json.dumps({"stage": stage, "status": "running", "scope": receipt["scope"]}), flush=True)
                if stage != "build" and not receipt["stages"]["build"]["passed"]:
                    receipt["stages"][stage] = {"passed": False, "status": "not_run", "reason": "Editor build failed"}
                    continue
                remaining = min(STAGE_BUDGETS[stage], deadline - time.monotonic())
                if remaining <= 0:
                    receipt["stages"][stage] = {"passed": False, "status": "not_run", "reason": "Total check deadline exhausted"}
                elif stage == "visual":
                    cases, stage_end = [], time.monotonic() + remaining
                    for index, name in enumerate(inventory["visual"], 1):
                        print(json.dumps({"stage": "visual", "name": name, "index": index,
                                          "total": len(inventory["visual"]), "status": "running"}), flush=True)
                        budget = min(600, stage_end - time.monotonic(), deadline - time.monotonic())
                        if budget <= 0:
                            cases.append({"name": name, "passed": False, "status": "not_run", "reason": "Visual deadline exhausted"})
                        else:
                            cases.append(_execute_stage(stage, workspace, engine, run_dir, run_dir / stage / f"case-{index:03}", inventory, budget, name))
                    receipt["stages"][stage] = {"cases": cases, "passed": all(row["passed"] for row in cases),
                        "status": "passed" if all(row["passed"] for row in cases) else "failed"}
                else:
                    receipt["stages"][stage] = _execute_stage(stage, workspace, engine, run_dir, run_dir / stage, inventory, remaining)
                if _snapshot(workspace) != before:
                    raise ValueError("Source inputs changed during complete repair checks")
                publish()
            after = _snapshot(workspace)
            write_json(run_dir / "source-after.json", after)
            receipt["source_after"] = {key: after[key] for key in ("head_sha", "clean", "inputs_sha256", "status_sha256")}
            if before != after or _runners() != receipt["runner_sha256"]:
                raise ValueError("Source or trusted runner changed while checking")
            if _context(runtime, workspace, engine, self_test)[4] != op:
                raise ValueError("Owning operation changed while checking")
            receipt["passed"] = all(receipt["stages"].get(stage, {}).get("passed") for stage in STAGE_BUDGETS)
        except Exception as error:
            receipt.update(passed=False, error=f"{type(error).__name__}: {error}")
        receipt.update(status="passed" if receipt["passed"] else "failed", finished_at=_now())
        # Archive generated proof only. The mutable workspace's originals are
        # never part of the durable evidence list.
        receipt["evidence"] = [_record(path, run_dir) for path in sorted(run_dir.rglob("*")) if path.is_file() and path != receipt_path]
        receipt["images"] = [row for stage in receipt["stages"].values() for case in stage.get("cases", [stage]) for row in case.get("images", [])]
        publish()
        regression = receipt["stages"].get("regression", {}).get("automation", {})
        visual = receipt["stages"].get("visual", {}).get("cases", [])
        print(json.dumps({"receipt_path": str(receipt_path), "receipt_sha256": _sha(receipt_path),
            "scope": receipt["scope"], "head_sha": head, "passed": receipt["passed"],
            "regression": {"expected": regression.get("expected_count"), "successful": regression.get("successful"),
                           "failed": regression.get("failed"), "deferred_package_skip": regression.get("package_skipped")},
            "visual": {"expected": len(receipt.get("inventory", {}).get("visual", {})),
                       "successful": sum(row.get("passed") is True for row in visual),
                       "failed_or_not_run": sum(row.get("passed") is not True for row in visual)},
            "image_count": len(receipt["images"])}), flush=True)
        print(("UE_FULL_FLOW_REPAIR_SELFTEST_" if self_test else "UE_FULL_FLOW_REPAIR_") + ("PASSED" if receipt["passed"] else "FAILED"), flush=True)
        return receipt


def verify_repair_check(runtime, workspace, task, head_sha):
    """Re-derive success from immutable raw proofs; a JSON passed flag is insufficient."""
    runtime, workspace = Path(runtime).absolute(), Path(workspace).absolute()
    if workspace != runtime / "workspace":
        raise ValueError("Repair verification requires the managed workspace")
    profile = validate_repair_profile(task, runtime)
    if profile is None:
        raise ValueError("Missing active repair authorization")
    request = load_repair_request(Path(profile["request_path"]), runtime)
    mirror = _object(_mirror(workspace, task["id"], False))
    parent = runtime / "tasks" / task["id"] / "full-repair-checks"
    path = _safe(parent, Path(mirror["receipt_path"]))
    if path.name != "receipt.json" or path.parent.parent != parent or not re.fullmatch(r"[0-9a-f]{32}", path.parent.name):
        raise ValueError("Invalid fixed repair receipt location")
    if _sha(path) != mirror.get("receipt_sha256"):
        raise ValueError("Repair receipt changed after mirror publication")
    receipt, run_dir = _object(path), path.parent
    expected = {"version": 1, "id": run_dir.name, "task_id": task["id"], "authorization_id": profile["id"],
                "request_sha256": profile["request_sha256"], "head_sha": head_sha, "scope": SCOPE}
    if any(receipt.get(k) != v or mirror.get(k) != v for k, v in expected.items()):
        raise ValueError("Repair receipt is not this authorization, scope and HEAD")
    if receipt.get("status") != "passed" or receipt.get("passed") is not True or not receipt.get("finished_at"):
        raise ValueError("Complete repair checks did not pass")
    op = receipt.get("operation", {})
    if (op.get("kind") != "check" or op.get("check_id") != CHECK_ID or op.get("task_id") != task["id"] or
            op.get("head_sha") != head_sha or mirror.get("status") != "passed" or mirror.get("passed") is not True):
        raise ValueError("Saved receipt is not an independent complete CHECK")
    if Path(receipt["workspace"]).absolute() != workspace or receipt.get("runner_sha256") != _runners():
        raise ValueError("Repair workspace or trusted runner binding differs")
    rows = receipt.get("evidence", [])
    if not rows or len({r["relative_path"] for r in rows}) != len(rows):
        raise ValueError("Repair evidence is missing or duplicated")
    indexed = {}
    for row in rows:
        evidence = ue_checks._safe_under(run_dir, row["relative_path"])
        if not evidence.is_file() or evidence.stat().st_size != row["size_bytes"] or _sha(evidence) != row["sha256"]:
            raise ValueError("Repair archived evidence changed")
        indexed[row["relative_path"]] = row
    actual = {p.relative_to(run_dir).as_posix() for p in run_dir.rglob("*") if p.is_file() and p != path}
    if set(indexed) != actual:
        raise ValueError("Repair evidence manifest does not bind every archived proof")
    before, after, current = _object(run_dir / "source-before.json"), _object(run_dir / "source-after.json"), _snapshot(workspace)
    if before != after or current != before or not current["clean"] or current["head_sha"] != head_sha:
        raise ValueError("Repair source snapshot differs from current clean HEAD")
    inventory = _inventory(request, workspace)
    if receipt.get("inventory") != inventory:
        raise ValueError("Repair coverage inventory changed")
    engine = Path(receipt["engine_dir"])
    original = _object(Path(request["source_full_flow_run"]) / "request.json")
    if ue_checks._binaries(engine) != ue_checks._binaries(Path(original["engine_dir"])):
        raise ValueError("Repair engine binding changed")
    for stage in STAGE_BUDGETS:
        stage_row = receipt["stages"][stage]
        cases = stage_row["cases"] if stage == "visual" else [stage_row]
        if stage == "visual" and [row.get("name") for row in cases] != list(inventory["visual"]):
            raise ValueError("Exact visual coverage differs")
        for index, row in enumerate(cases, 1):
            folder = run_dir / stage / f"case-{index:03}" if stage == "visual" else run_dir / stage
            process = _object(folder / "process.json")
            if (row.get("process") != process or process.get("argv") != _argv(stage, engine, workspace, folder, row.get("name")) or
                    Path(process.get("cwd", "")).absolute() != workspace):
                raise ValueError("Repair execution command/receipt differs")
            parsed = _parse(stage, _raw(folder, stage), process, inventory, row.get("name"))
            if not parsed["passed"] or any(row.get(k) != v for k, v in parsed.items()):
                raise ValueError("Repair raw execution does not prove passed coverage")
            if stage == "visual":
                wanted = inventory["visual"][row["name"]]["images"]
                if [Path(image["relative_path"]).name for image in row["images"]] != wanted:
                    raise ValueError("Visual screenshots are incomplete")
                for image in row["images"]:
                    relative = image["relative_path"]
                    if relative not in indexed or indexed[relative]["sha256"] != image["sha256"]:
                        raise ValueError("Visual image lacks bound evidence")
                    if ue_checks._fingerprint(run_dir / relative).get("png_dimensions") != {
                            "width": image["dimensions"][0], "height": image["dimensions"][1]}:
                        raise ValueError("Visual image dimensions differ")
            elif stage == "package":
                _verify_package_archive(workspace, run_dir, dict(row["package"]), head_sha)
    return {"passed": True, "receipt_path": str(path), "receipt_sha256": _sha(path), "scope": SCOPE,
            "head_sha": head_sha, "authorization_id": profile["id"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("runtime", "workspace", "engine"):
        parser.add_argument("--" + key, required=True, type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(args.runtime, args.workspace, args.engine, self_test=args.self_test)
        return 0 if result["passed"] else 1
    except Exception as error:
        print(("UE_FULL_FLOW_REPAIR_SELFTEST_FAILED" if args.self_test else "UE_FULL_FLOW_REPAIR_FAILED") +
              ": " + type(error).__name__ + ": " + str(error), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
