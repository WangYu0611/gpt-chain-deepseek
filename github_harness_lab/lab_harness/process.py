"""Bounded, shell-free subprocesses. Logs stay local; never upload raw logs blindly."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence


_MAX_OUTPUT_RECORD_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    timed_out: bool
    output_limit_exceeded: bool
    stdout_path: Path
    stderr_path: Path


class _WindowsJob:
    """An owned Job terminates descendants on close, including an orphaned child."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Io),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            kernel.CloseHandle(handle)
            raise OSError(error, "SetInformationJobObject failed")
        self.kernel, self.handle = kernel, handle

    def assign(self, process: subprocess.Popen[bytes]):
        import ctypes
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _resume_windows_process(process: subprocess.Popen[bytes]):
    """Resume the sole initial thread only after the suspended root owns its Job.

    Popen closes CreateProcess's primary-thread handle on Windows. Use documented
    Toolhelp/OpenThread APIs instead of resuming a process through an undocumented
    NT API or changing subprocess's process-global CreateProcess function.
    """
    import ctypes
    from ctypes import wintypes

    class ThreadEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                    ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Thread32First", "Thread32Next"):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        function.restype = wintypes.BOOL
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.GetProcessIdOfThread.argtypes = [wintypes.HANDLE]
    kernel.GetProcessIdOfThread.restype = wintypes.DWORD
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    TH32CS_SNAPTHREAD = 0x00000004
    THREAD_SUSPEND_RESUME = 0x0002
    THREAD_QUERY_LIMITED_INFORMATION = 0x0800
    ERROR_NO_MORE_FILES = 18
    snapshot = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    try:
        entry = ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel.Thread32First(snapshot, ctypes.byref(entry))
        thread_ids = []
        while found:
            if entry.th32OwnerProcessID == process.pid:
                thread_ids.append(entry.th32ThreadID)
            entry.dwSize = ctypes.sizeof(entry)
            found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        error = ctypes.get_last_error()
        if error != ERROR_NO_MORE_FILES:
            raise OSError(error, "Thread enumeration failed")
    finally:
        kernel.CloseHandle(snapshot)
    if len(thread_ids) != 1:
        raise RuntimeError("Suspended subprocess must have exactly one initial thread")
    thread = kernel.OpenThread(THREAD_SUSPEND_RESUME | THREAD_QUERY_LIMITED_INFORMATION,
                               False, thread_ids[0])
    if not thread:
        raise OSError(ctypes.get_last_error(), "OpenThread failed")
    try:
        if kernel.GetProcessIdOfThread(thread) != process.pid:
            raise RuntimeError("Suspended subprocess thread ownership changed")
        suspended = kernel.ResumeThread(thread)
        if suspended == 0xFFFFFFFF:
            raise OSError(ctypes.get_last_error(), "ResumeThread failed")
        if suspended != 1:
            raise RuntimeError("Subprocess initial thread was not suspended exactly once")
    finally:
        kernel.CloseHandle(thread)


def _terminate(process: subprocess.Popen[bytes], job: _WindowsJob | None):
    if job is not None:
        job.close()
    elif os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=15, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def safe_environment(source: Mapping[str, str] | None = None, *, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Do not hand the model runner the publisher's GitHub/AWS/other credentials."""
    source = os.environ if source is None else source
    allowed = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP",
               "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
               "PROGRAMDATA", "ALLUSERSPROFILE", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"}
    environment = {k: v for k, v in source.items() if k.upper() in allowed}
    extra_allowed = {"DSH_HOME", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "NODE_EXTRA_CA_CERTS"}
    for key, value in (extra or {}).items():
        if key not in extra_allowed or not isinstance(value, str) or "\0" in value:
            raise ValueError(f"Child environment variable is not allowed: {key}")
        environment[key] = value
    environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", GIT_TERMINAL_PROMPT="0")
    return environment


def run_process(argv: Sequence[str], *, cwd: Path, output_dir: Path, timeout_seconds: float,
                environment: Mapping[str, str], stdin: bytes = b"", max_output_bytes: int = 16 * 1024 * 1024,
                secrets: Sequence[str] = (), stdout_record_filter: Callable[[bytes], bytes] | None = None) -> ProcessResult:
    """Never invokes a shell. The command and task must come from trusted code/config."""
    if not argv or any(not isinstance(x, str) or "\0" in x for x in argv):
        raise ValueError("argv must contain non-NUL strings")
    if Path(argv[0]).suffix.lower() in {".cmd", ".bat"}:
        raise ValueError("Use a native executable or node.exe plus a JS entrypoint, not a cmd/bat shim")
    if not 0 < timeout_seconds < float("inf") or max_output_bytes < 1024:
        raise ValueError("A positive timeout and bounded output are required")
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_path, stderr_path = output_dir / "stdout.log", output_dir / "stderr.log"
    secret_bytes = sorted({s.encode("utf-8") for s in secrets if s}, key=len, reverse=True)
    keep = max((len(s) for s in secret_bytes), default=1) - 1
    stopped = threading.Event()
    overflow = threading.Event()
    failures: list[BaseException] = []
    written = [0]
    mutex = threading.Lock()
    process = None
    job = None
    threads = []
    timed_out = False

    def copy(source, target, record_filter=None):
        pending = b""
        record = bytearray()

        def write(chunk, *, final=False):
            with mutex:
                remaining = max_output_bytes - written[0]
                if len(chunk) > remaining:
                    overflow.set()
                    if not final:
                        stopped.set()
                        return False
                    chunk = chunk[:remaining]
                written[0] += len(chunk)
            target.write(chunk)
            if not final:
                target.flush()
            return True

        def emit(chunk, *, final=False):
            if record_filter is None:
                return write(chunk, final=final)
            parts = chunk.split(b"\n")
            for index, part in enumerate(parts):
                terminated = index < len(parts) - 1
                if len(record) + len(part) + terminated > _MAX_OUTPUT_RECORD_BYTES:
                    overflow.set()
                    stopped.set()
                    return False
                record.extend(part)
                if terminated:
                    record.append(10)
                if terminated or (final and record):
                    filtered = record_filter(bytes(record))
                    for secret in secret_bytes:
                        filtered = filtered.replace(secret, b"[REDACTED]")
                    if not write(filtered):
                        return False
                if terminated:
                    record.clear()
            return True

        try:
            while True:
                raw = source.read1(65536)
                if not raw:
                    break
                pending += raw
                # Retain a suffix so a secret split between pipe reads is still redacted.
                if len(pending) <= keep:
                    continue
                cut = len(pending) - keep
                for secret in secret_bytes:
                    index = pending.find(secret)
                    while index >= 0:
                        if index < cut < index + len(secret):
                            cut = index
                        index = pending.find(secret, index + 1)
                chunk, pending = pending[:cut], pending[cut:]
                for secret in secret_bytes:
                    chunk = chunk.replace(secret, b"[REDACTED]")
                if not emit(chunk):
                    if record_filter is not None:
                        return
                    break
            for secret in secret_bytes:
                pending = pending.replace(secret, b"[REDACTED]")
            emit(pending, final=True)
        except BaseException as error:
            failures.append(error)
            stopped.set()
        finally:
            source.close()

    def write_prompt():
        try:
            process.stdin.write(stdin)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            process.stdin.close()

    with stdout_path.open("xb") as out, stderr_path.open("xb") as err:
        try:
            if os.name == "nt":
                # Create the kill-only Job first. CREATE_SUSPENDED prevents even a
                # fast root from exiting or spawning uncontained descendants before
                # assignment, and remains compatible with inherited nested Jobs.
                job = _WindowsJob()
                CREATE_SUSPENDED = 0x00000004
                options = {"creationflags": subprocess.CREATE_NO_WINDOW | CREATE_SUSPENDED}
            else:
                options = {"start_new_session": True}
            process = subprocess.Popen(list(argv), cwd=str(cwd), env=dict(environment), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, **options)
            if os.name == "nt":
                try:
                    job.assign(process)
                    _resume_windows_process(process)
                except BaseException:
                    # The root has never run if assignment fails. After assignment,
                    # closing the Job also contains any partial resume failure.
                    _terminate(process, job)
                    raise
            for source, target, record_filter in ((process.stdout, out, stdout_record_filter), (process.stderr, err, None)):
                thread = threading.Thread(target=copy, args=(source, target, record_filter), daemon=True)
                threads.append(thread)
                thread.start()
            writer = threading.Thread(target=write_prompt, daemon=True)
            threads.append(writer)
            writer.start()
            import time
            deadline = time.monotonic() + timeout_seconds
            while process.poll() is None:
                if stopped.wait(min(0.1, max(0, deadline - time.monotonic()))):
                    _terminate(process, job)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    _terminate(process, job)
                    break
            # Closing the job also kills children holding inherited pipes after the root exited.
            if job is not None:
                job.close()
            elif os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            if process is not None and process.poll() is None:
                _terminate(process, job)
            if job is not None:
                job.close()
            for thread in threads:
                thread.join(timeout=5)
            if any(t.is_alive() for t in threads):
                raise RuntimeError("Subprocess output did not settle; cannot trust this run")
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
    if failures:
        raise RuntimeError("Subprocess log capture failed") from failures[0]
    return ProcessResult(process.returncode, timed_out, overflow.is_set(), stdout_path, stderr_path)
