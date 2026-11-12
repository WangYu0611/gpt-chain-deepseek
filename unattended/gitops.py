"""A continuous branch in a separate clone; never reset the user's checkout."""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from github_harness_lab.lab_harness.process import run_process
from .storage import read_json, write_json

_DEADLINE = ContextVar("unattended_git_deadline", default=None)


class WindowClosed(RuntimeError):
    pass


@contextmanager
def deadline(seconds):
    token = _DEADLINE.set(None if seconds is None else time.monotonic() + seconds)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def git(root: Path, *args: str, timeout: float = 60) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_PAGER="cat")
    limit = _DEADLINE.get()
    remaining = limit - time.monotonic() if limit is not None else timeout
    if remaining <= 0:
        raise WindowClosed("工作窗口已截止。")
    with tempfile.TemporaryDirectory(prefix="unattended-git-") as scratch:
        result = run_process(["git", "-c", "core.quotepath=false", "-c", "core.fsmonitor=false",
                              "-c", "diff.external=", "-C", str(root), *args], cwd=root,
                             environment=env, output_dir=Path(scratch), timeout_seconds=min(timeout, remaining))
        if result.timed_out and limit is not None and remaining <= timeout:
            raise WindowClosed("Git 步骤已在工作窗口截止时停止，将从断点对账恢复。")
        if result.exit_code or result.timed_out or result.output_limit_exceeded:
            error = result.stderr_path.read_text(encoding="utf-8", errors="replace")[-1600:].strip()
            raise RuntimeError(f"git {args[0]} failed ({result.exit_code}): {error}")
        return result.stdout_path.read_text(encoding="utf-8", errors="replace").rstrip("\r\n")


def common_dir(project: Path) -> Path:
    value = Path(git(project, "rev-parse", "--git-common-dir"))
    return (project / value).resolve() if not value.is_absolute() else value.resolve()


def head(project: Path) -> str:
    return git(project, "rev-parse", "HEAD")


def clean(project: Path) -> bool:
    return not git(project, "status", "--porcelain=v1", "--untracked-files=all")


def _windows_sid_text(sid) -> str:
    import ctypes
    from ctypes import wintypes
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    security.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    value = wintypes.LPWSTR()
    if not security.ConvertSidToStringSidW(sid, ctypes.byref(value)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return value.value
    finally:
        kernel.LocalFree(ctypes.cast(value, ctypes.c_void_p))


def _windows_user_sid() -> str:
    """Use the running token, never an environment-supplied account name."""
    import ctypes
    from ctypes import wintypes
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.GetTokenInformation.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_USER starts with SID_AND_ATTRIBUTES; its first field is PSID.
        return _windows_sid_text(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value)
    finally:
        kernel.CloseHandle(token)


def _windows_owner_sid(directory: Path) -> str:
    import ctypes
    from ctypes import wintypes
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p)]
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    owner, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    error = security.GetNamedSecurityInfoW(str(directory), 1, 1, ctypes.byref(owner),
                                         None, None, None, ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    try:
        return _windows_sid_text(owner)
    finally:
        kernel.LocalFree(descriptor)


def _windows_icacls() -> Path:
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel.GetSystemDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    size = kernel.GetSystemDirectoryW(buffer, len(buffer))
    if not size or size >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    executable = Path(buffer.value) / "icacls.exe"
    if not executable.is_file():
        raise RuntimeError("Windows 系统 icacls.exe 不可用。")
    return executable


def _ensure_windows_workspace_access(directory: Path, runtime: Path):
    """Grant only the owner of a newly created managed clone inheritable F.

    DSH's Windows sandbox temporarily edits the workspace DACL. Inherited
    Modify alone omits WRITE_DAC/WRITE_OWNER and makes its grantWrite fail.
    The source checkout, parent runtime and unrelated directories are untouched.
    """
    if os.name != "nt":
        return
    directory, runtime = Path(directory), Path(runtime).resolve()
    if (directory.is_symlink() or directory.is_junction() or
            directory.parent.resolve() != runtime or
            not re.fullmatch(r"workspace\.build-[0-9a-f]{32}", directory.name) or
            not (directory / ".git").is_dir() or
            (directory / ".git").is_symlink() or (directory / ".git").is_junction()):
        raise RuntimeError("只允许为运行目录中刚创建的独立工作副本设置权限。")
    directory = directory.resolve()
    user = _windows_user_sid()
    if not re.fullmatch(r"S-1-\d+(?:-\d+)+", user) or _windows_owner_sid(directory) != user:
        raise RuntimeError("新工作副本所有者不是当前用户，拒绝接管或更改权限。")
    limit = _DEADLINE.get()
    remaining = limit - time.monotonic() if limit is not None else 30
    if remaining <= 0:
        raise WindowClosed("工作窗口已截止，未更改工作副本权限。")
    with tempfile.TemporaryDirectory(prefix="unattended-acl-") as scratch:
        result = run_process([str(_windows_icacls()), str(directory), "/grant:r", "*" + user + ":(OI)(CI)F", "/Q"],
                             cwd=directory, environment=dict(os.environ), output_dir=Path(scratch),
                             timeout_seconds=min(30, remaining), max_output_bytes=1024 * 1024)
        if result.timed_out and limit is not None and remaining <= 30:
            raise WindowClosed("工作副本权限步骤已在工作窗口截止时停止。")
        if result.exit_code or result.timed_out or result.output_limit_exceeded:
            error = result.stderr_path.read_text(encoding="utf-8", errors="replace")[-1600:].strip()
            raise RuntimeError(f"工作副本当前用户权限设置失败 ({result.exit_code}): {error}")
    if _windows_owner_sid(directory) != user:
        raise RuntimeError("设置权限后工作副本所有者发生变化。")


def ensure_workspace(config: dict) -> Path:
    project, work = Path(config["project_path"]), Path(config["runtime_dir"]) / "workspace"
    branch = config["branch"]
    binding_path = work.parent / "workspace.json"
    binding = {"project_path": os.path.normcase(str(project.resolve())), "branch": branch}
    git(project, "check-ref-format", "--branch", branch)
    if work.exists():
        if not (work / ".git").is_dir():
            raise RuntimeError("工作目录不是独立 Git 副本，拒绝复用。")
        if git(work, "symbolic-ref", "--short", "HEAD") != branch:
            raise RuntimeError("工作分支被外部改变。")
        if not binding_path.is_file():
            raise RuntimeError("工作副本缺少来源绑定，不能自动接管。")
        saved = read_json(binding_path)
        if any(saved.get(key) != value for key, value in binding.items()):
            raise RuntimeError("工作副本与配置项目不匹配。")
        git(work, "merge-base", "--is-ancestor", saved["base_sha"], "HEAD")
        return work
    work.parent.mkdir(parents=True, exist_ok=True)
    # A worktree's .git points outside the workspace, preventing sandboxed DSH
    # from committing. Keep both files and metadata inside this independent clone.
    baseline = git(project, "rev-parse", config.get("base_ref", "HEAD"))
    write_json(binding_path, dict(binding, base_sha=baseline))
    temporary = work.with_name("workspace.build-" + uuid.uuid4().hex)
    git(project, "clone", "--no-hardlinks", "--no-checkout", "--", str(project), str(temporary), timeout=600)
    _ensure_windows_workspace_access(temporary, work.parent)
    remotes = git(project, "remote").splitlines()
    remote = config.get("remote", "origin")
    if remote in remotes:
        url = git(project, "remote", "get-url", remote)
        git(temporary, "remote", "set-url", "origin", url)
        if remote != "origin":
            git(temporary, "remote", "rename", "origin", remote)
    else:
        git(temporary, "remote", "remove", "origin")
    git(temporary, "checkout", "-b", branch, baseline, timeout=300)
    # Copy only nonsecret commit identity, never the user's credential config.
    for key in ("user.name", "user.email"):
        try:
            value = git(project, "config", "--get", key)
        except RuntimeError:
            continue
        git(temporary, "config", key, value)
    temporary.rename(work)
    return work


def verify_candidate(work: Path, base: str, task_id: str) -> dict:
    current = head(work)
    if current == base:
        raise RuntimeError("DSH 尚未产生代码提交。")
    if not clean(work):
        raise RuntimeError("DSH 工作区仍有未提交内容。")
    git(work, "merge-base", "--is-ancestor", base, current)
    if git(work, "rev-list", "--merges", base + ".." + current):
        raise RuntimeError("任务提交不能包含合并或历史改写。")
    rows = git(work, "log", "--reverse", "--format=%H%x09%s", base + ".." + current).splitlines()
    if not rows or any(row.split("\t", 1)[-1] != task_id for row in rows):
        raise RuntimeError("任务的所有 commit 标题必须仅为实际任务编号。")
    files = [x for x in git(work, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", base, current).split("\0") if x]
    if not files:
        raise RuntimeError("提交没有实际文件变化。")
    return {"head_sha": current, "base_sha": base, "commits": [x.split("\t")[0] for x in rows], "files": files}


def publish(work: Path, config: dict, expected_head: str):
    """An interrupted push is reconciled against the remote before retrying."""
    if head(work) != expected_head or not clean(work):
        raise RuntimeError("推送前提交或工作区发生变化。")
    remote, branch = config.get("remote", "origin"), config["branch"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", remote):
        raise ValueError("remote 必须是 Git remote 名称。")
    target = "refs/heads/" + branch
    before = git(work, "ls-remote", "--refs", remote, target, timeout=60)
    if before and before.split()[0] == expected_head:
        return {"head_sha": expected_head, "remote": remote, "branch": branch, "already_published": True}
    git(work, "push", "--porcelain", remote, expected_head + ":" + target, timeout=300)
    after = git(work, "ls-remote", "--refs", remote, target, timeout=60)
    if not after or after.split()[0] != expected_head:
        raise RuntimeError("推送后无法核对远端提交。")
    return {"head_sha": expected_head, "remote": remote, "branch": branch, "already_published": False}
