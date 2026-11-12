"""Read-only flow diagrams from bounded, task-owned dashboard evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from .config import ART_CODING_MODEL, ART_CODING_REASONING_EFFORT, load_config
from .calendar import BEIJING
from .dashboard_state import _clean, _confined, _iso, _object

NODE_IDS = {
    "development": ("routine_plan", "critical_plan", "code", "check", "review", "publish", "milestone_review", "milestone_publish"),
    "art": ("queue", "concept", "model", "blender", "ue_import", "ue_capture", "check", "review", "approval", "promotion"),
    "operations": ("development_worker", "art_worker", "notifications"),
}
LABELS = {"running": "正在执行", "ready": "等待执行", "waiting": "等待恢复", "paused": "已暂停",
          "stale": "状态待核实", "blocked": "需要处理", "completed": "已完成", "idle": "待命",
          "disabled": "未启用", "not_required": "本轮无需"}
ART_TITLES = ("任务与制作编排", "概念图", "三维源模型", "Blender 制作", "UE 导入", "UE 展示图",
              "独立技术检查", "Codex 技术审核", "用户视觉验收", "批准资产入库")
ART_ACTORS = ("Claude Code", "Tripo", "Tripo", "Blender", "UE", "UE", "控制器", "Codex", "用户", "控制器")
_SAFE_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError, OverflowError)


def _text(value, limit=1000):
    # Flow summaries never need network destinations or signed download URLs.
    return re.sub(r"https?://[^\s<>\"']+", "[链接省略]", _clean(value, limit))


def _node(lane, identity, title, *, actor="系统", state="idle", current=False, summary="", reason=None,
          model=None, effort=None, source=None):
    state = state if state in LABELS else "stale"
    return {"id": identity, "title": title, "state": state, "state_label": LABELS[state], "current": current,
            "actor": actor, "model": _clean(model, 120) or None, "effort": _clean(effort, 40) or None,
            "model_source": source if source in {"recorded", "configured"} and model else None,
            "summary": _text(summary, 1000), "blocked_reason": _text(reason, 1200) or None,
            "detail_url": f"/api/flow/detail?lane={lane}&node={identity}"}


def _lane(identity, title, current, nodes, edges, summary):
    return {"id": identity, "title": title, "current": current, "nodes": nodes, "edges": edges,
            "summary": _text(summary, 1200)}


def _wait_summary(retry):
    if not isinstance(retry, dict) or retry.get("status") != "waiting":
        return ""
    reason = _clean(retry.get("reason_label"), 300) or "等待模型服务恢复"
    at = _local_time(retry.get("next_retry_at"))
    return reason + ("；下次重试 " + at if at else "")


def _local_time(value):
    at = _iso(value)
    return datetime.fromisoformat(at).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S（北京时间）") if at else None


def _development(snapshot, config):
    workflow, status = snapshot.get("workflow") or {}, snapshot.get("status") or {}
    current = workflow.get("current")
    current = current if current in NODE_IDS["development"] else None
    roles = {"routine_plan": ("Claude Code" if config.get("planning_provider") == "claude" else "Codex"),
             "critical_plan": "Claude Code", "code": "Claude Code" if config.get("coding_provider") == "claude" else "DSH",
             "check": "UE / 控制器", "review": "Codex", "milestone_review": "Codex",
             "publish": "控制器", "milestone_publish": "Git / 控制器"}
    nodes = []
    waiting = _wait_summary(snapshot.get("provider_retry"))
    for row in workflow.get("nodes", []):
        identity = row.get("id")
        if identity not in NODE_IDS["development"]:
            continue
        active = identity == current
        summary = waiting if active and waiting else row.get("summary")
        reason = (status.get("error") or status.get("summary")) if active and row.get("state") in {"blocked", "stale"} else None
        if active and waiting:
            reason = waiting
        model, effort, source = row.get("model"), row.get("effort"), row.get("model_source")
        # Codex protocol receipts currently need not record the requested model.
        # Today's configuration is a useful fallback, but is never actual evidence.
        if not model and active and identity in {"review", "milestone_review"}:
            settings = config.get("codex", {})
            model, effort, source = settings.get("model"), settings.get("reasoning_effort"), "configured"
        nodes.append(_node("development", identity, _clean(row.get("title"), 100), actor=roles[identity],
                           state=row.get("state"), current=active, summary=summary, reason=reason,
                           model=model, effort=effort, source=source))
    edges = [{key: row.get(key) for key in ("from", "to", "label", "kind", "active")} for row in workflow.get("edges", [])
             if row.get("from") in NODE_IDS["development"] and row.get("to") in NODE_IDS["development"]]
    return _lane("development", "功能开发", current, nodes, edges, waiting or workflow.get("summary"))


def _art_context(main, art, now):
    """Recheck bindings; a snapshot racing a worker transition cannot borrow a new run."""
    from .dashboard_art import _production_progress, _state, worker_config
    cfg = worker_config(main)
    state = _state(cfg)
    task = state.get("task") or {}
    if state.get("phase", "PLAN") != art.get("phase") or (task.get("id") or "") != (art.get("task_id") or ""):
        raise ValueError("美术任务已切换，等待下一次快照。")
    operation = state.get("operation") or {}
    expected = {"CODE": "code", "ART_DIAGNOSE": "art_diagnose", "ART_CHECK": "art_check", "ART_REVIEW": "review"}.get(state.get("phase"))
    owned = bool(expected and task.get("kind") == "art" and operation.get("kind") == expected
                 and operation.get("task_id") == task.get("id") and _iso(operation.get("started_at")))
    if expected == "art_diagnose" and owned:
        head = task.get("art_workspace_head") or (task.get("candidate") or {}).get("head_sha") or task.get("code_base") or state.get("expected_head")
        owned = owned and bool(head) and operation.get("head_sha") == head
        directory = _confined(Path(cfg["runtime_dir"]) / "runs", Path(operation.get("output_dir", "")))
        owned = owned and directory.parent == Path(cfg["runtime_dir"]) / "runs" and bool(re.fullmatch(r"art_diagnose-[a-f0-9]{32}", directory.name))
    progress = None
    if owned and expected == "code":
        request, approved = task.get("art_request") or {}, cfg["art_pipeline"]
        batch = _confined(Path(approved["config_file"]).parent, Path(approved["config_file"]))
        with batch.open("rb") as stream:
            raw = stream.read(128 * 1024 + 1)
        if len(raw) <= 128 * 1024 and hashlib.sha256(raw).hexdigest() == request.get("config_sha256"):
            progress = _production_progress(cfg, state, now)
    return cfg, state, owned, progress


def _diagnosis_model(runtime, directory, operation, task, receipt):
    """Observe only the owned diagnostic opening record, never native chat files."""
    from .dashboard_state import CLAUDE_SESSION_HEADER_BYTES, _CLAUDE_SESSION_ID
    source = task.get("session_id")
    if (task.get("session_owner_task_id", task.get("id")) != task.get("id") or
            not isinstance(source, str) or not _CLAUDE_SESSION_ID.fullmatch(source)):
        return None, None
    try:
        with _confined(runtime, directory / "stdout.log").open("rb") as stream:
            opening = stream.read(CLAUDE_SESSION_HEADER_BYTES)
        first = next((line for line in opening.split(b"\n")[:-1] if line.strip()), None)
        event = json.loads(first.decode("utf-8-sig")) if first else {}
        session, model = event.get("session_id"), event.get("model")
        if (event.get("type") != "system" or event.get("subtype") != "init" or session == source or
                not isinstance(session, str) or not _CLAUDE_SESSION_ID.fullmatch(session) or
                not isinstance(model, str) or not re.fullmatch(r"claude-(?:sonnet|opus|haiku)-[A-Za-z0-9.-]{1,95}", model)):
            return None, None
        effort = _clean(operation.get("diagnosis_reasoning_effort"), 40) or None
        if receipt:
            if (receipt.get("kind") != "art_diagnose" or receipt.get("diagnosis_provider") != "claude" or receipt.get("task_id") != task.get("id") or
                    receipt.get("head_sha") != operation.get("head_sha") or receipt.get("requested_session") != source or
                    receipt.get("diagnosis_session") != session):
                return None, None
            selected = receipt.get("model_selection") or {}
            if selected.get("model_verified") is not True or selected.get("observed_model") != model:
                return None, None
            effort = _clean(selected.get("requested_reasoning_effort"), 40) or effort
        return model, effort
    except _SAFE_ERRORS:
        return None, None


def _art_operation(cfg, state, owned):
    """Read only current-run metadata; never scan an arbitrary or historical log."""
    from .dashboard_state import _codex_route, _operation_model, _task_session
    if not owned:
        return None, None, False
    operation, task = state["operation"], state["task"]
    runtime = Path(cfg["runtime_dir"])
    try:
        directory = _confined(runtime / "runs", Path(operation["output_dir"]))
        if operation["kind"] == "review":
            _, directory = _codex_route(runtime, directory, "review")
        receipt = _object(_confined(runtime, directory / "process.json"), missing={}, maximum=16384)
        finished = receipt.get("kind") == operation["kind"] and (
            type(receipt.get("exit_code")) is int or receipt.get("launch_or_capture_failed") is True)
        if operation["kind"] == "art_diagnose":
            model, effort = _diagnosis_model(runtime, directory, operation, task, receipt)
            return model, effort, finished
        if operation["kind"] == "art_check":
            check = _object(_confined(runtime, directory / "check.json"), missing={}, maximum=65536)
            finished = (check.get("id") == "art-pipeline-verify" and type(check.get("passed")) is bool
                        and bool(operation.get("head_sha")) and check.get("head_sha") == operation["head_sha"])
        if operation["kind"] != "code":
            return None, None, finished
        session, _ = _task_session(runtime, task, operation, "CODE", "claude")
        model, effort = _operation_model(runtime, operation, directory, session)
        if model and receipt.get("kind") == "code" and receipt.get("session_id") == session:
            selection = receipt.get("model_selection") or {}
            if selection.get("model_verified") is True and selection.get("observed_model") == model:
                effort = _clean(selection.get("requested_reasoning_effort"), 40) or effort
        return model, effort, finished
    except _SAFE_ERRORS:
        return None, None, False


def _art_gate(art):
    if art.get("error") or art.get("status") == "error":
        return "stale", art.get("error") or "美术状态待核实"
    if art.get("paused") or art.get("status") == "paused":
        return "paused", (art.get("maintenance") or {}).get("summary") or "美术流程已暂停"
    if art.get("status") == "blocked" or art.get("phase") == "BLOCKED":
        return "blocked", art.get("blocking_summary") or art.get("last_error") or "需要处理"
    waiting = _wait_summary(art.get("provider_retry"))
    if waiting:
        return "waiting", waiting
    worker = art.get("worker") or {}
    if worker.get("alive") is not True:
        return "waiting", "美术后台已停止"
    if worker.get("healthy") is not True:
        return "stale", "美术后台心跳待核实"
    return None, None


def _art(main, snapshot, now):
    art = snapshot.get("art") or {}
    ids = NODE_IDS["art"]
    edges = [{"from": a, "to": b, "label": "", "kind": "next"} for a, b in zip(ids, ids[1:])]
    edges.extend({"from": start, "to": "queue", "label": "同任务返工", "kind": "rework"}
                 for start in ("check", "review", "approval"))
    nodes = [_node("art", key, title, actor=actor, state="idle" if art.get("configured") else "disabled")
             for key, title, actor in zip(ids, ART_TITLES, ART_ACTORS)]
    rows = {row["id"]: row for row in nodes}
    if not art.get("configured"):
        return _lane("art", "美术制作", None, nodes, edges, "尚未配置独立美术执行器")
    cfg, state, owned, progress = {}, {}, False, None
    read_error = None
    try:
        cfg, state, owned, progress = _art_context(main, art, now)
    except _SAFE_ERRORS:
        read_error = "美术断点暂时无法核实，等待下一次快照。"
    phase = state.get("blocked_from") if state.get("phase") == "BLOCKED" else art.get("phase")
    current = {"CODE": "queue", "ART_DIAGNOSE": "queue", "ART_CHECK": "check", "ART_REVIEW": "review"}.get(phase)
    selected = next((row for row in art.get("requests", []) if row.get("id") == art.get("task_id")), None)
    if selected is None and not art.get("task_id"):
        selected = next((row for row in reversed(art.get("requests", [])) if row.get("status") in {"awaiting_approval", "accepted"}), None)
    if current is None:
        current = "approval" if selected and selected.get("status") == "awaiting_approval" else "queue"
    row = selected or {}
    preview = bool(phase not in {"CODE", "ART_DIAGNOSE"} and row.get("preview_sha256") and row.get("previews") and not row.get("preview_error"))
    completed = set()
    if preview and row.get("preview_kind") in {"technical", "review"}:
        completed.update(ids[:6])
    if preview and ((row.get("technical_review") or {}).get("decision") == "accepted"):
        completed.update(("check", "review"))
    task = state.get("task") or {}
    if (preview and (task.get("art_review") or {}).get("review_package_sha256") == row.get("preview_sha256") and
            any(check.get("id") == "art-pipeline-verify" and check.get("passed") is True and
                bool(check.get("head_sha")) and check.get("head_sha") == (task.get("art_workspace_head") or task.get("base_sha"))
                for check in task.get("check_results", []) if isinstance(check, dict))):
        completed.add("check")
    if selected and selected.get("status") == "accepted" and not art.get("task_id"):
        completed.add("approval")
        current = "promotion"
    promotions = [p for p in art.get("promotions", []) if selected and p.get("source_task_id") == selected.get("id")]
    promotion = promotions[-1] if promotions else None
    if promotion and selected.get("status") == "accepted" and not art.get("task_id"):
        current = "promotion"
        if promotion.get("status") == "integrated":
            completed.add("promotion")
    if progress:
        stage = progress["stage"]
        current = {"generated": "model", "processed": "blender", "imported": "ue_import",
                   "pending_visual_acceptance": "ue_capture", "failed": "queue"}.get(stage, stage)
        # concept/model may be repeated for several assets; don't claim the batch done early.
        if stage not in {"concept", "model", "failed"}:
            completed.update(ids[:ids.index(current)])
        if stage in {"generated", "processed", "imported", "pending_visual_acceptance"}:
            completed.add(current)
    for identity in completed:
        rows[identity].update(state="completed", state_label=LABELS["completed"], summary="当前版本已有对应阶段证据。")
    active = rows[current]
    active["current"] = True
    model, effort, finished = _art_operation(cfg, state, owned) if cfg else (None, None, False)
    rows["queue"].update(model=model or ART_CODING_MODEL, effort=effort if model else ART_CODING_REASONING_EFFORT,
                         model_source="recorded" if model else "configured")
    rows["review"].update(model=(cfg.get("codex") or {}).get("model"), effort=(cfg.get("codex") or {}).get("reasoning_effort"),
                          model_source="configured" if (cfg.get("codex") or {}).get("model") else None)
    summary = art.get("label") or "等待任务"
    if current == "queue" and phase == "ART_DIAGNOSE":
        active.update(title="Claude 自主诊断与调整", actor="Claude Code")
        summary = ("本次诊断已结束，等待控制器按既有授权接续制作。" if finished else
                   "Claude 在原制作会话的只读分支复盘证据并制定调整计划，返回后按既有授权接续制作。")
        summary += "诊断计划不代表制作完成或视觉验收通过。"
    elif current == "queue" and phase == "CODE":
        summary = "Claude 编写制作脚本、读取参考或准备固定制作入口；尚无本次工具阶段回执。"
        active.update(model=model or ART_CODING_MODEL, effort=effort if model else ART_CODING_REASONING_EFFORT,
                      model_source="recorded" if model else "configured")
    elif current == "review":
        active.update(model=(cfg.get("codex") or {}).get("model"), effort=(cfg.get("codex") or {}).get("reasoning_effort"),
                      model_source="configured" if (cfg.get("codex") or {}).get("model") else None)
    if progress:
        summary = progress["label"] + (" · " + progress["asset"] if progress.get("asset") else "") + "；更新于 " + (_local_time(progress["updated_at"]) or "时间待核实")
    active["state"] = "running" if owned and not finished and current not in completed else "completed" if current in completed else "ready"
    if current == "approval":
        active["state"], summary = "waiting", "等待用户视觉验收；技术通过不能代替用户接受。"
        if row.get("accept_block_reason"):
            active["blocked_reason"] = _clean(row["accept_block_reason"], 1200)
    if current == "promotion":
        active["state"] = "completed" if "promotion" in completed else "blocked" if promotion and promotion.get("status") == "blocked" else "waiting"
        summary = "批准资产已入库；游戏功能绑定另行开发。" if "promotion" in completed else _clean((promotion or {}).get("message"), 600) or "等待批准资产入库。"
        if active["state"] == "blocked":
            active["blocked_reason"] = summary
    gate, reason = _art_gate(art)
    if read_error and not gate:
        gate, reason = "stale", read_error
    if gate:
        active["state"], active["blocked_reason"] = gate, _clean(reason, 1200)
        summary = _clean(reason, 1200) + ("；" + summary if progress else "")
    elif progress and progress["stage"] == "failed":
        active["state"], active["blocked_reason"] = "blocked", "制作入口报告失败，请查看本次日志。"
        summary = active["blocked_reason"]
    elif current == "queue" and phase == "PLAN" and art.get("status") == "idle":
        active["state"] = "idle"
    active.update(state_label=LABELS[active["state"]], summary=_text(summary, 1000),
                  blocked_reason=_text(active.get("blocked_reason"), 1200) or None)
    if current != "promotion":
        accepted_ids = {item.get("id") for item in art.get("requests", []) if item.get("status") == "accepted"}
        outstanding = [p for p in art.get("promotions", []) if p.get("source_task_id") in accepted_ids
                       and p.get("source_task_id") != art.get("task_id") and p.get("status") != "integrated"]
        pending = next((p for p in outstanding if p.get("status") == "blocked"), outstanding[0] if outstanding else None)
        if pending:
            pending_state = "blocked" if pending.get("status") == "blocked" else "waiting"
            pending_summary = "另一个已批准批次 " + _clean(pending.get("batch_id") or pending.get("source_task_id"), 120) + "：" + (
                _text(pending.get("message") or pending.get("last_error"), 600) or "等待入库。")
            rows["promotion"].update(state=pending_state, state_label=LABELS[pending_state], summary=pending_summary,
                                     blocked_reason=pending_summary if pending_state == "blocked" else None)
    identity = art.get("task_id") or row.get("id")
    if identity:
        summary = "当前任务 " + _clean(identity, 100) + " · " + summary
    return _lane("art", "美术制作", current, nodes, edges, summary)


def _operations(snapshot):
    nodes = []
    for identity, title, worker, enabled in (
            ("development_worker", "功能开发后台", snapshot.get("worker") or {}, True),
            ("art_worker", "美术后台", (snapshot.get("art") or {}).get("worker") or {}, bool((snapshot.get("art") or {}).get("configured")))):
        state = "disabled" if not enabled else "waiting" if worker.get("alive") is not True else "stale" if worker.get("healthy") is not True else "running"
        summary = "未启用" if not enabled else "后台已停止" if worker.get("alive") is not True else "后台心跳待核实" if worker.get("healthy") is not True else "后台在线；任务是否执行由工作流程状态决定。"
        nodes.append(_node("operations", identity, title, actor="后台监督器", state=state, summary=summary,
                           reason=summary if state in {"waiting", "stale"} else None))
    notify = snapshot.get("notifications") or {}
    state = "disabled" if notify.get("enabled") is not True else "blocked" if notify.get("last_error") else "ready"
    summary = "通知未启用" if state == "disabled" else "通知已启用；尚无已确认的成功投递记录。"
    if notify.get("enabled") is True and notify.get("last_sent_at"):
        summary = "通知已启用；最近成功投递 " + (_local_time(notify["last_sent_at"]) or "时间待核实")
    if state == "blocked":
        summary = "通知投递异常：" + _clean(notify["last_error"], 400)
    nodes.append(_node("operations", "notifications", "通知服务", actor="飞书通知器", state=state,
                       summary=summary, reason=summary if state == "blocked" else None))
    return _lane("operations", "后台与通知", None, nodes,
                 [{"from": key, "to": "notifications", "label": "只读状态事件", "kind": "observe"}
                  for key in ("development_worker", "art_worker")], "后台监督运行进程；通知服务独立观察事件并记录投递结果。")


def collect_flows(config_path, snapshot):
    """No locks, mutations, subprocesses, controllers, private ledger or network."""
    try:
        config = load_config(Path(config_path))
    except _SAFE_ERRORS:
        config = {}
    return {"development": _development(snapshot, config), "art": _art(config, snapshot, datetime.now(timezone.utc)),
            "operations": _operations(snapshot)}
