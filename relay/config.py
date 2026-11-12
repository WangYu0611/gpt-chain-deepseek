from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from .storage import read_json


def resolve_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def load_config(path: Path) -> dict:
    path = path.resolve()
    config = read_json(path)
    if config.get("version") != 1:
        raise ValueError("配置 version 必须为 1。")
    for key in ("repository", "browser", "dsh"):
        if not isinstance(config.get(key), dict):
            raise ValueError(f"配置缺少对象：{key}")
    runtime = resolve_path(config.get("runtime_dir", ".relay"), path.parent)
    config["runtime_dir"] = str(runtime)
    repository = config["repository"]
    repository["workdir"] = str(resolve_path(repository.get("workdir") or str(runtime / "project"), path.parent))
    branch = repository.get("branch", "main")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) or ".." in branch or "@{" in branch:
        raise ValueError("repository.branch 不是有效的分支名称。")
    repository["branch"] = branch
    workdir = Path(repository["workdir"])
    tool_dir = Path(__file__).resolve().parent.parent
    if tool_dir == workdir or tool_dir.is_relative_to(workdir):
        raise ValueError("项目工作区不能包含传话工具本身。请指定独立项目目录。")
    if runtime == workdir or runtime.is_relative_to(workdir):
        raise ValueError("runtime_dir 必须在项目仓库之外。")
    url = repository.get("url", "")
    if url:
        validate_remote(url)
    browser = config["browser"]
    if browser.get("profile_dir"):
        browser["profile_dir"] = str(resolve_path(browser["profile_dir"], path.parent))
    dsh = config["dsh"]
    for key in ("home", "patch"):
        if dsh.get(key):
            dsh[key] = str(resolve_path(dsh[key], path.parent))
    executable = dsh.get("executable", "dsh")
    if "/" in executable or "\\" in executable:
        dsh["executable"] = str(resolve_path(executable, path.parent))
    if not isinstance(dsh.get("args", []), list) or not all(isinstance(x, str) for x in dsh.get("args", [])):
        raise ValueError("dsh.args 必须为字符串数组。")
    limits = config.setdefault("limits", {})
    for key, default in (("max_cycles", 10), ("max_revisions", 3)):
        limits.setdefault(key, default)
        if type(limits[key]) is not int or limits[key] < 1:
            raise ValueError(f"limits.{key} 必须为正整数。")
    config.setdefault("evidence", {})
    config["_config_path"] = str(path)
    return config


def validate_remote(url: str) -> None:
    if url.startswith("git@") and re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+", url):
        return
    parts = urlsplit(url)
    if parts.scheme not in ("https", "ssh") or not parts.hostname or parts.password or parts.query or parts.fragment:
        raise ValueError("仓库地址须为不含密码或 token 的 HTTPS/SSH Git 地址。")
    if parts.scheme == "https" and parts.username:
        raise ValueError("请使用 Git 凭据管理器，不要把凭据写进仓库地址。")


def missing_settings(config: dict) -> list[str]:
    missing = []
    if not config["repository"].get("url"):
        missing.append("repository.url")
    if not config["browser"].get("conversation_url"):
        missing.append("browser.conversation_url（填写原 GPT 对话的完整网址）")
    if not config["dsh"].get("executable"):
        missing.append("dsh.executable")
    return missing
