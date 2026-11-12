"""Bounded public art-lane views and hash-bound PNG previews; no controller polling."""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from pathlib import Path
import re
from urllib.parse import urlencode

from .config import ART_CODING_MODEL, ART_CODING_REASONING_EFFORT, load_config

MAX_PREVIEW_BYTES = 16 * 1024 * 1024
# Asset manifests enumerate thousands of versioned meshes and textures. Keep
# their bounded reader independent of the smaller controller JSON limit.
MAX_REVIEW_PACKAGE_BYTES = 16 * 1024 * 1024
SHA = re.compile(r"[0-9a-f]{64}")
TASK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
PRODUCTION_STAGES = {"concept": "生成概念图", "model": "生成三维模型", "generated": "源模型已就绪",
                     "blender": "Blender 模型处理", "processed": "模型处理已完成", "ue_import": "导入 UE 展示工程",
                     "imported": "UE 导入已完成", "ue_capture": "生成 UE 展示图",
                     "pending_visual_acceptance": "展示已生成，等待技术检查", "failed": "资产处理失败"}


def worker_config(main_config):
    route = main_config.get("art_worker")
    if not isinstance(route, dict) or set(route) != {"config_file"}:
        raise ValueError("尚未配置独立美术执行器。")
    config = load_config(Path(route["config_file"]))
    if (not config.get("art_pipeline") or config.get("coding_provider") != "claude" or
            config.get("push") or config.get("publication") or config.get("art_worker")):
        raise ValueError("美术执行器必须使用独立 Claude 配置，不能直接发布功能分支。")
    main_roots = [Path(main_config[key]).resolve() for key in ("project_path", "runtime_dir")]
    art_roots = [Path(config[key]).resolve() for key in ("project_path", "runtime_dir")]
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a) for a in main_roots for b in art_roots):
        raise ValueError("美术项目和运行目录必须与功能开发目录隔离。")
    return config


def _queue(config):
    from .dashboard_state import _confined, _object
    runtime = Path(config["runtime_dir"])
    value = _object(_confined(runtime, runtime / "art-queue.json"), missing={"version": 1, "requests": []})
    if (value.get("version") != 1 or not isinstance(value.get("requests"), list) or len(value["requests"]) > 200 or
            any(not isinstance(item, dict) for item in value["requests"])):
        raise ValueError("美术队列格式或大小无效。")
    return value["requests"]


def _state(config):
    """Use the same configuration-bound checkpoint for snapshots and image reads."""
    import json
    from .dashboard_state import _confined, _object
    runtime = Path(config["runtime_dir"])
    state = _object(_confined(runtime, runtime / "state.json"), missing={})
    if state:
        values = {key: config.get(key) for key in ("project_path", "goal", "branch", "base_ref", "checks", "push", "remote", "task_prefix")}
        binding = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if state.get("version") != 1 or state.get("binding") != binding:
            raise ValueError("美术断点与独立配置不匹配。")
    if state.get("phase", "PLAN") not in {"PLAN", "CODE", "ART_DIAGNOSE", "ART_CHECK", "ART_REVIEW", "BLOCKED"}:
        raise ValueError("美术执行器断点阶段无效。")
    return state


def _preview_source(config, item, state):
    """Keep approval evidence separate from an active task's technical evidence."""
    if item.get("status") in {"awaiting_approval", "accepted"}:
        return "review", item
    task = state.get("task")
    phase = state.get("blocked_from") if state.get("phase") == "BLOCKED" else state.get("phase")
    if (item.get("status") == "active" and state.get("phase") in {"CODE", "ART_DIAGNOSE"} and isinstance(task, dict) and
            task.get("kind") == "art" and task.get("id") == item.get("id") and task.get("art_review") is None):
        return None, None  # A normal rework has not produced this attempt's evidence yet.
    if (item.get("status") != "active" or phase not in {"ART_CHECK", "ART_REVIEW"} or
            not isinstance(task, dict) or task.get("kind") != "art" or task.get("id") != item.get("id")):
        raise ValueError("当前任务尚无已绑定的技术预览。")
    request, evidence = task.get("art_request"), task.get("art_review")
    if not isinstance(request, dict) or not isinstance(evidence, dict):
        raise ValueError("当前任务尚无已绑定的技术预览。")
    root = Path(config["art_pipeline"]["artifacts_dir"]).resolve()
    if (Path(request.get("artifacts_dir", "")).resolve() != root or
            Path(item.get("artifacts_dir", "")).resolve() != root or
            Path(evidence.get("review_package", "")).resolve() != root / "review-package.json"):
        raise ValueError("当前技术预览目录与独立配置不一致。")
    return "technical", {**item, "review_package_sha256": evidence.get("review_package_sha256")}


def _package(config, item, expected_sha):
    from .dashboard_state import _confined
    if not isinstance(expected_sha, str) or not SHA.fullmatch(expected_sha):
        raise ValueError("验收包版本无效。")
    if item.get("review_package_sha256") != expected_sha:
        raise ValueError("验收包版本已变化，请刷新。")
    root = Path(config["art_pipeline"]["artifacts_dir"])
    if Path(item.get("artifacts_dir", "")).resolve() != root.resolve():
        raise ValueError("美术队列产物目录与配置不一致。")
    path = _confined(root, root / "review-package.json")
    with path.open("rb") as stream:
        raw = stream.read(MAX_REVIEW_PACKAGE_BYTES + 1)
    if len(raw) > MAX_REVIEW_PACKAGE_BYTES:
        raise ValueError("验收清单超过网页读取上限，暂不能确认此版本。")
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("验收包已变化，请重新查看当前版本。")
    import json
    package = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(package, dict):
        raise ValueError("验收包格式无效。")
    capture = package.get("capture")
    shots = capture.get("shots") if isinstance(capture, dict) else None
    if not isinstance(shots, list) or not 1 <= len(shots) <= 32 or any(not isinstance(shot, dict) for shot in shots):
        raise ValueError("验收包截图列表无效。")
    return root, package, shots


def preview_png(main_config, task_id, review_sha, index):
    """Serve a PNG only from the current approval or bound technical package."""
    from .dashboard_state import _confined
    if not isinstance(task_id, str) or not TASK.fullmatch(task_id) or type(index) is not int or not 0 <= index < 32:
        raise ValueError("预览请求无效。")
    config = worker_config(main_config)
    item = next((entry for entry in _queue(config) if entry.get("id") == task_id), None)
    if item is None:
        raise ValueError("美术任务不存在。")
    _, source = _preview_source(config, item, _state(config))
    if source is None:
        raise ValueError("正在制作新版，当前技术预览尚未绑定。")
    root, _, shots = _package(config, source, review_sha)
    if index >= len(shots):
        raise ValueError("截图编号无效。")
    shot = shots[index]
    path = _confined(root, Path(shot["path"]))
    if path.suffix.lower() != ".png" or not SHA.fullmatch(str(shot.get("sha256", ""))):
        raise ValueError("只允许已绑定的 PNG 截图。")
    with path.open("rb") as stream:
        data = stream.read(MAX_PREVIEW_BYTES + 1)
    if (len(data) > MAX_PREVIEW_BYTES or data[:8] != b"\x89PNG\r\n\x1a\n" or
            hashlib.sha256(data).hexdigest() != shot["sha256"]):
        raise ValueError("截图内容或版本校验失败。")
    return data


def _backlog(runtime):
    """Planning inventory only: no queue/controller/paid-generation entry point."""
    from .dashboard_state import _clean, _confined, _object
    data = _object(_confined(runtime, runtime / "art-backlog.json"),
                   missing={"version": 1, "batches": []}, maximum=256 * 1024)
    batches = data.get("batches")
    if data.get("version") != 1 or not isinstance(batches, list) or len(batches) > 64:
        raise ValueError("美术计划批次格式或大小无效。")
    result, identities = [], set()
    for batch in batches:
        if (not isinstance(batch, dict) or not TASK.fullmatch(str(batch.get("id", ""))) or
                batch["id"] in identities or not isinstance(batch.get("title"), str) or
                not batch["title"].strip() or type(batch.get("priority")) is not int or
                not 0 <= batch["priority"] <= 999 or
                batch.get("status") not in {"waiting_budget", "waiting_sample_acceptance"}):
            raise ValueError("美术计划必须有唯一编号、标题、优先级和等待条件。")
        identities.add(batch["id"])
        row = {"id": batch["id"], "title": _clean(batch["title"], 300), "priority": batch["priority"],
               "status": batch["status"], "dispatchable": False}
        for key, maximum in (("definition_ids", 128), ("levels", 32)):
            values = batch.get(key, [])
            if not isinstance(values, list) or len(values) > maximum or any(not isinstance(value, str) for value in values):
                raise ValueError("美术计划的定义或等级列表无效。")
            row[key] = [_clean(value, 160) for value in values]
        result.append(row)
    return sorted(result, key=lambda batch: (batch["priority"], batch["id"]))


def _budget(package):
    """Project the current review's public cost snapshot, never the private ledger."""
    report = package.get("budget")
    value = report.get("budget") if isinstance(report, dict) else None
    if not isinstance(value, dict):
        raise ValueError("当前验收版本未提供可核实的预算快照。")
    result = {}
    for key in ("actual", "reserved", "remaining", "limit", "unknown", "committed"):
        amount = value.get(key)
        try:
            valid = type(amount) in (int, float) and math.isfinite(amount) and amount >= 0
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("当前验收版本的预算金额无效。")
        result[key] = amount
    committed = sum(float(result[key]) for key in ("actual", "reserved", "unknown"))
    if (result["limit"] <= 0 or not math.isfinite(committed) or
            not math.isclose(result["committed"], committed, rel_tol=1e-9, abs_tol=1e-6) or
            not math.isclose(result["remaining"], max(0, result["limit"] - committed), rel_tol=1e-9, abs_tol=1e-6)):
        raise ValueError("当前验收版本的预算合计不一致。")
    return result


def _technical_review(state, item, review_sha):
    """A technical pass is separate from the user's visual acceptance decision."""
    from .dashboard_state import _clean
    candidates = []
    for key in ("art_pending", "art_history"):
        records = state.get(key)
        if isinstance(records, list):
            candidates.extend(reversed(records))
    candidates.append(item)
    for record in candidates:
        if (not isinstance(record, dict) or record.get("id") != item["id"] or
                record.get("review_package_sha256") != review_sha):
            continue
        review = record.get("technical_review")
        if (not isinstance(review, dict) or not isinstance(review.get("decision"), str) or
                review["decision"] not in {"accepted", "rework", "blocked"}):
            continue
        if record.get("head_sha") and review.get("head_sha") != record["head_sha"]:
            continue
        summary = _clean(review.get("summary"), 1000)
        if summary:
            return {"decision": review["decision"], "summary": summary,
                    "feedback": _clean(review.get("feedback"), 2400), "review_package_sha256": review_sha}
    return None


def _accept_block_reason(row, package):
    """Project existing acceptance gates; POST still rechecks live evidence/costs."""
    if not row["can_resolve"]:
        if row.get("preview_kind") == "technical":
            return "当前仅展示技术预览；尚未提交用户视觉验收。"
        if row.get("status") == "active" and not row.get("preview_error"):
            return "正在制作新版，待绑定当前技术预览。"
        return row["preview_error"] or ("已提交验收决定，请等待处理。" if row["decision"] else
                                         "当前任务尚未提交有效的验收版本。")
    budget = row["budget"]
    if budget is None:
        return row["budget_error"] or "当前验收版本的预算尚未核实。"
    # art_pipeline.load_config fixes the authorized preview batch cap at 3000.
    if budget["limit"] != 3000:
        return "预算上限与本批次已授权的 3000 积分不一致。"
    breached = package["budget"]["budget"].get("breached")
    if budget["committed"] > budget["limit"] or breached is True:
        return "本批次预算已超限，暂不能接受。"
    if breached is not False:
        return "当前验收版本的预算状态尚未核实。"
    if budget["reserved"] != 0 or budget["unknown"] != 0:
        return "仍有预留或待确认费用，请待费用结清后验收。"
    return None


def _blocking_text(state):
    from .dashboard_state import _clean
    if state.get("phase") != "BLOCKED":
        return None, None
    raw = state.get("last_error")
    raw = raw if isinstance(raw, str) else ""
    cleaned = _clean(raw, 64 * 1024)
    detail = _clean(cleaned, 7999)
    summary = detail
    # Keep the recorded wording; foreground actual defects over passed checks.
    for heading in ("可观察缺陷", "必须修复", "阻塞及解除条件"):
        found = re.search(re.escape(heading) + r"\s*[:：]", cleaned)
        if found:
            summary = cleaned[found.start():]
            break
    return detail or None, _clean(summary, 599) or None


def _production_progress(config, state, now):
    """Expose only fresh, fixed stage metadata from this task's current CODE call."""
    from .dashboard_state import _confined, _iso, _object
    try:
        task, operation = state.get("task") or {}, state.get("operation") or {}
        if (state.get("phase") != "CODE" or task.get("kind") != "art" or
                not TASK.fullmatch(str(task.get("id", ""))) or operation.get("kind") != "code" or
                operation.get("task_id") != task["id"]):
            return None
        settings, request = config["art_pipeline"], task["art_request"]
        root = Path(settings["artifacts_dir"]).resolve()
        batch_file = Path(settings["config_file"]).resolve()
        if (Path(request.get("artifacts_dir", "")).resolve() != root or
                Path(request.get("config_file", "")).resolve() != batch_file):
            return None
        batch = _object(_confined(batch_file.parent, batch_file), maximum=128 * 1024)
        if Path(batch.get("artifacts_dir", "")).resolve() != root:
            return None
        value = _object(_confined(root, root / "pipeline-state.json"), missing=None, maximum=64 * 1024)
        if not value or not isinstance(batch.get("batch_id"), str) or value.get("batch_id") != batch["batch_id"]:
            return None
        stage, updated = value.get("stage"), value.get("updated_epoch")
        started = _iso(operation.get("started_at"))
        if (not isinstance(stage, str) or stage not in PRODUCTION_STAGES or not started or
                type(updated) not in (int, float) or not math.isfinite(updated) or
                updated < datetime.fromisoformat(started).timestamp() or updated > now.timestamp() + 30):
            return None
        asset = value.get("asset") if stage in {"concept", "model"} else None
        approved_ids = {entry.get("asset_id") for entry in batch.get("assets", []) if isinstance(entry, dict)
                        and isinstance(entry.get("asset_id"), str)}
        if not isinstance(asset, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,100}", asset) or asset not in approved_ids:
            asset = None
        return {"stage": stage, "label": PRODUCTION_STAGES[stage], "asset": asset,
                "updated_at": datetime.fromtimestamp(updated, timezone.utc).isoformat()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
        return None


def _diagnosis_response(config, state):
    """Read the exact blocked receipt; this projection never constructs a worker."""
    import json
    from .dashboard_state import _confined, _object
    from .dashboard_evidence import _diagnosis_result, redact
    task = state.get("task") or {}
    pending = task.get("art_diagnosis") or {}
    if (state.get("phase") != "BLOCKED" or state.get("blocked_from") != "ART_DIAGNOSE" or
            state.get("operation") is not None or task.get("kind") != "art" or pending.get("status") != "waiting_user"):
        return None
    identity, number = task.get("id"), pending.get("round")
    if not TASK.fullmatch(str(identity or "")) or type(number) is not int or not 1 <= number <= 10000:
        raise ValueError("待回应的诊断任务或轮次无效，请刷新核对。")
    runtime = Path(config["runtime_dir"]).absolute()
    path = _confined(runtime, runtime / "tasks" / identity / f"diagnosis-{number}.json")
    if Path(pending.get("receipt_path", "")).absolute() != path:
        raise ValueError("待回应诊断回执路径已变化，请刷新核对。")
    with path.open("rb") as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("待回应诊断回执过大，请刷新核对。")
    receipt = json.loads(raw.decode("utf-8-sig"))
    source = pending.get("fingerprint")
    history = task.get("art_diagnosis_history")
    head = task.get("art_workspace_head") or (task.get("candidate") or {}).get("head_sha") or task.get("code_base") or state.get("expected_head")
    result = _diagnosis_result(receipt.get("result"), head) if isinstance(receipt, dict) else None
    if (not result or not isinstance(source, dict) or source.get("head_sha") != head or
            not isinstance(history, list) or not history or history[-1] != receipt or
            receipt.get("art_autonomy_epoch", 0) != task.get("art_autonomy_epoch", 0) or
            receipt.get("task_id") != identity or receipt.get("round") != number or
            receipt.get("head_sha") != head or receipt.get("source_fingerprint") != source or
            receipt.get("execution_decision") != "waiting"):
        raise ValueError("诊断任务、证据或回执版本已变化，请刷新后重新查看。")
    receipt_sha = hashlib.sha256(raw).hexdigest()
    queue = _object(_confined(runtime, runtime / "art-diagnosis-responses.json"), missing={"version": 1, "responses": []}, maximum=16 * 1024 * 1024)
    if (queue.get("version") != 1 or not isinstance(queue.get("responses"), list) or len(queue["responses"]) > 500 or
            any(not isinstance(entry, dict) for entry in queue["responses"])):
        raise ValueError("诊断回应队列暂时无法核实，请刷新后核对。")
    queued = any(entry.get("task_id") == identity and entry.get("receipt_sha256") == receipt_sha and
                 entry.get("status") == "queued" for entry in queue["responses"])
    return {"task_id": identity, "receipt_sha256": receipt_sha, "status": "waiting_user", "response_status": "queued" if queued else None,
            "can_respond": not queued, "summary": redact(result["diagnosis"], 3000), "plan": redact(result["plan"], 6000),
            "reason": redact(receipt.get("controller_reason") or result["reason"], 1600),
            "questions": [redact(question, 800) for question in result["questions"][:20]]}


def _reload_maintenance(config, state, *, lane="art"):
    """Project only an owned reload marker; never invoke the reload manager."""
    from .dashboard_state import (_CLAUDE_SESSION_ID, CLAUDE_SESSION_HEADER_BYTES, OWNER,
                                  _confined, _iso, _native_code_active, _object, process_identity)
    import json
    if lane not in {"art", "development"}:
        return None
    runtime, config_path = Path(config["runtime_dir"]).absolute(), Path(config["config_path"]).resolve()
    prefix = "art-reload" if lane == "art" else "background-reload"
    pointer = _object(_confined(runtime, runtime / (prefix + "-request.json")), missing={}, maximum=4096)
    identity = pointer.get("id")
    if pointer.get("version") != 1 or not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{32}", identity):
        return None
    request = _object(_confined(runtime, runtime / "reload-history" / (identity + ".json")), missing={}, maximum=256 * 1024)
    stage = request.get("status")
    if (request.get("version") != 1 or request.get("id") != identity or request.get("lane", "art") != lane or stage not in {"waiting_boundary", "stopping", "starting", "completing"} or
            request.get("config_file") != str(config_path) or request.get("runtime_dir") != str(runtime) or
            request.get("config_sha256") != hashlib.sha256(config_path.read_bytes()).hexdigest()):
        return None
    keys = {"pid", "process_created", "image"}
    source, replacement = request.get("source_identity"), request.get("replacement_identity")
    valid_identity = lambda value: (isinstance(value, dict) and set(value) == keys and type(value["pid"]) is int and value["pid"] > 0 and
                                    type(value["process_created"]) is int and value["process_created"] > 0 and isinstance(value["image"], str) and bool(value["image"]))
    if not valid_identity(source) or replacement is not None and (not valid_identity(replacement) or replacement == source):
        return None
    health = _object(_confined(runtime, runtime / "background-health.json"), missing={}, maximum=65536)
    expected = replacement if stage == "completing" else source
    if (not valid_identity(expected) or health.get("owner") != OWNER or health.get("config") != str(config_path) or
            any(health.get(key) != expected[key] for key in keys)):
        return None
    try:
        actual = process_identity(expected["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        actual = None
    if actual is not None and any(actual.get(key) != expected[key] for key in keys) or stage in {"waiting_boundary", "completing"} and actual is None:
        return None
    pause = request.get("pause") or {}
    token = prefix + ":" + identity + "\n"
    marker = _confined(runtime, runtime / "PAUSE")
    if pause.get("mode") != "owned" or pause.get("token") != token or not marker.is_file():
        return None
    fact = marker.stat()
    if fact.st_size > 4096:
        return None
    raw = marker.read_bytes()
    if raw != token.encode("utf-8") or pause.get("fingerprint") != {"sha256": hashlib.sha256(raw).hexdigest(), "mtime_ns": fact.st_mtime_ns, "size": fact.st_size}:
        return None
    active = False
    task, operation = state.get("task") or {}, state.get("operation") or {}
    if (stage == "waiting_boundary" and state.get("phase") == "CODE" and (task.get("kind") == "art") == (lane == "art") and
            operation.get("kind") == "code" and operation.get("task_id") == task.get("id") and _iso(operation.get("started_at"))):
        directory = _confined(runtime, Path(operation.get("output_dir", "")))
        if directory.parent == runtime / "runs" and re.fullmatch(r"code-[a-f0-9]{32}", directory.name):
            receipt = _object(_confined(runtime, directory / "process.json"), missing={}, maximum=65536)
            finished = receipt.get("kind") == "code" and (type(receipt.get("exit_code")) is int or receipt.get("launch_or_capture_failed") is True)
            if not finished:
                try:
                    with _confined(runtime, directory / "stdout.log").open("rb") as stream:
                        header = stream.read(CLAUDE_SESSION_HEADER_BYTES)
                    first = next((line for line in header.split(b"\n")[:-1] if line.strip()), None)
                    opening = json.loads(first.decode("utf-8-sig")) if first else {}
                    session = opening.get("session_id")
                    active = (opening.get("type") == "system" and opening.get("subtype") == "init" and
                              isinstance(session, str) and bool(_CLAUDE_SESSION_ID.fullmatch(session)) and
                              session == task.get("session_id") and task.get("session_owner_task_id", task.get("id")) == task.get("id"))
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
            if lane == "development":
                active = _native_code_active(runtime, task, operation, directory)
    summary = ("Claude 完成当前调用后自动切换调度；暂停标记只限制下一步，当前调用会自然结束。" if active else
               "调度维护正在等待安全边界，随后自动切换。" if stage == "waiting_boundary" else
               "正在安全切换" + ("美术" if lane == "art" else "功能开发") + "调度，等待后台接续。")
    return {"status": stage, "current_call_active": active, "label": "当前调用后自动切换调度" if active else "等待调度安全切换", "summary": summary}


def collect_art_snapshot(main_config, now):
    from .dashboard_state import _clean, _confined, _object, _provider_retry, _worker
    result = {"configured": bool(main_config.get("art_worker")), "status": "not_configured",
              "label": "尚未配置", "requests": [], "promotions": [], "backlog": [], "backlog_error": None,
              "blocking_detail": None, "blocking_summary": None, "provider_retry": None,
              "production_progress": None, "diagnosis_response": None, "diagnosis_response_error": None, "maintenance": None, "error": None}
    if not result["configured"]:
        return result
    try:
        config = worker_config(main_config)
        runtime = Path(config["runtime_dir"])
        try:
            result["backlog"] = _backlog(runtime)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as error:
            result["backlog_error"] = _clean(str(error), 300)
        state = _state(config)
        try:
            result["maintenance"] = _reload_maintenance(config, state)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        try:
            result["diagnosis_response"] = _diagnosis_response(config, state)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            result["diagnosis_response_error"] = _clean(str(error), 400)
        phase = state.get("phase", "PLAN")
        retry = _provider_retry(state, "Claude Code", "Claude Code" if config.get("planning_provider") == "claude" else "Codex")
        result["provider_retry"] = retry
        result["production_progress"] = _production_progress(config, state, now)
        result["blocking_detail"], result["blocking_summary"] = _blocking_text(state)
        result.update(phase=phase, model=_clean((state.get("task") or {}).get("coding_model") or ART_CODING_MODEL, 120),
                      effort=_clean((state.get("task") or {}).get("coding_reasoning_effort") or ART_CODING_REASONING_EFFORT, 40),
                      worker=_worker(runtime, Path(config["config_path"]), now),
                      task_id=_clean((state.get("task") or {}).get("id"), 100),
                      last_error="" if retry else _clean(state.get("last_error"), 400),
                      updated_at=state.get("updated_at"),
                      paused=_confined(runtime, runtime / "PAUSE").exists())
        for item in _queue(config):
            if not isinstance(item, dict) or not TASK.fullmatch(str(item.get("id", ""))):
                raise ValueError("美术任务记录无效。")
            row = {key: _clean(item.get(key), 600 if key == "title" else 120)
                   for key in ("id", "title", "status", "enqueued_at", "review_package_sha256")}
            decision = item.get("decision")
            row["decision"] = {key: _clean(decision.get(key), 1000) for key in ("decision", "feedback", "recorded_at")} if isinstance(decision, dict) else None
            row.update(previews=[], can_resolve=False, reviewable=False, preview_kind=None, preview_sha256=None,
                       preview_error=None, budget=None, budget_error=None, technical_review=None)
            package = None
            if row["review_package_sha256"] or item.get("status") == "active":
                try:
                    kind, source = _preview_source(config, item, state)
                    if source is not None:
                        sha = source.get("review_package_sha256")
                        _, package, shots = _package(config, source, sha)
                        row.update(preview_kind=kind, preview_sha256=sha)
                        row["batch_id"] = _clean(package.get("batch_id"), 120)
                        row["approved_levels"] = [_clean(level, 20) for level in package.get("approved_levels", [])[:16]]
                        row["previews"] = [{"name": _clean(shot.get("name"), 120), "url": "/api/art/preview?" + urlencode({
                            "task": row["id"], "review": sha, "index": index})}
                            for index, shot in enumerate(shots)]
                        row["can_resolve"] = kind == "review" and item.get("status") == "awaiting_approval" and not decision
                        row["reviewable"] = row["can_resolve"]
                        try:
                            row["budget"] = _budget(package)
                        except ValueError as error:
                            row["budget_error"] = _clean(str(error), 300)
                        row["technical_review"] = _technical_review(state, item, sha) if kind == "review" else None
                except (OSError, ValueError, KeyError, TypeError) as error:
                    row["preview_error"] = _clean(str(error), 300)
            row["accept_block_reason"] = _accept_block_reason(row, package)
            row["can_accept"] = row["accept_block_reason"] is None
            result["requests"].append(row)
        promotion = _object(_confined(runtime, runtime / "promotion-requests.json"), missing={"requests": []})
        if not isinstance(promotion.get("requests"), list) or len(promotion["requests"]) > 200:
            raise ValueError("美术接入队列格式无效。")
        result["promotions"] = [{key: _clean(item.get(key), 400) for key in
                                 ("id", "batch_id", "source_task_id", "status", "requested_at", "last_error", "message", "head_sha", "gameplay_binding")}
                                for item in promotion["requests"][-50:]]
        statuses = {row["status"] for row in result["requests"]}
        status = "paused" if result["paused"] else "blocked" if phase == "BLOCKED" else phase.lower() if phase != "PLAN" else (
            "awaiting_approval" if "awaiting_approval" in statuses else "queued" if "queued" in statuses else "idle")
        result.update(status=status, label={"paused": "已暂停", "blocked": "需要处理", "code": "Claude 制作中",
            "art_diagnose": "Claude 自主诊断与调整", "art_check": "独立技术检查", "art_review": "技术审核中", "awaiting_approval": "等待视觉验收",
            "queued": "已排队", "idle": "待命"}[status])
        if result["maintenance"] and result["paused"]:
            result["label"] = result["maintenance"]["label"]
        if status not in {"paused", "blocked"} and retry and retry["status"] == "waiting":
            label = {"local_cli_unavailable": f"等待本地 {retry['provider']} 恢复",
                     "claude_subscription_limit": "等待 Claude 额度恢复",
                     "codex_subscription_limit": "等待 Codex 额度恢复"}.get(retry["reason"], "等待服务恢复")
            result.update(status="waiting_provider", label=label)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as error:
        result.update(status="error", label="美术状态待核实", error=_clean(str(error), 400))
    return result
