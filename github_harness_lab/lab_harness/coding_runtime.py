"""Fixed-commit materialization and bounded reviewed checks; no live entrypoint.

Repository files are untrusted. Git blobs are verified before writing and LFS
objects can only be satisfied from matching, ordinary local files. Nothing in
this module fetches LFS, reuses a project build tree, or interprets Issue commands.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
import string
import uuid

from .coding_contract import expand_paths, validate_profile, validate_task_id
from .policy import safe_path, under
from .process import run_process, safe_environment
from .project_profile import repository_namespace

MAX_FILES = 1000
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_LOG_BYTES = 16 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_LFS = re.compile(rb"version https://git-lfs.github.com/spec/v1\noid sha256:([0-9a-f]{64})\nsize ([0-9]+)\n?\Z")


class RuntimeBlocked(ValueError):
    """Materialization or check evidence cannot safely establish a result."""


def _utc():
    return datetime.now(timezone.utc)


def _deadline(activation):
    if activation.get("enabled") is not True:
        raise RuntimeBlocked("A separate enabled activation is required")
    if "expires_at" in activation and activation["expires_at"] is None:
        return None  # Explicit continuous activation; individual checks stay bounded.
    try:
        end = datetime.fromisoformat(activation["expires_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, TypeError, ValueError):
        raise RuntimeBlocked("Invalid activation deadline") from None
    if end.tzinfo is None or end.utcoffset().total_seconds() != 0:
        raise RuntimeBlocked("Activation deadline must be UTC")
    return end


def _ordinary(path: Path, *, file=False):
    """Reject redirected parents and multiply linked final files before access."""
    path = Path(os.path.abspath(path))
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeBlocked("Linked or reparse paths are not supported")
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise RuntimeBlocked("A parent path is not an ordinary directory")
    if file:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeBlocked("An ordinary single-link file is required")
    return path


def _read_file(path: Path, maximum: int) -> bytes:
    path = _ordinary(path, file=True)
    before = path.stat()
    if before.st_size > maximum:
        raise RuntimeBlocked("Local file exceeds the byte limit")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_nlink) != (before.st_dev, before.st_ino, 1):
            raise RuntimeBlocked("Local file changed during validation")
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    if (len(data) > maximum or before.st_size != len(data)
            or (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns)):
        raise RuntimeBlocked("Local file changed or exceeded the byte limit")
    return data


def _reject_user_workspace(path, profile):
    source = profile["source_repository"]
    if source and path.is_relative_to(Path(source)):
        raise RuntimeBlocked("The user source repository cannot be used as a build workspace")
    if os.name == "nt" and path.is_relative_to(Path(r"C:\ue_game_build")):
        raise RuntimeBlocked("The existing user build directory cannot be reused")


def resolve_lfs(profile, path, pointer) -> bytes:
    """Resolve an exact Git pointer from local cache/worktree, never the network."""
    normalized = pointer.replace(b"\r\n", b"\n")
    match = _LFS.fullmatch(normalized)
    if not match:
        raise RuntimeBlocked("Unsupported or malformed Git LFS pointer")
    oid, size = match[1].decode("ascii"), int(match[2])
    if size > MAX_FILE_BYTES:
        raise RuntimeBlocked("LFS object exceeds the single-file limit")
    source = profile.get("source_repository")
    if not source or not Path(source).is_absolute():
        raise RuntimeBlocked("LFS object requires a configured local source repository")
    source = _ordinary(Path(source))
    relative = safe_path(path)
    if relative != path:
        raise RuntimeBlocked("Ambiguous LFS path")
    candidates = [source / ".git" / "lfs" / "objects" / oid[:2] / oid[2:4] / oid,
                  source.joinpath(*relative.split("/"))]
    for candidate in candidates:
        try:
            data = _read_file(candidate, MAX_FILE_BYTES)
        except FileNotFoundError:
            continue
        if len(data) == size and hashlib.sha256(data).hexdigest() == oid:
            return data
        # A stale working tree/cache is not evidence for a fixed Git object.
    raise RuntimeBlocked("No verified local LFS object matches the fixed commit")


def prepare_workspace(profile, task, activation, root, client, *, process=run_process) -> Path:
    """Create one new ASCII tree from task.base_sha using read-only Git object APIs.

    Full authorization and task/parent validation belong to the calling engine.
    The injected process is retained for interface compatibility; it is not used.
    An interrupted/failed directory is preserved and cannot silently be reused.
    """
    profile = validate_profile(profile)
    deadline = _deadline(activation)
    if deadline is not None and deadline <= _utc():
        raise RuntimeBlocked("Activation expired before materialization")
    if client.repository != profile["repository"] or activation.get("repository") != profile["repository"]:
        raise RuntimeBlocked("Repository binding mismatch")
    sha = task.get("base_sha")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise RuntimeBlocked("A fixed lowercase Git commit SHA is required")
    validate_task_id(task.get("task_id"))
    if type(task.get("number")) is not int or task["number"] < 1:
        raise RuntimeBlocked("A positive task sequence number is required")
    nonce = activation.get("nonce")
    try:
        if not isinstance(nonce, str) or str(uuid.UUID(nonce)) != nonce:
            raise ValueError()
    except (ValueError, AttributeError):
        raise RuntimeBlocked("A canonical activation nonce is required") from None
    root = Path(root)
    if not root.is_absolute() or not str(root).isascii():
        raise RuntimeBlocked("Controller root must be an absolute ASCII path")
    root = _ordinary(root)
    workspace = root / ".runtime" / "coding" / repository_namespace(profile["repository"]) / nonce / str(task["number"]) / "workspace"
    _reject_user_workspace(root, profile)
    _reject_user_workspace(workspace, profile)
    _ordinary(workspace)
    _ordinary(workspace.parent)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(exist_ok=False)
    scopes = expand_paths(profile, task["task_id"], "materialize_paths")

    def read(endpoint, **kwargs):
        if deadline is not None and _utc() >= deadline:
            raise RuntimeBlocked("Activation expired during materialization")
        result = client.request("GET", endpoint, **kwargs)
        if deadline is not None and _utc() >= deadline:
            raise RuntimeBlocked("Activation expired during materialization")
        return result

    commit = read("/git/commits/" + sha)
    if not isinstance(commit, dict) or commit.get("sha") != sha:
        raise RuntimeBlocked("Git commit response does not match the requested SHA")
    tree_sha = commit.get("tree", {}).get("sha")
    if not isinstance(tree_sha, str) or not _SHA.fullmatch(tree_sha):
        raise RuntimeBlocked("Git commit has no valid tree SHA")
    tree = read("/git/trees/" + tree_sha, params={"recursive": 1})
    if (not isinstance(tree, dict) or tree.get("sha") != tree_sha or tree.get("truncated") is not False
            or type(tree.get("tree")) is not list or len(tree["tree"]) > MAX_FILES):
        raise RuntimeBlocked("A complete bounded Git tree is required")
    selected, seen, total, matched = [], {}, 0, set()
    for item in tree["tree"]:
        if type(item) is not dict:
            raise RuntimeBlocked("Malformed Git tree entry")
        path = safe_path(item.get("path"))
        if path != item.get("path") or not path.isascii():
            raise RuntimeBlocked("Git paths must be unambiguous ASCII paths")
        if path.casefold() in seen:
            raise RuntimeBlocked("Git paths collide on this filesystem")
        seen[path.casefold()] = item.get("type")
        wanted = [scope for scope in scopes if under(path, scope)]
        if not wanted:
            continue
        matched.update(wanted)
        if item.get("type") == "tree" and item.get("mode") == "040000":
            continue
        size, blob_sha = item.get("size"), item.get("sha")
        if (item.get("type") != "blob" or item.get("mode") not in {"100644", "100755"}
                or type(size) is not int or not 0 <= size <= MAX_FILE_BYTES
                or not isinstance(blob_sha, str) or not _SHA.fullmatch(blob_sha)):
            raise RuntimeBlocked("Only bounded ordinary Git blobs can be materialized")
        total += size
        if total > MAX_TOTAL_BYTES or len(selected) >= MAX_FILES:
            raise RuntimeBlocked("Materialization exceeds the reviewed size bounds")
        selected.append((path, size, blob_sha))
    if set(scopes) != matched:
        raise RuntimeBlocked("A configured materialization path is absent at the fixed commit")
    for path in seen:
        if any(seen.get(str(parent).replace("\\", "/").casefold()) not in (None, "tree") for parent in Path(path).parents if str(parent) != "."):
            raise RuntimeBlocked("Git tree contains a file/directory collision")
    actual_total = 0
    for path, size, blob_sha in selected:
        blob = read("/git/blobs/" + blob_sha)
        if (not isinstance(blob, dict) or blob.get("sha") != blob_sha or blob.get("size") != size
                or blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str)
                or len(blob["content"]) > (MAX_FILE_BYTES * 4 // 3 + 4) * 2):
            raise RuntimeBlocked("Invalid or oversized Git blob response")
        try:
            data = base64.b64decode(blob["content"].replace("\n", ""), validate=True)
        except (ValueError, UnicodeError):
            raise RuntimeBlocked("Invalid base64 Git blob content") from None
        if len(data) != size or hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest() != blob_sha:
            raise RuntimeBlocked("Git blob content failed object integrity verification")
        if data.startswith(b"version https://git-lfs.github.com/spec/"):
            data = resolve_lfs(profile, path, data)
        actual_total += len(data)
        if actual_total > MAX_TOTAL_BYTES:
            raise RuntimeBlocked("Resolved content exceeds the total byte limit")
        target = workspace.joinpath(*path.split("/"))
        _ordinary(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
    return workspace


def _summary(identity, status, reason, *, result=None, stdout=None, stderr=None, duration=0):
    return {"id": identity, "status": status,
            "exit_code": result.exit_code if result is not None else None,
            "timed_out": bool(result.timed_out) if result is not None else False,
            "output_limit_exceeded": bool(result.output_limit_exceeded) if result is not None else False,
            "stdout_sha256": hashlib.sha256(stdout).hexdigest() if stdout is not None else None,
            "stderr_sha256": hashlib.sha256(stderr).hexdigest() if stderr is not None else None,
            "duration_seconds": max(0, duration), "reason": reason}


def run_checks(profile, task, activation, workspace, changed_paths, output, *, process=run_process, now=None) -> list[dict]:
    """Run only configured exact-file checks, with literal output evidence rules."""
    profile = validate_profile(profile)
    clock = _utc if now is None else now
    deadline = _deadline(activation)
    validate_task_id(task.get("task_id"))
    allowed = set(expand_paths(profile, task["task_id"]))
    if type(changed_paths) is not list or any(path not in allowed for path in changed_paths):
        raise RuntimeBlocked("Changed paths must be exact reviewed files")
    workspace, output = _ordinary(Path(workspace)), _ordinary(Path(output))
    _reject_user_workspace(workspace, profile)
    _reject_user_workspace(output, profile)
    if not workspace.is_dir() or not str(workspace).isascii():
        raise RuntimeBlocked("Checks require the new ASCII workspace")
    summaries, stopped = [], False
    for spec in profile["checks"]:
        identity = spec["id"]
        triggers = {path.replace("{task_id}", task["task_id"]) for path in spec["paths"]}
        if not set(changed_paths) & triggers:
            summaries.append(_summary(identity, "NOT_REQUIRED", "No changed file triggers this reviewed check"))
            continue
        started = clock()
        if stopped or (deadline is not None and started >= deadline):
            stopped = True
            summaries.append(_summary(identity, "NOT_RUN", "A previous check failed or the activation deadline elapsed"))
            continue
        check_output = output / identity
        result = stdout = stderr = None
        try:
            _ordinary(check_output)
            check_output.mkdir(parents=True, exist_ok=False)
            replacements = {"workspace": str(workspace), "output": str(check_output), "task_id": task["task_id"]}
            argv = []
            for argument in spec["argv"]:
                for _, field, format_spec, conversion in string.Formatter().parse(argument):
                    if field is not None and (field not in replacements or format_spec or conversion):
                        raise RuntimeBlocked("Unsupported trusted command placeholder")
                argv.append(argument.format_map(replacements))
            executable = Path(argv[0])
            if (not executable.is_absolute() or executable.suffix.lower() in {".bat", ".cmd"}
                    or executable.is_relative_to(workspace)):
                raise RuntimeBlocked("Checks require a trusted absolute native executable outside the workspace")
            _ordinary(executable, file=True)
            environment = safe_environment()
            if executable.name.lower() in {"dotnet", "dotnet.exe"}:
                environment.update(DOTNET_ROOT=str(executable.parent), DOTNET_MULTILEVEL_LOOKUP="0", DOTNET_ROLL_FORWARD="LatestMajor")
            timeout = spec["timeout_seconds"]
            if deadline is not None:
                timeout = min(timeout, (deadline - clock()).total_seconds())
            if timeout <= 0:
                raise RuntimeBlocked("Activation expired before starting the check")
            result = process(argv, cwd=workspace, output_dir=check_output, timeout_seconds=timeout,
                             environment=environment, max_output_bytes=MAX_LOG_BYTES)
            if (Path(result.stdout_path) != check_output / "stdout.log"
                    or Path(result.stderr_path) != check_output / "stderr.log"):
                raise RuntimeBlocked("Unexpected subprocess log locations")
            stdout = _read_file(result.stdout_path, MAX_LOG_BYTES)
            stderr = _read_file(result.stderr_path, MAX_LOG_BYTES)
            text = stdout.decode("utf-8", "replace") + "\n" + stderr.decode("utf-8", "replace")
            reason = "Reviewed command and required literal evidence succeeded"
            status = "PASS"
            if result.timed_out:
                status, reason = "FAIL", "Check exceeded its bounded timeout"
            elif result.output_limit_exceeded or len(stdout) + len(stderr) > MAX_LOG_BYTES:
                status, reason = "FAIL", "Check output exceeded its byte limit"
            elif result.exit_code != 0:
                status, reason = "FAIL", "Reviewed command exited unsuccessfully"
            elif deadline is not None and clock() >= deadline:
                status, reason = "FAIL", "Activation expired before check evidence was complete"
            elif any(pattern in text for pattern in spec["failure_patterns"]):
                status, reason = "FAIL", "A configured literal failure marker was found"
            elif not all(pattern in text for pattern in spec["success_patterns"]):
                status, reason = "FAIL", "Required literal success evidence is missing"
        except Exception:
            status, reason = "FAIL", "Check could not produce trustworthy bounded evidence"
        stopped = status == "FAIL"
        summaries.append(_summary(identity, status, reason, result=result, stdout=stdout, stderr=stderr,
                                  duration=(clock() - started).total_seconds()))
    return summaries
