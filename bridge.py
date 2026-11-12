#!/usr/bin/env python3
"""CLI entry point. Running without a subcommand never starts automation."""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path

from relay.config import load_config, missing_settings
from relay.engine import Relay
from relay.prompts import execution_prompt, review_prompt
from relay.storage import read_json, run_lock, write_json


ROOT = Path(__file__).resolve().parent


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="网页 GPT ↔ DSH 自动传话器（默认不启动真实任务）")
    cli.add_argument("--config", type=Path, default=ROOT / "config.json", help="配置文件路径")
    sub = cli.add_subparsers(dest="command")
    sub.add_parser("init", help="生成配置，不启动浏览器或任务")
    sub.add_parser("doctor", help="检查本机配置和依赖，不访问仓库或启动 DSH")
    sub.add_parser("login", help="打开独立浏览器并手动登录，不发送消息")
    seed = sub.add_parser("seed", help="登记 DSH 已完成且已推送的交付，准备 GPT 审阅")
    seed.add_argument("--delivery", type=Path, required=True, help="交付 JSON（参考 delivery.example.json）")
    seed.add_argument("--replace-state", action="store_true", help="明确以本次交付替换当前状态（先备份旧状态）")
    run = sub.add_parser("run", help="按当前状态运行或恢复循环")
    run.add_argument("--once", action="store_true", help="完成一次 GPT 审阅和 DSH 执行后停下")
    run.add_argument("--dry-run", action="store_true", help="离线打印交接示例，不调用浏览器、Git 或 DSH")
    run.add_argument("--external-browser", action="store_true", help="本轮通过已连接浏览器助手传话；请求和回复保存在 runtime/external，需要助手处理")
    sub.add_parser("status", help="查看当前阶段和最后错误")
    sub.add_parser("pause", help="在下一次交接前暂停；立刻中止当前任务请在运行窗口 Ctrl+C")
    sub.add_parser("resume", help="清除暂停标记，不自动启动任务")
    retry = sub.add_parser("retry-review", help="人工确认网页未发送后，允许同一编号重试一次")
    retry.add_argument("--confirm-not-sent", action="store_true", required=True)
    return cli


def dry_run(config: dict) -> None:
    delivery = {"task_id": "DEMO-001", "commit_sha": "1" * 40, "report_paths": ["Docs/Reports/DEMO-001.md"], "screenshot_paths": ["Docs/Evidence/demo.png"]}
    print("离线演练：没有访问 GitHub，没有打开浏览器，没有启动 DSH。\n")
    print("1. DSH 完成交付 → 网页 GPT 收到：\n")
    print(review_prompt(config, delivery, "demo-review"))
    print("\n2. GPT 发布任务后 → DSH 收到：\n")
    task = {"action": "execute", "task_id": "DEMO-002", "task_path": "Docs/Tasks/DEMO-002.md", "run_id": "demo-task"}
    print(execution_prompt(config, task, "2" * 40))
    print("\n3. DSH 完成后保存状态，回到第 1 步。")


def doctor(config: dict) -> int:
    errors = missing_settings(config)
    from relay.browser import validate_conversation_url
    if config["browser"].get("conversation_url"):
        try:
            validate_conversation_url(config["browser"]["conversation_url"])
        except Exception as error:
            errors.append(str(error))
    for label, command in (("Git", config["repository"].get("git_executable", "git")), ("DSH CLI", config["dsh"].get("executable", "dsh"))):
        present = bool(shutil.which(command) or Path(command).is_file())
        print(f"{'OK' if present else '缺少'} {label}: {command}")
        if not present:
            errors.append(f"找不到 {label}")
    if Path(config["dsh"].get("executable", "")).name.lower() == "deepseek harness.exe":
        errors.append("dsh.executable 是桌面程序，请改为安装目录 resources/runtime/cli/bin/dsh.cmd。")
    patch = config["dsh"].get("patch")
    if patch and not Path(patch).is_file():
        errors.append("DSH patch 文件不存在")
    if importlib.util.find_spec("playwright") is None:
        errors.append("当前 Python 未安装 Playwright，请运行 setup.ps1")
    print(f"项目目录：{config['repository']['workdir']}")
    workdir = Path(config["repository"]["workdir"])
    if workdir.exists() and not (workdir / ".git").exists():
        errors.append("项目目录已存在，但未发现 .git，请确认工作区路径")
    print(f"GPT 对话：{config['browser'].get('conversation_url')}")
    print(f"运行数据：{config['runtime_dir']}")
    print("网页选择器、账号登录、GitHub 读写能力和 DSH 模型权限需实际使用时验证。")
    for error in errors:
        print(f"待处理：{error}")
    print("检查完成；未访问项目仓库，未启动浏览器或 DSH。")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    if not args.command:
        cli.print_help()
        return 0
    try:
        if args.command == "init":
            if args.config.exists():
                raise RuntimeError("配置已存在，不会覆盖。请直接编辑现有配置。")
            args.config.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / "config.example.json", args.config)
            print(f"已生成 {args.config.resolve()}，编辑后运行 doctor。")
            return 0
        config = load_config(args.config)
        if args.command == "doctor":
            return doctor(config)
        if args.command == "run" and args.dry_run:
            dry_run(config)
            return 0
        runtime = Path(config["runtime_dir"])
        if args.command == "status":
            state = runtime / "state.json"
            print(json.dumps(read_json(state), ensure_ascii=False, indent=2) if state.exists() else "尚未登记交付。")
            return 0
        if args.command in ("pause", "resume"):
            runtime.mkdir(parents=True, exist_ok=True)
            marker = runtime / "PAUSE"
            if args.command == "pause":
                marker.touch()
                print("已要求在下一次交接前暂停。正在运行的 DSH 如需立刻停止，请在运行窗口 Ctrl+C。")
            else:
                marker.unlink(missing_ok=True)
                print("暂停标记已清除。运行 run 继续；不确定的 DSH 执行仍需人工核对。")
            return 0
        missing = missing_settings(config)
        if missing:
            raise RuntimeError("请先配置：" + "、".join(missing))
        with run_lock(runtime):
            if args.command == "login":
                from relay.browser import ChatGPTBrowser
                with ChatGPTBrowser(config["browser"], runtime) as browser:
                    browser.login()
                return 0
            relay = Relay(config)
            if args.command == "seed":
                if args.replace_state and relay.state_path.exists():
                    backup = runtime / "state-before-reseed.json"
                    write_json(backup, relay.state)
                relay.seed(read_json(args.delivery), replace=args.replace_state)
                print("已登记交付。运行 run --once 开始首轮审阅和执行。")
            elif args.command == "retry-review":
                relay.retry_review()
                print("已允许同一交接编号重试一次。请运行 run。")
            elif args.command == "run":
                from relay.browser import ChatGPTBrowser
                from relay.dsh import DshRunner
                # Lazy opening: no browser when state is done or DSH recovery is uncertain.
                if args.external_browser:
                    from relay.external_browser import ExternalBrowser
                    browser = ExternalBrowser(config["browser"], runtime)
                else:
                    browser = ChatGPTBrowser(config["browser"], runtime)
                relay.browser = browser
                relay.runner = DshRunner(config["dsh"], runtime)
                try:
                    relay.run(once=args.once)
                finally:
                    browser.close()
        return 0
    except KeyboardInterrupt:
        print("\n已中断。状态已保留；DSH 是否已交付请先核对日志。", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"已暂停：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
