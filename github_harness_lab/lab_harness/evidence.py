"""Controller-created evidence. Never infer test success from agent prose."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_manifest(value: dict) -> None:
    """Small stdlib invariant validator; full published JSON Schema is also supplied."""
    required = {"schema_version", "mode", "issue", "pr", "base_commit_sha", "result_commit_sha", "harness", "started_at", "finished_at", "changed_files", "tests", "ci", "artifacts", "blocked_checks", "status"}
    if not required <= value.keys() or value["schema_version"] != "1.0":
        raise ValueError("incomplete manifest")
    if value["status"] not in {"REVIEW", "FAILED", "BLOCKED"}:
        raise ValueError("invalid evidence status")
    if value["ci"].get("status") not in {"NOT_RUN", "PENDING", "PASS", "FAIL", "BLOCKED"}:
        raise ValueError("invalid CI status")
    start, end = [datetime.fromisoformat(value[x].replace("Z", "+00:00")) for x in ("started_at", "finished_at")]
    if start.tzinfo is None or end.tzinfo is None or end < start:
        raise ValueError("invalid evidence timestamps")
    for name in ("base_commit_sha", "result_commit_sha"):
        if value[name] is not None and not re.fullmatch(r"[0-9a-f]{40}", value[name]):
            raise ValueError("invalid commit SHA")
    if value["status"] == "REVIEW":
        h = value["harness"]
        if not value["base_commit_sha"] or not value["result_commit_sha"] or type(h["exit_code"]) is not int or h["exit_code"] != 0 or h["final_kind"] != "completed" or not h["session_id"]:
            raise ValueError("REVIEW requires process and structured completion evidence")
        if not value["tests"] or any(type(t["exit_code"]) is not int or t["exit_code"] != 0 or t["timed_out"] is not False or t.get("output_limit_exceeded", False) is not False for t in value["tests"]):
            raise ValueError("REVIEW requires real successful tests")
        if not value["changed_files"]:
            raise ValueError("REVIEW requires a verified Git diff")
    if value["mode"] not in {"dry-run", "offline-demo", "live"}:
        raise ValueError("unknown execution mode")
