"""Shell-free Codex, DSH and Claude Code adapters for the single-host runner.

Each invocation owns a fresh output directory. Raw protocol logs and process
facts are durable before a validated result is published. Credentials are
passed through process environments and kept out of receipts.
"""
from __future__ import annotations

import json
import errno
import hashlib
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit

from github_harness_lab.lab_harness.dsh import parse_events
from github_harness_lab.lab_harness.dsh_legacy import VERIFIED_VERSION, parse_legacy_events
from github_harness_lab.lab_harness.process import run_process, safe_environment
from . import ue_checks
from .config import (ART_CODING_MODEL, ART_CODING_REASONING_EFFORT,
                     validate_art_pipeline_settings, validate_model_settings)
from .secrets import read_secret
from .storage import atomic_replace


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["task", "complete", "blocked"]},
        "title": {"type": "string"}, "body": {"type": "string"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["action", "title", "body", "acceptance_criteria", "reason"],
    "additionalProperties": False,
}
PLAN_WITH_CORE_TESTS_SCHEMA = {
    **PLAN_SCHEMA,
    "properties": {**PLAN_SCHEMA["properties"], "core_tests": {"type": "array", "items": {"type": "string"}}},
    "required": [*PLAN_SCHEMA["required"], "core_tests"],
}
TIERED_PLAN_SCHEMA = {
    **PLAN_SCHEMA,
    "properties": {
        **PLAN_SCHEMA["properties"],
        "planning_level": {"type": "string", "enum": ["routine", "critical"]},
        "planning_reason": {"type": "string"},
    },
    "required": [*PLAN_SCHEMA["required"], "planning_level", "planning_reason"],
}
TIERED_PLAN_WITH_CORE_TESTS_SCHEMA = {
    **TIERED_PLAN_SCHEMA,
    "properties": {**TIERED_PLAN_SCHEMA["properties"],
                   "core_tests": PLAN_WITH_CORE_TESTS_SCHEMA["properties"]["core_tests"]},
    "required": [*TIERED_PLAN_SCHEMA["required"], "core_tests"],
}
_EXACT_CORE_TESTS_SCHEMA = {
    "type": "array",
    "description": (f"For a task, use 1..{ue_checks.MAX_CORE_TESTS} distinct exact test identifiers. "
                    "Use an empty array for complete or blocked. Put explanations and acceptance mappings in body."),
    "items": {"type": "string", "pattern": r"^ZhuGuoCity\.[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*$"},
}
STRICT_PLAN_WITH_CORE_TESTS_SCHEMA = {
    **PLAN_WITH_CORE_TESTS_SCHEMA,
    "properties": {**PLAN_WITH_CORE_TESTS_SCHEMA["properties"], "core_tests": _EXACT_CORE_TESTS_SCHEMA},
}
STRICT_TIERED_PLAN_WITH_CORE_TESTS_SCHEMA = {
    **TIERED_PLAN_WITH_CORE_TESTS_SCHEMA,
    "properties": {**TIERED_PLAN_WITH_CORE_TESTS_SCHEMA["properties"], "core_tests": _EXACT_CORE_TESTS_SCHEMA},
}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["accepted", "rework", "blocked"]},
        "summary": {"type": "string"}, "feedback": {"type": "string"},
        "head_sha": {"type": "string"},
    },
    "required": ["decision", "summary", "feedback", "head_sha"],
    "additionalProperties": False,
}
ART_DIAGNOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["continue", "needs_user", "blocked"]},
        "diagnosis": {"type": "string", "minLength": 1},
        "plan": {"type": "string", "minLength": 1},
        "reason": {"type": "string", "minLength": 1},
        "required_approvals": {"type": "array", "items": {"type": "string", "enum": [
            "style_reference", "budget_limit", "batch_scope", "approval_policy", "tool_permissions"]}},
        "questions": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "additional_tripo_credits": {"type": "integer", "minimum": 0},
        "head_sha": {"type": "string", "pattern": r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"},
    },
    "required": ["action", "diagnosis", "plan", "reason", "required_approvals", "questions",
                 "additional_tripo_credits", "head_sha"],
    "additionalProperties": False,
}
_ACCOUNT_METADATA = re.compile(
    r"\[llm-deepseek-account\] model listing from https://api\.deepseek\.com/models "
    r"loaded [0-9]{1,6} model\(s\), skipped [0-9]{1,6} row\(s\)"
)
_SESSION = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
_CLAUDE_SESSION = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_CLAUDE_RETRY_CODES = {"overloaded": "server_unavailable", "server_error": "server_unavailable",
                       "rate_limit": "rate_limited"}
_CLAUDE_ENV = ("CLAUDE_CODE_GIT_BASH_PATH", "NODE_EXTRA_CA_CERTS")
_CLAUDE_LIMIT_TEXT = re.compile(r"You(?:'|’)ve hit your limit · resets [^\r\n]{1,160}")
_LEGACY_CORE_TEST_ANNOTATION = re.compile(
    r"(ZhuGuoCity\.[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)"
    r"(?:（[^()（）\x00-\x1f\x7f]+）|\([^()（）\x00-\x1f\x7f]+\))\Z"
)
_CLAUDE_GIT_COMMANDS = ("status", "diff", "log", "show", "rev-parse", "ls-files", "check-ignore",
                        "add", "commit")
_CLAUDE_TOOL_RESULT_FIELDS = {"filePath", "type", "mode", "numLines", "totalLines", "startLine", "numFiles",
                              "numMatches", "totalMatches", "durationMs", "exitCode", "isImage", "interrupted",
                              "truncated", "userModified", "replaceAll"}
_FAILURE_STDOUT_LIMIT = 256 * 1024
_FAILURE_STDERR_LIMIT = 64 * 1024
_FAILURE_LINE_LIMIT = 64 * 1024
_DEFAULT_PROVIDER_OUTPUT_LIMIT = 16 * 1024 * 1024
_CODEX_SUBSCRIPTION_DIR = "codex-subscription"
_ART_PIPELINE_SCRIPT = Path(__file__).resolve().with_name("art_pipeline.py")
_CODEX_API_BILLING_CODES = frozenset({
    "credit_balance_exhausted", "insufficient_quota", "organization_spend_limit_exceeded",
    "organization_usage_limit_exceeded", "project_spend_limit_exceeded",
})
_CODEX_API_QUOTA_TEXT = "Quota exceeded. Check your plan and billing details."
_CODEX_HTTP_429 = re.compile(r"unexpected status 429(?: Too Many Requests)?: (?=\{)")
_CODEX_SUBSCRIPTION_LIMIT_TEXT = re.compile(
    r"You(?:'|’)ve hit your usage limit\. "
    r"(?:Visit https://chatgpt\.com/settings/usage to purchase more credits or try again|Try again) "
    r"(?:later|at [^\r\n]{1,120})\.")
_PERMANENT_FAILURE = re.compile(
    r"unauthori[sz]ed|forbidden|authentication\s+(?:failed|required)|invalid[ _-]*(?:api[ _-]*)?key|"
    r"permission[ _-]*denied|access[ _-]*denied|insufficient[ _-]*(?:quota|balance|credits?)|"
    r"(?:quota|balance|credits?|billing)[^\r\n]{0,80}(?:exhausted|exceeded|insufficient|depleted)|"
    r"context[ _-]*(?:length|window|limit)|maximum[^\r\n]{0,30}tokens?|"
    r"余额不足|配额不足|额度不足|上下文.{0,12}(?:超|不足)", re.IGNORECASE)
_NETWORK_FAILURE = re.compile(
    r"error sending request|connection (?:reset|refused|timed out|failed|closed)|"
    r"network (?:is )?unreachable|temporary failure in name resolution|"
    r"(?:dns|hostname) (?:lookup|resolution) (?:failed|failure)|failed to lookup address|"
    r"tls handshake (?:eof|failed|timeout)|transport error|"
    r"\b(?:ECONNRESET|ECONNREFUSED|ETIMEDOUT|ENETUNREACH|EHOSTUNREACH|EAI_AGAIN|ENOTFOUND)\b",
    re.IGNORECASE)


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, session_id: str | None = None,
                 interrupted: bool = False, transient: bool = False,
                 retry_reason: str | None = None, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.session_id, self.interrupted = session_id, interrupted
        self.transient = transient
        self.retry_reason, self.retry_after_seconds = retry_reason, retry_after_seconds


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        atomic_replace(Path(name), path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _strict_json(raw: str) -> Any:
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("non-finite JSON value")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def _json_file(path: Path, limit: int = 2 * 1024 * 1024) -> Any:
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError("result is a symlink or exceeds its size limit")
    return _strict_json(path.read_text(encoding="utf-8-sig"))


def _failure_tail(path: Path, limit: int) -> tuple[bytes, bool]:
    if path.is_symlink():
        raise ValueError("Failure log is a symlink")
    with path.open("rb") as source:
        size = os.fstat(source.fileno()).st_size
        skipped = size > limit
        source.seek(max(0, size - limit))
        raw = source.read(limit)
    if skipped:
        # A tail may start inside a model/tool payload. Never interpret that
        # partial line as a protocol record or search its prose for error words.
        raw = raw.partition(b"\n")[2]
    return raw, skipped


def _failure_events(path: Path, kind: str) -> tuple[list[dict], bool, str]:
    if path.is_symlink():
        raise ValueError("Failure log is a symlink")
    with path.open("rb") as source:
        first = source.readline(_FAILURE_LINE_LIMIT + 1)
    if len(first) > _FAILURE_LINE_LIMIT:
        raise ValueError("Oversized opening event")
    opening = _strict_json(first.decode("utf-8-sig"))
    field, expected = ("sessionId", "session") if kind == "code" else ("thread_id", "thread.started")
    if (type(opening) is not dict or opening.get("type") != expected or
            not isinstance(opening.get(field), str) or not _SESSION.fullmatch(opening[field])):
        raise ValueError("Invalid opening event")
    raw, skipped = _failure_tail(path, _FAILURE_STDOUT_LIMIT)
    events = []
    for line in raw.decode("utf-8-sig").splitlines():
        if not line.strip() or (kind == "code" and _ACCOUNT_METADATA.fullmatch(line)):
            continue
        if len(line.encode("utf-8")) > _FAILURE_LINE_LIMIT:
            raise ValueError("Oversized failure event")
        event = _strict_json(line)
        if type(event) is not dict or not isinstance(event.get("type"), str):
            raise ValueError("Invalid failure event")
        # Legacy DSH marks shortened tool output, independently of protocol
        # completeness. Its body is ignored by this classifier. Control and
        # unknown events must still be complete before scheduling a retry.
        if event.get("truncated") and not (kind == "code" and event["type"] == "tool_result"):
            raise ValueError("Truncated failure event")
        events.append(event)
    if not skipped and (not events or events[0] != opening):
        raise ValueError("Opening event changed")
    return events, skipped, opening[field]


def _codex_api_billing_error(error: dict) -> bool:
    status = error.get("status", error.get("status_code"))
    if status is not None and status not in {429, "429"}:
        return False
    code = error.get("code")
    if code is not None:
        return isinstance(code, str) and code in _CODEX_API_BILLING_CODES
    error_type = error.get("type")
    if error_type is not None:
        return error_type == "insufficient_quota"
    message = error.get("message")
    if not isinstance(message, str) or len(message) > 8192:
        return False
    if message == _CODEX_API_QUOTA_TEXT:
        # This is the native Codex rendering of a quota error. The caller has
        # already established that this terminal event came from the API path.
        return True
    prefix = _CODEX_HTTP_429.match(message)
    if prefix is None:
        return False
    body = message[prefix.end():]
    _, end = json.JSONDecoder().raw_decode(body)
    suffix = body[end:].strip()
    if suffix and not re.fullmatch(r", url: https://api\.openai\.com/v1/[^\s,]+(?:, [^\r\n]+)?", suffix):
        return False
    envelope = _strict_json(body[:end])
    inner = envelope.get("error") if type(envelope) is dict else None
    if type(inner) is not dict:
        return False
    # The API's specific code takes precedence over its broader error type.
    code = inner.get("code")
    return (isinstance(code, str) and code in _CODEX_API_BILLING_CODES if code is not None else
            inner.get("type") == "insufficient_quota")


def _codex_terminal_error(path: Path, capture_limit: int) -> dict | None:
    """Inspect one complete Codex turn, including logs with large tool items."""
    if path.is_symlink():
        return None
    opened = started = False
    terminal = None
    with path.open("rb") as source:
        remaining = os.fstat(source.fileno()).st_size
        if remaining > capture_limit:
            return None
        while remaining:
            line = source.readline(remaining)
            if not line:
                return None
            remaining -= len(line)
            if not line.strip():
                continue
            event = _strict_json(line.decode("utf-8-sig" if not opened else "utf-8"))
            if terminal is not None or type(event) is not dict or event.get("truncated"):
                return None
            event_type = event.get("type")
            if not isinstance(event_type, str):
                return None
            if not opened:
                thread_id = event.get("thread_id")
                if event_type != "thread.started" or not isinstance(thread_id, str) or not _SESSION.fullmatch(thread_id):
                    return None
                opened = True
            elif event_type in {"thread.started", "turn.completed"}:
                return None
            elif event_type == "turn.started":
                if started:
                    return None
                started = True
            elif event_type == "turn.failed":
                terminal = event.get("error")
                if not started or type(terminal) is not dict:
                    return None
        if source.read(1):
            return None
    return terminal


def _codex_api_quota_failed(kind: str, out: Path) -> bool:
    if kind not in {"plan", "review"}:
        return False
    try:
        record = _json_file(out / "process.json", 16384)
        if (type(record) is not dict or record.get("kind") != kind or
                record.get("codex_auth_mode") != "api_key" or record.get("codex_subscription_fallback") is not True or
                type(record.get("exit_code")) is not int or record["exit_code"] == 0 or
                record.get("timed_out") is not False or record.get("output_limit_exceeded") is not False or
                record.get("launch_or_capture_failed")):
            return False
        capture_limit = record.get("max_output_bytes", _DEFAULT_PROVIDER_OUTPUT_LIMIT)
        if type(capture_limit) is not int or capture_limit < 1024:
            return False
        terminal = _codex_terminal_error(out / "stdout.log", capture_limit)
        return terminal is not None and _codex_api_billing_error(terminal)
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        return False


def codex_result_directory(kind: str, out: Path) -> Path:
    """Use a nested subscription receipt only after a verified API rejection."""
    out = Path(out).resolve()
    if kind not in {"plan", "review"}:
        return out
    fallback = out / _CODEX_SUBSCRIPTION_DIR
    try:
        if fallback.is_symlink() or fallback.resolve() != fallback:
            return out
        record = _json_file(fallback / "process.json", 16384)
        if (type(record) is dict and record.get("kind") == kind and record.get("codex_auth_mode") == "chatgpt" and
                _codex_api_quota_failed(kind, out)):
            return fallback
    except (OSError, UnicodeError, ValueError, RuntimeError):
        pass
    return out


def _retry_after(error: dict) -> int | None:
    values = [error.get(key) for key in ("retry_after_seconds", "retryAfterSeconds", "retry_after")]
    headers = error.get("headers")
    if type(headers) is dict:
        values.extend(value for key, value in headers.items() if isinstance(key, str) and key.lower() == "retry-after")
    for value in values:
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,5}", value):
            value = int(value)
        if type(value) is int and 0 < value <= 86400:
            return value
    return None


def _codex_subscription_limit(error: dict) -> dict | None:
    """Recognize native limit notices, without interpreting prose reset dates."""
    message = error.get("message")
    if not isinstance(message, str) or not _CODEX_SUBSCRIPTION_LIMIT_TEXT.fullmatch(message):
        return None
    result = {"reason": "codex_subscription_limit"}
    delay = _retry_after(error)
    if delay is not None:
        result["retry_after_seconds"] = delay
    return result


def _transient_error(error: dict) -> dict | None:
    """Only provider-owned error metadata; never a model final/text/tool body."""
    message = error.get("message", "")
    code = error.get("code", "")
    if not isinstance(message, str) or len(message) > 8192 or not isinstance(code, str) or len(code) > 100:
        raise ValueError("Invalid provider error fields")
    code = code.upper()
    status = error.get("status", error.get("status_code"))
    if isinstance(status, str) and re.fullmatch(r"[0-9]{3}", status):
        status = int(status)
    if status is not None and type(status) is not int:
        raise ValueError("Invalid provider HTTP status")
    if (status in {401, 403} or _PERMANENT_FAILURE.search(message) or
            re.search(r"AUTH|FORBIDDEN|PERMISSION|QUOTA|BALANCE|CREDIT|CONTEXT|INVALID|UNKNOWN", code)):
        raise ValueError("Permanent provider error")
    if status == 429:
        result = {"reason": "rate_limited"}
    elif status in {500, 502, 503, 504}:
        result = {"reason": "server_unavailable"}
    elif status is None and code in {"TRANSPORT", "TIMEOUT", "ETIMEDOUT", "ECONNRESET", "ECONNREFUSED",
                                     "ENETUNREACH", "EHOSTUNREACH", "EAI_AGAIN", "ENOTFOUND"}:
        result = {"reason": "transport"}
    elif (status is None and code == "STREAM_CLOSED" and
          message == "DeepSeek Messages stream ended before message_stop"):
        # DSH's provider-owned terminal error for an interrupted Messages
        # stream. Keep the exact known pair; unrelated stream/protocol errors
        # must not acquire unlimited retries merely from this error code.
        result = {"reason": "transport"}
    elif status is None and _NETWORK_FAILURE.search(message):
        result = {"reason": "transport"}
    else:
        return None
    delay = _retry_after(error)
    if delay is not None:
        result["retry_after_seconds"] = delay
    return result


def _claude_session(event: dict) -> str | None:
    if event.get("type") == "system" and event.get("subtype") == "init":
        value = event.get("session_id")
        if isinstance(value, str) and _CLAUDE_SESSION.fullmatch(value):
            return value
    return None


def _claude_header(event: dict, pending_session: str | None,
                   requested_session: str | None) -> tuple[str, bool]:
    """Bind native UI preamble events to the required opening init session."""
    identity = event.get("session_id")
    if (event.get("type") != "system" or event.get("subtype") not in ("init", "ui_invalidate") or
            not isinstance(identity, str) or not _CLAUDE_SESSION.fullmatch(identity) or
            (pending_session is not None and identity != pending_session) or
            (requested_session is not None and identity != requested_session)):
        raise ValueError("Invalid Claude Code opening session")
    return identity, event["subtype"] == "init"


def _claude_receipt(kind: str, record: dict) -> bool:
    return ((kind == "plan" and record.get("planning_provider") == "claude") or
            (kind == "code" and record.get("coding_provider") == "claude") or
            (kind == "art_diagnose" and record.get("diagnosis_provider") == "claude"))


def _claude_receipt_session(record: dict) -> str | None:
    if record.get("kind") != "art_diagnose":
        return record.get("requested_session")
    source, branch = record.get("requested_session"), record.get("diagnosis_session")
    if (not isinstance(source, str) or not _CLAUDE_SESSION.fullmatch(source) or
            not isinstance(branch, str) or not _CLAUDE_SESSION.fullmatch(branch) or source.casefold() == branch.casefold()):
        raise ValueError("Invalid Claude diagnostic fork identity")
    return branch


def _claude_log_path(value):
    return value if isinstance(value, str) and len(value) <= 4096 and not re.search(r"[\x00-\x1f]", value) else None


def _claude_log_tool(block):
    if not isinstance(block, dict):
        return None
    kind = block.get("type")
    if kind == "tool_use":
        keys = ("id", "name")
    elif kind == "tool_result":
        keys = ("tool_use_id",)
    else:
        return None
    tool = {"type": kind}
    for key in keys:
        value = block.get(key)
        if isinstance(value, str) and _SESSION.fullmatch(value):
            tool[key] = value
    if kind == "tool_result" and type(block.get("is_error")) is bool:
        tool["is_error"] = block["is_error"]
    return tool


def _claude_log_tool_metadata(value):
    if not isinstance(value, dict):
        return {}
    metadata = {}
    for key in _CLAUDE_TOOL_RESULT_FIELDS & value.keys():
        field = value[key]
        if key == "filePath" and _claude_log_path(field) is not None:
            metadata[key] = field
        elif key in ("type", "mode") and isinstance(field, str) and _SESSION.fullmatch(field):
            metadata[key] = field
        elif type(field) in (bool, int):
            metadata[key] = field
    return metadata


def _claude_log_record(line: bytes) -> bytes:
    """Keep the protocol and tool activity, without saving file or command content."""
    if not line.strip():
        return line
    try:
        event = _strict_json(line.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError):
        # Preserve a validation failure without writing a possibly partial tool payload.
        return b"{}\n"
    if type(event) is not dict or not isinstance(event.get("type"), str):
        return b"{}\n"
    kind = event["type"]
    if kind in ("result", "rate_limit_event") or (kind == "system" and event.get("subtype") == "init"):
        return line
    if not _SESSION.fullmatch(kind):
        return b"{}\n"

    record = {"type": kind}
    for key in ("subtype", "session_id", "uuid", "parent_tool_use_id", "error"):
        if key in event:
            value = event[key]
            record[key] = value if isinstance(value, str) and _SESSION.fullmatch(value) else None
            if key == "parent_tool_use_id" and value is not None and record[key] is None:
                record[key] = ""
    timestamp = event.get("timestamp")
    if isinstance(timestamp, str) and re.fullmatch(r"[0-9T:.+Z-]{1,40}", timestamp):
        record["timestamp"] = timestamp
    message = event.get("message")
    blocks = message.get("content") if isinstance(message, dict) else None
    if isinstance(blocks, list):
        record["message"] = {"content": [tool for block in blocks if (tool := _claude_log_tool(block))]}
    payload = event.get("tool_use_result")
    if isinstance(payload, dict):
        metadata = _claude_log_tool_metadata(payload)
        file_metadata = _claude_log_tool_metadata(payload.get("file"))
        if file_metadata:
            metadata["file"] = file_metadata
        if metadata:
            record["tool_use_result"] = metadata
    return (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")


def _claude_notification_suffix(source, session: str) -> None:
    """Validate complete same-session notifications after the foreground result."""
    phase = "between"
    run_ids = set()
    for line in source:
        if not line.strip():
            continue
        event = _strict_json(line)
        if type(event) is not dict or event.get("session_id") != session:
            raise ValueError("Invalid Claude Code notification session")
        kind, subtype = event.get("type"), event.get("subtype")
        if phase != "running":
            if kind == "system" and subtype in ("task_updated", "background_tasks_changed"):
                if phase == "between":
                    phase = "lifecycle"
                continue
            if kind == "system" and subtype == "task_notification" and phase != "announced":
                phase = "announced"
                continue
            if phase == "announced" and _claude_session(event) == session:
                phase = "running"
                continue
        elif kind == "result":
            origin = event.get("origin")
            if type(origin) is dict and origin.get("kind") == "task-notification" and origin.get("producer") == "session-task":
                run_id = origin.get("runId")
                if (isinstance(run_id, str) and _SESSION.fullmatch(run_id) and run_id not in run_ids and
                        type(event.get("is_error")) is bool):
                    run_ids.add(run_id)
                    phase = "between"
                    continue
        elif kind in ("assistant", "user", "rate_limit_event") or (
                kind == "system" and subtype not in ("init", "task_notification")):
            continue
        raise ValueError("Invalid Claude Code notification suffix")
    if phase != "between":
        raise ValueError("Incomplete Claude Code notification suffix")


def _claude_turn(path: Path, requested_session: str | None) -> tuple[str, dict, list[str], dict | None]:
    """Read the foreground Claude Code turn and validate any notification suffix."""
    if path.is_symlink():
        raise ValueError("Linked Claude Code output")
    session, result = None, None
    pending_session = None
    errors = []
    rate_limit = None
    with path.open(encoding="utf-8-sig") as source:
        for line in source:
            if not line.strip():
                continue
            event = _strict_json(line)
            if type(event) is not dict or not isinstance(event.get("type"), str):
                raise ValueError("Invalid Claude Code event stream")
            if session is None:
                pending_session, initialized = _claude_header(event, pending_session, requested_session)
                if initialized:
                    session = pending_session
                continue
            if event.get("session_id", session) != session or _claude_session(event):
                raise ValueError("Conflicting Claude Code session")
            if event["type"] == "result":
                if (event.get("session_id") != session or type(event.get("is_error")) is not bool or
                        event.get("origin") is not None):
                    raise ValueError("Invalid Claude Code result identity")
                result = event
                _claude_notification_suffix(source, session)
                break
            elif event["type"] == "assistant" and event.get("parent_tool_use_id") is None and "error" in event:
                if not isinstance(event["error"], str):
                    raise ValueError("Invalid Claude Code API error code")
                errors.append(event["error"])
            elif event["type"] == "rate_limit_event":
                info = event.get("rate_limit_info")
                if event.get("session_id") != session or type(info) is not dict or info.get("status") not in {
                        "allowed", "allowed_warning", "rejected"}:
                    raise ValueError("Invalid Claude Code rate limit event")
                rate_limit = info
    if session is None or result is None:
        raise ValueError("Incomplete Claude Code turn")
    return session, result, errors, rate_limit


def _claude_model_evidence(path: Path, requested_session: str | None,
                           requested_model: str | None, effort: str | None) -> dict:
    """Model identity comes from native protocol metadata, never assistant prose."""
    _, terminal, _, _ = _claude_turn(path, requested_session)
    observed = None
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            if not line.strip():
                continue
            event = _strict_json(line)
            if event.get("type") == "system" and event.get("subtype") == "init":
                value = event.get("model")
                observed = value if isinstance(value, str) and _SESSION.fullmatch(value) else None
                break
    usage = terminal.get("modelUsage")
    models = sorted(key for key in usage if isinstance(key, str) and _SESSION.fullmatch(key)) if isinstance(usage, dict) else []
    return {"requested_model": requested_model, "requested_reasoning_effort": effort,
            "reasoning_effort_source": "cli_argument", "observed_model": observed,
            "model_usage_models": models,
            "model_verified": bool(requested_model and observed == requested_model and requested_model in models)}


def _claude_retry(out: Path, record: dict) -> dict | None:
    # The SDK error code is provider-owned metadata, unlike text returned by a
    # model, source file or tool. A successful terminal result ends retries.
    limit = record.get("max_output_bytes", _DEFAULT_PROVIDER_OUTPUT_LIMIT)
    if type(limit) is not int or not 1024 <= limit <= 256 * 1024 * 1024 or (out / "stdout.log").stat().st_size > limit:
        return None
    if record.get("native_protocol") == "full_flow_native_v1":
        try:
            _, _, native_errors, native_rate, selection = _claude_repair_native(out, record)
        except ValueError:
            # A single failed turn has no completed answer for the multi-turn
            # parser. The existing strict failure parser can still classify it.
            pass
        else:
            if not selection["model_verified"]:
                return None
            stderr = _failure_tail(out / "stderr.log", _FAILURE_STDERR_LIMIT)[0].decode("utf-8-sig")
            if _PERMANENT_FAILURE.search(stderr):
                return None
            codes = [code for code in native_errors if code != "native_terminal_error"]
            if native_rate and native_rate["status"] == "rejected":
                allowed = set(_CLAUDE_RETRY_CODES)
                if native_rate.get("errorCode") == "credits_required":
                    allowed.add("billing_error")
                if any(code not in allowed for code in codes):
                    return None
                retry = {"reason": "claude_subscription_limit"}
                reset = native_rate.get("resetsAt")
                if type(reset) in {int, float} and 0 < reset < 10_000_000_000:
                    retry["retry_after_seconds"] = max(1, math.ceil(reset - time.time()))
                return retry
            if codes and all(code in _CLAUDE_RETRY_CODES for code in codes):
                return {"reason": _CLAUDE_RETRY_CODES[codes[-1]]}
            return None
    _, result, errors, rate_limit = _claude_turn(out / "stdout.log", _claude_receipt_session(record))
    if (not result["is_error"] or result.get("startup_failure_reason") or
            result.get("subtype") not in {"success", "error_during_execution"}):
        return None
    stderr = _failure_tail(out / "stderr.log", _FAILURE_STDERR_LIMIT)[0].decode("utf-8-sig")
    terminal_errors = result.get("errors", [])
    if not isinstance(terminal_errors, list) or any(not isinstance(item, str) for item in terminal_errors):
        return None
    if any(_PERMANENT_FAILURE.search(message) for message in [stderr, *terminal_errors]):
        return None
    if rate_limit and rate_limit["status"] == "rejected":
        allowed = set(_CLAUDE_RETRY_CODES)
        if rate_limit.get("errorCode") == "credits_required":
            allowed.add("billing_error")
        if any(code not in allowed for code in errors):
            return None
        retry = {"reason": "claude_subscription_limit"}
        reset = rate_limit.get("resetsAt")
        if type(reset) in {int, float} and 0 < reset < 10_000_000_000:
            retry["retry_after_seconds"] = max(1, math.ceil(reset - time.time()))
        return retry
    # Older CLI builds may provide only the typed API error plus the exact
    # native usage-limit notice. Let the scheduler use its default reset wait.
    terminal_text = result.get("result")
    if (errors and all(code == "rate_limit" for code in errors) and
            isinstance(terminal_text, str) and _CLAUDE_LIMIT_TEXT.fullmatch(terminal_text.strip())):
        return {"reason": "claude_subscription_limit"}
    if errors and all(code in _CLAUDE_RETRY_CODES for code in errors):
        return {"reason": _CLAUDE_RETRY_CODES[errors[-1]]}
    if (not errors or all(code == "unknown" for code in errors)) and any(
            message.strip() in {"Connection error.", "Request timed out."} for message in terminal_errors):
        return {"reason": "transport"}
    return None


def classify_transient_failure(kind: str, out: Path) -> dict | None:
    """Read-only, bounded classification of a *failed* provider invocation.

    A retry remains a failure, never proof of completion. Only terminal protocol
    error metadata and CLI stderr are examined; thinking, tool output and final
    prose do not supply evidence. Persisted retry flags alone are not trusted.
    """
    if kind not in {"code", "plan", "review", "art_diagnose"}:
        return None
    out = codex_result_directory(kind, out) if kind in {"plan", "review"} else Path(out)
    try:
        path = out / "process.json"
        if path.is_symlink():
            return None
        with path.open("rb") as source:
            raw = source.read(16385)
        if len(raw) > 16384:
            return None
        record = _strict_json(raw.decode("utf-8-sig"))
        if (type(record) is not dict or record.get("kind") != kind or
                type(record.get("exit_code")) is not int or
                record.get("timed_out") is not False or record.get("output_limit_exceeded") is not False or
                record.get("launch_or_capture_failed")):
            return None
        if _claude_receipt(kind, record):
            return _claude_retry(out, record)
        if record["exit_code"] == 0:
            return None
        # Limit this behavior to the already-authorized API -> subscription
        # route. An unrelated child directory cannot supply retry evidence.
        subscription_fallback = (kind in {"plan", "review"} and record.get("codex_auth_mode") == "chatgpt" and
                                 out.name == _CODEX_SUBSCRIPTION_DIR and _codex_api_quota_failed(kind, out.parent))
        events, skipped, session = _failure_events(out / "stdout.log", kind)
        stderr = _failure_tail(out / "stderr.log", _FAILURE_STDERR_LIMIT)[0].decode("utf-8-sig")
        if _PERMANENT_FAILURE.search(stderr):
            return None
        errors, observed_errors, terminal_error, final_seen = [], [], False, False
        active_turn = ... if skipped else None
        turn_started = skipped
        last_dsh_reason = None
        routing = False
        for index, event in enumerate(events):
            event_type = event["type"]
            if final_seen:
                return None
            if event_type in {"session", "thread.started"}:
                if skipped or index != 0 or event_type != ("session" if kind == "code" else "thread.started"):
                    return None
            elif event_type == "error":
                error = event.get("error", event)
                if type(error) is not dict:
                    return None
                errors.append(error)
                observed_errors.append(error)
                terminal_error = kind == "code"
            elif kind == "code" and event_type == "status":
                phase = event.get("phase")
                if phase in {"turn_start", "turn_end"}:
                    turn = event.get("turn")
                    if not ((type(turn) is int and turn >= 0) or (isinstance(turn, str) and turn)):
                        return None
                    if phase == "turn_start":
                        if active_turn is not None and active_turn is not ...:
                            return None
                        active_turn, errors, terminal_error = turn, [], False
                    else:
                        if active_turn is None or (active_turn is not ... and active_turn != turn):
                            return None
                        reason = event.get("reason")
                        if type(reason) is not dict or reason.get("kind") not in {"completed", "error"}:
                            return None
                        terminal_error = reason["kind"] == "error"
                        last_dsh_reason = reason["kind"]
                        errors = [reason["error"]] if terminal_error and type(reason.get("error")) is dict else []
                        observed_errors.extend(errors)
                        active_turn = None
            elif kind == "code" and event_type == "final":
                if active_turn is not None or ("sessionId" in event and event["sessionId"] != session):
                    return None
                reason = event.get("reason")
                if reason is not None:
                    if type(reason) is not dict or reason.get("kind") not in {"completed", "error"}:
                        return None
                    if last_dsh_reason is not None and reason["kind"] != last_dsh_reason:
                        return None
                    terminal_error = reason["kind"] == "error"
                    errors = [reason["error"]] if terminal_error and type(reason.get("error")) is dict else []
                    observed_errors.extend(errors)
                final_seen = True
            elif kind != "code" and event_type == "turn.started":
                if turn_started:
                    return None
                turn_started = True
            elif kind != "code" and event_type == "turn.failed":
                if not turn_started or type(event.get("error")) is not dict:
                    return None
                errors.append(event["error"])
                observed_errors.append(event["error"])
                terminal_error, final_seen = True, True
            elif kind != "code" and event_type == "turn.completed":
                return None
        # A native top-level limit error may be the last event before a nonzero
        # CLI exit; it is still distinct from quoted tool/model item content.
        quota_candidates = [_codex_subscription_limit(error) for error in observed_errors] if subscription_fallback else []
        quota_only = bool(quota_candidates) and all(quota_candidates)
        if (quota_only and not skipped and turn_started and not final_seen and events[-1]["type"] == "error"):
            terminal_error = True
        if not terminal_error or not errors:
            return None
        # A later retryable code must not erase explicit authentication, billing,
        # context or protocol failure evidence from this bounded event interval.
        for error in observed_errors:
            if record.get("codex_auth_mode") == "api_key" and _codex_api_billing_error(error):
                return None
            _transient_error(error)
        if quota_only:
            # Only typed retry-after metadata is reliable. Native text dates
            # have no timezone; the scheduler supplies a bounded default wait.
            return next((candidate for candidate in reversed(quota_candidates) if "retry_after_seconds" in candidate),
                        quota_candidates[-1])
        candidates = []
        for error in errors:
            if type(error) is not dict:
                return None
            candidate = _transient_error(error)
            if candidate:
                candidates.append(candidate)
            message = error.get("message", "")
            routing |= "workspace routing discovery failed" in message.lower()
        if kind != "code" and routing:
            return {"reason": "workspace_routing_network"} if _NETWORK_FAILURE.search(stderr) else None
        return candidates[-1] if candidates else None
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        return None


def _normalize_legacy_plan_core_tests(value: Any) -> Any:
    if type(value) is not dict:
        return value
    normalized = dict(value)
    names = value.get("core_tests")
    if type(names) is not list:
        return normalized
    exact_names = []
    for name in names:
        match = _LEGACY_CORE_TEST_ANNOTATION.fullmatch(name) if isinstance(name, str) else None
        exact_names.append(match.group(1) if match else name)
    normalized["core_tests"] = exact_names
    return normalized


def validate_output(kind: str, value: Any) -> dict:
    schema = REVIEW_SCHEMA if kind == "review" else ART_DIAGNOSE_SCHEMA if kind == "art_diagnose" else None
    if kind == "plan":
        schema = PLAN_WITH_CORE_TESTS_SCHEMA if type(value) is dict and "core_tests" in value else PLAN_SCHEMA
        if type(value) is dict and "planning_level" in value:
            schema = TIERED_PLAN_WITH_CORE_TESTS_SCHEMA if "core_tests" in value else TIERED_PLAN_SCHEMA
    if schema is None or type(value) is not dict or set(value) != set(schema["required"]):
        raise ProviderError("Agent output does not match the required object fields")
    for field, rule in schema["properties"].items():
        item = value[field]
        if rule["type"] == "string":
            if not isinstance(item, str) or ("enum" in rule and item not in rule["enum"]):
                raise ProviderError("Agent output has an invalid " + field)
        elif rule["type"] == "integer":
            if type(item) is not int or item < rule.get("minimum", 0):
                raise ProviderError("Agent output has an invalid " + field)
        elif (type(item) is not list or len(item) > 100 or
              any(not isinstance(entry, str) or not entry.strip() for entry in item)):
            raise ProviderError("Agent output has invalid " + field)
        elif "enum" in rule.get("items", {}) and any(entry not in rule["items"]["enum"] for entry in item):
            raise ProviderError("Agent output has invalid " + field)
    if kind == "plan" and value["action"] == "task":
        if not value["title"].strip() or not value["body"].strip() or not value["acceptance_criteria"]:
            raise ProviderError("A task requires a title, task body, and acceptance criteria")
    if kind == "plan" and "planning_reason" in value and not value["planning_reason"].strip():
        raise ProviderError("A tiered plan requires a planning_reason")
    if kind == "plan" and "core_tests" in value:
        if value["action"] == "task":
            try:
                ue_checks.validate_core_tests(value["core_tests"])
            except ue_checks.ValidationError as error:
                raise ProviderError("Agent output has invalid core_tests") from error
        elif value["core_tests"]:
            raise ProviderError("A complete or blocked plan must have empty core_tests")
    if kind in {"review", "art_diagnose"} and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value["head_sha"]):
        raise ProviderError("Review must name a complete Git commit identity")
    if kind == "art_diagnose":
        if any(not value[field].strip() for field in ("diagnosis", "plan", "reason")):
            raise ProviderError("Art diagnosis requires nonempty diagnosis, plan, and reason")
        if value["action"] == "continue" and (value["required_approvals"] or value["questions"]):
            raise ProviderError("Continue cannot require approvals or questions")
        if value["action"] == "needs_user" and not (value["required_approvals"] or value["questions"]):
            raise ProviderError("Needs-user diagnosis must specify an approval or question")
    return value


def parse_codex_events(path: Path, *, exit_code: int) -> dict:
    """Require a complete single-turn event interval, not a prose completion."""
    session = None
    started = False
    completed = False
    count = 0
    reconnects = 0
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            event = _strict_json(line)
            count += 1
            if type(event) is not dict or not isinstance(event.get("type"), str) or completed:
                raise ValueError("invalid event or data after turn completion")
            kind = event["type"]
            if count == 1 and kind != "thread.started":
                raise ValueError("missing opening thread")
            if kind == "thread.started":
                identity = event.get("thread_id")
                if session is not None or not isinstance(identity, str) or not identity.strip():
                    raise ValueError("invalid thread identity")
                session = identity
            elif kind == "turn.started":
                if started:
                    raise ValueError("overlapping turns")
                started = True
            elif kind == "error":
                # This CLI reports automatic transport reconnect progress as
                # `error` events even when the same turn subsequently succeeds.
                # Only known progress notices are nonterminal; all other errors and
                # missing/failing turn completion still fail the protocol.
                message = event.get("message")
                notice = (re.fullmatch(r"Reconnecting\.\.\. ([1-9][0-9]?)/([1-9][0-9]?) "
                                      r"\((?:stream disconnected before completion: [^\r\n]{1,1000}|request timed out)\)", message)
                          if isinstance(message, str) else None)
                numbered = bool(notice and 1 <= int(notice.group(1)) <= int(notice.group(2)) <= 10)
                waiting = message == "Reconnecting... waiting for network (Connection failed: error sending request)"
                if not started or not (numbered or waiting):
                    raise ValueError("Codex reported a terminal or unknown error")
                reconnects += 1
            elif kind == "turn.failed":
                raise ValueError("Codex reported a failed turn")
            elif kind == "turn.completed":
                if not started:
                    raise ValueError("completion without a turn")
                completed = True
        if type(exit_code) is not int or exit_code != 0 or not session or not completed:
            raise ValueError("process failed or incomplete event stream")
    except (OSError, UnicodeError, ValueError) as error:
        reason = (str(error) if type(error) is ValueError else "unreadable or malformed event log")
        raise ProviderError("Codex did not produce a valid completed turn: " + reason) from error
    return {"session_id": session, "event_count": count, "reconnect_notices": reconnects}


def read_session(out: Path, *, requested_session: str | None = None) -> str | None:
    """Read only the opening session identity, including after a killed run."""
    pending_session = None
    try:
        with (Path(out) / "stdout.log").open(encoding="utf-8-sig") as source:
            for _ in range(5):
                line = source.readline(65536)
                if not line:
                    break
                if not line.strip():
                    continue
                event = _strict_json(line)
                if event.get("type") == "system":
                    receipt = Path(out) / "process.json"
                    if requested_session is None and receipt.is_file():
                        record = _json_file(receipt, limit=16384)
                        if not _claude_receipt(record.get("kind"), record):
                            return None
                        requested_session = _claude_receipt_session(record)
                    pending_session, initialized = _claude_header(event, pending_session, requested_session)
                    if initialized:
                        return pending_session
                    continue
                if pending_session is not None:
                    return None
                identity = (event.get("sessionId") if event.get("type") == "session" else
                            event.get("thread_id") if event.get("type") == "thread.started" else None)
                return identity if isinstance(identity, str) and _SESSION.fullmatch(identity) else None
    except (OSError, UnicodeError, ValueError, AttributeError):
        pass
    return None


def _normalize_dsh(source: Path, target: Path) -> None:
    rows = []
    for line in source.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        if _ACCOUNT_METADATA.fullmatch(line):
            if not rows or _strict_json(rows[-1]).get("type") == "final":
                raise ProviderError("DSH metadata occurred outside the event interval")
            continue
        event = _strict_json(line)
        if type(event) is not dict or not isinstance(event.get("type"), str):
            raise ProviderError("Invalid DSH JSON event")
        rows.append(line)
    target.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _command(value: Any, default: list[str]) -> list[str]:
    result = value if value is not None else default
    if (type(result) is not list or not result or
            any(not isinstance(part, str) or not part or "\0" in part for part in result)):
        raise ProviderError("Provider command must be a nonempty argv array")
    if Path(result[0]).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise ProviderError("Use a native executable or native runtime plus a script, not a shell shim")
    return list(result)


def _claude_command(settings: dict, *, read_only_dirs: list[Path] | None = None) -> list[str]:
    policy = {"forceLoginMethod": "claudeai", "disableAllHooks": True,
              "permissions": {"blockReadsOutsideWorkingDirectories": True}}
    if read_only_dirs:
        deny = []
        for path in read_only_dirs:
            pattern = path.as_posix()
            if path.drive:
                pattern = "/" + path.drive[0].lower() + pattern[2:]
            deny.extend([f"Edit(/{pattern})", f"Edit(/{pattern}/**)"])
        policy["permissions"].update(additionalDirectories=[str(path) for path in read_only_dirs], deny=deny)
    return [*_command(settings.get("command"), ["claude"]),
            "--setting-sources", "", "--settings", json.dumps(policy, separators=(",", ":"))]


def _claude_fixed_bash(argv: Any, paths: dict[str, Path]) -> tuple[str, list[Path]]:
    if not isinstance(argv, list) or not argv:
        raise ProviderError("Claude preflight requires a nonempty argv array")
    expanded = []
    for arg in argv:
        if not isinstance(arg, str) or not arg:
            raise ProviderError("Claude preflight arguments must be nonempty strings")
        for name, path in paths.items():
            arg = arg.replace("{" + name + "}", path.as_posix())
        arg = arg.replace("\\", "/")
        if any(char in arg for char in "*\r\n\0"):
            raise ProviderError("Claude preflight cannot contain wildcards or control characters")
        expanded.append(arg)
    executable = Path(expanded[0])
    if not executable.is_absolute() or executable.suffix.lower() != ".exe":
        raise ProviderError("Claude preflight requires an absolute native executable path")
    binaries = [executable, *(Path(arg) for arg in expanded[1:] if arg.lower().endswith(".dll"))]
    directories = [binary.parent for binary in binaries if binary.is_absolute()]
    return shlex.join(expanded), directories


def _claude_planned_test_commands(config: dict, cwd: Path, logs: Path, names: Any) -> list[list[str]]:
    bridge = next((check for check in config.get("checks", []) if check["id"] == "ue-task-validation"), None)
    argv = bridge["argv"] if bridge else []
    if argv.count("--engine") != 1 or argv.index("--engine") + 1 == len(argv):
        raise ProviderError("Planned Claude tests require the trusted ue-task-validation --engine")
    engine = Path(argv[argv.index("--engine") + 1])
    if not engine.is_absolute():
        raise ProviderError("Planned Claude tests require an absolute UE engine path")
    try:
        names = ue_checks.validate_core_tests(names)
        binaries = ue_checks._binaries(engine)
        project = ue_checks._project(cwd)
        return [ue_checks._command(binaries, project, {"name": name, "mode": "nullrhi"}, logs / f"core-{index}.log")
                for index, name in enumerate(names, 1)]
    except ue_checks.ValidationError as error:
        raise ProviderError("Invalid planned Claude tests: " + str(error)) from error


def _claude_preflight(config: dict, cwd: Path, out: Path) -> tuple[list[str], str, list[Path]]:
    settings = config.get("claude", {})
    enabled = settings.get("ue_preflight", False)
    planned = settings.get("planned_core_tests", False)
    tests = settings.get("test_preflight")
    visual = settings.get("visual_preflight")
    if type(enabled) is not bool:
        raise ProviderError("claude.ue_preflight must be a boolean")
    if type(planned) is not bool or (planned and not enabled):
        raise ProviderError("claude.planned_core_tests must be a boolean and requires claude.ue_preflight: true")
    for option, scoped, field in (("test_preflight", tests, "commands"), ("visual_preflight", visual, "argv")):
        if scoped is None:
            continue
        if not enabled:
            raise ProviderError(f"claude.{option} requires claude.ue_preflight: true")
        if (not isinstance(scoped, dict) or set(scoped) != {"task_id", field} or
                not isinstance(scoped["task_id"], str) or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", scoped["task_id"])):
            raise ProviderError(f"claude.{option} must specify task_id and {field}")
    if tests is not None and (not isinstance(tests["commands"], list) or not tests["commands"]):
        raise ProviderError("claude.test_preflight requires a nonempty commands array")
    if not enabled:
        return [], "", []
    check = next((check for check in config.get("checks", []) if check["id"] == "ue-editor-build"), None)
    if check is None:
        raise ProviderError("claude.ue_preflight requires the trusted ue-editor-build check")
    cwd = cwd.resolve()
    paths = {"workspace": cwd}
    if config.get("runtime_dir"):
        paths["runtime"] = Path(config["runtime_dir"]).resolve()
    build, directories = _claude_fixed_bash(check.get("argv"), paths)
    commands = [build]
    instructions = (
        "\n本地提交前，用 Bash 原样运行以下 UE 编译命令（timeout=600000）。每次修正后重新编译，"
        "只有退出码为 0 且输出包含 Result: Succeeded 才继续。根据编译诊断修复代码并重试。"
        "遇到无法排除的失败、权限拒绝或超时，交付实际诊断，不要声称通过或提交未编译的代码。\n" + build
    )
    if not planned and tests is None and visual is None:
        return commands, instructions, directories
    runtime = paths.get("runtime")
    if runtime is None or cwd != runtime / "workspace" or out.parent != runtime / "runs":
        raise ProviderError("Claude task preflight requires the managed runtime workspace and operation directory")
    try:
        state = _json_file(runtime / "state.json")
        operation = state["operation"]
        task = state["task"]
        task_id = task["id"]
        if (state["phase"] != "CODE" or operation["kind"] != "code" or operation["task_id"] != task_id or
                Path(operation["output_dir"]).resolve() != out):
            raise ValueError("operation mismatch")
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise ProviderError("Claude task preflight requires the active persisted coding operation") from error
    preview_logs = cwd / "Saved" / "Logs" / out.name
    paths["output_dir"] = preview_logs
    test_argvs = []
    if planned and "core_tests" in task:
        test_argvs = _claude_planned_test_commands(config, cwd, preview_logs, task["core_tests"])
        instructions += (
            "\n提交后，最终验证请求必须包含任务书中这些同名核心测试，模式均为 nullrhi。"
            "仅当本任务验收明确要求更多 M2/M3 测试时，在绑定最终提交 HEAD 的 validation-request.json 中保留 tests 的核心测试，"
            "额外精确测试名可放入 test_batches（字符串数组的数组），"
            f"每批最多{ue_checks.MAX_TESTS}个、最多{ue_checks.MAX_TEST_BATCHES}批，固定 NullRHI，"
            "与 tests 及其他批次中的名称均不得重复，由外层验证器按本任务明确验收范围执行。"
        )
    if tests is not None and task_id == tests["task_id"]:
        planned_selectors = {arg for argv in test_argvs for arg in argv if arg.startswith("-ExecCmds=")}
        test_argvs.extend(argv for argv in tests["commands"] if not isinstance(argv, list) or
                          not any(isinstance(arg, str) and arg in planned_selectors for arg in argv))
    if test_argvs:
        test_commands = []
        for test_argv in test_argvs:
            command, test_directories = _claude_fixed_bash(test_argv, paths)
            test_commands.append(command)
            directories.extend(test_directories)
        preview_logs.mkdir(parents=True, exist_ok=True)
        commands.extend(test_commands)
        instructions += (
            "\n本任务编译成功后、提交前，用 Bash 逐条原样运行以下 UE 自动化测试命令（timeout=600000）。"
            "读取本轮 UE 日志，逐项核对指定测试名称、发现数和实际执行数，以及对应 Test Completed 的 Result 为 Success。"
            "退出码为 0 不能替代测试通过。修复失败并重新编译、重跑测试，再继续交付。\n" +
            "\n".join(test_commands) + "\n日志目录：" + preview_logs.as_posix()
        )
    if visual is not None and task_id == visual["task_id"]:
        command, preview_directories = _claude_fixed_bash(visual["argv"], paths)
        preview_logs.mkdir(parents=True, exist_ok=True)
        commands.append(command)
        directories.extend(preview_directories)
        instructions += (
            "\n本任务编译成功后、提交前，用 Bash 原样运行以下真实渲染测试命令（timeout=600000）。"
            "读取本轮 UE 日志，确认对应 Test Completed 的 Result 为 Success，并检查本轮新生成的 PNG，"
            "核对任务书和返工反馈要求的场景，"
            "修复发现的问题并重新运行编译和渲染测试。"
            "跳过渲染、旧截图或测试失败均不能作为通过证据。\n" + command +
            "\n日志目录：" + preview_logs.as_posix() +
            "\n截图目录：" + (cwd / "Saved" / "Evidence" / task_id).as_posix()
        )
    return commands, instructions, directories


def _claude_repair_context(config: dict, cwd: Path, out: Path, session: str | None) -> tuple[dict, dict, dict] | None:
    """The internal clone is an assertion, never a substitute for persisted authority."""
    profile = config.get("_user_repair_profile")
    if profile is None:
        return None
    try:
        from .user_repair import load_repair_request, validate_repair_profile
        runtime = Path(config["runtime_dir"]).absolute()
        cwd, out = Path(cwd).absolute(), Path(out).absolute()
        if (cwd != runtime / "workspace" or out.parent != runtime / "runs" or
                any(_reparse(path) for path in (runtime, *runtime.parents))):
            raise ValueError("unmanaged repair workspace")
        for relative in ("workspace", "runs", "state.json", Path("runs") / out.name):
            ue_checks._safe_under(runtime, relative)
        state = _json_file(runtime / "state.json")
        task, op = state["task"], state["operation"]
        if (state["phase"] != "CODE" or task.get("kind") == "art" or op["kind"] != "code" or
                op["task_id"] != task["id"] or Path(op["output_dir"]).absolute() != out or
                not isinstance(profile, dict) or task.get("user_repair") != profile or
                validate_repair_profile(task, runtime) != profile or profile.get("status") != "active" or
                op.get("user_repair_id") != profile["id"] or
                op.get("user_repair_request_sha256") != profile["request_sha256"]):
            raise ValueError("repair task, authorization or operation mismatch")
        request = load_repair_request(Path(profile["request_path"]), runtime)
        if (session != task.get("session_id") or session != request["target_session_id"] or
                not isinstance(session, str) or not _CLAUDE_SESSION.fullmatch(session) or
                task.get("session_owner_task_id", task["id"]) != task["id"] or
                op.get("requested_session_id") != session or
                op.get("coding_provider") != "claude" or op.get("coding_model") != "claude-opus-5-5" or
                op.get("coding_reasoning_effort") != "high" or
                config.get("coding_provider") != "claude" or
                config.get("claude", {}).get("model") != "claude-opus-5-5" or
                config.get("claude", {}).get("reasoning_effort") != "high"):
            raise ValueError("repair session or pinned model differs from its operation")
        binding = {"task_id": task["id"], "workspace": str(cwd), "output_dir": str(out),
                   "head_sha": op["head_sha"], "native_protocol": "full_flow_native_v1",
                   "user_repair_profile": dict(profile)}
        return request, task, binding
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise ProviderError("Claude repair requires its current managed authorization: " + str(error)) from error


def _claude_repair_preflight(config: dict, cwd: Path, out: Path, context: tuple) -> tuple[list[str], str, list[Path]]:
    """Fixed local diagnostics plus the controller-owned final-HEAD validation bridge."""
    from .full_flow_validation import _task_output_directory
    request, task, _ = context
    runtime = Path(config["runtime_dir"]).absolute()
    source_run = Path(request["source_full_flow_run"]).absolute()
    try:
        full_request = _json_file(source_run / "request.json")
        manifest = _json_file(source_run / "source-manifest.json", limit=32 * 1024 * 1024)
        bridge = next(item for item in config["checks"] if item["id"] == "ue-task-validation")
        argv = bridge["argv"]
        if argv.count("--engine") != 1:
            raise ValueError("missing trusted engine argument")
        engine = Path(argv[argv.index("--engine") + 1]).absolute()
        if (not Path(argv[argv.index("--engine") + 1]).is_absolute() or
                ue_checks._binaries(engine) != ue_checks._binaries(Path(full_request["engine_dir"])) or
                full_request["task_id"] != task["id"] or full_request["head_sha"] != request["source_head"]):
            raise ValueError("original full-flow evidence differs from the authorized engine/task/head")
        visuals = manifest["automation_inventory"]["visual"]
        if (not isinstance(visuals, dict) or not 1 <= len(visuals) <= 128 or
                any(not isinstance(name, str) or not re.fullmatch(r"ZhuGuoCity\.Local\.[A-Za-z0-9_.]+Visual", name)
                    for name in visuals)):
            raise ValueError("invalid original exact visual inventory")
        logs = cwd / "Saved" / "Logs" / out.name / "full-repair"
        project = ue_checks._project(cwd)
        binaries = ue_checks._binaries(engine)
        commands = [ue_checks._command(binaries, project, {"name": "ZhuGuoCity.M3.DeathRules", "mode": "nullrhi"},
                    logs / "regression.log", milestone_regression=True)]
        for index, name in enumerate(sorted(visuals), 1):
            commands.append(ue_checks._command(binaries, project, {"name": name, "mode": "normal"},
                            logs / f"visual-{index:02}.log") + ["-d3d12", "-windowed", "-ResX=1280", "-ResY=720"])
        commands.append(ue_checks._command(binaries, project,
                        {"name": "ZhuGuoCity.M2.PackageIntegration", "mode": "nullrhi"}, logs / "package.log"))
        script = Path(__file__).resolve().parent / "full_repair_check.py"
        if not script.is_file():
            raise ValueError("controller-owned complete validation bridge is missing")
        commands.append([str(Path(sys.executable).resolve()), str(script), "--runtime", str(runtime),
                         "--workspace", str(cwd), "--engine", str(engine), "--self-test"])
        cache = _task_output_directory(cwd, task["session_id"])
        directories = [source_run, runtime / "tasks" / task["id"], Path(request["request_path"]).parent
                       if "request_path" in request else Path(task["user_repair"]["request_path"]).parent,
                       script.parent, Path(sys.executable).resolve().parent, cache]
        ubt_source = binaries.parent.parent / "Source" / "Programs" / "UnrealBuildTool"
        if ubt_source.is_dir():
            directories.append(ubt_source)
        for directory in directories:
            if (directory == runtime or directory == cwd or cwd.is_relative_to(directory) or
                    any(_reparse(path) for path in (directory, *directory.parents))):
                raise ValueError("repair evidence grant is too broad or linked")
            # AdditionalDirectories grants a subtree, so a nested link must not
            # turn this exact evidence grant into access to another directory.
            if directory.is_dir():
                for parent, children, files in os.walk(directory, followlinks=False):
                    if any(_reparse(Path(parent) / name) for name in (*children, *files)):
                        raise ValueError("linked repair evidence")
        cache.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        fixed = []
        for argv in commands:
            command, binary_dirs = _claude_fixed_bash(argv, {})
            fixed.append(command)
            directories.extend(binary_dirs)
        prompt = (
            "\n本轮是用户明确授权的完整修复，由你本人以 Opus 5.5/high 自主诊断、修改项目 Source、测试和 Tools，并本地 git commit。"
            "原始只读测试阶段已经结束；该阶段‘不修改源码’限制不适用于本轮修复。禁止修改控制器框架、运行态、历史回执或用户授权范围；"
            "框架问题列明事实交维护者处理，不以删除用例、放宽断言、跳过检查或已知失败基线代替修复。\n"
            f"本次授权：{task['user_repair']['request_path']}；真实 Opus 方案/报告及原始证据：{source_run.as_posix()}；"
            f"原任务审核目录：{(runtime / 'tasks' / task['id']).as_posix()}。先读取这些材料，自行决定实现。\n"
            "保留既有编译与五项核心检查。以下额外命令只限本任务，全部 M 与每个 Local 必须核对实际发现/执行/Success，"
            "Local 还须核对真实 D3D12 与当轮截图；PackageIntegration 必须正式执行，前缀扫描的跳过不算通过。\n" +
            "\n".join(fixed) +
            "\n最后一条是允许 dirty WIP 的完整自测入口；其 user_repair_selftest 回执不能代替提交后的独立 CHECK。"
            "可按诊断需要选择上面的精确单项或完整自测，不要求重复执行已有有效证据的全部环节。"
            "最终新提交仍必须经过外层全 M、全部精确 Local 真渲染、正式包的独立检查，不能以已知失败基线忽略问题。"
            "旧 HEAD 的通过不能证明新提交通过；未完成、缺失、失败必须如实说明，不能报告已完成。\n"
            "本轮覆盖前文预检的执行等待参数：所有长 Bash 命令必须 run_in_background=true 并显式 timeout=3600000，"
            "后台不填 timeout 也会受到默认 600 秒限制。等原生完成通知，用 Read 读取通知 output_file 及检查回执。"
            "不要调用 TaskOutput 或 Monitor，不要启动只用于等待的 shell，不要终止用户 UE/其他会话。"
            f"原生后台输出只读目录：{cache.as_posix()}。\n"
        )
        return fixed, prompt, list(dict.fromkeys(directories))
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration) as error:
        raise ProviderError("Claude repair preflight is not safely bound: " + str(error)) from error


def _claude_repair_native(out: Path, record: dict) -> tuple:
    from .full_flow_native import parse_full_flow_native
    if (record.get("kind") != "code" or record.get("coding_provider") != "claude" or
            record.get("strict_model_selection") is not True or
            record.get("native_protocol") != "full_flow_native_v1" or
            not isinstance(record.get("user_repair_profile"), dict)):
        raise ValueError("Repair receipt lacks its explicit protocol authorization")
    return parse_full_flow_native(out / "stdout.log", record.get("requested_session"),
                                  record.get("coding_model"), record.get("coding_reasoning_effort"))


def _claude_followup_context(config, cwd, out, session, repair_context):
    profile = config.get("_user_followup_profile")
    if profile is None:
        return None
    try:
        from .user_followup import validate_followup_profile
        if repair_context is None:
            raise ValueError("A follow-up requires the active user repair")
        runtime = Path(config["runtime_dir"]).absolute()
        state = _json_file(runtime / "state.json")
        task, op = state["task"], state["operation"]
        turn = op.get("user_followup_turn")
        attempts = task.get("user_followup_attempts", [])
        if (profile != task.get("user_followup") or validate_followup_profile(task, runtime) != profile or
                profile.get("status") not in {"active", "completed"} or profile.get("session_id") != session or
                op.get("user_followup_id") != profile["id"] or
                op.get("user_followup_request_sha256") != profile["request_sha256"]):
            raise ValueError("Follow-up authorization or native session mismatch")
        if profile["status"] == "completed":
            from .user_followup import completed_followup_evidence
            proof = completed_followup_evidence(task, runtime)
            if turn is not None or proof.get("completed") is not True:
                raise ValueError("Ordinary repair cannot invent another completed follow-up turn")
            approved = {key: proof["evidence"][key] for key in ("config_sha256", "server_name", "raw_stdout_sha256")}
            return {"user_followup_profile": dict(profile), "user_followup_turn": None,
                    "user_followup_reuse": True, "user_followup_approved_mcp": approved}
        if (type(turn) is not int or not 1 <= turn <= min(3, profile["max_turns"]) or
                not attempts or attempts[-1].get("id") != profile["id"] or attempts[-1].get("turn") != turn or
                Path(attempts[-1].get("output_dir", "")).absolute() != Path(out).absolute()):
            raise ValueError("Follow-up authorization, attempt or native session mismatch")
        return {"user_followup_profile": dict(profile), "user_followup_turn": turn}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise ProviderError("Claude UE follow-up is not bound to its authorized turn: " + str(error)) from error


def _claude_local_mcp_config(cwd, out):
    """Load only Claude's project-authored single loopback HTTP server; never invent an endpoint."""
    path = Path(cwd) / ".mcp.json"
    selection = {"config_sha256": None, "server_name": None, "validation_error": None}
    if not path.exists() and not path.is_symlink():
        return selection, '{"mcpServers":{}}'
    try:
        ue_checks._safe_under(Path(cwd), ".mcp.json")
        if not path.is_file() or path.stat().st_size > 64 * 1024:
            raise ValueError("MCP configuration must be a bounded regular project file")
        raw = path.read_bytes()
        selection["config_sha256"] = hashlib.sha256(raw).hexdigest()
        value = _strict_json(raw.decode("utf-8-sig"))
        if (type(value) is not dict or set(value) != {"mcpServers"} or type(value["mcpServers"]) is not dict or
                len(value["mcpServers"]) != 1):
            raise ValueError("Declare exactly one local UE server in mcpServers")
        name, server = next(iter(value["mcpServers"].items()))
        if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name) or type(server) is not dict or
                set(server) != {"type", "url"} or server.get("type") != "http" or not isinstance(server.get("url"), str)):
            raise ValueError("UE MCP must have a simple server name and only type=http plus url")
        url = server["url"]
        parsed = urlsplit(url)
        if (any(ord(char) < 33 for char in url) or parsed.scheme != "http" or
                parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username is not None or
                parsed.password is not None or parsed.query or parsed.fragment or not parsed.port or not 1 <= parsed.port <= 65535):
            raise ValueError("UE MCP URL must use explicit-port loopback HTTP without credentials or query")
        snapshot = Path(out) / "ue-mcp-config.json"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if snapshot.exists():
            if snapshot.is_symlink() or snapshot.read_bytes() != raw:
                raise ValueError("This invocation already has a different MCP configuration snapshot")
        else:
            with snapshot.open("xb") as stream:
                stream.write(raw)
        selection["server_name"] = name
        return selection, str(snapshot)
    except (OSError, ValueError, UnicodeError, TypeError) as error:
        # A malformed model-authored file must not make it impossible for the
        # next bounded model turn to repair its own configuration. Never print
        # values from the rejected file (which might contain credentials).
        selection["validation_error"] = "Project .mcp.json was not loaded: one credential-free, explicit-port loopback HTTP server is required."
        return selection, '{"mcpServers":{}}'


def _claude_followup_preflight(config, cwd, out, context):
    runtime = Path(config["runtime_dir"]).absolute()
    task = _json_file(runtime / "state.json")["task"]
    argv = next(check["argv"] for check in config["checks"] if check["id"] == "ue-task-validation")
    engine = ue_checks._binaries(Path(argv[argv.index("--engine") + 1])).parent.parent
    directories = [engine / "Plugins/Experimental/ModelContextProtocol", engine / "Plugins/Experimental/Toolsets/AllToolsets",
                   Path(context["user_followup_profile"]["request_path"]).parent]
    for directory in directories:
        if not directory.is_dir() or any(_reparse(path) for path in (directory, *directory.parents)):
            raise ProviderError("Official UE MCP source or authorized follow-up directory is missing or linked")
    project = ue_checks._project(Path(cwd))
    owned = ue_checks._safe_under(Path(cwd), Path("Saved/Automation") / task["id"] / "ue-mcp")
    owned.mkdir(parents=True, exist_ok=True)
    powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not powershell.is_file():
        raise ProviderError("The task-scoped UE follow-up requires native Windows PowerShell")
    commands, binary_dirs = [], []
    for raw in ([str(engine / "Binaries/Win64/UnrealEditor.exe"), str(project)],
                [str(Path(sys.executable).resolve()), str(owned / "connect.py")],
                [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(owned / "connect.ps1")]):
        command, dirs = _claude_fixed_bash(raw, {})
        commands.extend([command, command + " *"])
        binary_dirs.extend(dirs)
    queries = [
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('UnrealEditor.exe','UnrealEditor-Cmd.exe') } | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Depth 3",
        "Get-NetTCPConnection -State Listen | Select-Object LocalAddress,LocalPort,OwningProcess | ConvertTo-Json -Depth 3",
    ]
    for query in queries:
        command, dirs = _claude_fixed_bash([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", query], {})
        commands.append(command); binary_dirs.extend(dirs)
    selection, mcp_argument = _claude_local_mcp_config(cwd, out)
    approved = context.get("user_followup_approved_mcp")
    if approved and any(selection.get(key) != approved[key] for key in ("config_sha256", "server_name")):
        selection.update(server_name=None, validation_error="Project MCP configuration differs from the previously verified connection; no server was loaded.")
        mcp_argument = '{"mcpServers":{}}'
    prompt = (
        "\n用户追加要求：UE MCP 的配置、启动、连接和工具选择由你本人负责，控制器没有替你选择端口、URL或server名。"
        "请读取本回合追加原文及官方源码，自主决定合理接法并实际使用。官方源码只读：\n" +
        "\n".join(str(path) for path in directories) +
        f"\n你可编辑项目 {Path(cwd) / '.mcp.json'}。它只允许一个server，字段为type=http及url；"
        "URL须是localhost/127.0.0.1/::1带显式端口，无用户信息、认证headers、query或fragment。"
        "本回合启动时没有合法配置则不连接任何MCP；你可本回合自行修好，下一个同会话原生回合才加载。最多三回合。"
        "本回合加载的是你原文件的不可变字节快照，回合中修改文件不会给本进程热加服务器。\n"
        f"加载结果：server={selection['server_name'] or '未加载'}；{selection['validation_error'] or '配置校验无错误'}。\n"
        "允许的额外本机接入命令如下（后缀*表示仅该固定程序/项目或你自己的明确脚本入口的参数）：\n" +
        "\n".join(commands) +
        f"\n脚本由你自行编写，仅限 {owned / 'connect.py'} 或 {owned / 'connect.ps1'} 两个入口；用途限本项目MCP配置、启动、连接和验证。"
        "重要宿主事实：当前CLI及它直接创建的子孙进程受Windows Job管理，KILL_ON_JOB_CLOSE会在原生回合退出时终止子孙，"
        "包括直接拉起的UE。请你依据官方工具和本机实际情况自行处理进程持久及所有权，控制器不会代写启动器。"
        "不得关闭用户已有UE或无关进程，不读密钥，不修改控制器框架、runtime状态或历史证明。"
        "Read/Edit外部只读授权不是UE/MCP内部执行代码的文件系统沙箱，你仍必须遵守上述边界。"
        "先确认MCP连接的是本任务项目和UE5.8，再合理选择工具；通用call_tool不等于只读工具。"
        "配置完成或报告文字不能代替实际调用，必须在原生MCP工具上完成调用。"
        "本追加回合允许无新提交；如有应版本化的项目改动，由你提交。最终仍须clean候选与原完整独立CHECK。"
        "不要为满足提交判定制造无关提交；未接通须如实交代本轮事实与下一步。\n"
    )
    if context.get("user_followup_reuse"):
        prompt += (
            "\n此前同任务MCP接入已经真实验证，本轮为正常后续修复CODE，不增加接入回合次数，也不要求每轮重复调用MCP。"
            "仅与你此前成功证据字节SHA及server名一致的项目配置可继续加载；更改配置不会自动获得新连接权限。"
            "普通CODE仍须按原任务提交真实变更并通过全部独立检查，上述无新提交特例只适用于之前的追加接入回合。\n"
        )
    elif context["user_followup_profile"].get("status") == "active":
        prompt += (
            "\n本追加接入回合的最终范围说明（优先于前文普通修复CODE的交付和自测要求）："
            "本回合仅处理用户追加的UE MCP接入，不在此回合重复原完整游戏修复提交及全套游戏自测。"
            "请自行决定必要的接入验证；配置就绪后即可如实交接并结束本回合，让下一个同会话原生回合加载配置并实际调用MCP。"
            "不必因仅有MCP配置提交而重跑全量M、原5核心、19项Local或正式PackageIntegration。"
            "这不改变最终验收：接入结束后控制器仍会对clean候选完整运行一次独立CHECK，后续普通CODE也保持原提交与检查门槛。\n"
        )
    return commands, prompt, directories + binary_dirs, selection, mcp_argument


def _claude_followup_evidence(out, record):
    selection = record.get("user_followup_mcp")
    if not isinstance(selection, dict):
        raise ValueError("Follow-up receipt lacks its MCP loading selection")
    name = selection.get("server_name")
    if name:
        snapshot = out / "ue-mcp-config.json"
        if not snapshot.is_file() or snapshot.is_symlink() or hashlib.sha256(snapshot.read_bytes()).hexdigest() != selection["config_sha256"]:
            raise ValueError("Loaded model-authored MCP snapshot changed")
    native = _claude_repair_native(out, record)
    prefix = "mcp__" + name + "__" if name else None
    connected, registered, calls = False, set(), {}
    with (out / "stdout.log").open(encoding="utf-8-sig") as stream:
        for line in stream:
            if not line.strip():
                continue
            event = _strict_json(line)
            if event.get("type") == "system" and event.get("subtype") == "init":
                connected |= any(type(server) is dict and server.get("name") == name and server.get("status") == "connected"
                                 for server in event.get("mcp_servers", [])) if name else False
                registered.update(tool for tool in event.get("tools", []) if prefix and isinstance(tool, str) and tool.startswith(prefix))
            blocks = event.get("message", {}).get("content", []) if type(event.get("message")) is dict else []
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if (event.get("type") == "assistant" and block.get("type") == "tool_use" and prefix and
                        isinstance(block.get("name"), str) and block["name"].startswith(prefix)):
                    identity = block.get("id")
                    if not isinstance(identity, str) or identity in calls:
                        raise ValueError("Duplicate or missing MCP native tool-call identity")
                    calls[identity] = {"tool_use_id": identity, "tool_name": block["name"], "completed": False, "is_error": None}
                elif event.get("type") == "user" and block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                    call = calls[block["tool_use_id"]]
                    if call["completed"]:
                        raise ValueError("Duplicate MCP native tool result")
                    call.update(completed=True, is_error=block.get("is_error") is True)
    evidence = {**selection, "connected": connected, "registered_tools": sorted(registered),
                "invocations": list(calls.values()), "raw_stdout_sha256": native[4]["raw_stdout_sha256"]}
    return {"completed": bool(connected and any(call["completed"] and call["is_error"] is False for call in calls.values())),
            "evidence": evidence}


def _managed_art_task(config: dict, cwd: Path, out: Path, kind: str) -> tuple[dict, dict] | None:
    """Bind art-only privileges/model selection to the scheduler's current turn."""
    if "art_pipeline" not in config:
        return None
    try:
        runtime = Path(config["runtime_dir"]).absolute()
        state = _json_file(runtime / "state.json")
        task = state.get("task") or {}
        if task.get("kind") != "art":
            return None
        settings = validate_art_pipeline_settings(config["art_pipeline"],
                                                   project=Path(config["project_path"]), runtime=runtime)
        cwd, out = Path(cwd).absolute(), Path(out).absolute()
        operation = state["operation"]
        phases = {"code": "CODE", "review": "ART_REVIEW", "art_diagnose": "ART_DIAGNOSE"}
        if (kind not in phases or cwd != runtime / "workspace" or out.parent != runtime / "runs"
                or state["phase"] != phases[kind]
                or operation["kind"] != kind or operation["task_id"] != task["id"]
                or Path(operation["output_dir"]).absolute() != out or task["id"] not in settings["task_ids"]):
            raise ValueError("art operation mismatch")
        if any(_reparse(path) for path in (runtime, *runtime.parents)):
            raise ValueError("linked art runtime")
        for relative in ("workspace", "runs", "state.json", Path("runs") / out.name):
            ue_checks._safe_under(runtime, relative)
        if task.get("coding_model") != ART_CODING_MODEL or task.get("coding_reasoning_effort") != ART_CODING_REASONING_EFFORT:
            raise ValueError("art tasks require Claude Opus 5.5 high")
        if kind == "code" and (operation.get("coding_model") != ART_CODING_MODEL or
                               operation.get("coding_reasoning_effort") != ART_CODING_REASONING_EFFORT):
            raise ValueError("art operation model differs from its task")
        if kind == "art_diagnose" and (operation.get("diagnosis_provider") != "claude" or
                operation.get("diagnosis_model") != ART_CODING_MODEL or
                operation.get("diagnosis_reasoning_effort") != ART_CODING_REASONING_EFFORT):
            raise ValueError("art diagnosis model differs from its task")
        return task, settings
    except (KeyError, TypeError, ValueError, OSError, AttributeError) as error:
        raise ProviderError("Art provider requires a valid managed task: " + str(error)) from error


def _art_public_evidence_directory(config: dict, cwd: Path, out: Path, kind: str) -> Path | None:
    context = _managed_art_task(config, cwd, out, kind)
    if context is None:
        return None
    task, settings = context
    try:
        artifacts, batch_file = Path(settings["artifacts_dir"]), Path(settings["config_file"])
        request = task["art_request"]
        if (Path(request["artifacts_dir"]).absolute() != artifacts or
                Path(request["config_file"]).absolute() != batch_file or
                hashlib.sha256(batch_file.read_bytes()).hexdigest() != request["config_sha256"]):
            raise ValueError("art review batch does not match its queued version")
        if not artifacts.is_dir() or any(_reparse(path) for root in (artifacts, batch_file) for path in (root, *root.parents)):
            raise ValueError("missing or linked public artifact directory")
        pending = [artifacts]
        while pending:
            for child in pending.pop().iterdir():
                if _reparse(child):
                    raise ValueError("linked artifact")
                if child.is_dir():
                    pending.append(child)
        batch = _json_file(batch_file)
        if Path(batch["artifacts_dir"]).resolve() != artifacts.resolve():
            raise ValueError("batch artifact directory differs from the approved bridge")
        from unattended.art_pipeline import blender_job_file_path, recipe_file_path
        recipe_file_path(batch, workspace=cwd)
        blender_job_file_path(batch, workspace=cwd)
        private, key_file = Path(batch["private_state_dir"]), Path(batch["key_file"])
        if not private.is_absolute() or not key_file.is_absolute():
            raise ValueError("private state and key paths must be absolute")
        granted = [artifacts.resolve(), Path(cwd).resolve()]
        if any(private.resolve() == directory or private.resolve().is_relative_to(directory)
               or directory.is_relative_to(private.resolve()) or key_file.resolve().is_relative_to(directory)
               for directory in granted):
            raise ValueError("private state or credential overlaps review-readable files")
        return artifacts.resolve()
    except (KeyError, TypeError, ValueError, OSError, AttributeError) as error:
        raise ProviderError("Art provider requires isolated public evidence: " + str(error)) from error


def _codex_art_review_directory(config: dict, cwd: Path, out: Path) -> Path | None:
    return _art_public_evidence_directory(config, cwd, out, "review")


def _claude_art_diagnosis_context(config: dict, cwd: Path, out: Path, source: str) -> tuple[list[Path], dict]:
    """Grant only this task's public evidence, never the whole runtime or credentials."""
    context = _managed_art_task(config, cwd, out, "art_diagnose")
    if context is None:
        raise ProviderError("Art diagnosis requires a managed art task")
    task, settings = context
    try:
        if (not isinstance(source, str) or not _CLAUDE_SESSION.fullmatch(source) or
                task.get("session_id") != source or task.get("session_owner_task_id", task["id"]) != task["id"]):
            raise ValueError("diagnostic source differs from the production session")
        runtime = Path(config["runtime_dir"]).absolute()
        state = _json_file(runtime / "state.json")
        operation = state["operation"]
        head = operation.get("head_sha", task.get("candidate"))
        if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
            raise ValueError("diagnostic operation requires a complete head identity")
        artifacts = _art_public_evidence_directory(config, cwd, out, "art_diagnose")
        task_directory = ue_checks._safe_under(runtime, Path("tasks") / task["id"])
        if not task_directory.is_dir():
            raise ValueError("current task evidence directory is missing")
        directories = [Path(cwd).resolve(), artifacts, task_directory.resolve()]
        batch = _json_file(Path(settings["config_file"]))
        private, key = Path(batch["private_state_dir"]).resolve(), Path(batch["key_file"]).resolve()
        for directory in directories:
            if (directory == runtime.resolve() or directory == private or directory.is_relative_to(private) or
                    private.is_relative_to(directory) or key.is_relative_to(directory)):
                raise ValueError("private state or credential overlaps diagnostic evidence")
            pending = [directory]
            while pending:
                for child in pending.pop().iterdir():
                    if _reparse(child):
                        raise ValueError("linked diagnostic evidence")
                    if child.is_dir():
                        pending.append(child)
        binding = {"task_id": task["id"], "workspace": str(Path(cwd).resolve()),
                   "output_dir": str(Path(out).resolve()), "head_sha": head}
        return directories, binding
    except (KeyError, TypeError, ValueError, OSError, AttributeError) as error:
        raise ProviderError("Claude art diagnosis requires task-scoped read-only evidence: " + str(error)) from error


def _codex_read_only_profile(directory: Path, out: Path) -> list[str]:
    # --add-dir grants writable roots; use a read-only permission profile instead.
    # Override only this invocation, with a unique name to avoid inherited entries.
    name = "art-review-" + hashlib.sha256(str(out).encode()).hexdigest()[:16]
    # Legacy elevated Windows sandbox requires effective :root read. Keep the
    # existing Codex read-only baseline; this is not a read-isolation boundary.
    # The validated public directory itself cannot contain private state/keys.
    filesystem = {":root": "read", ":workspace_roots": "read", directory.as_posix(): "read"}
    inline = "{" + ",".join(json.dumps(key) + "=" + json.dumps(value) for key, value in filesystem.items()) + "}"
    return ["-c", "default_permissions=" + json.dumps(name),
            "-c", "permissions." + name + ".filesystem=" + inline,
            "-c", "permissions." + name + ".network.enabled=false"]


def _claude_art_preflight(config: dict, cwd: Path, out: Path) -> tuple[list[str], str, list[Path]]:
    """Allow only the configured preview for its persisted, explicitly named CODE task."""
    if "art_pipeline" not in config:
        return [], "", []
    try:
        runtime = Path(config["runtime_dir"]).absolute()
        settings = validate_art_pipeline_settings(config["art_pipeline"],
                                                   project=Path(config["project_path"]), runtime=runtime)
        cwd, out = Path(cwd).absolute(), Path(out).absolute()
        if (cwd != runtime / "workspace" or out.parent != runtime / "runs" or
                any(_reparse(path) for path in (runtime, *runtime.parents))):
            raise ValueError("unmanaged operation")
        for relative in ("workspace", "runs", "state.json", Path("runs") / out.name):
            ue_checks._safe_under(runtime, relative)
        state = _json_file(runtime / "state.json")
        task_id, operation = state["task"]["id"], state["operation"]
        if (state["phase"] != "CODE" or operation["kind"] != "code" or
                operation["task_id"] != task_id or Path(operation["output_dir"]).absolute() != out):
            raise ValueError("operation mismatch")
        if task_id not in settings["task_ids"]:
            return [], "", []
        batch_file, artifacts = Path(settings["config_file"]), Path(settings["artifacts_dir"])
        if (not batch_file.is_file() or not artifacts.is_dir() or
                any(_reparse(path) for root in (batch_file, artifacts) for path in (root, *root.parents))):
            raise ValueError("missing or linked batch configuration or public artifact directory")
        # AdditionalDirectories is a whole subtree grant: refuse linked descendants.
        pending = [artifacts]
        while pending:
            for child in pending.pop().iterdir():
                if _reparse(child):
                    raise ValueError("linked artifact")
                if child.is_dir():
                    pending.append(child)
        script = _ART_PIPELINE_SCRIPT
        if not script.is_file():
            raise ValueError("art pipeline entry point is missing")
        command, command_directories = _claude_fixed_bash(
            [str(Path(sys.executable).resolve()), str(script), "--config", str(batch_file), "preview"], {})
        directories = list(dict.fromkeys([artifacts.resolve(), script.parent.resolve(),
                                          *command_directories, batch_file.parent.resolve()]))
        batch = _json_file(batch_file)
        if Path(batch["artifacts_dir"]).resolve() != artifacts.resolve():
            raise ValueError("batch artifact directory differs from the approved bridge")
        from unattended.art_pipeline import blender_job_file_path, recipe_file_path
        editable_recipe = recipe_file_path(batch, workspace=cwd)
        editable_job = blender_job_file_path(batch, workspace=cwd)
        private = Path(batch["private_state_dir"])
        key_file = Path(batch["key_file"])
        if not private.is_absolute() or not key_file.is_absolute():
            raise ValueError("private state and key paths must be absolute")
        private, key_file = private.resolve(), key_file.resolve()
        if any(private == directory or private.is_relative_to(directory) or directory.is_relative_to(private) or
               key_file == directory or key_file.is_relative_to(directory) for directory in directories):
            raise ValueError("private state or credential path overlaps an approved read-only directory")
    except (KeyError, TypeError, ValueError, OSError, AttributeError) as error:
        raise ProviderError("Claude art preview requires a valid task-scoped managed operation: " + str(error)) from error
    instructions = (
        "\n本任务获准调用固定美术资产预览入口，必须用前台 Bash 原样执行下列命令并等候其结束。"
        "批次总预算硬上限为 3000 积分，实际可用额度以批次账本为准；上限不是必须花完的目标。"
        "本轮仅生产已批准的低、中、高三个住宅等级，不得自动补齐八级或扩大资产范围。"
        "其余五级必须等待用户确认预览并明确授权。可以只读查看专用目录中的非密钥批次配置，"
        "不要改批次配置、账本、预算或调用参数；"
        "不要读取密钥、直接请求 Tripo、另起未列出的 Blender/UE 命令，或用另一脚本绕过固定入口。"
        "入口负责复用已有任务与合格产物；未知远端提交结果不得盲目重交。"
        "生成、处理、UE 导入与保存均发生在本地提交之前；提交后的独立 CHECK 只能检查并采证，不能修改 Content。"
        "返回成功或截图存在不代表美术验收通过，应读取实际回执、标准镜头和近景证据，"
        "按任务书核对风格、三级差异、尺度、材质与院落组织，并保留用户视觉确认状态。\n"
        + command + "\n本批次公共产物只读目录：" + artifacts.as_posix() + "\n"
    )
    if editable_recipe is not None:
        instructions += (
            "本任务可编辑的美术配方文件：" + editable_recipe.as_posix() + "。先按用户反馈修改该文件，再执行上面的固定预览命令。"
            "配方只允许 version=1 和按批准顺序排列的全部三个 assets；每项字段严格限于 asset_id、revision、prompt、"
            "target_height_m、max_footprint_m、front_rotation_deg。不得增删、改名、重排资产或加入等级、预算、参考图、路径及工具参数。"
            "revision 为 v001..v999；改变生成提示词必须给该资产新版本，仅返工的等级升级版本，其他等级原样保留以复用已付费模型。"
            "prompt 非空且不超过 8000 字符；高度 1.5..8 米，占地两维各不超过固定配置上限，朝向为 0/90/180/270。"
            "配方及其 SHA 会随产物保存，修改后必须重新完成固定预览与独立检查；不得拿旧截图冒充新配方结果。\n"
        )
    if editable_job is not None:
        instructions += (
            "本任务指定由你亲自编写的 Blender 脚本：" + editable_job.as_posix() + "。"
            "先读取工作区只读 BLENDER_JOB_API.md；只可编辑本脚本及已列出的 recipe_file，禁止改框架、预算、账本或主游戏。"
            "实现 run_job(context) 并返回 {'instances':list,'plots':list,'report':dict}。"
            "context 提供 houses（bpy对象列表）、entries、manifest、output_dir、assets/materials注册表及api："
            "bounds、join_meshes、export_asset、procedural_material、process_material、box、cylinder、timber_between、pottery。"
            "由你修改房屋并制作院落和必要生活道具，修改后的资产必须通过 export_asset 重新导出并保留source provenance。"
            "有此脚本时固定院落和屋顶预设完全跳过，维护验证稿不代表你的正式作品。"
            "仅使用上面的固定 preview 入口运行脚本；不能直接启动其他Python、Blender或UE命令。"
            "控制器会按每个允许文件SHA和当前验收包SHA提交；不要自行 git add、commit 或push。\n"
        )
    return [command], instructions, directories


def _claude_validation_read_directory(config: dict, cwd: Path, out: Path) -> Path | None:
    """Grant current-task validation evidence only for a matching managed CODE operation."""
    if not config.get("runtime_dir"):
        return None
    try:
        runtime = Path(config["runtime_dir"]).absolute()
        cwd, out = Path(cwd).absolute(), Path(out).absolute()
        if (cwd != runtime / "workspace" or out.parent != runtime / "runs" or
                any(_reparse(path) for path in (runtime, *runtime.parents))):
            return None
        for relative in ("workspace", "runs", "state.json", Path("runs") / out.name):
            ue_checks._safe_under(runtime, relative)
        state = _json_file(runtime / "state.json")
        task_id = state["task"]["id"]
        operation = state["operation"]
        if (not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", task_id) or
                state["phase"] != "CODE" or operation["kind"] != "code" or
                operation["task_id"] != task_id or Path(operation["output_dir"]).absolute() != out):
            return None
        directory = ue_checks._safe_under(runtime, Path("tasks") / task_id / "validation")
        if not directory.is_dir():
            return None
        pending = [directory]
        while pending:
            for child in pending.pop().iterdir():
                if _reparse(child):
                    return None
                if child.is_dir():
                    pending.append(child)
        return directory.resolve()
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _claude_environment() -> dict[str, str]:
    # Login is owned by the native CLI. Do not pass API keys, gateways, cloud
    # provider selectors, or overrides that point it at another credential store.
    environment = safe_environment()
    environment.update({key: os.environ[key] for key in _CLAUDE_ENV if os.environ.get(key)})
    environment["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
    return environment


def claude_subscription_ready(settings: dict, cwd: Path) -> bool:
    """Check native Pro authentication without logging account details or calling a model."""
    try:
        process = subprocess.run([*_claude_command(settings), "auth", "status", "--json"],
                                 cwd=cwd, env=_claude_environment(), capture_output=True,
                                 timeout=15, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if process.returncode or len(process.stdout) > 16384:
            return False
        status = _strict_json(process.stdout.decode("utf-8-sig"))
        return (type(status) is dict and status.get("loggedIn") is True and
                status.get("authMethod") == "claude.ai" and status.get("apiProvider") == "firstParty" and
                status.get("subscriptionType") == "pro")
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
        return False


def _bundled_codex_root(executable: str) -> Path | None:
    """Recognize only this user's configured desktop-managed version layout."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    path, base = Path(executable), Path(local)
    root = base / "OpenAI" / "Codex" / "bin"
    if (not path.is_absolute() or not base.is_absolute() or path.name.casefold() != "codex.exe" or
            not re.fullmatch(r"[0-9a-fA-F]{16}", path.parent.name) or
            os.path.normcase(str(path.parent.parent)) != os.path.normcase(str(root))):
        return None
    return root


def _reparse(path: Path) -> bool:
    try:
        fact = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(fact, "st_file_attributes", 0) & 0x400)


def _codex_command(value: Any) -> list[str]:
    """Survive desktop updates without changing explicitly selected custom CLIs."""
    command = _command(value, ["codex"])
    root = _bundled_codex_root(command[0])
    if root is None:
        return command
    path = Path(command[0])
    # Check the configured installation components before selecting any sibling.
    if any(_reparse(part) for part in (root.parent.parent.parent, root.parent.parent, root.parent, root,
                                        path.parent, path)):
        raise ProviderError("Bundled Codex executable paths cannot be links or junctions")
    if path.is_file():
        return command
    candidates = []
    try:
        entries = list(root.iterdir()) if root.is_dir() else []
        if len(entries) > 100:
            raise ProviderError("Bundled Codex executable inventory is ambiguous")
        for folder in entries:
            if not re.fullmatch(r"[0-9a-fA-F]{16}", folder.name) or _reparse(folder) or not folder.is_dir():
                continue
            candidate = folder / "codex.exe"
            if not _reparse(candidate) and candidate.is_file():
                candidates.append((candidate.stat().st_mtime_ns, candidate))
    except FileNotFoundError:
        # The desktop updater may replace a directory during this inventory.
        candidates = []
    candidates.sort(key=lambda item: item[0], reverse=True)
    if candidates and (len(candidates) == 1 or candidates[0][0] > candidates[1][0]):
        command[0] = str(candidates[0][1])
        return command
    raise ProviderError("Bundled Codex executable is temporarily unavailable during update",
                        transient=True, retry_reason="local_cli_unavailable")


def _codex_api_key(config_path: str | Path | None = None) -> str | None:
    """Read the operator's named variable even when Task Scheduler has an old environment."""
    value = os.environ.get("CODEX_API_KEY")
    if not value:
        value = read_secret("CODEX_API_KEY", config_path=config_path)
    if not value and os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as variables:
                value, _ = winreg.QueryValueEx(variables, "CODEX_API_KEY")
        except OSError:
            pass
    if isinstance(value, str):
        value = value.strip()
        if value and len(value) <= 4096 and not any(char.isspace() or ord(char) < 32 for char in value):
            return value
    return None


class AgentProviders:
    def __init__(self, config: dict):
        self.config = config

    def plan(self, prompt: str, cwd: Path, out: Path, timeout: float, *,
             model: str | None = None, reasoning_effort: str | None = None,
             tiered: bool | None = None) -> dict:
        if tiered is not None and type(tiered) is not bool:
            raise ProviderError("Tiered planning must be a boolean")
        if self.config.get("planning_provider", "codex") == "claude":
            return self._claude_plan(prompt, cwd, out, timeout, model=model,
                                     reasoning_effort=reasoning_effort, tiered=tiered)
        if model is not None or reasoning_effort is not None or tiered is True:
            raise ProviderError("Planner model overrides require the Claude planning provider")
        return self._codex("plan", prompt, cwd, out, timeout)

    def review(self, prompt: str, cwd: Path, out: Path, timeout: float) -> dict:
        return self._codex("review", prompt, cwd, out, timeout)

    def _codex(self, kind: str, prompt: str, cwd: Path, out: Path, timeout: float) -> dict:
        settings = self.config.get("codex", {})
        try:
            validate_model_settings(settings, "codex")
        except ValueError as error:
            raise ProviderError(str(error)) from error
        if settings.get(kind + "_instructions"):
            prompt += "\n" + str(settings[kind + "_instructions"])
        command = _codex_command(settings.get("command"))
        sandbox = settings.get("sandbox", "read-only")
        if sandbox != "read-only":
            raise ProviderError("Codex planner and reviewer require the read-only sandbox")
        out = Path(out).resolve()
        art_directory = _codex_art_review_directory(self.config, Path(cwd), out) if kind == "review" else None
        if art_directory is not None:
            prompt += ("\n本轮美术验收公共证据目录（仅可读取）：" + art_directory.as_posix() +
                       "。读取 review-package.json、independent-check.json 与实际截图；"
                       "只审阅公共产物与主项目，不要读取密钥、私有账本或无关目录。"
                       "不得修改产物、配置或主项目。此次验收不能代替用户视觉批准。\n")
        deadline = time.monotonic() + timeout
        auth_mode = settings.get("auth_mode")
        fallback = auth_mode == "api_key" and settings.get("subscription_fallback") is True
        if fallback and any((out / name).exists() for name in (
                "stdout.log", "stderr.log", "process.json", "result.json", "final.json", _CODEX_SUBSCRIPTION_DIR)):
            raise ProviderError("Invocation directory is already used; recover it or allocate a new attempt")
        try:
            self._codex_attempt(kind, prompt, cwd, out, deadline, command, sandbox, settings,
                                auth_mode, subscription_fallback=fallback, art_directory=art_directory)
        except ProviderError:
            if not fallback or not _codex_api_quota_failed(kind, out):
                raise
            self._codex_attempt(kind, prompt, cwd, out / _CODEX_SUBSCRIPTION_DIR, deadline,
                                command, sandbox, settings, "chatgpt", art_directory=art_directory)
        recovered = self.recover(kind, out)
        if recovered is None:
            raise ProviderError("Codex completed without a durable structured result")
        _atomic_json(out / "result.json", recovered)
        return recovered

    def _codex_attempt(self, kind: str, prompt: str, cwd: Path, out: Path, deadline: float,
                       command: list[str], sandbox: str, settings: dict, auth_mode: str | None,
                       *, subscription_fallback: bool = False, art_directory: Path | None = None) -> None:
        out.mkdir(parents=True, exist_ok=True)
        schema = REVIEW_SCHEMA
        if kind == "plan":
            planned = (self.config.get("coding_provider") == "claude" and
                       self.config.get("claude", {}).get("planned_core_tests") is True)
            schema = PLAN_WITH_CORE_TESTS_SCHEMA if planned else PLAN_SCHEMA
        _atomic_json(out / "schema.json", schema)
        permission_args = (_codex_read_only_profile(art_directory, out) if art_directory is not None
                           else ["--sandbox", sandbox])
        argv = [*command, "--no-daemon", "-a", "never", "exec", *permission_args, "--json",
                "--color", "never", "--output-schema", str(out / "schema.json"),
                "-o", str(out / "final.json")]
        if settings.get("model"):
            argv.extend(["--model", settings["model"]])
        if "reasoning_effort" in settings:
            argv.extend(["-c", "model_reasoning_effort=" + json.dumps(settings["reasoning_effort"])])
        environment = safe_environment()
        auth_variables = ("CODEX_HOME", "NODE_EXTRA_CA_CERTS")
        if auth_mode == "api_key":
            key = _codex_api_key(self.config.get("config_path"))
            if not key:
                raise ProviderError("CODEX_API_KEY is required for API-authenticated planning and review")
            environment["CODEX_API_KEY"] = key
            # The built-in provider can follow a saved ChatGPT login even
            # when CODEX_API_KEY is present. Bind this request to the key.
            argv.extend(["-c", 'model_provider="unattended_openai_api"',
                         "-c", 'model_providers.unattended_openai_api={name="OpenAI API",'
                               'base_url="https://api.openai.com/v1",env_key="CODEX_API_KEY",'
                               'requires_openai_auth=false,wire_api="responses"}',
                         "-c", 'shell_environment_policy.filters={CODEX_API_KEY="exclude",OPENAI_API_KEY="exclude"}',
                         "-c", "shell_environment_policy.ignore_default_excludes=false"])
        elif auth_mode != "chatgpt":
            auth_variables += ("CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL")
        for key in auth_variables:
            if os.environ.get(key):
                environment[key] = os.environ[key]
        if auth_mode == "chatgpt":
            self._check_codex_subscription(command, Path(cwd), environment, deadline)
            argv.extend(["-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"'])
        argv.append("-")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError("Codex exhausted its time budget before the request", interrupted=True)
        self._invoke(kind, argv, prompt, Path(cwd), out, remaining, environment,
                     codex_auth_mode=auth_mode, codex_subscription_fallback=subscription_fallback,
                     review_read_only_dirs=[str(art_directory)] if art_directory is not None else None)

    @staticmethod
    def _check_codex_subscription(command: list[str], cwd: Path, environment: dict[str, str], deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError("Codex exhausted its time budget before subscription fallback", interrupted=True)
        try:
            # Forcing a mismatched login can log out existing credentials.
            # Confirm native subscription auth before enforcing the mode.
            result = subprocess.run([*command, "--no-daemon", "login", "status"], cwd=cwd, env=environment,
                                    capture_output=True, stdin=subprocess.DEVNULL, timeout=min(10, remaining),
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProviderError("OpenAI API quota exhausted; could not verify the saved ChatGPT login",
                                interrupted=time.monotonic() >= deadline) from error
        if result.returncode != 0 or b"\n".join((result.stdout, result.stderr)).strip() != b"Logged in using ChatGPT":
            raise ProviderError("OpenAI API quota exhausted; subscription fallback requires a saved ChatGPT login")

    def code(self, prompt: str, cwd: Path, out: Path, timeout: float,
             session_id: str | None = None) -> dict:
        if self.config.get("coding_provider", "dsh") == "claude":
            return self._claude_code(prompt, cwd, out, timeout, session_id)
        settings = self.config.get("dsh", {})
        try:
            validate_model_settings(settings, "dsh")
        except ValueError as error:
            raise ProviderError(str(error)) from error
        if settings.get("instructions"):
            prompt += "\n" + str(settings["instructions"])
        if session_id is not None and not _SESSION.fullmatch(session_id):
            raise ProviderError("Invalid persisted DSH session identity")
        out = Path(out).resolve()
        out.mkdir(parents=True, exist_ok=True)
        if settings.get("command"):
            argv = _command(settings["command"], [])
        else:
            install = Path(settings.get("desktop_install", "")).expanduser().resolve()
            executable = install / "DeepSeek Harness.exe"
            archive = install / "resources/app.asar"
            if not executable.is_file() or not archive.is_file():
                raise ProviderError("Set dsh.command or the installed DeepSeek Harness desktop_install")
            entry = archive / "dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"
            argv = [str(executable), "--expose-internals", str(entry)]
        for raw in settings.get("patches", []):
            patch = Path(raw).expanduser().resolve()
            if not patch.is_file():
                raise ProviderError("Configured DSH overlay does not exist: " + str(patch))
            argv.extend(["--patch", str(patch)])
        permission = settings.get("permission_mode", "workspace-write")
        if permission not in {"workspace-write", "danger-full-access"}:
            raise ProviderError("DSH coding requires workspace-write or danger-full-access")
        retries = settings.get("request_retries", 0)
        if type(retries) is not int or not 0 <= retries <= 3:
            raise ProviderError("DSH request_retries must be an integer from 0 through 3")
        policy = [
            {"id": "sandbox-policy", "config": {"mode": permission}},
            {"id": "approval", "config": {"policy": "never"}},
            {"id": "llm-retry", "disabled": retries == 0},
        ]
        account_policy = {}
        if retries:
            if settings.get("provider", "deepseek-account") != "deepseek-account":
                raise ProviderError("Bounded request retries currently require deepseek-account")
            account_policy.update({
                "baseURL": settings.get("account_base_url", "https://api.deepseek.com/anthropic"),
                "retryPolicy": {"mode": "normal", "maxRetries": retries,
                                "retryableCodes": ["TRANSPORT"],
                                "backoff": {"initialDelayMs": 1000, "maxDelayMs": 3000, "jitterRatio": 0.1}}})
        selection = {}
        if settings.get("model"):
            selection.update(provider=settings.get("provider", "deepseek-account"), model=settings["model"])
        if "reasoning_effort" in settings:
            if settings.get("provider", "deepseek-account") != "deepseek-account":
                raise ProviderError("Explicit DSH reasoning currently requires deepseek-account")
            selection["reasoningEffort"] = settings["reasoning_effort"]
            account_policy["thinking"] = "disabled" if settings["reasoning_effort"] == "off" else "enabled"
        if account_policy:
            policy.append({"id": "llm-deepseek-account", "config": account_policy})
        if selection:
            policy.append({"id": "agent-default-model", "config": selection})
        _atomic_json(out / "invocation-policy.yml", policy)  # JSON is valid YAML.
        argv.extend(["--patch", str(out / "invocation-policy.yml"), "--profile", "headless", "--json"])
        if session_id:
            argv.extend(["--session-id", session_id])
        argv.append("-")
        environment = safe_environment()
        environment.update(ELECTRON_RUN_AS_NODE="1", DSH_PERMISSION_MODE=permission)
        if settings.get("home"):
            home = Path(settings["home"]).expanduser().resolve()
            if not home.is_dir():
                raise ProviderError("Configured DSH home must be an existing directory")
            environment["DSH_HOME"] = str(home)
        for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "NODE_EXTRA_CA_CERTS"):
            if os.environ.get(key):
                environment[key] = os.environ[key]
        if "DEEPSEEK_API_KEY" not in environment:
            key = read_secret("DEEPSEEK_API_KEY", config_path=self.config.get("config_path"))
            if key:
                environment["DEEPSEEK_API_KEY"] = key
        self._invoke("code", argv, prompt, Path(cwd), out, timeout, environment,
                     requested_session=session_id)
        recovered = self.recover("code", out)
        if recovered is None:
            raise ProviderError("DSH completed without a durable result", session_id=read_session(out))
        _atomic_json(out / "result.json", recovered)
        return recovered

    def _claude_plan(self, prompt: str, cwd: Path, out: Path, timeout: float, *,
                     model: str | None = None, reasoning_effort: str | None = None,
                     tiered: bool | None = None) -> dict:
        settings = dict(self.config.get("claude", {}))
        if model is not None:
            settings["model"] = model
        if reasoning_effort is not None:
            settings["reasoning_effort"] = reasoning_effort
        try:
            validate_model_settings(settings, "claude")
        except ValueError as error:
            raise ProviderError(str(error)) from error
        if settings.get("plan_instructions"):
            prompt += "\n" + str(settings["plan_instructions"])
        if not claude_subscription_ready(settings, cwd):
            raise ProviderError("Claude planning requires an active native Claude Pro login")
        out = Path(out).resolve()
        if any((out / name).exists() for name in ("stdout.log", "stderr.log", "process.json", "result.json", "final.json")):
            raise ProviderError("Invocation directory is already used; recover it or allocate a new attempt")
        planned = (self.config.get("coding_provider") == "claude" and
                   settings.get("planned_core_tests") is True)
        schema = STRICT_PLAN_WITH_CORE_TESTS_SCHEMA if planned else PLAN_SCHEMA
        if tiered is None:
            tiered = bool(settings.get("critical_plan_model"))
        if tiered:
            schema = STRICT_TIERED_PLAN_WITH_CORE_TESTS_SCHEMA if planned else TIERED_PLAN_SCHEMA
            prompt += (
                "\n同时返回 planning_level=routine/critical 和非空 planning_reason，按本轮任务实际要改变的内容分级。"
                "涉及关键系统设计、跨系统接口或状态流、存档格式与迁移、核心模拟规则的新增或实质修改时选 critical。"
                "范围明确的局部修正、已有行为的测试夹具更新、界面表现调整通常选 routine。"
                "仅要求保持既有存档、接口或核心规则兼容，不代表本轮要修改这些系统，也不能据此升级为 critical。"
                "action=complete 时选 critical，并说明需要阶段完整性复核。"
                "planning_reason 必须具体说明本次变更范围与分级依据。\n"
            )
        if planned:
            prompt += (
                "\ncore_tests 数组每项只能填写完整、精确的 ZhuGuoCity 自动化测试名。"
                "测试与验收条件的对应关系、复用或新增说明只写在 body。"
                "core_tests 中不得附加括号、空格、解释文字、命令、通配符或其他前后缀。\n"
            )
        _atomic_json(out / "schema.json", schema)
        directories = [Path(self.config["runtime_dir"]).resolve()] if self.config.get("runtime_dir") else []
        argv = _claude_command(settings, read_only_dirs=directories)
        argv.extend(["--print", "--output-format", "stream-json", "--verbose",
                     "--permission-mode", "plan", "--permission-prompts", "none",
                     "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                     "--tools", "Read,Glob,Grep", "--disallowedTools", "mcp__*",
                     "--json-schema", json.dumps(schema, separators=(",", ":"))])
        if settings.get("model"):
            argv.extend(["--model", settings["model"]])
        if settings.get("reasoning_effort"):
            argv.extend(["--effort", settings["reasoning_effort"]])
        self._invoke("plan", argv, prompt, Path(cwd), out, timeout, _claude_environment(),
                     planning_provider="claude", planning_model=settings.get("model"),
                     planning_reasoning_effort=settings.get("reasoning_effort"))
        recovered = self.recover("plan", out)
        if recovered is None:
            raise ProviderError("Claude planning completed without a durable structured result")
        _atomic_json(out / "result.json", recovered)
        return recovered

    def art_diagnose(self, prompt: str, cwd: Path, out: Path, timeout: float, *, session_id: str) -> dict:
        """Fork the production conversation for diagnosis without editing it or the workspace."""
        cwd, out = Path(cwd).absolute(), Path(out).absolute()
        directories, binding = _claude_art_diagnosis_context(self.config, cwd, out, session_id)
        if any((out / name).exists() for name in ("stdout.log", "stderr.log", "process.json", "result.json", "final.json")):
            raise ProviderError("Invocation directory is already used; recover it or allocate a new attempt")
        settings = dict(self.config.get("claude", {}))
        settings.update(model=ART_CODING_MODEL, reasoning_effort=ART_CODING_REASONING_EFFORT)
        if not claude_subscription_ready(settings, cwd):
            raise ProviderError("Claude art diagnosis requires an active native Claude Pro login")
        prompt += (
            "\n这是原制作会话的只读诊断分支，不能修改文件、执行命令、制作资产、提交或批准验收。"
            "仅结合当前任务证据，说明失败原因及你在现有艺术方向、预算、批次、工具和审批权限内的下一步计划；"
            "需要改变边界时返回 needs_user 并明确授权或问题，不把自己的造型偏好升级为用户要求。"
            "continue 只表示计划可在现有授权内继续，不表示制作成功或技术/视觉验收通过。"
            "不得读取私有账本、密钥或其他任务。公开证据目录：" + directories[1].as_posix() +
            "；本任务记录目录：" + directories[2].as_posix() +
            "。head_sha 必须是 " + binding["head_sha"] + "。只返回指定 JSON 对象。\n"
        )
        out.mkdir(parents=True, exist_ok=True)
        _atomic_json(out / "schema.json", ART_DIAGNOSE_SCHEMA)
        argv = _claude_command(settings, read_only_dirs=directories)
        argv.extend(["--print", "--output-format", "stream-json", "--verbose",
                     "--permission-mode", "plan", "--permission-prompts", "none",
                     "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                     "--tools", "Read,Glob,Grep", "--allowedTools", "Read", "Glob", "Grep",
                     "--disallowedTools", "mcp__*", "Bash", "Edit", "Write",
                     "--resume", session_id, "--fork-session",
                     "--model", ART_CODING_MODEL, "--effort", ART_CODING_REASONING_EFFORT,
                     "--json-schema", json.dumps(ART_DIAGNOSE_SCHEMA, separators=(",", ":"))])
        self._invoke("art_diagnose", argv, prompt, cwd, out, timeout, _claude_environment(),
                     requested_session=session_id, diagnosis_binding=binding,
                     strict_model_selection=True)
        recovered = self.recover("art_diagnose", out)
        if recovered is None:
            raise ProviderError("Claude art diagnosis completed without a durable structured result")
        _atomic_json(out / "result.json", recovered)
        return recovered

    def _claude_code(self, prompt: str, cwd: Path, out: Path, timeout: float,
                     session_id: str | None) -> dict:
        settings = dict(self.config.get("claude", {}))
        repair_context = _claude_repair_context(self.config, Path(cwd), Path(out), session_id)
        followup_context = _claude_followup_context(self.config, Path(cwd), Path(out), session_id, repair_context)
        art_context = _managed_art_task(self.config, Path(cwd), Path(out), "code")
        if art_context is not None:
            settings.update(model=ART_CODING_MODEL, reasoning_effort=ART_CODING_REASONING_EFFORT)
        try:
            validate_model_settings(settings, "claude")
        except ValueError as error:
            raise ProviderError(str(error)) from error
        if session_id is not None and (not isinstance(session_id, str) or not _CLAUDE_SESSION.fullmatch(session_id)):
            raise ProviderError("Invalid persisted Claude Code session identity")
        if settings.get("instructions"):
            prompt += "\n" + str(settings["instructions"])
        validation_directory = _claude_validation_read_directory(self.config, Path(cwd), Path(out))
        out = Path(out).resolve()
        preflight, instructions, directories = _claude_preflight(self.config, Path(cwd), out)
        prompt += instructions
        art_commands, art_instructions, art_directories = _claude_art_preflight(self.config, Path(cwd), out)
        preflight.extend(art_commands)
        prompt += art_instructions
        directories.extend(art_directories)
        if repair_context is not None:
            repair_commands, repair_instructions, repair_directories = _claude_repair_preflight(
                self.config, Path(cwd), out, repair_context)
            preflight.extend(repair_commands)
            prompt += repair_instructions
            directories.extend(repair_directories)
        mcp_argument, mcp_tools, followup_binding = '{"mcpServers":{}}', [], None
        if followup_context is not None:
            extra_commands, extra_instructions, extra_directories, selection, mcp_argument = _claude_followup_preflight(
                self.config, Path(cwd), out, followup_context)
            preflight.extend(extra_commands); prompt += extra_instructions; directories.extend(extra_directories)
            if selection["server_name"]:
                mcp_tools = ["mcp__" + selection["server_name"] + "__*"]
            followup_binding = {**followup_context, "user_followup_mcp": selection}
        if validation_directory is not None:
            directories.append(validation_directory)
            prompt += (
                "\n本任务外层独立验证日志可只读查看，路径为 " + validation_directory.as_posix() +
                "。可结合工作区 Saved/Automation/" + validation_directory.parent.name +
                "/validation-results.json 摘要定位失败。仅据本轮实际日志修复，验证回执和外层日志由调度器维护。\n"
            )
        prompt += "\n交付摘要只写变更要点、提交号、验证结果及证据路径。不要粘贴源码或完整命令输出。"
        if not claude_subscription_ready(settings, cwd):
            raise ProviderError("Claude Code requires an active native Claude Pro login")
        working_directory = Path(cwd).resolve()
        external = [path for path in dict.fromkeys(path.resolve() for path in directories)
                    if not path.is_relative_to(working_directory)]
        argv = _claude_command(settings, read_only_dirs=external)
        art_edit_rules = []
        if art_commands:
            from unattended.art_pipeline import blender_job_file_path, recipe_file_path
            batch = _json_file(Path(self.config["art_pipeline"]["config_file"]))
            for path in (recipe_file_path(batch, workspace=cwd), blender_job_file_path(batch, workspace=cwd)):
                if path is not None:
                    pattern = path.as_posix()
                    if path.drive:
                        pattern = "/" + path.drive[0].lower() + pattern[2:]
                    # Claude's Edit permission covers both Edit and Write tools.
                    art_edit_rules.append(f"Edit(/{pattern})")
        git_commands = () if art_commands else _CLAUDE_GIT_COMMANDS
        argv.extend(["--print", "--output-format", "stream-json", "--verbose",
                     "--permission-mode", "default" if art_commands else "acceptEdits", "--permission-prompts", "none",
                     "--strict-mcp-config", "--mcp-config", mcp_argument,
                     "--tools", "Bash,Read,Glob,Grep,Edit,Write",
                     "--disallowedTools", *([] if mcp_tools else ["mcp__*"]),
                     "Bash(git push *)", "Bash(git reset *)", "Bash(git clean *)",
                     "--allowedTools", *("Bash(git " + name + " *)" for name in git_commands),
                     *(["Read", "Glob", "Grep", *art_edit_rules] if art_commands else []),
                     *mcp_tools,
                     *("Bash(" + command + ")" for command in preflight)])
        if session_id:
            argv.extend(["--resume", session_id])
        if settings.get("model"):
            argv.extend(["--model", settings["model"]])
        if settings.get("reasoning_effort"):
            argv.extend(["--effort", settings["reasoning_effort"]])
        out.mkdir(parents=True, exist_ok=True)
        environment = _claude_environment()
        if repair_context is not None:
            environment.pop("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", None)
        self._invoke("code", argv, prompt, Path(cwd), out, timeout, environment,
                     requested_session=session_id, coding_provider="claude",
                     coding_model=settings.get("model"), coding_reasoning_effort=settings.get("reasoning_effort"),
                     strict_model_selection=art_context is not None or repair_context is not None,
                     repair_binding=repair_context[2] if repair_context is not None else None,
                     followup_binding=followup_binding)
        recovered = self.recover("code", out)
        if recovered is None:
            raise ProviderError("Claude Code completed without a durable result", session_id=read_session(out))
        _atomic_json(out / "result.json", recovered)
        return recovered

    def _invoke(self, kind: str, argv: list[str], prompt: str, cwd: Path, out: Path,
                timeout: float, environment: dict[str, str], requested_session: str | None = None,
                *, coding_provider: str | None = None, planning_provider: str | None = None,
                planning_model: str | None = None, planning_reasoning_effort: str | None = None,
                coding_model: str | None = None, coding_reasoning_effort: str | None = None,
                strict_model_selection: bool = False, review_read_only_dirs: list[str] | None = None,
                diagnosis_binding: dict | None = None,
                repair_binding: dict | None = None,
                followup_binding: dict | None = None,
                codex_auth_mode: str | None = None,
                codex_subscription_fallback: bool = False) -> None:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ProviderError("A nonempty provider prompt is required")
        # A reused invocation directory must never re-run an external action.
        if any((out / name).exists() for name in ("stdout.log", "stderr.log", "process.json", "result.json", "final.json")):
            raise ProviderError("Invocation directory is already used; recover it or allocate a new attempt")
        secrets = [environment[key] for key in ("CODEX_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")
                   if environment.get(key)]
        receipt_fields = {"coding_provider": coding_provider} if coding_provider else {}
        if coding_model is not None:
            receipt_fields["coding_model"] = coding_model
        if coding_reasoning_effort is not None:
            receipt_fields["coding_reasoning_effort"] = coding_reasoning_effort
        if strict_model_selection:
            receipt_fields["strict_model_selection"] = True
        if repair_binding is not None:
            if kind != "code" or coding_provider != "claude" or not strict_model_selection:
                raise ProviderError("Repair native protocol only applies to verified Claude CODE")
            receipt_fields.update(repair_binding)
        if followup_binding is not None:
            if repair_binding is None:
                raise ProviderError("UE follow-up requires its authorized repair native protocol")
            receipt_fields.update(followup_binding)
        if review_read_only_dirs is not None:
            receipt_fields["review_access"] = {"mode": "read-only", "artifact_directories": review_read_only_dirs,
                                               "command_network": False}
        if planning_provider is not None:
            receipt_fields["planning_provider"] = planning_provider
        if planning_model is not None:
            receipt_fields["planning_model"] = planning_model
        if planning_reasoning_effort is not None:
            receipt_fields["planning_reasoning_effort"] = planning_reasoning_effort
        diagnostic = kind == "art_diagnose"
        if diagnostic:
            if diagnosis_binding is None:
                raise ProviderError("Art diagnosis is missing its managed operation binding")
            receipt_fields.update(diagnosis_provider="claude", diagnosis_model=ART_CODING_MODEL,
                                  diagnosis_reasoning_effort=ART_CODING_REASONING_EFFORT, **diagnosis_binding)
        protocol_session = None if diagnostic else requested_session
        claude = _claude_receipt(kind, receipt_fields)
        if codex_auth_mode is not None:
            receipt_fields["codex_auth_mode"] = codex_auth_mode
        try:
            capture_limit = int(self.config.get("max_output_bytes", _DEFAULT_PROVIDER_OUTPUT_LIMIT))
            capture_options = {}
            if claude:
                capture_limit = self.config.get("claude", {}).get("max_output_bytes", capture_limit)
                receipt_fields["max_output_bytes"] = capture_limit
                capture_options["stdout_record_filter"] = _claude_log_record
            if codex_subscription_fallback:
                receipt_fields.update(codex_subscription_fallback=True, max_output_bytes=capture_limit)
            result = run_process(argv, cwd=cwd, output_dir=out, timeout_seconds=timeout,
                                 environment=environment, stdin=prompt.encode("utf-8"),
                                 max_output_bytes=capture_limit,
                                 secrets=secrets, **capture_options)
        except (OSError, RuntimeError, ValueError) as error:
            updating = (kind in {"plan", "review"} and isinstance(error, OSError) and
                        (getattr(error, "errno", None) == errno.ENOENT or getattr(error, "winerror", None) in {2, 3}) and
                        _bundled_codex_root(argv[0]) is not None and not Path(argv[0]).is_file())
            record = {
                "kind": kind, "exit_code": None, "timed_out": False,
                "output_limit_exceeded": False, "launch_or_capture_failed": True,
                "session_id": read_session(out, requested_session=protocol_session), "requested_session": requested_session,
                "launch_error": {"type": type(error).__name__, "errno": getattr(error, "errno", None),
                                 "winerror": getattr(error, "winerror", None)},
                "transient": updating,
                **receipt_fields,
            }
            if updating:
                record["retry_reason"] = "local_cli_unavailable"
            if diagnostic:
                record["diagnosis_session"] = record["session_id"]
            _atomic_json(out / "process.json", record)
            raise ProviderError("Provider launch or log capture failed", session_id=None if diagnostic else record["session_id"],
                                transient=updating, retry_reason="local_cli_unavailable" if updating else None) from error
        record = {"kind": kind, "exit_code": result.exit_code,
                  "timed_out": result.timed_out, "output_limit_exceeded": result.output_limit_exceeded,
                  "requested_session": requested_session,
                  "session_id": read_session(out, requested_session=protocol_session), "transient": False,
                  **receipt_fields}
        if diagnostic:
            record["diagnosis_session"] = record["session_id"]
        selection_model = ART_CODING_MODEL if diagnostic else coding_model
        selection_effort = ART_CODING_REASONING_EFFORT if diagnostic else coding_reasoning_effort
        repair_facts = None
        if diagnostic or (kind == "code" and coding_provider == "claude" and coding_model is not None):
            try:
                if repair_binding is not None:
                    repair_facts = _claude_repair_native(out, record)
                    record["session_id"], record["model_selection"] = repair_facts[0], repair_facts[4]
                else:
                    record["model_selection"] = _claude_model_evidence(out / "stdout.log", protocol_session,
                                                                        selection_model, selection_effort)
            except (OSError, ValueError, UnicodeError, RecursionError):
                record["model_selection"] = {"requested_model": selection_model,
                    "requested_reasoning_effort": selection_effort,
                    "reasoning_effort_source": "cli_argument", "observed_model": None,
                    "model_usage_models": [], "model_verified": False}
        followup_error = None
        if followup_binding is not None and repair_facts is not None and repair_facts[4]["model_verified"]:
            try:
                record["user_followup"] = _claude_followup_evidence(out, record)
            except (OSError, ValueError, TypeError, KeyError) as error:
                followup_error = error
                record["user_followup_validation_error"] = "Invalid native MCP evidence"
        _atomic_json(out / "process.json", record)
        if "user_followup" in record:
            _atomic_json(out / "ue-mcp-evidence.json", record["user_followup"])
        if diagnostic:
            _atomic_json(out / "model-selection.json", {**record["model_selection"], **diagnosis_binding,
                         "requested_session": requested_session, "diagnosis_session": record["diagnosis_session"]})
        elif repair_binding is not None:
            _atomic_json(out / "model-selection.json", {**record["model_selection"], **repair_binding,
                         "requested_session": requested_session, "session_id": record["session_id"]})
        error_session = None if diagnostic else record["session_id"]
        if result.timed_out:
            raise ProviderError("Provider exceeded its time limit", session_id=error_session, interrupted=True)
        if result.output_limit_exceeded:
            raise ProviderError("Provider exceeded its log limit", session_id=error_session)
        retry = classify_transient_failure(kind, out) if result.exit_code != 0 or claude else None
        if result.exit_code != 0 or retry:
            if retry:
                record.update(transient=True, retry_reason=retry["reason"])
                if "retry_after_seconds" in retry:
                    record["retry_after_seconds"] = retry["retry_after_seconds"]
                _atomic_json(out / "process.json", record)
            raise ProviderError("Provider exited unsuccessfully; see its local logs", session_id=error_session,
                                transient=bool(retry), retry_reason=retry["reason"] if retry else None,
                                retry_after_seconds=retry.get("retry_after_seconds") if retry else None)
        if strict_model_selection and not record.get("model_selection", {}).get("model_verified"):
            raise ProviderError("Claude pinned task did not confirm the required Opus 5.5 model in native receipts",
                                session_id=error_session)
        if repair_binding is not None and (repair_facts is None or repair_facts[2] or
                (repair_facts[3] and repair_facts[3].get("status") == "rejected")):
            raise ProviderError("Claude repair contains an unsuccessful native turn; see its local logs",
                                session_id=error_session)
        if followup_error is not None:
            raise ProviderError("Claude UE follow-up contains invalid native MCP evidence", session_id=error_session) from followup_error

    def recover(self, kind: str, out: Path) -> dict | None:
        """Validate existing process/protocol artifacts without issuing a request."""
        out = codex_result_directory(kind, out) if kind in {"plan", "review"} else Path(out).resolve()
        if not (out / "process.json").is_file():
            return None
        try:
            record = _json_file(out / "process.json")
            if record.get("kind") != kind:
                raise ProviderError("Saved provider stage differs from the requested stage")
            if (type(record.get("exit_code")) is not int or record["exit_code"] != 0 or record.get("timed_out") is not False or
                    record.get("output_limit_exceeded") is not False or record.get("launch_or_capture_failed")):
                return None
            if kind == "art_diagnose":
                if not (out / "stdout.log").is_file():
                    return None
                if (record.get("diagnosis_provider") != "claude" or
                        record.get("diagnosis_model") != ART_CODING_MODEL or
                        record.get("diagnosis_reasoning_effort") != ART_CODING_REASONING_EFFORT or
                        record.get("strict_model_selection") is not True):
                    raise ProviderError("Saved art diagnosis does not require the pinned Claude model")
                _, binding = _claude_art_diagnosis_context(self.config, Path(record["workspace"]), out,
                                                           record.get("requested_session"))
                if any(record.get(key) != value for key, value in binding.items()):
                    raise ProviderError("Saved art diagnosis differs from its current operation binding")
                branch = _claude_receipt_session(record)
                if record.get("session_id") != branch:
                    raise ProviderError("Saved art diagnosis has conflicting branch sessions")
                _, terminal, _, _ = _claude_turn(out / "stdout.log", branch)
                if terminal.get("subtype") != "success" or terminal["is_error"]:
                    return None
                if _json_file(out / "schema.json") != ART_DIAGNOSE_SCHEMA:
                    raise ProviderError("Saved art diagnosis has an invalid output schema")
                selection = _claude_model_evidence(out / "stdout.log", branch, ART_CODING_MODEL,
                                                    ART_CODING_REASONING_EFFORT)
                if not selection["model_verified"] or record.get("model_selection") != selection:
                    raise ProviderError("Saved art diagnosis does not verify the required model")
                # The sidecar is supplementary. A crash after process.json but before
                # this write remains recoverable from the complete native receipt.
                sidecar = out / "model-selection.json"
                if sidecar.exists() and _json_file(sidecar) != {
                        **selection, **binding, "requested_session": record["requested_session"],
                        "diagnosis_session": branch}:
                    raise ProviderError("Saved art diagnosis model evidence differs from its native receipt")
                result = validate_output(kind, terminal.get("structured_output"))
                if result["head_sha"] != binding["head_sha"]:
                    raise ProviderError("Art diagnosis output differs from its bound head")
                if (out / "result.json").exists() and _json_file(out / "result.json") != result:
                    raise ProviderError("Saved art diagnosis result differs from its native receipt")
            elif kind == "plan" and _claude_receipt(kind, record):
                if not (out / "stdout.log").is_file():
                    return None
                _, terminal, _, _ = _claude_turn(out / "stdout.log", record.get("requested_session"))
                if terminal.get("subtype") != "success" or terminal["is_error"]:
                    return None
                schema = _json_file(out / "schema.json")
                if schema not in (PLAN_SCHEMA, PLAN_WITH_CORE_TESTS_SCHEMA,
                                   TIERED_PLAN_SCHEMA, TIERED_PLAN_WITH_CORE_TESTS_SCHEMA,
                                   STRICT_PLAN_WITH_CORE_TESTS_SCHEMA, STRICT_TIERED_PLAN_WITH_CORE_TESTS_SCHEMA):
                    raise ProviderError("Claude planner receipt has an invalid output schema")
                value = terminal.get("structured_output")
                if schema in (PLAN_WITH_CORE_TESTS_SCHEMA, TIERED_PLAN_WITH_CORE_TESTS_SCHEMA):
                    value = _normalize_legacy_plan_core_tests(value)
                result = validate_output("plan", value)
                if set(result) != set(schema["required"]):
                    raise ProviderError("Claude planner output differs from its saved schema")
            elif kind in {"plan", "review"}:
                if not (out / "final.json").is_file() or not (out / "stdout.log").is_file():
                    return None
                parse_codex_events(out / "stdout.log", exit_code=record["exit_code"])
                result = validate_output(kind, _json_file(out / "final.json"))
            elif kind == "code":
                if not (out / "stdout.log").is_file():
                    return None
                if record.get("coding_provider") == "claude":
                    if record.get("native_protocol") is not None or record.get("user_repair_profile") is not None:
                        context = _claude_repair_context(self.config, Path(record["workspace"]), out,
                                                        record.get("requested_session"))
                        if context is None or any(record.get(key) != value for key, value in context[2].items()):
                            raise ProviderError("Saved repair receipt differs from its managed operation")
                        session, terminal, errors, rate, selection = _claude_repair_native(out, record)
                        if (errors or (rate and rate.get("status") == "rejected") or
                                not selection["model_verified"] or record.get("model_selection") != selection or
                                record.get("session_id") != session):
                            raise ProviderError("Saved repair native evidence is unsuccessful or mismatched")
                        sidecar = out / "model-selection.json"
                        if sidecar.exists() and _json_file(sidecar) != {
                                **selection, **context[2], "requested_session": record["requested_session"],
                                "session_id": session}:
                            raise ProviderError("Saved repair model evidence differs from its native receipt")
                        result = {"session_id": session, "text": terminal["result"], "completed": True,
                                  "model_selection": selection}
                        if record.get("user_followup_profile") is not None or self.config.get("_user_followup_profile") is not None:
                            followup = _claude_followup_context(self.config, Path(record["workspace"]), out,
                                                              record.get("requested_session"), context)
                            if followup is None or any(record.get(key) != value for key, value in followup.items()):
                                raise ProviderError("Saved UE follow-up differs from the authorized turn")
                            proof = _claude_followup_evidence(out, record)
                            if record.get("user_followup") != proof:
                                raise ProviderError("Saved UE MCP proof differs from native calls")
                            proof_path = out / "ue-mcp-evidence.json"
                            if proof_path.exists() and _json_file(proof_path) != proof:
                                raise ProviderError("Saved UE MCP sidecar differs from native calls")
                            result["user_followup"] = proof
                        if (out / "result.json").exists() and _json_file(out / "result.json") != result:
                            raise ProviderError("Saved repair result differs from its native receipt")
                        return result
                    try:
                        session, terminal, _, _ = _claude_turn(out / "stdout.log", record.get("requested_session"))
                    except (OSError, UnicodeError, ValueError, RecursionError):
                        return None
                    if (terminal.get("subtype") != "success" or terminal["is_error"] or
                            not isinstance(terminal.get("result"), str)):
                        return None
                    result = {"session_id": session, "text": terminal["result"], "completed": True}
                    if record.get("coding_model") is not None:
                        selection = _claude_model_evidence(out / "stdout.log", record.get("requested_session"),
                                                          record["coding_model"], record.get("coding_reasoning_effort"))
                        if record.get("strict_model_selection") and not selection["model_verified"]:
                            raise ProviderError("Saved Claude art receipt does not verify the required model")
                        result["model_selection"] = selection
                    return result
                # Existing parsers consume paths. Use disposable scratch space so
                # recovery never modifies the saved operation or its timestamps.
                with tempfile.TemporaryDirectory(prefix="dsh-protocol-") as scratch:
                    normalized = Path(scratch) / "events.jsonl"
                    _normalize_dsh(out / "stdout.log", normalized)
                    settings = self.config.get("dsh", {})
                    parser = parse_legacy_events if settings.get("verified_version", VERIFIED_VERSION) == VERIFIED_VERSION else parse_events
                    facts = parser(normalized, exit_code=record["exit_code"],
                                   requested_session=record.get("requested_session"))
                    events = [_strict_json(line) for line in normalized.read_text(encoding="utf-8").splitlines() if line.strip()]
                    result = {"session_id": facts["session_id"], "text": events[-1]["text"], "completed": True}
            else:
                raise ProviderError("Unknown provider stage")
            return result
        except ProviderError:
            raise
        except (OSError, UnicodeError, ValueError, KeyError, AttributeError, RuntimeError) as error:
            raise ProviderError("Saved provider output is missing, invalid, or incomplete",
                                session_id=None if kind == "art_diagnose" else read_session(out)) from error

    def doctor(self) -> dict:
        """Read-only executable/overlay inventory; never calls a model or reads auth."""
        codex_unavailable = False
        try:
            codex = _codex_command(self.config.get("codex", {}).get("command"))
        except ProviderError as error:
            if not error.transient:
                raise
            codex = _command(self.config.get("codex", {}).get("command"), ["codex"])
            codex_unavailable = True
        dsh = self.config.get("dsh", {})
        command = dsh.get("command")
        if command:
            dsh_command = _command(command, [])
            dsh_available = bool(shutil.which(dsh_command[0]) or Path(dsh_command[0]).is_file())
        else:
            install = Path(dsh.get("desktop_install", "")).expanduser().resolve()
            dsh_command = [str(install / "DeepSeek Harness.exe")]
            dsh_available = (install / "DeepSeek Harness.exe").is_file() and (install / "resources/app.asar").is_file()
        return {
            "codex_available": bool(not codex_unavailable and (shutil.which(codex[0]) or Path(codex[0]).is_file())),
            "codex_command": codex,
            "dsh_available": dsh_available, "dsh_command": dsh_command,
            "dsh_home_exists": not dsh.get("home") or Path(dsh["home"]).expanduser().is_dir(),
            "dsh_patches_exist": all(Path(raw).expanduser().is_file() for raw in dsh.get("patches", [])),
            "authentication": "not_checked", "sends_model_request": False,
        }
