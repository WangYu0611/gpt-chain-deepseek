"""Bounded art review history, preserving authorship and receipt-level evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from .dashboard_art import _queue
from .dashboard_state import _confined, _object

MAX_ROUNDS = 12
MAX_DOCUMENT = 64 * 1024
MAX_MANIFEST = 4 * 1024 * 1024
MAX_PROSE = 32 * 1024
SHA = re.compile(r"[0-9a-f]{64}")
HEAD = re.compile(r"[0-9a-f]{40}")
CAPTURE = re.compile(r"screenshots[\\/]([0-9a-f]{12}-[0-9a-f]{12})(?:[\\/]|\b)")


def _read(root, path, maximum=MAX_DOCUMENT):
    path = _confined(root, path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("历史文档超过读取上限")
        return raw.decode("utf-8-sig"), hashlib.sha256(raw).hexdigest(), path
    except FileNotFoundError:
        return None, None, path


def _iso(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc).isoformat() if parsed.utcoffset() is not None else None
    except ValueError:
        return None


def _sections(text):
    """Split existing headings only; these are reviewer words, not new judgments."""
    rows = []
    for paragraph in re.split(r"\n\s*\n|\n(?=[^\n：:]{1,24}[：:])", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        match = re.match(r"^([^\n：:]{1,36})[：:]", paragraph)
        title = match[1].strip(" *#") if match else "审核原文"
        kind = ("completed" if title.startswith(("已修复", "已完成")) else
                "required" if title.startswith(("必须", "材料需", "材料差异", "屋顶")) else
                "limitations" if title.startswith(("证据限制", "范围", "尺度", "阻塞")) else "other")
        rows.append({"title": title, "text": paragraph, "kind": kind})
    return rows[:32]


def _public_root(config, task):
    settings = config.get("art_pipeline") or {}
    request = task.get("art_request") or {}
    if (task.get("id") not in settings.get("task_ids", []) or
            request.get("config_file") != settings.get("config_file") or
            Path(request.get("artifacts_dir", "")).absolute() != Path(settings.get("artifacts_dir", "")).absolute()):
        return None
    path = Path(settings["config_file"])
    if not SHA.fullmatch(str(request.get("config_sha256", ""))):
        return None
    with path.open("rb") as stream:
        raw = stream.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != request["config_sha256"]:
        return None
    return Path(settings["artifacts_dir"]).absolute()


def _screenshots(child, root, text, label):
    """Only a directory explicitly cited in this round can select a receipt."""
    ids = list(dict.fromkeys(CAPTURE.findall(text)))
    if len(ids) != 1:
        return [], "该轮没有唯一明确的截图目录引用；未借用其他轮次截图。"
    identity = ids[0]
    receipt_path = _confined(root, root / "receipts" / (identity + "-capture.json"))
    raw, digest, _ = _read(root, receipt_path)
    if raw is None:
        return [], "该轮引用的截图回执缺失；旧完整验收包未归档或无法核实。"
    receipt = json.loads(raw)
    shots = receipt.get("shots") if isinstance(receipt, dict) else None
    if (receipt.get("status") != "passed" or receipt.get("real_rhi") is not True or
            receipt.get("capture_id") != identity or not isinstance(shots, list) or len(shots) != 8):
        raise ValueError("历史截图回执身份或八图列表不匹配")
    selected = []
    for shot in shots:
        if not isinstance(shot, dict) or not isinstance(shot.get("path"), str):
            raise ValueError("历史截图条目无效")
        path = _confined(root, Path(shot["path"]))
        if (path.parent != root / "screenshots" / identity or path.suffix.lower() != ".png" or
                not SHA.fullmatch(str(shot.get("sha256", ""))) or
                type(shot.get("size")) is not int or not 0 < shot["size"] <= 16 * 1024 * 1024 or
                not path.is_file() or path.stat().st_size != shot["size"]):
            raise ValueError("历史截图路径、大小或摘要不可核实")
        selected.append((path, shot))
    if len({path for path, _ in selected}) != 8:
        raise ValueError("历史截图列表存在重复文件")
    # A receipt edit invalidates every image reference, even before PNG download.
    child.scope = {**child.scope, "capture_receipt_sha256": digest}
    child.add(root, receipt_path, label + " · 引用截图回执（非完整旧包）", mode="json", expected=digest)
    for path, shot in selected:
        child.add(root, path, label + " · " + str(shot.get("name") or path.stem), mode="png", expected=shot["sha256"])
    return [row for row in child.artifacts if row["kind"] == "png"], "该轮引用截图回执；下载时逐图核对 SHA。旧完整验收包未归档或无法核实，不代表当前验收版本。"


def _job_commit(task, head):
    relative = "art-jobs/" + task["id"] + "/blender_job.py"
    return next((item for item in task.get("art_recipe_commits", [])
                 if isinstance(item, dict) and item.get("head_sha") == head
                 and isinstance(item.get("files"), dict)
                 and SHA.fullmatch(str(item["files"].get(relative, "")))), None)


def _audit_images(child, root, feedback, capture_text, label):
    """Resolve only audit PNGs named by this reviewer through its import digest."""
    captures = list(dict.fromkeys(CAPTURE.findall(capture_text)))
    explicit = re.findall(r"processed[\\/]([0-9a-f]{12})[\\/]claude_job_audit[\\/]([A-Za-z0-9_-]{1,100}\.png)(?![A-Za-z0-9_.-])", feedback)
    named = re.findall(r"(?<![A-Za-z0-9_])(L[148]_house_(?:before|after)_[A-Za-z0-9_]{1,64}\.png)(?![A-Za-z0-9_.-])", feedback)
    names = list(dict.fromkeys([name for _, name in explicit] + named))
    if not names:
        return []
    if len(captures) != 1 or len(names) > 8:
        raise ValueError("审计图引用不能绑定唯一截图批次或超出数量上限")
    run = captures[0].split("-", 1)[0]
    if any(version != run for version, _ in explicit):
        raise ValueError("审核引用审计图与该轮截图批次不一致")
    raw, import_sha, _ = _read(root, root / "receipts" / (run + "-import.json"), MAX_MANIFEST)
    if raw is None:
        raise ValueError("该轮导入回执缺失，不能核实审计图")
    imported = json.loads(raw)
    if not isinstance(imported, dict) or imported.get("status") != "passed" or imported.get("run") != run:
        raise ValueError("该轮导入回执身份不匹配")
    folder = root / "processed" / run
    raw, manifest_sha, _ = _read(root, folder / "assets_manifest.json", MAX_MANIFEST)
    if raw is None or imported.get("manifest_sha256") != manifest_sha:
        raise ValueError("该轮处理清单与导入回执摘要不匹配")
    manifest = json.loads(raw)
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list) or len(files) > 10000:
        raise ValueError("该轮审计图清单无效")
    selected = []
    for name in names:
        relative = "claude_job_audit/" + name
        records = [item for item in files if isinstance(item, dict) and item.get("path") == relative]
        if len(records) != 1 or not SHA.fullmatch(str(records[0].get("sha256", ""))):
            raise ValueError("审核点名审计图不在该轮导入清单中")
        item = records[0]; path = _confined(root, folder / relative)
        if (type(item.get("size_bytes")) is not int or not 0 < item["size_bytes"] <= 16 * 1024 * 1024 or
                not path.is_file() or path.stat().st_size != item["size_bytes"]):
            raise ValueError("审核点名审计图大小不可核实")
        selected.append((path, item))
    child.scope = {**child.scope, "audit_import_sha256": import_sha, "audit_manifest_sha256": manifest_sha}
    begin = len(child.artifacts)
    for path, item in selected:
        child.add(root, path, label + " · Blender 审计 · " + path.stem, mode="png", expected=item["sha256"])
    return child.artifacts[begin:]


def _capability_time(task):
    records = [item for item in task.get("maintenance_history", []) if isinstance(item, dict)
               and item.get("system_change") == "versioned job execution; no authored model geometry"
               and HEAD.fullmatch(str(item.get("head_sha", ""))) and _iso(item.get("at"))]
    return min((_iso(item["at"]) for item in records), default=None)


def collect_reworks(config, state, task, evidence, *, redact, check_files):
    runtime = Path(config["runtime_dir"]).absolute()
    directory = _confined(runtime, runtime / "tasks" / task["id"])
    current = task.get("reworks")
    if type(current) is not int or not 0 <= current <= 10000:
        raise ValueError("返工轮次无效")
    # Exact numbered reads only: never walk runs, workspaces, chats or other tasks.
    indices = [0] + list(range(max(1, current - MAX_ROUNDS + 2), current + 1))
    rounds = []
    capability = _capability_time(task)
    root = _public_root(config, task)
    phase = state.get("blocked_from") if state.get("phase") == "BLOCKED" else state.get("phase")
    active = (state.get("task") or {}).get("id") == task["id"]
    operation = state.get("operation") or {}
    started = _iso(operation.get("started_at")) if active and operation.get("task_id") == task["id"] else None
    for index in indices:
        raw, review_sha, review_path = _read(runtime, directory / f"review-{index}.json")
        delivery, delivery_sha, delivery_path = _read(runtime, directory / f"delivery-{index}.md")
        review = json.loads(raw) if raw else {}
        if not isinstance(review, dict):
            raise ValueError("历史审核格式无效")
        if not review and delivery is None and (index != current or not active):
            continue
        head = review.get("head_sha")
        valid_review = bool(HEAD.fullmatch(str(head or ""))) and review.get("decision") in {"accepted", "rework", "blocked"}
        attempts = [a for a in task.get("art_attempts", []) if isinstance(a, dict) and a.get("rework") == index]
        attempt = attempts[0] if len(attempts) == 1 else None
        checks = attempt.get("check_results") if attempt and valid_review else None
        checked = (isinstance(checks, list) and bool(checks) and all(isinstance(c, dict) and c.get("head_sha") == head for c in checks))
        matched = attempt if checked else None
        check_status = "matched_attempt" if matched else "unavailable"
        if (not matched and index == current and valid_review and
                (task.get("art_workspace_head") or (task.get("candidate") or {}).get("head_sha")) == head):
            checks = task.get("check_results")
            checked = isinstance(checks, list) and bool(checks) and all(isinstance(c, dict) and c.get("head_sha") == head for c in checks)
            if checked:
                check_status = "current_task"
        if not checked:
            checks = []
        package = ((matched or {}).get("art_review") or {}).get("review_package_sha256")
        child = type(evidence)(evidence.lane, evidence.node, {**evidence.scope, "history_round": index,
                     "review_sha256": review_sha, "delivery_sha256": delivery_sha, "review_head": head, "history_package": package})
        label = "初次审核（0，非返工）" if index == 0 else f"第 {index} 轮返工"
        feedback = redact(review.get("feedback") or "", MAX_PROSE)
        delivered = redact(delivery or "", MAX_PROSE)
        reviewed_at, time_source = None, None
        if raw:
            reviewed_at = datetime.fromtimestamp(review_path.stat().st_mtime, timezone.utc).isoformat()
            time_source = "审核文件修改时间（非独立调用起止时间）"
        commit = _job_commit(task, head) if valid_review else None
        mode = "claude_blender_job" if commit else "unknown"
        recipe_commit = next((c for c in task.get("art_recipe_commits", []) if isinstance(c, dict)
                              and c.get("head_sha") == head and isinstance(c.get("relative_path"), str)
                              and c["relative_path"] == "art-jobs/" + task["id"] + "/recipe.json"), None)
        if not commit and capability and recipe_commit:
            mode = "recipe_only"
        if not review and index == current and active and capability:
            mode = "claude_blender_job"
        row = {"index": index, "label": label, "decision": review.get("decision") if valid_review else "in_progress" if index == current and active else "unavailable",
               "summary": redact(review.get("summary") or "本轮尚无完成审核回执。", 3000),
               "feedback": feedback, "delivery_text": delivered,
               "review_source": "Codex技术审核", "delivery_source": "Claude交付声明",
               "reviewed_at": reviewed_at, "time_source": time_source,
               "attempt_recorded_at": _iso((matched or {}).get("at")),
               "attempt_time_source": "控制器归档/转阶段时间（可能是人工恢复时间）" if matched else None,
               "started_at": started if index == current else None,
               "checks": [{k: c.get(k) for k in ("id", "passed", "head_sha", "exit_code", "timed_out")} for c in checks],
               "checks_status": check_status, "sections": _sections(feedback), "production_mode": mode,
               "evidence_images": [], "logs": [], "artifacts": [], "evidence_status": "text_only",
               "evidence_note": "仅提供本任务该轮原文；旧完整验收包未归档或无法核实。",
               "feedback_truncated": len(review.get("feedback") or "") > MAX_PROSE,
               "delivery_truncated": len(delivery or "") > MAX_PROSE}
        if raw:
            child.add(runtime, review_path, label + " · Codex 审核原文", mode="json", expected=review_sha)
        if delivery is not None:
            child.add(runtime, delivery_path, label + " · Claude 交付声明", expected=delivery_sha)
        for check in checks:
            try:
                check_files(child, runtime, check, head)
            except (OSError, ValueError, TypeError, KeyError):
                row["evidence_note"] += " 部分检查日志不可核实。"
        if valid_review and root:
            try:
                images, note = _screenshots(child, root, feedback + "\n" + delivered, label)
                row.update(evidence_images=images, evidence_note=note)
                if images:
                    row["evidence_status"] = "historical_capture_receipt"
                    try:
                        audits = _audit_images(child, root, feedback, feedback + "\n" + delivered, label)
                        row["evidence_images"].extend(audits)
                        if audits:
                            row["evidence_note"] += " 审核点名的 Blender 审计图另经同批导入回执与处理清单摘要绑定。"
                    except (OSError, ValueError, TypeError, KeyError) as error:
                        row["evidence_note"] += redact(" 审核点名审计图不可核实：" + str(error), 500)
            except (OSError, ValueError, TypeError, KeyError) as error:
                row.update(evidence_status="unavailable", evidence_note=redact("历史截图不可核实：" + str(error), 500))
        row["logs"] = child.logs
        row["artifacts"] = [a for a in child.artifacts if a["kind"] != "png"]
        evidence.members.update(child.members)
        rounds.append(row)

    user_feedback = []
    queued = next((item for item in _queue(config) if item.get("id") == task["id"]), {})
    decisions = list(queued.get("decision_history") or [])
    if isinstance(queued.get("decision"), dict):
        decisions.append(queued["decision"])
    for decision in decisions[-20:]:
        if not isinstance(decision, dict) or decision.get("decision") not in {"accepted", "rework"}:
            continue
        user_feedback.append({"source": "用户", "decision": decision["decision"],
                              "feedback": redact(decision.get("feedback") or "", MAX_PROSE),
                              "recorded_at": _iso(decision.get("recorded_at")),
                              "review_package_sha256": decision.get("review_package_sha256") if SHA.fullmatch(str(decision.get("review_package_sha256", ""))) else None})
    completed = [row for row in rounds if row["decision"] in {"accepted", "rework", "blocked"}]
    job_rounds = [row for row in completed if row["production_mode"] == "claude_blender_job"]
    old_rounds = [row for row in completed if row["index"] > 0 and row["production_mode"] == "recipe_only"]
    completed_indices = {row["index"] for row in completed if row["index"] > 0}
    expected_indices = set(range(1, current + int(current in completed_indices)))
    counts_known = (bool(capability) and current < MAX_ROUNDS and completed_indices == expected_indices
                    and all(row["production_mode"] != "unknown" for row in completed if row["index"] > 0))
    current_job = len(job_rounds) + 1 if counts_known and active and phase == "CODE" and not any(row["index"] == current for row in completed) else None
    text = f"当前返工计数 {current}；初审 0 不计入返工。"
    if counts_known:
        text += f" Blender job 能力接入前有 {len(old_rounds)} 轮返工记录；接入后已完成 {len(job_rounds)} 轮审核。"
        if current_job is not None:
            text += f" 当前正在进行第 {current_job} 轮 Blender job 制作。"
        text += " 前期权限限制请查看当轮原文；返工总数不能直接当作有效 Blender 修模轮数。"
    else:
        text += " 工具能力轮次无法完整核实，按原始审核时间线展示。"
    return {"rounds": rounds, "user_feedback": user_feedback, "summary": {
        "reworks": current, "current_index": current, "initial_review_count": int(any(row["index"] == 0 for row in completed)),
        "completed_job_rounds": len(job_rounds) if counts_known else None,
        "pre_job_review_rounds": len(old_rounds) if counts_known else None,
        "current_job_round": current_job if current < MAX_ROUNDS else None, "job_capability_at": capability,
        "current_started_at": started, "phase": state.get("phase"), "history_truncated": current >= MAX_ROUNDS,
        "text": text}}
