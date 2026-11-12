"""Windows Task Scheduler hosting; importing never registers or starts tasks."""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from desktop_background import process_identity
from .storage import is_windows_sharing_error, write_json

ROOT = Path(__file__).resolve().parent.parent
OWNER = "gpt_chain_deepseek.unattended/v1"


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig")) if Path(path).exists() else None


def _write(path, value):
    write_json(Path(path), value)


def context(config_path):
    from .config import load_config
    config_path = Path(config_path).expanduser().resolve()
    config = load_config(config_path)
    runtime = Path(config["runtime_dir"]).resolve()
    binding = {"owner": OWNER, "config": os.path.normcase(str(config_path)),
               "runtime": os.path.normcase(str(runtime)), "root": str(ROOT)}
    description = json.dumps(binding, ensure_ascii=False, sort_keys=True)
    name = "CodexDSH-" + hashlib.sha256(binding["config"].encode("utf-8")).hexdigest()[:16]
    return config, runtime, name, description


class TaskScheduler:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("后台任务管理需要 Windows 任务计划程序；其他系统可使用 run 命令。")
        import comtypes.client
        self.service = comtypes.client.CreateObject("Schedule.Service", dynamic=True)
        self.service.Connect()
        self.folder = self.service.GetFolder("\\")

    def _task(self, name):
        tasks = self.folder.GetTasks(1)
        if int(tasks.Count) > 2000:
            raise RuntimeError("任务数量超限，无法完整核对后台任务所有权。")
        matches = [tasks.Item(index) for index in range(1, int(tasks.Count) + 1)
                   if str(tasks.Item(index).Name).casefold() == name.casefold()]
        if len(matches) > 1:
            raise RuntimeError("后台任务名称不唯一。")
        return matches[0] if matches else None

    def info(self, name):
        task = self._task(name)
        if task is None:
            return None
        registration = task.Definition.RegistrationInfo
        triggers = task.Definition.Triggers
        return {"state": int(task.State), "last_result": int(task.LastTaskResult),
                "last_run_time": str(task.LastRunTime), "owner": str(registration.Source),
                "description": str(registration.Description),
                "logon_trigger": any(int(triggers.Item(i).Type) == 9 for i in range(1, int(triggers.Count) + 1))}

    def _owned(self, name, description):
        info = self.info(name)
        if info and (info["owner"] != OWNER or info["description"] != description):
            raise RuntimeError("同名任务不属于此配置，拒绝修改或停止。")
        return info

    def register(self, name, description, executable, arguments, *, logon=False):
        existing = self._owned(name, description)
        if existing and existing["state"] in (2, 4):
            raise RuntimeError("后台任务正在运行，不能覆盖注册。")
        task = self.service.NewTask(0)
        task.RegistrationInfo.Source = OWNER
        task.RegistrationInfo.Description = description
        domain = os.environ.get("USERDOMAIN", "")
        user = (domain + "\\" if domain else "") + getpass.getuser()
        task.Principal.UserId = user
        task.Principal.LogonType = 3
        task.Principal.RunLevel = 0
        task.Settings.Enabled = True
        task.Settings.AllowDemandStart = True
        task.Settings.Hidden = False
        task.Settings.MultipleInstances = 2
        task.Settings.ExecutionTimeLimit = "PT0S"
        task.Settings.DisallowStartIfOnBatteries = False
        task.Settings.StopIfGoingOnBatteries = False
        task.Settings.StartWhenAvailable = True
        task.Settings.RestartInterval = "PT1M"
        task.Settings.RestartCount = 3
        if logon or (existing and existing.get("logon_trigger")):
            trigger = task.Triggers.Create(9)
            trigger.UserId = user
            trigger.Enabled = True
        action = task.Actions.Create(0)
        action.Path = str(executable)
        action.Arguments = arguments
        action.WorkingDirectory = str(ROOT)
        self.folder.RegisterTaskDefinition(name, task, 6, user, None, 3)

    def run(self, name):
        task = self._task(name)
        if task is None:
            raise RuntimeError("后台任务尚未注册。")
        task.Run(None)

    def stop(self, name, description):
        if self._owned(name, description):
            self._task(name).Stop(0)

    def delete(self, name, description):
        info = self._owned(name, description)
        if info and info["state"] in (2, 4):
            raise RuntimeError("后台任务仍在运行，请先 stop。")
        if info:
            self.folder.DeleteTask(name, 0)


class BackgroundManager:
    def __init__(self, config_path, scheduler=None, inspector=process_identity, sleeper=time.sleep):
        self.config_path = Path(config_path).expanduser().resolve()
        self.config, self.runtime, self.name, self.description = context(self.config_path)
        self.scheduler, self.inspector, self.sleep = scheduler, inspector, sleeper
        self.health_path = self.runtime / "background-health.json"

    def _scheduler(self):
        if self.scheduler is None:
            self.scheduler = TaskScheduler()
        return self.scheduler

    def status(self):
        task = self._scheduler().info(self.name)
        health = _read(self.health_path)
        alive = False
        if health and health.get("owner") == OWNER and health.get("config") == str(self.config_path):
            try:
                actual = self.inspector(health["pid"])
                alive = all(actual[key] == health[key] for key in ("pid", "process_created", "image"))
            except (OSError, ValueError, KeyError):
                pass
        age = max(0, time.time() - health.get("heartbeat_epoch", 0)) if health else None
        return {"status": "running" if alive else "stopped", "task_name": self.name, "task": task,
                "process_alive": alive, "healthy": bool(alive and age < 35 and health.get("status") in {"running", "recovering"}),
                "heartbeat_age_seconds": age, "health": health}

    def _registration(self, logon=False):
        executable = Path(sys.executable).with_name("pythonw.exe")
        if not executable.is_file():
            executable = ROOT / ".venv" / "Scripts" / "pythonw.exe"
        if not executable.is_file():
            raise RuntimeError("缺少 pythonw.exe，请先为本工具安装 Windows Python 环境。")
        arguments = subprocess.list2cmdline(["-m", "unattended", "background", "worker", "--config", str(self.config_path)])
        self._scheduler().register(self.name, self.description, executable, arguments, logon=logon)

    def install(self):
        self._registration(logon=True)
        return {"status": "installed", "task_name": self.name, "logon_start": True,
                "message": "已注册当前用户登录启动；本次未启动循环。"}

    def start(self, timeout=15):
        if (self.runtime / "PAUSE").exists():
            raise RuntimeError("循环已暂停；请先执行 unattended.cmd resume --config \"" + str(self.config_path) + "\"，再启动后台。")
        before = self.status()
        if before["process_alive"] or (before["task"] and before["task"]["state"] in (2, 4)):
            raise RuntimeError("后台实例正在运行或启动，拒绝重复启动。")
        self._registration()
        self._scheduler().run(self.name)
        started = time.monotonic()
        deadline = started + timeout
        while True:
            status = self.status()
            if status["healthy"]:
                return dict(status, status="started")
            if time.monotonic() - started > 1 and status["task"] and status["task"]["state"] not in (2, 4):
                return dict(status, status="failed", message="后台任务已退出，请检查 background.err.log。")
            if time.monotonic() >= deadline:
                return dict(status, status="start_timeout", message="尚未确认后台进程健康启动。")
            self.sleep(0.2)

    def stop(self, timeout=10):
        from .engine import Engine
        self._scheduler()._owned(self.name, self.description)
        try:
            Engine(self.config).pause()
        except (ValueError, RuntimeError):
            # pause() saves the marker before reading its status. A changed
            # goal/check binding can make that status unreadable, but must not
            # prevent stopping the old process after pause was persisted.
            if not (self.runtime / "PAUSE").exists():
                raise
        self._scheduler().stop(self.name, self.description)
        deadline = time.monotonic() + max(0, min(float(timeout), 15))
        while True:
            status = self.status()
            if not status["process_alive"] and (not status["task"] or status["task"]["state"] not in (2, 4)):
                health = status["health"]
                if health and health.get("owner") == OWNER and health.get("config") == str(self.config_path):
                    health.update(status="stopped", stopped_by_user=True,
                                  exited_at=datetime.now(timezone.utc).isoformat(), heartbeat_epoch=time.time())
                    _write(self.health_path, health)
                return {"status": "stopped", "task_name": self.name, "message": "已确认本配置后台进程停止，暂停请求及原任务断点保留。"}
            if time.monotonic() >= deadline:
                return {"status": "stopping", "task_name": self.name,
                        "message": "已持久化暂停并请求停止，尚未确认进程退出；请用 background status 核对。"}
            self.sleep(0.2)

    def uninstall(self):
        self._scheduler().delete(self.name, self.description)
        return {"status": "uninstalled", "task_name": self.name, "message": "后台注册已移除，任务及日志保留。"}

    def _stop_paused_for_reload(self, identity):
        """Only the coordinator may call this after a fresh paused loop receipt."""
        self._scheduler()._owned(self.name, self.description)
        current = self.status()
        if not (self.runtime / "PAUSE").exists():
            raise RuntimeError("功能后台重载暂停标记已改变，拒绝停止。")
        if not current["process_alive"] or any((current.get("health") or {}).get(key) != identity.get(key)
                                                for key in ("pid", "process_created", "image")):
            raise RuntimeError("功能后台身份已改变，拒绝停止其他进程。")
        self._scheduler().stop(self.name, self.description)

    def _start_paused_for_reload(self):
        """Launch the host paused; never retry or mutate the controller checkpoint."""
        self._scheduler()._owned(self.name, self.description)
        current = self.status()
        if not (self.runtime / "PAUSE").exists():
            raise RuntimeError("功能后台重载必须在暂停标记仍存在时启动。")
        if current["process_alive"] or current["task"] and current["task"]["state"] in (2, 4):
            raise RuntimeError("功能后台已运行或正在启动，拒绝重复启动。")
        self._registration()
        self._scheduler().run(self.name)


def worker(config_path, run=None, inspector=process_identity, interval=10, recovery_seconds=30):
    """Redirect logs before importing the engine, and publish a separate heartbeat."""
    config_path = Path(config_path).expanduser().resolve()
    config, runtime, name, _ = context(config_path)
    runtime.mkdir(parents=True, exist_ok=True)
    health_path = runtime / "background-health.json"
    stop = threading.Event()
    mutex = threading.Lock()
    original_out, original_error = sys.stdout, sys.stderr
    health = {"owner": OWNER, "config": str(config_path), "task_name": name,
              "status": "starting", "started_at": datetime.now(timezone.utc).isoformat(),
              "last_error": None, "exit_code": None}
    code, thread = 1, None
    with (runtime / "background.log").open("a", encoding="utf-8", buffering=1) as stdout, \
         (runtime / "background.err.log").open("a", encoding="utf-8", buffering=1) as stderr:
        sys.stdout, sys.stderr = stdout, stderr
        def pulse():
            with mutex:
                candidate = dict(health, heartbeat_epoch=time.time())
                _write(health_path, candidate)
                # Only publish a new in-memory heartbeat after its atomic
                # receipt is durable. A failed write cannot refresh health.
                health["heartbeat_epoch"] = candidate["heartbeat_epoch"]
        def best_effort_pulse():
            try:
                pulse()
            except OSError as error:
                if not is_windows_sharing_error(error):
                    raise
                print(json.dumps({"event": "health_write_wait", "winerror": error.winerror}),
                      file=stderr, flush=True)
        def running():
            with mutex:
                health["status"], health["last_error"] = "running", None
                health.pop("recovery", None)
            best_effort_pulse()
        def heartbeat():
            while not stop.wait(interval):
                try:
                    best_effort_pulse()
                except Exception:
                    traceback.print_exc(file=stderr)
        try:
            health.update(inspector())
            if run is None:
                # cli.main deliberately turns exceptions into exit codes.
                # Keep the typed sharing error here so the host can resume
                # the same durable Engine checkpoint instead of exiting.
                from .engine import Engine
                def execute():
                    engine = Engine(config)
                    engine.status()  # Verify durable checkpoint access first.
                    running()
                    value = engine.run()
                    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)
                    status = str(value.get("status", "")).casefold() if isinstance(value, dict) else ""
                    if status == "interrupted":
                        return 130
                    unhealthy = isinstance(value, dict) and (value.get("ready") is False or value.get("ok") is False)
                    return 1 if unhealthy or status in {"error", "failed", "blocked", "needs_setup", "start_timeout"} else 0
            else:
                def execute():
                    running()
                    return run()
            health["status"] = "running"
            best_effort_pulse()
            thread = threading.Thread(target=heartbeat, daemon=True, name="unattended-heartbeat")
            thread.start()
            attempt = 0
            while True:
                try:
                    code = int(execute() or 0)
                    break
                except OSError as error:
                    if not is_windows_sharing_error(error):
                        raise
                    attempt += 1
                    retry_at = datetime.now(timezone.utc) + timedelta(seconds=recovery_seconds)
                    with mutex:
                        health.update(status="recovering", last_error="状态文件暂被占用，等待自动恢复。",
                                      recovery={"kind": "filesystem_sharing", "attempt": attempt,
                                                "next_retry_at": retry_at.isoformat()})
                    print(json.dumps({"event": "filesystem_recovery", "attempt": attempt,
                                      "winerror": error.winerror, "next_retry_at": retry_at.isoformat()}),
                          file=stderr, flush=True)
                    best_effort_pulse()
                    # Re-enter through Engine's locks and durable state. Never
                    # clear PAUSE, alter the calendar or reset failure counts.
                    stop.wait(recovery_seconds)
            if code:
                health["last_error"] = "后台循环返回非零状态；请查看 background.log 与 background.err.log。"
        except BaseException:
            health["last_error"] = traceback.format_exc()
            traceback.print_exc(file=stderr)
            code = 1
        finally:
            stop.set()
            if thread:
                thread.join(timeout=2)
            health.update(status="stopped", exit_code=code, exited_at=datetime.now(timezone.utc).isoformat())
            try:
                best_effort_pulse()
            finally:
                sys.stdout, sys.stderr = original_out, original_error
    return code
