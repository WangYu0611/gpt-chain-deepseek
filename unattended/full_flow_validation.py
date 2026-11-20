"""Run an isolated, user-requested full-flow test at one fixed commit."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from github_harness_lab.lab_harness.process import run_process, safe_environment
from . import full_flow_checks, gitops, ue_checks
from .config import codex_role_settings, load_config
from .network import proxy_environment
from .providers import (AgentProviders, _codex_command, _codex_headless_args, _codex_windows_sandbox_args,
                        _strict_json, parse_codex_events)
from .secrets import secret_values
from .storage import lock, read_json, write_json, write_text

ROOT = Path(__file__).resolve().parent.parent
STAGES = full_flow_checks.STAGES
_BINDING_KEYS = ("id", "task_id", "head_sha", "request_sha256")
_TERMINAL = {"passed", "failed"}


def now():
    return datetime.now(timezone.utc).isoformat()


canonical_hash = full_flow_checks.request_hash


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _task_output_directory(source, session):
    """Read location for historical Claude receipts."""
    uuid.UUID(session)
    project_key = re.sub(r"[^A-Za-z0-9]", "-", str(source))
    return Path(tempfile.gettempdir()) / "claude" / project_key / session / "tasks"


def _request(path):
    request = full_flow_checks.load_request(Path(path).absolute())
    if not re.fullmatch(r"[0-9a-f]{32}", request["id"]):
        raise ValueError("Invalid full-flow request identity")
    run = Path(request["run_dir"])
    if Path(request["source_workspace"]) != Path(request["runtime_dir"]) / "workspace":
        raise ValueError("Full-flow request binding changed")
    return request, run


def _status(request, run, **changes):
    path = run / "status.json"
    value = read_json(path) if path.exists() else {}
    value.update(version=1, **{key: request[key] for key in _BINDING_KEYS}, updated_at=now())
    value.setdefault("model", request["model"])
    value.setdefault("effort", request["effort"])
    value.update(changes)
    write_json(path, value)


def create(config_path):
    config = load_config(config_path)
    selected_model, selected_effort = codex_role_settings(config, "plan")
    if full_flow_checks.full_flow_provider(selected_model, selected_effort) != "codex":
        raise ValueError("Full-flow planning requires a configured Codex model at high or ultra effort")
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
            _, old_run = _request(runtime / read_json(current)["request_path"])
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
        request = {"version": 1, "id": ident, "task_id": task["id"], "head_sha": gitops.head(source),
                   "source_workspace": str(source), "runtime_dir": str(runtime), "run_dir": str(run),
                   "project_dir": str(run / "project"),
                   "engine_dir": "C:/Program Files/Epic Games/UE_5.8/Engine",
                   "authorized_at": now(), "model": selected_model, "effort": selected_effort}
        request["request_sha256"] = canonical_hash(request)
        write_json(run / "request.json", request)
        write_json(run / "operator.json", {"user_request": "使用 Codex 执行完整流程测试",
                   "source_session": task["session_id"], "source_state_sha256": file_hash(runtime / "state.json"),
                   "source_reworks": task["reworks"], "owns_pause": owned, "pause_token": token if owned else None,
                   "pause_sha256": file_hash(pause), "pause_mtime_ns": pause.stat().st_mtime_ns,
                   "authorized_at": request["authorized_at"]})
        _status(request, run, status="planning", stage="plan", summary="Codex 正在等待规划全流程测试。",
                output_dir=str(run / "native" / "plan"))
        write_json(current, {"version": 1, "id": ident,
                   "request_path": (Path("full-flow") / "runs" / ident / "request.json").as_posix(),
                   "request_sha256": request["request_sha256"],
                   "request_file_sha256": file_hash(run / "request.json")})
        return {"request": str(run / "request.json"), "status": "created", "model": selected_model,
                "effort": selected_effort, "head_sha": request["head_sha"]}


def _assert_source(request, run):
    runtime, source = Path(request["runtime_dir"]), Path(request["source_workspace"])
    operator = read_json(run / "operator.json")
    state = read_json(runtime / "state.json")
    task = state.get("task") or {}
    if (state.get("phase") != "BLOCKED" or state.get("operation") or task.get("id") != request["task_id"] or
            task.get("session_id") != operator["source_session"] or gitops.head(source) != request["head_sha"] or
            not gitops.clean(source)):
        raise RuntimeError("The original task, session, or clean candidate changed")


def _native_turn(config, source, output, prompt, model, effort):
    settings = config.get("codex", {})
    command = _codex_command(settings.get("command"))
    environment = safe_environment()
    environment.update(proxy_environment(config_path=config.get("config_path"),
                                         secrets_file=config.get("secrets_file"), network_file=config.get("network_file")))
    for name in ("CODEX_HOME", "NODE_EXTRA_CA_CERTS"):
        if os.environ.get(name):
            environment[name] = os.environ[name]
    AgentProviders._check_codex_subscription(command, source, environment, time.monotonic() + 15)
    argv = [*command, "--no-daemon", "-a", "never", "exec",
            *(_codex_headless_args(config) or ["--ignore-user-config"]), "--sandbox", "read-only",
            "--json", "--color", "never", "--model", model, "-c", "model_reasoning_effort=" + json.dumps(effort),
            "-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"']
    argv.extend(_codex_windows_sandbox_args(settings, default="elevated"))
    argv.append("-")
    result = run_process(argv, cwd=source, output_dir=output, timeout_seconds=1800,
                         environment=environment, stdin=prompt.encode("utf-8"),
                         secrets=secret_values(config_path=config.get("config_path"), secrets_file=config.get("secrets_file")),
                         max_output_bytes=64 * 1024 * 1024)
    receipt = {"exit_code": result.exit_code, "timed_out": result.timed_out,
               "output_limit_exceeded": result.output_limit_exceeded, "finished_at": now(),
               "model_selection": {"requested_model": model, "requested_reasoning_effort": effort}}
    write_json(output / "process.json", receipt)
    if result.exit_code or result.timed_out or result.output_limit_exceeded:
        raise RuntimeError("Native Codex full-flow invocation did not complete")
    log = output / "stdout.log"
    session = parse_codex_events(log, exit_code=result.exit_code)["session_id"]
    uuid.UUID(session)
    answer = observed_model = None
    with log.open(encoding="utf-8-sig") as stream:
        for line in stream:
            if not line.strip():
                continue
            event = _strict_json(line)
            if event["type"] == "thread.started":
                observed_model = event.get("model")
            item = event.get("item")
            if event["type"] == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
                answer = item.get("text")
    if not isinstance(answer, str) or not answer.strip():
        raise RuntimeError("Native Codex full-flow response was empty")
    if observed_model is not None and (not isinstance(observed_model, str) or
            observed_model != model and not re.fullmatch(re.escape(model) + r"-\d{4}-\d{2}-\d{2}", observed_model)):
        raise RuntimeError("Native Codex full-flow response names a different model")
    receipt["model_selection"].update(observed_model=observed_model, model_verified=observed_model is not None)
    write_json(output / "process.json", receipt)
    return {"session_id": session, "answer": answer.strip(), "model_selection": receipt["model_selection"], "finished_at": now()}


def _receipts(request, run):
    results = {}
    for stage in (*STAGES[:-1], "package-short"):
        path = ue_checks._safe_under(run, Path("checks") / stage / "receipt.json")
        if not path.is_file():
            continue
        row = ue_checks._object(path, maximum=16 * 1024 * 1024)
        if row.get("stage") != stage or any(row.get(key) != request[key] for key in _BINDING_KEYS):
            raise ValueError("Full-flow stage receipt belongs to another stage, request or HEAD")
        if row.get("status") in _TERMINAL and row.get("finished_at"):
            results[stage] = row["status"]
    return results


def _run_checks(request_path, request, run, output):
    for stage in STAGES[:-1]:
        _status(request, run, status="testing", stage=stage, output_dir=str(output),
                summary="正在执行固定全流程检查。")
        full_flow_checks.run_stage(request_path, stage)


def _output_dir(run, phase):
    candidates = (phase, *(f"analysis-{n}" for n in range(2, 10))) if phase == "analysis" else (phase,)
    for name in candidates:
        path = ue_checks._safe_under(run, Path("native") / name)
        if not path.exists():
            return path
    raise RuntimeError("Full-flow phase already has an invocation")


def invoke(config_path, request_path, phase):
    if phase not in {"plan", "test", "analysis"}:
        raise ValueError("Unknown full-flow phase")
    config = load_config(config_path)
    request, run = _request(request_path)
    if Path(config["runtime_dir"]).resolve() != Path(request["runtime_dir"]):
        raise ValueError("Config belongs to another runtime")
    if full_flow_checks.full_flow_provider(request["model"], request["effort"]) != "codex":
        raise RuntimeError("Historical Claude requests are read-only; create a Codex full-flow request")
    _assert_source(request, run)
    source = Path(request["source_workspace"])
    if phase != "plan":
        plan = read_json(ue_checks._safe_under(run, "native/plan/answer.json"))
        if any(plan.get(key) != request[key] for key in _BINDING_KEYS):
            raise ValueError("The Codex test plan belongs to another request or candidate")
    output = _output_dir(run, phase)
    output.mkdir(parents=True, exist_ok=False)
    stage = "plan" if phase == "plan" else "analysis"
    try:
        if phase == "test":
            _run_checks(Path(request_path).absolute(), request, run, output)
        results = _receipts(request, run) if phase != "plan" else {}
        missing = [name for name in STAGES[:-1] if name not in results] if phase != "plan" else []
        role = "plan" if phase == "plan" else "review"
        model, effort = codex_role_settings(config, role)
        if full_flow_checks.full_flow_provider(model, effort) != "codex":
            raise ValueError("Full-flow verification requires a configured Codex model at high or ultra effort")
        _status(request, run, status="planning" if phase == "plan" else "analyzing", stage=stage,
                output_dir=str(output), model=model, effort=effort, summary="Codex 正在只读核对全流程测试。")
        common = (f"对固定提交 {request['head_sha']} 执行独立全流程验证。任务 {request['task_id']}。"
                  f"原工作区 {source.as_posix()}，隔离测试目录 {run.as_posix()}。"
                  "仅查看该任务、源码和测试证据。基于实际运行数据给出中文结论。")
        if phase == "plan":
            prompt = common + ("先报告测试方案、覆盖范围、已知风险和结果判定规则。"
                               "固定检查覆盖构建、完整回归、真实 RHI、正式打包和失败项复测。")
        else:
            prompt = common + (f"\n已完成的阶段状态为 {json.dumps(results, ensure_ascii=False)}。"
                               f"缺失阶段 {json.dumps(missing, ensure_ascii=False)}。"
                               f"读取 {run.as_posix()}/checks 下的实际回执和相关日志，复核原始结果。"
                               "报告真实失败、环境阻断和未运行项目，按证据路径说明覆盖与结论。")
        write_text(output / "prompt.txt", prompt)
        write_json(output / "invocation.json", {"model": model, "effort": effort, "phase": phase,
                   "provider": "codex", "sandbox": "read-only", **{key: request[key] for key in _BINDING_KEYS},
                   "started_at": now(), "orchestrator_sha256": file_hash(Path(__file__))})
        with lock(run / "native.lock"):
            answer = _native_turn(config, source, output, prompt, model, effort)
        _assert_source(request, run)
        write_json(output / "answer.json", {**answer, **{key: request[key] for key in _BINDING_KEYS}})
        write_text(output / "answer.md", answer["answer"])
        if phase == "plan":
            _status(request, run, status="preparing", stage="prepare", model=model, effort=effort,
                    summary="Codex 已返回方案，等待隔离测试。")
            outcome = "planned"
        else:
            write_text(run / "report.md", answer["answer"])
            _status(request, run, status="failed" if missing else "completed", stage=stage,
                    model=model, effort=effort, report_path=str(run / "report.md"), missing_stages=missing,
                    summary=("部分阶段缺少终态回执：" + "、".join(missing) if missing else "本轮测试完成，结果以各阶段回执为准。"))
            outcome = "incomplete" if missing else "completed"
        print(json.dumps({"status": outcome, "session": answer["session_id"], "output": str(output),
                          "model": model, "effort": effort}, ensure_ascii=True), flush=True)
    except Exception as error:
        write_json(output / "operator-error.json", {"type": type(error).__name__, "error": str(error), "at": now()})
        _status(request, run, status="failed", stage=stage, summary="全流程执行或证据核验失败。",
                output_dir=str(output))
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
            parser.error("--request is required")
        _, run = _request(args.request)
        with lock(run / "dispatch.lock"):
            invoke(args.config, args.request, args.phase)


if __name__ == "__main__":
    main()
