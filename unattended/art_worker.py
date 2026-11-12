"""Independent, durable art lane hosted by the unattended system.

The normal game controller is never opened or changed by this worker. Production
uses a pre-existing isolated workspace; approval creates a promotion request for
the separately controlled game integration consumer, never a direct Content copy.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from . import gitops
from .background import BackgroundManager, ROOT, worker as background_worker
from .config import load_config
from .engine import Engine
from .storage import lock, read_json, write_json


class ArtWorker(Engine):
    def __init__(self, config, providers=None, *, now=None):
        if not config.get("art_pipeline") or config.get("coding_provider") != "claude":
            raise ValueError("美术 worker 需要独立 Claude 配置和 art_pipeline。")
        if config.get("push") or config.get("publication") or config.get("art_worker"):
            raise ValueError("美术 worker 不允许发布游戏分支、里程碑策略或递归 worker 路由。")
        super().__init__(config, providers, now=now)

    @classmethod
    def from_main_config(cls, main_config, providers=None, *, now=None):
        route = main_config.get("art_worker")
        if not route or not route.get("config_file"):
            raise ValueError("主面板尚未配置独立 art_worker.config_file。")
        config = load_config(Path(route["config_file"]))
        main_roots = [Path(main_config[key]).resolve() for key in ("project_path", "runtime_dir")]
        worker_roots = [Path(config[key]).resolve() for key in ("project_path", "runtime_dir")]
        if any(a == b or a.is_relative_to(b) or b.is_relative_to(a) for a in main_roots for b in worker_roots):
            raise ValueError("美术 worker 的项目与运行目录必须独立于功能开发目录。")
        return cls(config, providers, now=now)

    def _locks(self):
        stack = ExitStack()
        try:
            # Interlock both generic controllers and the retired one-shot entry.
            # These locks all belong to this art runtime, never the game repo.
            for name in ("art-worker.lock", "controller.lock", "claude-runner.lock"):
                stack.enter_context(lock(self.runtime / name))
        except OSError as error:
            stack.close()
            raise RuntimeError("美术 worker 锁文件不可用或已有实例持锁。") from error
        except BaseException:
            stack.close()
            raise
        return stack

    def _load(self):
        state = super()._load()
        if state["phase"] not in {"PLAN", "CODE", "ART_CHECK", "ART_REVIEW", "ART_DIAGNOSE", "BLOCKED"}:
            raise ValueError("美术 worker 拒绝接管普通功能任务或旧单次执行器断点。")
        task, op = state.get("task"), state.get("operation")
        if task is not None and task.get("kind") != "art":
            raise ValueError("美术 worker 只能恢复 kind=art 的任务。")
        if state["phase"] in {"CODE", "ART_CHECK", "ART_REVIEW", "ART_DIAGNOSE"} and task is None:
            raise ValueError("美术活动阶段丢失任务绑定。")
        if state["phase"] == "PLAN" and (task is not None or op is not None or state.get("planning_selection")):
            raise ValueError("美术空闲阶段不能包含普通规划或活动操作。")
        return state

    def _workspace(self):
        work = self.runtime / "workspace"
        if work.resolve().parent != self.runtime.resolve() or not (work / ".git").is_dir():
            raise RuntimeError("美术 worker 需要预先建立的隔离 Git 工作副本；不会克隆游戏项目。")
        if gitops.common_dir(work).resolve() != (work / ".git").resolve():
            raise RuntimeError("美术工作副本不能引用外部 Git 元数据。")
        if gitops.git(work, "symbolic-ref", "--short", "HEAD") != self.config["branch"]:
            raise RuntimeError("美术工作分支与独立配置不匹配。")
        return work

    def _checks_ready(self):
        # ART_CHECK has its own fixed independent verifier, not game checks.
        return True

    def _plan(self, state, work):
        if not gitops.clean(work):
            raise RuntimeError("美术派发前工作副本必须干净。")
        self._dispatch_art(state, work)
        # No generic planner, coding task, milestone, or game publication.

    def _snapshot(self, state):
        result = super()._snapshot(state)
        queue = self.art_queue()
        if not result["paused"] and state["phase"] == "PLAN":
            actionable = any(item.get("status") == "queued" or item.get("decision")
                             and item.get("status") == "awaiting_approval" for item in queue["requests"])
            result["status"] = "queued" if actionable else "awaiting_approval" if state.get("art_pending") else "idle"
        result.update(worker_mode="independent_art", queue=queue, promotions=self.promotion_queue())
        return result

    def promotion_queue(self):
        path = self.runtime / "promotion-requests.json"
        value = read_json(path) if path.exists() else {"version": 1, "requests": []}
        if value.get("version") != 1 or not isinstance(value.get("requests"), list):
            raise ValueError("美术接入请求队列格式无效。")
        return value

    def _emit_promotions(self):
        accepted = self._load().get("art_history", [])
        if not accepted:
            return
        with lock(self.runtime / "promotion-requests.lock"):
            queue = self.promotion_queue()
            changed = False
            for record in accepted:
                decision = record.get("visual_decision") or {}
                if record.get("status") != "accepted" or decision.get("decision") != "accepted":
                    raise RuntimeError("未获用户接受的美术记录不能创建接入任务。")
                request = record["task"]["art_request"]
                config_file = Path(request["config_file"])
                if hashlib.sha256(config_file.read_bytes()).hexdigest() != request["config_sha256"]:
                    raise RuntimeError("获批批次配置发生变化，不能创建接入任务。")
                batch = read_json(config_file)
                key = hashlib.sha256((batch["batch_id"] + ":" + record["review_package_sha256"]).encode()).hexdigest()
                entry = {"id": "art-promotion-" + key[:24], "status": "queued", "kind": "approved_art",
                         "source_task_id": record["id"], "batch_id": batch["batch_id"],
                         "config_file": request["config_file"], "config_sha256": request["config_sha256"],
                         "artifacts_dir": request["artifacts_dir"], "approved_levels": batch["approved_levels"],
                         "approval": decision, "technical_review": record["technical_review"],
                         "requested_at": decision["recorded_at"],
                         **{key: record[key] for key in ("review_package", "review_package_sha256",
                                                        "independent_check", "independent_check_sha256")}}
                previous = next((item for item in queue["requests"] if item["id"] == entry["id"]), None)
                if previous is not None:
                    if any(previous.get(key) != entry[key] for key in ("batch_id", "review_package_sha256", "config_sha256")):
                        raise RuntimeError("美术接入请求编号冲突。")
                    continue  # Preserve the consumer's status/receipts across restarts.
                queue["requests"].append(entry)
                changed = True
            if changed:
                write_json(self.runtime / "promotion-requests.json", queue)

    def _worker_step(self):
        state = self._load()
        # Idle polling must not touch Git, run checks, or start any model.
        queue = self.art_queue()
        actionable = any(item.get("status") == "queued" or item.get("status") == "awaiting_approval"
                         and item.get("decision") for item in queue["requests"])
        # State was saved before the public queue. Finish an interrupted handoff
        # through the same checked dispatch path, without replaying its model.
        statuses = {item["id"]: item.get("status") for item in queue["requests"]}
        actionable = actionable or any(statuses.get(item["id"]) != "awaiting_approval"
                                       for item in state.get("art_pending", []))
        if state["phase"] == "PLAN" and not actionable:
            result = self._snapshot(state)
        else:
            result = super()._step()
        if not result["paused"]:
            self._emit_promotions()  # Recover a crash after acceptance but before enqueue.
            if self.config.get("art_promotion") and self._calendar().allowed(self.now()):
                from .art_promotion import ArtPromotion
                result["promotion"] = ArtPromotion(self.config).step()
        result["promotions"] = self.promotion_queue()
        write_json(self.runtime / "heartbeat.json", {**result, "heartbeat_at": self.now().isoformat()})
        return result

    def step(self):
        with self._locks():
            return self._worker_step()

    def run(self, once=False):
        if once:
            return self.step()
        with self._locks():
            previous = None
            while True:
                result = self._worker_step()
                summary = (result["status"], result.get("task_id"), result.get("last_error"))
                if summary != previous:
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                    previous = summary
                if result["status"] in {"idle", "awaiting_approval", "paused", "blocked", "waiting_window",
                                         "calendar_update_required", "waiting_provider"} or result["failures"]:
                    time.sleep(self._wait_seconds(result))

    enqueue = Engine.enqueue_art
    resolve = Engine.resolve_art

    def start(self):
        return ArtWorkerManager(self.config["config_path"]).start()


class ArtWorkerManager(BackgroundManager):
    """Reuse the supervisor's process identity, heartbeat and Windows restart policy."""
    def _registration(self, logon=False):
        ArtWorker(self.config).status()  # Validate binding before registering a process.
        executable = Path(sys.executable).with_name("pythonw.exe")
        if not executable.is_file():
            executable = ROOT / ".venv" / "Scripts" / "pythonw.exe"
        if not executable.is_file():
            raise RuntimeError("独立美术后台需要 pythonw.exe。")
        arguments = subprocess.list2cmdline(["-m", "unattended.art_worker", "--config", str(self.config_path), "worker"])
        self._scheduler().register(self.name, self.description, executable, arguments, logon=logon)

    def _stop_paused_for_reload(self, identity):
        """The reload coordinator has already observed a completed paused loop."""
        self._scheduler()._owned(self.name, self.description)
        current = self.status()
        if not (self.runtime / "PAUSE").exists():
            raise RuntimeError("美术重载暂停标记已改变，拒绝停止后台。")
        if not current["process_alive"] or any((current.get("health") or {}).get(key) != identity.get(key)
                                                for key in ("pid", "process_created", "image")):
            raise RuntimeError("美术后台身份已改变，拒绝停止其他进程。")
        self._scheduler().stop(self.name, self.description)

    def _start_paused_for_reload(self):
        """Start only the art host, still paused; do not execute a controller step here."""
        self._scheduler()._owned(self.name, self.description)
        current = self.status()
        if not (self.runtime / "PAUSE").exists():
            raise RuntimeError("美术重载必须在暂停标记仍存在时启动。")
        if current["process_alive"] or current["task"] and current["task"]["state"] in (2, 4):
            raise RuntimeError("美术后台已运行或正在启动，拒绝重复启动。")
        self._registration()
        self._scheduler().run(self.name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("command", choices=("status", "step", "run", "start", "stop", "worker", "pause", "resume",
                                            "reload", "reload-status", "reload-worker"))
    parser.add_argument("--request-id")
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    args = parser.parse_args(argv)
    if args.command in {"reload", "reload-status", "reload-worker"}:
        from .art_reload import request_reload, reload_status, run_helper
        if args.command == "reload":
            result = request_reload(args.config, timeout_seconds=args.timeout_seconds)
        elif args.command == "reload-status":
            result = reload_status(args.config)
        else:
            if not args.request_id:
                parser.error("reload-worker requires --request-id")
            result = run_helper(args.config, args.request_id)
    elif args.command in {"start", "stop"}:
        result = getattr(ArtWorkerManager(args.config), args.command)()
    elif args.command == "worker":
        def execute():
            ArtWorker(load_config(args.config)).run()
            return 0
        return background_worker(args.config, run=execute)
    else:
        result = getattr(ArtWorker(load_config(args.config)), args.command)()
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
