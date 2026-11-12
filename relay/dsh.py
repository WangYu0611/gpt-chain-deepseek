"""Run the official DSH headless CLI without sending prompts through a shell.

The event contract follows deepseek-ai/deepseek-harness's headless profile:
``session`` opens the NDJSON stream and ``final`` contains ``text`` and
``reason.kind``. A completed model turn is separate from project acceptance.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass
from typing import Any


class DshError(RuntimeError):
    """The invocation failed or did not produce a completed headless turn."""

    def __init__(
        self,
        message: str,
        *,
        events_path: Path | None = None,
        exit_code: int | None = None,
        session_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.events_path = events_path
        self.exit_code = exit_code
        self.session_id = session_id


@dataclass(frozen=True)
class DshResult:
    session_id: str
    answer: str
    events_path: Path
    exit_code: int


class _WindowsJob:
    """Kill descendants when the job closes, including after the parent exits."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "Cannot create DSH process job")
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            kernel.CloseHandle(handle)
            raise OSError(error, "Cannot configure DSH process job")
        if not kernel.AssignProcessToJobObject(handle, int(process._handle)):
            error = ctypes.get_last_error()
            kernel.CloseHandle(handle)
            raise OSError(error, "Cannot contain DSH process tree")
        self._kernel = kernel
        self._handle = handle

    def close(self) -> None:
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


def _terminate_tree(process: subprocess.Popen[bytes], job: _WindowsJob | None) -> None:
    if job is not None:
        job.close()
    elif os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        subprocess.run(
            [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def _command(executable: str, args: list[str]) -> tuple[list[str] | str, dict[str, Any]]:
    """cmd shims are supported only with a deliberately restricted grammar.

    Every shim argument is quoted individually. The prompt is never an argument.
    Reject cmd expansion/operators instead of trying to invent shell escaping.
    """
    located = shutil.which(executable)
    if located is None:
        candidate = Path(executable).expanduser()
        if candidate.is_file():
            located = str(candidate.resolve())
        else:
            raise DshError("找不到 DSH 命令，请配置 dsh.executable 的完整路径。")
    if os.name == "nt":
        options: dict[str, Any] = {"creationflags": subprocess.CREATE_NO_WINDOW}
        if Path(located).suffix.lower() in {".cmd", ".bat"}:
            forbidden = re.compile(r'["\r\n\x00%!^&|<>]')
            if any(forbidden.search(part) for part in [located, *args]):
                raise DshError(
                    "DSH 的 .cmd/.bat 路径或参数含 shell 特殊字符；"
                    "请改用不含这些字符的路径，或配置 dsh.exe / node.exe 入口。"
                )
            cmd = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
            # A native CLI reached via the shim still applies Windows argv
            # decoding: double a trailing backslash before the closing quote.
            quoted = " ".join(
                '"' + re.sub(r"(\\+)$", lambda match: match.group(1) * 2, part) + '"'
                for part in [located, *args]
            )
            # /s removes exactly the first and last quote around /c's payload.
            line = subprocess.list2cmdline([str(cmd), "/d", "/s", "/c"]) + ' "' + quoted + '"'
            return line, options
        return [located, *args], options
    return [located, *args], {"start_new_session": True}


def _secret_values(environment: dict[str, str]) -> list[bytes]:
    pattern = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)", re.I)
    return sorted(
        {value.encode("utf-8") for key, value in environment.items()
         if pattern.search(key) and len(value) >= 8 and "\n" not in value},
        key=len,
        reverse=True,
    )


class DshRunner:
    def __init__(self, config: dict[str, Any], runtime_dir: Path) -> None:
        self.config = config
        self.runtime_dir = Path(runtime_dir).resolve()

    def run(
        self,
        prompt: str,
        cwd: Path,
        run_id: str,
        session_id: str | None = None,
    ) -> DshResult:
        if not isinstance(prompt, str) or not prompt.strip():
            raise DshError("DSH 任务内容不能为空。")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", run_id):
            raise DshError("无效的 DSH run_id。")
        cwd = Path(cwd).resolve()
        if not cwd.is_dir():
            raise DshError("DSH 工作目录不存在。")
        executable = self.config.get("executable", "dsh")
        configured_args = self.config.get("args", ["--profile", "headless", "--json"])
        if not isinstance(executable, str) or not executable.strip():
            raise DshError("dsh.executable 必须是非空字符串。")
        if not isinstance(configured_args, list) or not all(isinstance(x, str) for x in configured_args):
            raise DshError("dsh.args 必须是字符串数组。")
        args = list(configured_args)
        if any("\x00" in arg for arg in args):
            raise DshError("DSH 参数包含无效字符。")
        if "--session-id" in args or "-" in args:
            raise DshError("请通过 session_id 续接会话；dsh.args 中不要放 --session-id 或 stdin 标记。")
        if "--json" not in args:
            args.append("--json")
        patch = self.config.get("patch")
        if patch:
            patch_path = Path(patch).expanduser().resolve()
            if not patch_path.is_file():
                raise DshError("配置的 DSH patch 文件不存在。")
            # --patch belongs to the DSH launcher, not the headless app. The
            # launcher forwards everything after --profile to the app, so keep
            # this option before that boundary while preserving a Node/Python
            # entry-script prefix in configured_args.
            profile_index = next(
                (index for index, value in enumerate(args)
                 if value == "--profile" or value.startswith("--profile=")),
                None,
            )
            if profile_index is None:
                if args and args[0] == "headless":
                    profile_index = 0
                elif len(args) > 1 and args[1] == "headless" and Path(args[0]).suffix.lower() in {".js", ".cjs", ".mjs", ".py"}:
                    profile_index = 1
                else:
                    # A custom test/wrapper executable without a profile
                    # boundary owns its argument grammar; do not move options
                    # in front of its entry script.
                    profile_index = len(args)
            args[profile_index:profile_index] = ["--patch", str(patch_path)]
        if session_id and self.config.get("reuse_session", True):
            args.extend(["--session-id", session_id])
        args.append("-")
        command, popen_options = _command(executable, args)
        environment = dict(os.environ)
        custom_env = self.config.get("env", {})
        if not isinstance(custom_env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and "=" not in k and "\x00" not in k + v
            for k, v in custom_env.items()
        ):
            raise DshError("dsh.env 必须是环境变量名到字符串值的映射。")
        environment.update(custom_env)
        environment.setdefault("PYTHONIOENCODING", "utf-8")
        environment.setdefault("PYTHONUTF8", "1")
        if self.config.get("home"):
            environment["DSH_HOME"] = str(Path(self.config["home"]).expanduser().resolve())
        try:
            timeout = float(self.config.get("timeout_seconds", 7200))
        except (TypeError, ValueError) as error:
            raise DshError("dsh.timeout_seconds 必须是正数。") from error
        if not 0 < timeout < float("inf"):
            raise DshError("dsh.timeout_seconds 必须是有限正数。")

        logs = self.runtime_dir / "dsh"
        logs.mkdir(parents=True, exist_ok=True)
        events_path = logs / f"{run_id}.jsonl"
        stderr_path = logs / f"{run_id}.stderr.log"
        try:
            stdout_log = events_path.open("xb")
        except FileExistsError as error:
            raise DshError("此 DSH run_id 已执行或启动过，请核对状态，避免重复执行。", events_path=events_path) from error
        try:
            stderr_log = stderr_path.open("xb")
        except BaseException:
            stdout_log.close()
            raise

        process: subprocess.Popen[bytes] | None = None
        job: _WindowsJob | None = None
        readers: list[threading.Thread] = []
        writer: threading.Thread | None = None
        stream_errors: list[BaseException] = []
        secrets = _secret_values(environment)

        def copy_stream(source: Any, target: Any) -> None:
            try:
                while True:
                    raw_line = source.readline()
                    if not raw_line:
                        break
                    for secret in secrets:
                        raw_line = raw_line.replace(secret, b"[REDACTED]")
                    target.write(raw_line)
                    target.flush()
            except BaseException as error:
                stream_errors.append(error)
            finally:
                source.close()

        def write_prompt() -> None:
            assert process is not None and process.stdin is not None
            try:
                process.stdin.write(prompt.encode("utf-8"))
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                process.stdin.close()

        try:
            try:
                process = subprocess.Popen(
                    command, cwd=str(cwd), env=environment,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    shell=False, **popen_options,
                )
            except OSError as error:
                raise DshError("无法启动 DSH；请检查命令路径和安装。", events_path=events_path) from error
            if os.name == "nt":
                try:
                    job = _WindowsJob(process)
                except OSError as error:
                    _terminate_tree(process, None)
                    raise DshError("无法隔离 DSH 进程树，已停止启动。", events_path=events_path) from error
            for source, target in [(process.stdout, stdout_log), (process.stderr, stderr_log)]:
                thread = threading.Thread(target=copy_stream, args=(source, target), daemon=True)
                readers.append(thread)
                thread.start()
            writer = threading.Thread(target=write_prompt, daemon=True)
            writer.start()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                _terminate_tree(process, job)
                raise DshError("DSH 执行超时，已停止进程树；请检查日志后继续。", events_path=events_path) from error
            except KeyboardInterrupt:
                _terminate_tree(process, job)
                raise
            # The official one-shot contract ends all child work with the run.
            # Closing our job also handles a child that leaked an inherited pipe.
            if job is not None:
                job.close()
            else:
                _terminate_tree(process, None)
            writer.join(timeout=5)
            for thread in readers:
                thread.join(timeout=5)
            if any(thread.is_alive() for thread in readers) or stream_errors:
                raise DshError("DSH 输出日志未完整写入；本轮不能视为完成。", events_path=events_path)
        finally:
            if process is not None and process.poll() is None:
                _terminate_tree(process, job)
            if job is not None:
                job.close()
            for thread in readers:
                thread.join(timeout=5)
            if writer is not None:
                writer.join(timeout=5)
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
            stdout_log.close()
            stderr_log.close()

        assert process is not None
        return self._read_result(events_path, process.returncode, session_id)

    def _read_result(self, events_path: Path, exit_code: int, requested_session: str | None) -> DshResult:
        opened_session: str | None = None
        session_seen = False
        final: dict[str, Any] | None = None
        malformed = False
        error_event = False
        current_turn: int | None = None
        terminal_reason: dict[str, Any] | None = None
        legacy_turn_valid = True
        try:
            with events_path.open("r", encoding="utf-8-sig", errors="strict") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, UnicodeError):
                        malformed = True
                        continue
                    if not isinstance(event, dict):
                        malformed = True
                        continue
                    if final is not None:
                        # No second final, status, or other event can follow the
                        # terminal record without invalidating this run.
                        malformed = True
                        continue
                    if event.get("type") == "session":
                        if session_seen:
                            malformed = True
                        session_seen = True
                        opened_session = event.get("sessionId")
                        if not isinstance(opened_session, str) or not opened_session:
                            malformed = True
                    elif event.get("type") == "final":
                        final = event
                    elif event.get("type") == "error":
                        error_event = True
                    elif event.get("type") == "status":
                        phase = event.get("phase")
                        if phase == "turn_start":
                            # Older installed DSH builds omit the session and
                            # reason from final. Only the latest started turn
                            # can establish their completed outcome.
                            turn = event.get("turn")
                            current_turn = turn if type(turn) is int and turn > 0 else None
                            terminal_reason = None
                            if not session_seen or current_turn is None:
                                legacy_turn_valid = False
                        elif phase == "turn_end":
                            turn, reason = event.get("turn"), event.get("reason")
                            if (current_turn is None or type(turn) is not int or turn != current_turn
                                    or not isinstance(reason, dict) or not isinstance(reason.get("kind"), str)):
                                legacy_turn_valid = False
                                terminal_reason = None
                            else:
                                terminal_reason = reason
        except (OSError, UnicodeError) as error:
            raise DshError("无法读取完整的 DSH JSON 日志。", events_path=events_path, exit_code=exit_code) from error

        details = {"events_path": events_path, "exit_code": exit_code, "session_id": opened_session}
        if exit_code != 0:
            raise DshError(f"DSH 退出码为 {exit_code}；本轮已暂停，请检查 stderr 日志。", **details)
        if malformed or error_event or final is None or not isinstance(opened_session, str) or not opened_session:
            raise DshError("DSH 未返回完整有效的 session/final JSON 事件，不能确认完成。", **details)
        if requested_session and self.config.get("reuse_session", True) and requested_session != opened_session:
            raise DshError("DSH 返回的会话与请求续接的会话不一致。", **details)
        if "sessionId" in final or "reason" in final:
            # A partially populated new-format final is not an old-format final.
            # Never fill in its missing fields from earlier status records.
            if "sessionId" not in final or "reason" not in final:
                raise DshError("DSH 新版 final 事件字段不完整，不能降级判定完成。", **details)
            if final.get("sessionId") != opened_session:
                raise DshError("DSH session 和 final 的会话标识不一致。", **details)
            reason = final.get("reason")
        else:
            if set(final) != {"type", "text"} or not legacy_turn_valid or current_turn is None:
                raise DshError("DSH 旧版 final 缺少可核对的最后一轮完整状态。", **details)
            reason = terminal_reason
        if not isinstance(reason, dict) or reason.get("kind") != "completed":
            raise DshError("DSH 未返回 completed 状态；需要检查阻塞或错误后继续。", **details)
        answer = final.get("text")
        if not isinstance(answer, str) or not answer.strip():
            raise DshError("DSH 已退出但没有最终交付说明，不能进入 GPT 验收。", **details)
        return DshResult(opened_session, answer, events_path, exit_code)
