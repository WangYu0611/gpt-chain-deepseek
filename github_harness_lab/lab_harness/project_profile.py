"""Repository-independent, inert policy for two bounded Markdown tasks.

A profile describes work; it never supplies permission to execute that work.
This module performs no network, process, credential or project-content access.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import unicodedata

from .policy import PROTECTED, SECRET_NAMES, safe_path
from .github import GitHubClient
from .repository_setup import RepositorySettings, normalize_repository, validate_settings

SCHEMA = "harness-project/v1"
MAX_PROFILE_BYTES = 64 * 1024
_PROFILE_FIELDS = {"schema", "repository", "default_branch", "trusted_actor", "source_repository",
                   "capability", "live_enabled", "runner_label", "document_tasks", "stop_task"}
_TASK_FIELDS = {"number", "task_id", "document_path", "report_path", "prompt", "validation"}
_VALIDATION_FIELDS = {"first_line", "required_headings", "min_chars", "max_chars", "bullet_count"}
_TASK_ID = re.compile(r"TASK-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\Z", re.ASCII)
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z", re.ASCII)
_HEADING = re.compile(r"^ {0,3}(#{1,6}) +(.+?)(?: +#+)? *$", re.ASCII)
_URI = re.compile(r"(?i)\b(?:https?|ftp|file|mailto|data|javascript):|\bwww\.|(?<!:)//[A-Za-z0-9]")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _object(value, fields: set[str], label: str) -> dict:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} must contain exactly: {', '.join(sorted(fields))}")
    return value


def _text(value, label: str, *, multiline: bool = False) -> str:
    if not isinstance(value, str) or not value or any(
        (unicodedata.category(char).startswith("C") and not (multiline and char == "\n"))
        or unicodedata.category(char) in {"Zl", "Zp"} for char in value
    ):
        raise ValueError(f"{label} must be nonempty text without control or invisible characters")
    return value


def _task_id(value) -> str:
    if not isinstance(value, str) or len(value) > 100 or not _TASK_ID.fullmatch(value):
        raise ValueError("task_id must be an ASCII TASK- identifier with hyphen-separated words")
    return value


def _document_path(value) -> str:
    path = safe_path(value)
    if path != value or len(path) > 240 or not path.endswith(".md"):
        raise ValueError("document/report paths must be ordinary relative .md files")
    parts = path.split("/")
    if any(not _SEGMENT.fullmatch(part) for part in parts):
        raise ValueError("document/report paths must use unambiguous ASCII segments")
    protected = {name.casefold() for name in (*PROTECTED, *SECRET_NAMES)}
    if any(part.casefold() in protected or part.casefold().endswith((".pem", ".key", ".pfx"))
           for part in parts):
        raise ValueError("document/report paths cannot target protected or sensitive locations")
    return path


def _plain_markdown(text: str) -> None:
    _text(text, "Markdown", multiline=True)
    if (any(token in text for token in ("<", ">", "```", "~~~"))
            or _URI.search(text) or _EMAIL.search(text)
            or re.search(r"!?\[[^\]\n]*\]\s*[\[(]", text)
            or re.search(r"(?m)^ {0,3}\[[^\]\n]+\]:", text)):
        raise ValueError("Markdown must not contain HTML, fenced code or links")


def repository_namespace(repository: str) -> str:
    repository = normalize_repository(repository)
    return hashlib.sha256(repository.lower().encode("utf-8")).hexdigest()[:12]


def validate_profile(profile: dict) -> dict:
    """Return a normalized independent copy, rejecting active or extended profiles."""
    _object(profile, _PROFILE_FIELDS, "profile")
    if profile["schema"] != SCHEMA or profile["capability"] != "bounded-document":
        raise ValueError("Only harness-project/v1 bounded-document profiles are supported")
    if profile["live_enabled"] is not False:
        raise ValueError("Profiles must have live_enabled=false; activation is separate")
    settings = validate_settings(RepositorySettings(
        repository=profile["repository"], default_branch=profile["default_branch"],
        trusted_actor=profile["trusted_actor"], source_repository=""))
    GitHubClient._branch(settings.default_branch)
    source = profile["source_repository"]
    if source != "":
        _text(source, "source_repository")
        if len(source) > 2048 or not (PureWindowsPath(source).is_absolute() or PurePosixPath(source).is_absolute()):
            raise ValueError("source_repository must be empty or an absolute path")
    if not isinstance(source, str):
        raise ValueError("source_repository must be text")
    expected_label = "harness-" + repository_namespace(settings.repository)
    if profile["runner_label"] != expected_label:
        raise ValueError("runner_label must be the repository-specific harness namespace")
    tasks = profile["document_tasks"]
    if type(tasks) is not list or len(tasks) != 2:
        raise ValueError("Exactly two document tasks are required")
    normalized_tasks, numbers, identities, paths = [], set(), set(), []
    for task in tasks:
        _object(task, _TASK_FIELDS, "document task")
        number = task["number"]
        if type(number) is not int or number not in (1, 2) or number in numbers:
            raise ValueError("Executable task numbers must be exactly 1 and 2")
        numbers.add(number)
        identity = _task_id(task["task_id"])
        if identity.casefold() in identities:
            raise ValueError("Task identifiers must be unique, including case")
        identities.add(identity.casefold())
        document = _document_path(task["document_path"])
        report = _document_path(task["report_path"])
        paths.extend((document.casefold(), report.casefold()))
        prompt = _text(task["prompt"], "prompt", multiline=True)
        if len(prompt.encode("utf-8")) > 4096:
            raise ValueError("Model prompt must be at most 4096 UTF-8 bytes")
        validation = _object(task["validation"], _VALIDATION_FIELDS, "validation")
        if validation["first_line"] != "# " + identity:
            raise ValueError("first_line must identify the exact task: '# ' plus task_id")
        headings = validation["required_headings"]
        if type(headings) is not list or not 1 <= len(headings) <= 8:
            raise ValueError("One to eight required headings are needed")
        for heading in headings:
            _text(heading, "required heading")
            _plain_markdown(heading)
            if (not heading.startswith("## ") or not heading[3:].strip()
                    or heading != heading.strip() or len(heading) > 160
                    or heading.endswith("#")):
                raise ValueError("Required headings must be canonical '## ' section headings")
        if len({heading.casefold() for heading in headings}) != len(headings):
            raise ValueError("Required headings must be unique")
        minimum, maximum = validation["min_chars"], validation["max_chars"]
        if type(minimum) is not int or type(maximum) is not int or not 80 <= minimum <= maximum <= 3000:
            raise ValueError("Document character limits must satisfy 80 <= min <= max <= 3000")
        bullets = validation["bullet_count"]
        if bullets is not None and (type(bullets) is not int or not 0 <= bullets <= 30):
            raise ValueError("bullet_count must be null or an integer from 0 to 30")
        normalized_tasks.append({"number": number, "task_id": identity,
                                 "document_path": document, "report_path": report,
                                 "prompt": prompt, "validation": copy.deepcopy(validation)})
    if len(set(paths)) != len(paths) or any(
        left.startswith(right + "/") or right.startswith(left + "/")
        for index, left in enumerate(paths) for right in paths[index + 1:]
    ):
        raise ValueError("Document and report paths must be distinct and cannot overlap")
    stop = _object(profile["stop_task"], {"number", "task_id", "dispatch"}, "stop task")
    if type(stop["number"]) is not int or stop["number"] != 3 or stop["dispatch"] is not False:
        raise ValueError("Task 3 must be publish-only with dispatch=false")
    stop_id = _task_id(stop["task_id"])
    if stop_id.casefold() in identities:
        raise ValueError("Stop task identifier must be unique")
    return {"schema": SCHEMA, "repository": settings.repository,
            "default_branch": settings.default_branch, "trusted_actor": settings.trusted_actor,
            "source_repository": source, "capability": "bounded-document", "live_enabled": False,
            "runner_label": expected_label,
            "document_tasks": sorted(normalized_tasks, key=lambda task: task["number"]),
            "stop_task": {"number": 3, "task_id": stop_id, "dispatch": False}}


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON keys are forbidden")
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError("Non-finite JSON values are forbidden")


def load_profile(path: Path) -> dict:
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_PROFILE_BYTES + 1)
    if len(raw) > MAX_PROFILE_BYTES:
        raise ValueError("Project profile exceeds 64 KiB")
    try:
        profile = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_keys,
                             parse_constant=_invalid_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Project profile must be valid UTF-8 JSON") from error
    return validate_profile(profile)


def profile_digest(profile: dict) -> str:
    normalized = validate_profile(profile)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def task_spec(profile: dict, number: int) -> dict:
    if type(number) is not int or number not in (1, 2):
        raise ValueError("Only tasks 1 and 2 are executable; task 3 is publish-only")
    return validate_profile(profile)["document_tasks"][number - 1]


def validate_document(profile: dict, number: int, text: str) -> str:
    task = task_spec(profile, number)
    if not isinstance(text, str):
        raise ValueError("Document must be text")
    normalized = text.replace("\r\n", "\n")
    _plain_markdown(normalized)
    normalized = normalized.strip() + "\n"
    validation = task["validation"]
    if not validation["min_chars"] <= len(normalized.rstrip("\n")) <= validation["max_chars"]:
        raise ValueError("Document length is outside its configured bounds")
    lines = normalized.splitlines()
    if lines[0] != validation["first_line"]:
        raise ValueError("Document first line differs from the task")
    headings = []
    for line in lines:
        match = _HEADING.fullmatch(line)
        if match:
            headings.append(match[1] + " " + match[2].strip())
    for heading in [validation["first_line"], *validation["required_headings"]]:
        if headings.count(heading) != 1:
            raise ValueError("First line and required sections must each appear exactly once")
    expected_bullets = validation["bullet_count"]
    if expected_bullets is not None:
        bullets = re.findall(r"(?m)^ {0,3}[-+*] +\S.*$", normalized)
        if (len(bullets) != expected_bullets or any(not line.startswith("- ") for line in bullets)
                or re.search(r"(?m)^ {0,3}[0-9]+[.)] +\S", normalized)):
            raise ValueError("Document must contain the configured number of '- ' bullet items")
    return normalized


def make_profile(repository: str, default_branch: str, trusted_actor: str, *, source_repository: str = "") -> dict:
    repository = normalize_repository(repository)
    namespace = repository_namespace(repository)
    stem = "TASK-HARNESS-" + namespace.upper()
    tasks = []
    for number in (1, 2):
        identity = f"{stem}-{number:02d}"
        headings = ["## 验证范围", "## 完成条件"] if number == 1 else ["## 三项检查"]
        request = ("写一份中文接入说明，说明本次仅验证 GPT、GitHub 与官方 DSH 的文档交接，不声明项目开发完成。"
                   if number == 1 else "写一份中文检查清单，恰好三条以“- ”开头，分别检查首份报告、第二份执行证据和第三项只发布不执行。")
        prompt = (request + f"第一行必须为 {identity!r} 对应的 Markdown 标题：# {identity}。"
                  + "必须包含以下小节：" + "、".join(headings)
                  + "。正文 80—1500 字。只输出 Markdown，不含代码围栏、HTML、链接或其他任务内容。")
        tasks.append({"number": number, "task_id": identity,
                      "document_path": f"Docs/Harness/{identity}.md",
                      "report_path": f"Docs/Reports/{identity}.md", "prompt": prompt,
                      "validation": {"first_line": "# " + identity, "required_headings": headings,
                                     "min_chars": 80, "max_chars": 1500,
                                     "bullet_count": None if number == 1 else 3}})
    return validate_profile({"schema": SCHEMA, "repository": repository, "default_branch": default_branch,
                             "trusted_actor": trusted_actor, "source_repository": source_repository,
                             "capability": "bounded-document", "live_enabled": False,
                             "runner_label": "harness-" + namespace, "document_tasks": tasks,
                             "stop_task": {"number": 3, "task_id": stem + "-03", "dispatch": False}})
