"""Read-only, bounded dashboard projections; never advance the controller."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import stat

from desktop_background import process_identity
from .background import OWNER
from .calendar import BEIJING, WorkCalendar
from .config import load_config
from .providers import _CODEX_SUBSCRIPTION_DIR, _codex_api_quota_failed

TAIL_BYTES = 128 * 1024
MAX_ACTIVITY = 40
MAX_JSON_BYTES = 4 * 1024 * 1024
_RETRY_REASONS = {"rate_limited": "服务请求暂时受限", "server_unavailable": "模型服务暂时不可用",
                  "transport": "网络连接暂时异常", "workspace_routing_network": "工作区服务连接暂时异常",
                  "local_cli_unavailable": "本地 Codex 正在更新或暂不可用",
                  "claude_subscription_limit": "Claude Pro 额度已用完",
                  "codex_subscription_limit": "Codex额度已用完"}
SESSION_HEADER_BYTES = 8 * 1024
CLAUDE_SESSION_HEADER_BYTES = 64 * 1024
_SESSION_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
_CLAUDE_SESSION_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_CODING_NAMES = {"dsh": "DSH", "claude": "Claude Code"}
_CLAUDE_TOOLS = {
    "Read": "读取文件", "Edit": "修改文件", "MultiEdit": "修改文件", "Write": "写入文件",
    "NotebookEdit": "修改笔记本", "Bash": "执行命令", "PowerShell": "执行命令",
    "Glob": "查找文件", "Grep": "搜索内容", "Agent": "委派子任务",
}
_PHASES = {"PLAN": ("规划任务", "Codex"), "REPLAN": ("Claude重新分析修复计划", "Claude Code"), "CODE": ("编写代码", "DSH"),
           "CHECK": ("独立检查", "UE/CHECK"), "REVIEW": ("验收审阅", "Codex"),
           "PUBLISH": ("交付任务", "系统"), "MILESTONE_REVIEW": ("阶段验收", "Codex"),
           "MILESTONE_PUBLISH": ("发布里程碑", "系统"), "BLOCKED": ("需要处理", "系统"),
           "DONE": ("目标已完成", "系统")}
_FULL_FLOW_NATIVE_PHASES = ("plan", "test", "analysis", *(f"analysis-{number}" for number in range(2, 10)))
_FULL_FLOW_RECORD_BYTES = 16 * 1024 * 1024
_FULL_FLOW_MANIFEST_BYTES = 32 * 1024 * 1024


def _now():
    return datetime.now(timezone.utc)


def _clean(value, limit=600):
    """Only project selected prose fields; suppress embedded reasoning and secrets."""
    if not isinstance(value, str):
        return ""
    value = re.sub(r"(?is)<(?:think|thinking|analysis)\b[^>]*>.*?(?:</(?:think|thinking|analysis)>|$)", "", value)
    value = re.sub(r"(?i)\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]+)", "[已隐藏凭据]", value)
    value = re.sub(r"(?i)(\b(?:[A-Za-z0-9_]*_)?(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|token)\b[\"']?\s*[:=]\s*)(?:Bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", r"\1[已隐藏凭据]", value)
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [已隐藏凭据]", value)
    value = re.sub(r"(https?://)[^\s/@:]+:[^\s/@]+@", r"\1[已隐藏凭据]@", value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value).strip()
    return value if len(value) <= limit else value[:limit] + "…"


def _prose(value):
    if not isinstance(value, str):
        return ""
    stripped = value.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped)
    if stripped.startswith(("{", "[")):
        try:
            result = json.loads(stripped)
        except (ValueError, RecursionError):
            return "结构化结果正在输出"
        if isinstance(result, dict):
            return _clean(next((result[key] for key in ("summary", "title", "reason", "feedback")
                               if isinstance(result.get(key), str) and result[key].strip()), "结构化结果已生成"))
        return "结构化结果已生成"
    return _clean(value)


def _linked(path):
    try:
        return path.is_symlink() or bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except AttributeError:
        return False
    except FileNotFoundError:
        return False


def _confined(root: Path, candidate: Path) -> Path:
    """Check lexical ancestry AND every existing reparse component before resolve."""
    root = Path(os.path.abspath(root))
    candidate = Path(os.path.abspath(candidate))
    if candidate == root or not candidate.is_relative_to(root):
        raise ValueError("运行日志路径不在受管目录内")
    current = root
    if _linked(current):
        raise ValueError("运行目录不能为符号链接或 junction")
    for part in candidate.relative_to(root).parts:
        current /= part
        if _linked(current):
            raise ValueError("运行文件路径包含符号链接或 junction")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("运行文件路径越界")
    return resolved


def _object(path, *, missing=None, maximum=MAX_JSON_BYTES):
    try:
        with path.open("rb") as stream:
            raw = stream.read(maximum + 1)
    except FileNotFoundError:
        return missing
    if len(raw) > maximum:
        raise ValueError("运行 JSON 文件超过读取上限")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("运行 JSON 必须为对象")
    return value


def _tail(path):
    """Read a fixed byte tail, excluding truncated first/last protocol records."""
    with path.open("rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        start = max(0, size - TAIL_BYTES)
        stream.seek(start)
        raw = stream.read(TAIL_BYTES)
        modified = os.fstat(stream.fileno()).st_mtime
    if start:
        raw = raw.partition(b"\n")[2]
    # A live writer may have flushed only a prefix of its next JSON record.
    if raw and not raw.endswith(b"\n"):
        raw = raw.rpartition(b"\n")[0]
    return raw.decode("utf-8-sig", errors="replace").splitlines(), modified


def _iso(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.isoformat() if parsed.tzinfo else None
    except ValueError:
        return None


def _age(value, now):
    parsed = _iso(value)
    return max(0, (now - datetime.fromisoformat(parsed)).total_seconds()) if parsed else None


def _number(value, default=0):
    return value if type(value) in (int, float) and math.isfinite(value) else default


def _worker(runtime, config_path, now):
    health = _object(_confined(runtime, runtime / "background-health.json"), missing={}, maximum=65536)
    valid = (health.get("owner") == OWNER and isinstance(health.get("config"), str)
             and os.path.normcase(health["config"]) == os.path.normcase(str(config_path)))
    alive = False
    if valid and type(health.get("pid")) is int and health["pid"] > 0:
        try:
            actual = process_identity(health["pid"])
            alive = all(actual[key] == health[key] for key in ("pid", "process_created", "image"))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    epoch = _number(health.get("heartbeat_epoch"), None) if valid else None
    age = max(0, now.timestamp() - epoch) if epoch is not None else None
    recovery = None
    record = health.get("recovery") if valid and health.get("status") == "recovering" else None
    if (isinstance(record, dict) and record.get("kind") == "filesystem_sharing" and
            type(record.get("attempt")) is int and 1 <= record["attempt"] <= 1_000_000_000 and
            _iso(record.get("next_retry_at")) is not None):
        recovery = {"kind": "filesystem_sharing", "attempt": record["attempt"],
                    "next_retry_at": _iso(record["next_retry_at"])}
    healthy = bool(alive and age is not None and age < 35 and
                   (health.get("status") == "running" or (health.get("status") == "recovering" and recovery)))
    return {"alive": alive, "healthy": healthy, "pid": health.get("pid") if valid else None,
            "heartbeat_age_seconds": age, "recovery": recovery,
            "label": "后台等待状态文件恢复" if healthy and recovery else
                     "后台正常" if healthy else "心跳待核实" if alive else "后台已停止"}


def _tool_summary(event):
    name = event.get("name", event.get("toolName", event.get("tool", "工具")))
    name = name if isinstance(name, str) and re.fullmatch(r"[\w./:-]{1,80}", name) else "工具"
    arguments = event.get("input", event.get("arguments", {}))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if len(arguments) <= 65536 else {}
        except (ValueError, RecursionError):
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    description = _clean(arguments.get("description"), 160)
    tool = re.split(r"[./:]", name)[-1].lower()
    action = {"read": "读取文件", "read_file": "读取文件", "readfile": "读取文件",
              "edit": "修改文件", "edit_file": "修改文件", "editfile": "修改文件",
              "write": "写入文件", "write_file": "写入文件", "writefile": "写入文件"}.get(tool)
    file_path = arguments.get("file_path")
    filename = ""
    if action and isinstance(file_path, str) and len(file_path) <= 4096 and "\0" not in file_path:
        # PureWindowsPath understands both slash styles on every supported host.
        filename = _clean(PureWindowsPath(file_path).name, 120).replace("\n", " ").replace("\r", " ")
    text = action + "：" + filename if filename else "调用工具：" + _clean(name, 80)
    return text + (" · " + description if description else "")


def _claude_event(event):
    """Keep only fixed activity labels, never project Claude event payloads."""
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        return "开始本轮执行"
    if kind == "assistant" and isinstance(event.get("message"), dict):
        content = event["message"].get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name")
                    label = _CLAUDE_TOOLS.get(name) if isinstance(name, str) else None
                    return "工具调用 · " + label if label else "调用工具"
    if kind == "user" and isinstance(event.get("message"), dict):
        content = event["message"].get("content")
        if isinstance(content, list) and any(isinstance(block, dict) and block.get("type") == "tool_result"
                                             for block in content):
            return "收到工具结果"
    if kind == "result":
        return "本轮执行结束" if event.get("subtype") == "success" and event.get("is_error") is False else "本轮执行失败"
    return ""


def _event(event, actor):
    if not isinstance(event, dict) or str(event.get("channel", "")).lower() in {"analysis", "thinking", "reasoning"}:
        return None
    kind, text = event.get("type"), ""
    if not isinstance(kind, str):
        return None
    if actor == _CODING_NAMES["claude"]:
        text = _claude_event(event)
        return {"at": _iso(event.get("timestamp", event.get("at"))), "actor": actor,
                "kind": kind, "text": text} if text else None
    item = event.get("item")
    if kind in {"item.started", "item.updated", "item.completed"} and isinstance(item, dict):
        if item.get("channel") in {"analysis", "thinking", "reasoning"}:
            return None
        if item.get("type") == "agent_message":
            text = _prose(item.get("text"))
        elif item.get("type") == "command_execution":
            # Arguments/output can contain secrets, source, or full protocol dumps.
            text = "执行命令" + (" · 已结束" if kind == "item.completed" else " · 进行中")
            if type(item.get("exit_code")) is int:
                text += " · 退出码 " + str(item["exit_code"])
        else:
            return None
    elif kind == "text":
        text = _prose(event.get("text", event.get("content")))
    elif kind == "tool_call":
        text = _tool_summary(event)
    elif kind == "status":
        phase = event.get("phase")
        text = ({"turn_start": "开始本轮执行", "turn_end": "本轮执行结束"}.get(phase, "")
                if isinstance(phase, str) else "")
    elif kind in {"turn.started", "turn.completed", "turn.failed"}:
        text = {"turn.started": "开始本轮执行", "turn.completed": "本轮执行结束", "turn.failed": "本轮执行失败"}[kind]
    if not text:
        return None
    return {"at": _iso(event.get("timestamp", event.get("at"))), "actor": actor,
            "kind": "command" if isinstance(item, dict) and item.get("type") == "command_execution" else str(kind), "text": text}


def _operation_directory(runtime, operation):
    if not operation:
        return None
    raw = operation.get("output_dir")
    if not isinstance(raw, str):
        raise ValueError("操作日志目录无效")
    directory = _confined(runtime, Path(raw))
    _confined(runtime / "runs", directory)
    return directory


def _codex_turn_started(stdout):
    with stdout.open("rb") as stream:
        header = stream.read(SESSION_HEADER_BYTES)
    records = (json.loads(line.decode("utf-8-sig")) for line in header.split(b"\n")[:-1] if line.strip())
    opened = next(records, None)
    if not isinstance(opened, dict) or opened.get("type") != "thread.started":
        return False
    thread_id = opened.get("thread_id")
    if not isinstance(thread_id, str) or not _SESSION_ID.fullmatch(thread_id):
        return False
    turn = next(records, None)
    return isinstance(turn, dict) and turn.get("type") == "turn.started"


def _codex_route(runtime, directory, kind):
    """Select the active transcript only after confirming its authentication route."""
    try:
        api = _object(_confined(runtime, directory / "process.json"), missing={}, maximum=16384)
    except (OSError, ValueError, RuntimeError):
        return None, directory
    if api.get("kind") != kind or api.get("codex_auth_mode") != "api_key" or type(api.get("exit_code")) is not int:
        return None, directory
    try:
        child = _confined(runtime, directory / _CODEX_SUBSCRIPTION_DIR)
        if api.get("codex_subscription_fallback") is True and child.is_dir():
            receipt = _object(_confined(runtime, child / "process.json"), missing=None, maximum=16384)
            stdout = _confined(runtime, child / "stdout.log")
            confirmed = (_codex_turn_started(stdout) if receipt is None else
                         receipt.get("kind") == kind and receipt.get("codex_auth_mode") == "chatgpt"
                         and type(receipt.get("exit_code")) is int)
            _confined(runtime, directory / "stdout.log")
            if confirmed and _codex_api_quota_failed(kind, directory):
                return "chatgpt", child
    except (OSError, ValueError, RuntimeError):
        pass
    return "api_key", directory


def _attempt_finished_at(runtime, directory, operation, check_id, started_at, updated_at):
    """Use the receipt from the operation shown in the activity panel."""
    if directory is None or started_at is None:
        return None
    kind = operation.get("kind")
    checking = kind in {"check", "milestone_check"} and check_id is not None
    if not checking and kind not in {"plan", "code", "review"}:
        return None
    try:
        path = _confined(runtime, directory / ("check.json" if checking else "process.json"))
        receipt = _object(path, missing={}, maximum=65536 if checking else 16384)
        if checking:
            if (receipt.get("id") != check_id or not operation.get("head_sha") or
                    receipt.get("head_sha") != operation["head_sha"] or type(receipt.get("passed")) is not bool):
                return None
        elif receipt.get("kind") != kind or (
                type(receipt.get("exit_code")) is not int and receipt.get("launch_or_capture_failed") is not True):
            return None
        finished = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if finished < datetime.fromisoformat(started_at):
            return None
        # An API quota receipt can appear before the child transcript starts.
        # Keep the operation open until fallback starts or the worker persists
        # its outcome. The caller passes the child directory once confirmed.
        checkpoint = _iso(updated_at)
        if (receipt.get("codex_auth_mode") == "api_key" and
                receipt.get("codex_subscription_fallback") is True and
                (checkpoint is None or datetime.fromisoformat(checkpoint) < finished) and
                _codex_api_quota_failed(kind, directory)):
            return None
        return finished
    except (OSError, ValueError, RuntimeError, RecursionError):
        return None


def _claude_code_opening(header, session, *, allow_notifications=False):
    """Inspect a bounded opening, never use a background result as completion."""
    between = {"ui_invalidate", "task_notification", "task_updated", "background_tasks_changed", "task_started"}
    observed, active, notification, foreground_seen = None, False, False, False
    for raw in header.split(b"\n")[:-1]:
        if not raw.strip():
            continue
        event = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(event, dict) or event.get("session_id") != session:
            return None
        kind, subtype = event.get("type"), event.get("subtype")
        if kind == "system" and subtype == "init":
            model = event.get("model")
            if (active or not isinstance(model, str) or not re.fullmatch(r"claude-(?:sonnet|opus|haiku)-[A-Za-z0-9.-]{1,95}", model) or
                    observed is not None and model != observed):
                return None
            observed, active = model, True
            foreground_seen = foreground_seen or not notification
            if not allow_notifications:
                return observed
        elif not allow_notifications:
            return None
        elif not active:
            if kind != "system" or subtype not in between:
                return None
            notification = notification or subtype == "task_notification"
        elif kind == "result":
            origin = event.get("origin")
            if (event.get("is_error") is not False or subtype != "success" or
                    origin is not None and (not isinstance(origin, dict) or origin.get("kind") != "task-notification" or
                        set(origin) - {"kind", "producer", "runId"} or origin.get("producer", "session-task") != "session-task")):
                return None
            active, notification = False, False
        elif kind not in {"system", "assistant", "user", "tool_progress", "rate_limit_event"}:
            return None
    return observed if foreground_seen else None


def _native_code_active(runtime, task, operation, directory):
    """Confirm this task's current public opening and absence of a terminal receipt."""
    if (not isinstance(task, dict) or operation.get("kind") != "code" or operation.get("coding_provider") != "claude" or
            operation.get("task_id") != task.get("id") or not _iso(operation.get("started_at")) or directory is None):
        return False
    try:
        directory = _confined(runtime, directory)
        if directory.parent != runtime / "runs" or not re.fullmatch(r"code-[a-f0-9]{32}", directory.name):
            return False
        head = operation.get("head_sha")
        if not isinstance(head, str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", head):
            return False
        if head not in {task.get("code_base"), (task.get("candidate") or {}).get("head_sha"), task.get("base_sha")}:
            return False
        receipt = _object(_confined(runtime, directory / "process.json"), missing={}, maximum=65536)
        if receipt and (receipt.get("kind") != "code" or type(receipt.get("exit_code")) is int or
                        receipt.get("launch_or_capture_failed") is True or receipt.get("timed_out") is True or
                        receipt.get("output_limit_exceeded") is True):
            return False
        session, owner = _task_session(runtime, task, operation, "CODE", "claude")
        if owner != task.get("id") or not session:
            return False
        with _confined(runtime, directory / "stdout.log").open("rb") as stream:
            header = stream.read(CLAUDE_SESSION_HEADER_BYTES)
        followup = _user_followup_context(runtime, task, operation) if task.get("user_followup") else None
        allow = bool(followup and (followup["owned"] or followup["public"]["status"] == "completed") and
                     operation.get("user_followup_id") == followup["profile"]["id"] and
                     operation.get("user_followup_request_sha256") == followup["profile"]["request_sha256"])
        return _claude_code_opening(header, session, allow_notifications=allow) is not None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return False


def _activity(runtime, directory, kind, actor, check_id):
    if directory is None:
        return [], None
    stdout = _confined(runtime, directory / "stdout.log")
    try:
        lines, modified = _tail(stdout)
    except FileNotFoundError:
        return [], None
    modified_iso = datetime.fromtimestamp(modified, timezone.utc).isoformat()
    if kind in ("check", "milestone_check"):
        # A check may contain megabytes of UE noise: show identity and freshness only.
        result = _object(_confined(runtime, directory / "check.json"), missing={}, maximum=65536)
        text = "独立检查：" + _clean(check_id or result.get("id") or "检查", 100)
        if type(result.get("passed")) is bool:
            text += " · " + ("通过" if result["passed"] else "未通过")
        else:
            text += " · 日志已有更新，等待检查回执"
        return [{"at": modified_iso, "actor": "UE/CHECK", "kind": "check", "text": text}], modified_iso
    rows = []
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        row = _event(event, actor)
        if row:
            rows.append(row)
    return rows[-MAX_ACTIVITY:], modified_iso


def _task_session(runtime, task, operation, phase, coding_provider):
    """Project only task-owned IDs; a live opening record may precede state save."""
    task_id = task.get("id")
    owner = task.get("session_owner_task_id", task.get("session_owner", task_id))
    session = task.get("session_id")
    if owner != task_id:
        return None, owner
    claude = coding_provider == "claude"
    identity_pattern = _CLAUDE_SESSION_ID if claude else _SESSION_ID
    if isinstance(session, str) and identity_pattern.fullmatch(session):
        return session, owner
    if (session or not task_id or phase != "CODE" or operation.get("kind") != "code"
            or operation.get("task_id") != task_id or operation.get("coding_provider", "dsh") != coding_provider):
        return None, owner
    raw = operation.get("output_dir")
    if not isinstance(raw, str):
        return None, owner
    directory = _confined(runtime, Path(raw))
    _confined(runtime / "runs", directory)
    stdout = _confined(runtime, directory / "stdout.log")
    try:
        with stdout.open("rb") as stream:
            header = stream.read(CLAUDE_SESSION_HEADER_BYTES if claude else SESSION_HEADER_BYTES)
    except FileNotFoundError:
        return None, owner
    # Only the first nonblank complete record is eligible. Never parse later
    # text/reasoning records or scan the entire log for an identity.
    for line in header.split(b"\n")[:-1]:
        line = line.decode("utf-8-sig", errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            return None, owner
        # The opening protocol permits metadata such as cwd. Project only ID.
        identity = None
        if isinstance(event, dict):
            if claude and event.get("type") == "system" and event.get("subtype") == "init":
                identity = event.get("session_id")
            elif not claude and event.get("type") == "session":
                identity = event.get("sessionId")
        return (identity if isinstance(identity, str) and identity_pattern.fullmatch(identity) else None), owner
    return None, owner


def _provider_retry(state, coding_name, planning_name="Codex"):
    """Project only a validated retry belonging to this phase/task; never raw errors."""
    record = state.get("provider_retry")
    if record is None or state.get("phase") in {"DONE", "BLOCKED"}:
        return None
    invalid = "服务重试记录无效，请核对运行断点。"
    if not isinstance(record, dict):
        raise ValueError(invalid)
    phase = record.get("phase")
    if phase not in {"PLAN", "REPLAN", "CODE", "REVIEW", "MILESTONE_REVIEW", "ART_REVIEW", "ART_DIAGNOSE"} or phase != state.get("phase"):
        raise ValueError(invalid)
    task_id = record.get("task_id")
    task_phase = phase in {"REPLAN", "CODE", "REVIEW", "ART_REVIEW", "ART_DIAGNOSE"}
    expected_task = (state.get("task") or {}).get("id") if task_phase else None
    if (phase in {"ART_REVIEW", "ART_DIAGNOSE"} and (state.get("task") or {}).get("kind") != "art"):
        raise ValueError(invalid)
    if phase == "REPLAN" and (state.get("task") or {}).get("kind") == "art":
        raise ValueError(invalid)
    if (task_id != expected_task or (task_phase and
            (not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", task_id)))):
        raise ValueError(invalid)
    attempt, reason = record.get("attempt"), record.get("reason")
    timestamp = _iso(record.get("next_retry_at"))
    head = record.get("head_sha")
    if (type(attempt) is not int or not 1 <= attempt <= 1_000_000_000 or
            not isinstance(reason, str) or reason not in _RETRY_REASONS or timestamp is None or
            not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head) or
            record.get("status") not in {"waiting", "retrying"}):
        raise ValueError(invalid)
    if phase == "REPLAN":
        source = ((state.get("task") or {}).get("replanning") or {}).get("fingerprint")
        if not isinstance(source, dict) or head != source.get("head_sha"):
            raise ValueError(invalid)
    # CODE may commit before a transient failure; only Engine may compare actual Git HEAD.
    provider = coding_name if phase in {"CODE", "ART_DIAGNOSE"} else planning_name if phase in {"PLAN", "REPLAN"} else "Codex"
    if reason == "claude_subscription_limit" and provider != _CODING_NAMES["claude"]:
        raise ValueError(invalid)
    if reason == "codex_subscription_limit" and provider != "Codex":
        raise ValueError(invalid)
    reason_label = f"本地 {provider} 暂不可用" if reason == "local_cli_unavailable" and phase in {"CODE", "ART_DIAGNOSE"} else _RETRY_REASONS[reason]
    return {"phase": phase, "task_id": task_id, "attempt": attempt, "reason": reason,
            "reason_label": reason_label, "next_retry_at": timestamp,
            "provider": provider, "status": record["status"]}


def _empty(now):
    return {"generated_at": now.isoformat(),
            "project": {"name": "无人值守开发", "source": None, "workspace": None, "branch": None, "milestone": None},
            "status": {"code": "error", "label": "状态读取失败", "phase": None, "actor": "系统", "summary": "",
                       "paused": False, "failures": 0, "error": None, "started_at": None, "elapsed_seconds": None,
                       "attempt_finished": False, "last_activity_at": None, "last_activity_age_seconds": None},
            "worker": {"alive": False, "healthy": False, "pid": None, "heartbeat_age_seconds": None, "recovery": None, "label": "状态未知"},
            "schedule": {"timezone": "Asia/Shanghai", "allowed": False, "next_open": None, "closes_at": None,
                         "weekday_start": "18:00", "weekday_end": "08:00", "weekends_all_day": True},
            "coding_provider": "dsh",
            "planning_provider": "codex",
            "models": {role: {"name": name, "model": None, "effort": None} for role, name in (("codex", "Codex"), ("dsh", "DSH"))},
            "codex_auth": {"policy": None, "current": None, "fallback": False},
            "check_progress": {"total": 0, "current_id": None},
            "task": None, "checks": [], "accepted": [], "deferred": [], "activity": [], "burst": None, "provider_retry": None,
            "publication": {"mode": None, "milestone_id": None, "pending_tasks": 0, "summary": ""},
            "workflow": {"current": None, "nodes": [], "edges": [], "summary": "", "route": "unknown",
                         "route_reason": "", "rework": {"active": False, "count": 0, "reason": ""}},
            "modules": [], "art": {"configured": False, "status": "not_configured", "requests": [], "promotions": []}, "error": None}


def _planning_source(runtime, value):
    if not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value:
        return None, {}
    try:
        directory = _confined(runtime, Path(value))
        record = _object(_confined(runtime, directory / "planning-decision.json"), missing={}, maximum=256 * 1024)
        return _clean(directory.name, 120), record
    except (OSError, ValueError, TypeError, RecursionError):
        return None, {}


def _operation_model(runtime, operation, directory, session=None, *, allow_notifications=False):
    """Return recorded model identity, never infer an old invocation from today's config."""
    model = _clean(operation.get("planning_model"), 120)
    effort = _clean(operation.get("planning_reasoning_effort"), 40)
    if model:
        return model, effort or None
    if directory is None:
        return None, None
    try:
        receipt = _object(_confined(runtime, directory / "process.json"), missing={}, maximum=16384)
        model = _clean(receipt.get("planning_model"), 120)
        effort = _clean(receipt.get("planning_reasoning_effort"), 40)
        if model:
            return model, effort or None
        if (operation.get("kind") != "code" or operation.get("coding_provider") != "claude" or
                not isinstance(session, str) or not _CLAUDE_SESSION_ID.fullmatch(session) or
                receipt.get("requested_session", session) not in (None, session)):
            return None, None
        with _confined(runtime, directory / "stdout.log").open("rb") as stream:
            header = stream.read(CLAUDE_SESSION_HEADER_BYTES)
        model = _claude_code_opening(header, session, allow_notifications=allow_notifications)
        if model:
            requested = operation.get("coding_model")
            effort = _clean(operation.get("coding_reasoning_effort"), 40) or None
            return model, effort if requested == model else None
    except (OSError, ValueError, TypeError, RecursionError):
        pass
    return None, None


def _user_repair_context(runtime, task):
    """Read a fixed, hash-bound user authorization without exposing sessions."""
    profile = task.get("user_repair")
    if not isinstance(profile, dict) or profile.get("status") != "active":
        return None
    identity = profile.get("id")
    if (profile.get("version") != 1 or not re.fullmatch(r"[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", str(identity or "")) or
            profile.get("epoch", identity) != identity or profile.get("model") != "claude-opus-5-5" or
            profile.get("reasoning_effort", profile.get("effort")) != "high" or
            profile.get("effort", "high") != "high" or
            not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", str(profile.get("source_head") or ""))):
        raise ValueError("用户修复授权身份无效")
    source_run = Path(str(profile.get("source_full_flow_run") or ""))
    if not source_run.is_absolute():
        source_run = runtime / "full-flow/runs" / source_run
    source_run = _confined(runtime / "full-flow/runs", source_run)
    if source_run.parent != runtime / "full-flow/runs" or not re.fullmatch(r"[a-f0-9]{32}", source_run.name):
        raise ValueError("用户修复的历史测试目录不匹配")
    root = _confined(runtime, runtime / "user-repairs" / identity)
    path = _confined(root, root / "request.json")
    relative = "user-repairs/" + identity + "/request.json"
    if profile.get("request_path") not in {str(path), relative}:
        raise ValueError("用户修复授权路径不匹配")
    with path.open("rb") as stream:
        raw = stream.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024 or hashlib.sha256(raw).hexdigest() != profile.get("request_sha256"):
        raise ValueError("用户修复授权文件摘要不匹配")
    request = json.loads(raw.decode("utf-8-sig"))
    if (not isinstance(request, dict) or request.get("task_id") != task.get("id") or
            any(request.get(key) != profile[key] for key in ("id", "version", "model", "source_head", "source_full_flow_run")) or
            request.get("reasoning_effort") != "high" or
            request.get("target_session_id") != task.get("session_id") or
            task.get("session_owner_task_id", task.get("id")) != task.get("id")):
        raise ValueError("用户修复授权与当前任务不匹配")
    public = {"id": identity, "title": "Opus修复全流程问题", "model": profile["model"], "effort": "high", "status": "active",
              "source_head": profile["source_head"], "source_full_flow_run": source_run.name,
              "request_sha256": profile["request_sha256"], "epoch": identity, "detail_url": "/api/flow/detail?lane=development&node=code"}
    for key in ("activation_rework", "max_cycles", "rework_window"):
        if type(profile.get(key)) is int and 0 <= profile[key] <= 10000:
            public[key] = profile[key]
    plan = None
    if profile.get("plan_path"):
        if any(request.get(key) != profile.get(key) for key in ("plan_path", "plan_sha256")):
            raise ValueError("本批修复计划与授权不匹配")
        candidate = Path(profile["plan_path"])
        candidate = _confined(root, candidate if candidate.is_absolute() else runtime / candidate)
        if candidate.suffix.lower() not in {".md", ".json", ".txt"} or not candidate.is_file() or candidate.stat().st_size > MAX_JSON_BYTES:
            raise ValueError("本批修复计划路径无效")
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != profile.get("plan_sha256"):
            raise ValueError("本批修复计划摘要不匹配")
        plan = candidate
    public["plan_available"] = plan is not None
    return {"profile": profile, "request": request, "request_path": path, "root": root, "plan": plan, "public": public}


def _user_followup_context(runtime, task, operation=None):
    profile = task.get("user_followup")
    if not isinstance(profile, dict):
        return None
    repair = _user_repair_context(runtime, task)
    identity = profile.get("id")
    if not repair or not re.fullmatch(r"[a-f0-9]{32}", str(identity or "")) or profile.get("status") not in {"active", "completed", "exhausted"}:
        raise ValueError("追加交接身份无效")
    root = _confined(runtime, runtime / "user-followups" / identity)
    path = _confined(root, root / "request.json")
    if profile.get("request_path") != str(path):
        raise ValueError("追加交接路径不匹配")
    with path.open("rb") as stream:
        raw = stream.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024 or hashlib.sha256(raw).hexdigest() != profile.get("request_sha256"):
        raise ValueError("追加交接授权摘要不匹配")
    request = json.loads(raw.decode("utf-8-sig"))
    keys = ("version", "id", "task_id", "repair_id", "repair_request_sha256", "session_id", "source_head", "max_turns")
    if (not isinstance(request, dict) or any(request.get(key) != profile.get(key) for key in keys) or
            profile.get("version") != 1 or profile.get("task_id") != task.get("id") or profile.get("session_id") != task.get("session_id") or
            profile.get("repair_id") != repair["profile"]["id"] or profile.get("repair_request_sha256") != repair["profile"]["request_sha256"] or
            type(profile.get("max_turns")) is not int or not 1 <= profile["max_turns"] <= 3):
        raise ValueError("追加交接与本任务授权不匹配")
    operation = operation or {}
    turn = operation.get("user_followup_turn")
    owned = (operation.get("kind") == "code" and operation.get("task_id") == task["id"] and
             operation.get("user_followup_id") == identity and operation.get("user_followup_request_sha256") == profile["request_sha256"] and
             type(turn) is int and 1 <= turn <= profile["max_turns"])
    summary = "等待 Claude 自主接入 UE MCP。"
    if profile["status"] == "active" and owned:
        summary = f"Claude自主接入UE MCP，回合{turn}/{profile['max_turns']}"
    elif profile["status"] == "exhausted":
        summary = "UE MCP 接入回合已用完，实际连接仍未证实；等待处理。"
    elif profile["status"] == "completed":
        from .user_followup import completed_followup_evidence
        proof = completed_followup_evidence(task, runtime)
        facts = proof.get("evidence") or {}
        if (proof.get("completed") is not True or facts.get("connected") is not True or
                not any(row.get("completed") is True and row.get("is_error") is False for row in facts.get("invocations", []) if isinstance(row, dict))):
            raise ValueError("UE MCP 实际连接证据尚未证实")
        summary = "UE MCP实际连接与工具调用已证；游戏修复仍需独立检查与审核。"
    public = {"id": identity, "request_sha256": profile["request_sha256"], "status": profile["status"],
              "turn": turn if owned else None, "max_turns": profile["max_turns"], "summary": summary}
    return {"profile": profile, "request": request, "root": root, "public": public, "owned": owned}


def _replanning_summary(runtime, task, context=None):
    """Use the bound review's short reason; keep full repair feedback in evidence."""
    pending = task.get("replanning") or {}
    source = pending.get("fingerprint") or {}
    from_code = pending.get("source_phase") == "CODE"
    reason = "本轮开发调用未完成，正在复盘执行与验证安排。" if from_code else "上轮审核仍有未解决问题，正在重新分析修复方案。"
    identity = task.get("id")
    number = source.get("review_round", task.get("reworks")) if isinstance(source, dict) else None
    if (isinstance(identity, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", identity) and
            type(number) is int and 0 <= number <= 10000 and isinstance(source, dict)):
        try:
            path = _confined(runtime, runtime / "tasks" / identity / ("review-" + str(number) + ".json"))
            with path.open("rb") as stream:
                raw = stream.read(256 * 1024 + 1)
            if len(raw) <= 256 * 1024 and hashlib.sha256(raw).hexdigest() == source.get("review_sha256"):
                review = json.loads(raw.decode("utf-8-sig"))
                review_head = source.get("review_head_sha", source.get("head_sha")) if from_code else source.get("head_sha")
                if review.get("head_sha") == review_head and isinstance(review.get("summary"), str) and review["summary"].strip():
                    short_reason = _clean(re.sub(r"\s+", " ", review["summary"]), 100)
                    reason = "开发调用未完成；历史审核提示：" + short_reason if from_code else short_reason
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            pass
    prefix = context or "Claude重新分析修复计划，原任务与会话保留；返回后继续开发、检查和审核。"
    return _clean(prefix + " 当前卡点：" + reason, 159)


def _replanning_model(runtime, operation, directory):
    """Use the fresh planner's bounded native opening, never the coder session."""
    if operation.get("kind") != "plan" or operation.get("planning_provider") != "claude" or directory is None:
        return None, None
    try:
        directory = _confined(runtime, directory)
        if directory.parent != runtime / "runs" or not re.fullmatch(r"plan-[a-f0-9]{32}", directory.name):
            return None, None
        with _confined(runtime, directory / "stdout.log").open("rb") as stream:
            header = stream.read(CLAUDE_SESSION_HEADER_BYTES)
        first = next((line for line in header.split(b"\n")[:-1] if line.strip()), None)
        event = json.loads(first.decode("utf-8-sig")) if first else {}
        session, model = event.get("session_id"), event.get("model")
        if (event.get("type") != "system" or event.get("subtype") != "init" or
                not isinstance(session, str) or not _CLAUDE_SESSION_ID.fullmatch(session) or
                not isinstance(model, str) or not re.fullmatch(r"claude-(?:sonnet|opus|haiku)-[A-Za-z0-9.-]{1,95}", model)):
            return None, None
        receipt = _object(_confined(runtime, directory / "process.json"), missing={}, maximum=65536)
        if receipt and receipt.get("kind") != "plan":
            return None, None
        selection = receipt.get("model_selection") or {}
        if selection and (selection.get("model_verified") is not True or selection.get("observed_model") != model):
            return None, None
        return model, _clean(selection.get("requested_reasoning_effort") or operation.get("planning_reasoning_effort"), 40) or None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return None, None


def _workflow(snapshot, config, state, operation, directory=None):
    """Build role and stage views from the same bounded checkpoint evidence."""
    status = snapshot["status"]
    phase = state.get("blocked_from") if state.get("phase") == "BLOCKED" else state.get("phase")
    task = state.get("task") or {}
    milestone = state.get("milestone") or {}
    selection = state.get("planning_selection") or {}
    if not isinstance(selection, dict) or not isinstance(milestone, dict):
        selection, milestone = {}, {}
    claude, codex = config.get("claude", {}), config.get("codex", {})
    planner = config.get("planning_provider", "codex")
    coder = config.get("coding_provider", "dsh")
    replans = [row for row in task.get("replanning_history", []) if isinstance(row, dict) and row.get("task_id") == task.get("id")]
    tier = "critical" if phase == "REPLAN" or replans else operation.get("planning_level") or selection.get("planning_level")
    current = {"PLAN": "critical_plan" if tier == "critical" else "routine_plan", "REPLAN": "critical_plan", "CODE": "code",
               "CHECK": "check", "REVIEW": "review", "PUBLISH": "publish",
               "MILESTONE_REVIEW": "milestone_review", "MILESTONE_PUBLISH": "milestone_publish"}.get(phase)
    if phase == "MILESTONE_REVIEW" and (operation.get("kind") == "milestone_check" or
            len(snapshot["checks"]) < snapshot["check_progress"]["total"]):
        current = "check"
    source_value = selection.get("source_output_dir") if phase == "PLAN" else task.get("planning_source_output_dir")
    source, draft = _planning_source(Path(config["runtime_dir"]), source_value)
    level = task.get("planning_level") or (milestone.get("proposal") or {}).get("planning_level")
    route = "critical" if tier == "critical" or level == "critical" else "routine" if level == "routine" else "unknown"
    critical_enabled = (planner == "claude" and bool(claude.get("critical_plan_model"))) or route == "critical"
    check_rows = snapshot["checks"]
    previous_checks = phase in {"CODE", "REPLAN"} and bool(check_rows)
    if previous_checks:
        snapshot["check_progress"]["evidence_scope"] = "previous_attempt"
    checks_done = phase not in {"CODE", "REPLAN"} and bool(check_rows) and len(check_rows) >= snapshot["check_progress"]["total"] and all(row["passed"] is True for row in check_rows)
    checks_failed = phase not in {"CODE", "REPLAN"} and any(row["passed"] is False for row in check_rows)
    reworks = task.get("reworks") if type(task.get("reworks")) is int else 0
    rework_active = phase in {"CODE", "REPLAN"} and reworks > 0
    published = milestone.get("status") == "published"
    completed = {
        "routine_plan": bool(task or source or milestone.get("proposal")),
        "critical_plan": bool(replans or task.get("planning_level") == "critical" or
                              (milestone.get("proposal") or {}).get("planning_level") == "critical"),
        "code": phase != "REPLAN" and bool((task.get("candidate") or {}).get("head_sha")), "check": checks_done,
        "review": phase == "PUBLISH" or published,
        "publish": bool(task.get("id")) and any(record["id"] == task["id"] for record in snapshot["accepted"]),
        "milestone_review": published or phase == "MILESTONE_PUBLISH",
        "milestone_publish": published,
    }
    roles = [
        ("routine_planner", "常规任务规划", "明确本轮任务范围、验收条件和核心测试，判断是否涉及关键系统。", config.get(planner, {})),
        ("critical_planner", "关键系统规划", "核对关键系统边界、兼容性和跨模块行为，形成最终任务书。", {"model": claude.get("critical_plan_model"), "reasoning_effort": claude.get("critical_plan_reasoning_effort", claude.get("reasoning_effort"))}),
        ("developer", "功能开发", "按任务书修改代码、运行提交前验证并提交本地变更。", config.get(coder, {})),
        ("checks", "构建与测试", "在固定提交上独立运行构建、核心测试及阶段检查。", {}),
        ("reviewer", "独立审核", "由 Codex 核对实际代码和证据，决定通过、返工或阻塞。", codex),
        ("publisher", "交付与发布", "登记通过的本地任务，阶段独立验收通过后按策略发布。", {}),
    ]
    role_map = {identity: settings for identity, _, _, settings in roles}
    user_repair = (snapshot.get("task") or {}).get("user_repair")
    user_followup = (snapshot.get("task") or {}).get("user_followup")
    if user_repair:
        role_map["developer"] = {"model": user_repair["model"], "reasoning_effort": user_repair["effort"]}
    definitions = [
        ("routine_plan", "常规规划", "routine_planner"), ("critical_plan", "关键规划", "critical_planner"),
        ("code", "开发实现", "developer"), ("check", "独立检查", "checks"),
        ("review", "任务审核", "reviewer"), ("publish", "任务登记", "publisher"),
        ("milestone_review", "阶段审核", "reviewer"), ("milestone_publish", "阶段发布", "publisher"),
    ]
    labels = {"running": "正在执行", "ready": "等待执行", "waiting": "等待恢复", "paused": "已暂停",
              "stale": "状态待核实", "blocked": "需要处理", "completed": "已完成", "idle": "待命",
              "disabled": "未启用", "not_required": "本轮无需"}
    current_state = {"paused": "paused", "heartbeat_stale": "stale", "stopped": "waiting", "waiting_window": "waiting",
                     "waiting_provider": "waiting", "waiting_filesystem": "waiting", "blocked": "blocked", "error": "stale"}.get(status["code"], "ready")
    expected_kind = {"routine_plan": "plan", "critical_plan": "plan", "code": "code", "check": "milestone_check" if phase == "MILESTONE_REVIEW" else "check",
                     "review": "review", "milestone_review": "review"}.get(current)
    owns_operation = operation.get("task_id", task.get("id")) == task.get("id")
    if phase == "REPLAN":
        pending = task.get("replanning") or {}
        head = (pending.get("fingerprint") or {}).get("head_sha")
        owns_operation = (owns_operation and operation.get("task_id") == task.get("id") and bool(head) and
                          operation.get("head_sha") == head and operation.get("planning_provider") == "claude" and
                          operation.get("planning_level") == "critical" and
                          (pending.get("source_phase") == "CODE" or head in {(task.get("candidate") or {}).get("head_sha"), task.get("code_base"), state.get("expected_head")}))
    if (current_state == "ready" and expected_kind and operation.get("kind") == expected_kind and owns_operation and
            status.get("started_at") and not status["attempt_finished"] and snapshot["worker"]["alive"] and snapshot["worker"]["healthy"] and
            (current != "check" or snapshot["check_progress"]["current_id"] is not None)):
        current_state = "running"
    if current == "check" and checks_failed and current_state == "ready":
        current_state = "blocked"
    model_evidence = operation
    if phase == "PLAN" and not operation and selection.get("planning_model"):
        model_evidence = selection
    session = (snapshot["task"] or {}).get("session_id")
    model_owned = owns_operation and (operation.get("kind") != "code" or operation.get("task_id") == task.get("id"))
    if user_repair and operation.get("kind") == "code":
        model_owned = (model_owned and operation.get("user_repair_id") == user_repair["id"] and
                       operation.get("user_repair_request_sha256") == user_repair["request_sha256"])
        if current_state == "running" and not model_owned:
            current_state = "ready"
    followup_bound = bool(user_followup and operation.get("user_followup_id") == user_followup["id"] and
                         operation.get("user_followup_request_sha256") == user_followup["request_sha256"] and
                         (user_followup["status"] == "completed" or user_followup.get("turn") == operation.get("user_followup_turn") and user_followup.get("turn")))
    if user_followup and user_followup["status"] == "active" and operation.get("kind") == "code" and not followup_bound:
        model_owned = False
    recorded_model, recorded_effort = _operation_model(Path(config["runtime_dir"]), model_evidence, directory, session,
        allow_notifications=followup_bound) if model_owned else (None, None)
    maintenance = snapshot.get("maintenance") or {}
    if (current_state == "paused" and current == "code" and state.get("phase") == "CODE" and
            maintenance.get("status") == "waiting_boundary" and maintenance.get("current_call_active") is True and
            model_owned and recorded_model and snapshot["worker"]["alive"] and snapshot["worker"]["healthy"] and
            _native_code_active(Path(config["runtime_dir"]), task, operation, directory)):
        current_state = "running"
    if phase == "REPLAN":
        recorded_model, recorded_effort = _replanning_model(Path(config["runtime_dir"]), operation, directory) if model_owned else (None, None)
        if current_state == "running" and not recorded_model:
            current_state = "ready"
    nodes = []
    for identity, title, module_id in definitions:
        if identity == "code" and user_repair:
            title = "Opus修复全流程问题"
            if user_followup and user_followup["status"] == "active" and user_followup.get("turn"):
                title = user_followup["summary"]
        enabled = critical_enabled if identity == "critical_plan" else bool(config.get("publication")) if identity.startswith("milestone_") else True
        node_state = "completed" if completed[identity] else "idle"
        if not enabled:
            node_state = "disabled"
        if identity == "critical_plan" and critical_enabled and level == "routine" and phase != "REPLAN" and not replans:
            node_state = "not_required"
        if identity == "check" and checks_failed:
            node_state = "blocked"
        if identity == current:
            node_state = current_state
        settings = role_map[module_id]
        model, effort = _clean(settings.get("model"), 120) or None, _clean(settings.get("reasoning_effort"), 40) or None
        model_source = "configured" if model else "none"
        if identity == current and module_id not in {"checks", "publisher"} and owns_operation and (
                operation.get("kind") == expected_kind or (phase == "PLAN" and selection.get("planning_model"))):
            model, effort = recorded_model, recorded_effort
            model_source = "recorded" if model else "unknown"
        elif identity == "critical_plan" and replans:
            latest = replans[-1]
            historical = {"kind": "plan", "planning_provider": "claude", "planning_model": latest.get("planning_model"),
                          "planning_reasoning_effort": latest.get("planning_reasoning_effort")}
            try:
                model, effort = _replanning_model(Path(config["runtime_dir"]), historical,
                                                  _confined(Path(config["runtime_dir"]), Path(latest.get("output_dir", ""))))
            except (OSError, ValueError, TypeError):
                model, effort = None, None
            model_source = "recorded" if model else "unknown"
        elif identity in {"routine_plan", "critical_plan"} and completed[identity]:
            evidence = draft if identity == "routine_plan" and (source or level == "critical") else task
            model, effort = _clean(evidence.get("planning_model"), 120) or None, _clean(evidence.get("planning_reasoning_effort"), 40) or None
            model_source = "recorded" if model else "unknown"
        elif completed[identity] and module_id in {"developer", "reviewer"}:
            model, effort, model_source = None, None, "unknown"
        summary = status["summary"] if identity == current else labels[node_state]
        if identity == "critical_plan" and (phase == "REPLAN" or replans):
            title = "Claude重新分析修复计划"
            if identity != current:
                summary = "本任务修复计划已返回；后续仍需独立检查和审核。"
            elif phase == "REPLAN":
                summary = _replanning_summary(Path(config["runtime_dir"]), task,
                                               None if status["code"] == "replan" else summary)
        if identity == "critical_plan" and node_state == "not_required":
            summary = "本轮为常规任务，直接由常规规划进入开发。"
        elif identity == "routine_plan" and source:
            summary = "候选任务已转交关键系统规划。"
        elif identity == "check" and check_rows:
            summary = f"已记录 {len(check_rows)} / {snapshot['check_progress']['total']} 项检查，{sum(row['passed'] is True for row in check_rows)} 项通过。"
            if previous_checks:
                summary = "上一轮检查记录 · " + summary + "等待本轮新提交后重新检查。"
            if checks_failed:
                summary += "存在未通过的检查。"
        nodes.append({"id": identity, "title": title, "module_id": module_id, "current": identity == current,
                      "optional": identity == "critical_plan", "enabled": enabled, "state": node_state,
                      "state_label": labels[node_state], "model": model, "effort": effort, "model_source": model_source,
                      "configured_model": _clean(settings.get("model"), 120) or None, "summary": _clean(summary, 600)})
    previous = {"critical_plan": "review" if phase == "REPLAN" else "routine_plan", "code": "critical_plan" if replans else "review" if rework_active else "critical_plan" if route == "critical" else "routine_plan",
                "check": "critical_plan" if phase == "MILESTONE_REVIEW" and route == "critical" else "routine_plan" if phase == "MILESTONE_REVIEW" else "code",
                "review": "check", "publish": "review", "milestone_review": "check", "milestone_publish": "milestone_review",
                "routine_plan": "milestone_review" if state.get("milestone_feedback") else "publish" if snapshot["accepted"] else None}.get(current)
    connections = [("routine_plan", "critical_plan", "关键任务或阶段完成", "conditional"), ("routine_plan", "code", "常规任务", "next"),
                   ("critical_plan", "code", "最终任务书", "next"), ("code", "check", "本地提交", "next"),
                   ("check", "review", "任务检查完成", "next"), ("review", "publish", "验收通过", "next"),
                   ("publish", "routine_plan", "下一项任务", "next"), ("review", "code", "同任务返工", "rework"),
                   ("review", "critical_plan", "重新分析修复计划", "rework"),
                   ("critical_plan", "check", "阶段完成提案", "conditional"), ("routine_plan", "check", "阶段完成提案", "conditional"),
                   ("check", "milestone_review", "阶段检查完成", "conditional"), ("milestone_review", "milestone_publish", "阶段验收通过", "next"),
                   ("milestone_review", "routine_plan", "阶段补充任务", "rework")]
    edges = [{"from": start, "to": end, "label": label, "kind": kind, "active": start == previous and end == current}
             for start, end, label, kind in connections]
    modules = []
    for identity, title, responsibility, _ in roles:
        choices = [node for node in nodes if node["module_id"] == identity]
        chosen = next((node for node in choices if node["current"]), next((node for node in reversed(choices) if node["state"] == "completed"), choices[0]))
        activities = [{"at": row.get("at"), "text": _clean(row.get("text"), 300)} for row in snapshot["activity"][-3:]] if chosen["current"] and owns_operation and operation.get("kind") == expected_kind else []
        summary = chosen["summary"]
        if identity == "developer" and rework_active:
            summary = "第 " + str(reworks) + " 次返工 · " + _clean(status["summary"], 400)
        modules.append({key: chosen[key] for key in ("state", "state_label", "current", "model", "effort", "model_source", "configured_model")})
        modules[-1].update(id=identity, title=title, responsibility=responsibility, summary=summary,
                           activity=activities, task_id=_clean(task.get("id"), 101) or None,
                           task_title=_clean(task.get("title"), 300) or None)
    snapshot["workflow"] = {"current": current, "nodes": nodes, "edges": edges, "summary": _clean(status["summary"], 600),
                            "route": route, "route_reason": _clean(task.get("planning_reason") or (selection.get("critical_candidate") or {}).get("planning_reason"), 600),
                            "rework": {"active": rework_active, "count": max(0, reworks), "reason": _clean(task.get("feedback"), 600)}}
    snapshot["modules"] = modules


def _full_flow_context(config, state=None):
    """Validate a single explicit isolated run; never follow request-provided roots."""
    runtime = Path(config["runtime_dir"]).absolute()
    pointer_path = _confined(runtime, runtime / "full-flow/current.json")
    if not pointer_path.exists():
        return None
    pointer = _object(pointer_path, missing={}, maximum=16384)
    identity = pointer.get("id")
    if pointer.get("version") != 1 or not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{32}", identity):
        raise ValueError("全流程测试指针无效")
    run = _confined(runtime, runtime / "full-flow/runs" / identity)
    expected_relative = "full-flow/runs/" + identity + "/request.json"
    if pointer.get("request_path") != expected_relative:
        raise ValueError("全流程测试请求路径不匹配")
    request_path = _confined(runtime, run / "request.json")
    with request_path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != pointer.get("request_file_sha256"):
        raise ValueError("全流程测试请求文件摘要不匹配")
    request = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(request, dict):
        raise ValueError("全流程测试请求格式无效")
    digest = hashlib.sha256(json.dumps({key: value for key, value in request.items() if key != "request_sha256"},
                                     sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    if request.get("request_sha256") != digest or pointer.get("request_sha256") != digest:
        raise ValueError("全流程测试请求绑定摘要不匹配")
    source, project = _confined(runtime, runtime / "workspace"), _confined(run, run / "project")
    if (request.get("version") != 1 or request.get("id") != identity or
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", str(request.get("task_id") or "")) or
            not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", str(request.get("head_sha") or "")) or
            request.get("runtime_dir") != str(runtime) or request.get("source_workspace") != str(source) or
            request.get("run_dir") != str(run) or request.get("project_dir") != str(project) or
            not isinstance(request.get("engine_dir"), str) or not Path(request["engine_dir"]).is_absolute() or
            _iso(request.get("authorized_at")) is None or request.get("model") != "claude-opus-5-5" or request.get("effort") != "high"):
        raise ValueError("全流程测试任务、版本或隔离目录绑定不匹配")
    status = _object(_confined(run, run / "status.json"), missing={}, maximum=65536)
    phases = {"planning", "preparing", "testing", "analyzing", "completed", "failed"}
    stages = {"plan", "prepare", "build", "regression", "visual", "package", "package-short", "retest", "analysis"}
    native = next((_confined(run, run / "native" / phase) for phase in _FULL_FLOW_NATIVE_PHASES
                   if status.get("output_dir") == str(run / "native" / phase)), None)
    if (status.get("version") != 1 or status.get("id") != identity or status.get("request_sha256") != digest or
            any(status.get(key) != request[key] for key in ("task_id", "head_sha", "model", "effort")) or
            status.get("status") not in phases or status.get("stage") not in stages or _iso(status.get("updated_at")) is None or native is None or
            status.get("report_path") not in (None, "", str(run / "report.md"))):
        raise ValueError("全流程测试状态与受信请求不匹配")
    return {"runtime": runtime, "run": run, "project": project, "source": source, "native": native,
            "request": request, "status": status, "request_sha256": digest,
            "request_file_sha256": pointer["request_file_sha256"]}


def _workspace_head(source):
    """Read only a clone's bounded HEAD/ref metadata; never follow an external gitdir."""
    git = _confined(source, source / ".git")
    if not git.is_dir():
        return None
    def read(path, maximum):
        path = _confined(git, path)
        with path.open("rb") as stream:
            value = stream.read(maximum + 1)
        if len(value) > maximum:
            raise ValueError("Git 版本元数据过大")
        return value.decode("ascii").strip()
    value = read(git / "HEAD", 4096)
    if re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", value):
        return value
    if not value.startswith("ref: "):
        return None
    ref = value[5:]
    if not re.fullmatch(r"refs/[A-Za-z0-9_./-]{1,200}", ref) or ".." in ref:
        return None
    path = _confined(git, git / ref)
    if path.is_file():
        value = read(path, 4096)
        return value if re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", value) else None
    packed = _confined(git, git / "packed-refs")
    if packed.is_file():
        for row in read(packed, 1024 * 1024).splitlines():
            parts = row.split(" ", 1)
            if len(parts) == 2 and parts[1] == ref and re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", parts[0]):
                return parts[0]
    return None


def _full_flow_notification_proof(context):
    """Only real test-native startup and terminal local facts are notification evidence."""
    request, status, run = (context[key] for key in ("request", "status", "run"))
    try:
        with _confined(run, run / "native/test/stdout.log").open("rb") as stream:
            raw = stream.read(CLAUDE_SESSION_HEADER_BYTES)
        first = next((line for line in raw.split(b"\n")[:-1] if line.strip()), None)
        event = json.loads(first.decode("utf-8-sig")) if first else {}
        if (event.get("type") != "system" or event.get("subtype") != "init" or event.get("model") != request["model"] or
                not isinstance(event.get("session_id"), str) or not _CLAUDE_SESSION_ID.fullmatch(event["session_id"])):
            return None
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    proof = {"request_sha256": context["request_sha256"], "test_native_started": True, "terminal_evidence": False}
    if status["status"] not in {"completed", "failed"}:
        return proof
    def bound(value, stage):
        return (value.get("version") == 1 and value.get("id") == request["id"] and value.get("task_id") == request["task_id"] and
                value.get("head_sha") == request["head_sha"] and value.get("request_sha256") == context["request_sha256"] and value.get("stage") == stage)
    try:
        receipts = []
        for stage in ("prepare", "build", "regression", "visual", "package"):
            value = _object(_confined(run, run / "checks" / stage / "receipt.json"), missing={}, maximum=_FULL_FLOW_RECORD_BYTES)
            if bound(value, stage) and value.get("status") in {"passed", "failed"} and _iso(value.get("finished_at")):
                receipts.append(value)
        facts = _object(_confined(run, run / "facts.json"), missing={}, maximum=_FULL_FLOW_RECORD_BYTES)
        report = _confined(run, run / "report.md")
        report_ready = status.get("report_path") == str(report) and report.is_file() and 0 < report.stat().st_size <= MAX_JSON_BYTES
        if status["status"] == "completed":
            if not (report_ready and len(receipts) == 5 and bound(facts, "status") and facts.get("all_stages_finished") is True):
                return proof
            all_passed = facts.get("passed") is True and all(row.get("status") == "passed" and row.get("passed") is True for row in receipts)
            proof.update(terminal_evidence=True, outcome="checks_passed" if all_passed else "checks_failed_or_not_run",
                         failed_stages=[row["stage"] for row in receipts if row.get("status") != "passed" or row.get("passed") is not True])
        else:
            terminal_process = False
            for phase in _FULL_FLOW_NATIVE_PHASES[1:]:
                process = _object(_confined(run, run / "native" / phase / "process.json"), missing={}, maximum=65536)
                terminal_process = terminal_process or type(process.get("exit_code")) is int or process.get("timed_out") is True or process.get("launch_or_capture_failed") is True
            if terminal_process or report_ready or any(row.get("status") == "failed" for row in receipts):
                proof.update(terminal_evidence=True, outcome="execution_failed", failed_stages=[row["stage"] for row in receipts if row.get("status") == "failed"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return proof


def _full_flow_manifest_sha(context):
    if "_manifest_sha" in context:
        return context["_manifest_sha"]
    path = _confined(context["run"], context["run"] / "source-manifest.json")
    if not path.is_file() or path.stat().st_size > _FULL_FLOW_MANIFEST_BYTES:
        context["_manifest_sha"] = None
        return None
    digest, length = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while data := stream.read(65536):
            length += len(data)
            if length > _FULL_FLOW_MANIFEST_BYTES:
                return None
            digest.update(data)
    context["_manifest_sha"] = digest.hexdigest()
    return context["_manifest_sha"]


def _full_flow_reconciliations(context, retest, manifest_sha):
    """Verify separately derived parser corrections; these are never new executions."""
    request, run = context["request"], context["run"]
    rows = retest.get("reconciled_tool_false_failure")
    if not isinstance(rows, list) or not manifest_sha:
        return []
    result, seen, raw_cache = [], set(), {}
    for item in rows[:200]:
        if not isinstance(item, dict):
            continue
        index = item.get("case_index")
        keys = ("id", "task_id", "head_sha", "request_sha256", "source_manifest_sha256", "name", "mode", "source_stage",
                "case_index", "status", "original_status", "execution", "passed_after_parser_correction", "normal_rhi_proven")
        if (type(index) is not int or not 1 <= index <= 999 or not isinstance(item.get("name"), str) or not item["name"] or
                item.get("id") != request["id"] or item.get("task_id") != request["task_id"] or item.get("head_sha") != request["head_sha"] or
                item.get("request_sha256") != context["request_sha256"] or item.get("source_manifest_sha256") != manifest_sha or
                item.get("mode") != "normal" or item.get("source_stage") != "visual" or item.get("status") != "reconciled_tool_false_failure" or
                item.get("original_status") != "FAIL" or item.get("execution") != "not_reexecuted" or item.get("normal_rhi_proven") is not True or
                item.get("passed_after_parser_correction") is not True or (item["name"], index) in seen):
            continue
        expected = {"original_visual_receipt": run / "checks/visual/receipt.json",
                    "original_case_receipt": run / "checks/visual/attempt-1" / f"case-{index:03d}" / "receipt.json",
                    "derived_receipt": run / "checks/retest/attempt-1/reconciliations" / f"case-{index:03d}.json"}
        valid, derived = True, None
        try:
            for key, path in expected.items():
                ref = item.get(key)
                path = _confined(run, path)
                if (not isinstance(ref, dict) or ref.get("path") != str(path) or not re.fullmatch(r"[a-f0-9]{64}", str(ref.get("sha256") or ""))):
                    valid = False; break
                if path not in raw_cache:
                    with path.open("rb") as stream:
                        raw_cache[path] = stream.read(_FULL_FLOW_RECORD_BYTES + 1)
                raw = raw_cache[path]
                if len(raw) > _FULL_FLOW_RECORD_BYTES or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                    valid = False; break
                if key == "derived_receipt":
                    derived = json.loads(raw.decode("utf-8-sig"))
            if not valid or not isinstance(derived, dict) or any(derived.get(key) != item.get(key) for key in keys):
                continue
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
        seen.add((item["name"], index)); result.append(item)
    return result


def _full_flow_runner_bound(context, row):
    """Recognize only this run's original runner or its exact recorded migration."""
    request, run = context["request"], context["run"]
    path = _confined(run, run / "binding.json")
    original = _object(path, missing={}, maximum=16384)
    identity = {key: request[key] for key in ("version", "id", "task_id", "head_sha", "request_sha256")}
    identity["request_file_sha256"] = context["request_file_sha256"]
    if any(original.get(key) != value or row.get(key) != value for key, value in identity.items()):
        return False
    code_keys = ("runner_sha256", "retest_runner_sha256")
    if any(not re.fullmatch(r"[a-f0-9]{64}", str(original.get(key) or "")) for key in code_keys):
        return False
    if all(row.get(key) == original[key] for key in code_keys):
        return True
    migration_path = _confined(run, run / "operator-runner-migration.json")
    migration = _object(migration_path, missing={}, maximum=16384)
    binding_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    reason = "recognize_forced_d3d12_rhi_and_reconcile_visual_parser_false_failures"
    expected = {key: identity[key] for key in ("version", "id", "task_id", "head_sha", "request_sha256")}
    expected.update(binding_sha256=binding_sha, reason=reason)
    for key in code_keys:
        expected["from_" + key] = original[key]
        expected["to_" + key] = row.get(key)
        if not re.fullmatch(r"[a-f0-9]{64}", str(row.get(key) or "")):
            return False
    if migration != expected:
        return False
    raw = migration_path.read_bytes()
    return row.get("operator_runner_migration") == {"path": str(migration_path), "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw), "reason": reason, "original_binding_sha256": binding_sha}


def _full_flow_running_manifest_bound(context, row, stage):
    manifest_sha = _full_flow_manifest_sha(context)
    if not manifest_sha:
        return False
    if "source_manifest_sha256" in row:
        return row["source_manifest_sha256"] == manifest_sha
    # The helper writes its running receipt before _verify_inputs adds this SHA
    # in memory. Its completed prepare receipt supplies the immutable input link.
    request, run = context["request"], context["run"]
    attempt = row.get("attempt")
    if (type(attempt) is not int or attempt not in ({1, 2} if stage == "package" else {1}) or
            row.get("output_dir") != str(_confined(run, run / "checks" / stage / f"attempt-{attempt}")) or
            not _full_flow_runner_bound(context, row)):
        return False
    prepare = _object(_confined(run, run / "checks/prepare/receipt.json"), missing={}, maximum=_FULL_FLOW_RECORD_BYTES)
    if (prepare.get("stage") != "prepare" or prepare.get("status") != "passed" or prepare.get("passed") is not True or
            not _iso(prepare.get("finished_at")) or prepare.get("source_manifest_sha256") != manifest_sha or
            not _full_flow_runner_bound(context, prepare)):
        return False
    manifest = _object(_confined(run, run / "source-manifest.json"), missing={}, maximum=_FULL_FLOW_MANIFEST_BYTES)
    return manifest.get("head_sha") == request["head_sha"] and manifest.get("request_sha256") == context["request_sha256"]


def _full_flow_stage_projection(context, labels):
    request, status, run = (context[key] for key in ("request", "status", "run"))
    result = {"operator_stage": status["stage"], "stage_source": "operator"}
    if status["status"] not in {"preparing", "testing", "analyzing"}:
        return result
    running = []
    for stage in ("prepare", "build", "regression", "visual", "package", "retest", "package-short"):
        row = _object(_confined(run, run / "checks" / stage / "receipt.json"), missing={}, maximum=_FULL_FLOW_RECORD_BYTES)
        if (row.get("version") != 1 or row.get("id") != request["id"] or row.get("task_id") != request["task_id"] or
                row.get("head_sha") != request["head_sha"] or row.get("request_sha256") != context["request_sha256"] or
                row.get("stage") != stage or row.get("status") != "running" or not _iso(row.get("started_at")) or not _iso(row.get("updated_at"))):
            continue
        if stage != "prepare" and not _full_flow_running_manifest_bound(context, row, stage):
            continue
        if stage == "package-short" and row.get("attempt") != 1:
            continue
        running.append((stage, row))
    if len(running) == 1:
        stage, row = running[0]
        result.update(stage=stage, stage_label=labels[stage], stage_source="receipt", stage_updated_at=_iso(row["updated_at"]),
                      summary=labels[stage] + "正在执行；当前阶段依据本次绑定的运行中回执。")
    elif len(running) > 1:
        stages = [stage for stage, _ in running]
        result.update(stage="conflict", stage_label="阶段回执冲突", stage_source="conflict", stage_conflicts=stages,
                      summary=_clean("多个阶段同时标记运行中：" + "、".join(labels[stage] for stage in stages) + "。暂不判定当前阶段。", 159))
    elif status["status"] in {"testing", "analyzing"} and status["stage"] != "analysis":
        result.update(stage="analysis", stage_label=labels["analysis"], stage_source="fallback",
                      summary="当前没有绑定阶段标记为运行中；等待 Opus 分析或安排下一步。")
    return result


def _full_flow_snapshot(config, state, *, context=None):
    context = context or _full_flow_context(config, state)
    if context is None:
        return None
    request, status, native, run = (context[key] for key in ("request", "status", "native", "run"))
    source = "configured"
    try:
        with _confined(run, native / "stdout.log").open("rb") as stream:
            header = stream.read(CLAUDE_SESSION_HEADER_BYTES)
        first = next((line for line in header.split(b"\n")[:-1] if line.strip()), None)
        opening = json.loads(first.decode("utf-8-sig")) if first else {}
        if (opening.get("type") == "system" and opening.get("subtype") == "init" and opening.get("model") == request["model"] and
                isinstance(opening.get("session_id"), str) and _CLAUDE_SESSION_ID.fullmatch(opening["session_id"])):
            source = "recorded"
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    current_head = None
    try:
        current_head = _workspace_head(context["source"])
    except (OSError, ValueError, RuntimeError):
        pass
    task_id = (state.get("task") or {}).get("id")
    historical = bool(current_head and current_head != request["head_sha"] or task_id and task_id != request["task_id"])
    labels = {"planning": "只读规划", "preparing": "准备隔离测试环境", "testing": "执行全流程测试", "analyzing": "分析测试结果", "completed": "本次测试已结束", "failed": "本次测试未完成"}
    stages = {"plan": "测试规划", "prepare": "准备环境", "build": "构建", "regression": "回归测试", "visual": "视觉检查", "package": "打包验证", "package-short": "短路径正式打包补测", "retest": "失败项逐项复测", "analysis": "结果分析"}
    public = {"id": request["id"], "title": "Opus全流程测试", "task_id": request["task_id"], "head_sha": request["head_sha"],
            "current_head_sha": current_head, "historical": historical, "status": status["status"], "status_label": labels[status["status"]],
            "stage": status["stage"], "stage_label": stages[status["stage"]], "summary": _clean(status.get("summary"), 159),
            "updated_at": _iso(status["updated_at"]), "actor": "Claude Code", "model": request["model"], "effort": request["effort"],
            "model_source": source, "detail_url": "/api/flow/detail?lane=development&node=full_flow_test"}
    public.update(_full_flow_stage_projection(context, stages))
    proof = _full_flow_notification_proof(context)
    if proof:
        public["notification_proof"] = proof
    retest = _object(_confined(run, run / "checks/retest/receipt.json"), missing={}, maximum=_FULL_FLOW_RECORD_BYTES)
    short = _object(_confined(run, run / "checks/package-short/receipt.json"), missing={}, maximum=_FULL_FLOW_RECORD_BYTES)
    corrections = retest.get("reconciled_tool_false_failure")
    manifest_sha = _full_flow_manifest_sha(context) if short or corrections else None
    if (retest.get("version") == 1 and retest.get("id") == request["id"] and retest.get("task_id") == request["task_id"] and
            retest.get("head_sha") == request["head_sha"] and retest.get("request_sha256") == context["request_sha256"] and retest.get("stage") == "retest"):
        count = lambda key: retest.get(key) if type(retest.get(key)) is int and 0 <= retest[key] <= 1000 else None
        planned, attempted = count("planned_count"), count("attempted_count")
        line = "补充复测 · " + {"running": "进行中", "passed": "已形成独立回执", "failed": "仍有失败或未运行项"}.get(retest.get("status"), "记录已生成")
        if planned is not None and attempted is not None:
            line += f" · 已逐项尝试 {attempted}/{planned}"
        if planned == 0 and retest.get("no_failures") is True:
            line = "补充复测 · 首轮没有符合条件的失败项，未新增测试执行。"
        else:
            for key, label in (("newly_passed", "本次通过"), ("still_failed", "仍失败"), ("not_run", "未运行")):
                if isinstance(retest.get(key), list) and all(isinstance(value, str) for value in retest[key]):
                    line += f" · {label} {len(retest[key])}"
        line += "。首轮失败记录保持独立。"
        public["retest"] = {"status": retest.get("status"), "planned_count": planned, "attempted_count": attempted,
                            "first_pass_unchanged": retest.get("first_pass_unchanged") is True, "summary": _clean(line, 159)}
        reconciled = _full_flow_reconciliations(context, retest, manifest_sha)
        if reconciled:
            public["retest"]["reconciled_tool_false_failure_count"] = len(reconciled)
            public["retest"]["reconciliation_summary"] = f"校验器误报已核实 {len(reconciled)} 项；原 FAIL 记录保留，未重新执行，不计入真实重测通过。"
    if (short.get("version") == 1 and short.get("id") == request["id"] and short.get("task_id") == request["task_id"] and
            short.get("head_sha") == request["head_sha"] and short.get("request_sha256") == context["request_sha256"] and
            short.get("stage") == "package-short" and short.get("attempt") == 1 and short.get("status") in {"running", "passed", "failed"} and
            manifest_sha and short.get("source_manifest_sha256") == manifest_sha):
        label = "补测回执标记通过" if short.get("status") == "passed" and short.get("passed") is True else "补测仍失败或未完成" if short.get("status") == "failed" else "正在补测"
        public["package_short"] = {"status": short.get("status"), "passed": short.get("status") == "passed" and short.get("passed") is True,
                                   "summary": "短路径环境补测 · " + label + "；原 package 路径长度失败记录保留，补测不替换首轮结论。"}
    return public


def _accepted_full_flow_repair(runtime, accepted, test):
    """Link archived publication evidence to its original isolated test."""
    if test.get("status") not in {"completed", "failed"}:
        return None
    task_id = test["task_id"]
    matches = [row for row in accepted if isinstance(row, dict) and row.get("id") == task_id]
    if len(matches) != 1:
        return None
    published = matches[0]
    head, accepted_at = published.get("head_sha"), _iso(published.get("accepted_at"))
    if not accepted_at or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", str(head or "")):
        return None
    directory = _confined(runtime, runtime / "tasks" / task_id)
    archive = _object(_confined(directory, directory / "accepted.json"), missing={}, maximum=65536)
    if not archive or any(archive.get(key) != published.get(key) for key in
                          ("id", "title", "head_sha", "base_sha", "accepted_at", "publication")):
        return None
    task = _object(_confined(directory, directory / "completed-task.json"), missing={}, maximum=512 * 1024)
    if task.get("id") != task_id or (task.get("candidate") or {}).get("head_sha") != head:
        return None
    repair = _user_repair_context(runtime, task)
    if not repair:
        return None
    profile = repair["public"]
    if (profile["source_full_flow_run"] != test["id"] or profile["source_head"] != test["head_sha"] or
            task.get("coding_model") != profile["model"] or task.get("coding_reasoning_effort") != profile["effort"]):
        return None
    reworks = task.get("reworks")
    if type(reworks) is not int or not 0 <= reworks <= 10000:
        return None
    review = _object(_confined(directory, directory / f"review-{reworks}.json"), missing={}, maximum=256 * 1024)
    if review.get("head_sha") != head or review.get("decision") != "accepted":
        return None
    checks = [row for row in task.get("check_results", []) if isinstance(row, dict) and row.get("head_sha") == head]
    mandatory = [row for row in checks if row.get("id") == "ue-full-flow-repair"]
    if len(mandatory) != 1 or mandatory[0].get("passed") is not True:
        return None
    stdout = _confined(runtime, Path(mandatory[0]["stdout_path"]))
    output = stdout.parent
    if stdout.name != "stdout.log" or output.parent != runtime / "runs" or not re.fullmatch(r"check-[a-f0-9]{32}", output.name):
        return None

    # Publication already reverified the complete evidence. Read only bounded
    # archived receipts here, without using the current workspace's mirror.
    parent = _confined(directory, directory / "full-repair-checks")
    with os.scandir(parent) as entries:
        remaining_bytes = _FULL_FLOW_RECORD_BYTES
        for index, entry in enumerate(entries):
            if index >= 64:
                break
            if not re.fullmatch(r"[a-f0-9]{32}", entry.name) or not entry.is_dir(follow_symlinks=False):
                continue
            try:
                path = _confined(parent, Path(entry.path) / "receipt.json")
                size = path.stat().st_size
                if not 0 < size <= remaining_bytes:
                    continue
                remaining_bytes -= size
                receipt = _object(path, maximum=size)
            except (OSError, ValueError, RecursionError):
                continue
            identity = {"version": 1, "id": entry.name, "task_id": task_id, "authorization_id": profile["id"],
                        "request_sha256": profile["request_sha256"], "head_sha": head, "scope": "user_repair_check", "status": "passed"}
            if any(receipt.get(key) != value for key, value in identity.items()) or receipt.get("passed") is not True:
                continue
            operation = receipt.get("operation")
            expected_operation = {"kind": "check", "task_id": task_id, "head_sha": head,
                                  "check_id": "ue-full-flow-repair", "output_dir": str(output)}
            if not isinstance(operation, dict) or any(operation.get(key) != value for key, value in expected_operation.items()):
                continue
            finished_at = _iso(receipt.get("finished_at"))
            if not finished_at or datetime.fromisoformat(finished_at) > datetime.fromisoformat(accepted_at):
                continue
            return {"task_id": task_id, "head_sha": head, "accepted_at": accepted_at,
                    "model": profile["model"], "effort": profile["effort"],
                    "passed_checks": sum(row.get("passed") is True for row in checks), "total_checks": len(checks)}
    return None


def collect_snapshot(config_path: Path) -> dict:
    """Project current local evidence without locks, writes, COM, or agent calls.

    Missing state means not initialized. Malformed/incompatible state is an error,
    while missing/stale log output never implies the worker has died.
    """
    now = _now()
    snapshot = _empty(now)
    try:
        config_path = Path(config_path).expanduser().resolve()
        config = load_config(config_path)
        from .dashboard_art import collect_art_snapshot
        snapshot["art"] = collect_art_snapshot(config, now)
        runtime = Path(config["runtime_dir"])
        publication = config.get("publication", {})
        snapshot["project"].update(name=config.get("name") or Path(config["project_path"]).name,
                                   source=config["project_path"], workspace=str(runtime / "workspace"),
                                   branch=config["branch"], milestone=publication.get("milestone_id"))
        snapshot["schedule"].update({key: config["schedule"].get(key, snapshot["schedule"][key])
                                     for key in ("weekday_start", "weekday_end", "weekends_all_day")})
        snapshot["schedule"].update(WorkCalendar(config["schedule"], base_dir=config_path.parent).describe(now))
        coding_provider = config.get("coding_provider", "dsh")
        coding_name = _CODING_NAMES[coding_provider]
        planning_provider = config.get("planning_provider", "codex")
        planning_name = "Claude Code" if planning_provider == "claude" else "Codex"
        snapshot["coding_provider"] = coding_provider
        snapshot["planning_provider"] = planning_provider
        snapshot["models"] = {
            role: {"name": name, "model": _clean(config[role].get("model"), 120) or "配置默认模型",
                   "effort": _clean(config[role].get("reasoning_effort"), 40) or "默认"}
            for role, name in (("codex", "Codex"), (coding_provider, coding_name), (planning_provider, planning_name))
        }
        codex = config["codex"]
        policy = codex.get("auth_mode", "inherit")
        snapshot["codex_auth"]["policy"] = "api_then_subscription" if codex.get("subscription_fallback") is True else policy
        snapshot["check_progress"]["total"] = len(config["checks"])
        snapshot["worker"] = _worker(runtime, config_path, now)
        state = _object(_confined(runtime, runtime / "state.json"), missing=None)
        try:
            snapshot["full_flow_test"] = _full_flow_snapshot(config, state or {})
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            snapshot["full_flow_test"] = None
            snapshot["full_flow_error"] = "全流程测试记录暂时无法核实，请刷新后核对。"
        if state is None:
            snapshot["status"].update(code="not_initialized", label="尚未生成断点", summary="等待执行器创建运行状态。")
            _workflow(snapshot, config, {}, {})
            return snapshot
        phase = state.get("phase")
        if state.get("version") != 1 or phase not in _PHASES:
            raise ValueError("断点版本或执行阶段无效")
        # BLOCKED retains the failed operation. Use its original phase only for
        # matching evidence; the visible status must remain blocked.
        operation_phase = state.get("blocked_from") if phase == "BLOCKED" else phase
        if not isinstance(operation_phase, str) or operation_phase not in _PHASES:
            operation_phase = phase
        # Match Engine's binding without constructing an Engine or provider.
        values = {key: config.get(key) for key in ("project_path", "goal", "branch", "base_ref", "checks", "push", "remote", "task_prefix")}
        if "publication" in config:
            values["publication"] = publication
        binding = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if state.get("binding") != binding:
            raise ValueError("运行断点与当前项目、目标或配置不匹配")
        task = state.get("task")
        operation = state.get("operation") or {}
        accepted = state.get("accepted", [])
        deferred = state.get("deferred", [])
        if (task is not None and not isinstance(task, dict)) or not isinstance(operation, dict) or not isinstance(accepted, list):
            raise ValueError("断点任务或操作结构无效")
        full_flow = snapshot.get("full_flow_test")
        if full_flow:
            try:
                accepted_repair = _accepted_full_flow_repair(runtime, accepted, full_flow)
                if accepted_repair:
                    full_flow["accepted_repair"] = accepted_repair
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
                pass
        try:
            from .dashboard_art import _reload_maintenance
            snapshot["maintenance"] = _reload_maintenance(config, state, lane="development")
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            snapshot["maintenance"] = None
        retry_error = None
        if operation_phase == "REPLAN":
            planning_name = "Claude Code"
        elif operation.get("kind") == "plan":
            planning_name = "Claude Code" if operation.get("planning_provider", "codex") == "claude" else "Codex"
        selection = operation if operation.get("planning_model") else state.get("planning_selection") or {}
        if phase in {"PLAN", "REPLAN"} and planning_name == "Claude Code" and selection.get("planning_model"):
            snapshot["models"].setdefault("claude", {"name": "Claude Code"}).update(
                model=_clean(selection["planning_model"], 120),
                effort=_clean(selection.get("planning_reasoning_effort"), 40) or "默认")
            snapshot["planning_selection"] = {
                "model": _clean(selection["planning_model"], 120),
                "level": _clean(selection.get("planning_level"), 40),
            }
        try:
            snapshot["provider_retry"] = _provider_retry(state, coding_name, planning_name)
        except (ValueError, TypeError):
            retry_error = "服务重试记录无效，请核对运行断点。"
        retry = snapshot["provider_retry"]
        if not isinstance(deferred, list):
            raise ValueError("待手动确认事项结构无效")
        for record in deferred[-100:]:
            if not isinstance(record, dict):
                raise ValueError("待手动确认事项结构无效")
            identity = _clean(record.get("id"), 101)
            snapshot["deferred"].append({
                "id": identity, "title": _clean(record.get("title"), 300) or identity or "未命名事项",
                "head_sha": _clean(record.get("head_sha"), 64), "base_sha": _clean(record.get("base_sha"), 64),
                "reason": _prose(record.get("reason")), "deferred_at": _iso(record.get("deferred_at")),
                "status": "pending_manual_confirmation" if record.get("status") == "pending_manual_confirmation" else "unknown",
            })
        label, actor = _PHASES[phase]
        if phase == "CODE":
            actor = coding_name
        elif phase in {"PLAN", "REPLAN"}:
            actor = planning_name
        if "check" in str(operation.get("kind", "")).lower():
            actor = "UE/CHECK"
        status = snapshot["status"]
        status.update(code=phase.lower(), label=label, phase=phase, actor=actor,
                      paused=(runtime / "PAUSE").exists(), failures=_number(state.get("failures")),
                      error=_clean(state.get("last_error")) or None, started_at=_iso(operation.get("started_at")))
        status["provider_actor"] = retry["provider"] if retry else None
        if retry or retry_error:
            status["error"] = retry_error
        if task:
            snapshot["task"] = {key: task.get(key) for key in ("id", "title", "body", "acceptance_criteria", "reworks", "base_sha")}
            snapshot["task"]["title"] = task.get("title") or task.get("id") or "未命名任务"
            snapshot["task"]["head_sha"] = (task.get("candidate") or {}).get("head_sha")
            task_provider = task.get("coding_provider", "dsh")
            session, owner = _task_session(runtime, task, operation, phase, task_provider)
            snapshot["task"].update(session_id=session, session_owner_task_id=owner,
                                    session_provider=_CODING_NAMES[task_provider])
            try:
                repair = _user_repair_context(runtime, task)
                if repair:
                    snapshot["task"]["user_repair"] = repair["public"]
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                snapshot["task"]["user_repair_error"] = "本批用户修复授权或计划待核实，请查看当前任务证据。"
            try:
                followup = _user_followup_context(runtime, task, operation)
                if followup:
                    snapshot["task"]["user_followup"] = followup["public"]
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                snapshot["task"]["user_followup_error"] = "UE MCP 交接或实际连接证据待核实。"
            source, _ = _planning_source(runtime, task.get("planning_source_output_dir"))
            snapshot["task"]["planning"] = {
                "level": task.get("planning_level") if task.get("planning_level") in {"routine", "critical"} else None,
                "model": _clean(task.get("planning_model"), 120) or None,
                "effort": _clean(task.get("planning_reasoning_effort"), 40) or None,
                "reason": _clean(task.get("planning_reason"), 600), "source": source,
            }
        check_owner = state.get("milestone", {}) if operation_phase.startswith("MILESTONE") else (task or {})
        results = check_owner.get("check_results", [])
        if not isinstance(results, list):
            raise ValueError("检查结果结构无效")
        snapshot["checks"] = [{"id": _clean(row.get("id"), 120), "passed": row.get("passed") if type(row.get("passed")) is bool else None,
                               "exit_code": row.get("exit_code") if type(row.get("exit_code")) is int else None,
                               "status": "not_run" if row.get("status") == "NOT_RUN" else None}
                              for row in results if isinstance(row, dict)]
        for record in accepted[-100:]:
            if not isinstance(record, dict):
                raise ValueError("已验收任务结构无效")
            detail = {}
            task_id = record.get("id", "")
            if isinstance(task_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", task_id):
                detail = _object(_confined(runtime, runtime / "tasks" / task_id / "completed-task.json"), missing={}, maximum=512 * 1024)
                reworks = detail.get("reworks")
                if type(reworks) is int and 0 <= reworks <= 20:
                    detail["review"] = _object(_confined(runtime, runtime / "tasks" / task_id / f"review-{reworks}.json"),
                                               missing={}, maximum=65536)
            snapshot["accepted"].append({"id": _clean(task_id, 101), "title": _clean(record.get("title") or task_id or "未命名任务", 300),
                "sha": record.get("head_sha"), "accepted_at": _iso(record.get("accepted_at")), "reworks": detail.get("reworks", record.get("reworks")),
                "review_summary": _clean((detail.get("review") or {}).get("summary", record.get("review_summary")))})
        kind = operation.get("kind")
        task_id = (task or {}).get("id")
        owns_operation = operation.get("task_id", task_id) == task_id
        if operation_phase == "REPLAN":
            source = (((task or {}).get("replanning") or {}).get("fingerprint") or {})
            head = source.get("head_sha")
            owns_operation = (owns_operation and operation.get("task_id") == task_id and bool(head) and
                              operation.get("head_sha") == head and operation.get("planning_provider") == "claude" and
                              operation.get("planning_level") == "critical" and
                              ((((task or {}).get("replanning") or {}).get("source_phase") == "CODE") or
                               head in {((task or {}).get("candidate") or {}).get("head_sha"), (task or {}).get("code_base"), state.get("expected_head")}))
        check_kind = {"CHECK": "check", "MILESTONE_REVIEW": "milestone_check"}.get(operation_phase)
        configured_checks = config["checks"]
        if (snapshot.get("task") or {}).get("user_repair") and not operation_phase.startswith("MILESTONE"):
            if not any(row.get("id") == "ue-full-flow-repair" for row in configured_checks):
                configured_checks = configured_checks + [{"id": "ue-full-flow-repair"}]
            snapshot["check_progress"]["total"] = len(configured_checks)
        if operation_phase.startswith("MILESTONE"):
            configured_checks = configured_checks + config.get("milestone_checks", [])
            snapshot["check_progress"]["total"] = len(configured_checks)
        check_id = None
        if owns_operation and check_kind and kind == check_kind and len(results) < len(configured_checks):
            check_id = configured_checks[len(results)]["id"]
        snapshot["check_progress"]["current_id"] = check_id
        directory = _operation_directory(runtime, operation)
        if operation_phase == "REPLAN" and (not owns_operation or not _replanning_model(runtime, operation, directory)[0]):
            status["started_at"] = None
            if not owns_operation:
                directory = None
        codex_kind = {"PLAN": "plan", "REPLAN": "plan", "REVIEW": "review", "MILESTONE_REVIEW": "review"}.get(operation_phase)
        codex_operation = kind == "review" or (kind == "plan" and planning_name == "Codex")
        if directory and owns_operation and codex_kind and kind == codex_kind and codex_operation:
            mode, directory = _codex_route(runtime, directory, codex_kind)
            snapshot["codex_auth"].update(current=mode, fallback=mode == "chatgpt")
        matches_operation = owns_operation and kind is not None and kind in {
            codex_kind, check_kind, "code" if operation_phase == "CODE" else None}
        finished_at = (_attempt_finished_at(runtime, directory, operation, check_id,
                                            status["started_at"], state.get("updated_at"))
                       if matches_operation
                       else None)
        activity_actor = _CODING_NAMES[operation.get("coding_provider", "dsh")] if kind == "code" else actor
        snapshot["activity"], last_log = _activity(runtime, directory, kind, activity_actor, check_id)
        activity_at = last_log or _iso(state.get("updated_at"))
        clock_running = (matches_operation and phase not in {"BLOCKED", "DONE"}
                         and snapshot["worker"]["alive"] and snapshot["worker"]["healthy"])
        status.update(attempt_finished=finished_at is not None,
                      elapsed_seconds=(_age(status["started_at"], min(finished_at, now) if finished_at else now)
                                       if finished_at is not None or clock_running else None),
                      last_activity_at=activity_at,
                      last_activity_age_seconds=_age(activity_at, now))
        burst = _object(_confined(runtime, runtime / "burst.json"), missing=None, maximum=256 * 1024)
        if burst:
            snapshot["burst"] = {key: burst.get(key) for key in ("status", "completed_tasks", "requested_tasks", "remaining_tasks")}
            snapshot["burst"]["ignore_window"] = burst.get("ignore_window") is True
        historical_ids = {task_id for entry in state.get("milestone_history", []) if isinstance(entry, dict)
                          for task_id in entry.get("accepted_task_ids", []) if isinstance(task_id, str)}
        stage_id = publication.get("milestone_id")
        pending = sum(1 for row in accepted if isinstance(row, dict) and isinstance(row.get("publication"), dict)
                      and row["publication"].get("local_only") is True and not row.get("milestone_publication")
                      and (not stage_id or row["publication"].get("deferred_to_milestone") == stage_id or
                           (row["publication"].get("deferred_to_milestone") is None and row.get("id") not in historical_ids)))
        snapshot["publication"].update(mode=publication.get("mode", "task"), milestone_id=publication.get("milestone_id"),
            pending_tasks=pending, summary="里程碑独立验收通过后统一发布" if publication else "逐任务发布" if config["push"] else "仅本地提交")
        if status["paused"]:
            status.update(code="paused", label="已请求暂停")
        elif phase not in {"DONE", "BLOCKED"}:
            if not snapshot["worker"]["alive"]:
                status.update(code="stopped", label="后台已停止")
            elif not snapshot["worker"]["healthy"]:
                status.update(code="heartbeat_stale", label="后台心跳待核实")
            elif not snapshot["schedule"]["allowed"] and not (burst and burst.get("ignore_window") is True and burst.get("status") in {"running", "retrying"}):
                status.update(code="waiting_window", label="等待工作窗口")
            elif snapshot["worker"]["recovery"]:
                status.update(code="waiting_filesystem", label="等待状态文件恢复")
            elif retry and retry["status"] == "waiting":
                wait_label = {"local_cli_unavailable": f"等待本地 {retry['provider']} 恢复",
                              "claude_subscription_limit": "等待 Claude 额度恢复",
                              "codex_subscription_limit": "等待 Codex 额度恢复"}.get(retry["reason"], "等待服务恢复")
                status.update(code="waiting_provider", label=wait_label)
        if retry_error:
            snapshot["error"] = retry_error
            if status["code"] not in {"paused", "blocked", "done", "stopped", "heartbeat_stale", "waiting_window", "waiting_filesystem"}:
                status.update(code="error", label="重试状态待核实", actor="系统")
        status["summary"] = status["error"] or ("正在规划下一项任务，任务书尚未生成。" if phase == "PLAN" else label)
        if phase == "CODE" and (snapshot.get("task") or {}).get("user_repair") and not status["error"]:
            status["summary"] = "Opus修复全流程问题；本批修改仍需独立检查与审核。"
            followup = (snapshot.get("task") or {}).get("user_followup")
            if followup and followup.get("turn") and followup["status"] == "active":
                status["summary"] = followup["summary"] + "；保留原任务独立检查与审核。"
        if phase == "REPLAN" and not status["error"]:
            status["summary"] = _replanning_summary(runtime, task or {})
        if status["code"] in {"waiting_window", "stopped", "heartbeat_stale", "paused", "waiting_provider", "waiting_filesystem"}:
            status["actor"] = "系统"
        if status["code"] == "waiting_filesystem":
            recovery = snapshot["worker"]["recovery"]
            next_retry = datetime.fromisoformat(recovery["next_retry_at"]).astimezone(BEIJING).strftime("%m-%d %H:%M:%S")
            status["error"] = None
            status["provider_actor"] = None
            status["summary"] = ("文件暂被占用，自动重试；第 " + str(recovery["attempt"]) +
                                 " 次恢复计划于 " + next_retry + "（北京时间），原任务断点保留。")
        elif status["code"] == "waiting_provider":
            next_retry = datetime.fromisoformat(retry["next_retry_at"]).astimezone(BEIJING).strftime("%m-%d %H:%M:%S")
            status["summary"] = (retry["provider"] + " · " + retry["reason_label"] + "；第 " + str(retry["attempt"]) +
                                 " 次自动重试计划于 " + next_retry + "（北京时间）。")
        elif status["code"] == "stopped":
            status["summary"] = "未确认本配置后台进程存活；断点保留在" + label + "阶段。"
        elif status["code"] == "waiting_window":
            status["summary"] = "当前在原排期工作窗口之外；等待下一窗口 " + str(snapshot["schedule"]["next_open"] or "（时间待核实）") + "，断点保留在" + label + "阶段。"
        elif status["code"] == "heartbeat_stale":
            status["summary"] = "后台进程仍存活，但心跳状态待核实；当前活动无法确认，断点保留在" + label + "阶段。"
        elif status["code"] == "paused":
            maintenance = snapshot.get("maintenance")
            status["summary"] = (maintenance["summary"] if maintenance else
                "暂停标记已设置；" + ("后台仍存活，当前操作可能尚未结束。" if snapshot["worker"]["alive"] else "任务断点已保留。"))
            if maintenance:
                status["label"] = maintenance["label"]
        resuming = status["code"] == "code" or status["code"] == "paused" and (snapshot.get("maintenance") or {}).get("current_call_active") is True
        if (phase == "CODE" and resuming and status["error"] and not snapshot["error"] and
                snapshot["worker"]["alive"] and snapshot["worker"]["healthy"] and
                _native_code_active(runtime, task, operation, directory)):
            previous_error = status["error"]
            status["previous_error"] = previous_error
            status["error"] = None
            if status["code"] == "code":
                status["summary"] = ("上次调用超时，当前已继续；正在执行本轮开发与验证。" if "time limit" in previous_error.lower() or "超时" in previous_error else
                                     "上次调用的异常已保留，当前调用已继续执行。")
        _workflow(snapshot, config, state, operation, directory)
        return snapshot
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        message = "无法完整读取运行状态：" + _clean(str(error), 300)
        snapshot["error"] = message
        snapshot["status"].update(code="error", label="状态读取失败", summary=message, error=message)
        return snapshot
