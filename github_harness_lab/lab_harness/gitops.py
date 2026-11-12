"""Isolated local clones; no fetch, push or credential operations here."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .policy import PolicyError, check_changes, safe_path


def git(root: Path, *args: str, timeout: int = 60) -> str:
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT"}}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "GIT_OPTIONAL_LOCKS": "0"})
    command = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", "-c", "protocol.ext.allow=never", "-c", "diff.external=", "-c", "user.name=Harness Lab", "-c", "user.email=harness-lab@localhost", "-C", str(root), *args]
    result = subprocess.run(command, env=env, capture_output=True, timeout=timeout, encoding="utf-8", errors="replace")
    if result.returncode:
        raise PolicyError(f"git {args[0]} failed (exit {result.returncode})")
    # A -z path listing must preserve leading spaces and control characters so
    # policy can reject the exact name rather than inspect a different file.
    return result.stdout if "-z" in args else result.stdout.strip()


def clone_local(source: Path, destination: Path, branch: str) -> str:
    source = source.resolve()
    if not source.is_dir() or not (source / ".git").is_dir():
        raise PolicyError("source must be a trusted local Git clone with a .git directory")
    if destination.exists():
        raise PolicyError("clone destination already exists; recovery must reuse its journal")
    destination.parent.mkdir(parents=True, exist_ok=True)
    git(destination.parent, "clone", "--no-hardlinks", "--no-local", "--no-checkout", "--", str(source), str(destination))
    git(destination, "checkout", "-B", branch, "HEAD")
    # Publication uses a separate, privileged process; the agent clone has no remote credentials.
    git(destination, "remote", "remove", "origin")
    return git(destination, "rev-parse", "HEAD")


def changed_files(root: Path, base: str) -> list[str]:
    # -z preserves spaces, newlines and Unicode names. Rename is represented as delete/add.
    tracked = git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", base).strip("\x00").split("\x00")
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z").strip("\x00").split("\x00")
    return sorted({p for p in tracked + untracked if p})


def metadata_fingerprint(root: Path) -> dict[str, bytes]:
    # Agent must leave repository metadata alone; controller owns commits and publication.
    entries = ["config", "config.worktree", "HEAD", "packed-refs", "shallow", "info/grafts",
               "info/attributes", "info/exclude", "objects/info/alternates"]
    metadata = root / ".git"

    def linked(path: Path) -> bool:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())

    def contents(path: Path) -> bytes:
        for parent in (path, *path.parents):
            if linked(parent):
                raise PolicyError("Git metadata links are forbidden")
            if parent == metadata:
                break
        if not path.exists():
            return b""
        if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise PolicyError("unexpected or oversized Git metadata")
        return path.read_bytes()

    if linked(metadata):
        raise PolicyError("Git metadata links are forbidden")
    result = {p: contents(metadata / p) for p in entries}
    for name in ("refs", "hooks"):
        directory = metadata / name
        if linked(directory):
            raise PolicyError("Git metadata links are forbidden")
        if directory.exists():
            for path in directory.rglob("*"):
                if len(result) > 10000:
                    raise PolicyError("unexpected Git metadata volume")
                if linked(path):
                    raise PolicyError("Git metadata links are forbidden")
                if path.is_file():
                    result[path.relative_to(metadata).as_posix()] = contents(path)
    return result


def verify_metadata(root: Path, original: dict[str, bytes], base: str) -> None:
    if metadata_fingerprint(root) != original or git(root, "rev-parse", "HEAD") != base:
        raise PolicyError("agent changed Git metadata or committed outside controller")
    index = git(root, "ls-files", "--stage")
    if any(line.startswith(("120000 ", "160000 ")) for line in index.splitlines()):
        raise PolicyError("symlinks and submodules need an explicitly reviewed deployment")
    flags = git(root, "ls-files", "-v", "-z")
    if any(entry[0].islower() or entry[0] == "S" for entry in flags.split("\x00") if entry):
        raise PolicyError("assume-unchanged and skip-worktree index flags are forbidden")
    if git(root, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--name-only", "-z", base):
        raise PolicyError("agent staged changes; controller exclusively owns the index")
    # A fingerprint cannot compare all object bytes at reasonable memory cost.
    # Verify hashes and reachability instead; this detects rewritten loose/pack
    # objects. Unreachable *valid* objects may still be created by local Git.
    git(root, "fsck", "--full", "--strict", "--no-reflogs", timeout=120)


def commit(root: Path, files: list[str], task_id: str, *, task: dict | None = None) -> str:
    if not files:
        raise PolicyError("no code changes; cannot publish a fabricated completion")
    # Never inherit the agent's index. A hidden staged forbidden blob can differ
    # from both HEAD and the working tree while `git diff HEAD` looks harmless.
    files = [safe_path(file) for file in files]
    if task is not None:
        check_changes(root, files, task)
    git(root, "reset", "--mixed", "HEAD", "--")
    git(root, "add", "--", *files)
    staged = {p for p in git(root, "diff", "--cached", "--no-ext-diff", "--no-textconv",
                            "--no-renames", "--name-only", "-z").split("\x00") if p}
    if staged != set(files):
        raise PolicyError("staged files differ from the verified change list")
    if task is not None:
        check_changes(root, sorted(staged), task)
    # EOL normalization and clean filters can make the index differ from the
    # tested working tree. Fail closed rather than publish untested bytes.
    for file in staged:
        target = root / file
        if target.exists():
            expected = git(root, "hash-object", "--no-filters", "--", file)
            actual = git(root, "rev-parse", ":" + file)
            if expected != actual:
                raise PolicyError("staged blob differs from the tested working tree")
    git(root, "commit", "--no-verify", "-m", f"agent: {task_id}")
    return git(root, "rev-parse", "HEAD")
