from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .config import validate_remote
from .protocol import ProtocolError, commit_sha, repository_path


class GitError(RuntimeError):
    pass


def remote_identity(url: str) -> tuple[str, str]:
    url = url.strip().rstrip("/")
    if url.startswith("git@") and ":" in url:
        host, path = url[4:].split(":", 1)
    else:
        parts = urlsplit(url)
        host, path = parts.hostname or "", parts.path
    return host.lower(), path.strip("/").removesuffix(".git").lower()


class GitRepository:
    def __init__(self, config: dict):
        self.config = config
        self.path = Path(config["workdir"]).resolve()
        self.url = config["url"]
        self.branch = config.get("branch", "main")
        self.git = config.get("git_executable", "git")
        self.allow_local = config.get("allow_local_remote", False)

    def command(self, *args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
        result = subprocess.run(
            [self.git, "-c", "protocol.ext.allow=never", *args],
            cwd=cwd or self.path, env=env, capture_output=True, timeout=180, shell=False,
        )
        if check and result.returncode:
            message = result.stderr.decode("utf-8", errors="replace")[-3000:]
            raise GitError(f"Git 操作失败（{args[0]}）：{message}")
        return result

    def text(self, *args: str) -> str:
        return self.command(*args).stdout.decode("utf-8", errors="strict").strip()

    def ensure(self) -> None:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.command("clone", "--single-branch", "--branch", self.branch, "--", self.url, str(self.path), cwd=self.path.parent)
        if not (self.path / ".git").exists():
            raise GitError("项目目录已存在但不是 Git 工作区。请在配置中指定正确路径。")
        self.verify_origin()

    def verify_origin(self) -> None:
        origin = self.text("remote", "get-url", "origin")
        effective = self.text("ls-remote", "--get-url", "origin")
        push_urls = self.text("remote", "get-url", "--push", "--all", "origin").splitlines()
        if self.allow_local:
            if any(Path(candidate).resolve() != Path(self.url).resolve() for candidate in [origin, effective, *push_urls]):
                raise GitError("测试仓库 origin 与配置不符。")
            return
        validate_remote(self.url)
        for candidate in (origin, effective, *push_urls):
            validate_remote(candidate)
            if remote_identity(candidate) != remote_identity(self.url):
                raise GitError("项目 origin 或实际 Git 传输目标与配置不一致，已暂停。")

    def fetch(self) -> str:
        self.verify_origin()
        self.command("fetch", "--no-tags", "origin", f"+refs/heads/{self.branch}:refs/remotes/origin/{self.branch}")
        return self.text("rev-parse", f"refs/remotes/origin/{self.branch}")

    def is_ancestor(self, first: str, second: str) -> bool:
        result = self.command("merge-base", "--is-ancestor", commit_sha(first), commit_sha(second), check=False)
        if result.returncode not in (0, 1):
            raise GitError("无法验证提交之间的关系。")
        return result.returncode == 0

    def require_published(self, sha: str, remote_head: str | None = None) -> str:
        sha = commit_sha(sha)
        head = remote_head or self.fetch()
        if not self.is_ancestor(sha, head):
            raise GitError("交付提交不在已配置远端分支上；尚未推送或版本错误。")
        return head

    def sync(self, expected_sha: str) -> str:
        head = self.fetch()
        self.require_published(expected_sha, head)
        if self.text("status", "--porcelain"):
            raise GitError("项目工作区有未提交改动。请先处理，脚本不会覆盖它们。")
        if self.text("branch", "--show-current") != self.branch:
            raise GitError(f"当前工作区不在 {self.branch} 分支，请手动确认后切换。")
        self.command("merge", "--ff-only", f"refs/remotes/origin/{self.branch}")
        if self.text("rev-parse", "HEAD") != head:
            raise GitError("本地分支存在未推送或分叉提交，无法自动同步。")
        return head

    def read_file(self, sha: str, path: str) -> bytes:
        path = repository_path(path)
        sha = commit_sha(sha)
        mode = self.text("ls-tree", sha, "--", path).split(" ", 1)[0]
        if mode not in ("100644", "100755"):
            raise GitError(f"任务或证据不是普通文件：{path}")
        data = self.command("show", f"{sha}:{path}").stdout
        if data.startswith(b"version https://git-lfs.github.com/spec/"):
            return self._read_lfs(data, path, sha)
        return data

    def _read_lfs(self, pointer: bytes, relative: str, sha: str) -> bytes:
        """Use only real bytes whose size/hash match the committed pointer."""
        oid = re.search(rb"^oid sha256:([0-9a-f]{64})$", pointer, re.M)
        size = re.search(rb"^size ([0-9]+)$", pointer, re.M)
        if not oid or not size:
            raise GitError(f"无效的 Git LFS 指针：{relative}")
        digest, expected_size = oid[1].decode(), int(size[1])
        git_dir = Path(self.text("rev-parse", "--git-common-dir"))
        if not git_dir.is_absolute():
            git_dir = self.path / git_dir
        candidates = [git_dir / "lfs" / "objects" / digest[:2] / digest[2:4] / digest]
        working_file = (self.path / relative).resolve()
        if working_file.is_relative_to(self.path):
            candidates.append(working_file)
        for candidate in candidates:
            if candidate.is_file() and candidate.stat().st_size == expected_size:
                content = candidate.read_bytes()
                if hashlib.sha256(content).hexdigest() == digest:
                    return content
        raise GitError(f"缺少 {relative} 的真实 LFS 内容。请先执行 git lfs fetch origin {sha}，再继续；不能把指针当图片。")

    def diff(self, base: str | None, sha: str) -> bytes:
        if base:
            return self.command("diff", "--no-ext-diff", "--no-textconv", commit_sha(base), commit_sha(sha), "--").stdout
        return self.command("show", "--format=fuller", "--no-ext-diff", "--no-textconv", commit_sha(sha), "--").stdout

    def verify_task(self, task: dict, delivery: dict, review_base: str | None = None) -> None:
        head = self.fetch()
        published = task["published_sha"]
        self.require_published(published, head)
        if not self.is_ancestor(delivery["commit_sha"], published):
            raise GitError("新任务发布版本早于本次交付版本。")
        baseline = review_base or delivery["commit_sha"]
        if not self.is_ancestor(baseline, published):
            raise GitError("任务发布版本不包含本轮审阅开始前的远端版本。")
        if not self.read_file(published, task["task_path"]).strip():
            raise GitError("发布的任务文件为空。")
        if self.read_file(head, task["task_path"]) != self.read_file(published, task["task_path"]):
            raise GitError("任务文件在 GPT 发布后又发生变化，已暂停等待重新确认。")
        changed = self.command("diff", "--name-only", baseline, published, "--", task["task_path"]).stdout
        if not changed.strip():
            raise GitError("GitHub 上任务文件未在本轮审阅后更新，暂不派发。")
