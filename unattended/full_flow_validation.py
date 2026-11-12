"""Run a user-requested, isolated whole-flow test with native Claude Opus.

This operator job does not grant a rework, change the coding model, or accept a
game task. The original workspace stays paused while a fixed commit is tested.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys
import tempfile
import uuid

from github_harness_lab.lab_harness.process import run_process
from . import gitops, ue_checks
from .config import load_config
from .full_flow_native import parse_full_flow_native
from .providers import (_claude_command, _claude_environment, _claude_log_record,
                        _claude_model_evidence, _claude_turn, claude_subscription_ready)
from .storage import lock, read_json, write_json, write_text

ROOT = Path(__file__).resolve().parent.parent
MODEL = "claude-opus-5-5"
EFFORT = "high"
STAGES = ("prepare", "build", "regression", "visual", "package", "retest", "status")


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical_hash(request):
    value = {key: value for key, value in request.items() if key != "request_sha256"}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _task_output_directory(source, session):
    uuid.UUID(session)
    project_key = re.sub(r"[^A-Za-z0-9]", "-", str(source))
    return Path(tempfile.gettempdir()) / "claude" / project_key / session / "tasks"


def _request(path):
    path = Path(path).resolve(strict=True)
    value = read_json(path)
    ident = value.get("id", "")
    if not re.fullmatch(r"[0-9a-f]{32}", ident):
        raise ValueError("Invalid full-flow request identity")
    runtime = Path(value["runtime_dir"]).resolve(strict=True)
    run = runtime / "full-flow" / "runs" / ident
    if (path != run / "request.json" or Path(value["run_dir"]).resolve() != run or
            Path(value["project_dir"]).resolve() != run / "project" or
            Path(value["source_workspace"]).resolve() != runtime / "workspace" or
            value.get("model") != MODEL or value.get("effort") != EFFORT or
            value.get("request_sha256") != canonical_hash(value)):
        raise ValueError("Full-flow request binding changed")
    return value, run


def _status(request, run, **changes):
    path = run / "status.json"
    value = read_json(path) if path.exists() else {}
    value.update(version=1, id=request["id"], request_sha256=request["request_sha256"],
                 task_id=request["task_id"], head_sha=request["head_sha"], model=MODEL,
                 effort=EFFORT, updated_at=now(), **changes)
    write_json(path, value)


def create(config_path):
    config = load_config(config_path)
    runtime = Path(config["runtime_dir"]).resolve()
    base = runtime / "full-flow"
    with lock(base / "operator.lock"):
        state = read_json(runtime / "state.json")
        task = state.get("task") or {}
        source = runtime / "workspace"
        if state.get("phase") != "BLOCKED" or state.get("operation"):
            raise RuntimeError("Wait for a blocked, idle main task before this operator test")
        if not task.get("id") or not task.get("session_id") or not gitops.clean(source):
            raise RuntimeError("An intact task/session and clean candidate are required")
        current = base / "current.json"
        if current.exists():
            old, old_run = _request(runtime / read_json(current)["request_path"])
            if read_json(old_run / "status.json").get("status") not in {"completed", "failed"}:
                raise RuntimeError("A full-flow test is already pending")
        ident = uuid.uuid4().hex
        run = base / "runs" / ident
        run.mkdir(parents=True)
        pause = runtime / "PAUSE"
        token = "full-flow-verification:" + ident + "\n"
        owned = not pause.exists()
        if owned:
            with pause.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(token)
        request = {"version": 1, "id": ident, "task_id": task["id"],
                   "head_sha": gitops.head(source), "source_workspace": str(source),
                   "runtime_dir": str(runtime), "run_dir": str(run),
                   "project_dir": str(run / "project"),
                   "engine_dir": "C:/Program Files/Epic Games/UE_5.8/Engine",
                   "authorized_at": now(), "model": MODEL, "effort": EFFORT}
        request["request_sha256"] = canonical_hash(request)
        write_json(run / "request.json", request)
        write_json(run / "operator.json", {"user_request": "用opus5.5把全流程测一次",
                   "source_session": task["session_id"], "source_state_sha256": file_hash(runtime / "state.json"),
                   "source_reworks": task["reworks"], "owns_pause": owned,
                   "pause_token": token if owned else None,
                   "pause_sha256": file_hash(pause), "pause_mtime_ns": pause.stat().st_mtime_ns,
                   "authorized_at": request["authorized_at"]})
        _status(request, run, status="planning", stage="plan", summary="Opus 5.5 正在核对完整测试范围。",
                output_dir=str(run / "native" / "plan"))
        write_json(current, {"version": 1, "id": ident,
                   "request_path": (Path("full-flow") / "runs" / ident / "request.json").as_posix(),
                   "request_sha256": request["request_sha256"],
                   "request_file_sha256": file_hash(run / "request.json")})
        return {"request": str(run / "request.json"), "status": "created", "model": MODEL,
                "head_sha": request["head_sha"]}


def invoke(config_path, request_path, phase):
    config = load_config(config_path)
    request, run = _request(request_path)
    runtime = Path(request["runtime_dir"])
    if Path(config["runtime_dir"]).resolve() != runtime:
        raise ValueError("Config belongs to another runtime")
    source = Path(request["source_workspace"])
    operator = read_json(run / "operator.json")
    state = read_json(runtime / "state.json")
    if (gitops.head(source) != request["head_sha"] or not gitops.clean(source) or
            (state.get("task") or {}).get("session_id") != operator["source_session"]):
        raise RuntimeError("The original task, session, or candidate changed")
    settings = config["claude"]
    if not claude_subscription_ready(settings, source):
        raise RuntimeError("Native Claude subscription login is unavailable")
    output = run / "native" / phase
    if phase == "analysis" and output.exists():
        output = next((run / "native" / f"analysis-{number}" for number in range(2, 10)
                       if not (run / "native" / f"analysis-{number}").exists()), None)
        if output is None:
            raise RuntimeError("Full-flow continuation attempts are exhausted")
    output.mkdir(parents=True, exist_ok=False)
    documents = [source, runtime / "tasks" / request["task_id"], run]
    if phase != "plan":
        plan = read_json(run / "native" / "plan" / "answer.json")
        task_outputs = _task_output_directory(source, plan["session_id"])
        task_outputs.mkdir(parents=True, exist_ok=True)
        documents.extend([ROOT / "unattended", Path(sys.executable).resolve().parent])
        documents.append(task_outputs)
        engine_sources = Path(request["engine_dir"]) / "Source" / "Programs" / "UnrealBuildTool"
        if engine_sources.is_dir():
            documents.append(engine_sources)
    argv = _claude_command(settings, read_only_dirs=documents)
    argv += ["--print", "--output-format", "stream-json", "--verbose",
             "--permission-mode", "plan" if phase == "plan" else "default",
             "--permission-prompts", "none", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
             "--tools", "Read,Glob,Grep" if phase == "plan" else "Bash,Read,Glob,Grep",
             "--disallowedTools", "mcp__*", "Edit", "Write"]
    allowed_tools = ["Read", "Glob", "Grep"]
    common = (
        "用户最新明确要求：\"用opus5.5把全流程测一次\"。你是本次真实测试负责人，模型为Opus5.5、思考high。\n"
        "这是一次完整测试，覆盖当前工程已实现流程及连带影响，不限于原来五项核心自验；不是要求宣称整个未完成的M3已完成。"
        "由你独立判断测试覆盖、读取源码与真实回执，给出你自己的诊断，转达者的意见不作为结论。\n"
        "本次仅测试：不改游戏源文件、定义或测试断言，不编码、不commit/push，不改变主任务的验收状态，不调用美术/付费API。"
        "不终止用户已打开的UE编辑器或其他会话。主任务原代码/会话保留。未知或失败如实记录，不把退出码0当作测试全过。\n"
        f"固定候选HEAD：{request['head_sha']}；原工程：{source.as_posix()}；本次目录：{run.as_posix()}。\n"
        f"任务与各轮独立审核：{(runtime / 'tasks' / request['task_id']).as_posix()}。\n"
        "第8、9轮review已accepted，但控制器把一个原约定非门槛的额外打包环境FAIL聚合成了返工；请自行核实。"
        "这项控制器问题由维护者另行修复。测试不要修改历史回执、替代Codex独立审核或人工验收。\n"
    )
    if phase == "plan":
        argv += ["--resume", operator["source_session"], "--fork-session"]
        prompt = common + (
            "现在先只读检查现状，自主给出这次全流程测试方案、覆盖范围、已知风险和结果判定规则。"
            "下一次调用会给你隔离候选副本和固定测试工具，由你实际执行。工具将支持独立构建、"
            "全部ZhuGuoCity.M自动回归、真实RHI必要玩法/界面验证、精确PackageIntegration打包和结果查询。"
            "可提出你认为需要的其他验证，不能用文档检查冒充运行证据；不要声称现在已重测。"
            "中文简明输出计划，并明确哪些历史人工待办无法由自动测试确认。"
        )
        status, stage, summary, timeout = "planning", "plan", "Opus 5.5 正在独立核对全流程测试方案。", 1800
    else:
        argv += ["--resume", plan["session_id"]]
        helper = ROOT / "unattended" / "full_flow_checks.py"
        commands = [shlex.join([Path(sys.executable).as_posix(), helper.as_posix(),
                               "--request", Path(request_path).resolve().as_posix(), "--stage", name])
                    for name in STAGES]
        retry_command = shlex.join([Path(sys.executable).as_posix(), helper.as_posix(),
                                   "--request", Path(request_path).resolve().as_posix(),
                                   "--stage", "package", "--retry"])
        allowed_tools.extend("Bash(" + command + ")" for command in commands)
        allowed_tools.append("Bash(" + retry_command + ")")
        supplemental = ""
        if (run / "operator-short-package.json").exists():
            authorization = read_json(run / "operator-short-package.json")
            short_helper = ROOT / "unattended" / "full_flow_short_package.py"
            if (any(authorization.get(key) != request[key] for key in
                    ("id", "task_id", "head_sha", "request_sha256")) or
                    authorization.get("runner_sha256") != file_hash(short_helper)):
                raise RuntimeError("Supplemental short-package authorization changed")
            short_command = shlex.join([Path(sys.executable).as_posix(), short_helper.as_posix(),
                                       "--request", Path(request_path).resolve().as_posix()])
            allowed_tools.append("Bash(" + short_command + ")")
            supplemental = ("\n打包首轮的真实错误是隔离路径超过260字符，维护者已提供短路径环境补测工具。"
                "它保持同HEAD/逐文件输入/正式PackageIntegration不变，并保留原失败；新证据归档checks/package-short。"
                "按你原测试方案，请用Bash后台执行下面这个精确命令一次并等完成通知，核对新产物后更新最终报告；"
                "不要因此重复已经完成的回归、画面或retest：\n" + short_command + "\n")
        if (run / "operator-retest-recovery.json").exists():
            recovery = read_json(run / "operator-retest-recovery.json")
            recovery_helper = ROOT / "unattended" / "full_flow_retest_resume.py"
            if (any(recovery.get(key) != request[key] for key in
                    ("id", "task_id", "head_sha", "request_sha256")) or
                    recovery.get("resume_runner_sha256") != file_hash(recovery_helper)):
                raise RuntimeError("Interrupted retest recovery authorization changed")
            recovery_command = shlex.join([Path(sys.executable).as_posix(), recovery_helper.as_posix(),
                                          "--request", Path(request_path).resolve().as_posix()])
            allowed_tools.append("Bash(" + recovery_command + ")")
            supplemental += ("\n最新执行安排：短路径打包已经实际执行，读取它的回执即可，不再提交。"
                "原retest因Bash后台默认600秒时限被原生系统停止：只有前30项完成，第31项有真实中断证据，32/33未启动。"
                "维护者已签发operator-retest-recovery.json，严格绑定原生killed事件、原检查点和前30项哈希。"
                "现在只调用下列恢复工具：保留30项，单独重新执行明确中断的31，并执行未开始的32/33；不重跑其他阶段。"
                "必须Bash run_in_background=true并显式timeout=3600000，后台同样受默认时限约束；等通知后读回执。\n"
                + recovery_command + "\n之后按实际结果更新完整报告，区分首次失败、重测、工具误报和环境阻断。")
        prompt = common + (
            ("本次已启用Bash后台任务。按本机CLI官方内置说明，后台命令完成会自动发送一次通知；"
             "TaskOutput已移除，Monitor用于新建事件监听，不是等待已有任务。请等完成通知后用Read读取通知的output_file和回执。"
             "保留原有prepare和所有真实回执，仅接续未完成阶段，不重跑已完成阶段。\n" if phase == "analysis" else "") +
            "此前retest的工具哈希阻断已由维护者提供的operator-runner-migration.json精确解决；"
            "原binding和原FAIL未改。retest会依据原始日志单列RHI解析误报校核，其他真实失败仍须按你原方案实际复测一次。\n"
            "继续你刚才制定的方案，现在请实际执行一次全流程测试。固定工具会在本次目录导出同HEAD隔离副本，"
            "不修改原工作区；机器上的全局LiveCoding占用以实际环境日志为准，隔离副本不能保证消除全局锁。"
            "所有阶段原始日志和结构化回执都留在checks/下。\n"
            "你可用Bash原样调用下列工具。prepare/status可前台执行；build/regression/visual/package必须"
            "使用Bash的run_in_background=true启动，等原生完成通知后用Read读取通知里的output_file，再读对应receipt，"
            "所有长命令均显式设置timeout=3600000，不能省略而触发默认600秒后台期限。"
            "确认passed或failed后再下一阶段。后台输出目录已单独授权只读。等待时不要反复轮询、独立sleep或调用不存在的TaskOutput。"
            "不要以Bash的600秒前台上限截断长测试。"
            "各阶段串行执行，避免并发占用UE构建/打包资源；不要重复发起仍在运行的同一命令。"
            "先prepare再build，其余阶段由你按真实依赖安排。regression只运行全部M系列；visual逐项正常RHI运行19项Local画面测试，"
            "外加3项M3逻辑/控件测试，后者不能当作画面证据。retest仅从本批首轮regression/visual真实失败集合逐项再测一次，保留两轮结果。"
            "全量regression含PackageIntegration的Success可能只是skip，"
            "因此必须执行package阶段，让工具验证本次真实打包摘要；visual需正常RHI实际运行，不能用NullRHI结果代替。"
            "一次完整验证要覆盖每个阶段；失败不等于停止所有其他有意义的独立验证。不要修改测试以求通过。\n"
            + "\n".join(commands) + "\n"
            "如果首次精确打包失败，可自行判断是否有意义，仅允许一次保留首轮日志的重试：\n"
            + retry_command + "\n"
            "每阶段回执和run/facts.json可用Read读取；工具stage status返回当前事实。"
            "原工程Saved基线在run/source-saved-before.json（若存在），由维护者收尾核对；截图用Read逐张实际打开，不能只看文件存在。"
            "如你需要其他验证但工具未覆盖，列明确切缺口，不猜测为PASS。不要自己运行任意shell、关进程或改脚本。"
            "结束时用中文给出：实际通过/失败/未运行项目与数量，核心玩法是否稳定，环境问题与功能缺陷分开，"
            "证据路径，以及你自己认为反复返工的原因及建议。结论必须基于本次运行，不把历史成功当新测试。"
            "不要给自己无限重试，也不要代替独立审核宣布主任务通过。"
            + supplemental
        )
        status, stage, summary, timeout = "testing", "prepare", "Opus 5.5 正在执行隔离全流程测试。", 25200
    argv += ["--allowedTools", *allowed_tools, "--model", MODEL, "--effort", EFFORT]
    write_text(output / "prompt.txt", prompt)
    write_json(output / "invocation.json", {"model": MODEL, "effort": EFFORT, "phase": phase,
               "request_sha256": request["request_sha256"], "head_sha": request["head_sha"],
               "timeout_seconds": timeout, "started_at": now(), "read_only_roots": [str(p) for p in documents],
               "orchestrator_sha256": file_hash(Path(__file__)),
               "checks_sha256": file_hash(ROOT / "unattended" / "full_flow_checks.py"),
               "native_parser_sha256": file_hash(ROOT / "unattended" / "full_flow_native.py"),
               "background_tasks_enabled": phase != "plan"})
    _status(request, run, status=status, stage=stage, summary=summary, output_dir=str(output))
    print(json.dumps({"status": status, "output": str(output), "model": MODEL}, ensure_ascii=True), flush=True)
    with lock(run / "native.lock"):
        try:
            environment = _claude_environment()
            if phase != "plan":
                # Ordinary code calls keep the provider's no-background policy.
                # This whole-flow job explicitly waits for each owned long task.
                environment.pop("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", None)
            result = run_process(argv, cwd=source, output_dir=output, timeout_seconds=timeout,
                                 environment=environment, stdin=prompt.encode("utf-8"),
                                 max_output_bytes=64 * 1024 * 1024, stdout_record_filter=_claude_log_record)
            write_json(output / "process.json", {"exit_code": result.exit_code, "timed_out": result.timed_out,
                       "output_limit_exceeded": result.output_limit_exceeded, "finished_at": now()})
            if result.exit_code or result.timed_out or result.output_limit_exceeded:
                _status(request, run, status="failed", stage=stage, summary="Opus 本次调用未正常结束，保留全部现场与日志。")
                raise RuntimeError("Native full-flow invocation failed; see " + str(output))
            requested_session = None if phase == "plan" else plan["session_id"]
            if phase == "plan":
                session, terminal, errors, limit = _claude_turn(output / "stdout.log", requested_session)
                model = _claude_model_evidence(output / "stdout.log", requested_session, MODEL, EFFORT)
            else:
                session, terminal, errors, limit, model = parse_full_flow_native(
                    output / "stdout.log", requested_session, MODEL, EFFORT)
            answer = terminal.get("result")
            if (not model.get("model_verified") or errors or (limit or {}).get("status") == "rejected" or terminal.get("is_error") or
                    not isinstance(answer, str) or not answer.strip()):
                _status(request, run, status="failed", stage=stage, summary="Opus 返回结果或模型证据无效。")
                raise RuntimeError("Unverified native Opus result")
            if phase == "plan" and session == operator["source_session"]:
                raise RuntimeError("The planning call did not fork the original coding session")
            if phase != "plan" and session != requested_session:
                raise RuntimeError("Testing did not resume the verified Opus planning branch")
            write_json(output / "answer.json", {"session_id": session, "answer": answer,
                       "model_selection": model, "finished_at": now()})
            write_text(output / "answer.md", answer)
            if gitops.head(source) != request["head_sha"] or not gitops.clean(source):
                raise RuntimeError("Testing changed the original candidate")
            if phase == "plan":
                _status(request, run, status="preparing", stage="prepare", summary="Opus 方案已返回，正在准备隔离测试工具。")
                outcome = "planned"
            else:
                missing = []
                required_stages = list(STAGES[:-1])
                if supplemental:
                    required_stages.append("package-short")
                for required in required_stages:
                    receipt_path = ue_checks._safe_under(run, Path("checks") / required / "receipt.json")
                    if not receipt_path.exists():
                        missing.append(required)
                        continue
                    receipt = ue_checks._object(receipt_path, maximum=16 * 1024 * 1024)
                    if any(receipt.get(key) != request[key] for key in
                           ("id", "task_id", "request_sha256", "head_sha")):
                        raise RuntimeError("Test receipt belongs to another candidate or request")
                    if receipt.get("stage") != required:
                        raise RuntimeError("Test receipt belongs to another stage")
                    if receipt.get("status") not in {"passed", "failed"} or not receipt.get("finished_at"):
                        missing.append(required)
                write_text(run / "report.md", answer)
                _status(request, run, status="failed" if missing else "completed", stage="analysis",
                        report_path=str(run / "report.md"), missing_stages=missing,
                        summary=("Opus 已返回报告，但部分测试阶段尚未取得回执：" + ", ".join(missing)) if missing else
                        "Opus 已完成本轮测试并给出报告；通过与失败以各阶段真实回执为准。")
                outcome = "incomplete" if missing else "completed"
            print(json.dumps({"status": outcome, "session": session,
                              "output": str(output), "model_verified": True}, ensure_ascii=True), flush=True)
        except Exception as error:
            write_json(output / "operator-error.json", {"type": type(error).__name__, "error": str(error), "at": now()})
            _status(request, run, status="failed", stage=stage, summary="Opus 调用或证据核验失败，已保留日志：" + type(error).__name__)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "unattended.local.json")
    parser.add_argument("--request", type=Path)
    parser.add_argument("--phase", choices=("create", "plan", "test", "analysis"), required=True)
    args = parser.parse_args()
    if args.phase == "create":
        print(json.dumps(create(args.config), ensure_ascii=True))
    else:
        if not args.request:
            parser.error("--request is required for a native call")
        _, run = _request(args.request)
        with lock(run / "dispatch.lock"):
            invoke(args.config, args.request, args.phase)


if __name__ == "__main__":
    main()
