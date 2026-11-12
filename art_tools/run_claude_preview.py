"""Run one managed Claude asset task; each explicit invocation keeps its history."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unattended.providers import AgentProviders
from unattended.art_pipeline import atomic


RUNTIME = Path("C:/codex-dsh-work/zhuguo-art")
PROMPT = (
    "TASK-ART-0001：执行已授权的普通住宅三级美术预览。用户已确认半写实手绘A并要求更精细美观、"
    "固定较大地块、3000积分全批上限。读取art-brief.json，原样执行本消息后附的固定preview入口，"
    "使用前台Bash，timeout=600000，等待实际完成。不要改写脚本，不要直接调Tripo，不读密钥或私有账本。"
    "本次显式启动使用新会话，先核对公共产物与已完成阶段，复用已有任务及合格缓存。"
    "若命令需续跑，只可重跑同一个幂等入口；具体失败需如实说明，不编造通过。"
    "三级样板及必要道具完成后检查公共review-package.json和截图。"
    "这里只是独立资产生产任务，不需要游戏代码提交；不要改当前M3工作副本。"
    "最终用中文报告实际生成/处理/导入/展示结果、实际积分、产物路径、仍待用户视觉确认。"
    "禁止自动补齐其余五级。"
)


class RunnerBusy(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def read_object(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Runner configuration and state must be JSON objects")
    return value


@contextmanager
def runner_lock(runtime):
    """Acquire before reading/replacing state; process death releases this OS lock."""
    runtime = Path(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "claude-runner.lock").open("a+b") as stream:
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RunnerBusy("A Claude art runner already owns this runtime; no state was changed") from None
        try:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def run(runtime=RUNTIME, *, provider_factory=None):
    """Invoke Claude once, preserving previous runs and cached asset production."""
    runtime = Path(runtime).resolve()
    with runner_lock(runtime):
        config = read_object(runtime / "provider.json")
        if Path(config["runtime_dir"]).resolve() != runtime:
            raise ValueError("Provider configuration belongs to another runtime")
        if config.get("coding_provider") != "claude" or "TASK-ART-0001" not in config["art_pipeline"]["task_ids"]:
            raise ValueError("The runner requires the explicitly approved Claude asset task")
        artifacts = Path(config["art_pipeline"]["artifacts_dir"])
        if not artifacts.is_absolute():
            raise ValueError("Public asset artifacts must use an absolute path")
        state_path = runtime / "state.json"
        previous = read_object(state_path) if state_path.is_file() else None
        out = runtime / "runs" / ("code-" + uuid.uuid4().hex)
        out.mkdir(parents=True)
        state = {"version": 1, "phase": "CODE", "updated_at": now(),
                 "task": {"id": "TASK-ART-0001", "title": "普通住宅三级美术样板", "session_id": None},
                 "operation": {"kind": "code", "task_id": "TASK-ART-0001", "output_dir": str(out),
                               "started_at": now(), "status": "running", "completed": False}}
        if previous is not None:
            history = runtime / "runner-history" / (out.name + "-previous.json")
            atomic(history, previous)
            state["previous_state_path"] = str(history)

        def persist():
            state["updated_at"] = now()
            atomic(out / "runner-state.json", state)
            atomic(state_path, state)

        persist()
        print("Claude art operation:", out, flush=True)
        try:
            factory = AgentProviders if provider_factory is None else provider_factory
            result = factory(config).code(PROMPT, runtime / "workspace", out, 7200)
            atomic(out / "runner-result.json", result)
            atomic(runtime / "claude-execution-result.json", result)
            if result.get("completed") is not True:
                raise RuntimeError("Claude did not complete its protocol turn")
            state["task"]["session_id"] = result.get("session_id")
            pipeline_state = artifacts / "pipeline-state.json"
            actual = read_object(pipeline_state) if pipeline_state.is_file() else {}
            complete = actual.get("stage") == "pending_visual_acceptance"
            state["phase"] = "AWAITING_ART_APPROVAL" if complete else "ART_REWORK"
            state["production_result"] = actual
            state["operation"].update(status="completed", completed=True, finished_at=now())
            persist()
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return state
        except BaseException as error:
            interrupted = isinstance(error, (KeyboardInterrupt, SystemExit)) or getattr(error, "interrupted", False)
            state["phase"] = "ART_INTERRUPTED" if interrupted else "ART_FAILED"
            state["operation"].update(status="interrupted" if interrupted else "failed", completed=False,
                                      finished_at=now())
            # Provider logs already carry its sanitized diagnostics. Do not copy
            # arbitrary exception text (which can include credentials/URLs) here.
            state["last_error"] = {"type": type(error).__name__,
                                   "message": "Asset runner did not complete; inspect this operation's provider logs."}
            session = getattr(error, "session_id", None)
            if session:
                state["task"]["session_id"] = session
            atomic(out / "runner-failure.json", {"phase": state["phase"], "error": state["last_error"],
                                                 "recorded_at": now()})
            persist()
            raise


def main():
    try:
        run()
        return 0
    except KeyboardInterrupt:
        print("Claude art runner interrupted; state and run history were preserved.", flush=True)
        return 130
    except Exception as error:
        print(type(error).__name__ + ": Claude art runner did not complete; inspect its saved run record.", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
