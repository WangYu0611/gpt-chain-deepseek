"""Versioned, budgeted art preview lane. Safe to invoke from a Claude task.

The batch configuration lives outside the coding workspace. Preview never expands
the approved level set. Tripo task identity survives local processing revisions.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


RECIPE_FIELDS = {"asset_id", "revision", "prompt", "target_height_m", "max_footprint_m", "front_rotation_deg"}
BLENDER_DEPENDENCIES = ("art_tools/blender/prepare_batch.py", "art_tools/blender/roof_repair.py",
                        "art_tools/blender/roof_repair_plans.json", "art_tools/blender/courtyard_geometry.py",
                        "art_tools/blender/blender_job_runtime.py")


def blender_provenance():
    """Hash only the fixed trusted tools; recipes cannot select executable paths."""
    root = REPO.resolve()
    files = []
    for index, relative in enumerate(BLENDER_DEPENDENCIES):
        path = root / relative
        if path.is_symlink():
            raise ValueError("Invalid trusted Blender dependency: " + relative)
        if not path.exists() and index:
            continue
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError("Invalid trusted Blender dependency: " + relative)
        files.append({"path": relative, "sha256": sha(path), "size_bytes": path.stat().st_size})
    return {"schema_version": 1, "files": files}


def blender_fingerprint(manifest):
    """The manifest includes both asset inputs and the trusted tool dependency hashes."""
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:12]


def blender_matches_review(cfg, report, manifest):
    try:
        provenance = blender_provenance()
        source = Path(cfg["artifacts_dir"]) / "production-input.json"
        source_data, processed = read(source), read(manifest)
        if not isinstance(source_data, dict) or not isinstance(processed, dict):
            return False
        return (report.get("blender_provenance") == provenance and
                source_data.get("blender_provenance") == provenance and
                processed.get("blender_provenance") == provenance and
                processed.get("input_manifest_sha256") == sha(source))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def recipe_file_path(cfg, *, workspace=None):
    """The trusted batch selects one editable file; an overlay cannot select paths."""
    raw = cfg.get("recipe_file")
    if raw is None:
        return None
    if (not isinstance(raw, str) or not raw or any(ord(c) < 32 for c in raw) or
            any(c in raw for c in "*?[]") or ".." in Path(raw).parts):
        raise ValueError("recipe_file must be an exact absolute JSON file path")
    path = Path(raw)
    if not path.is_absolute() or path.suffix.lower() != ".json":
        raise ValueError("recipe_file must be an exact absolute JSON file path")
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.stat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("recipe_file may not cross a linked path")
    path = path.resolve()
    if workspace is not None and not path.is_relative_to(Path(workspace).resolve()):
        raise ValueError("recipe_file must be inside the isolated worker workspace")
    for name in ("artifacts_dir", "private_state_dir"):
        if path.is_relative_to(Path(cfg[name]).resolve()):
            raise ValueError("recipe_file must be outside artifacts and private state")
    if path == Path(cfg["key_file"]).resolve():
        raise ValueError("recipe_file cannot be a credential file")
    if not path.is_file():
        raise ValueError("recipe_file must already exist")
    return path


def recipe_overlay(cfg):
    """Read one bounded snapshot; immutable authorization always comes from cfg."""
    path = recipe_file_path(cfg)
    if path is None:
        return cfg, None, None
    with path.open("rb") as stream:
        raw = stream.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise ValueError("Art recipe is too large")
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate art recipe field")
            result[key] = value
        return result
    document = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_fields)
    if (not isinstance(document, dict) or set(document) != {"version", "assets"} or
            type(document["version"]) is not int or document["version"] != 1 or
            not isinstance(document["assets"], list) or len(document["assets"]) != 3):
        raise ValueError("Art recipe requires version=1 and exactly three approved assets")
    result = copy.deepcopy(cfg)
    def bounded_number(value, low, high):
        return type(value) in (int, float) and math.isfinite(value) and low <= value <= high
    for approved, overlay, target in zip(cfg["assets"], document["assets"], result["assets"]):
        if not isinstance(overlay, dict) or set(overlay) != RECIPE_FIELDS or overlay["asset_id"] != approved["asset_id"]:
            raise ValueError("Art recipe identities, order and editable fields must match the approved assets")
        revision = overlay["revision"]
        if not isinstance(revision, str) or not re.fullmatch(r"v(?!000)\d{3}", revision, flags=re.ASCII):
            raise ValueError("Art recipe revision must be v001..v999")
        baseline = approved.get("revision", "v001")
        if re.fullmatch(r"v\d{3}", baseline, flags=re.ASCII) and revision < baseline:
            raise ValueError("Art recipe cannot roll back below the approved revision")
        prompt = overlay["prompt"]
        if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000 or
                "\0" in prompt or prompt.startswith("@")):
            raise ValueError("Art recipe prompt must be nonempty text of at most 8000 characters")
        if prompt != approved["prompt"] and revision == baseline:
            raise ValueError("A changed generation prompt requires a new revision")
        if not bounded_number(overlay["target_height_m"], 1.5, 8):
            raise ValueError("Art recipe target height must be 1.5..8 meters")
        maximum = approved["max_footprint_m"]
        if type(maximum) in (int, float):
            maximum = [maximum, maximum]
        footprint = overlay["max_footprint_m"]
        if (not isinstance(footprint, list) or len(footprint) != 2 or
                any(not bounded_number(value, 1, cap) for value, cap in zip(footprint, maximum))):
            raise ValueError("Art recipe footprint must stay within each approved dimension")
        if type(overlay["front_rotation_deg"]) not in (int, float) or overlay["front_rotation_deg"] not in (0, 90, 180, 270):
            raise ValueError("Art recipe front rotation must be 0, 90, 180 or 270 degrees")
        target.update(overlay)
    digest = hashlib.sha256(raw).hexdigest()
    provenance = {"schema_version": 1, "source_file": str(path), "sha256": digest,
                  "snapshot": str(Path(cfg["artifacts_dir"]) / "recipes" / (digest + ".json"))}
    return result, provenance, raw


def blender_job_file_path(cfg, *, workspace=None):
    """The trusted batch may select one versioned Claude-authored Blender job."""
    raw = cfg.get("blender_job_file")
    if raw is None:
        return None
    if (not isinstance(raw, str) or not raw or any(ord(c) < 32 for c in raw) or
            any(c in raw for c in "*?[]") or ".." in Path(raw).parts):
        raise ValueError("blender_job_file must be an exact absolute Python file path")
    path = Path(raw)
    if not path.is_absolute() or path.suffix.lower() != ".py":
        raise ValueError("blender_job_file must be an exact absolute Python file path")
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.stat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("blender_job_file may not cross a linked path")
    path = path.resolve()
    if workspace is not None and not path.is_relative_to(Path(workspace).resolve()):
        raise ValueError("blender_job_file must be inside the isolated worker workspace")
    for name in ("artifacts_dir", "private_state_dir"):
        if path.is_relative_to(Path(cfg[name]).resolve()):
            raise ValueError("blender_job_file must be outside artifacts and private state")
    if path == Path(cfg["key_file"]).resolve() or not path.is_file():
        raise ValueError("blender_job_file must already exist outside credentials")
    return path


def blender_job_snapshot(cfg):
    path = blender_job_file_path(cfg)
    if path is None:
        return None, None
    with path.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if not raw.strip() or len(raw) > 1024 * 1024:
        raise ValueError("Blender job must contain at most 1 MiB of Python source")
    # Syntax validation is an early diagnostic, not a Python security sandbox.
    compile(raw, str(path), "exec")
    digest = hashlib.sha256(raw).hexdigest()
    return {"schema_version": 1, "source_file": str(path), "sha256": digest,
            "size_bytes": len(raw),
            "snapshot": str(Path(cfg["artifacts_dir"]) / "blender-jobs" / (digest + ".py"))}, raw


def blender_job_matches_review(cfg, report, manifest):
    try:
        provenance, _ = blender_job_snapshot(cfg)
        source = read(Path(cfg["artifacts_dir"]) / "production-input.json")
        processed = read(manifest)
        if provenance is None:
            return all("blender_job" not in item for item in (report, source, processed))
        return (all(item.get("blender_job") == provenance for item in (report, source, processed)) and
                sha(provenance["snapshot"]) == provenance["sha256"] and
                processed.get("input_manifest_sha256") == sha(Path(cfg["artifacts_dir"]) / "production-input.json"))
    except (OSError, ValueError, KeyError, TypeError, SyntaxError):
        return False


def recipe_matches_review(cfg, report, manifest):
    try:
        _, provenance, _ = recipe_overlay(cfg)
        if provenance is None:
            return "recipe" not in report
        source = Path(cfg["artifacts_dir"]) / "production-input.json"
        return (report.get("recipe") == provenance and sha(provenance["snapshot"]) == provenance["sha256"] and
                read(source).get("recipe") == provenance and
                read(manifest).get("input_manifest_sha256") == sha(source))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def log(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


@contextlib.contextmanager
def exclusive(path):
    """Kernel lock; unlike a marker file it is released after a process crash."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0)
        if stream.read(1) == b"":
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Art batch is already running; inspect status instead") from exc
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def load_config(path):
    cfg = read(path)
    if not isinstance(cfg, dict) or cfg.get("version") != 1 or cfg.get("batch_id") != "HOUSE-COMMON-001":
        raise ValueError("Unsupported art batch/version")
    if cfg.get("budget_limit") != 3000:
        raise ValueError("This approved batch has a fixed 3000-credit cap")
    if cfg.get("approved_levels") != ["L1", "L4", "L8"] or cfg.get("phase") != "preview":
        raise ValueError("Only the approved L1/L4/L8 preview may run; full eight levels await user acceptance")
    if [a.get("level") for a in cfg.get("assets", [])] != cfg["approved_levels"]:
        raise ValueError("Asset list must exactly match approved preview levels")
    ids = [a.get("asset_id") for a in cfg["assets"]]
    if len(set(ids)) != 3 or any(not isinstance(i, str) or not i.replace("_", "").isalnum() for i in ids):
        raise ValueError("Invalid asset identities")
    for field in ("artifacts_dir", "private_state_dir", "key_file", "blender", "engine"):
        if not Path(cfg[field]).is_absolute():
            raise ValueError(field + " must be absolute")
    root = Path(cfg["artifacts_dir"]).resolve()
    private = Path(cfg["private_state_dir"]).resolve()
    if Path(cfg["key_file"]).resolve().is_relative_to(root) or private.is_relative_to(root):
        raise ValueError("Secrets and private Tripo state must be outside public artifacts")
    references = cfg.get("references")
    if not isinstance(references, list) or not references:
        raise ValueError("At least one confirmed, hash-pinned style reference is required")
    for reference in references:
        if (not isinstance(reference, dict) or not isinstance(reference.get("path"), str) or
                not Path(reference["path"]).is_absolute() or not isinstance(reference.get("sha256"), str) or
                len(reference["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in reference["sha256"])):
            raise ValueError("Style references require absolute paths and SHA-256 digests")
        if sha(reference["path"]) != reference["sha256"]:
            raise ValueError("Confirmed style reference changed: " + Path(reference["path"]).name)
    recipe_file_path(cfg)
    blender_job_file_path(cfg)
    return cfg


def blender_environment():
    """DCC processing does not need the controller's provider credentials."""
    names = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
             "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "COMMONPROGRAMFILES", "OS", "PATHEXT",
             "COMSPEC", "SYSTEMDRIVE", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "HOMEDRIVE", "HOMEPATH"}
    return {key: value for key, value in os.environ.items() if key.upper() in names}


def process(argv, logfile, timeout=1800, env=None):
    logfile = Path(logfile)
    logfile.parent.mkdir(parents=True, exist_ok=True)
    log("process_started", executable=Path(argv[0]).name, log=str(logfile))
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with logfile.open("w", encoding="utf-8") as stream:
        p = subprocess.Popen(argv, stdout=stream, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, env=env, creationflags=flags)
        atomic(logfile.with_suffix(".process.json"), {"pid": p.pid, "started_epoch": time.time(),
                                                     "executable": argv[0]})
        try:
            code = p.wait(timeout=timeout)
        except BaseException:
            # Only terminate the process started by this invocation.
            p.terminate()
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
            raise
    if code != 0:
        raise RuntimeError(f"{Path(argv[0]).name} failed ({code}); see {logfile}")


def require_free_project(project):
    """Never import into a project still owned by an editor from another run."""
    if os.name != "nt":
        return
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process -Filter \"Name = 'UnrealEditor.exe' OR Name = 'UnrealEditor-Cmd.exe'\" | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
        capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError("Cannot verify preview project ownership")
    raw = result.stdout.decode("utf-8-sig", errors="replace").strip()
    rows = json.loads(raw) if raw else []
    if isinstance(rows, dict):
        rows = [rows]
    normalized = str(Path(project).resolve()).replace("\\", "/").lower()
    if any(normalized in (row.get("CommandLine") or "").replace("\\", "/").lower() for row in rows):
        raise RuntimeError("Preview project is already open; let that owned operation finish before resuming")


def ready_file(record, field):
    """Accept only downloaded and hash-verified files, never remote URLs."""
    if record.get("ready") is not True:
        raise RuntimeError("Tripo task is not ready; generation, download and cost must all be confirmed")
    files = record.get("files", {})
    entry = files.get(field)
    if isinstance(entry, dict):
        path = entry.get("path") or entry.get("local_path")
        expected = entry.get("sha256")
    elif isinstance(entry, str):
        path, expected = entry, None
    else:
        raise RuntimeError("Missing downloaded Tripo output: " + field)
    if not path or not Path(path).is_file() or Path(path).stat().st_size < 32:
        raise RuntimeError("Missing/empty downloaded artifact: " + field)
    if not expected or sha(path) != expected:
        raise RuntimeError("Downloaded artifact digest mismatch: " + field)
    return str(Path(path).resolve())


def verify_processed(path, *, source_manifest=None):
    try:
        data = read(path)
        parent = Path(path).resolve().parent
        pairs = [(data["blend"], data["blend_sha256"])]
        pairs.extend((a["fbx"], a["fbx_sha256"]) for a in data["assets"])
        for m in data["materials"]:
            for field in ("base_color", "orm", "normal"):
                pairs.append((m[field], m[field + "_sha256"]))
        for raw, digest in pairs:
            p = (parent / raw).resolve()
            if not p.is_relative_to(parent) or not p.is_file() or sha(p) != digest:
                return False
        if source_manifest is not None:
            provenance = blender_provenance()
            source = read(source_manifest)
            if (data.get("input_manifest_sha256") != sha(source_manifest) or
                    data.get("blender_provenance") != provenance or
                    not isinstance(source, dict) or source.get("blender_provenance") != provenance):
                return False
            job = source.get("blender_job")
            if data.get("blender_job") != job:
                return False
            if job is not None and sha(job["snapshot"]) != job["sha256"]:
                return False
        return len(data["plots"]) == 3 and len(data["assets"]) >= 3
    except (OSError, ValueError, KeyError, TypeError):
        return False


def verify_capture(path):
    try:
        data = read(path)
        if not isinstance(data, dict) or data.get("status") != "passed" or data.get("real_rhi") is not True or len(data.get("shots", [])) != 8:
            return False
        if len({Path(s["path"]).resolve() for s in data["shots"]}) != 8:
            return False
        return all(Path(s["path"]).is_file() and sha(s["path"]) == s["sha256"] for s in data["shots"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def capture_fingerprint(recipe, import_receipt, script):
    return recipe + "-" + hashlib.sha256((sha(import_receipt) + sha(script)).encode()).hexdigest()[:12]


def verify_import(receipt, project, script, manifest):
    try:
        data = read(receipt)
        return (isinstance(data, dict) and data.get("status") == "passed" and data.get("script_sha256") == sha(script)
                and data.get("manifest_sha256") == sha(manifest)
                and len(data.get("ue_files", [])) > 0
                and all((Path(project).parent / f["path"]).is_file()
                        and sha(Path(project).parent / f["path"]) == f["sha256"] for f in data["ue_files"]))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def write_project(project):
    project = Path(project)
    project.parent.mkdir(parents=True, exist_ok=True)
    atomic(project, {"FileVersion": 3, "EngineAssociation": "5.8", "Category": "Art",
                     "Description": "ZhuGuo housing preview - approved A style",
                     "Plugins": [{"Name": "PythonScriptPlugin", "Enabled": True},
                                 {"Name": "EditorScriptingUtilities", "Enabled": True}]})
    config = project.parent / "Config"
    config.mkdir(exist_ok=True)
    (config / "DefaultEngine.ini").write_text(
        "[/Script/EngineSettings.GameMapsSettings]\nEditorStartupMap=/Game/ArtPreview/Maps/HousingPreview\n"
        "GameDefaultMap=/Game/ArtPreview/Maps/HousingPreview\n"
        "[/Script/Engine.RendererSettings]\nr.DefaultFeature.AutoExposure=False\n"
        "r.DynamicGlobalIlluminationMethod=1\nr.ReflectionMethod=1\nr.Shadow.Virtual.Enable=1\n"
        "r.DefaultFeature.MotionBlur=False\n"
        "[/Script/WindowsTargetPlatform.WindowsTargetSettings]\nDefaultGraphicsRHI=DefaultGraphicsRHI_DX12\n",
        encoding="utf-8")


def evidence(root):
    result = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and path.suffix.lower() in {".png", ".fbx", ".blend", ".uasset", ".umap"}:
            if any(x in path.parts for x in ("Intermediate", "DerivedDataCache", "Saved")):
                continue
            result.append({"path": str(path), "size": path.stat().st_size, "sha256": sha(path)})
    return result


def verify_review(cfg, *, persist=True):
    """Independent CHECK: verify fixed files without importing or modifying Content."""
    from unattended.art_tripo import TripoClient
    root = Path(cfg["artifacts_dir"])
    report = read(root / "review-package.json")
    if (report.get("batch_id") != cfg["batch_id"] or report.get("approved_levels") != cfg["approved_levels"] or
            report.get("full_batch_released") is not False):
        raise RuntimeError("Review scope does not match preview authorization")
    manifest = Path(report["manifest"])
    project = root / "UE/HousingPreview/HousingPreview.uproject"
    imported = root / "receipts" / (report["ue_import"]["run"] + "-import.json")
    captured = root / "receipts" / (report["capture"]["capture_id"] + "-capture.json")
    expected_capture = capture_fingerprint(report["ue_import"]["run"], imported,
                                           REPO / "art_tools/ue/capture_preview.py")
    # Read the current local ledger under its lock. status() never contacts Tripo
    # or opens the key file; the public review package is only an old snapshot.
    current_budget = TripoClient(cfg["private_state_dir"], cfg["key_file"],
                                budget_limit=cfg["budget_limit"]).status()["budget"]
    checks = {"processed_artifacts": verify_processed(manifest),
              "ue_assets": verify_import(imported, project, REPO / "art_tools/ue/import_preview.py", manifest),
              "fresh_capture_artifacts": verify_capture(captured),
              "capture_matches_current_import": report["capture"]["capture_id"] == expected_capture,
              "import_receipt_matches_report": report["ue_import"] == read(imported),
              "capture_receipt_matches_report": report["capture"] == read(captured),
              "budget_within_cap": (current_budget["limit"] == cfg["budget_limit"] and
                                    current_budget["committed"] <= cfg["budget_limit"] and
                                    current_budget["breached"] is False),
              "budget_settled": current_budget["reserved"] == 0 and current_budget["unknown"] == 0,
              "budget_matches_report": current_budget == report["budget"]["budget"],
              "recipe_matches_current_inputs": recipe_matches_review(cfg, report, manifest),
              "blender_matches_current_inputs": blender_matches_review(cfg, report, manifest),
              "blender_job_matches_current_inputs": blender_job_matches_review(cfg, report, manifest),
              "all_file_digests": bool(report.get("files")) and all(
                  Path(f["path"]).is_file() and sha(f["path"]) == f["sha256"] for f in report["files"])}
    result = {"status": "passed" if all(checks.values()) else "failed", "checks": checks,
              "budget": current_budget,
              "review_package_sha256": sha(root / "review-package.json"), "checked_epoch": time.time(),
              "human_visual_acceptance": "pending", "full_batch_released": False}
    if persist:
        atomic(root / "independent-check.json", result)
        log("independent_check", **result)
    if not all(checks.values()):
        raise RuntimeError("Independent asset check failed")
    return result


def run_preview(cfg, until="capture"):
    from unattended.art_tripo import TripoClient
    root = Path(cfg["artifacts_dir"])
    root.mkdir(parents=True, exist_ok=True)
    statefile = root / "pipeline-state.json"
    with exclusive(Path(cfg["private_state_dir"]) / "pipeline.lock"):
        state = read(statefile) if statefile.is_file() else {"version": 1, "batch_id": cfg["batch_id"]}
        client = TripoClient(cfg["private_state_dir"], cfg["key_file"], budget_limit=cfg["budget_limit"])
        def update(stage, **extra):
            if stage != "failed":
                state.pop("error", None)
                state.pop("error_type", None)
            state.update(stage=stage, updated_epoch=time.time(), **extra)
            atomic(statefile, state)
            log(stage, **extra)
        try:
            cfg, provenance, recipe_bytes = recipe_overlay(cfg)
            job_provenance, job_bytes = blender_job_snapshot(cfg)
            if job_provenance is not None:
                snapshot = Path(job_provenance["snapshot"])
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                temporary = snapshot.with_name(snapshot.name + "." + uuid.uuid4().hex + ".tmp")
                temporary.write_bytes(job_bytes)
                os.replace(temporary, snapshot)
                state["blender_job"] = job_provenance
            else:
                state.pop("blender_job", None)
            if provenance is not None:
                snapshot = Path(provenance["snapshot"])
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                temporary = snapshot.with_name(snapshot.name + "." + uuid.uuid4().hex + ".tmp")
                with temporary.open("wb") as stream:
                    stream.write(recipe_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, snapshot)
                state["recipe"] = provenance
            else:
                state.pop("recipe", None)
            prepared = []
            for a in cfg["assets"]:
                tag = a["asset_id"] + "_" + a.get("revision", "v001")
                update("concept", asset=a["asset_id"])
                body = {"input": cfg["style_reference_task_id"], "prompt": a["prompt"],
                        "model": "seedream_v5", "size": "2560x2048", "output_format": "png"}
                concept = client.run(tag + "_concept", "generation/image-to-image", body, 5,
                                     required_outputs=["generated_image_url"], timeout_seconds=1800)
                concept_path = ready_file(concept, "generated_image_url")
                public_concept = root / "concepts" / (tag + ".png")
                public_concept.parent.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copyfile(concept_path, public_concept)
                update("model", asset=a["asset_id"])
                model = client.run(tag + "_model", "generation/image-to-model", {
                    "input": "@" + tag + "_concept", "model": "v3.1-20260211",
                    "texture": True, "pbr": True, "texture_quality": "detailed",
                    "texture_version": "v3.5-20260815", "delight": True,
                    "geometry_quality": "detailed", "face_limit": 140000,
                    "auto_size": True, "texture_alignment": "original_image"}, 60,
                    required_outputs=["model_url"], timeout_seconds=2400)
                source = ready_file(model, "model_url")
                dst = root / "sources" / (tag + ".glb")
                dst.parent.mkdir(exist_ok=True)
                shutil.copyfile(source, dst)
                prepared.append({"asset_id": a["asset_id"], "level": a["level"], "input_glb": str(dst),
                                 "target_height_m": a["target_height_m"],
                                 "max_footprint_m": a["max_footprint_m"],
                                 "front_rotation_deg": a.get("front_rotation_deg", 0),
                                 "source_sha256": sha(dst), "concept_sha256": sha(public_concept)})
            update("generated", budget=client.status())
            if until == "models":
                return state
            manifest = {"schema_version": 1, "batch_id": cfg["batch_id"],
                        "style": {"id": "A", "version": "A-refined-v001", "detail_preserving": True},
                        "plot_size_m": 16, "assets": prepared,
                        "blender_provenance": blender_provenance()}
            if provenance is not None:
                manifest["recipe"] = provenance
            if job_provenance is not None:
                manifest["blender_job"] = job_provenance
            manifest_path = root / "production-input.json"
            atomic(manifest_path, manifest)
            blender_script = REPO / "art_tools/blender/prepare_batch.py"
            recipe = blender_fingerprint(manifest)
            output = root / "processed" / recipe
            output_manifest = output / "assets_manifest.json"
            if not verify_processed(output_manifest, source_manifest=manifest_path):
                update("blender", recipe=recipe)
                process([cfg["blender"], "-b", "--factory-startup", "--python-exit-code", "1", "--python",
                         str(blender_script), "--", str(manifest_path), str(output)],
                        root / "logs" / (recipe + "-blender.log"), timeout=2400, env=blender_environment())
            if not verify_processed(output_manifest, source_manifest=manifest_path):
                raise RuntimeError("Blender exited without an assets manifest")
            update("processed", manifest=str(output_manifest))
            if until == "prepare":
                return state
            project = root / "UE/HousingPreview/HousingPreview.uproject"
            require_free_project(project)
            write_project(project)
            env = os.environ.copy()
            env["ZG_ART_MANIFEST"] = str(output_manifest)
            env["ZG_ART_OUTPUT"] = str(root)
            env["ZG_ART_RUN"] = recipe
            engine = Path(cfg["engine"])
            update("ue_import")
            receipt = root / "receipts" / (recipe + "-import.json")
            script = REPO / "art_tools/ue/import_preview.py"
            if not verify_import(receipt, project, script, output_manifest):
                process([str(engine / "Engine/Binaries/Win64/UnrealEditor-Cmd.exe"), str(project),
                         "-ExecutePythonScript=" + str(script), "-unattended", "-nosplash", "-NoLiveCoding",
                         "-nullrhi", "-NoSound", "-DisablePlugins=Fab", "-stdout", "-FullStdOutLogOutput"],
                        root / "logs" / (recipe + "-ue-import.log"), timeout=2400, env=env)
            if not verify_import(receipt, project, script, output_manifest):
                raise RuntimeError("UE import did not produce a passing receipt")
            update("imported", project=str(project), import_receipt=str(receipt))
            if until == "import":
                return state
            update("ue_capture")
            capture_id = capture_fingerprint(recipe, receipt, REPO / "art_tools/ue/capture_preview.py")
            env["ZG_ART_CAPTURE_ID"] = capture_id
            capture_receipt = root / "receipts" / (capture_id + "-capture.json")
            if not verify_capture(capture_receipt):
                process([str(engine / "Engine/Binaries/Win64/UnrealEditor.exe"), str(project),
                         "-ExecutePythonScript=" + str(REPO / "art_tools/ue/capture_preview.py"),
                         "-unattended", "-nosplash", "-NoLiveCoding", "-NoSound", "-DisablePlugins=Fab",
                         "-windowed", "-ResX=1920", "-ResY=1080", "-RenderOffscreen",
                         "-stdout", "-FullStdOutLogOutput"],
                        root / "logs" / (capture_id + "-ue-capture.log"), timeout=1800, env=env)
            if not verify_capture(capture_receipt):
                raise RuntimeError("Fresh rendered screenshots were not verified")
            report = {"version": 1, "batch_id": cfg["batch_id"], "status": "pending_visual_acceptance",
                      "approved_levels": cfg["approved_levels"], "full_batch_released": False,
                      "budget": client.status(), "references": cfg["references"],
                      "blender_provenance": manifest["blender_provenance"],
                      "manifest": str(output_manifest), "ue_import": read(receipt),
                      "capture": read(capture_receipt), "files": evidence(root),
                      "limitations": ["Standalone UE 5.8 preview; gameplay footprint unchanged",
                                      "Human visual acceptance pending; remaining five levels not authorized"]}
            if provenance is not None:
                report["recipe"] = provenance
            if job_provenance is not None:
                report["blender_job"] = job_provenance
            atomic(root / "review-package.json", report)
            from art_tools.review_gallery import build as build_review_gallery
            build_review_gallery(root)
            update("pending_visual_acceptance", review_package=str(root / "review-package.json"),
                   review_gallery=str(root / "住宅美术预览.html"), budget=client.status())
            return state
        except Exception as exc:
            update("failed", error=str(exc), error_type=type(exc).__name__)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("command", choices=("preview", "status", "verify"))
    parser.add_argument("--until", choices=("models", "prepare", "import", "capture"), default="capture")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if args.command == "status":
        file = Path(cfg["artifacts_dir"]) / "pipeline-state.json"
        print(json.dumps(read(file) if file.is_file() else {"stage": "not_started"}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "verify":
        verify_review(cfg)
        return 0
    run_preview(cfg, args.until)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        log("error", error_type=type(error).__name__, message=str(error))
        raise SystemExit(1)
