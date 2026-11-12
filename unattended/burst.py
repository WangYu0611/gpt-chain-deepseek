"""Restartable task-count limits around the serial engine, with no config edits."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager, ExitStack, nullcontext
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from .calendar import BEIJING
from .engine import Engine
from .storage import read_json, write_json


def _schedule_hash(schedule):
    raw = json.dumps(schedule, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _task_ids(state):
    return [task["id"] for task in state["accepted"]]


def _save(path, record, engine):
    record["updated_at"] = engine.now().isoformat()
    write_json(path, record)


def _private_calendar(config, record, engine):
    # Never accept a persisted arbitrary pathname as a write target. The only
    # calendar this module owns is underneath this runtime and its random ID.
    if not re.fullmatch(r"[0-9a-f]{32}", record["id"]):
        raise ValueError("burst 识别码无效，保留记录并核对。")
    runtime = Path(config["runtime_dir"]).resolve()
    parent = runtime / "bursts"
    directory = parent / record["id"]
    junction = lambda path: getattr(path, "is_junction", lambda: False)()
    if (parent.is_symlink() or junction(parent) or parent.resolve().parent != runtime or
            directory.is_symlink() or junction(directory) or directory.resolve().parent != parent.resolve()):
        raise ValueError("burst 私有日历目录不是本运行目录下的受管目录。")
    path = directory / "calendar.json"
    if path.is_symlink():
        raise ValueError("burst 私有日历不能是符号链接。")
    start = engine.now().astimezone(BEIJING).date()
    days = [start + timedelta(days=offset) for offset in range(371)]
    write_json(path, {"years": sorted({day.year for day in days}), "holidays": [], "workdays": [],
                      "overrides": {day.isoformat(): "all_day" for day in days},
                      "notes": "Explicit ignore-window scope for burst " + record["id"] + "; not a public holiday calendar."})
    record["private_calendar"] = str(path)
    config["schedule"] = {"timezone": "Asia/Shanghai", "weekday_start": "18:00", "weekday_end": "08:00",
                          "weekends_all_day": True, "holidays_file": str(path)}


def _progress(record, state):
    ids = _task_ids(state)
    baseline = record["start_accepted_count"]
    if len(ids) < baseline or ids[:baseline] != record["start_task_ids"]:
        raise ValueError("burst 的已验收任务基线发生回退或替换，拒绝继续。")
    target = record["target_accepted_count"]
    record["completed_task_ids"] = ids[baseline:min(len(ids), target)]
    record["completed_tasks"] = len(record["completed_task_ids"])
    record["remaining_tasks"] = max(0, target - len(ids))
    record["current_accepted_count"] = len(ids)
    record["phase"] = state["phase"]
    record["task_id"] = state["task"]["id"] if state.get("task") else None
    record["provider_retry"] = deepcopy(state.get("provider_retry"))
    return len(ids)


def _report(record, path):
    return {"status": record["status"], "burst_id": record["id"],
            "requested_tasks": record["requested_tasks"], "completed_tasks": record.get("completed_tasks", 0),
            "remaining_tasks": record.get("remaining_tasks", record["requested_tasks"]),
            "completed_task_ids": record.get("completed_task_ids", []),
            "start_accepted_count": record["start_accepted_count"],
            "target_accepted_count": record["target_accepted_count"],
            "current_accepted_count": record.get("current_accepted_count", record["start_accepted_count"]),
            "phase": record.get("phase"), "task_id": record.get("task_id"),
            "last_error": record.get("last_error"), "ignore_window": record["ignore_window"],
            "original_schedule_hash": record["original_schedule_hash"],
            "original_close_policy": record["original_close_policy"],
            "retry_count": record.get("retry_count", 0),
            "provider_retry": record.get("provider_retry"),
            "schedule_restored": True, "record_path": str(path)}


@contextmanager
def _exclusive(engine):
    stack = ExitStack()
    try:
        stack.enter_context(engine._locks())
    except (OSError, RuntimeError) as error:
        stack.close()
        raise RuntimeError("无法独占项目及运行目录；可能已有执行器运行或锁不可用：" + str(error)) from error
    with stack:
        yield


def _execute_burst(config: dict, tasks: int, ignore_window: bool = False, providers=None,
                   *, locked=False, once=False, now=None) -> dict:
    """Shared loop; ``locked`` is only used by an Engine already owning both locks."""
    if type(tasks) is not int or tasks <= 0:
        raise ValueError("tasks 必须为正整数，表示新验收通过的任务数量。")
    if type(ignore_window) is not bool:
        raise ValueError("ignore_window 必须显式使用布尔值。")
    private = deepcopy(config)
    original_schedule = deepcopy(private["schedule"])
    original_close_policy = private["close_policy"]
    schedule_hash = _schedule_hash(original_schedule)
    engine = Engine(private, providers=providers, now=now) if now is not None else Engine(private, providers=providers)
    path = Path(private["runtime_dir"]) / "burst.json"
    # The locks cover both the durable target and the complete phase loop. Do
    # not call Engine.step(), which would attempt to acquire these locks again.
    with (nullcontext() if locked else _exclusive(engine)):
        state = engine._load()
        if path.exists():
            record = read_json(path)
            if (record.get("version") != 1 or record.get("binding") != engine._binding() or
                    record.get("project_path") != os.path.normcase(str(Path(private["project_path"]).resolve()))):
                raise ValueError("burst 记录与当前项目或配置绑定不匹配，保留原记录并核对。")
            if record.get("requested_tasks") != tasks:
                raise ValueError("已有 burst 的任务总数不能重置；请用原 --tasks 数量续跑。")
            if record.get("ignore_window") is not ignore_window:
                raise ValueError("续跑须显式保留原 burst 的 --ignore-window 选择，不会隐式忽略工作窗口。")
            if record.get("original_schedule_hash") != schedule_hash or record.get("original_close_policy") != original_close_policy:
                raise ValueError("原工作时间配置在 burst 中途发生变化，保留断点并核对后再继续。")
            if record.get("status") in {"completed", "done"}:
                return _report(record, path)
        else:
            accepted = _task_ids(state)
            record = {"version": 1, "id": uuid.uuid4().hex, "binding": engine._binding(),
                      "project_path": os.path.normcase(str(Path(private["project_path"]).resolve())),
                      "requested_tasks": tasks, "start_accepted_count": len(accepted),
                      "target_accepted_count": len(accepted) + tasks, "start_task_ids": accepted,
                      "ignore_window": ignore_window, "original_schedule": original_schedule,
                      "original_schedule_hash": schedule_hash, "original_close_policy": original_close_policy,
                      "started_at": engine.now().isoformat(), "status": "running", "last_error": None}
        _progress(record, state)
        _save(path, record, engine)  # Save the target before the first agent step.
        try:
            if ignore_window:
                _private_calendar(private, record, engine)
            record["status"], record["last_error"] = "running", None
            _save(path, record, engine)
            while True:
                state = engine._load()
                accepted_count = _progress(record, state)
                target = record["target_accepted_count"]
                if accepted_count >= target:
                    if accepted_count > target:
                        record["status"] = "failed"
                        record["last_error"] = "验收计数超过本 burst 的固定目标，可能有其他执行器推进；本程序未继续派工。"
                    elif state["phase"] != "PLAN" or state.get("task") is not None:
                        record["status"] = "failed"
                        record["last_error"] = "目标任务数已达到，但状态已被推进到下一任务或非 PLAN 阶段；不会继续执行。"
                    else:
                        record["status"] = "completed"
                    break
                if state["phase"] in {"DONE", "BLOCKED"}:
                    record["status"] = "done" if state["phase"] == "DONE" else "blocked"
                    record["last_error"] = state.get("last_error")
                    break
                result = engine._step()
                _progress(record, engine._load())
                status = str(result.get("status", "")).lower()
                if status == "waiting_provider":
                    record["status"], record["last_error"] = status, result.get("last_error")
                    _save(path, record, engine)
                    if once:
                        break
                    time.sleep(engine._wait_seconds(result))
                    continue
                if status in {"done", "blocked", "paused", "waiting_window", "calendar_update_required", "needs_setup"}:
                    record["status"], record["last_error"] = status, result.get("last_error")
                    break
                if result.get("failures", 0):
                    # Only nontransient failures consume the bounded failure
                    # budget. Provider cooldowns use their own durable gate.
                    record["status"], record["last_error"] = "retrying", result.get("last_error")
                    record["retry_count"] = record.get("retry_count", 0) + 1
                    _save(path, record, engine)
                    if once:
                        break
                    time.sleep(max(0, min(float(private.get("poll_seconds", 30)), 60)))
                    continue
                record["status"], record["last_error"] = "running", None
                _save(path, record, engine)
                if once:
                    # Re-enter the counter gate to finish the final receipt,
                    # but never run a second phase or an N+1 planning step.
                    if record["current_accepted_count"] >= record["target_accepted_count"]:
                        continue
                    break
        except KeyboardInterrupt:
            _progress(record, engine._load())
            record["status"], record["last_error"] = "interrupted", "用户中断；原任务、阶段和 burst 目标均保留。"
        except Exception as error:
            record["status"], record["last_error"] = "failed", f"{type(error).__name__}: {error}"
        finally:
            # Only the private in-memory config was changed. The original file,
            # user calendar and caller's config dict were never written.
            private["schedule"], private["close_policy"] = original_schedule, original_close_policy
            _save(path, record, engine)
        return _report(record, path)


def run_burst(config: dict, tasks: int, ignore_window: bool = False, providers=None) -> dict:
    """Accept exactly N new tasks, retaining the same target across restarts.

    A finished record is idempotent: another invocation returns its receipt.
    To explicitly begin another batch, archive the completed burst.json while
    all runners are stopped. Engine state and task history must be preserved.
    The caller manages any background runner; this function only uses the same
    project/runtime OS locks and never stops or starts Windows tasks.
    """
    return _execute_burst(config, tasks, ignore_window, providers)


def resume_pending_burst(engine, *, once=False):
    """Resume an unfinished receipt while the caller holds project/runtime locks.

    The recorded ignore-window choice is the original batch authorization;
    neither a login nor a normal run creates a new target or a new override.
    Completed historical receipts do not constrain later regular scheduling.
    """
    path = Path(engine.config["runtime_dir"]) / "burst.json"
    if not path.exists():
        return None
    record = read_json(path)
    if record.get("status") in {"completed", "done"}:
        return None
    # Active records are fully revalidated by the same path as explicit burst
    # execution; malformed/mismatched records fail closed, never fall through.
    return _execute_burst(engine.config, record.get("requested_tasks"), record.get("ignore_window"),
                          providers=engine.providers, locked=True, once=once, now=engine.now)
