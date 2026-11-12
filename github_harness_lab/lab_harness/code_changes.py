"""Validate and apply bounded text changes; no model, network or code execution.

The caller must own an exclusive, isolated workspace. These checks reject links
and stale files, but do not provide an OS sandbox against concurrent hostile
filesystem mutation. Validation precedes writes. Each file replacement is
atomic; the complete multi-file operation is deliberately NOT a transaction.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unicodedata

SCHEMA = "harness-code-change/v1"
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_CHANGES = 16
MAX_ALLOWED_PATHS = 64
MAX_CONTENT_BYTES = 128 * 1024
MAX_TOTAL_CONTENT_BYTES = 512 * 1024
MAX_ORIGINAL_BYTES = 4 * 1024 * 1024
MAX_ORIGINAL_TOTAL_BYTES = 16 * 1024 * 1024
MAX_EDITS = 64
_FIELDS = {"schema", "repository", "base_sha", "task_id", "changes"}
_COMMON_CHANGE_FIELDS = {"path", "operation", "before_sha256"}
_PROTECTED = {".git", ".github", ".agents", ".codex", ".runtime", ".ssh", ".aws",
              ".azure", "lab_harness", "live_tools", "harnessbridge", "agents.md",
              ".npmrc", ".pypirc", ".netrc", "id_rsa", "id_ed25519", "id_ecdsa"}
_SENSITIVE = re.compile(r"(?:credentials?|secrets?|tokens?|api[-_]?keys?|config\.local|settings\.local)(?:[._-]|$)", re.I)
_SEGMENT = re.compile(r"[A-Za-z0-9_. ()\-\u3400-\u4dbf\u4e00-\u9fff]+\Z")
_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])\Z", re.I)


class ApplyChangeSetError(OSError):
    """Application stopped; applied_paths lists files already replaced."""

    def __init__(self, applied_paths, failed_path):
        self.applied_paths = tuple(applied_paths)
        self.failed_path = failed_path
        super().__init__(f"Code change application stopped at {failed_path!r}; "
                         f"{len(self.applied_paths)} file(s) already applied; inspect before retrying")


class _ValidatedChangeSet(dict):
    """In-memory validation receipt; serialized data must be validated again."""

    def __init__(self, data, allowed_paths):
        super().__init__(data)
        self._allowed_paths = tuple(allowed_paths)
        self._digest = _digest(_canonical(data))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON keys are forbidden")
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError("Non-finite JSON values are forbidden")


def _object(value, fields, label):
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} has missing or unknown fields")


def _text_bytes(value, limit, label):
    if type(value) is not str or "\x00" in value:
        raise ValueError(f"{label} must be text without NUL")
    try:
        data = value.encode("utf-8")
    except UnicodeError as error:
        raise ValueError(f"{label} must be valid UTF-8 text") from error
    if len(data) > limit:
        raise ValueError(f"{label} exceeds its byte limit")
    return data


def _path(value, *, protected=True):
    if (type(value) is not str or not value or len(value) > 240
            or unicodedata.normalize("NFC", value) != value):
        raise ValueError("Paths must be bounded canonical relative text paths")
    parts = value.split("/")
    for part in parts:
        if (part in {"", ".", ".."} or not _SEGMENT.fullmatch(part)
                or part != part.strip(" ") or part.endswith(".")
                or _DEVICE.fullmatch(part.split(".", 1)[0].rstrip(" "))):
            raise ValueError("Ambiguous, absolute, device or traversal path")
        folded = part.casefold()
        if protected and (folded in _PROTECTED or folded.startswith(".env")
                or _SENSITIVE.match(folded) or "密钥" in part or "凭据" in part
                or folded.endswith((".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".kdbx"))):
            raise ValueError("Protected or credential paths cannot be changed")
    return value


def _paths(values, label, *, protected=True):
    seen = {}
    spelling = {}
    for value in values:
        path = _path(value, protected=protected)
        pieces = path.split("/")
        for length in range(1, len(pieces) + 1):
            prefix = "/".join(pieces[:length])
            prior = spelling.setdefault(prefix.casefold(), prefix)
            if prior != prefix:
                raise ValueError(f"{label} contains case-colliding directory names")
        folded = path.casefold()
        if folded in seen:
            raise ValueError(f"{label} contains duplicate or case-colliding paths")
        seen[folded] = path
    keys = sorted(seen)
    if any(right.startswith(left + "/") for index, left in enumerate(keys) for right in keys[index + 1:]):
        raise ValueError(f"{label} has file/directory overlaps")
    return set(seen.values())


def _render(change, originals):
    if change["operation"] != "replace":
        return change["content"]
    content = originals[change["path"]]
    for edit in change["edits"]:
        if not edit["old"] or content.count(edit["old"]) != 1:
            raise ValueError("Each replace old fragment must match exactly once at that step")
        content = content.replace(edit["old"], edit["new"], 1)
    return content


def validate_change_set(raw_json: str, *, repository: str, base_sha: str, task_id: str,
                        originals: dict[str, str], allowed_paths: list[str]) -> dict:
    """Return a JSON-serializable receipt bound to the exact caller allowlist.

    add/modify use content; replace uses sequential [{old,new}] edits instead.
    The SHA256 is over exact UTF-8 bytes, including line endings and any BOM.
    """
    _text_bytes(raw_json, MAX_JSON_BYTES, "change-set JSON")
    if (type(repository) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", repository)
            or repository.split("/")[1] in {".", ".."}):
        raise ValueError("repository must be an owner/name identifier")
    if type(base_sha) is not str or not re.fullmatch(r"[0-9a-f]{40}", base_sha):
        raise ValueError("base_sha must be a full lowercase commit SHA")
    if type(task_id) is not str or not re.fullmatch(r"TASK[-_][A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*", task_id) or len(task_id) > 100:
        raise ValueError("task_id must be a bounded TASK identifier")
    if type(allowed_paths) is not list or not 1 <= len(allowed_paths) <= MAX_ALLOWED_PATHS:
        raise ValueError("An explicit bounded file allowlist is required")
    allowed = _paths(allowed_paths, "allowed_paths")
    if type(originals) is not dict or len(originals) > MAX_ALLOWED_PATHS:
        raise ValueError("originals must be a bounded path/text mapping")
    original_paths = _paths(originals, "originals", protected=False)
    total_original = sum(len(_text_bytes(text, MAX_ORIGINAL_BYTES, "original")) for text in originals.values())
    if total_original > MAX_ORIGINAL_TOTAL_BYTES:
        raise ValueError("originals exceed the total byte limit")
    # Case-only disagreement with context paths must not become a new file.
    original_case = {path.casefold(): path for path in original_paths}
    if any(path.casefold() in original_case and original_case[path.casefold()] != path for path in allowed):
        raise ValueError("Allowlist and originals disagree on filename case")
    try:
        data = json.loads(raw_json, object_pairs_hook=_unique, parse_constant=_invalid_constant)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Invalid change-set JSON") from error
    _object(data, _FIELDS, "change set")
    if data["schema"] != SCHEMA or any(data[key] != value for key, value in
            (("repository", repository), ("base_sha", base_sha), ("task_id", task_id))):
        raise ValueError("Change-set schema or task binding mismatch")
    changes = data["changes"]
    if type(changes) is not list or not 1 <= len(changes) <= MAX_CHANGES:
        raise ValueError("changes must contain a bounded, nonempty list")
    changed_paths, total = [], 0
    for change in changes:
        if (type(change) is not dict or type(change.get("operation")) is not str
                or change["operation"] not in {"add", "modify", "replace"}):
            raise ValueError("Only add, modify and replace operations are supported")
        operation = change["operation"]
        _object(change, _COMMON_CHANGE_FIELDS | ({"edits"} if operation == "replace" else {"content"}), "change")
        path = _path(change["path"])
        if path not in allowed:
            raise ValueError("Change path is not explicitly allowed")
        changed_paths.append(path)
        if operation == "add":
            if path in originals or change["before_sha256"] is not None:
                raise ValueError("add requires an absent original and null before_sha256")
        elif path not in originals or change["before_sha256"] != _digest(originals[path]):
            raise ValueError("Original file hash mismatch")
        if operation == "replace":
            edits = change["edits"]
            if type(edits) is not list or not 1 <= len(edits) <= MAX_EDITS:
                raise ValueError("replace requires bounded nonempty edits")
            for edit in edits:
                _object(edit, {"old", "new"}, "edit")
                total += len(_text_bytes(edit["old"], MAX_CONTENT_BYTES, "old fragment"))
                total += len(_text_bytes(edit["new"], MAX_CONTENT_BYTES, "new fragment"))
        else:
            total += len(_text_bytes(change["content"], MAX_CONTENT_BYTES, "content"))
        if total > MAX_TOTAL_CONTENT_BYTES:
            raise ValueError("Model changes exceed the total byte limit")
        result = _render(change, originals)
        _text_bytes(result, MAX_ORIGINAL_BYTES, "result file")
        if operation != "add" and result == originals[path]:
            raise ValueError("A modification must change file content")
    _paths(changed_paths, "changes")
    return _ValidatedChangeSet(data, allowed_paths)


def _safe_stat(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Symlinks, junctions and reparse points are forbidden")
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise ValueError("Hard-linked files are forbidden")
    return info


def _workspace(path):
    root = Path(path).absolute()
    for ancestor in (*reversed(root.parents), root):
        if not stat.S_ISDIR(_safe_stat(ancestor).st_mode):
            raise ValueError("Workspace ancestors must be ordinary directories")
    return root


def _target(root, relative):
    current = root
    missing = False
    parts = relative.split("/")
    for index, part in enumerate(parts):
        if not missing:
            matches = [entry.name for entry in current.iterdir() if entry.name.casefold() == part.casefold()]
            if matches and matches != [part]:
                raise ValueError("An existing filename has conflicting case")
        current /= part
        try:
            info = _safe_stat(current)
        except FileNotFoundError:
            missing = True
            continue
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ValueError("A target ancestor is not a directory")
        if index == len(parts) - 1 and not stat.S_ISREG(info.st_mode):
            raise ValueError("A target is not a regular file")
        if not current.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
            raise ValueError("Target resolved outside the workspace")
    return current


def _check_disk(root, change, originals):
    target = _target(root, change["path"])
    if change["operation"] == "add":
        if target.exists():
            raise ValueError("add target already exists")
    else:
        if not target.is_file() or target.stat().st_size > MAX_ORIGINAL_BYTES:
            raise ValueError("Original file is absent or oversized")
        with target.open("rb") as stream:
            current = stream.read(MAX_ORIGINAL_BYTES + 1)
        if current != originals[change["path"]].encode("utf-8"):
            raise ValueError("Workspace file differs from the validated original")
    return target


def apply_change_set(workspace: Path, validated: dict, *, originals: dict[str, str]) -> list[str]:
    """Apply to an exclusively owned workspace; stop on error, never auto-retry.

    All candidates are checked before any write. Partial I/O failure raises
    ApplyChangeSetError with applied_paths; earlier replacements are not rolled
    back. Serialized receipts must pass validate_change_set again before use.
    """
    if (not isinstance(validated, _ValidatedChangeSet)
            or _digest(_canonical(validated)) != validated._digest):
        raise ValueError("A fresh, unmodified validation receipt is required")
    checked = validate_change_set(_canonical(validated), repository=validated["repository"],
        base_sha=validated["base_sha"], task_id=validated["task_id"], originals=originals,
        allowed_paths=list(validated._allowed_paths))
    root = _workspace(workspace)
    targets = [_check_disk(root, change, originals) for change in checked["changes"]]
    prepared, created_dirs, applied = [], [], []
    failed_path = "preparation"
    try:
        for target, change in zip(targets, checked["changes"]):
            failed_path = change["path"]
            missing = []
            parent = target.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir()
                created_dirs.append(directory)
            descriptor, name = tempfile.mkstemp(prefix=".harness-code-", suffix=".tmp", dir=target.parent)
            temporary = Path(name)
            prepared.append((temporary, target, change))
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(_render(change, originals).encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            if target.exists():
                os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
        # Staging cannot turn a later validation failure into a partial update.
        _workspace(root)
        for change in checked["changes"]:
            _check_disk(root, change, originals)
        for temporary, target, change in prepared:
            failed_path = change["path"]
            _check_disk(root, change, originals)
            os.replace(temporary, target)
            applied.append(change["path"])
        return applied
    except (OSError, ValueError) as error:
        if isinstance(error, ValueError) and not applied:
            raise
        raise ApplyChangeSetError(applied, failed_path) from error
    finally:
        for temporary, _, _ in prepared:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
