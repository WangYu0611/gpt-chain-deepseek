"""Loopback dashboard with explicit, isolated, hash-bound art review actions."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import getpass
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit, quote
from urllib.request import urlopen
import webbrowser

from desktop_background import process_identity
from .background import TaskScheduler
from .config import load_config
from .dashboard_state import collect_snapshot
from .storage import lock, read_json, write_json

ROOT = Path(__file__).resolve().parent.parent
OWNER = "gpt_chain_deepseek.dashboard/v1"
DEFAULT_PORT = 8765


def _identity(config_path):
    path = Path(config_path).expanduser().resolve()
    return path, hashlib.sha256(os.path.normcase(str(path)).encode()).hexdigest()[:16]


class SnapshotCache:
    def __init__(self, config_path):
        self.config_path = Path(config_path)
        self.mutex = threading.Lock()
        self.value = collect_snapshot(self.config_path)
        self.action_token = secrets.token_urlsafe(32)
        self.notifications = {"enabled": False, "last_error": None}

    def refresh(self):
        value = collect_snapshot(self.config_path)
        with self.mutex:
            self.value = value

    def get(self):
        with self.mutex:
            value = deepcopy(self.value)
            value["notifications"] = deepcopy(self.notifications)
            if value.get("art", {}).get("configured"):
                value["art"]["action_token"] = self.action_token
        try:
            from .dashboard_flows import collect_flows
            value["flows"] = collect_flows(self.config_path, value)
        except Exception:
            value["flows"] = {"lanes": [], "error": "流程状态暂时无法核实"}
        return value

    def flow_detail(self, lane, node):
        from .dashboard_evidence import collect_node_detail
        return collect_node_detail(self.config_path, lane, node, snapshot=self.get())

    def flow_evidence(self, lane, node, ref):
        from .dashboard_evidence import read_node_evidence
        return read_node_evidence(self.config_path, lane, node, ref, snapshot=self.get())

    def resolve_art(self, payload):
        from .art_worker import ArtWorker
        worker = ArtWorker.from_main_config(load_config(self.config_path))
        result = worker.resolve_art(payload["task_id"], payload["decision"], payload["feedback"],
                                    payload["review_package_sha256"])
        self.refresh()
        return result

    def respond_art_diagnosis(self, payload):
        from .art_worker import ArtWorker
        worker = ArtWorker.from_main_config(load_config(self.config_path))
        result = worker.respond_art_diagnosis(payload["task_id"], payload["receipt_sha256"], payload["feedback"])
        self.refresh()
        return result

    def art_preview(self, task_id, review_sha, index):
        from .dashboard_art import preview_png
        return preview_png(load_config(self.config_path), task_id, review_sha, index)

    def notification_status(self, value):
        # The webhook, settings object and full journal never reach the browser.
        public = {key: value.get(key) for key in
                  ("enabled", "last_sent_at", "last_event", "last_error", "pending")}
        public["last_sent_at"] = value.get("last_sent_at") or value.get("last_success_at")
        public["last_event"] = {"test": "连接测试", "window_start": "开工简报", "accepted": "任务验收通过",
                                "blocked": "任务阻塞", "worker_exit": "后台异常", "window_end": "收工简报",
                                "art_review_ready": "模型待视觉验收", "art_accepted": "模型通过意见已提交",
                                "art_rework": "模型返工意见已提交", "art_blocked": "模型制作异常",
                                "full_flow_started": "Opus全流程测试已启动", "full_flow_finished": "Opus全流程测试已结束"}.get(value.get("last_event"))
        public["last_error"] = {"delivery_failed": "投递失败，将按退避策略重试", "http_error": "飞书连接失败",
                                "bot_rejected": "机器人拒绝卡片，请检查飞书机器人安全设置",
                                "redirect_rejected": "通知地址发生重定向，已拒绝发送",
                                "invalid_response": "飞书返回结果无法确认", "card_too_large": "卡片超过大小限制",
                                "delivery_uncertain": "上次投递结果未知，已避免重复发送",
                                "notification_state_unavailable": "简报配置或记录暂时无法读取"}.get(value.get("last_error"), value.get("last_error"))
        with self.mutex:
            self.notifications = public


def make_handler(cache, identity):
    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalStatusPanel"

        def log_message(self, *args):
            pass

        def _allowed(self):
            port = self.server.server_port
            if self.headers.get("Host", "") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

        def _respond(self, status, payload, content_type="application/json; charset=utf-8", filename=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            if filename:
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename, safe=""))
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
                             "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def do_GET(self):
            if not self._allowed():
                self._respond(403, b'{"error":"local origin required"}')
                return
            path = urlsplit(self.path).path
            if path in {"/", "/dashboard.html"}:
                self._respond(200, (ROOT / "unattended/dashboard.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/status":
                self._respond(200, json.dumps(cache.get(), ensure_ascii=False).encode("utf-8"))
            elif path == "/api/health":
                self._respond(200, json.dumps(identity).encode())
            elif path in {"/api/flow/detail", "/api/flow/evidence"}:
                try:
                    query = parse_qs(urlsplit(self.path).query, strict_parsing=True)
                    required = {"lane", "node"} | ({"ref"} if path.endswith("/evidence") else set())
                    if set(query) != required or any(len(v) != 1 for v in query.values()):
                        raise ValueError("节点证据参数无效")
                    lane, node = query["lane"][0], query["node"][0]
                    if path.endswith("/detail"):
                        result = cache.flow_detail(lane, node)
                        self._respond(200, json.dumps(result, ensure_ascii=False).encode("utf-8"))
                    else:
                        data, mime, filename = cache.flow_evidence(lane, node, query["ref"][0])
                        self._respond(200, data, mime, filename)
                except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
                    from .dashboard_evidence import redact
                    self._respond(409, json.dumps({"error": redact(str(error), 300)}, ensure_ascii=False).encode("utf-8"))
            elif path == "/api/art/preview":
                try:
                    query = parse_qs(urlsplit(self.path).query, strict_parsing=True)
                    if set(query) != {"task", "review", "index"} or any(len(v) != 1 for v in query.values()):
                        raise ValueError("预览参数无效。")
                    data = cache.art_preview(query["task"][0], query["review"][0], int(query["index"][0]))
                    self._respond(200, data, "image/png")
                except (OSError, ValueError, TypeError, KeyError) as error:
                    from .dashboard_state import _clean
                    self._respond(409, json.dumps({"error": _clean(str(error), 300)}, ensure_ascii=False).encode())
            elif path == "/favicon.ico":
                self._respond(204, b"")
            else:
                self._respond(404, b'{"error":"not found"}')

        do_HEAD = do_GET

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in {"/api/art/resolve", "/api/art/diagnosis/respond"} or urlsplit(self.path).query:
                self._respond(405, b'{"error":"unsupported action"}')
                return
            origin = self.headers.get("Origin")
            token = self.headers.get("X-Art-Action-Token", "")
            expected = getattr(cache, "action_token", None)
            if (not self._allowed() or origin not in {f"http://127.0.0.1:{self.server.server_port}",
                                                       f"http://localhost:{self.server.server_port}"} or
                    not isinstance(expected, str) or not secrets.compare_digest(token, expected)):
                self._respond(403, b'{"error":"local action token required"}')
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384 or self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    raise ValueError("美术操作请求必须为有长度限制的 JSON。")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if path == "/api/art/diagnosis/respond":
                    fields = {"task_id", "receipt_sha256", "feedback"}
                    if (not isinstance(payload, dict) or set(payload) != fields or
                            any(not isinstance(payload[key], str) for key in fields) or
                            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", payload["task_id"]) or
                            not re.fullmatch(r"[a-f0-9]{64}", payload["receipt_sha256"]) or
                            not payload["feedback"].strip() or len(payload["feedback"]) > 4000):
                        raise ValueError("回复必须指定当前美术任务、诊断回执版本，并填写不超过 4000 字的意见；请刷新后核对。")
                    result = cache.respond_art_diagnosis(payload)
                else:
                    fields = {"task_id", "decision", "feedback", "review_package_sha256"}
                    if (not isinstance(payload, dict) or set(payload) != fields or
                            any(not isinstance(payload[key], str) for key in fields) or
                            payload["decision"] not in {"accepted", "rework"} or
                            not payload["feedback"].strip() or len(payload["feedback"]) > 4000):
                        raise ValueError("验收必须指定任务、当前版本、通过或返工，并填写意见。")
                    result = cache.resolve_art(payload)
                self._respond(200, json.dumps(result, ensure_ascii=False).encode())
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
                from .dashboard_state import _clean
                self._respond(409, json.dumps({"error": _clean(str(error), 400)}, ensure_ascii=False).encode())

        def do_PUT(self):
            self._respond(405, b'{"error":"unsupported method"}')

        do_DELETE = do_PATCH = do_OPTIONS = do_PUT

    return Handler


def _sample(cache, stop):
    while not stop.wait(3):
        try:
            cache.refresh()
        except Exception:
            # Keep the last readable state; don't claim a stopped engine.
            with cache.mutex:
                cache.value["error"] = "状态读取暂时失败；当前保留上次快照。"


def _notify(cache, config_path, settings_path, stop):
    monitor = None
    while not stop.is_set():
        try:
            if settings_path.exists():
                if monitor is None:
                    from .notifications import NotificationMonitor
                    monitor = NotificationMonitor(config_path, settings_path)
                cache.notification_status(monitor.tick(cache.get()))
            else:
                cache.notification_status({"enabled": False, "last_error": None})
        except Exception as error:
            cache.notification_status({"enabled": True, "last_error":
                                       "简报服务暂时不可用（" + type(error).__name__ + "）"})
        stop.wait(10)


def serve(config_path, port=DEFAULT_PORT):
    path, key = _identity(config_path)
    config = load_config(path)
    runtime = Path(config["runtime_dir"])
    runtime.mkdir(parents=True, exist_ok=True)
    settings_path = ROOT / ".unattended/feishu.local.json"
    with lock(runtime / "dashboard-server.lock"), \
         (runtime / "dashboard.log").open("a", encoding="utf-8", buffering=1) as log, \
         (runtime / "dashboard.err.log").open("a", encoding="utf-8", buffering=1) as error_log, \
         redirect_stdout(log), redirect_stderr(error_log):
        cache = SnapshotCache(path)
        identity = {"service": OWNER, "config_id": key, "instance": secrets.token_hex(16)}
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(cache, identity))
        server.daemon_threads = True
        metadata = {**identity, **process_identity(os.getpid()), "port": server.server_port,
                    "started_at": datetime.now(timezone.utc).isoformat()}
        write_json(runtime / "dashboard.json", metadata)
        stop = threading.Event()
        threads = [threading.Thread(target=_sample, args=(cache, stop), daemon=True),
                   threading.Thread(target=_notify, args=(cache, path, settings_path, stop), daemon=True)]
        for thread in threads:
            thread.start()
        print(json.dumps({"status": "listening", "port": server.server_port,
                          "config_id": key, "started_at": metadata["started_at"]}), flush=True)
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            stop.set()
            server.server_close()
            for thread in threads:
                thread.join(timeout=9)


class DashboardManager:
    def __init__(self, config_path, port=DEFAULT_PORT):
        self.path, self.key = _identity(config_path)
        self.config = load_config(self.path)
        self.runtime = Path(self.config["runtime_dir"])
        self.port = port
        self.name = "CodexDSHPanel-" + self.key
        self.description = json.dumps({"owner": OWNER, "config": os.path.normcase(str(self.path)),
                                       "root": str(ROOT)}, sort_keys=True, ensure_ascii=False)

    def _running(self):
        try:
            metadata = read_json(self.runtime / "dashboard.json")
            if metadata.get("service") != OWNER or metadata.get("config_id") != self.key:
                return None
            current = process_identity(metadata["pid"])
            if any(current[field] != metadata[field] for field in ("pid", "process_created", "image")):
                return None
            port = metadata["port"]
            if type(port) is not int or not 1 <= port <= 65535:
                return None
            url = f"http://127.0.0.1:{port}/"
            with urlopen(url + "api/health", timeout=2) as response:
                if response.geturl() != url + "api/health":
                    return None
                health = json.loads(response.read(4096))
            if any(health.get(field) != metadata.get(field) for field in ("service", "config_id", "instance")):
                return None
            return {"status": "running", "url": url, "pid": metadata["pid"], "task_name": self.name}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _register(self, scheduler):
        info = scheduler.info(self.name)
        if info and (info["owner"] != OWNER or info["description"] != self.description):
            raise RuntimeError("面板任务名已由其它配置使用，拒绝覆盖。")
        if info and info["state"] in (2, 4):
            return
        executable = Path(sys.executable).with_name("pythonw.exe")
        if not executable.is_file():
            executable = ROOT / ".venv/Scripts/pythonw.exe"
        if not executable.is_file():
            raise RuntimeError("本地面板需要 Windows pythonw.exe。")
        user = ((os.environ.get("USERDOMAIN", "") + "\\") if os.environ.get("USERDOMAIN") else "") + getpass.getuser()
        task = scheduler.service.NewTask(0)
        task.RegistrationInfo.Source = OWNER
        task.RegistrationInfo.Description = self.description
        task.Principal.UserId, task.Principal.LogonType, task.Principal.RunLevel = user, 3, 0
        task.Settings.Enabled = True
        task.Settings.AllowDemandStart = True
        task.Settings.MultipleInstances = 2
        task.Settings.ExecutionTimeLimit = "PT0S"
        task.Settings.DisallowStartIfOnBatteries = False
        task.Settings.StopIfGoingOnBatteries = False
        task.Settings.StartWhenAvailable = True
        task.Settings.RestartInterval, task.Settings.RestartCount = "PT1M", 3
        trigger = task.Triggers.Create(9)
        trigger.UserId, trigger.Enabled = user, True
        action = task.Actions.Create(0)
        action.Path = str(executable)
        action.Arguments = subprocess.list2cmdline(["-m", "unattended", "dashboard", "--serve", "--config",
                                                   str(self.path), "--port", str(self.port)])
        action.WorkingDirectory = str(ROOT)
        scheduler.folder.RegisterTaskDefinition(self.name, task, 6, user, None, 3)

    def start(self, open_browser=True):
        self.runtime.mkdir(parents=True, exist_ok=True)
        with lock(self.runtime / "dashboard-start.lock"):
            running = self._running()
            if running is None:
                scheduler = TaskScheduler()
                self._register(scheduler)
                info = scheduler.info(self.name)
                if info["state"] not in (2, 4):
                    scheduler.run(self.name)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    running = self._running()
                    if running:
                        break
                    time.sleep(0.2)
                if running is None:
                    raise RuntimeError("面板未确认启动，请检查运行目录 dashboard.err.log。")
        if open_browser:
            webbrowser.open(running["url"], new=2)
        return running
