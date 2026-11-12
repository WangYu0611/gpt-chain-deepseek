"""Command line entry point for the single-host Codex/DSH loop."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _config_argument(parser):
    parser.add_argument("--config", default=argparse.SUPPRESS,
                        help="配置文件，默认 unattended.local.json")


def _positive_tasks(value):
    try:
        count = int(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("任务数量必须为正整数") from error
    if count < 1:
        raise argparse.ArgumentTypeError("任务数量必须为正整数")
    return count


def parser():
    root = argparse.ArgumentParser(description="Codex 派工验收 / dsh 编码提交：单机串行无人值守")
    _config_argument(root)
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="为一个现有 Git 项目创建配置，不启动模型")
    _config_argument(init)
    init.add_argument("--project", required=True, type=Path)
    goal = init.add_mutually_exclusive_group(required=True)
    goal.add_argument("--goal", help="项目开发目标及完成条件")
    goal.add_argument("--goal-file", type=Path, help="包含开发目标的 UTF-8 文本文件")
    for command, help_text in (
        ("doctor", "检查配置、Git 和模型运行入口"),
        ("status", "查看当前任务、断点及工作时间"),
        ("run", "按工作时间持续执行闭环"),
        ("pause", "保存暂停请求"),
        ("resume", "解除暂停，保持原任务断点"),
        ("retry", "重试失败或阻塞步骤，保持原任务编号"),
    ):
        child = commands.add_parser(command, help=help_text)
        _config_argument(child)
        if command == "retry":
            recovery = child.add_mutually_exclusive_group()
            recovery.add_argument("--rework", action="store_true", help="按最新验收回执显式恢复同任务返工，可解除验收阻塞")
            recovery.add_argument("--external-validation", action="store_true", help="为已有受审候选提交恢复任务级外层补证，保留编码会话")
            recovery.add_argument("--replan", action="store_true", help="将当前功能任务交给 Claude 关键规划重新分析，保留原验收条件与编码会话")
            child.add_argument("--new-session", action="store_true", help="配合 --rework：归档旧会话并用新 DSH 会话返工")
        if command == "run":
            run_mode = child.add_mutually_exclusive_group()
            run_mode.add_argument("--once", action="store_true", help="只执行一个状态步骤")
            run_mode.add_argument("--tasks", type=_positive_tasks, help="本批最多验收指定数量的任务后停止，重启继续原批次")
            child.add_argument("--ignore-window", action="store_true",
                               help="仅配合 --tasks：本批临时忽略工作窗口，保留原时间配置")
    demo = commands.add_parser("demo", help="用假模型、真实 Git 和独立检查离线验证闭环")
    demo.add_argument("--output", required=True, type=Path, help="尚不存在的演示输出目录")
    defer = commands.add_parser("defer", help="按明确指示将当前验收阻塞项记为待人工确认，继续其他开发")
    _config_argument(defer)
    defer.add_argument("--reason", required=True, help="延期原因及用户确认范围，不将延期记为验收通过")
    repair = commands.add_parser("repair", help="按显式用户授权为当前同任务启用 Opus 修复和固定全流程验收")
    _config_argument(repair)
    repair.add_argument("--request", type=Path, required=True)
    dashboard = commands.add_parser("dashboard", help="打开自动刷新的本地运行面板")
    _config_argument(dashboard)
    dashboard.add_argument("--port", type=int, default=8765, help="本机监听端口，默认 8765")
    dashboard.add_argument("--no-open", action="store_true", help="启动面板服务，不自动打开浏览器")
    dashboard.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    background = commands.add_parser("background", help="Windows 后台运行与登录启动")
    _config_argument(background)
    actions = background.add_subparsers(dest="background_action", required=True)
    for action in ("start", "stop", "status", "install", "uninstall", "worker", "reload", "reload-status", "reload-worker"):
        child = actions.add_parser(action, help=argparse.SUPPRESS if action in {"worker", "reload-worker"} else None)
        _config_argument(child)
        if action == "reload":
            child.add_argument("--timeout-seconds", type=int, default=7200, help="等待当前步骤自然结束的上限，30..86400秒")
        elif action == "reload-worker":
            child.add_argument("--request-id", required=True)
    art = commands.add_parser("art", help="管理独立美术队列与视觉验收，不切换功能开发任务")
    _config_argument(art)
    art_actions = art.add_subparsers(dest="art_action", required=True)
    for action in ("status", "start", "stop", "pause", "resume", "enqueue", "resolve"):
        child = art_actions.add_parser(action)
        _config_argument(child)
        if action == "enqueue":
            child.add_argument("--task", required=True)
            child.add_argument("--title")
            child.add_argument("--body")
        elif action == "resolve":
            child.add_argument("--task", required=True)
            child.add_argument("--decision", choices=("accepted", "rework"), required=True)
            child.add_argument("--feedback", required=True)
            child.add_argument("--review-package-sha256", required=True)
    return root


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str), flush=True)


def main(argv=None):
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    if args.command == "run" and args.ignore_window and args.tasks is None:
        argument_parser.error("--ignore-window 必须配合 --tasks 使用")
    if args.command == "retry" and args.new_session and not args.rework:
        argument_parser.error("--new-session 必须配合 --rework 使用")
    config_path = Path(getattr(args, "config", "unattended.local.json")).expanduser().resolve()
    try:
        if args.command == "demo":
            from .demo import run_demo
            value = run_demo(args.output)
        elif args.command == "dashboard":
            if not 1 <= args.port <= 65535:
                raise ValueError("面板端口必须为 1..65535。")
            from .dashboard import DashboardManager, serve
            if args.serve:
                serve(config_path, args.port)
                return 0
            value = DashboardManager(config_path, args.port).start(open_browser=not args.no_open)
        elif args.command == "background":
            from .background import BackgroundManager, worker
            if args.background_action == "worker":
                return worker(config_path)
            if args.background_action in {"reload", "reload-status", "reload-worker"}:
                from .background_reload import request_reload, reload_status, run_helper
                if args.background_action == "reload":
                    value = request_reload(config_path, timeout_seconds=args.timeout_seconds)
                elif args.background_action == "reload-status":
                    value = reload_status(config_path)
                else:
                    value = run_helper(config_path, args.request_id)
            else:
                value = getattr(BackgroundManager(config_path), args.background_action)()
        elif args.command == "art":
            from .config import load_config
            from .art_worker import ArtWorker, ArtWorkerManager
            worker = ArtWorker.from_main_config(load_config(config_path))
            if args.art_action in {"start", "stop"}:
                value = getattr(ArtWorkerManager(Path(worker.config["config_path"])), args.art_action)()
            elif args.art_action == "enqueue":
                options = {key: getattr(args, key) for key in ("title", "body") if getattr(args, key) is not None}
                value = worker.enqueue_art(args.task, **options)
            elif args.art_action == "resolve":
                value = worker.resolve_art(args.task, args.decision, args.feedback, args.review_package_sha256)
            else:
                value = getattr(worker, args.art_action)()
        else:
            from .config import doctor, init_config, load_config
            if args.command == "init":
                goal = args.goal_file.read_text(encoding="utf-8-sig") if args.goal_file else args.goal
                value = init_config(config_path, args.project, goal)
            else:
                config = load_config(config_path)
                if args.command == "doctor":
                    value = doctor(config)
                elif args.command == "run" and args.tasks is not None:
                    from .burst import run_burst
                    value = run_burst(config, tasks=args.tasks, ignore_window=args.ignore_window)
                else:
                    from .engine import Engine
                    engine = Engine(config)
                    if args.command == "retry" and args.rework:
                        value = engine.retry(rework=args.rework, new_session=args.new_session)
                    elif args.command == "retry" and args.external_validation:
                        value = engine.retry(external_validation=True)
                    elif args.command == "retry" and args.replan:
                        value = engine.retry(replan=True)
                    elif args.command == "defer":
                        value = engine.defer(args.reason)
                    elif args.command == "repair":
                        value = engine.activate_user_repair(args.request)
                    else:
                        value = engine.run(once=args.once) if args.command == "run" else getattr(engine, args.command)()
        _print(value)
        status = str(value.get("status", "")).casefold() if isinstance(value, dict) else ""
        if status == "interrupted":
            return 130
        unhealthy = isinstance(value, dict) and (value.get("ready") is False or value.get("ok") is False)
        failed_action = args.command != "status" and status in {"error", "failed", "blocked", "needs_setup", "start_timeout"}
        return 1 if unhealthy or failed_action else 0
    except KeyboardInterrupt:
        _print({"status": "interrupted", "message": "进度已保留，下次运行从原断点继续。"})
        return 130
    except Exception as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr, flush=True)
        return 1
