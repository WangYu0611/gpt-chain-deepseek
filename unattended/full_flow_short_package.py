"""One formal package retry in a short, owned copy of an authorized snapshot.

The original test source, arguments, full-flow binding and failed receipt stay
unchanged. Only this request's isolated filesystem location changes.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from unattended import full_flow_checks as flow, gitops, ue_checks
from unattended.storage import lock, write_json

SHORT_ROOT = Path("C:/zg-flow")
EDITOR_FILES = ("UnrealEditor-ZhuGuoCity.dll", "UnrealEditor.modules", "ZhuGuoCityEditor.target")


def _original_inputs(request, request_path):
    run = Path(request["run_dir"])
    binding_path = ue_checks._safe_under(run, "binding.json")
    if not binding_path.is_file():
        raise ValueError("Original full-flow binding is required")
    # Existing bindings are read-only here. The fixed runner recognizes only
    # an exact operator-authorized migration when its implementation changed.
    binding = flow._bind(request, request_path)
    required = {key: request[key] for key in ("id", "task_id", "head_sha", "request_sha256")}
    required.update(request_file_sha256=flow._sha(request_path), runner_sha256=flow._sha(flow.__file__),
                    retest_runner_sha256=flow._sha(Path(flow.__file__).with_name("full_flow_retest.py")))
    if any(binding.get(key) != value for key, value in required.items()):
        raise ValueError("Original full-flow request or frozen runner binding changed")
    manifest_sha = flow._verify_inputs(request)
    manifest_path = ue_checks._safe_under(run, "source-manifest.json")
    manifest = ue_checks._object(manifest_path, maximum=32 * 1024 * 1024)
    build = ue_checks._object(ue_checks._safe_under(run, "checks/build/receipt.json"))
    failed_path = ue_checks._safe_under(run, "checks/package/receipt.json")
    failed = ue_checks._object(failed_path, maximum=16 * 1024 * 1024)
    for stage, value, status in (("build", build, "passed"), ("package", failed, "failed")):
        if (any(value.get(key) != request[key] for key in ("id", "task_id", "head_sha", "request_sha256")) or
                value.get("stage") != stage or value.get("status") != status or not value.get("finished_at") or
                value.get("source_manifest_sha256") != manifest_sha):
            raise ValueError("Short retry requires this HEAD's successful build and failed formal package receipt")
        flow._check_files(value.get("evidence", []), run)
    uat_path = ue_checks._safe_under(Path(request["project_dir"]), "Saved/Evidence/TASK-LOCAL-0066/package-uat.log")
    if "action paths are longer than 260 characters" not in flow._log_text(uat_path):
        raise ValueError("This environment retry requires the actual UBT path-length failure")
    return manifest, build, {"source_manifest_sha256": manifest_sha,
            "operator_runner_migration": binding.get("operator_runner_migration"),
            "original_package_receipt": flow._file_record(failed_path), "original_path_failure": flow._file_record(uat_path)}


def _short_directory(request):
    root = flow._absolute(str(SHORT_ROOT), "fixed short root")
    short = ue_checks._safe_under(root, request["id"][:12])
    original = Path(request["run_dir"])
    source = Path(request["source_workspace"])
    if any(short.is_relative_to(path) or path.is_relative_to(short) for path in (original, source)):
        raise ValueError("Short package directory overlaps original inputs")
    return short


def _own_short_directory(short, owner):
    if short.exists():
        marker = ue_checks._safe_under(short, "owner.json")
        if not marker.is_file() or ue_checks._object(marker) != owner:
            raise ValueError("Existing short directory is not owned by this exact request; nothing was removed")
        return
    short.mkdir(parents=True, exist_ok=False)
    write_json(short / "owner.json", owner)


def _verify_short(request, short, original_manifest):
    project = ue_checks._safe_under(short, "project")
    if gitops.head(project) != request["head_sha"] or not gitops.clean(project):
        raise ValueError("Short project no longer has the clean authorized HEAD")
    for name, value in original_manifest["files"].items():
        path = ue_checks._safe_under(project, name)
        if not path.is_file() or path.stat().st_size != value["size_bytes"] or flow._sha(path) != value["sha256"]:
            raise ValueError("Short project input differs from original source manifest: " + name)


def _prepare_short(request, short, folder, manifest, build):
    derived = {**request, "run_dir": str(short), "project_dir": str(short / "project")}
    preparation = flow._prepare(derived, folder / "prepare")
    exported = ue_checks._object(short / "source-manifest.json", maximum=32 * 1024 * 1024)
    if (exported["files"] != manifest["files"] or exported["head_sha"] != request["head_sha"] or
            exported["request_sha256"] != request["request_sha256"]):
        raise ValueError("New archive/LFS inputs differ from the authorized original manifest")
    # Reuse only the same snapshot's already-built editor host. The formal
    # PackageIntegration test still performs its original full UAT -build.
    started = datetime.fromisoformat(build["started_at"]).timestamp()
    finished = datetime.fromisoformat(build["finished_at"]).timestamp()
    copied = []
    for name in EDITOR_FILES:
        source = ue_checks._safe_under(Path(request["project_dir"]), Path("Binaries/Win64") / name)
        if not source.is_file() or not started <= source.stat().st_mtime <= finished + 2:
            raise ValueError("Same-run editor artifact is missing or not bound to its successful build: " + name)
        target = ue_checks._safe_under(short, Path("project/Binaries/Win64") / name)
        target.parent.mkdir(parents=True, exist_ok=True)
        source_record = flow._file_record(source)
        shutil.copy2(source, target)
        if flow._sha(target) != source_record["sha256"]:
            raise ValueError("Editor artifact copy differs from its proven source")
        copied.append({"name": name, "sha256": source_record["sha256"], "size_bytes": source_record["size_bytes"],
                       "source_path": str(source), "short_path": str(target)})
    _verify_short(request, short, manifest)
    shutil.copy2(short / "source-manifest.json", folder / "short-source-manifest.json")
    return derived, {"preparation": preparation, "editor_artifacts": copied,
                     "short_source_manifest": flow._file_record(folder / "short-source-manifest.json")}


def _archive_package(request, derived, raw, started, folder):
    short = Path(derived["run_dir"])
    proof = flow._package_evidence(derived, raw, started, short / "package-evidence")
    evidence, images = [], []
    target = folder / "package-evidence"
    target.mkdir(exist_ok=True)
    for kind, records in (("summary", [proof["summary"]] if proof.get("summary") else []),
                          ("images", proof.get("images", [])), ("artifacts", proof.get("artifacts", []))):
        copied = []
        for record in records:
            source = Path(record["path"])
            if not source.is_relative_to(short):
                raise ValueError("Package proof escaped the owned short directory")
            source = ue_checks._safe_under(short, source.relative_to(short))
            if flow._sha(source) != record["sha256"]:
                raise ValueError("Short package evidence changed before archival")
            destination = ue_checks._safe_under(target, source.name)
            shutil.copy2(source, destination)
            archived = {**record, **flow._file_record(destination),
                        "relative_path": destination.relative_to(Path(request["run_dir"])).as_posix(),
                        "short_source_path": str(source)}
            copied.append(archived)
            evidence.append(archived)
        if kind == "summary" and copied:
            proof["summary"] = copied[0]
        elif kind != "summary":
            proof[kind] = copied
        if kind == "images":
            images = copied
    # A failure must also retain its actual new UAT log/partial summary.
    source_dir = ue_checks._safe_under(short, "project/Saved/Evidence/TASK-LOCAL-0066")
    for name in ("package-uat.log", "package-summary.json", "package-artifacts.json"):
        source = ue_checks._safe_under(source_dir, name)
        destination = target / ("raw-" + name)
        if source.is_file() and source.stat().st_mtime >= started:
            shutil.copy2(source, destination)
            evidence.append(flow._file_record(destination))
    return proof, evidence, images


def run(request_path):
    request_path = Path(request_path)
    request = flow.load_request(request_path)
    run_dir = Path(request["run_dir"])
    base = ue_checks._safe_under(run_dir, "checks/package-short")
    receipt_path = ue_checks._safe_under(base, "receipt.json")
    # Serializes with the existing validation stages, without touching workers.
    with lock(ue_checks._safe_under(run_dir, "checks.lock")):
        manifest, build, inputs = _original_inputs(request, request_path)
        short = _short_directory(request)
        owner = {"version": 1, **{key: request[key] for key in ("id", "task_id", "head_sha", "request_sha256")},
                 "source_manifest_sha256": inputs["source_manifest_sha256"], "short_root": str(short),
                 "runner_sha256": flow._sha(__file__), "full_flow_runner_sha256": flow._sha(flow.__file__),
                 "retest_runner_sha256": flow._sha(Path(flow.__file__).with_name("full_flow_retest.py"))}
        if receipt_path.is_file():
            previous = ue_checks._object(receipt_path, maximum=16 * 1024 * 1024)
            if any(previous.get(key) != value for key, value in owner.items()):
                raise ValueError("Previous short-package attempt has a different immutable binding")
            _own_short_directory(short, owner)
            flow._check_files(previous.get("evidence", []), run_dir)
            if previous.get("status") == "running":
                raise ValueError("Short-package submission is indeterminate; it will not be repeated automatically")
            return previous
        if short.exists():
            # Never adopt a preexisting project even if its marker was copied.
            _own_short_directory(short, owner)
            raise ValueError("Owned short directory exists without a complete run receipt; refusing implicit reuse")
        _own_short_directory(short, owner)
        folder = ue_checks._safe_under(base, "attempt-1")
        folder.mkdir(parents=True, exist_ok=False)
        receipt = {**owner, **inputs, "stage": "package-short", "attempt": 1, "status": "running", "passed": False,
                   "started_at": flow._now(), "updated_at": flow._now(), "output_dir": str(folder), "evidence": [],
                   "environment_change": "Short owned project path; original test, UAT arguments and assertions unchanged"}
        # Persist intent before preparation or any game process; no blind retry.
        write_json(receipt_path, receipt)
        write_json(folder / "receipt.json", receipt)
        try:
            derived, preparation = _prepare_short(request, short, folder, manifest, build)
            receipt.update(preparation)
            receipt["evidence"].append(preparation["short_source_manifest"])
            failure_copy = folder / "original-path-failure.log"
            shutil.copy2(inputs["original_path_failure"]["path"], failure_copy)
            receipt["evidence"].append(flow._file_record(failure_copy))
            started = time.time()
            logs = folder / "rawlogs"
            argv = flow._automation_command(derived, "package", logs / "Unreal.log")
            process = flow._execute(argv, Path(derived["project_dir"]), logs, flow.BUDGETS["package"])
            receipt["process"] = process
            raw = flow._log_text(logs / "Unreal.log") if (logs / "Unreal.log").is_file() else flow._log_text(process["stdout_path"])
            receipt["automation"] = ue_checks.parse_automation_output(raw, flow.PACKAGE_TEST)
            proof, evidence, images = _archive_package(request, derived, raw, started, folder)
            receipt.update(package_execution=proof, images=images)
            receipt["evidence"].extend(evidence)
            for name in ("stdout.log", "stderr.log", "process.json", "Unreal.log"):
                if (logs / name).is_file():
                    receipt["evidence"].append(flow._file_record(logs / name))
            _verify_short(request, short, manifest)
            flow._verify_inputs(request)
            if flow._sha(Path(inputs["original_package_receipt"]["path"])) != inputs["original_package_receipt"]["sha256"]:
                raise ValueError("Original failed package receipt changed")
            receipt["passed"] = (process["completed"] and process["exit_code"] == 0 and
                                 receipt["automation"]["passed"] and proof["passed"])
            receipt["status"] = "passed" if receipt["passed"] else "failed"
        except Exception as error:
            receipt.update(status="failed", passed=False, error=f"{type(error).__name__}: {error}")
        receipt["finished_at"] = flow._now()
        receipt["updated_at"] = receipt["finished_at"]
        write_json(folder / "receipt.json", receipt)
        write_json(receipt_path, receipt)
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run(args.request)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if result.get("passed") else 1
    except Exception as error:
        print(json.dumps({"passed": False, "error": f"{type(error).__name__}: {error}"}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
