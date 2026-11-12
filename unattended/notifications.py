"""Explicit, best-effort Feishu cards; reading state never sends a message.

Call tick from the dashboard's dedicated notification thread, never the engine.
Only this module's private journal/lock beside settings_path are written. Feishu
does not offer an idempotency key: a crash during a request leaves delivery
uncertain; on restart that attempt is NOT repeated to avoid duplicate cards.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from urllib import error, request
from urllib.parse import urlsplit
import uuid

from .secrets import read_secret
from .storage import lock, write_json

ART_EVENTS = ("art_review_ready", "art_accepted", "art_rework", "art_blocked")
FULL_FLOW_EVENTS = ("full_flow_started", "full_flow_finished")
EVENTS = ("window_start", "accepted", "blocked", "worker_exit", "window_end", *ART_EVENTS, *FULL_FLOW_EVENTS)
TITLES = {"test": "通知连接测试", "window_start": "工作窗口开始", "accepted": "任务验收通过",
          "blocked": "任务需要处理", "worker_exit": "后台意外退出", "window_end": "工作窗口结束",
          "art_review_ready": "等待视觉验收", "art_accepted": "用户通过已提交",
          "art_rework": "返工意见已提交", "art_blocked": "制作异常待处理",
          "full_flow_started": "Opus全流程测试开始", "full_flow_finished": "Opus全流程测试结束"}
PHASES = {"PLAN": "规划任务", "CODE": "编写代码", "CHECK": "独立检查", "REVIEW": "验收审阅",
          "PUBLISH": "本地交付", "MILESTONE_REVIEW": "里程碑验收", "MILESTONE_PUBLISH": "发布里程碑",
          "BLOCKED": "等待处理", "DONE": "目标已完成"}
MAX_BYTES = 4 * 1024 * 1024
MAX_ATTEMPTS = 3
BACKOFF = (60, 300, 1800)


def _now():
    return datetime.now(timezone.utc)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _text(value, limit=180):
    if not isinstance(value, str):
        return ""
    value = re.sub(r"(?is)<(?:think|thinking|analysis)\b[^>]*>.*?(?:</(?:think|thinking|analysis)>|$)", "", value)
    value = re.sub(r"(?s)```.*?(?:```|$)", "[代码省略]", value)
    value = re.sub(r"(?i)https?://[^\s<>\"']+", "[链接省略]", value)
    value = re.sub(r"(?i)\b(?:sk-[\w-]{8,}|gh[pousr]_[\w-]{8,}|github_pat_[\w]+)", "[凭据已隐藏]", value)
    value = re.sub(r"(?i)(?:\b(?:[\w]*_)?(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|token)\b[\"']?\s*[:=]\s*)(?:Bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", "[凭据已隐藏]", value)
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "[凭据已隐藏]", value)
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()
    return value[:limit] + ("…" if len(value) > limit else "")


def validate_webhook(value):
    """Validate without ever including the private URL in an exception."""
    valid = False
    if isinstance(value, str) and not re.search(r"\s", value):
        try:
            parts = urlsplit(value)
            valid = (parts.scheme == "https" and parts.netloc == "open.feishu.cn"
                     and not parts.query and not parts.fragment
                     and re.fullmatch(r"/open-apis/bot/v2/hook/[A-Za-z0-9_-]{16,128}", parts.path))
        except ValueError:
            pass
    if not valid:
        raise ValueError("通知地址无效：仅允许飞书官方 HTTPS 自定义机器人地址")
    return value


def _safe(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
            raise ValueError("通知文件不能经过符号链接或 junction")
    return path


def _read(path, maximum=MAX_BYTES):
    with _safe(path).open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("通知文件超出大小限制")
    result = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(result, dict):
        raise ValueError("通知文件必须是 JSON 对象")
    return result


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post(webhook, payload):
    """Return only fixed diagnostic codes; never server text or exceptions."""
    try:
        validate_webhook(webhook)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > 18000:
            return False, "card_too_large"
        req = request.Request(webhook, data=body, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
        with request.build_opener(_NoRedirect()).open(req, timeout=8) as response:
            if response.status != 200:
                return False, "http_error"
            raw = response.read(65537)
        if len(raw) > 65536:
            return False, "invalid_response"
        result = json.loads(raw)
        if not isinstance(result, dict):
            return False, "invalid_response"
        code = result.get("code", result.get("StatusCode"))
        return (True, None) if type(code) is int and code == 0 else (False, "bot_rejected")
    except error.HTTPError as exc:
        return False, "redirect_rejected" if 300 <= exc.code < 400 else "http_error"
    except Exception:
        return False, "delivery_failed"


def _plain(content):
    return {"tag": "plain_text", "content": content}


def _art_card(snapshot, event, item):
    art = snapshot.get("art") or {}
    item = item or {}
    decision = item.get("decision") if isinstance(item.get("decision"), dict) else {}
    review = item.get("technical_review") if isinstance(item.get("technical_review"), dict) else {}
    version = item.get("review_package_sha256")
    version = version[:12] if isinstance(version, str) and re.fullmatch(r"[a-f0-9]{64}", version) else "尚无验收版本"
    summary = {"art_review_ready": "固定版本已提交，请在本机面板查看模型展示并进行视觉验收。",
               "art_accepted": "用户已提交通过意见，系统将按现有流程处理入库。",
               "art_rework": "返工意见已提交，系统将按现有流程继续处理。",
               "art_blocked": "模型制作流程已阻塞，需要查看本机面板中的处理原因。"}[event]
    detail = (_text(review.get("summary"), 280) if event == "art_review_ready" else
              _text(art.get("blocking_summary") or art.get("last_error"), 280) if event == "art_blocked" else _text(decision.get("feedback"), 280))
    label = {"art_review_ready": "待验收 · 请查看固定版本", "art_accepted": "通过 · 用户意见已提交",
             "art_rework": "返工 · 用户意见已提交", "art_blocked": "异常 · 需要处理"}[event]
    levels = item.get("approved_levels")
    levels = " / ".join(_text(level, 20) for level in levels[:16]) if isinstance(levels, list) else ""
    budget = item.get("budget")
    cost = "待核实"
    if isinstance(budget, dict):
        try:
            if all(type(budget.get(key)) in (int, float) and math.isfinite(budget[key]) and budget[key] >= 0 for key in ("actual", "limit")):
                cost = f"{budget['actual']:g} / {budget['limit']:g} 积分"
        except OverflowError:
            pass
    fields = [f"状态\n{label}", f"验收版本\n{version}", f"模型等级\n{levels or '以任务书为准'}", f"费用快照\n{cost}"]
    elements = [{"tag": "div", "text": _plain(summary)},
                {"tag": "div", "text": _plain("任务：" + (_text(item.get("id") or art.get("task_id"), 70) + " " + _text(item.get("title"), 160)).strip())},
                {"tag": "div", "fields": [{"is_short": True, "text": _plain(value)} for value in fields]}]
    if detail:
        elements.append({"tag": "div", "text": _plain(detail)})
    if event == "art_review_ready":
        note = _text(item.get("accept_block_reason"), 180) or (
            "技术检查通过；等待用户对本版本作出视觉验收决定。" if review.get("decision") == "accepted" else
            "技术检查摘要未提供，请核对本机证据后进行视觉验收。")
        elements.append({"tag": "note", "elements": [_plain(note)]})
    return {"msg_type": "interactive", "card": {"config": {"wide_screen_mode": True},
        "header": {"template": "red", "title": _plain("模型验收 · " + TITLES[event])}, "elements": elements}}


def _full_flow_card(snapshot, event):
    job = snapshot.get("full_flow_test") or {}
    proof = job.get("notification_proof") or {}
    if event == "full_flow_started":
        label, summary = "实际测试已启动", "已确认本次测试执行阶段的 Opus 原生启动记录。规划与准备环境不计作测试启动。"
    else:
        label = {"checks_passed": "检查回执均标记通过", "checks_failed_or_not_run": "已结束 · 首轮回执含 FAIL 或未运行记录",
                 "execution_failed": "已结束 · 执行失败或中断"}.get(proof.get("outcome"), "已结束 · 结果待核实")
        summary = {"checks_passed": "五个首轮阶段的终态回执均标记通过；具体结果见本次报告。",
                   "checks_failed_or_not_run": "本次测试已有终态回执与报告；首轮原记录保留，补测与校核结论单独列示，不能将结束视为全通过。",
                   "execution_failed": "本次测试执行失败或中断。" + (_text(job.get("summary"), 200) or "请查看本机终态回执。")}.get(proof.get("outcome"), label)
    head = _text(job.get("head_sha"), 12)
    fields = ["状态\n" + label, "执行者\nClaude Opus 5.5 / high", "固定版本\n" + head,
              "阶段\n" + (_text(job.get("stage_label"), 60) or "以本次测试记录为准")]
    elements = [{"tag": "div", "text": _plain(summary)},
                {"tag": "div", "text": _plain("任务：" + _text(job.get("task_id"), 70) + " · 测试 " + _text(job.get("id"), 12))},
                {"tag": "div", "fields": [{"is_short": True, "text": _plain(value)} for value in fields]}]
    failed = proof.get("failed_stages")
    if event == "full_flow_finished" and isinstance(failed, list) and failed:
        elements.append({"tag": "div", "text": _plain("失败或未完成阶段：" + "、".join(_text(value, 40) for value in failed[:8]))})
    if event == "full_flow_finished" and isinstance(job.get("retest"), dict):
        elements.append({"tag": "div", "text": _plain(_text(job["retest"].get("summary"), 220))})
        if job["retest"].get("reconciliation_summary"):
            elements.append({"tag": "div", "text": _plain(_text(job["retest"]["reconciliation_summary"], 220))})
    if event == "full_flow_finished" and isinstance(job.get("package_short"), dict):
        elements.append({"tag": "div", "text": _plain(_text(job["package_short"].get("summary"), 220))})
    elements.append({"tag": "note", "elements": [_plain("独立全流程测试；结束不等于全通过，也不代表主任务或资产已验收。详细回执与报告见本机面板。") ]})
    return {"msg_type": "interactive", "card": {"config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": _plain("功能开发 · " + TITLES[event])}, "elements": elements}}


def build_card(snapshot, event, *, accepted=None, window_count=0, dashboard_url=None, art_request=None):
    """Pure card builder. Never include prompts, source, logs or webhook settings."""
    if event in ART_EVENTS:
        return _art_card(snapshot, event, art_request)
    if event in FULL_FLOW_EVENTS:
        return _full_flow_card(snapshot, event)
    status = snapshot.get("status") or {}
    task = accepted or snapshot.get("task") or {}
    publication = snapshot.get("publication") or {}
    records = snapshot.get("accepted") or []
    sha = task.get("sha") or task.get("head_sha") or (records[-1].get("sha") if records else None)
    sha = sha[:12] if isinstance(sha, str) and re.fullmatch(r"[a-fA-F0-9]{7,64}", sha) else "尚无提交"
    label = _text(status.get("label"), 60) or PHASES.get(status.get("phase"), "等待运行")
    label = {"accepted": "通过 · 独立任务验收", "blocked": "异常 · 需要处理",
             "worker_exit": "异常 · 后台已停止", "test": "连接测试"}.get(event, label)
    summary = _text(accepted.get("review_summary")) if accepted else _text(status.get("summary"))
    if event == "window_end":
        summary = f"本窗口自监测起新增验收 {window_count} 项；断点已保留。"
    elif event == "worker_exit":
        summary = "检测到后台进程从运行变为停止；任务断点保留，请检查后台状态。"
    elif event == "test":
        summary = "飞书卡片通知已连接。后续仅推送工作窗口、验收结果和需处理事项。"
    elif event == "accepted":
        summary = summary or "独立检查与验收通过，已形成任务提交。"
    milestone = _text(publication.get("milestone_id") or snapshot.get("project", {}).get("milestone"), 60)
    pending = publication.get("pending_tasks", 0)
    pending = pending if type(pending) is int and pending >= 0 else 0
    publish = (_text(publication.get("summary"), 140) or "仅本地提交")
    if publication.get("mode") == "milestone":
        publish = f"{milestone or '当前里程碑'}：{pending} 项本地验收任务待整体发布。" + publish
    fields = [f"状态\n{label}", f"阶段\n{PHASES.get(status.get('phase'), '等待运行')}",
              f"验收记录\n{len(records)} 项", f"本地提交\n{sha}"]
    elements = [{"tag": "div", "text": _plain(summary or TITLES[event])},
                {"tag": "div", "text": _plain("任务：" + (_text(task.get("id"), 70) + " " + _text(task.get("title"), 160)).strip() if task else "任务：等待下一项任务")},
                {"tag": "div", "fields": [{"is_short": True, "text": _plain(value)} for value in fields]},
                {"tag": "note", "elements": [_plain(publish)]}]
    if event == "test" and isinstance(dashboard_url, str):
        # A loopback address is informative only on this PC, never a phone button.
        parsed = urlsplit(dashboard_url)
        if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"} and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment:
            elements.append({"tag": "note", "elements": [_plain("本机面板（仅此电脑可访问）：" + dashboard_url[:150])]})
    return {"msg_type": "interactive", "card": {"config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": _plain("功能开发 · " + TITLES[event])}, "elements": elements}}


class NotificationMonitor:
    """One bounded send per tick, persistent dedup, three attempts with backoff.

    settings: enabled, webhook_url, optional dashboard_url and events (EVENTS).
    tick/status return a fixed, public status shape; never return settings.
    """
    def __init__(self, config_path: Path, settings_path: Path):
        self.config_path = Path(os.path.abspath(config_path))
        self.settings_path = Path(os.path.abspath(settings_path))
        self.journal_path = self.settings_path.parent / ("notifications-" + _hash(os.path.normcase(str(self.config_path)))[:16] + ".json")
        self.lock_path = self.journal_path.with_suffix(".lock")

    def _settings(self):
        try:
            settings = _read(self.settings_path, 65536)
        except FileNotFoundError:
            return {"enabled": False}
        if type(settings.get("enabled", False)) is not bool:
            raise ValueError("通知 enabled 必须为布尔值")
        if settings.get("enabled"):
            webhook = settings.get("webhook_url")
            if webhook is None or webhook == "":
                webhook = read_secret("FEISHU_WEBHOOK_URL", config_path=self.config_path)
            settings["webhook_url"] = validate_webhook(webhook)
        events = settings.get("events", list(EVENTS))
        if not isinstance(events, list) or any(not isinstance(x, str) or x not in EVENTS for x in events):
            raise ValueError("通知事件配置无效")
        settings["events"] = events
        return settings

    def _load(self):
        try:
            journal = _read(self.journal_path)
        except FileNotFoundError:
            return {"version": 1, "initialized": False, "seen": [], "events": {}, "delivery": {}}
        if journal.get("version") != 1 or not isinstance(journal.get("seen"), list) or not isinstance(journal.get("events"), dict):
            raise ValueError("通知 journal 无效")
        return journal

    def _save(self, journal):
        if len(json.dumps(journal, ensure_ascii=False).encode()) > MAX_BYTES - 65536:
            raise ValueError("通知 journal 已达到容量上限")
        write_json(_safe(self.journal_path), journal)

    @staticmethod
    def _public(journal=None, enabled=False, failure=None):
        journal = journal or {}
        delivery = journal.get("delivery", {})
        result = {"enabled": bool(enabled), "state": "ready" if enabled else "disabled", "pending": 0,
                  "last_event": None, "last_attempt_at": None, "last_success_at": None, "last_error": failure}
        for key in ("last_event", "last_attempt_at", "last_success_at", "last_error"):
            value = delivery.get(key)
            if key == "last_event":
                result[key] = value if value in (*EVENTS, "test") else None
            elif key.endswith("_at"):
                result[key] = value if isinstance(value, str) and re.fullmatch(r"[0-9T:.+Z-]{10,40}", value) else None
            elif value in {"card_too_large", "http_error", "invalid_response", "bot_rejected", "redirect_rejected", "delivery_failed", "delivery_uncertain"}:
                result[key] = value
        result["pending"] = sum(row.get("state") == "pending" for row in journal.get("events", {}).values())
        if failure:
            result.update(state="error", last_error=failure)
        elif result["last_error"]:
            result["state"] = "delivery_error" if enabled else "disabled"
        return result

    def status(self):
        """Read-only, safe to expose in a local dashboard API."""
        try:
            settings = self._settings()
            return self._public(self._load(), settings.get("enabled", False))
        except Exception:
            return self._public(failure="notification_state_unavailable")

    def _queue(self, journal, settings, snapshot, event, key, **kwargs):
        identity = _hash([event, key])
        if event in settings["events"] and identity not in journal["events"]:
            journal["events"][identity] = {"event": event, "state": "pending", "attempts": 0, "next_at": 0,
                                         "card": build_card(snapshot, event, **kwargs)}

    def _observe(self, journal, settings, snapshot):
        records = snapshot.get("accepted") or []
        seen = set(journal["seen"])
        status = snapshot.get("status") or {}
        schedule = snapshot.get("schedule") or {}
        allowed = schedule.get("allowed") is True
        closing = schedule.get("closes_at")
        alive = snapshot.get("worker", {}).get("alive") is True
        blocked = status.get("phase") == "BLOCKED" or status.get("code") == "blocked"
        if not journal.get("initialized"):
            journal.update(initialized=True, seen=[_hash([r.get("id"), r.get("sha")]) for r in records],
                           allowed=allowed, closing=closing, window_count=0, alive=alive, blocked=blocked,
                           sequence=0, window_key=closing or uuid.uuid4().hex)
            return
        rolled = journal.get("allowed") and allowed and journal.get("closing") and closing != journal.get("closing")
        opening = allowed and (not journal.get("allowed") or rolled)
        if opening and not rolled:
            journal.update(window_count=0, window_key=closing or uuid.uuid4().hex)
            self._queue(journal, settings, snapshot, "window_start", journal["window_key"])
        next_window_count = 0
        # Accepted tasks observed at the closing edge belong to the prior window.
        for record in records:
            key = _hash([record.get("id"), record.get("sha")])
            if key not in seen:
                seen.add(key)
                in_next_window = False
                if rolled and isinstance(record.get("accepted_at"), str):
                    try:
                        accepted_at = datetime.fromisoformat(record["accepted_at"].replace("Z", "+00:00"))
                        previous_close = datetime.fromisoformat(journal["closing"].replace("Z", "+00:00"))
                        in_next_window = accepted_at >= previous_close
                    except (ValueError, TypeError):
                        pass
                if in_next_window:
                    next_window_count += 1
                else:
                    journal["window_count"] += 1
                self._queue(journal, settings, snapshot, "accepted", key, accepted=record)
        journal["seen"] = sorted(seen)
        if journal.get("allowed") and (not allowed or rolled):
            self._queue(journal, settings, snapshot, "window_end", journal["window_key"], window_count=journal["window_count"])
        if rolled:
            journal.update(window_count=next_window_count, window_key=closing or uuid.uuid4().hex)
            self._queue(journal, settings, snapshot, "window_start", journal["window_key"])
        if blocked and not journal.get("blocked"):
            journal["sequence"] += 1
            self._queue(journal, settings, snapshot, "blocked", journal["sequence"])
        if journal.get("alive") and not alive and not status.get("paused") and status.get("phase") not in {"DONE", "BLOCKED"}:
            journal["sequence"] += 1
            self._queue(journal, settings, snapshot, "worker_exit", journal["sequence"])
        journal.update(allowed=allowed, closing=closing, alive=alive, blocked=blocked)

    def _observe_art(self, journal, settings, snapshot):
        """Observe the existing public art projection; never open a worker or ledger."""
        art = snapshot.get("art")
        if (not isinstance(art, dict) or art.get("configured") is not True or art.get("error") or
                art.get("status") not in {"idle", "queued", "code", "art_check", "art_review", "awaiting_approval", "blocked", "paused", "waiting_provider"}):
            return False
        records = art.get("requests", [])
        if not isinstance(records, list) or len(records) > 200 or any(not isinstance(row, dict) for row in records):
            return False
        first = "art" not in journal
        observed = journal.setdefault("art", {"seen": [], "blocked": None, "sequence": 0})
        if (not isinstance(observed, dict) or not isinstance(observed.get("seen"), list) or
                any(not isinstance(key, str) for key in observed["seen"]) or type(observed.get("sequence")) is not int):
            raise ValueError("美术通知记录无效")
        seen = set(observed["seen"])
        current_ready = set()
        for item in records:
            identity, version = item.get("id"), item.get("review_package_sha256")
            if (not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", identity) or
                    not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version) or
                    item.get("preview_error") or not isinstance(item.get("previews"), list) or not item["previews"]):
                continue
            decision = item.get("decision")
            decision = decision.get("decision") if isinstance(decision, dict) else None
            event = ("art_accepted" if decision == "accepted" or item.get("status") == "accepted" else
                     "art_rework" if decision == "rework" else
                     "art_review_ready" if item.get("status") == "awaiting_approval" and item.get("can_resolve") is True else None)
            if event is None:
                continue
            key = _hash([identity, version])
            event_key = _hash([event, key])
            if event == "art_review_ready":
                current_ready.add(event_key)
            if event_key not in seen:
                # A legacy explicit allowlist can be migrated after deployment.
                # Keep its current actionable review eligible for the first alert.
                if event != "art_review_ready" or event in settings["events"]:
                    seen.add(event_key)
                # Initial deployment alerts current actionable reviews, not old decisions.
                if not first or event == "art_review_ready":
                    self._queue(journal, settings, snapshot, event, key, art_request=item)
        observed["seen"] = sorted(seen)
        blocked = _hash([art.get("task_id"), art.get("phase")]) if art.get("status") == "blocked" else None
        if blocked and blocked != observed.get("blocked"):
            observed["sequence"] += 1
            self._queue(journal, settings, snapshot, "art_blocked", [blocked, observed["sequence"]])
        observed["blocked"] = blocked
        for identity, row in journal["events"].items():
            if (row.get("state") == "pending" and ((row.get("event") == "art_review_ready" and identity not in current_ready) or
                                                    (row.get("event") == "art_blocked" and not blocked))):
                row.update(state="superseded")
                row.pop("card", None)
        return True

    def _observe_full_flow(self, journal, settings, snapshot):
        """Only the request-bound public native/test proof can announce a job."""
        job = snapshot.get("full_flow_test")
        if not isinstance(job, dict) or job.get("status") not in {"testing", "analyzing", "completed", "failed"} or job.get("stage") in {"plan", "prepare"}:
            return {}
        proof = job.get("notification_proof")
        if (not isinstance(proof, dict) or proof.get("test_native_started") is not True or
                not re.fullmatch(r"[a-f0-9]{64}", str(proof.get("request_sha256") or "")) or
                not re.fullmatch(r"[a-f0-9]{32}", str(job.get("id") or "")) or
                not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", str(job.get("head_sha") or "")) or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", str(job.get("task_id") or "")) or
                job.get("model") != "claude-opus-5-5" or job.get("effort") != "high"):
            return {}
        key = [job["id"], job["head_sha"]]
        self._queue(journal, settings, snapshot, "full_flow_started", key)
        finished = (job["status"] in {"completed", "failed"} and proof.get("terminal_evidence") is True and
                    proof.get("outcome") in {"checks_passed", "checks_failed_or_not_run", "execution_failed"})
        if finished:
            self._queue(journal, settings, snapshot, "full_flow_finished", key)
        return {"full_flow_started": True, "full_flow_finished": finished}

    def _deliver(self, journal, settings, row):
        now = _now()
        # Normalize cards queued by the old version before sending a retry.
        header = row["card"]["card"]["header"]
        art = row["event"] in ART_EVENTS
        header["template"] = "red" if art else "blue"
        prefix = "模型验收 · " if art else "功能开发 · "
        title = header.get("title")
        if isinstance(title, dict) and isinstance(title.get("content"), str):
            title["content"] = prefix + re.sub(r"^(?:无人值守开发|功能开发|模型验收)\s*[·:：]\s*", "", title["content"])
        else:
            header["title"] = _plain(prefix + TITLES[row["event"]])
        row.update(state="sending", attempts=row["attempts"] + 1)
        journal["delivery"].update(last_event=row["event"], last_attempt_at=now.isoformat())
        self._save(journal)  # Reserve before HTTP; crashed attempts are not replayed.
        ok, failure = _post(settings["webhook_url"], row["card"])
        row["state"] = "sent" if ok else "failed" if row["attempts"] >= MAX_ATTEMPTS else "pending"
        row["next_at"] = now.timestamp() + BACKOFF[min(row["attempts"] - 1, len(BACKOFF) - 1)]
        journal["delivery"]["last_error"] = failure
        if ok:
            journal["delivery"]["last_success_at"] = now.isoformat()
        else:
            journal["retry_after"] = row["next_at"]
        if row["state"] in {"sent", "failed"}:
            row.pop("card", None)
        self._save(journal)
        return ok

    def tick(self, snapshot):
        """Explicitly detect events and send at most one card; never raise."""
        try:
            settings = self._settings()
            if not settings.get("enabled"):
                return self._public(enabled=False)
            with lock(_safe(self.lock_path)):
                journal = self._load()
                main_valid = not snapshot.get("error") and snapshot.get("status", {}).get("code") not in {"error", "not_initialized"}
                for row in journal["events"].values():
                    if row["state"] == "sending":
                        row.update(state="uncertain")
                        row.pop("card", None)
                        journal["delivery"]["last_error"] = "delivery_uncertain"
                if main_valid:
                    self._observe(journal, settings, snapshot)
                art_valid = self._observe_art(journal, settings, snapshot)
                full_flow_valid = self._observe_full_flow(journal, settings, snapshot)
                if not main_valid and not art_valid and not full_flow_valid:
                    return self._public(journal, True)
                self._save(journal)
                now = _now().timestamp()
                if now >= journal.get("retry_after", 0):
                    for row in journal["events"].values():
                        if (row["state"] == "pending" and row["next_at"] <= now and row["event"] in settings["events"] and
                                (full_flow_valid.get(row["event"], False) if row["event"] in FULL_FLOW_EVENTS else
                                 art_valid if row["event"] in ART_EVENTS else main_valid)):
                            self._deliver(journal, settings, row)
                            break
                return self._public(journal, True)
        except Exception:
            return self._public(failure="notification_state_unavailable")

    def send_test(self, snapshot):
        """Send one explicitly requested test card, without changing event baseline."""
        try:
            settings = self._settings()
            if not settings.get("enabled"):
                return {**self._public(enabled=False), "ok": False}
            with lock(_safe(self.lock_path)):
                journal = self._load()
                row = {"event": "test", "state": "pending", "attempts": 0,
                       "card": build_card(snapshot, "test", dashboard_url=settings.get("dashboard_url"))}
                # Test is manual and is never automatically retried.
                journal["events"]["test-" + uuid.uuid4().hex] = row
                ok = self._deliver(journal, settings, row)
                if not ok:
                    row.update(state="failed")
                    row.pop("card", None)
                    self._save(journal)
                return {**self._public(journal, True), "ok": ok}
        except Exception:
            return {**self._public(failure="notification_state_unavailable"), "ok": False}
