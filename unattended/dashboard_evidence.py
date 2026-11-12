"""Read-only, task-bound node evidence. No arbitrary path or provider transcript API."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode

from .config import load_config
from .dashboard_art import worker_config, _state as art_state, _package
from .dashboard_state import _clean, _confined, _object, _claude_event, _codex_route, _iso
from .dashboard_flows import NODE_IDS

MAX_TEXT = 64 * 1024
MAX_BINARY = 128 * 1024 * 1024
MAX_PNG = 16 * 1024 * 1024
SHA = re.compile(r"[0-9a-f]{64}")
TASK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
NODES = {
    "development": dict(routine_plan="常规规划", critical_plan="关键规划", code="编写代码", check="独立检查",
                        review="技术审核", publish="交付任务", milestone_review="阶段审核", milestone_publish="阶段发布"),
    "art": dict(queue="任务与制作编排", concept="概念图", model="源模型", blender="Blender 制作",
                ue_import="UE 导入", ue_capture="UE 展示图", check="资产独立检查", review="美术技术审核",
                approval="用户视觉验收", promotion="批准资产入库"),
    "operations": dict(development_worker="功能开发后台", art_worker="美术后台", notifications="通知服务"),
}


def redact(text, limit=MAX_TEXT):
    text = _clean(text, limit * 2)
    text = re.sub(r"https?://[^\s<>\"']+", "[链接已隐藏]", text, flags=re.I)
    text = re.sub(r"(?i)(?:[A-Z]:[\\/]|/)[^\s\"'<>]*(?:private|secrets|credentials)[\\/][^\s\"'<>]*", "[私有路径已隐藏]", text)
    return text[-limit:]


def _public(value, depth=0):
    if depth > 12:
        return "[内容过深]"
    if isinstance(value, dict):
        return {str(k): _public(v, depth + 1) for k, v in list(value.items())[:200]
                if not re.search(r"(?i)(secret|password|token|authorization|api.?key|key_file|private|environment|webhook|prompt|messages|^argv$|config_file)", str(k))}
    if isinstance(value, list):
        return [_public(v, depth + 1) for v in value[:200]]
    if isinstance(value, str):
        return redact(value, 8000)
    return value if value is None or type(value) in (bool, int, float) else None


def _json_bytes(value):
    return json.dumps(_public(value), ensure_ascii=False, indent=2).encode("utf-8")[:MAX_TEXT]


def _state(config):
    runtime = Path(config["runtime_dir"])
    state = _object(_confined(runtime, runtime / "state.json"), missing={})
    if state:
        values = {k: config.get(k) for k in ("project_path", "goal", "branch", "base_ref", "checks", "push", "remote", "task_prefix")}
        if "publication" in config:
            values["publication"] = config["publication"]
        binding = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if state.get("version") != 1 or state.get("binding") != binding:
            raise ValueError("功能断点与配置不匹配")
    return state


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Evidence:
    def __init__(self, lane, node, scope, *, maximum_members=80):
        self.lane, self.node, self.scope = lane, node, scope
        self.maximum_members = maximum_members
        self.members = {}
        self.logs, self.artifacts = [], []

    def add(self, root, path, title, *, mode="text", expected=None, log=False):
        path = _confined(root, path)
        try:
            stat = path.stat()
        except FileNotFoundError:
            return
        if not path.is_file() or len(self.members) >= self.maximum_members:
            return
        relative = path.relative_to(Path(root).absolute()).as_posix()
        if any(part.lower() in {"private", "secrets", ".claude", ".git"} for part in path.parts):
            raise ValueError("证据路径不属于公开产物")
        if mode in {"png", "binary"} and (not SHA.fullmatch(str(expected or "")) or stat.st_size > (MAX_PNG if mode == "png" else MAX_BINARY)):
            return
        # Appending to an active log keeps its URL valid. Replacing the file or
        # changing task/operation/package invalidates it. Immutable artifacts use SHA.
        identity = [stat.st_dev, stat.st_ino]
        if not log:
            identity += [expected] if expected else [stat.st_size, stat.st_mtime_ns]
        ref = hashlib.sha256(json.dumps([self.scope, str(Path(root).absolute()), relative, mode, identity], sort_keys=True).encode()).hexdigest()
        if ref in self.members:
            return
        self.members[ref] = {"root": Path(root), "path": path, "mode": mode, "expected": expected,
                             "identity": [stat.st_dev, stat.st_ino], "log": log}
        row = {"id": ref, "title": redact(title, 160), "kind": mode, "size_bytes": stat.st_size,
               "url": "/api/flow/evidence?" + urlencode({"lane": self.lane, "node": self.node, "ref": ref})}
        (self.logs if log else self.artifacts).append(row)

    def virtual(self, title, value, *, log=False):
        data = _json_bytes(value)
        ref = hashlib.sha256(json.dumps(self.scope, sort_keys=True).encode() + title.encode() + data).hexdigest()
        self.members[ref] = {"data": data}
        row = {"id": ref, "title": title, "kind": "json", "size_bytes": len(data),
               "url": "/api/flow/evidence?" + urlencode({"lane": self.lane, "node": self.node, "ref": ref})}
        (self.logs if log else self.artifacts).append(row)


def _provider_files(evidence, runtime, directory, *, title="当前操作"):
    directory = _confined(runtime, Path(directory))
    if directory.parent != runtime / "runs" or not re.fullmatch(r"(?:code|plan|review|check|art_check|art_diagnose|milestone_check)-[a-f0-9]{32}", directory.name):
        raise ValueError("模型操作目录不匹配")
    kind = directory.name.split("-", 1)[0]
    route, selected = _codex_route(runtime, directory, kind) if kind in {"plan", "review"} else (None, directory)
    attempts = [(directory, title)] if selected == directory else [
        (directory, title + " · API 首次尝试（额度失败）"), (selected, title + " · Codex 订阅尝试")]
    for attempt, label in attempts:
        for name in ("stdout.log", "stderr.log"):
            # Provider stdout is event-label projection only, never native chat.
            evidence.add(runtime, attempt / name, label + (" · 事件摘要" if name == "stdout.log" else " · 错误日志"),
                         mode="events" if name == "stdout.log" else "errors", log=True)
        process = _object(_confined(runtime, attempt / "process.json"), missing={}, maximum=65536)
        if process:
            keys = ("kind", "exit_code", "timed_out", "output_limit_exceeded", "launch_or_capture_failed", "launch_error",
                    "coding_provider", "coding_model", "coding_reasoning_effort", "planning_provider", "planning_model",
                    "planning_reasoning_effort", "diagnosis_provider", "diagnosis_model", "diagnosis_reasoning_effort",
                    "model_selection", "retry_reason", "transient", "codex_auth_mode")
            evidence.virtual(label + " · 执行回执", {key: process[key] for key in keys if key in process})


def _diagnosis_result(value, head):
    """Publish the structured work product, never provider envelopes or native chat."""
    if (not isinstance(value, dict) or value.get("head_sha") != head or
            not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", str(head or "")) or
            value.get("action") not in {"continue", "needs_user", "blocked"}):
        return None
    prose = ("diagnosis", "plan", "reason")
    lists = ("required_approvals", "questions")
    if (any(not isinstance(value.get(key), str) for key in prose) or
            any(not isinstance(value.get(key), list) or len(value[key]) > 100 or
                any(not isinstance(item, str) for item in value[key]) for key in lists) or
            type(value.get("additional_tripo_credits")) is not int or value["additional_tripo_credits"] < 0):
        return None
    return {key: value[key] for key in ("action", *prose, *lists, "additional_tripo_credits", "head_sha")}


def _replanning_result(value):
    if (not isinstance(value, dict) or value.get("action") not in {"task", "complete", "blocked"} or
            any(not isinstance(value.get(key), str) for key in ("title", "body", "reason")) or
            not isinstance(value.get("acceptance_criteria"), list) or
            any(not isinstance(item, str) for item in value["acceptance_criteria"])):
        return None
    result = {key: value[key] for key in ("action", "title", "body", "acceptance_criteria", "reason")}
    if isinstance(value.get("core_tests"), list) and all(isinstance(item, str) for item in value["core_tests"]):
        result["core_tests"] = value["core_tests"]
    return result


def _replanning_files(evidence, runtime, state, task, head, operation):
    phase = state.get("blocked_from") if state.get("phase") == "BLOCKED" else state.get("phase")
    pending = task.get("replanning") or {}
    source = pending.get("fingerprint") or {}
    if phase == "REPLAN" and operation.get("kind") == "plan" and operation.get("task_id") == task.get("id"):
        if (not head or operation.get("head_sha") != head or not isinstance(source, dict) or source.get("head_sha") != head or
                operation.get("planning_provider") != "claude" or operation.get("planning_level") != "critical"):
            raise ValueError("重新分析计划的任务、HEAD 或规划操作不匹配")
        directory = _confined(runtime, Path(operation["output_dir"]))
        if directory.parent != runtime / "runs" or not re.fullmatch(r"plan-[a-f0-9]{32}", directory.name):
            raise ValueError("重新分析计划的操作目录不匹配")
        from_code = pending.get("source_phase") == "CODE"
        evidence.virtual("本轮重规划触发原因（开发调用与历史证据）" if from_code else "本轮重规划触发原因（上次审核意见）", {
            "task_id": task["id"], "head_sha": head, "round": pending.get("round"), "trigger": pending.get("trigger"),
            "source_phase": pending.get("source_phase"), "feedback": pending.get("feedback"),
            "previous_review_head_sha": source.get("review_head_sha") if from_code else None,
            "review_scope": "历史审核仅供参考，不代表当前工作区改动已被审核。" if from_code else "本轮审核意见",
            "wip_sha256": source.get("wip_sha256") if from_code else None})
        if from_code and type(source.get("review_round")) is int and 0 <= source["review_round"] <= 10000:
            path = _confined(runtime, runtime / "tasks" / task["id"] / ("review-" + str(source["review_round"]) + ".json"))
            if path.is_file() and _digest(path) == source.get("review_sha256"):
                review = _object(path, missing={}, maximum=256 * 1024)
                if review.get("head_sha") == source.get("review_head_sha", head):
                    evidence.virtual("历史审核回执（不代表当前 WIP 已通过）", {
                        "task_id": task["id"], "review_round": source["review_round"], "head_sha": review["head_sha"],
                        "scope": "这是先前版本的审核；当前工作区改动仍需独立检查与审核。",
                        "decision": review.get("decision"), "summary": review.get("summary"), "feedback": review.get("feedback")})
        _provider_files(evidence, runtime, directory, title="当前 Claude 只读修复规划")
        result = _replanning_result(_object(_confined(runtime, directory / "result.json"), missing={}, maximum=256 * 1024))
        if result:
            evidence.virtual("本轮重新分析修复计划（仍需开发、检查与审核）", {
                "task_id": task["id"], "head_sha": head, "round": pending.get("round"),
                "scope_note": "计划中的标题、验证步骤和测试建议不替换原任务、原验收条件或核心测试。", "result": result})
    history = task.get("replanning_history")
    if not isinstance(history, list):
        return
    for entry in history[-3:]:
        if (not isinstance(entry, dict) or entry.get("task_id") != task.get("id") or type(entry.get("round")) is not int or
                entry["round"] < 1 or not isinstance(entry.get("source_fingerprint"), dict)):
            continue
        source = entry["source_fingerprint"]
        if source.get("head_sha") != entry.get("head_sha"):
            continue
        receipt_path = _confined(runtime, runtime / "tasks" / task["id"] / ("replan-" + str(entry["round"]) + ".json"))
        if Path(entry.get("receipt_path", "")).absolute() != receipt_path:
            continue
        receipt = _object(receipt_path, missing={}, maximum=512 * 1024)
        if receipt != entry:
            continue
        result = _replanning_result(entry.get("result"))
        if not result:
            continue
        directory = _confined(runtime, Path(entry.get("output_dir", "")))
        if directory.parent != runtime / "runs" or not re.fullmatch(r"plan-[a-f0-9]{32}", directory.name):
            continue
        if phase == "REPLAN" and str(directory) == operation.get("output_dir"):
            continue
        child = Evidence(evidence.lane, evidence.node, {**evidence.scope, "replan_round": entry["round"],
                         "replan_head": entry["head_sha"], "replan_source": hashlib.sha256(_json_bytes(source)).hexdigest(),
                         "replan_receipt_sha256": _digest(receipt_path)})
        label = "历史修复规划 · 第 " + str(entry["round"]) + " 次 · HEAD " + str(entry["head_sha"])[:12]
        _provider_files(child, runtime, directory, title=label)
        child.virtual(label + " · 重新分析计划（不代表验收通过）", {
            "task_id": task["id"], "head_sha": entry["head_sha"], "round": entry["round"],
            "at": entry.get("at"), "execution_decision": entry.get("execution_decision"),
            "controller_reason": entry.get("controller_reason"),
            "scope_note": "这是原任务的修复计划，不是新任务书或新的验收标准。", "result": result})
        evidence.members.update(child.members); evidence.logs.extend(child.logs); evidence.artifacts.extend(child.artifacts)


def _diagnosis_operation_files(evidence, runtime, op, task, head):
    if op.get("kind") != "art_diagnose" or op.get("task_id") != task.get("id"):
        return
    if op.get("head_sha") != head or not head:
        raise ValueError("自主诊断操作的 HEAD 不匹配")
    directory = _confined(runtime, Path(op["output_dir"]))
    if directory.parent != runtime / "runs" or not re.fullmatch(r"art_diagnose-[a-f0-9]{32}", directory.name):
        raise ValueError("自主诊断操作目录不匹配")
    receipt = _object(_confined(runtime, directory / "process.json"), missing={}, maximum=65536)
    if receipt and (receipt.get("kind") != "art_diagnose" or receipt.get("task_id") != task.get("id") or receipt.get("head_sha") != head):
        raise ValueError("自主诊断执行回执不属于当前任务或 HEAD")
    _provider_files(evidence, runtime, directory, title="当前 Claude 只读诊断分支")
    result = _diagnosis_result(_object(_confined(runtime, directory / "result.json"), missing={}, maximum=256 * 1024), head)
    if result:
        pending = task.get("art_diagnosis") or {}
        evidence.virtual("本轮自主诊断与调整计划（不代表制作或验收通过）", {
            "round": pending.get("round"), "trigger": pending.get("trigger"),
            "source_fingerprint": pending.get("fingerprint"), "result": result})


def _diagnosis_history_files(evidence, runtime, task, current_op):
    history = task.get("art_diagnosis_history")
    if not isinstance(history, list):
        return
    # Keep the most recent bounded receipts discoverable after CODE resumes.
    for entry in history[-3:]:
        if not isinstance(entry, dict) or type(entry.get("round")) is not int or entry["round"] < 0:
            continue
        fingerprint = entry.get("source_fingerprint")
        source = fingerprint if isinstance(fingerprint, dict) else None
        if source is not None:
            fingerprint = hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if not SHA.fullmatch(str(fingerprint or "")):
            continue
        value = entry.get("result") or entry.get("diagnosis")
        result = _diagnosis_result(value, value.get("head_sha") if isinstance(value, dict) else None)
        if not result or source is not None and source.get("head_sha") != result["head_sha"]:
            continue
        raw = entry.get("output_dir")
        if not isinstance(raw, str) or raw == current_op.get("output_dir"):
            continue
        directory = _confined(runtime, Path(raw))
        if directory.parent != runtime / "runs" or not re.fullmatch(r"art_diagnose-[a-f0-9]{32}", directory.name):
            raise ValueError("历史诊断操作目录不匹配")
        receipt = _object(_confined(runtime, directory / "process.json"), missing={}, maximum=65536)
        if (receipt.get("kind") != "art_diagnose" or receipt.get("task_id") != task.get("id") or
                receipt.get("head_sha") != result["head_sha"]):
            continue
        child = Evidence(evidence.lane, evidence.node, {**evidence.scope, "diagnosis_round": entry["round"],
                         "diagnosis_head": result["head_sha"], "source_fingerprint": fingerprint, "diagnosis_operation": raw})
        label = "历史自主诊断 · 第 " + str(entry["round"]) + " 轮 · HEAD " + result["head_sha"][:12]
        _provider_files(child, runtime, directory, title=label)
        child.virtual(label + " · 调整计划（不代表制作或验收通过）", {
            "round": entry["round"], "trigger": entry.get("trigger"), "at": entry.get("at"),
            "source_fingerprint": fingerprint, "result": result})
        evidence.members.update(child.members)
        evidence.logs.extend(child.logs)
        evidence.artifacts.extend(child.artifacts)


def _check_files(evidence, runtime, result, expected_head):
    if not isinstance(result, dict) or result.get("head_sha") != expected_head:
        return
    evidence.virtual("独立检查结果", {k: result.get(k) for k in ("id", "head_sha", "passed", "exit_code", "timed_out", "patterns")})
    for name in ("stdout_path", "stderr_path"):
        raw = result.get(name)
        if not isinstance(raw, str):
            continue
        path = _confined(runtime, Path(raw))
        if path.name not in {"stdout.log", "stderr.log"} or path.parent.parent != runtime / "runs":
            raise ValueError("独立检查日志路径不匹配")
        receipt = _object(_confined(runtime, path.parent / "check.json"), missing={}, maximum=65536)
        if receipt.get("id") == result.get("id") and receipt.get("head_sha") == expected_head:
            evidence.add(runtime, path, str(result.get("id", "检查")) + " · " + path.name, mode="text", log=True)


def _user_repair_files(evidence, runtime, task):
    from .dashboard_state import _user_repair_context
    context = _user_repair_context(runtime, task)
    if not context:
        return None
    profile, request = context["profile"], context["request"]
    evidence.virtual("本批用户修复授权（不替换原验收）", {
        "task_id": task["id"], **context["public"],
        "scope_note": "授权本批 Opus 修复全流程发现的问题；原任务验收与独立审核继续保留。",
        **{key: request[key] for key in ("authorized_at", "user_instruction", "reason", "feedback", "instructions") if key in request}})
    if context["plan"]:
        plan = context["plan"]
        if plan.suffix.lower() == ".json":
            value = _object(plan, missing={}, maximum=4 * 1024 * 1024)
            evidence.virtual("本批修复计划与报告", {"task_id": task["id"], "user_repair_id": profile["id"],
                "source_head": profile["source_head"], "plan_sha256": profile["plan_sha256"],
                "plan": value.get("answer") if isinstance(value.get("answer"), str) else _public(value),
                "scope_note": "修复计划不代表开发完成或独立验收通过。"})
        else:
            evidence.add(context["root"], plan, "本批修复计划与报告", expected=profile["plan_sha256"])
    return context


def _repair_check_member(relative):
    """Accept only wrapper outputs; never follow source or native directories."""
    if not isinstance(relative, str) or len(relative) > 600:
        return None
    value = relative.replace("\\", "/")
    parts = value.split("/")
    if any(part in {"", ".", ".."} or ":" in part for part in parts):
        return None
    if len(parts) == 1 and parts[0] in {"source-before.json", "source-after.json"}:
        return "source", None
    stage = parts[0]
    if stage not in {"build", "regression", "visual", "package"}:
        return None
    regular = {"stdout.log", "stderr.log", "Unreal.log", "process.json", "receipt.json"}
    if len(parts) == 2 and stage != "visual" and parts[1] in (regular | {"UnrealBuildTool.log"} if stage == "build" else regular):
        return stage, None
    if stage == "visual" and len(parts) == 3 and re.fullmatch(r"case-(?:00[1-9]|0[1-9][0-9]|[1-9][0-9]{2})", parts[1]) and parts[2] in regular:
        return stage, int(parts[1][5:])
    if (stage == "visual" and len(parts) == 4 and re.fullmatch(r"case-(?:00[1-9]|0[1-9][0-9]|[1-9][0-9]{2})", parts[1]) and
            parts[2] == "images" and parts[3].lower().endswith(".png") and not parts[3].startswith(".") and len(parts[3]) <= 200):
        return stage, int(parts[1][5:])
    package = {"package-summary.json", "plain.png", "loop.png", "plain.log", "loop.log", "loop-Slot97.zgs", "raw-package-summary.json", "raw-package-uat.log"}
    if stage == "package" and len(parts) == 3 and parts[1] == "package-evidence" and parts[2] in package:
        return stage, None
    return None


def _user_repair_check_files(evidence, runtime, task, head, context, current_operation):
    if not context:
        return []
    profile = context["profile"]
    notes = []
    choices = (("full-repair-results.json", {"user_repair_check"}, "独立全流程检查"),
               ("selftest-results.json", {"user_repair_selftest"}, "制作自测（不替代独立检查）"))
    for name, scopes, label in choices:
        try:
            mirror_path = _confined(runtime, runtime / "workspace/Saved/Automation" / task["id"] / name)
            mirror = _object(mirror_path, missing={}, maximum=1024 * 1024)
            if not mirror:
                continue
            raw_path = mirror.get("receipt_path")
            if not isinstance(raw_path, str) or not SHA.fullmatch(str(mirror.get("receipt_sha256") or "")):
                raise ValueError("镜像缺少回执绑定")
            root = _confined(runtime, runtime / "tasks" / task["id"] / "full-repair-checks")
            path = _confined(root, Path(raw_path))
            if path.name != "receipt.json" or path.parent.parent != root or not re.fullmatch(
                    r"[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", path.parent.name):
                raise ValueError("回执路径不属于本任务固定目录")
            with path.open("rb") as stream:
                raw = stream.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != mirror["receipt_sha256"]:
                raise ValueError("回执文件摘要不匹配")
            row = json.loads(raw.decode("utf-8-sig"))
            expected = {"version": 1, "id": path.parent.name, "task_id": task["id"], "authorization_id": profile["id"],
                        "request_sha256": profile["request_sha256"], "head_sha": head}
            if (not isinstance(row, dict) or any(row.get(key) != value for key, value in expected.items()) or
                    any(mirror.get(key) != row.get(key) for key in (*expected, "scope", "status", "passed", "updated_at")) or
                    row.get("scope") not in scopes or row.get("status") not in {"running", "passed", "failed"} or
                    type(row.get("passed")) is not bool or not _iso(row.get("started_at")) or not _iso(row.get("updated_at")) or
                    row.get("status") != "running" and not _iso(row.get("finished_at")) or
                    row.get("workspace") != str(runtime / "workspace") or not isinstance(row.get("engine_dir"), str) or
                    not Path(row["engine_dir"]).is_absolute()):
                raise ValueError("回执任务、授权、版本或检查范围不匹配")
            child = Evidence(evidence.lane, evidence.node, {**evidence.scope, "repair_check_id": row["id"],
                "repair_check_scope": row["scope"], "repair_check_receipt_sha256": mirror["receipt_sha256"]}, maximum_members=256)
            public = {key: value for key, value in row.items() if key != "operation"}
            operation = row.get("operation") if isinstance(row.get("operation"), dict) else {}
            public["operation"] = {key: operation[key] for key in ("kind", "task_id", "head_sha", "check_id", "coding_model",
                "coding_reasoning_effort", "user_repair_id", "user_repair_request_sha256", "started_at") if key in operation}
            if row["status"] == "running" and (operation != current_operation or operation.get("task_id") != task["id"] or
                    (row["scope"] == "user_repair_check" and (operation.get("kind") != "check" or operation.get("check_id") != "ue-full-flow-repair")) or
                    (row["scope"] == "user_repair_selftest" and (operation.get("kind") != "code" or operation.get("user_repair_id") != profile["id"] or
                        operation.get("user_repair_request_sha256") != profile["request_sha256"]))):
                raise ValueError("运行中回执不属于当前操作")
            child.virtual(label + " · 绑定回执（完成不等于通过）", public)
            stages = row.get("stages") if isinstance(row.get("stages"), dict) else {}
            records = []
            for key in ("images", "evidence"):
                if isinstance(row.get(key), list): records.extend(row[key][:512])
            seen = set()
            for member in records:
                if (not isinstance(member, dict) or not SHA.fullmatch(str(member.get("sha256") or "")) or
                        type(member.get("size_bytes")) is not int or not 0 <= member["size_bytes"] <= MAX_BINARY):
                    continue
                relative = member.get("relative_path")
                selected = _repair_check_member(relative)
                if not selected:
                    continue
                candidate = _confined(path.parent, path.parent / Path(relative))
                if candidate in seen or not candidate.is_file() or candidate.stat().st_size != member["size_bytes"]:
                    continue
                seen.add(candidate)
                stage, index = selected
                stage_row = stages.get(stage) if isinstance(stages.get(stage), dict) else {}
                cases = stage_row.get("cases") if isinstance(stage_row.get("cases"), list) else []
                case = cases[index - 1] if index and index <= len(cases) and isinstance(cases[index - 1], dict) else {}
                title = label + " · " + stage + (" / " + str(case.get("name") or f"case-{index:03d}") if index else "") + " · "
                suffix = candidate.suffix.lower()
                if suffix == ".png":
                    child.add(path.parent, candidate, title + str(member.get("name") or candidate.name), mode="png", expected=member["sha256"])
                elif suffix == ".zgs":
                    child.add(path.parent, candidate, title + candidate.name, mode="binary", expected=member["sha256"])
                elif suffix == ".json":
                    child.add(path.parent, candidate, title + candidate.name, mode="json", expected=member["sha256"])
                else:
                    child.add(path.parent, candidate, title + candidate.name, expected=member["sha256"], log=True)
            if row["status"] == "running":
                # Only existing wrapper logs of the still-owned operation are
                # mutable. PNGs remain unavailable until a SHA manifest exists.
                active = [(stage, None, path.parent / stage) for stage in ("build", "regression", "package")]
                inventory = row.get("inventory") if isinstance(row.get("inventory"), dict) else {}
                visual = list(inventory["visual"]) if isinstance(inventory.get("visual"), (list, dict)) else []
                active.extend(("visual", index, path.parent / "visual" / f"case-{index:03d}") for index in range(1, min(len(visual), 999) + 1))
                for stage, index, folder in active:
                    title = label + " · " + stage + (" / " + str(visual[index - 1]) if index else "") + " · 进行中日志（尚无终态SHA） · "
                    for filename in ("stdout.log", "stderr.log", "UnrealBuildTool.log" if stage == "build" else "Unreal.log"):
                        candidate = _confined(path.parent, folder / filename)
                        if candidate not in seen:
                            child.add(path.parent, candidate, title + filename, log=True)
            # Preserve the normal operation's own budget after adding this
            # separately bounded batch of archived check artifacts.
            evidence.maximum_members += len(child.members)
            evidence.members.update(child.members); evidence.logs.extend(child.logs); evidence.artifacts.extend(child.artifacts)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            notes.append(label + "的当前任务/授权/HEAD或文件摘要待核实；未展示不匹配证据。")
    return notes


def _mcp_public(value):
    facts = value.get("evidence") if isinstance(value.get("evidence"), dict) else {}
    return {"completed": value.get("completed") is True, "evidence": {
        "connected": facts.get("connected") is True, "server_name": facts.get("server_name"),
        "registered_tools": facts.get("registered_tools"), "raw_stdout_sha256": facts.get("raw_stdout_sha256"),
        "invocations": [{key: row.get(key) for key in ("tool_name", "completed", "is_error")}
                        for row in facts.get("invocations", [])[:100] if isinstance(row, dict)]}}


def _user_followup_files(evidence, runtime, task, operation, node):
    from .dashboard_state import _user_followup_context
    try:
        context = _user_followup_context(runtime, task, operation)
        if not context:
            return None
        profile, root = context["profile"], context["root"]
        evidence.virtual("UE MCP 有界交接状态", {**context["public"], "task_id": task["id"],
            "scope_note": "工具接通不代表游戏质量或任务验收通过。", "user_instruction": context["request"].get("user_instruction")})
        for entry in (task.get("user_followup_results") or [])[-3:]:
            if (not isinstance(entry, dict) or entry.get("id") != profile["id"] or type(entry.get("turn")) is not int or
                    not 1 <= entry["turn"] <= profile["max_turns"]):
                continue
            number = entry["turn"]; path = _confined(root, root / f"turn-{number}.json")
            if entry.get("receipt_path") != str(path) or not SHA.fullmatch(str(entry.get("receipt_sha256") or "")):
                continue
            if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024 or _digest(path) != entry["receipt_sha256"]:
                continue
            receipt = _object(path, maximum=8 * 1024 * 1024)
            op, result = receipt.get("operation") or {}, receipt.get("result") or {}
            if (receipt.get("version") != 1 or receipt.get("id") != profile["id"] or receipt.get("task_id") != task["id"] or
                    receipt.get("request_sha256") != profile["request_sha256"] or op.get("task_id") != task["id"] or
                    op.get("user_followup_id") != profile["id"] or op.get("user_followup_request_sha256") != profile["request_sha256"] or
                    op.get("user_followup_turn") != number):
                continue
            evidence.virtual(f"UE MCP 第{number}回合 · turn-{number}.json", {"id": profile["id"], "task_id": task["id"],
                "request_sha256": profile["request_sha256"], "turn": number, "finished_at": receipt.get("finished_at"),
                "head_sha": (receipt.get("candidate") or {}).get("head_sha"), "connection": _mcp_public(result.get("user_followup") or {})})
            text = result.get("text")
            delivery = _confined(root, root / f"turn-{number}.md")
            if isinstance(text, str) and delivery.is_file() and delivery.stat().st_size <= MAX_TEXT and delivery.read_text(encoding="utf-8-sig") == text:
                public_delivery = re.sub(r"```[\s\S]*?```", "[配置或脚本代码片段已省略]", text)
                evidence.virtual(f"UE MCP 第{number}回合 · turn-{number}.md", {"task_id": task["id"], "turn": number, "delivery": public_delivery})
        if context["owned"] and node == "code":
            directory = _confined(runtime, Path(operation.get("output_dir", "")))
            if directory.parent != runtime / "runs" or not re.fullmatch(r"code-[a-f0-9]{32}", directory.name):
                raise ValueError("MCP操作目录不匹配")
            process = _object(_confined(runtime, directory / "process.json"), missing={}, maximum=256 * 1024)
            proof_path = _confined(runtime, directory / "ue-mcp-evidence.json")
            proof = _object(proof_path, missing={}, maximum=1024 * 1024)
            selected = process.get("user_followup_profile") or {}
            if (proof and process.get("task_id") == task["id"] and process.get("head_sha") == operation.get("head_sha") and
                    process.get("output_dir") == str(directory) and selected.get("id") == profile["id"] and
                    selected.get("request_sha256") == profile["request_sha256"] and process.get("user_followup_turn") == operation.get("user_followup_turn") and
                    process.get("user_followup") == proof and SHA.fullmatch(str((proof.get("evidence") or {}).get("raw_stdout_sha256") or ""))):
                stdout = _confined(runtime, directory / "stdout.log")
                if stdout.is_file() and stdout.stat().st_size <= MAX_BINARY and _digest(stdout) == proof["evidence"]["raw_stdout_sha256"]:
                    evidence.virtual("当前回合 · ue-mcp-evidence.json（连接与调用回执）", _mcp_public(proof))
        return context
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _bound_task(state, *, art=False):
    task = state.get("task")
    if isinstance(task, dict):
        return task, "当前任务", task.get("art_review") if art else None
    if art:
        pending = state.get("art_pending") or []
        history = state.get("art_history") or []
        record = pending[-1] if pending else history[-1] if history else None
        if isinstance(record, dict) and isinstance(record.get("task"), dict):
            return record["task"], "待用户验收版本" if pending else "最近已接受版本（历史）", record
    return {}, "当前没有活动任务", None


def _promotion_source(runtime, state, task, bound):
    """Match the flow's outstanding approved batch, never the new active task."""
    queue = _object(_confined(runtime, runtime / "promotion-requests.json"), missing={"requests": []})
    candidates = []
    for item in (queue.get("requests") or [])[-200:]:
        if not isinstance(item, dict):
            continue
        record = next((record for record in reversed(state.get("art_history") or [])
                       if isinstance(record, dict) and record.get("status") == "accepted"
                       and (record.get("visual_decision") or {}).get("decision") == "accepted"
                       and record.get("id") == item.get("source_task_id")
                       and SHA.fullmatch(str(record.get("review_package_sha256", "")))
                       and record.get("review_package_sha256") == item.get("review_package_sha256")
                       and isinstance(record.get("task"), dict)
                       and record["task"].get("id") == record.get("id")), None)
        if record:
            candidates.append((item, record))
    matching = [pair for pair in candidates if pair[0].get("source_task_id") == task.get("id")
                and pair[0].get("review_package_sha256") == (bound or {}).get("review_package_sha256")]
    if not state.get("task") and (bound or {}).get("status") == "accepted" and matching:
        return matching[-1]
    active_id = (state.get("task") or {}).get("id")
    outstanding = [pair for pair in candidates if pair[0].get("source_task_id") != active_id
                   and pair[0].get("status") != "integrated"]
    return next((pair for pair in outstanding if pair[0].get("status") == "blocked"),
                outstanding[0] if outstanding else matching[-1] if matching else (None, None))


def _art_inputs(config, task):
    settings = config["art_pipeline"]
    request = task.get("art_request") or {}
    root = Path(settings["artifacts_dir"]).absolute()
    if (task.get("id") not in settings["task_ids"] or request.get("config_file") != settings["config_file"]
            or Path(request.get("artifacts_dir", "")).absolute() != root):
        raise ValueError("美术任务的配置或产物范围改变")
    batch_path = Path(settings["config_file"])
    if _digest(batch_path) != request.get("config_sha256"):
        raise ValueError("美术批次配置版本改变")
    batch = _object(batch_path)
    workspace = Path(config["runtime_dir"]) / "workspace"
    recipe_path = _confined(workspace, Path(batch["recipe_file"])) if batch.get("recipe_file") else None
    recipe = _object(recipe_path) if recipe_path else None
    job_path = _confined(workspace, Path(batch["blender_job_file"])) if batch.get("blender_job_file") else None
    return root, batch, recipe, _digest(recipe_path) if recipe_path else None, _digest(job_path) if job_path else None


def _art_files(evidence, config, state, task, bound, node):
    root, batch, recipe, recipe_sha, job_sha = _art_inputs(config, task)
    if node in {"concept", "model"}:
        entries = recipe.get("assets", []) if recipe else batch["assets"]
        inputs = _object(_confined(root, root / "production-input.json"), missing={})
        sources = {a.get("asset_id"): a for a in inputs.get("assets", []) if isinstance(a, dict)}
        for asset in entries:
            tag = str(asset.get("asset_id", "")) + "_" + str(asset.get("revision", "v001"))
            if not re.fullmatch(r"[A-Za-z0-9_]+_v[0-9]{3}", tag):
                continue
            relative = Path("concepts", tag + ".png") if node == "concept" else Path("sources", tag + ".glb")
            path = _confined(root, root / relative)
            source = sources.get(asset.get("asset_id"), {})
            expected = source.get("concept_sha256" if node == "concept" else "source_sha256")
            if path.is_file() and Path(source.get("input_glb", "")).name == tag + ".glb":
                # These are the exact current recipe inputs, possibly reused.
                evidence.add(root, path, tag + "（当前配方输入，可复用）", mode="png" if node == "concept" else "binary", expected=expected)
        return
    package = None
    if isinstance(bound, dict) and bound.get("review_package_sha256"):
        source = {**task.get("art_request", {}), "review_package_sha256": bound["review_package_sha256"]}
        _, package, _ = _package(config, source, bound["review_package_sha256"])
    pipeline = _object(_confined(root, root / "pipeline-state.json"), missing={})
    source = _object(_confined(root, root / "production-input.json"), missing={})
    # A rework must not inherit outputs from a previous recipe/job merely because
    # their files still exist in the public tree.
    live = ((source.get("recipe") or {}).get("sha256") == recipe_sha and
            (source.get("blender_job") or {}).get("sha256") == job_sha and source.get("batch_id") == batch.get("batch_id"))
    manifest_raw = package.get("manifest") if package else pipeline.get("manifest") if live else None
    manifest = None
    if isinstance(manifest_raw, str):
        path = _confined(root, Path(manifest_raw))
        if path.name != "assets_manifest.json" or path.parent.parent != root / "processed":
            raise ValueError("处理清单不是本批版本化产物")
        candidate = _object(path)
        if package or candidate.get("input_manifest_sha256") == _digest(root / "production-input.json"):
            manifest = candidate
    if node == "blender" and manifest:
        evidence.add(root, path, "资产处理清单", mode="json", expected=_digest(path))
        evidence.add(root, path.parent / "processing_report.json", "Blender 处理报告", mode="json")
        for item in manifest.get("files", []):
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            file = _confined(path.parent, path.parent / item["path"])
            suffix = file.suffix.lower()
            if suffix in {".fbx", ".blend", ".png"}:
                evidence.add(root, file, file.name, mode="png" if suffix == ".png" else "binary", expected=item.get("sha256"))
        if live:
            evidence.add(root, root / "logs" / (path.parent.name + "-blender.log"), "Blender 本轮处理日志", mode="text", log=True)
    if node in {"ue_import", "ue_capture", "check", "review", "approval"} and package:
        evidence.virtual("验收包摘要", {k: package.get(k) for k in ("batch_id", "status", "approved_levels", "full_batch_released", "limitations", "recipe", "blender_job")})
        if node in {"check", "review", "approval"}:
            check = _confined(root, root / "independent-check.json")
            evidence.add(root, check, "固定版本独立检查", mode="json", expected=bound.get("independent_check_sha256"))
        if node in {"ue_capture", "review", "approval"}:
            for shot in package.get("capture", {}).get("shots", []):
                file = _confined(root, Path(shot["path"]))
                if file.suffix.lower() == ".png" and file.is_relative_to(root / "screenshots"):
                    evidence.add(root, file, str(shot.get("name", file.stem)), mode="png", expected=shot.get("sha256"))
        imported = package.get("ue_import", {})
        if node == "ue_import":
            evidence.virtual("UE 导入回执", imported)
            project = root / "UE/HousingPreview"
            asset_base = imported.get("asset_base", "")
            if re.fullmatch(r"/Game/ArtPreview/Versions/V_[0-9a-f]{16}", asset_base):
                version = project / "Content" / asset_base.removeprefix("/Game/")
                for file in imported.get("ue_files", []):
                    target = _confined(project, project / file["path"])
                    if target.is_relative_to(version) and target.suffix.lower() == ".uasset":
                        evidence.add(root, target, target.name, mode="binary", expected=file.get("sha256"))
        if node in {"ue_import", "ue_capture"}:
            run = imported.get("run") if node == "ue_import" else package.get("capture", {}).get("capture_id")
            if isinstance(run, str) and re.fullmatch(r"[a-f0-9-]{12,80}", run):
                evidence.add(root, root / "logs" / (run + ("-ue-import.log" if node == "ue_import" else "-ue-capture.log")), "该验收版本 UE 日志", mode="text", log=True)
    elif node in {"blender", "ue_import", "ue_capture"} and live:
        run = pipeline.get("recipe")
        if isinstance(run, str) and re.fullmatch(r"[a-f0-9]{12}", run):
            suffix = {"blender": "-blender.log", "ue_import": "-ue-import.log"}.get(node)
            if suffix:
                evidence.add(root, root / "logs" / (run + suffix), "当前制作阶段日志", mode="text", log=True)
            elif node == "ue_capture" and manifest:
                from .art_pipeline import capture_fingerprint
                receipt_path = _confined(root, root / "receipts" / (run + "-import.json"))
                imported = _object(receipt_path, missing={})
                if imported.get("status") == "passed" and imported.get("run") == run and imported.get("manifest_sha256") == _digest(path):
                    script = Path(__file__).resolve().parent.parent / "art_tools/ue/capture_preview.py"
                    capture_id = capture_fingerprint(run, receipt_path, script)
                    evidence.add(root, root / "logs" / (capture_id + "-ue-capture.log"),
                                 "本轮 UE 截图日志（验收包尚未提交）", mode="text", log=True)


def _full_flow_wrapper_case(directory, candidate):
    if (directory.name == "retest" and candidate.parent.name == "reconciliations" and candidate.parent.parent.name == "attempt-1" and
            candidate.parent.parent.parent == directory and re.fullmatch(r"case-[0-9]{3}\.json", candidate.name) and int(candidate.stem[5:]) > 0):
        return "attempt-1 · 校验器误报校核"
    if directory.name == "package-short":
        if candidate.parent.name == "rawlogs" and candidate.parent.parent.name == "attempt-1" and candidate.parent.parent.parent == directory:
            if candidate.name in {"stdout.log", "stderr.log", "Unreal.log", "process.json"}:
                return "attempt-1 · 原始打包输出"
        if candidate.parent == directory / "attempt-1" and candidate.name in {
                "receipt.json", "package-summary.json", "plain.log", "loop.log", "loop-Slot97.zgs",
                "raw-package-uat.log", "raw-package-summary.json", "raw-package-artifacts.json"}:
            return "attempt-1 · 短路径正式补测"
        return None
    if candidate.name not in {"stdout.log", "stderr.log", "Unreal.log", "process.json", "receipt.json"}:
        return None
    parent = candidate.parent
    case = None
    if re.fullmatch(r"case-[0-9]{3}", parent.name) and int(parent.name[5:]) > 0:
        case, parent = parent.name, parent.parent
        if directory.name not in {"visual", "retest"}:
            return None
    if parent.parent != directory or not re.fullmatch(r"attempt-[1-9][0-9]{0,2}", parent.name):
        return None
    return parent.name + (" · " + case if case else "")


def _full_flow_png_allowed(run, project, candidate):
    if candidate.suffix.lower() != ".png":
        return False
    if candidate.is_relative_to(project):
        return True
    parts = candidate.relative_to(run).parts
    if (len(parts) == 6 and parts[0] == "checks" and parts[1] in {"visual", "retest"} and
            re.fullmatch(r"attempt-[1-9][0-9]{0,2}", parts[2]) and re.fullmatch(r"case-[0-9]{3}", parts[3]) and
            int(parts[3][5:]) > 0 and parts[4] == "images"):
        return True
    if len(parts) == 5 and parts[0:2] == ("checks", "package-short"):
        return parts[2] == "attempt-1" and parts[3] == "package-evidence" and parts[4] in {"plain.png", "loop.png"}
    return bool(len(parts) == 5 and parts[0:2] == ("checks", "package") and
                re.fullmatch(r"attempt-[1-9][0-9]{0,2}", parts[2]) and parts[3] == "package-evidence")


def _full_flow_detail(config_path):
    from .dashboard_state import (_FULL_FLOW_NATIVE_PHASES, _FULL_FLOW_RECORD_BYTES, _full_flow_context, _full_flow_manifest_sha,
                                  _full_flow_reconciliations, _full_flow_snapshot)
    config = load_config(Path(config_path))
    state = _state(config)
    context = _full_flow_context(config, state)
    if context is None:
        raise ValueError("当前没有可核实的全流程测试记录")
    request, status, run = (context[key] for key in ("request", "status", "run"))
    public = _full_flow_snapshot(config, state, context=context)
    scope = {"lane": "development", "node": "full_flow_test", "run": request["id"], "head": request["head_sha"],
             "request_sha256": context["request_sha256"], "request_file_sha256": context["request_file_sha256"]}
    evidence = Evidence("development", "full_flow_test", scope)
    label = ("历史版本的隔离测试" if public["historical"] else "本次隔离全流程测试") + " · " + request["task_id"] + " · HEAD " + request["head_sha"][:12]
    detail = {"lane": "development", "node": "full_flow_test", "title": "Opus全流程测试", "task_id": request["task_id"],
              "scope_label": label, "summary": public["summary"] or public["stage_label"], "actor": "Claude Code",
              "model": public["model"], "effort": public["effort"], "model_source": public["model_source"],
              "blocked_reason": public["summary"] if status["status"] == "failed" else None}
    evidence.virtual("全流程测试绑定与状态", {key: public[key] for key in
        ("id", "task_id", "head_sha", "current_head_sha", "historical", "status", "status_label", "stage", "stage_label", "updated_at", "model", "effort", "model_source",
         "operator_stage", "stage_source", "stage_updated_at", "stage_conflicts") if key in public})
    for phase in _FULL_FLOW_NATIVE_PHASES:
        title = {"plan": "只读规划", "test": "测试执行", "analysis": "结果分析"}.get(phase)
        if title is None:
            title = "结果分析 · 第 " + phase.split("-", 1)[1] + " 次调用"
        directory = _confined(run, run / "native" / phase)
        for name in ("stdout.log", "stderr.log"):
            evidence.add(run, directory / name, title + (" · 原生事件摘要" if name == "stdout.log" else " · 错误摘要"),
                         mode="events", log=True)
        process = _object(_confined(run, directory / "process.json"), missing={}, maximum=65536)
        keys = ("kind", "exit_code", "timed_out", "output_limit_exceeded", "launch_or_capture_failed", "model_selection")
        if process:
            evidence.virtual(title + " · 执行回执", {key: process[key] for key in keys if key in process})
    if status.get("report_path"):
        report = _confined(run, run / "report.md")
        expected = _digest(report) if report.is_file() and report.stat().st_size <= MAX_TEXT else None
        evidence.add(run, report, "Opus全流程测试报告", mode="text", expected=expected)
    notices = []
    documents = []
    for stage in ("prepare", "build", "regression", "visual", "package", "retest", "package-short"):
        directory = _confined(run, run / "checks" / stage)
        for name in ("receipt.json",):
            path = _confined(run, directory / name)
            value = _object(path, missing={}, maximum=_FULL_FLOW_RECORD_BYTES)
            if not value:
                continue
            if (value.get("version") != 1 or value.get("request_sha256") != context["request_sha256"] or value.get("id") != request["id"] or
                    value.get("task_id") != request["task_id"] or value.get("head_sha") != request["head_sha"] or value.get("stage") != stage):
                notices.append(stage + "回执与本次测试绑定不匹配")
                continue
            if stage == "package-short" and (value.get("attempt") != 1 or not _full_flow_manifest_sha(context) or value.get("source_manifest_sha256") != _full_flow_manifest_sha(context)):
                notices.append("短路径补测的源输入摘要不匹配")
                continue
            reconciled = _full_flow_reconciliations(context, value, _full_flow_manifest_sha(context)) if stage == "retest" and value.get("reconciled_tool_false_failure") else []
            correction_refs = {(item[key]["path"], item[key]["sha256"]) for item in reconciled for key in
                               ("derived_receipt", "original_case_receipt", "original_visual_receipt")}
            evidence.virtual(stage + " · " + ("检查回执" if name == "receipt.json" else "测试事实"), value)
            documents.append((stage, value))
            for member in value.get("evidence", []) if isinstance(value.get("evidence"), list) else []:
                if not isinstance(member, dict) or not SHA.fullmatch(str(member.get("sha256") or "")) or not isinstance(member.get("path"), str):
                    continue
                try:
                    candidate = _confined(run, Path(member["path"]))
                    wrapper = _full_flow_wrapper_case(directory, candidate)
                    if candidate.parent.name == "reconciliations" and (str(candidate), member["sha256"]) not in correction_refs:
                        continue
                    if (not wrapper and stage == "retest" and (str(candidate), member["sha256"]) in correction_refs and candidate.name == "receipt.json"):
                        wrapper = _full_flow_wrapper_case(run / "checks/visual", candidate)
                        wrapper = "原 visual 记录 · " + wrapper if wrapper else None
                    if wrapper and candidate.is_file() and candidate.stat().st_size <= MAX_BINARY and _digest(candidate) == member["sha256"]:
                        title = stage + " · " + wrapper + " · "
                        if candidate.suffix == ".json":
                            evidence.add(run, candidate, title + candidate.name, mode="json", expected=member["sha256"])
                        elif candidate.suffix == ".zgs":
                            evidence.add(run, candidate, title + candidate.name, mode="binary", expected=member["sha256"])
                        else:
                            evidence.add(run, candidate, title + "UE/构建输出 · " + candidate.name, mode="text", log=True)
                    elif _full_flow_png_allowed(run, context["project"], candidate):
                        root = context["project"] if candidate.is_relative_to(context["project"]) else run
                        evidence.add(root, candidate, stage + "测试截图 · " + candidate.stem, mode="png", expected=member["sha256"])
                except (OSError, ValueError):
                    notices.append(stage + "部分原始证据不属于本次受信测试目录")
    facts = _object(_confined(run, run / "facts.json"), missing={}, maximum=_FULL_FLOW_RECORD_BYTES)
    if facts:
        if (facts.get("version") == 1 and facts.get("id") == request["id"] and facts.get("task_id") == request["task_id"] and
                facts.get("head_sha") == request["head_sha"] and facts.get("request_sha256") == context["request_sha256"] and facts.get("stage") == "status"):
            evidence.virtual("全流程事实汇总（完成不等于全通过）", facts)
            documents.append(("全流程", facts))
        else:
            notices.append("全流程事实汇总与本次测试绑定不匹配")
    for stage, value in documents:
        for shot in value.get("screenshots", []) if isinstance(value.get("screenshots"), list) else []:
            if not isinstance(shot, dict) or not SHA.fullmatch(str(shot.get("sha256") or "")) or not isinstance(shot.get("path"), str):
                continue
            candidate = Path(shot["path"])
            candidate = candidate if candidate.is_absolute() else context["project"] / candidate
            try:
                image = _confined(context["project"], candidate)
                if image.suffix.lower() == ".png":
                    evidence.add(context["project"], image, str(shot.get("title") or stage + "测试截图"), mode="png", expected=shot["sha256"])
            except (OSError, ValueError):
                notices.append(stage + "截图不属于本次隔离项目")
    if notices:
        detail["artifact_notice"] = "；".join(dict.fromkeys(notices))[:400]
    detail.update(logs=evidence.logs, artifacts=evidence.artifacts)
    return detail, evidence


def _collect(config_path, lane, node, snapshot=None):
    if lane == "development" and node == "full_flow_test":
        return _full_flow_detail(config_path)
    if lane not in NODE_IDS or node not in NODE_IDS[lane]:
        raise ValueError("未知流程节点")
    main = load_config(Path(config_path))
    config = worker_config(main) if lane == "art" or lane == "operations" and node == "art_worker" else main
    runtime = Path(config["runtime_dir"]).absolute()
    state = art_state(config) if config.get("art_pipeline") and lane != "development" else _state(config)
    task, label, bound = _bound_task(state, art=lane == "art")
    promotion = None
    if lane == "art" and node == "promotion":
        promotion, record = _promotion_source(runtime, state, task, bound)
        task, bound = (record["task"], record) if record else ({}, None)
        label = "已批准批次入库（固定验收版本）" if record else "尚无可核实的批准入库记录"
    if lane == "operations":
        task, bound = {}, None
    if task and not TASK.fullmatch(str(task.get("id", ""))):
        raise ValueError("任务标识无效")
    op = {} if lane == "operations" or node == "promotion" else state.get("operation") or {}
    head = None if lane == "operations" else task.get("art_workspace_head") or task.get("candidate", {}).get("head_sha") or task.get("code_base") or state.get("expected_head")
    replan_phase = state.get("blocked_from") if state.get("phase") == "BLOCKED" else state.get("phase")
    if lane == "development" and replan_phase == "REPLAN":
        pending = task.get("replanning") or {}
        source = pending.get("fingerprint") or {}
        if pending.get("source_phase") == "CODE" and isinstance(source, dict) and re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", str(source.get("head_sha") or "")):
            head = source["head_sha"]
    scope = {"lane": lane, "node": node, "binding": state.get("binding"), "task": task.get("id"),
             "head": head, "attempt": task.get("reworks"), "operation": op.get("output_dir"),
             "package": (bound or {}).get("review_package_sha256"), "promotion": (promotion or {}).get("id")}
    if lane == "art":
        diagnosis = task.get("art_diagnosis") or {}
        scope.update(operation_kind=op.get("kind"), operation_head=op.get("head_sha"),
                     diagnosis_fingerprint=diagnosis.get("fingerprint"), diagnosis_round=diagnosis.get("round"))
    elif lane == "development":
        replanning = task.get("replanning") or {}
        repair = task.get("user_repair") or {}
        followup = task.get("user_followup") or {}
        scope.update(operation_kind=op.get("kind"), operation_head=op.get("head_sha"),
                     replanning_fingerprint=replanning.get("fingerprint"), replanning_round=replanning.get("round"),
                     user_repair_id=repair.get("id"), user_repair_request_sha256=repair.get("request_sha256"), user_repair_epoch=repair.get("epoch"),
                     user_followup_id=followup.get("id"), user_followup_request_sha256=followup.get("request_sha256"))
    evidence = Evidence(lane, node, scope)
    detail = {"lane": lane, "node": node, "title": NODES[lane][node], "task_id": task.get("id"),
              "scope_label": label, "summary": "尚无此节点可公开的同任务证据", "model": None, "effort": None,
              "model_source": None, "blocked_reason": redact(state.get("last_error") or "", 1200)}
    if snapshot:
        flow = snapshot.get("flows", {}).get(lane, {})
        projected = next((n for n in flow.get("nodes", []) if n.get("id") == node), {})
        for key in ("summary", "model", "effort", "model_source", "blocked_reason"):
            if key in projected:
                detail[key] = projected[key]
    if lane == "operations":
        detail["scope_label"] = "当前服务状态；日志摘要可能含同服务历史事件"
        if node == "notifications":
            evidence.virtual("通知服务公开状态", (snapshot or {}).get("notifications", {"message": "暂无公开状态"}))
        else:
            health = _object(_confined(runtime, runtime / "background-health.json"), missing={}, maximum=65536)
            evidence.virtual("后台健康回执", {k: health.get(k) for k in ("status", "pid", "started_at", "exited_at", "exit_code", "last_error", "recovery")})
            for name in ("background.log", "background.err.log"):
                evidence.add(runtime, runtime / name, "服务运行摘要 · " + name, mode="service", log=True)
    elif task:
        repair_context = None
        if lane == "development" and node in {"code", "check", "review"}:
            repair_context = _user_repair_files(evidence, runtime, task)
            repair_notices = _user_repair_check_files(evidence, runtime, task, head, repair_context, op)
            if repair_notices:
                detail["artifact_notice"] = "；".join(repair_notices)
            if repair_context and node == "code":
                detail["title"] = "Opus修复全流程问题"
            followup_context = _user_followup_files(evidence, runtime, task, op, node)
            if followup_context and node == "code" and followup_context["public"]["status"] == "active" and followup_context["owned"]:
                detail["title"] = followup_context["public"]["summary"]
        kinds = {"routine_plan": {"plan"}, "critical_plan": {"plan"}, "code": {"code"}, "queue": {"code", "art_diagnose"},
                 "check": {"check", "art_check", "milestone_check"}, "review": {"review"}, "milestone_review": {"review", "milestone_check"}}
        matching_plan = node not in {"routine_plan", "critical_plan"} or (node == "critical_plan") == (op.get("planning_level") == "critical")
        if matching_plan and op.get("task_id") == task["id"] and op.get("kind") in kinds.get(node, set()):
            if repair_context and op.get("kind") == "code" and (
                    op.get("user_repair_id") != repair_context["profile"]["id"] or
                    op.get("user_repair_request_sha256") != repair_context["profile"]["request_sha256"]):
                raise ValueError("当前开发操作不属于本批用户修复授权")
            if op.get("head_sha") not in {head, task.get("code_base"), task.get("base_sha")}:
                raise ValueError("操作证据的 HEAD 不匹配")
            replan_phase = state.get("blocked_from") if state.get("phase") == "BLOCKED" else state.get("phase")
            if lane == "development" and node == "critical_plan" and replan_phase == "REPLAN":
                # The dedicated collector below binds fresh and historical repair plans.
                pass
            elif op["kind"] == "art_diagnose":
                if lane != "art" or task.get("kind") != "art":
                    raise ValueError("自主诊断不属于美术任务")
                _diagnosis_operation_files(evidence, runtime, op, task, head)
                detail["title"] = "Claude 自主诊断与调整"
                detail["summary"] = "原制作会话的只读诊断分支；以下为诊断日志与调整计划，不代表制作完成或视觉验收通过。"
            elif op["kind"] in {"check", "art_check", "milestone_check"}:
                directory = _confined(runtime, Path(op["output_dir"]))
                if directory.parent != runtime / "runs":
                    raise ValueError("检查操作目录不匹配")
                for name in ("stdout.log", "stderr.log"):
                    evidence.add(runtime, directory / name, "当前检查 · " + name, mode="text", log=True)
            else:
                _provider_files(evidence, runtime, op["output_dir"])
        directory = _confined(runtime, runtime / "tasks" / task["id"])
        if node in {"routine_plan", "critical_plan"} and task.get("planning_source_output_dir"):
            is_critical = task.get("planning_level") == "critical"
            if (node == "critical_plan") == is_critical:
                _provider_files(evidence, runtime, task["planning_source_output_dir"], title="本任务规划来源（历史操作）")
        if lane == "development" and node == "critical_plan":
            _replanning_files(evidence, runtime, state, task, head, op)
            replan_phase = state.get("blocked_from") if state.get("phase") == "BLOCKED" else state.get("phase")
            if replan_phase == "REPLAN" or task.get("replanning_history"):
                detail["title"] = "Claude重新分析修复计划"
                if replan_phase == "REPLAN":
                    from .dashboard_state import _replanning_summary
                    detail["summary"] = _replanning_summary(runtime, task)
        if node in {"code", "queue"}:
            if lane == "art":
                _diagnosis_history_files(evidence, runtime, task, op)
            path = task.get("delivery_path")
            if isinstance(path, str) and Path(path).parent == directory and re.fullmatch(r"delivery-\d+\.md", Path(path).name):
                evidence.add(runtime, Path(path), "本任务最近交付说明（可能属于上一轮）")
        if node == "check":
            for result in task.get("check_results", []):
                _check_files(evidence, runtime, result, head)
        if node == "review":
            review = directory / ("review-" + str(task.get("reworks", 0)) + ".json")
            record = _object(_confined(runtime, review), missing={}, maximum=256 * 1024)
            if record.get("head_sha") == head:
                evidence.virtual("本轮独立审核回执", record)
        if lane == "art" and node != "promotion":
            try:
                _art_files(evidence, config, state, task, bound, node)
            except (OSError, ValueError, TypeError, KeyError) as error:
                # Live job/recipe writes or stale artifacts must not hide the
                # current operation's safe log projection.
                detail["artifact_notice"] = redact("产物证据暂不可核实：" + str(error), 400)
            if node == "approval" and bound:
                evidence.virtual("人工验收状态", {k: bound.get(k) for k in ("status", "head_sha", "review_package_sha256", "visual_decision", "submitted_at")})
    if lane == "development" and not task and node in {"routine_plan", "critical_plan"}:
        phase = state.get("blocked_from") if state.get("phase") == "BLOCKED" else state.get("phase")
        if (phase == "PLAN" and op.get("kind") == "plan" and op.get("task_id") is None and
                (node == "critical_plan") == (op.get("planning_level") == "critical")):
            detail["scope_label"] = "当前规划操作；尚未生成任务编号"
            _provider_files(evidence, runtime, op["output_dir"])
    if lane == "development" and node == "publish":
        records = [item for item in (state.get("accepted") or []) if isinstance(item, dict)]
        record = next((r for r in reversed(records) if r.get("id") == task.get("id")), None) if task else records[-1] if records else None
        if record:
            detail["scope_label"] = "当前任务交付" if task else "最近已接受任务交付（历史）"
            detail["task_id"] = record.get("id")
            evidence.virtual("已接受任务交付记录", record)
    if lane == "development" and node in {"milestone_review", "milestone_publish"}:
        milestone = state.get("milestone") or {}
        identity = milestone.get("id")
        if isinstance(identity, str) and TASK.fullmatch(identity):
            detail["scope_label"] = "阶段 " + identity + " · 固定 HEAD " + str(milestone.get("head_sha", ""))[:12]
            if node == "milestone_review":
                for result in milestone.get("check_results", []):
                    _check_files(evidence, runtime, result, milestone.get("head_sha"))
                record = milestone.get("review")
                if isinstance(record, dict) and record.get("head_sha") == milestone.get("head_sha"):
                    evidence.virtual("阶段审核回执", record)
                if op.get("kind") in {"review", "milestone_check"} and op.get("task_id") is None and op.get("head_sha") == milestone.get("head_sha"):
                    _provider_files(evidence, runtime, op["output_dir"], title="当前阶段操作")
            elif milestone.get("report_path"):
                path = runtime / "milestones" / identity / "report.json"
                if Path(milestone["report_path"]).absolute() != path:
                    raise ValueError("阶段交付报告路径不匹配")
                record = _object(_confined(runtime, path), missing={})
                if record.get("head_sha") == milestone.get("head_sha"):
                    evidence.virtual("阶段交付报告", record)
    if lane == "art" and node == "promotion":
        if promotion:
            evidence.virtual("该验收版本入库状态", {k: promotion.get(k) for k in ("id", "batch_id", "status", "source_task_id", "review_package_sha256", "updated_epoch", "message", "last_error", "head_sha", "gameplay_binding")})
    if lane == "art" and node == "review" and task:
        from .dashboard_reworks import collect_reworks
        try:
            history = collect_reworks(config, state, task, evidence, redact=redact, check_files=_check_files)
            detail["rework_history"] = history
            detail["summary"] = history["summary"]["text"]
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            detail["rework_history_notice"] = redact("返工历史暂不可核实：" + str(error), 400)
    detail.update(logs=evidence.logs, artifacts=evidence.artifacts)
    if evidence.logs or evidence.artifacts:
        detail["summary"] = detail["summary"] if detail["summary"] != "尚无此节点可公开的同任务证据" else "以下证据已绑定所示任务或服务范围"
    return detail, evidence


def collect_node_detail(config_path, lane, node, *, snapshot=None):
    return _collect(config_path, lane, node, snapshot)[0]


def read_node_evidence(config_path, lane, node, ref, *, snapshot=None):
    if not isinstance(ref, str) or not SHA.fullmatch(ref):
        raise ValueError("证据引用无效")
    _, evidence = _collect(config_path, lane, node, snapshot)
    member = evidence.members.get(ref)
    if member is None:
        raise ValueError("证据范围已变化，请刷新节点详情")
    if "data" in member:
        return member["data"], "text/plain; charset=utf-8", None
    path = _confined(member["root"], member["path"])
    mode = member["mode"]
    with path.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        if [stat.st_dev, stat.st_ino] != member["identity"]:
            raise ValueError("证据文件实体发生变化")
        if mode in {"png", "binary"}:
            cap = MAX_PNG if mode == "png" else MAX_BINARY
            data = stream.read(cap + 1)
            if len(data) > cap or mode == "png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("证据文件类型或大小无效")
            if hashlib.sha256(data).hexdigest() != member["expected"]:
                raise ValueError("证据文件 SHA 不匹配")
            return data, "image/png" if mode == "png" else "application/octet-stream", path.name if mode == "binary" else None
        if mode == "json":
            cap = 16 * 1024 * 1024 if lane == "development" and node == "full_flow_test" else 4 * 1024 * 1024
            raw_json = stream.read(cap + 1)
            if len(raw_json) > cap:
                raise ValueError("证据 JSON 超过读取上限")
            if member.get("expected") and hashlib.sha256(raw_json).hexdigest() != member["expected"]:
                raise ValueError("证据文件 SHA 不匹配")
            return _json_bytes(json.loads(raw_json.decode("utf-8-sig"))), "text/plain; charset=utf-8", None
        if member.get("expected") and member.get("log"):
            if stat.st_size > MAX_BINARY or hashlib.file_digest(stream, "sha256").hexdigest() != member["expected"]:
                raise ValueError("检查日志大小或 SHA 不匹配")
        elif member.get("expected"):
            document = stream.read(MAX_TEXT + 1)
            if len(document) > MAX_TEXT or hashlib.sha256(document).hexdigest() != member["expected"]:
                raise ValueError("证据文档大小或 SHA 不匹配")
            return redact(document.decode("utf-8-sig", errors="replace")).encode("utf-8"), "text/plain; charset=utf-8", None
        stream.seek(max(0, stat.st_size - MAX_TEXT))
        raw = stream.read(MAX_TEXT).decode("utf-8-sig", errors="replace")
    if mode in {"events", "service", "errors"}:
        rows = []
        for line in raw.splitlines()[-250:]:
            try:
                event = json.loads(line)
            except ValueError:
                if mode != "events":
                    rows.append(redact(line, 1200))
                continue
            if not isinstance(event, dict):
                continue
            if mode == "events":
                label = _claude_event(event)
                if not label and event.get("type") in {"turn.started", "turn.completed", "turn.failed", "item.started", "item.completed"}:
                    label = {"turn.started": "开始执行", "turn.completed": "本轮结束", "turn.failed": "本轮失败",
                             "item.started": "执行步骤开始", "item.completed": "执行步骤完成"}[event["type"]]
                if label:
                    rows.append(label)
            else:
                projected = {key: event[key] for key in ("event", "status", "error", "message", "last_error") if key in event}
                if projected:
                    rows.append(json.dumps(_public(projected), ensure_ascii=False))
        data = ("有界事件摘要；原生对话、工具参数和完整聊天不会公开。\n" + "\n".join(rows)).encode("utf-8")
    else:
        data = redact(raw).encode("utf-8")
    return data, "text/plain; charset=utf-8", None
