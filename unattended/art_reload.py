"""Deferred, identity-bound reload of the independent art host, never its model.

The old host understands PAUSE already. Its main-loop heartbeat (not the
supervisor pulse) proves that its foreground step has naturally returned. All
launches remain paused until a different, healthy host identity is observed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from .background import ROOT
from .storage import lock, read_json, write_json

_TERMINAL = {"completed", "cancelled", "timed_out", "blocked", "superseded"}
_IDENTITY_KEYS = ("pid", "process_created", "image")
_CODE_FILES = ("art_reload.py", "art_worker.py", "engine.py", "providers.py", "config.py", "art_autonomy.py")
_DEVELOPMENT_CODE_FILES = ("art_reload.py", "background_reload.py", "background.py", "cli.py", "__main__.py",
                           "engine.py", "task_replanning.py", "providers.py", "config.py", "storage.py",
                           "gitops.py", "calendar.py", "burst.py", "ue_checks.py", "art_autonomy.py")


def _lane(manager):
    return getattr(manager, "_reload_lane", "art")


def _prefix(manager):
    return "background" if _lane(manager) == "development" else "art"


def _control_file(manager, suffix):
    return manager.runtime / (_prefix(manager) + "-reload-" + suffix)


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _fingerprint(path):
    fact = path.lstat()
    if path.is_symlink() or getattr(fact, "st_file_attributes", 0) & 0x400 or fact.st_size > 4096:
        raise RuntimeError("美术暂停标记不是预期的普通小文件。")
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mtime_ns": fact.st_mtime_ns,
            "size": fact.st_size}


def _identity(status):
    health = status.get("health") or {}
    if not status.get("process_alive") or any(health.get(key) is None for key in _IDENTITY_KEYS):
        return None
    return {key: health[key] for key in _IDENTITY_KEYS}


def _code_fingerprint(manager=None):
    if manager is not None and _lane(manager) == "development":
        # Missing target source is an invalid deployment, not an optional file.
        files = {name: ROOT / "unattended" / name for name in _DEVELOPMENT_CODE_FILES}
        files["github_harness_lab/lab_harness/process.py"] = ROOT / "github_harness_lab/lab_harness/process.py"
        return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}
    return {name: hashlib.sha256((ROOT / "unattended" / name).read_bytes()).hexdigest()
            for name in _CODE_FILES if (ROOT / "unattended" / name).is_file()}


def _manager(config_path, manager=None):
    if manager is not None and _lane(manager) == "development":
        from .engine import Engine
        if manager.config_path != Path(config_path).resolve() or manager.config.get("art_pipeline"):
            raise RuntimeError("功能后台重载只能绑定主开发配置，不能接管美术后台。")
        state = Engine(manager.config)._load()  # Read-only binding check, never a controller step.
        if (state.get("task") or {}).get("kind") == "art":
            raise RuntimeError("功能后台重载不能接管活动美术任务。")
        if manager._scheduler()._owned(manager.name, manager.description) is None:
            raise RuntimeError("功能后台计划任务尚未注册，拒绝重载。")
        return manager
    from .art_worker import ArtWorker, ArtWorkerManager
    manager = manager or ArtWorkerManager(config_path)
    if manager.config_path != Path(config_path).resolve():
        raise RuntimeError("美术重载配置不匹配。")
    ArtWorker(manager.config).status()  # Reject the main lane and foreign state before touching any marker.
    manager._scheduler()._owned(manager.name, manager.description)
    return manager


def _request_path(runtime, request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise ValueError("无效的美术重载请求编号。")
    return runtime / "reload-history" / (request_id + ".json")


def _read_request(manager, request_id):
    path = _request_path(manager.runtime, request_id)
    value = read_json(path)
    if (value.get("version") != 1 or value.get("id") != request_id or
            value.get("config_file") != str(manager.config_path) or value.get("runtime_dir") != str(manager.runtime)
            or value.get("lane", "art") != _lane(manager)):
        raise RuntimeError("美术重载请求绑定不匹配。")
    return value


def _save(manager, request, event=None, *, now=time.time):
    request["updated_at"] = _iso(now())
    if event:
        request.setdefault("events", []).append({"at": request["updated_at"], "event": event})
    write_json(_request_path(manager.runtime, request["id"]), request)


def reload_status(config_path, *, manager=None):
    manager = _manager(config_path, manager)
    pointer = _control_file(manager, "request.json")
    if not pointer.is_file():
        return {"status": "none"}
    return _read_request(manager, read_json(pointer)["id"])


def _launch_helper(manager, request_id):
    executable = Path(sys.executable).with_name("pythonw.exe")
    if not executable.is_file():
        executable = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if os.name != "nt" or not executable.is_file():
        raise RuntimeError("延后美术重载需要 Windows pythonw.exe。")
    name = manager.name + "-Reload"
    description = json.dumps({"owner": "gpt_chain_deepseek." + _prefix(manager) + "-reload/v1",
                              "worker_description": manager.description}, sort_keys=True)
    scheduler = manager._scheduler()
    existing = scheduler._owned(name, description)
    receipt = _control_file(manager, "helper-registration.json")
    if existing and existing.get("logon_trigger"):
        raise RuntimeError("重载 helper 不能包含登录触发器。")
    if existing and existing["state"] in (2, 4):
        if receipt.is_file() and read_json(receipt).get("request_id") == request_id:
            return {"task_name": name, "status": "already_running"}
        raise RuntimeError("上一个重载 helper 尚未退出，当前持久请求可稍后重入。")
    arguments = subprocess.list2cmdline(
        ["-m", "unattended", "background", "reload-worker", "--config", str(manager.config_path),
         "--request-id", request_id] if _lane(manager) == "development" else
        ["-m", "unattended.art_worker", "--config", str(manager.config_path),
         "reload-worker", "--request-id", request_id])
    scheduler.register(name, description, executable, arguments, logon=False)
    write_json(receipt, {"request_id": request_id, "task_name": name})
    scheduler.run(name)
    return {"task_name": name, "status": "submitted"}


def request_reload(config_path, *, timeout_seconds=7200, manager=None, launch=True, now=time.time):
    if type(timeout_seconds) is not int or not 30 <= timeout_seconds <= 86400:
        raise ValueError("美术重载等待时间必须为 30..86400 秒。")
    manager = _manager(config_path, manager)
    with lock(_control_file(manager, "request.lock")):
        current = reload_status(config_path, manager=manager)
        if current["status"] not in _TERMINAL | {"none"}:
            request = current  # A repeated request resumes the same intent, never creates another restart.
        else:
            status = manager.status()
            identity = _identity(status)
            if identity is None:
                raise RuntimeError("美术后台尚未运行；重载不会启动一个原本停止的工作进程。")
            stamp = now()
            request = {"version": 1, "id": uuid.uuid4().hex, "status": "pending", "lane": _lane(manager),
                       "config_file": str(manager.config_path), "runtime_dir": str(manager.runtime),
                       "config_sha256": hashlib.sha256(manager.config_path.read_bytes()).hexdigest(),
                       "code_sha256": _code_fingerprint(manager), "source_identity": identity,
                       "requested_at": _iso(stamp), "deadline_epoch": stamp + timeout_seconds,
                       "pause": None, "events": []}
            _save(manager, request, "requested", now=now)
            write_json(_control_file(manager, "request.json"), {"version": 1, "id": request["id"]})
        if launch:
            # Multiple helpers are harmless: the OS-owned helper lock permits
            # only one coordinator, including after a helper process crashes.
            # Do not rewrite a previously read request here: an existing helper
            # may already have advanced it while this caller checked Scheduler.
            launch_receipt = _launch_helper(manager, request["id"])
            write_json(manager.runtime / "reload-history" / (request["id"] + ".helper.json"),
                       {**launch_receipt, "requested_at": _iso(now())})
        return _read_request(manager, request["id"])


def _claim_pause(manager, request, *, now=time.time):
    path = manager.runtime / "PAUSE"
    token = _prefix(manager) + "-reload:" + request["id"] + "\n"
    # Save ownership intent before creation so a crash cannot strand an
    # unrecognizable marker. Exclusive creation never overwrites a user pause.
    request["pause"] = {"mode": "claiming", "token": token}
    _save(manager, request, "pause_intent", now=now)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        pass
    request["pause"] = {"mode": "owned" if path.read_text(encoding="utf-8-sig") == token else "user",
                        "token": token, "fingerprint": _fingerprint(path),
                        "boundary_epoch": max(now(), path.stat().st_mtime_ns / 1_000_000_000)}
    request["status"] = "waiting_boundary"
    _save(manager, request, "pause_observed", now=now)


def _pause_current(manager, request, *, now=time.time):
    path, saved = manager.runtime / "PAUSE", request.get("pause") or {}
    if not path.is_file():
        return False
    actual = _fingerprint(path)
    if actual != saved.get("fingerprint"):
        # A user's second pause replaces our marker. It becomes their pause
        # forever, even if its text happens to resemble a reload marker.
        saved.update(mode="user", fingerprint=actual,
                     boundary_epoch=max(now(), actual["mtime_ns"] / 1_000_000_000))
        _save(manager, request, "user_pause_changed", now=now)
    return True


@contextmanager
def _pause_guard(manager, request, *, deleting=False):
    """Hold the exact observed marker across stop/start or an owned deletion."""
    saved = request.get("pause") or {}
    path = manager.runtime / "PAUSE"
    if os.name != "nt":
        raise RuntimeError("美术重载暂停保护需要 Windows 文件句柄。")
    # Compare and delete through one exclusive native handle. A user pause
    # written by atomic replacement cannot sneak between comparison/unlink.
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel.ReadFile.restype = wintypes.BOOL
    class FileInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("creation", wintypes.FILETIME),
                    ("access", wintypes.FILETIME), ("write", wintypes.FILETIME),
                    ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD), ("size_low", wintypes.DWORD),
                    ("links", wintypes.DWORD), ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInfo)]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000 | (0x00010000 if deleting else 0), 1, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        code = ctypes.get_last_error()
        if code in (2, 3):
            yield None
            return
        raise ctypes.WinError(code)
    try:
        buffer, length = ctypes.create_string_buffer(4096), wintypes.DWORD()
        if not kernel.ReadFile(handle, buffer, 4096, ctypes.byref(length), None):
            raise ctypes.WinError(ctypes.get_last_error())
        fact = FileInfo()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(fact)):
            raise ctypes.WinError(ctypes.get_last_error())
        raw = buffer.raw[:length.value]
        actual = {"sha256": hashlib.sha256(raw).hexdigest(),
                  "size": (fact.size_high << 32) | fact.size_low,
                  "mtime_ns": (((fact.write.dwHighDateTime << 32) | fact.write.dwLowDateTime)
                               - 116444736000000000) * 100}
        if actual != saved.get("fingerprint") or (deleting and raw != saved["token"].encode("utf-8")):
            yield None
            return
        def remove():
            delete = ctypes.c_ubyte(1)
            if not kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)):
                raise ctypes.WinError(ctypes.get_last_error())
        yield remove if deleting else True
    finally:
        kernel.CloseHandle(handle)


def _release_owned_pause(manager, request):
    if (request.get("pause") or {}).get("mode") != "owned":
        return False
    with _pause_guard(manager, request, deleting=True) as remove:
        if remove is None:
            return False
        remove()
        return True


def _paused_boundary(manager, request, status, *, identity=None, boundary_epoch=None):
    identity = identity or request["source_identity"]
    boundary_epoch = request["pause"]["boundary_epoch"] if boundary_epoch is None else boundary_epoch
    if _identity(status) != identity:
        return False
    path = manager.runtime / "heartbeat.json"
    if not path.is_file():
        return False
    heartbeat = read_json(path)
    state = read_json(manager.runtime / "state.json")
    try:
        epoch = datetime.fromisoformat(heartbeat["heartbeat_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return False
    return (heartbeat.get("paused") is True and heartbeat.get("status") == "paused" and
            epoch >= boundary_epoch and
            path.stat().st_mtime_ns / 1_000_000_000 >= boundary_epoch and
            heartbeat.get("phase") == state.get("phase") and
            heartbeat.get("task_id") == (state.get("task") or {}).get("id") and
            heartbeat.get("updated_at") == state.get("updated_at") and
            heartbeat.get("runtime_dir") == str(manager.runtime) and
            heartbeat.get("workspace") == str(manager.runtime / "workspace") and
            _identity(manager.status()) == identity)


def _finish(manager, request, status, reason, *, release=False, now=time.time):
    if _lane(manager) == "development":
        reason = reason.replace("美术", "功能开发")
    if release:
        request["pause_release_intent_at"] = _iso(now())
        _save(manager, request, "pause_release_intent", now=now)
        request["owned_pause_released"] = _release_owned_pause(manager, request)
    request.update(status=status, reason=reason)
    _save(manager, request, status, now=now)
    return request


def _advance(manager, request, *, now=time.time):
    if request["status"] in _TERMINAL:
        return request
    if request["status"] == "completing":
        current = manager.status()
        if _identity(current) != request.get("replacement_identity") or not current.get("healthy"):
            return _finish(manager, request, "blocked", "已确认的新后台身份变化或不再健康；保留暂停。", now=now)
        _pause_current(manager, request, now=now)  # Preserve any intervening user pause.
        return _finish(manager, request, "completed", "美术后台已在安全边界重载；原任务与会话保留。",
                       release=True, now=now)
    if (hashlib.sha256(manager.config_path.read_bytes()).hexdigest() != request["config_sha256"] or
            _code_fingerprint(manager) != request["code_sha256"]):
        return _finish(manager, request, "cancelled", "配置或重载目标代码已变化；未替换请求目标。",
                       release=request["status"] in {"pending", "waiting_boundary"}, now=now)
    if now() >= request["deadline_epoch"]:
        return _finish(manager, request, "timed_out", "等待超时；未中断活动操作。停止后的暂停标记保留。",
                       release=request["status"] in {"pending", "waiting_boundary"}, now=now)
    if request["status"] == "pending" or (request.get("pause") or {}).get("mode") == "claiming":
        status = manager.status()
        if _identity(status) != request["source_identity"]:
            return _finish(manager, request, "superseded", "原美术后台身份已变化；不接管新进程。", now=now)
        _claim_pause(manager, request, now=now)
        return request
    if not _pause_current(manager, request, now=now):
        return _finish(manager, request, "cancelled", "暂停标记已由外部移除；不覆盖用户恢复操作。", now=now)
    status = manager.status()
    if request["status"] == "waiting_boundary":
        if _identity(status) != request["source_identity"]:
            return _finish(manager, request, "superseded", "原后台已退出或替换；未停止任何新进程。",
                           release=True, now=now)
        if not _paused_boundary(manager, request, status):
            return request
        request["status"] = "stopping"
        request["boundary_observed_at"] = _iso(now())
        _save(manager, request, "paused_boundary_confirmed", now=now)
        return request
    if request["status"] == "stopping":
        current = _identity(status)
        if current is not None:
            if current != request["source_identity"]:
                return _finish(manager, request, "blocked", "停止前后台身份已变化；保留暂停。", now=now)
            with _pause_guard(manager, request) as held:
                if held is None or not _paused_boundary(manager, request, manager.status()):
                    return request
                manager._stop_paused_for_reload(request["source_identity"])
                _save(manager, request, "stop_requested", now=now)
            return request
        if status.get("task") and status["task"]["state"] in (2, 4):
            return request
        with _pause_guard(manager, request) as held:
            if held is None:
                return request
            request["status"] = "starting"
            request["start_intent_at"] = _iso(now())
            _save(manager, request, "start_intent", now=now)
            # This launch intent is never replayed after a crash. The following
            # poll reconciles Task Scheduler and a new owned process identity.
            manager._start_paused_for_reload()
            request["launch_submitted_at"] = _iso(now())
            _save(manager, request, "start_submitted", now=now)
        return request
    if request["status"] == "starting":
        current = _identity(status)
        start_epoch = datetime.fromisoformat(request["start_intent_at"]).timestamp()
        if (current and current != request["source_identity"] and status.get("healthy") and
                _paused_boundary(manager, request, status, identity=current, boundary_epoch=start_epoch)):
            request["replacement_identity"] = current
            request["status"] = "completing"
            _save(manager, request, "replacement_confirmed_paused", now=now)
            return _finish(manager, request, "completed", "美术后台已在安全边界重载；原任务与会话保留。",
                           release=True, now=now)
        return request  # Unknown launch receipt is never a reason to submit another start.
    raise RuntimeError("无效的美术重载状态。")


def run_helper(config_path, request_id, *, manager=None, now=time.time, sleep=time.sleep):
    manager = _manager(config_path, manager)
    guard = lock(_control_file(manager, "helper.lock"))
    try:
        guard.__enter__()
    except (OSError, RuntimeError):
        return {"status": "helper_already_running", "request_id": request_id}
    try:
        request = _read_request(manager, request_id)
        while request["status"] not in _TERMINAL:
            try:
                request = _advance(manager, request, now=now)
            except Exception as error:
                request["last_helper_error"] = {"type": type(error).__name__, "message": str(error)[:1000]}
                _save(manager, request, "helper_failed", now=now)
                raise  # Task Scheduler may retry this same durable intent, never its model.
            if request["status"] not in _TERMINAL:
                sleep(2)
        return request
    finally:
        guard.__exit__(None, None, None)
