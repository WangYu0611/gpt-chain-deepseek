from __future__ import annotations

import json
import re
from pathlib import PurePosixPath


class ProtocolError(RuntimeError):
    pass


def repository_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value or ":" in value:
        raise ProtocolError("证据和任务路径必须是仓库内的相对路径，使用 / 分隔。")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in (".", "..") for p in value.split("/")) or value.startswith("-"):
        raise ProtocolError(f"不允许的仓库路径：{value}")
    return str(path)


def commit_sha(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
        raise ProtocolError("需要完整的提交 SHA，不能使用 main 或缩写。")
    return value.lower()


def extract_block(text: str, start: str, end: str) -> dict:
    first = text.rfind(start)
    last = text.find(end, first + len(start))
    if first < 0 or last < 0:
        raise ProtocolError(f"回复缺少完整的 {start} … {end} JSON 交接块。")
    content = text[first + len(start):last].strip()
    # ChatGPT's rendered innerText can include the language and copy controls
    # around a code block. Accept only these exact display labels at the edges;
    # never search for braces through arbitrary explanatory text.
    label = re.compile(r"(?:(?:json|copy code|copied|copy|复制代码|已复制|复制)[ \t]*)+", re.I)
    lines = content.splitlines()
    while lines and (not lines[0].strip() or label.fullmatch(lines[0].strip())):
        lines.pop(0)
    while lines and (not lines[-1].strip() or label.fullmatch(lines[-1].strip())):
        lines.pop()
    content = "\n".join(lines).strip()
    fence = re.match(r"^(```|~~~)(?:json)?[ \t]*\n", content, re.I)
    if fence:
        closing = fence.group(1)
        body = content[fence.end():]
        if not re.search(r"\n" + re.escape(closing) + r"[ \t]*$", body):
            raise ProtocolError("JSON 代码块缺少匹配的结束标记。")
        content = re.sub(r"\n" + re.escape(closing) + r"[ \t]*$", "", body).strip()
    try:
        result = json.loads(content)
    except json.JSONDecodeError as error:
        raise ProtocolError("交接块不是有效 JSON，请修正后重试。") from error
    if not isinstance(result, dict):
        raise ProtocolError("交接块必须为 JSON 对象。")
    return result


def parse_delivery(answer: str, expected_task: str | None = None) -> dict:
    result = extract_block(answer, "【交付】", "【交付结束】")
    task_id = result.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip() or (expected_task and task_id != expected_task):
        raise ProtocolError("DSH 交付任务编号缺失或与派发任务不一致。")
    result["commit_sha"] = commit_sha(result.get("commit_sha"))
    for field in ("report_paths", "screenshot_paths"):
        paths = result.get(field, [])
        if not isinstance(paths, list) or not all(isinstance(x, str) for x in paths):
            raise ProtocolError(f"{field} 必须是路径数组。")
        result[field] = list(dict.fromkeys(repository_path(p) for p in paths))
    return result


def parse_handoff(answer: str, relay_id: str, reviewed_sha: str) -> dict:
    result = extract_block(answer, "【交接】", "【交接结束】")
    actual_sha = result.get("reviewed_sha")
    if result.get("relay_id") != relay_id or not isinstance(actual_sha, str) or actual_sha.lower() != reviewed_sha.lower():
        raise ProtocolError("GPT 交接编号或审阅版本不匹配，拒绝使用旧回复。")
    action = result.get("action")
    if action not in ("execute", "revise", "pause", "done"):
        raise ProtocolError("action 必须为 execute、revise、pause 或 done。")
    if action in ("execute", "revise"):
        if not isinstance(result.get("task_id"), str) or not result["task_id"].strip():
            raise ProtocolError("GPT 未提供任务编号。")
        result["task_path"] = repository_path(result.get("task_path"))
        result["published_sha"] = commit_sha(result.get("published_sha"))
    return result
