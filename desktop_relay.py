"""Automatically hand work between GPT and DSH windows without repository access."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from relay.storage import read_json, run_lock, write_json
from windowrelay.cycle import AutoRelay
from windowrelay.direct import DirectRelay

ROOT = Path(__file__).resolve().parent
DEFAULT_MESSAGES = {"gpt": "DS做完了，请检查。", "dsh": "拉取最新的main，并完成指定任务书。"}


def load_config(path: Path) -> tuple[dict, Path]:
    config = read_json(path)
    gpt = config.get("gpt", {})
    url = urlparse(gpt.get("url", ""))
    if url.scheme != "https" or url.hostname not in {"chatgpt.com", "chat.openai.com"} or "/c/" not in url.path:
        raise ValueError("gpt.url 必须是指定 ChatGPT 对话的 HTTPS 地址。")
    if not config.get("dsh", {}).get("project", "").strip():
        raise ValueError("dsh.project 必须填写。")
    runtime = Path(config.get("runtime_dir", ".window-relay"))
    if not runtime.is_absolute():
        runtime = path.parent / runtime
    runtime = runtime.resolve()
    project_path = config.get("dsh", {}).get("project_path")
    if project_path:
        project = Path(project_path).resolve()
        if runtime == project or project in runtime.parents or ROOT == project or project in ROOT.parents:
            raise ValueError("工具和 runtime_dir 必须位于游戏/项目目录之外。")
    for target in DEFAULT_MESSAGES:
        message_for(config, target)
    auto = config.get("auto", {})
    if not isinstance(auto, dict):
        raise ValueError("auto 必须为对象。")
    interval = float(auto.get("poll_seconds", 3))
    if not 0.5 <= interval <= 60:
        raise ValueError("auto.poll_seconds 必须在 0.5 到 60 秒之间。")
    if auto.get("start_with", "gpt") not in {"gpt", "dsh"}:
        raise ValueError("auto.start_with 必须为 gpt 或 dsh。")
    return config, runtime


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def message_for(config: dict, target: str, override: str | None = None) -> str:
    messages = config.get("messages", {})
    if not isinstance(messages, dict):
        raise ValueError("messages 必须是包含 gpt / dsh 文字的对象。")
    value = override if override is not None else messages.get(target, DEFAULT_MESSAGES[target])
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"messages.{target} 必须是非空文字。")
    return value


def native():
    # Lazy import: previews do not need a Windows desktop.
    from windowrelay.windows import NativeDesktop, cursor_client, find_window, list_windows
    return NativeDesktop, cursor_client, find_window, list_windows


def sender_for(config: dict, runtime: Path):
    mode = config.get("sender_mode", "native")
    if mode == "uia":
        from windowrelay.uia_sender import UiaWindowSender
        return UiaWindowSender(config, runtime)
    if mode != "native":
        raise ValueError("sender_mode 必须为 uia 或 native。")
    if config.get("dsh", {}).get("uia", {}).get("access_mode"):
        raise ValueError("DSH 权限模式核对要求 sender_mode=uia；坐标模式不能跳过权限核对。")
    from windowrelay.sender import WindowSender
    return WindowSender(config, runtime)


def observer_for(config: dict):
    from windowrelay.observer import WindowObserver
    return WindowObserver(config)


def show_windows() -> None:
    _, _, _, list_windows = native()
    for info in list_windows():
        print(f"{info.hwnd}  {info.process_name}  {info.client_size[0]}x{info.client_size[1]}  DPI={info.dpi}  {info.title}")


def open_apps(config: dict, target: str) -> None:
    if target in {"dsh", "both"}:
        executable = Path(config["dsh"].get("executable", ""))
        if not executable.is_file():
            raise ValueError("dsh.executable 不是有效的客户端路径。")
        _, _, _, list_windows = native()
        if not any(window.process_name.casefold() == executable.name.casefold() for window in list_windows()):
            subprocess.Popen([str(executable)], cwd=executable.parent)
        print("DSH 客户端已打开或已在运行；没有创建任务卡或输入消息。")
    if target in {"gpt", "both"}:
        if not webbrowser.open(config["gpt"]["url"]):
            raise RuntimeError("无法打开系统默认浏览器，请手动打开配置的 gpt.url。")
        print("已让系统默认浏览器打开 GPT 对话；请保留一个唯一匹配的目标窗口。")


def capture_point(info, label: str, desktop, cursor_client) -> list[int]:
    input(f"\n{label}\n按 Enter 后有 5 秒把鼠标移到目标位置；脚本不会点击。")
    for remaining in range(5, 0, -1):
        if desktop.is_escape():
            raise KeyboardInterrupt
        print(f"  {remaining}", end=" ", flush=True)
        time.sleep(1)
    # Reading a coordinate must also work while the terminal has keyboard focus.
    actual = next((window for window in native()[3]() if window.hwnd == info.hwnd), None)
    if actual is None or (actual.process_name, actual.client_size, actual.dpi) != (info.process_name, info.client_size, info.dpi):
        raise ValueError("目标窗口已关闭、改变大小或缩放；本次校准未保存。")
    point = list(cursor_client(info.hwnd))
    if not (0 <= point[0] < info.client_size[0] and 0 <= point[1] < info.client_size[1]):
        raise ValueError("鼠标不在目标窗口的客户区内；本次校准未保存。")
    print(f"  已记录 {point}")
    return point


def calibrate(config: dict, path: Path, target: str, hwnd: int | None) -> None:
    NativeDesktop, cursor_client, find_window, list_windows = native()
    settings = config[target]
    desktop = NativeDesktop()
    spec = dict(settings.get("window", {}))
    if hwnd is not None:
        matches = [window for window in list_windows() if window.hwnd == hwnd]
        if len(matches) != 1:
            raise ValueError("指定的 HWND 不存在或窗口不可见，请先运行 windows。")
        info = matches[0]
        title = spec.get("title_contains", "")
        spec = {"process_name": info.process_name,
                "title_contains": title if title and title.casefold() in info.title.casefold() else info.title,
                "hwnd": hwnd}
    else:
        info = find_window(spec)
    print(f"目标：{info.process_name} / {info.title} / HWND {info.hwnd}")
    print("请保持窗口大小、缩放、侧栏位置固定。校准过程中所有点击都由你手动完成。")
    if target == "gpt":
        print(f"请先打开这个对话：{settings['url']}")
        print("请使用单独的浏览器窗口，停留在该对话底部，清空草稿，并等 GPT 回复结束。")
    else:
        print(f"目标项目：{settings['project']}。每次发消息都将重新走新任务卡入口。")
    title = input(f"窗口标题必须包含什么文字？[Enter 保留：{spec.get('title_contains', info.title)}] ").strip()
    if title:
        spec["title_contains"] = title
    spec.pop("hwnd", None)  # Handles are not stable across app restarts.
    spec["process_name"] = info.process_name
    if spec.get("title_contains", "").casefold() not in info.title.casefold():
        raise ValueError("标题文字与当前目标窗口不匹配。")
    # Require a unique durable selector before accepting any coordinates.
    selected = find_window(spec)
    if selected.hwnd != info.hwnd:
        raise ValueError("该选择条件指向了另一个窗口。")
    calibration = {
        "client_size": list(info.client_size), "dpi": info.dpi,
        "before_input": [],
    }
    if target == "dsh":
        count = int(input("从任意任务回到该项目新任务卡，需要几个鼠标步骤？（含新建按钮，1~12） "))
        if not 1 <= count <= 12:
            raise ValueError("步骤数必须在 1 到 12 之间。")
        new_task = int(input(f"第几步真正点击“新建任务”？（1~{count}） "))
        if not 1 <= new_task <= count:
            raise ValueError("新建任务步骤编号不正确。")
        print("按执行顺序校准。优先使用项目旁的新建入口；如果新会话需要选择项目，也将其列为后续步骤。")
        for index in range(1, count + 1):
            label = input(f"步骤 {index} 说明（例如：移到 ue_game 显示加号 / 点击项目新任务按钮）： ").strip() or f"步骤 {index}"
            action = input("动作 click / move（Enter=click）： ").strip() or "click"
            if action not in {"click", "move"} or (index == new_task and action != "click"):
                raise ValueError("动作必须为 click/move，且新建步骤必须为 click。")
            point = capture_point(info, label, desktop, cursor_client)
            step = {"point": point, "label": label, "action": action, "wait_seconds": 1.5}
            if index == new_task:
                step["role"] = "new_task"
            calibration["before_input"].append(step)
            input("记录完毕。请手动执行这一步，等待界面稳定后，回终端按 Enter。")
        calibration["project"] = settings["project"]
        print("现在 DSH 应处于目标项目的新空白任务卡。请核对项目标签和权限/模型，脚本不修改这些选项。")
    calibration["input"] = capture_point(info, "将鼠标移到空白消息输入区域的中间。", desktop, cursor_client)
    if input("使用鼠标点击发送按钮？y / Enter=用 Enter 键发送： ").strip().lower() == "y":
        calibration["send"] = capture_point(info, "将鼠标移到发送按钮。", desktop, cursor_client)
    else:
        calibration["send"] = None
    settings["window"] = spec
    settings["calibration"] = calibration
    if target == "gpt":
        settings["dedicated_input"] = True
    _, runtime = load_config(path)
    sender_for(config, runtime).validate(target)
    write_json(path, config)
    print(f"已保存 {target} 校准：{path}。校准没有输入或发送消息。")


def doctor(config: dict, runtime: Path, target: str | None = None, completion: bool = False) -> bool:
    checks = []
    sender = sender_for(config, runtime)
    for target in ([target] if target else ("gpt", "dsh")):
        try:
            sender.validate(target)
            checks.append({"check": target, "ok": True, "detail": "窗口和校准通过只读校验"})
            if completion:
                observation = observer_for(config).snapshot(target)
                checks.append({"check": f"{target}_completion", "ok": observation.available,
                               "busy": observation.busy, "idle": observation.idle, "detail": observation.reason})
        except (ValueError, RuntimeError, OSError) as error:
            checks.append({"check": target, "ok": False, "detail": str(error)})
    print_json({"runtime_dir": str(runtime), "checks": checks})
    return all(check["ok"] for check in checks)


def send_message(config: dict, runtime: Path, target: str, execute: bool, message: str | None = None) -> int:
    sender = sender_for(config, runtime) if execute else None
    result = DirectRelay(config, runtime, sender).send(target, message_for(config, target, message), execute=execute)
    print_json(result)
    return 2 if result["status"] in {"uncertain", "paused"} else 0


def manual(config: dict, runtime: Path, args) -> int:
    print("直接传话模式；不检查仓库，不自动判断任务完成。", flush=True)
    print("正式发送。" if args.send else "演练模式：只显示文字，不操作窗口。", flush=True)
    print("发送后，请用 Alt+Tab 回到本终端再选择下一项。", flush=True)
    while True:
        try:
            choice = input("g=通知 GPT，d=DSH 新任务卡，q=退出： ").strip().lower()
        except EOFError:
            return 0
        if choice in {"q", "quit", "exit"}:
            return 0
        if choice not in {"g", "gpt", "d", "dsh"}:
            print("请输入 g、d 或 q。", flush=True)
            continue
        target = "gpt" if choice in {"g", "gpt"} else "dsh"
        result = send_message(config, runtime, target, args.send)
        if result:
            return result


def wait_for_observation(runtime: Path, desktop, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if (runtime / "PAUSE").exists() or (desktop and desktop.is_escape()):
            runtime.mkdir(parents=True, exist_ok=True)
            (runtime / "PAUSE").touch()
            print("自动循环已暂停。", flush=True)
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(0.1, remaining))


def run(config: dict, runtime: Path, args) -> int:
    start_with = args.start_with or config.get("auto", {}).get("start_with", "gpt")
    if not args.send:
        print_json(AutoRelay(config, runtime, None, None).step(execute=False, start_with=start_with))
        return 0
    sender = sender_for(config, runtime)
    observer = observer_for(config)
    desktop = native()[0]()
    print("自动循环：GPT 完成 → DSH 新任务卡 → DSH 完成 → GPT。Ctrl+C / Esc 停止。", flush=True)
    print("通过界面和本轮完成回执交接；不检查 GitHub，不调用 Git。", flush=True)
    with run_lock(runtime / ".controller-lock"):
        if (DirectRelay(config, runtime, None).status().get("state") or {}).get("pending"):
            raise ValueError("单次发送有待确认记录，请先用 resolve 核对，不启动自动循环。")
        relay = AutoRelay(config, runtime, sender, observer)

        def record_pause() -> int:
            # Record the paused phase even when the request arrives between UI
            # checks, so resume preserves this turn and freezes its timeout.
            (runtime / "PAUSE").touch()
            result = relay.step(execute=True, start_with=start_with)
            print_json(result)
            return 2 if result["status"] == "uncertain" else 0

        previous = None
        calibration_checked = False
        desktop_waiting = False
        while True:
            if not wait_for_observation(runtime, desktop, 0):
                return record_pause()
            if not desktop.is_desktop_available():
                if not desktop_waiting:
                    print_json({"status": "waiting_desktop_unlock",
                                "reason": "Windows 已锁屏或交互桌面不可用；解锁后继续原轮次，不发送或重发消息。"})
                desktop_waiting = True
                calibration_checked = False
                if args.once:
                    return 0
                if not wait_for_observation(runtime, desktop, float(config.get("auto", {}).get("poll_seconds", 3))):
                    return record_pause()
                continue
            if desktop_waiting:
                print_json({"status": "desktop_ready", "reason": "交互桌面已恢复，继续原轮次。"})
                desktop_waiting = False
            if not calibration_checked:
                for target in ("gpt", "dsh"):
                    sender.validate(target)
                calibration_checked = True
            result = relay.step(execute=True, start_with=start_with)
            signature = json.dumps({key: result.get(key) for key in ("status", "target", "reason", "token", "stable_samples", "next_target")}, ensure_ascii=False, sort_keys=True)
            if signature != previous:
                print_json(result)
                previous = signature
            if result["status"] in {"paused", "uncertain"}:
                return 2
            if args.once:
                return 0
            if not wait_for_observation(runtime, desktop, float(config.get("auto", {}).get("poll_seconds", 3))):
                return record_pause()


def probe(config: dict, target: str, include_text: bool, diagnostic: bool = False) -> None:
    observer = observer_for(config)
    for name in (("gpt", "dsh") if target == "both" else (target,)):
        observation = observer.snapshot(name)
        result = {"target": name, "available": observation.available, "busy": observation.busy,
                  "idle": observation.idle, "blocked": observation.blocked,
                  "input_empty": not observation.editable_text.strip() if observation.available else None,
                  "draft_present": getattr(observation, "draft_present", bool(observation.editable_text.strip())),
                  "blocked_controls": list(getattr(observation, "blocked_controls", ())),
                  "reason_code": getattr(observation, "reason_code", ""), "reason": observation.reason,
                  "text_characters": len(observation.text), "buttons": list(observation.buttons)}
        if diagnostic:
            result["editor_source"] = getattr(observation, "editor_source", "")
            result["editor_state"] = getattr(observation, "editor_state", "")
            result["draft_preview"] = getattr(observation, "draft_preview", observation.editable_text[:160])
            result["editor_candidates"] = list(getattr(observation, "editor_candidates", ()))
        if include_text:
            result["text"] = observation.text
        print_json(result)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="观察 GPT / DSH 完成状态并自动循环传话")
    parser.add_argument("--config", type=Path, default=ROOT / "desktop-relay.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("windows", help="只读列出可见窗口")
    opening = sub.add_parser("open", help="打开正常浏览器/DSH 客户端，不输入消息")
    opening.add_argument("--target", choices=("gpt", "dsh", "both"), default="both")
    calibration = sub.add_parser("calibrate", help="人工示范并记录鼠标点位，不发送消息")
    calibration.add_argument("--target", choices=("gpt", "dsh"), required=True)
    calibration.add_argument("--hwnd", type=int, help="从 windows 列表选择目标窗口")
    checking = sub.add_parser("doctor", help="只读检查窗口及校准")
    checking.add_argument("--target", choices=("gpt", "dsh"))
    checking.add_argument("--completion", action="store_true", help="同时只读检查界面完成状态是否可读")
    diagnosis = sub.add_parser("probe", help="只读检查当前界面的运行/空闲/输入状态")
    diagnosis.add_argument("--target", choices=("gpt", "dsh", "both"), default="both")
    diagnosis.add_argument("--text", action="store_true", help="额外输出读取的可见对话文本")
    diagnosis.add_argument("--diagnostic", action="store_true", help="输出输入控件来源及最多160字的草稿预览，用于排查误判")
    preview = sub.add_parser("preview", help="离线显示一条将发送的消息")
    preview.add_argument("--target", choices=("gpt", "dsh"), required=True)
    preview.add_argument("--message")
    for target in ("gpt", "dsh"):
        direct = sub.add_parser(target, help="通知 GPT" if target == "gpt" else "在 DSH 指定项目新建任务卡并发送")
        direct.add_argument("--send", action="store_true", help="实际点击、输入并发送；默认只预览")
        direct.add_argument("--message", help="覆盖本次发送文字")
    loop = sub.add_parser("run", help="自动循环交接，无需选择 g / d；默认只预览")
    loop.add_argument("--send", action="store_true", help="实际点击、输入并发送")
    loop.add_argument("--start-with", choices=("gpt", "dsh"), help="仅在首次循环时选择先通知谁；重启沿用断点")
    loop.add_argument("--once", action="store_true", help="只运行一个状态检查步骤")
    menu = sub.add_parser("manual", help="可选手动 g / d 菜单")
    menu.add_argument("--send", action="store_true")
    background = sub.add_parser("background", help="独立后台运行、停止或检查传话器")
    background.add_argument("action", choices=("start", "status", "stop"))
    sub.add_parser("status", help="显示自动循环、单次发送记录和暂停标记")
    sub.add_parser("pause", help="暂停后续窗口操作")
    sub.add_parser("resume", help="清除暂停；不会自动运行或重发")
    resolve = sub.add_parser("resolve", help="核对窗口后处理一次不确定的发送")
    choice = resolve.add_mutually_exclusive_group(required=True)
    choice.add_argument("--sent", action="store_true", help="人工确认该消息已经发出")
    choice.add_argument("--skip", action="store_true", help="跳过本次中断；不会自动重发")
    resolve.add_argument("--mode", choices=("auto", "direct"), help="默认自动选择有待确认记录的模式")
    resetting = sub.add_parser("reset-cycle", help="暂停/终止后，人工明确重新开始一轮；不会发消息")
    resetting.add_argument("--start-with", choices=("gpt", "dsh"), default="gpt")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        if args.command == "windows":
            show_windows()
            return 0
        path = args.config.expanduser().resolve()
        config, runtime = load_config(path)
        if args.command == "open":
            open_apps(config, args.target)
        elif args.command == "calibrate":
            calibrate(config, path, args.target, args.hwnd)
        elif args.command == "doctor":
            return 0 if doctor(config, runtime, args.target, args.completion) else 1
        elif args.command == "probe":
            probe(config, args.target, args.text, args.diagnostic)
        elif args.command == "preview":
            print(message_for(config, args.target, args.message))
        elif args.command in {"gpt", "dsh"}:
            return send_message(config, runtime, args.command, args.send, args.message)
        elif args.command == "run":
            return run(config, runtime, args)
        elif args.command == "manual":
            return manual(config, runtime, args)
        elif args.command == "background":
            from desktop_background import main as background_main
            return background_main(["--config", str(path), args.action])
        elif args.command == "status":
            print_json({"auto": AutoRelay(config, runtime, None, None).status(),
                        "direct": DirectRelay(config, runtime, None).status()})
        elif args.command == "pause":
            runtime.mkdir(parents=True, exist_ok=True)
            (runtime / "PAUSE").touch()
            print("暂停标记已写入；正在发送的步骤将在下一个检查点停止。")
        elif args.command == "resume":
            with run_lock(runtime / ".controller-lock"):
                state = DirectRelay(config, runtime, None).status().get("state") or {}
                cycle = AutoRelay(config, runtime, None, None)
                cycle_state = cycle.status().get("state") or {}
                if state.get("pending") or cycle_state.get("pending") or cycle_state.get("phase") == "uncertain":
                    raise ValueError("存在不确定发送；请先核对窗口并运行 resolve --sent 或 --skip。")
                if cycle_state.get("phase") == "paused":
                    print_json(cycle.resume())
                (runtime / "PAUSE").unlink(missing_ok=True)
                print("已解除暂停；没有输入或重发消息。")
        elif args.command == "resolve":
            cycle = AutoRelay(config, runtime, None, None)
            cycle_state = cycle.status().get("state") or {}
            direct = DirectRelay(config, runtime, None)
            direct_state = direct.status().get("state") or {}
            auto_pending = bool(cycle_state.get("pending") or cycle_state.get("phase") == "uncertain")
            if args.mode is None and auto_pending and direct_state.get("pending"):
                raise ValueError("两种模式都有待确认记录，请指定 --mode auto 或 --mode direct。")
            if args.mode == "auto" or (args.mode is None and auto_pending):
                with run_lock(runtime / ".controller-lock"):
                    print_json(cycle.resolve(sent=args.sent))
            else:
                print_json(direct.resolve(sent=args.sent))
        elif args.command == "reset-cycle":
            with run_lock(runtime / ".controller-lock"):
                print_json(AutoRelay(config, runtime, None, None).reset(start_with=args.start_with))
        return 0
    except KeyboardInterrupt:
        print("\n已停止。若在发送中被打断，请用 status 检查 pending 后人工核对。", flush=True)
        return 130
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as error:
        print(f"错误：{error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    # UI Automation can return characters outside the Windows console code page.
    # Keep redirected background logs readable and never fail on those labels.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
