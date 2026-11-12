"""Fixed, isolated full-game checks invoked by the authorized read-only planner.

This runner never updates the development task or its acceptance. The request
selects one committed snapshot, not commands, executable paths, or test subsets.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
import time
import struct

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from github_harness_lab.lab_harness.process import run_process, safe_environment
from unattended import gitops, ue_checks
from unattended.storage import lock, write_json

REQUIRED_STAGES = ("prepare", "build", "regression", "visual", "package")
STAGES = (*REQUIRED_STAGES, "retest", "status")
BUDGETS = {"prepare": 300, "build": 1800, "regression": 10800, "visual": 1800, "package": 3600}
OUTPUT_LIMIT = 128 * 1024 * 1024
PACKAGE_TEST = "ZhuGuoCity.M2.PackageIntegration"
VISUAL_TESTS = ["ZhuGuoCity.M3.DeathFlow", "ZhuGuoCity.M3.DeathHud", "ZhuGuoCity.M3.DeathSave"]
REGRESSION_SELECTOR = "^ZhuGuoCity.M"
RUNNER_MIGRATION_REASON = "recognize_forced_d3d12_rhi_and_reconcile_visual_parser_false_failures"
_REGISTRATION = re.compile(r'IMPLEMENT_(?:SIMPLE|COMPLEX)_AUTOMATION_TEST\s*\(\s*\w+\s*,\s*"(ZhuGuoCity\.[^"]+)"')


def request_hash(request: dict) -> str:
    body = {key: value for key, value in request.items() if key != "request_sha256"}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _absolute(value, field):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError(field + " must be an absolute path")
    path = Path(value)
    return ue_checks._safe_under(Path(path.anchor), Path(*path.parts[1:]))


def load_request(path: Path) -> dict:
    path = _absolute(str(path), "request")
    request = ue_checks._object(path, maximum=65536)
    required = {"version", "id", "task_id", "head_sha", "source_workspace", "runtime_dir", "run_dir",
                "project_dir", "engine_dir", "authorized_at", "request_sha256", "model", "effort"}
    if set(request) != required or type(request["version"]) is not int or request["version"] != 1:
        raise ValueError("Full-flow request fields/version do not match the fixed schema")
    if any(not isinstance(request[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", request[key])
           for key in ("id", "task_id")):
        raise ValueError("Invalid full-flow identity")
    if not isinstance(request["head_sha"], str) or not re.fullmatch(r"[0-9a-f]{40}", request["head_sha"]):
        raise ValueError("An exact Git SHA is required")
    if request["model"] != "claude-opus-5-5" or request["effort"] != "high":
        raise ValueError("This authorized run requires Claude Opus 5.5/high")
    if request["request_sha256"] != request_hash(request):
        raise ValueError("Canonical request SHA does not match")
    if datetime.fromisoformat(request["authorized_at"]).utcoffset() is None:
        raise ValueError("authorized_at must include a timezone")
    paths = {key: _absolute(request[key], key) for key in
             ("source_workspace", "runtime_dir", "run_dir", "project_dir", "engine_dir")}
    run, source, runtime = paths["run_dir"], paths["source_workspace"], paths["runtime_dir"]
    if (run != runtime / "full-flow" / "runs" / request["id"] or path != run / "request.json" or
            paths["project_dir"] != run / "project" or run.is_relative_to(source) or source.is_relative_to(run)):
        raise ValueError("Full-flow paths do not describe an isolated, authorized run")
    if paths["engine_dir"].name != "Engine" or paths["engine_dir"].parent.name != "UE_5.8":
        raise ValueError("The fixed UE 5.8 Engine directory is required")
    if not source.is_dir():
        raise ValueError("Source workspace is missing")
    return request


def _sha(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def parse_regression(output: str, expected_paths=None) -> dict:
    found = [int(n) for n in re.findall(r"Found\s+(\d+)\s+automation tests? based on ['\"]" +
                                       re.escape(REGRESSION_SELECTOR) + r"['\"]", output)]
    discovered = re.findall(r"LogAutomationCommandLine: Display:[ \t]+(ZhuGuoCity\.[^\s]+)[ \t]*\r?$", output, re.M)
    successful = re.findall(r"Test Completed\. Result=\{Success\}\s+Name=\{[^}]+\}\s+Path=\{([^}]+)\}", output)
    failed = re.findall(r"Test Completed\. Result=\{(?:Fail|Failed|Failure)\}\s+Name=\{[^}]+\}\s+Path=\{([^}]+)\}", output)
    performed = [int(n) for n in re.findall(r"\b(\d+)\s+tests?\s+performed\.", output)]
    completed = successful + failed
    expected = sorted(expected_paths if expected_paths is not None else discovered)
    missing = sorted(set(expected) - set(completed))
    unexpected = sorted(set(discovered + completed) - set(expected))
    complete = (bool(discovered) and found == performed == [len(discovered)] and
                len(set(discovered)) == len(discovered) and sorted(completed) == sorted(discovered) and
                sorted(discovered) == expected and
                all(re.fullmatch(r"ZhuGuoCity\.M[0-9]+\.[A-Za-z0-9_.]+", name) for name in discovered))
    fatal = bool(re.search(r"Fatal error|Result=\{(?:Fail|Failed|Failure)\}", output, re.I))
    package_skipped = "skipped: package integration runs only when its exact test is selected" in output
    false_pass = [PACKAGE_TEST] if package_skipped and PACKAGE_TEST in successful else []
    return {"selector": REGRESSION_SELECTOR, "found_counts": found, "performed_counts": performed,
            "discovered_paths": discovered, "successful_paths": successful, "failed_paths": failed,
            "expected_paths": expected, "expected_count": len(expected), "not_run_paths": missing,
            "unexpected_paths": unexpected, "false_pass_paths": false_pass,
            "cases": [{"name": name, "mode": "nullrhi", "status": "NOT_RUN" if name in missing else
                       "FAIL" if name in failed else "FALSE_PASS" if name in false_pass else "PASS"} for name in expected],
            "discovered": len(discovered), "executed": len(completed), "successful": len(successful),
            "failed": len(failed), "complete": complete, "passed": complete and not failed and not fatal and not false_pass,
            "package_skipped": package_skipped,
            "skip_or_not_run_lines": [line.strip() for line in output.splitlines()
                                      if re.search(r"\b(?:skipped|skipping|not run|not executed)\b", line, re.I)][:200],
            "coverage_note": "Automation Success is a test result; conditional skips are not proof that every business path ran."}


def source_inventory(project):
    """Read registrations and the existing visual host's literal artifact list."""
    records, visual, sources = [], {}, []
    for path in sorted((project / "Source").rglob("*.cpp")):
        path = ue_checks._safe_under(project, path.relative_to(project))
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Oversized automation source")
        source = path.read_text(encoding="utf-8-sig")
        matches = list(_REGISTRATION.finditer(source))
        if not matches:
            continue
        sources.append(_file_record(path))
        for index, match in enumerate(matches):
            name = match[1]
            if not re.fullmatch(r"ZhuGuoCity\.[A-Za-z0-9_.]+", name):
                raise ValueError("Invalid source automation identity")
            records.append(name)
            if not name.startswith("ZhuGuoCity.Local."):
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
            body = source[match.end():end]
            states = re.findall(r"(?:TSharedPtr|MakeShared)<(Zg\w+)::FState", body)
            namespace = re.search(r"^namespace\s+" + re.escape(states[0]) + r"\b", source, re.M) if states else None
            if not namespace:
                raise ValueError("Visual host namespace could not be verified: " + name)
            helper_end = next((m.start() for m in matches if m.start() > namespace.start()), match.start())
            helper = source[namespace.start():helper_end]
            directories = sorted(set(re.findall(r'"(Evidence/TASK-[A-Za-z0-9_-]+)"', helper)))
            images = list(dict.fromkeys(re.findall(r'"([A-Za-z0-9_-]+\.png)"', helper)))
            if len(directories) != 1 or not images:
                raise ValueError("Visual host's fixed screenshot expectations are missing: " + name)
            visual[name] = {"directory": "Saved/" + directories[0], "images": images,
                            "source": path.relative_to(project).as_posix(), "source_sha256": _sha(path)}
    if len(set(records)) != len(records):
        raise ValueError("Duplicate automation registrations")
    return {"regression": sorted(name for name in records if re.fullmatch(r"ZhuGuoCity\.M[0-9]+\..+", name)),
            "visual": dict(sorted(visual.items())), "sources": sources}


def _inventory(request):
    return ue_checks._object(Path(request["run_dir"]) / "source-manifest.json", maximum=32 * 1024 * 1024)["automation_inventory"]


def _log_text(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > OUTPUT_LIMIT:
        raise ValueError("Expected bounded raw log is missing or oversized: " + str(path))
    return path.read_text(encoding="utf-8-sig", errors="replace")


def _file_record(path):
    path = Path(path)
    return {"path": str(path), "sha256": _sha(path), "size_bytes": path.stat().st_size}


def _check_files(records, run):
    for record in records:
        path = Path(record["path"])
        if not path.is_relative_to(run):
            raise ValueError("Receipt evidence escaped this run")
        path = ue_checks._safe_under(run, path.relative_to(run))
        if not path.is_file() or _sha(path) != record["sha256"] or path.stat().st_size != record["size_bytes"]:
            raise ValueError("Recorded evidence changed: " + str(path))


def _bind(request, path):
    run = Path(request["run_dir"])
    binding = {"version": 1, "id": request["id"], "task_id": request["task_id"],
               "head_sha": request["head_sha"], "request_sha256": request["request_sha256"],
               "request_file_sha256": _sha(path), "runner_sha256": _sha(__file__),
               "retest_runner_sha256": _sha(Path(__file__).with_name("full_flow_retest.py"))}
    target = ue_checks._safe_under(run, "binding.json")
    if target.exists():
        original = ue_checks._object(target)
        if original != binding:
            migration_path = ue_checks._safe_under(run, "operator-runner-migration.json")
            if not migration_path.is_file():
                raise ValueError("Authorized request or fixed runner changed after the run began")
            migration = ue_checks._object(migration_path)
            expected = {"version": 1, **{key: request[key] for key in ("id", "task_id", "head_sha", "request_sha256")},
                        "binding_sha256": _sha(target),
                        "from_runner_sha256": original.get("runner_sha256"),
                        "from_retest_runner_sha256": original.get("retest_runner_sha256"),
                        "to_runner_sha256": binding["runner_sha256"],
                        "to_retest_runner_sha256": binding["retest_runner_sha256"],
                        "reason": RUNNER_MIGRATION_REASON}
            code_keys = {"runner_sha256", "retest_runner_sha256"}
            if (migration != expected or type(migration.get("version")) is not int or
                    {key: value for key, value in original.items() if key not in code_keys} !=
                    {key: value for key, value in binding.items() if key not in code_keys} or
                    any(not isinstance(expected[key], str) or not re.fullmatch(r"[0-9a-f]{64}", expected[key])
                        for key in ("from_runner_sha256", "from_retest_runner_sha256", "to_runner_sha256", "to_retest_runner_sha256"))):
                raise ValueError("Operator runner migration does not match exact original and current bindings")
            binding["operator_runner_migration"] = {**_file_record(migration_path),
                "reason": RUNNER_MIGRATION_REASON, "original_binding_sha256": expected["binding_sha256"]}
    else:
        write_json(target, binding)
    return binding


def _execute(argv, cwd, output, timeout):
    environment = safe_environment()
    if argv[0] == "git":
        # Git 2.55 archives may apply LFS filters. Keep committed pointers so
        # only verified local objects below the source Git directory are used.
        environment["GIT_LFS_SKIP_SMUDGE"] = "1"
    result = run_process(argv, cwd=cwd, output_dir=output, timeout_seconds=timeout,
                         max_output_bytes=OUTPUT_LIMIT, environment=environment)
    value = {"argv": argv, "cwd": str(cwd), "timeout_seconds": timeout, "exit_code": result.exit_code,
             "timed_out": result.timed_out, "output_limit_exceeded": result.output_limit_exceeded,
             "stdout_path": str(result.stdout_path), "stderr_path": str(result.stderr_path),
             "completed": not result.timed_out and not result.output_limit_exceeded}
    write_json(output / "process.json", value)
    return value


def _prepare(request, folder):
    source, project, run = (Path(request[key]) for key in ("source_workspace", "project_dir", "run_dir"))
    head = request["head_sha"]
    if gitops.head(source) != head:
        raise ValueError("Source HEAD differs from the explicitly authorized commit")
    if project.exists():
        raise ValueError("Preparation requires a new isolated project directory")
    archive = ue_checks._safe_under(run, "source.tar")
    command = ["git", "-C", str(source), "archive", "--format=tar", "--output=" + str(archive), head]
    process = _execute(command, source, folder / "archive", BUDGETS["prepare"])
    if process["exit_code"] or not process["completed"]:
        raise RuntimeError("Git archive failed; see preparation raw logs")
    project.mkdir()
    files = []
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            parts = PurePosixPath(member.name).parts
            if (not parts or PurePosixPath(member.name).is_absolute() or
                    any(part in {"..", ".git"} or any(c in part for c in ':\\"<>|?*') for part in parts) or
                    not (member.isfile() or member.isdir())):
                raise ValueError("Unsafe source archive entry")
            ue_checks._safe_under(project, member.name)
            if member.isfile():
                files.append(member.name)
        if len({name.casefold() for name in files}) != len(files):
            raise ValueError("Case-aliasing archive entries are forbidden")
        tar.extractall(project, filter="data")
    common = Path(gitops.git(source, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = source / common
    lfs = []
    for relative in files:
        asset = ue_checks._safe_under(project, relative)
        if asset.stat().st_size > 2048:
            continue
        raw = asset.read_bytes()
        if not raw.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
            continue
        match = re.fullmatch(rb"version https://git-lfs.github.com/spec/v1\noid sha256:([0-9a-f]{64})\nsize ([0-9]+)\n?", raw)
        if not match:
            raise ValueError("Malformed Git LFS pointer: " + relative)
        digest, size = match[1].decode(), int(match[2])
        blob = ue_checks._safe_under(common, Path("lfs/objects") / digest[:2] / digest[2:4] / digest)
        if not blob.is_file() or blob.stat().st_size != size or _sha(blob) != digest:
            raise ValueError("Required local LFS object is missing or corrupt: " + relative)
        shutil.copyfile(blob, asset)
        lfs.append({"path": relative, "sha256": digest, "size_bytes": size})
    # The project's existing formal package verifier requires a real clean HEAD.
    # Metadata is local to the exported project; no checkout or source writes.
    commands = [["git", "init", str(project)],
                ["git", "-C", str(project), "config", "core.autocrlf", "false"],
                ["git", "-C", str(project), "fetch", "--no-tags", "--depth=1", str(source), head],
                ["git", "-C", str(project), "reset", "--mixed", "--no-refresh", head],
                ["git", "-C", str(project), "lfs", "install", "--local", "--skip-smudge"]]
    for index, argv in enumerate(commands, 1):
        result = _execute(argv, project, folder / f"metadata-{index}", BUDGETS["prepare"])
        if result["exit_code"] or not result["completed"]:
            raise RuntimeError("Isolated Git metadata preparation failed")
    if gitops.head(project) != head or not gitops.clean(project):
        raise ValueError("Exported project is not the clean authorized Git HEAD")
    if not (project / "ZhuGuoCity.uproject").is_file() or not (project / "Tools/package_verification.py").is_file():
        raise ValueError("The complete game project and its formal package verifier are required")
    inventory = source_inventory(project)
    manifest = {"head_sha": head, "request_sha256": request["request_sha256"], "lfs": lfs,
                "automation_inventory": inventory,
                "files": {name: {"sha256": _sha(project / name), "size_bytes": (project / name).stat().st_size}
                          for name in sorted(files)}}
    write_json(run / "source-manifest.json", manifest)
    environment = {"python_executable": sys.executable, "python_exists": Path(sys.executable).is_file(),
                   "source_head": gitops.head(source), "source_clean": gitops.clean(source), "process_queries": []}
    version = ue_checks._safe_under(Path(request["engine_dir"]), "Build/Build.version")
    if version.is_file():
        environment.update(engine_version=ue_checks._object(version), engine_version_file=_file_record(version))
    if os.name == "nt":
        tasklist = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/tasklist.exe"
        for index, name in enumerate(("UnrealEditor.exe", "UnrealEditor-Cmd.exe", "LiveCodingConsole.exe"), 1):
            result = _execute([str(tasklist), "/FO", "CSV", "/NH", "/FI", "IMAGENAME eq " + name],
                              project, folder / f"environment-{index}", 30)
            environment["process_queries"].append({"image_name": name, "process": result,
                "observed_output": _log_text(result["stdout_path"])[:16384]})
    return {"passed": True, "exported_files": len(files), "hydrated_lfs_files": len(lfs),
            "registered_regression_count": len(inventory["regression"]), "registered_visual_count": len(inventory["visual"]),
            "environment": environment,
            "source_manifest_sha256": _sha(run / "source-manifest.json"), "archive_sha256": _sha(archive)}


def _verify_inputs(request):
    run, project = Path(request["run_dir"]), Path(request["project_dir"])
    prepare = ue_checks._object(ue_checks._safe_under(run, "checks/prepare/receipt.json"))
    manifest_path = ue_checks._safe_under(run, "source-manifest.json")
    if (prepare.get("status") != "passed" or prepare.get("request_sha256") != request["request_sha256"] or
            prepare.get("head_sha") != request["head_sha"] or
            _sha(manifest_path) != prepare.get("source_manifest_sha256")):
        raise ValueError("Successful same-request preparation is required")
    manifest = ue_checks._object(manifest_path, maximum=32 * 1024 * 1024)
    if gitops.head(project) != request["head_sha"] or not gitops.clean(project):
        raise ValueError("Isolated project HEAD or tracked input cleanliness changed")
    for name, record in manifest["files"].items():
        path = ue_checks._safe_under(project, name)
        if not path.is_file() or path.stat().st_size != record["size_bytes"] or _sha(path) != record["sha256"]:
            raise ValueError("Exported source input changed: " + name)
    return prepare["source_manifest_sha256"]


def _automation_command(request, stage, log):
    binaries = ue_checks._binaries(Path(request["engine_dir"]))
    project = Path(request["project_dir"]) / "ZhuGuoCity.uproject"
    names = VISUAL_TESTS if stage == "visual" else [PACKAGE_TEST]
    mode = "normal" if stage == "visual" else "nullrhi"
    command = ue_checks._command(binaries, project, {"name": names[0], "mode": mode}, log,
                                batch_names=names if stage == "visual" else None)
    if stage == "regression":
        command[2] = "-ExecCmds=Automation RunTests " + REGRESSION_SELECTOR
    if stage == "visual":
        command.extend(["-d3d12", "-windowed", "-ResX=1280", "-ResY=720"])
    return command


def _normal_rhi(raw):
    return bool(re.search(r"LogRHI: Using (?:Default|Selected|Forced) RHI: D3D12\b", raw)) and not bool(
        re.search(r"(?:Using (?:Default|Selected|Forced) RHI:\s*Null|Command Line:.*-nullrhi)", raw, re.I))


def _snapshot_artifact(request, source, destination, *, started_epoch=0, image=False):
    run = Path(request["run_dir"])
    source = Path(source)
    if not source.is_relative_to(run):
        raise ValueError("Artifact escaped this full-flow run")
    source = ue_checks._safe_under(run, source.relative_to(run))
    if not source.is_file() or source.stat().st_mtime < started_epoch:
        raise ValueError("Missing or stale artifact: " + str(source))
    if not Path(destination).is_relative_to(run):
        raise ValueError("Artifact snapshot escaped this run")
    destination = ue_checks._safe_under(run, Path(destination).relative_to(run))
    destination.parent.mkdir(parents=True, exist_ok=True)
    dimensions = None
    if image:
        with source.open("rb") as stream:
            header = stream.read(24)
        if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
            raise ValueError("Invalid PNG evidence header")
        dimensions = list(struct.unpack(">II", header[16:24]))
        if min(dimensions) <= 0:
            raise ValueError("Invalid PNG evidence dimensions")
    shutil.copy2(source, destination)
    record = _file_record(destination)
    record.update(relative_path=destination.relative_to(run).as_posix(),
                  source_relative_path=source.relative_to(run).as_posix())
    if dimensions:
        record["dimensions"] = dimensions
    return record


def run_visual_case(request, name, folder, timeout):
    """One exact approved case, preserving fresh images before any later retry."""
    inventory = _inventory(request)
    if name not in inventory["visual"] and name not in VISUAL_TESTS:
        raise ValueError("Visual case is not in this committed source's authorized set")
    if not 0 < timeout <= BUDGETS["visual"]:
        raise ValueError("Visual case exceeds its fixed budget")
    folder = ue_checks._safe_under(Path(request["run_dir"]), Path(folder).relative_to(Path(request["run_dir"])))
    folder.mkdir(parents=True, exist_ok=True)
    started = time.time()
    log = folder / "Unreal.log"
    project = Path(request["project_dir"])
    argv = ue_checks._command(ue_checks._binaries(Path(request["engine_dir"])), project / "ZhuGuoCity.uproject",
                             {"name": name, "mode": "normal"}, log)
    argv.extend(["-d3d12", "-windowed", "-ResX=1280", "-ResY=720"])
    result = {"name": name, "mode": "normal", "kind": "screenshots" if name in inventory["visual"] else "logic_or_widget_evidence",
              "status": "FAIL", "passed": False, "started_at": _now(), "evidence": [], "images": []}
    try:
        process = _execute(argv, project, folder, timeout)
        result["process"] = process
        raw = _log_text(log) if log.exists() else _log_text(process["stdout_path"])
        parsed = ue_checks.parse_automation_output(raw, name)
        parsed["failed_paths"] = [name] if parsed["failure_names"] else []
        parsed["successful_paths"] = [name] if parsed["success_names"] else []
        result.update(automation=parsed, rhi={"normal_rhi_proven": _normal_rhi(raw), "expected": "D3D12"},
                      skip_lines=[line.strip() for line in raw.splitlines() if re.search(r"\bskipped:", line, re.I)])
        for filename in ("stdout.log", "stderr.log", "process.json", "Unreal.log"):
            if (folder / filename).is_file():
                result["evidence"].append(_file_record(folder / filename))
        missing = []
        if name in inventory["visual"]:
            expectation = inventory["visual"][name]
            for filename in expectation["images"]:
                source = ue_checks._safe_under(project, Path(expectation["directory"]) / filename)
                try:
                    image = _snapshot_artifact(request, source, folder / "images" / filename,
                                               started_epoch=started, image=True)
                    result["images"].append(image)
                    result["evidence"].append(image)
                except (ValueError, OSError) as error:
                    missing.append({"path": str(source), "reason": str(error)})
        result["missing_images"] = missing
        passed = (process["completed"] and process["exit_code"] == 0 and parsed["passed"] and
                  result["rhi"]["normal_rhi_proven"] and not result["skip_lines"] and not missing)
        result.update(passed=passed, status="PASS" if passed else "FALSE_PASS" if parsed["success_names"] and result["skip_lines"] else "FAIL")
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    result["finished_at"] = _now()
    write_json(folder / "receipt.json", result)
    return result


def _run_visual(request, folder):
    names = list(_inventory(request)["visual"]) + VISUAL_TESTS
    deadline = time.monotonic() + BUDGETS["visual"]
    cases, evidence = [], []
    for index, name in enumerate(names, 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            cases.append({"name": name, "mode": "normal", "status": "NOT_RUN", "passed": False,
                          "reason": "Stage wall budget exhausted before this case started", "evidence": [], "images": []})
            continue
        print(json.dumps({"stage": "visual", "case": name, "status": "running"}), flush=True)
        case_folder = folder / f"case-{index:03d}"
        row = run_visual_case(request, name, case_folder, remaining)
        cases.append(row)
        evidence.extend(row["evidence"])
        evidence.append(_file_record(case_folder / "receipt.json"))
    return {"passed": all(row["passed"] for row in cases), "cases": cases, "evidence": evidence,
            "expected_visual_count": len(names) - len(VISUAL_TESTS), "logic_widget_count": len(VISUAL_TESTS),
            "images": [image for row in cases for image in row.get("images", [])]}


def _package_evidence(request, output, started_epoch, snapshot_dir=None):
    project = Path(request["project_dir"])
    path = ue_checks._safe_under(project, "Saved/Evidence/TASK-LOCAL-0066/package-summary.json")
    if "package integration PASS: exit=0" not in output or not path.is_file():
        return {"passed": False, "reason": "Formal package execution/summary is missing (a skipped Success is insufficient)"}
    value = ue_checks._object(path, maximum=16 * 1024 * 1024)
    passed = (path.stat().st_mtime >= started_epoch and value.get("status") == "passed" and
              value.get("head_sha") == request["head_sha"] and
              Path(value.get("workspace", "")).absolute() == project)
    result = {"passed": passed, "summary": _file_record(path), "status": value.get("status"),
              "head_sha": value.get("head_sha"), "reason": None if passed else "Package summary is stale or mismatched",
              "images": [], "artifacts": []}
    if snapshot_dir is not None:
        result["summary"] = _snapshot_artifact(request, path, snapshot_dir / "package-summary.json", started_epoch=started_epoch)
        try:
            runs = value["runs"]
            for scenario in ("plain", "loop"):
                facts = runs[scenario]["facts"]
                record = facts["screenshot"]
                image = _snapshot_artifact(request, Path(record["path"]), snapshot_dir / f"{scenario}.png",
                                           started_epoch=started_epoch, image=True)
                if image["sha256"] != record["sha256"]:
                    raise ValueError("Package screenshot disagrees with the formal summary")
                if scenario == "plain" and image["dimensions"] != [1280, 720]:
                    raise ValueError("Formal plain screenshot is not 1280x720")
                result["images"].append(image)
                log = _snapshot_artifact(request, Path(facts["log"]), snapshot_dir / f"{scenario}.log",
                                         started_epoch=started_epoch)
                result["artifacts"].append(log)
            loop = runs["loop"]["facts"]
            save_record = loop["save"]
            save = _snapshot_artifact(request, Path(save_record["path"]), snapshot_dir / "loop-Slot97.zgs",
                                      started_epoch=started_epoch)
            if save["sha256"] != save_record["sha256"]:
                raise ValueError("Package save disagrees with the formal summary")
            with Path(save["path"]).open(encoding="utf-8-sig") as stream:
                magic, header_line = stream.readline(100), stream.readline(65536)
            header = json.loads(header_line)
            if magic.strip() != "ZGSAVE1" or header.get("structureVersion") != 44 or header.get("contentVersion") != "zg-defs-dev-56":
                raise ValueError("Slot97 header does not prove the requested v44/dev-56")
            compared = re.search(r"\bcompared=(\d+)", loop.get("summary", ""))
            if not compared or int(compared[1]) < 300:
                raise ValueError("Packaged loop comparison has fewer than 300 steps")
            result.update(save_header=header, slot=97, compared_steps=int(compared[1]))
            result["artifacts"].append(save)
        except (ValueError, KeyError, TypeError, OSError) as error:
            result.update(passed=False, reason="Package artifact verification: " + str(error))
    return result


def _run_check(request, stage, folder, started_epoch):
    if stage == "visual":
        return _run_visual(request, folder)
    project, engine = Path(request["project_dir"]), Path(request["engine_dir"])
    log = folder / ("UnrealBuildTool.log" if stage == "build" else "Unreal.log")
    argv = ue_checks._clean_build_command(engine, project / "ZhuGuoCity.uproject", log) if stage == "build" else _automation_command(request, stage, log)
    process = _execute(argv, project, folder, BUDGETS[stage])
    raw = _log_text(log) if log.is_file() else _log_text(process["stdout_path"])
    passed = process["completed"] and process["exit_code"] == 0
    result = {"process": process, "evidence": [_file_record(folder / name) for name in
              ("stdout.log", "stderr.log", "process.json") if (folder / name).is_file()]}
    if log.is_file():
        result["evidence"].append(_file_record(log))
    if stage == "build":
        result["build_success_marker"] = "Result: Succeeded" in raw or "Result: Succeeded" in _log_text(process["stdout_path"])
        passed = passed and result["build_success_marker"]
    elif stage == "regression":
        result["automation"] = parse_regression(raw, _inventory(request)["regression"])
        passed = passed and result["automation"]["passed"]
        if PACKAGE_TEST in result["automation"]["successful_paths"]:
            result["package_execution"] = _package_evidence(request, raw, started_epoch, folder / "package-evidence")
    else:
        result["automation"] = ue_checks.parse_automation_output(raw, PACKAGE_TEST)
        result["package_execution"] = _package_evidence(request, raw, started_epoch, folder / "package-evidence")
        passed = passed and result["automation"]["passed"] and result["package_execution"]["passed"]
    package = result.get("package_execution", {}).get("summary")
    if package:
        result["evidence"].append(package)
    result["images"] = result.get("package_execution", {}).get("images", [])
    result["evidence"].extend(result["images"])
    result["evidence"].extend(result.get("package_execution", {}).get("artifacts", []))
    result["passed"] = passed
    return result


def status(request):
    run = Path(request["run_dir"])
    stages = {}
    for stage in STAGES[:-1]:
        path = ue_checks._safe_under(run, Path("checks") / stage / "receipt.json")
        if path.is_file():
            receipt = ue_checks._object(path, maximum=16 * 1024 * 1024)
            if (any(receipt.get(key) != request[key] for key in ("id", "task_id", "request_sha256", "head_sha")) or
                    receipt.get("stage") != stage):
                raise ValueError("Stage receipt belongs to another request/HEAD")
            stages[stage] = receipt
    complete = all(stages.get(stage, {}).get("status") in {"passed", "failed"} for stage in REQUIRED_STAGES)
    return {"version": 1, "id": request["id"], "task_id": request["task_id"], "head_sha": request["head_sha"],
            "request_sha256": request["request_sha256"], "stage": "status",
            "status": "completed" if complete else "running" if any(r["status"] == "running" for r in stages.values()) else "pending",
            "stages": stages, "all_stages_finished": complete,
            "passed": all(stages.get(stage, {}).get("status") == "passed" for stage in REQUIRED_STAGES),
            "failed_stages": [stage for stage, receipt in stages.items() if receipt["status"] == "failed"],
            "updated_at": _now()}


def run_stage(request_path: Path, stage: str, *, retry=False):
    if stage not in STAGES or retry and stage != "package":
        raise ValueError("Only the fixed stages and one explicit package retry are supported")
    request = load_request(request_path)
    run = Path(request["run_dir"])
    if stage == "status":
        return status(request)
    with lock(ue_checks._safe_under(run, "checks.lock")):
        binding = _bind(request, request_path)
        receipt_path = ue_checks._safe_under(run, Path("checks") / stage / "receipt.json")
        previous = ue_checks._object(receipt_path, maximum=16 * 1024 * 1024) if receipt_path.is_file() else None
        if previous:
            if previous.get("request_sha256") != request["request_sha256"] or previous.get("head_sha") != request["head_sha"]:
                raise ValueError("Existing stage has a different input binding")
            if not retry:
                if stage != "prepare" or previous.get("status") == "passed":
                    _verify_inputs(request)
                _check_files(previous.get("evidence", []), run)
                return previous
            if previous.get("status") != "failed" or previous.get("attempt") != 1:
                raise ValueError("Package retry requires exactly one completed failed package attempt")
        elif retry:
            raise ValueError("Package retry requires an existing failed attempt")
        attempt = 2 if retry else 1
        folder = ue_checks._safe_under(run, Path("checks") / stage / f"attempt-{attempt}")
        folder.mkdir(parents=True, exist_ok=False)
        receipt = {**binding, "stage": stage, "attempt": attempt, "status": "running", "started_at": _now(), "updated_at": _now(),
                   "output_dir": str(folder), "passed": False}
        write_json(receipt_path, receipt)
        write_json(run / "facts.json", status(request))
        started_epoch = datetime.fromisoformat(receipt["started_at"]).timestamp()
        try:
            if stage == "prepare":
                result = _prepare(request, folder)
            else:
                receipt["source_manifest_sha256"] = _verify_inputs(request)
                build = ue_checks._object(run / "checks/build/receipt.json") if stage != "build" else None
                if build and (build.get("status") != "passed" or
                              any(build.get(key) != request[key] for key in ("id", "task_id", "request_sha256", "head_sha"))):
                    raise ValueError("This request's successful isolated editor build is required")
                if build:
                    _check_files(build.get("evidence", []), run)
                result = None
                if stage == "package" and not retry:
                    regression_path = run / "checks/regression/receipt.json"
                    regression = ue_checks._object(regression_path, maximum=16 * 1024 * 1024) if regression_path.is_file() else {}
                    package = regression.get("package_execution", {})
                    if (regression.get("request_sha256") == request["request_sha256"] and
                            regression.get("head_sha") == request["head_sha"] and
                            regression.get("source_manifest_sha256") == receipt["source_manifest_sha256"] and
                            package.get("passed") is True):
                        _check_files(regression.get("evidence", []), run)
                        result = {"passed": True, "reused_regression_receipt": _file_record(regression_path),
                                  "package_execution": package, "evidence": regression["evidence"]}
                if stage == "retest":
                    from unattended.full_flow_retest import run as retest
                    result = retest(request, folder, started_epoch)
                elif result is None:
                    result = _run_check(request, stage, folder, started_epoch)
                _verify_inputs(request)
            receipt.update(result)
            receipt["status"] = "passed" if receipt["passed"] else "failed"
        except Exception as error:
            receipt.update(status="failed", passed=False, error=f"{type(error).__name__}: {error}")
        receipt["finished_at"] = _now()
        receipt["updated_at"] = receipt["finished_at"]
        write_json(folder / "receipt.json", receipt)
        write_json(receipt_path, receipt)
        write_json(run / "facts.json", status(request))
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--stage", choices=STAGES, required=True)
    parser.add_argument("--retry", action="store_true", help="One explicit retry of a failed package stage")
    args = parser.parse_args(argv)
    try:
        result = run_stage(args.request, args.stage, retry=args.retry)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if args.stage == "status" or result.get("passed") is True else 1
    except Exception as error:
        print(json.dumps({"passed": False, "error": f"{type(error).__name__}: {error}"}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
