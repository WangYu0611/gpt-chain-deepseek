"""On-demand, interactive Windows Task Scheduler host for the desktop relay.

Registration has no trigger, password, elevated principal or service account.
Importing this module never registers a task or starts a process.
"""
from __future__ import annotations

import argparse
import ctypes
import faulthandler
import getpass
import hashlib
import importlib
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

from relay.storage import read_json, run_lock, write_json

ROOT = Path(__file__).resolve().parent
OWNER = "gpt_chain_deepseek.desktop_background/v1"


def _iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def context(config_path):
    config_path = Path(config_path).expanduser().resolve()
    config = read_json(config_path)
    runtime = Path(config.get("runtime_dir", ".window-relay"))
    runtime = (runtime if runtime.is_absolute() else config_path.parent / runtime).resolve()
    project_path = config.get("dsh", {}).get("project_path")
    if project_path:
        project = Path(project_path).resolve()
        if runtime == project or project in runtime.parents or ROOT == project or project in ROOT.parents:
            raise ValueError("后台运行目录和工具必须位于游戏/项目目录之外。")
    binding = {"owner": OWNER, "config": os.path.normcase(str(config_path)),
               "runtime": os.path.normcase(str(runtime)), "manager": os.path.normcase(str(Path(__file__).resolve()))}
    description = json.dumps(binding, ensure_ascii=False, sort_keys=True)
    name = "GPTDSHRelay-" + hashlib.sha256((binding["config"] + "\n" + binding["runtime"]).encode()).hexdigest()[:16]
    return config_path, runtime, name, description


def process_identity(pid=None):
    """Read process creation time/image to reject reused or unrelated PIDs."""
    if os.name != "nt":
        raise OSError("后台桌面进程管理仅支持 Windows。")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    pid = os.getpid() if pid is None else int(pid)
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), "进程不存在或不可读取。")
    try:
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
            raise OSError("进程已退出或状态不可读取。")
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
            raise OSError(ctypes.get_last_error(), "无法读取进程创建时间。")
        image = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(image))
        if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "无法读取进程路径。")
        return {"pid": pid, "process_created": (created.dwHighDateTime << 32) | created.dwLowDateTime,
                "image": os.path.normcase(image.value)}
    finally:
        kernel.CloseHandle(handle)


class TaskScheduler:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("Windows 任务计划程序仅支持 Windows。")
        import comtypes.client
        self.COMError = comtypes.COMError
        self.service = comtypes.client.CreateObject("Schedule.Service", dynamic=True)
        self.service.Connect()  # Current user; no credentials supplied or stored.
        self.folder = self.service.GetFolder("\\")

    def _task(self, name):
        # Task Scheduler may wrap missing GetTask in generic DISP_E_EXCEPTION.
        # Enumerate exact Windows names instead; never swallow access errors.
        collection = self.folder.GetTasks(1)  # Include hidden registrations too.
        if int(collection.Count) > 2000:
            raise RuntimeError("任务计划根目录条目超限，拒绝不完整的所有权检查。")
        found = []
        for index in range(1, int(collection.Count) + 1):
            task = collection.Item(index)
            if str(task.Name).casefold() == name.casefold():
                found.append(task)
        if len(found) > 1:
            raise RuntimeError("任务名称匹配不唯一，拒绝操作。")
        return found[0] if found else None

    def info(self, name):
        task = self._task(name)
        if task is None:
            return None
        registration = task.Definition.RegistrationInfo
        return {"state": int(task.State), "last_result": int(task.LastTaskResult),
                "last_run_time": str(task.LastRunTime), "owner": str(registration.Source),
                "description": str(registration.Description)}

    def register(self, name, description, executable, arguments):
        existing = self.info(name)
        if existing and (existing["owner"] != OWNER or existing["description"] != description):
            raise RuntimeError("同名任务不属于此配置，拒绝覆盖。")
        task = self.service.NewTask(0)
        task.RegistrationInfo.Source = OWNER
        task.RegistrationInfo.Description = description
        domain = os.environ.get("USERDOMAIN", "")
        user = (domain + "\\" if domain else "") + getpass.getuser()
        task.Principal.UserId = user
        task.Principal.LogonType = 3  # Existing interactive user token, no password.
        task.Principal.RunLevel = 0  # Least privilege.
        task.Settings.Enabled = True
        task.Settings.AllowDemandStart = True
        task.Settings.Hidden = False  # Registration remains visible to the user.
        task.Settings.MultipleInstances = 2  # IgnoreNew.
        task.Settings.ExecutionTimeLimit = "PT0S"
        task.Settings.DisallowStartIfOnBatteries = False
        task.Settings.StopIfGoingOnBatteries = False
        # NewTask has an empty trigger collection; never add login/timer triggers.
        action = task.Actions.Create(0)
        action.Path, action.Arguments, action.WorkingDirectory = str(executable), arguments, str(ROOT)
        self.folder.RegisterTaskDefinition(name, task, 6, user, None, 3)

    def run(self, name):
        task = self._task(name)
        if task is None:
            raise RuntimeError("注册的后台任务不存在，未启动。")
        task.Run(None)


class BackgroundManager:
    def __init__(self, config_path, scheduler=None, inspector=process_identity, sleeper=time.sleep):
        self.config, self.runtime, self.name, self.description = context(config_path)
        self.scheduler = scheduler
        self.inspector, self.sleep = inspector, sleeper
        self.health_path = self.runtime / "background-health.json"

    def _scheduler(self):
        if self.scheduler is None:
            self.scheduler = TaskScheduler()
        return self.scheduler

    def _health(self):
        return read_json(self.health_path) if self.health_path.exists() else None

    def _alive(self, health):
        if not health or health.get("owner") != OWNER or os.path.normcase(str(health.get("config", ""))) != os.path.normcase(str(self.config)):
            return False
        try:
            current = self.inspector(health["pid"])
            return all(current[key] == health[key] for key in ("pid", "process_created", "image"))
        except (OSError, ValueError, KeyError):
            return False

    def status(self):
        health = self._health()
        alive = self._alive(health)
        age = max(0, time.time() - health.get("heartbeat_epoch", 0)) if health else None
        state_path = self.runtime / "cycle-state.json"
        progress = read_json(state_path).get("updated_at") if state_path.exists() else None
        progress_epoch = state_path.stat().st_mtime if state_path.exists() else None
        task = self._scheduler().info(self.name)
        return {"task_name": self.name, "task": task, "health": health, "process_alive": alive,
                "healthy": bool(alive and age < 35 and health.get("status") == "running"), "heartbeat_age_seconds": age,
                "state_updated_at": progress,
                "state_updated_epoch": progress_epoch,
                "state_age_seconds": max(0, time.time() - progress_epoch) if progress_epoch is not None else None}

    def start(self, timeout=20):
        if (self.runtime / "PAUSE").exists():
            raise RuntimeError("循环存在暂停标记，请先核对并运行 desktop-relay resume；不会自动清除暂停。")
        status = self.status()
        if status["process_alive"] or (status["task"] and status["task"]["state"] in (2, 4)):
            raise RuntimeError("后台实例已运行或正在启动，拒绝重复启动。")
        with run_lock(self.runtime / ".controller-lock"):
            pass  # Detect a foreground/manual runner even without a health file.
        executable = ROOT / ".venv" / "Scripts" / "pythonw.exe"
        if not executable.is_file():
            raise RuntimeError("缺少 .venv\\Scripts\\pythonw.exe，请先安装本工具运行环境。")
        instance = str(uuid.uuid4())
        arguments = subprocess.list2cmdline([str(Path(__file__).resolve()), "worker", "--config", str(self.config), "--instance-id", instance])
        scheduler = self._scheduler()
        scheduler.register(self.name, self.description, executable, arguments)
        scheduler.run(self.name)
        started = time.monotonic()
        deadline = started + timeout
        while True:
            status = self.status()
            health = status["health"] or {}
            if health.get("instance_id") == instance:
                first_progress = (isinstance(health.get("started_epoch"), (int, float))
                                  and status["state_updated_epoch"] is not None
                                  and status["state_updated_epoch"] > health["started_epoch"])
                if status["healthy"] and first_progress and status["task"] and status["task"]["state"] == 4:
                    return {"status": "started", **status}
                if health.get("status") == "stopped":
                    return {"status": "failed", **status}
            if time.monotonic() - started > 1 and status["task"] and status["task"]["state"] not in (2, 4):
                return {"status": "failed", **status, "reason": "任务已退出但未报告健康启动，检查 last_result 和错误日志。"}
            if time.monotonic() >= deadline:
                return {"status": "start_timeout", **status}
            self.sleep(0.2)

    def stop(self, timeout=25):
        status = self.status()
        task = status["task"]
        if task and (task["owner"] != OWNER or task["description"] != self.description):
            raise RuntimeError("同名任务不属于此配置，拒绝操作。")
        self.runtime.mkdir(parents=True, exist_ok=True)
        (self.runtime / "PAUSE").touch()
        deadline = time.monotonic() + timeout
        while True:
            status = self.status()
            health = status["health"] or {}
            if not status["process_alive"] or health.get("status") == "stopped":
                return {"status": "stopped", **status}
            if time.monotonic() >= deadline:
                return {"status": "stopping", **status, "reason": "已请求暂停；没有强制结束任何进程。"}
            self.sleep(0.2)


def worker(config_path, instance_id=None, relay_main=None, inspector=process_identity, interval=10):
    config, runtime, name, _ = context(config_path)
    runtime.mkdir(parents=True, exist_ok=True)
    stdout = (runtime / "auto-run.log").open("a", encoding="utf-8", buffering=1)
    stderr = (runtime / "auto-run.err.log").open("a", encoding="utf-8", buffering=1)
    original_out, original_error = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout, stderr  # Before importing the relay entry.
    stop, lock = threading.Event(), threading.Lock()
    health_path = runtime / "background-health.json"
    health = {"owner": OWNER, "config": str(config), "runtime": str(runtime), "task_name": name,
              "instance_id": instance_id or str(uuid.uuid4()), "status": "starting", "started_at": _iso(),
              "started_epoch": time.time(),
              "exit_code": None, "exited_at": None, "last_error": None}
    code, thread = 1, None
    def pulse():
        with lock:
            health.update(heartbeat_at=_iso(), heartbeat_epoch=time.time())
            state = runtime / "cycle-state.json"
            health["loop_state_mtime"] = state.stat().st_mtime if state.exists() else None
            write_json(health_path, health)
    def heartbeat():
        while not stop.wait(interval):
            try:
                pulse()
            except BaseException:
                traceback.print_exc(file=stderr)
                stderr.flush()
    try:
        health.update(inspector())
        faulthandler.enable(file=stderr, all_threads=True)
        pulse()
        if relay_main is None:
            relay_main = importlib.import_module("desktop_relay").main
        health["status"] = "running"
        pulse()
        thread = threading.Thread(target=heartbeat, name="relay-health", daemon=True)
        thread.start()
        print(f"[{_iso()}] 后台实例 {health['instance_id']} 启动，PID={health['pid']}", flush=True)
        code = int(relay_main(["--config", str(config), "run", "--send"]) or 0)
        if code:
            health["last_error"] = f"Relay exited with code {code}; see auto-run.log / auto-run.err.log."
    except BaseException:
        health["last_error"] = traceback.format_exc()
        traceback.print_exc(file=stderr)
        code = 1
    finally:
        stop.set()
        if thread:
            thread.join(timeout=2)
        with lock:
            health.update(status="stopped", exit_code=code, exited_at=_iso(), heartbeat_at=_iso(), heartbeat_epoch=time.time())
            write_json(health_path, health)
        print(f"[{_iso()}] 后台实例退出，code={code}", flush=True)
        faulthandler.disable()
        stdout.flush(); stderr.flush()
        sys.stdout, sys.stderr = original_out, original_error
        stdout.close(); stderr.close()
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description="Windows 交互会话后台传话进程管理")
    parser.add_argument("--config", type=Path, default=ROOT / "desktop-relay.json")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "status", "stop", "worker"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        if name == "worker": command.add_argument("--instance-id")
    args = parser.parse_args(argv)
    if args.command == "worker":
        return worker(args.config, args.instance_id)
    try:
        result = getattr(BackgroundManager(args.config), args.command)()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("status") in {"failed", "start_timeout"} else 0
    except Exception as error:
        print(f"后台管理错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
