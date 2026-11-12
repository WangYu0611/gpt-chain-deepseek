"""Read-only parser for the explicitly authorized full-flow background CLI.

Background task notifications can open their own native init/result turns before
or after the foreground turn. This does not alter the normal worker protocol or
infer test completion; the operator must still verify mandatory stage receipts.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import ue_checks
from .providers import _CLAUDE_SESSION, _SESSION, _strict_json

MAX_STDOUT_BYTES = 64 * 1024 * 1024
MODEL = "claude-opus-5-5"
_BETWEEN = {"ui_invalidate", "task_notification", "task_updated", "background_tasks_changed", "task_started"}


def parse_full_flow_native(path: Path, requested_session: str | None,
                          requested_model: str = MODEL, effort: str = "high") -> tuple[str, dict, list[str], dict | None, dict]:
    """Return the final verified answer without dropping its native origin.

    Indices in the parser audit are zero-based; native result_index is retained
    separately. Raw stdout is never rewritten or stripped into a fake turn.
    """
    if requested_model != MODEL or effort != "high":
        raise ValueError("Full-flow native parsing requires the authorized Opus 5.5/high selection")
    if requested_session is not None and (not isinstance(requested_session, str) or not _CLAUDE_SESSION.fullmatch(requested_session)):
        raise ValueError("Invalid requested full-flow native session")
    path = Path(path).absolute()
    path = ue_checks._safe_under(Path(path.anchor), Path(*path.parts[1:]))
    before = path.stat()
    if not path.is_file() or not 0 < before.st_size <= MAX_STDOUT_BYTES:
        raise ValueError("Full-flow native output is empty or oversized")
    raw = path.read_bytes()
    after = path.stat()
    if (len(raw) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ino) !=
            (after.st_size, after.st_mtime_ns, after.st_ino)):
        raise ValueError("Full-flow native output changed while it was read")
    session, active, selected = None, None, None
    turns, errors, rates = [], [], []
    pending, notification_count, event_count = False, 0, 0
    native_index, run_ids = -1, set()
    for line_number, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        event = _strict_json(line)
        if type(event) is not dict or not isinstance(event.get("type"), str):
            raise ValueError("Invalid full-flow native event")
        identity = event.get("session_id")
        if (not isinstance(identity, str) or not _CLAUDE_SESSION.fullmatch(identity) or
                (requested_session is not None and identity != requested_session) or
                (session is not None and identity != session)):
            raise ValueError("Conflicting full-flow native session")
        session = identity
        event_count += 1
        kind, subtype = event["type"], event.get("subtype")
        if kind == "system" and subtype == "init":
            if active is not None:
                raise ValueError("Full-flow native init preceded an unfinished turn")
            observed = event.get("model")
            if not isinstance(observed, str) or not _SESSION.fullmatch(observed):
                raise ValueError("Missing native init model")
            active = {"index": len(turns), "init_line": line_number, "observed_model": observed}
            pending = False
            continue
        if active is None:
            if kind == "system" and subtype in _BETWEEN:
                notification_count += subtype == "task_notification"
                pending = True
                continue
            raise ValueError("Full-flow native event outside an initialized turn")
        if kind == "result":
            if type(event.get("is_error")) is not bool:
                raise ValueError("Invalid full-flow native terminal error flag")
            origin = event.get("origin")
            if origin is not None:
                if (type(origin) is not dict or origin.get("kind") != "task-notification" or
                        set(origin) - {"kind", "producer", "runId"} or
                        ("producer" in origin and origin["producer"] != "session-task")):
                    raise ValueError("Unsupported full-flow native result origin")
                if "runId" in origin:
                    run_id = origin["runId"]
                    if not isinstance(run_id, str) or not _SESSION.fullmatch(run_id) or run_id in run_ids:
                        raise ValueError("Invalid or duplicate full-flow notification run identity")
                    run_ids.add(run_id)
            if "result_index" in event:
                value = event["result_index"]
                if type(value) is not int or value < 0 or value <= native_index:
                    raise ValueError("Invalid full-flow native result order")
                native_index = value
            answer = event.get("result")
            if answer is not None and not isinstance(answer, str):
                raise ValueError("Invalid full-flow native answer type")
            usage = event.get("modelUsage")
            models = sorted(usage) if type(usage) is dict and all(isinstance(key, str) and _SESSION.fullmatch(key) for key in usage) else []
            verified = active["observed_model"] == requested_model and requested_model in models
            turn = {**active, "result_line": line_number, "native_result_index": event.get("result_index"),
                    "origin": origin, "is_error": event["is_error"], "subtype": subtype,
                    "answer_present": bool(answer and answer.strip()), "model_usage_models": models,
                    "model_verified": verified}
            turns.append(turn)
            if event["is_error"] or subtype != "success":
                errors.append("native_terminal_error")
            if not event["is_error"] and subtype == "success" and turn["answer_present"] and verified:
                selected = (event, turn)
            active = None
            continue
        if kind == "assistant":
            if event.get("parent_tool_use_id") is None and "error" in event:
                if not isinstance(event["error"], str):
                    raise ValueError("Invalid full-flow native API error")
                errors.append(event["error"])
        elif kind == "rate_limit_event":
            info = event.get("rate_limit_info")
            if type(info) is not dict or info.get("status") not in {"allowed", "allowed_warning", "rejected"}:
                raise ValueError("Invalid full-flow native rate limit")
            rates.append(info)
        elif kind == "system":
            if not isinstance(subtype, str) or not _SESSION.fullmatch(subtype):
                raise ValueError("Invalid full-flow native system event")
            notification_count += subtype == "task_notification"
        elif kind == "tool_progress":
            # _claude_log_record retains only this identifying envelope from
            # SDKToolProgressMessage. It is liveness, never a tool result or a
            # completed turn; the initialized-turn/session gates above apply.
            parent = event.get("parent_tool_use_id")
            identity = event.get("uuid")
            if (set(event) != {"type", "session_id", "uuid", "parent_tool_use_id"} or
                    not isinstance(identity, str) or not _CLAUDE_SESSION.fullmatch(identity) or
                    (parent is not None and (not isinstance(parent, str) or not _SESSION.fullmatch(parent)))):
                raise ValueError("Invalid sanitized full-flow native tool progress")
        elif kind != "user":
            raise ValueError("Unsupported full-flow native event type")
    if active is not None or pending or not turns or selected is None:
        raise ValueError("Incomplete full-flow native answer stream")
    terminal, selection = selected
    # An earlier answer must not hide a later error, wrong-model turn, or quota
    # rejection. The caller checks errors, rate and model_verified as usual.
    rate = next((value for value in reversed(rates) if value["status"] == "rejected"), rates[-1] if rates else None)
    model = {"requested_model": requested_model, "requested_reasoning_effort": effort,
             "reasoning_effort_source": "cli_argument", "observed_model": selection["observed_model"],
             "model_usage_models": selection["model_usage_models"],
             "model_verified": all(turn["model_verified"] for turn in turns),
             "parser": "full_flow_native_v1", "raw_stdout_sha256": hashlib.sha256(raw).hexdigest(),
             "raw_stdout_size_bytes": len(raw), "event_count": event_count, "turn_count": len(turns),
             "notification_count": notification_count, "selected_turn_index": selection["index"],
             "selected_result_index": selection["native_result_index"], "selected_result_line": selection["result_line"],
             "selected_origin": selection["origin"], "native_turns": turns}
    return session, terminal, errors, rate, model
