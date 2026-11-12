"""Crash-safe local files and OS-owned locks (released after process death)."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
import uuid


_ATOMIC_REPLACE_RETRY_DELAYS = (0.02, 0.05, 0.1, 0.2, 0.4, 0.8, 1.0)


def is_windows_sharing_error(error: BaseException) -> bool:
    """Windows file-busy/access errors eligible for bounded retry.

    Error 5 can also indicate a lasting permission problem, so callers must
    not infer eventual success from this predicate. Plain POSIX EACCES and
    unrelated Windows errors are deliberately excluded.
    """
    code = getattr(error, "winerror", None)
    return isinstance(error, OSError) and type(code) is int and code in {5, 32, 33}


def atomic_replace(source: Path, destination: Path) -> None:
    """Atomically replace once readers release a short Windows file lock.

    Reuse the already flushed temporary file. Never remove or rewrite the
    destination to work around sharing violations; readers retain old-or-new
    contents, and a lasting failure raises its original OS exception.
    """
    for attempt in range(len(_ATOMIC_REPLACE_RETRY_DELAYS) + 1):
        try:
            os.replace(source, destination)
            return
        except OSError as error:
            if not is_windows_sharing_error(error) or attempt == len(_ATOMIC_REPLACE_RETRY_DELAYS):
                raise
            time.sleep(_ATOMIC_REPLACE_RETRY_DELAYS[attempt])


def read_json(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return data


def write_text(path: Path, value: str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        atomic_replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, data: dict):
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


@contextmanager
def lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("该项目已有执行器运行，不能并发启动。") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
