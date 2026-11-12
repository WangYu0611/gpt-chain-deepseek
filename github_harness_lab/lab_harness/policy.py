"""Fail-closed input validation. These checks are NOT an operating-system sandbox."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


class PolicyError(ValueError):
    pass


PROTECTED = (".git", ".github", ".agents", ".codex", "lab_harness", "AGENTS.md")
SECRET_NAMES = (".env", ".npmrc", ".pypirc", "id_rsa", "id_ed25519")
SECRET_PATTERN = re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)")


def canonical_digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def safe_path(value: str) -> str:
    if (not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(c in value for c in '\\:<>"|?*')):
        raise PolicyError("paths must be non-empty relative POSIX paths")
    raw = value.rstrip("/")
    if not raw or raw.startswith("/") or any(x in {"", ".", ".."} for x in raw.split("/")):
        raise PolicyError("absolute, empty and traversal paths are forbidden")
    if any(x.rstrip(" .").lower() != x.lower() for x in raw.split("/")):
        raise PolicyError("ambiguous Windows path")
    if any(re.fullmatch(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", x, re.I) for x in raw.split("/")):
        raise PolicyError("reserved Windows path")
    return raw


def under(path: str, prefix: str) -> bool:
    return path.casefold() == prefix.casefold() or path.casefold().startswith(prefix.casefold() + "/")


def parse_event(event: dict, config: dict) -> dict:
    repo = event.get("repository", {}).get("full_name")
    if repo != config["repository"] or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
        raise PolicyError("repository mismatch")
    allowed = {x.casefold() for x in config.get("trusted_actors", [])}
    issue = event.get("issue", {})
    author = issue.get("user", {}).get("login", "")
    sender = event.get("sender", {}).get("login", "")
    if not author or author.casefold() not in allowed or sender.casefold() not in allowed:
        raise PolicyError("issue author and event sender must both be trusted")
    if issue.get("state") != "open" or "pull_request" in issue:
        raise PolicyError("only open Issues can be executed")
    if type(issue.get("number")) is not int or issue["number"] <= 0:
        raise PolicyError("invalid Issue number")
    labels = {x["name"] if isinstance(x, dict) else x for x in issue.get("labels", [])}
    if event.get("action") != "labeled" or event.get("label", {}).get("name") not in {"agent:ready", "agent:rework"}:
        raise PolicyError("only explicit ready/rework label events are accepted")
    current_states = labels & {"agent:ready", "agent:rework"}
    if (labels & {"agent:blocked", "agent:accepted", "agent:paused"}
            or len(current_states) != 1 or event["label"]["name"] not in current_states):
        raise PolicyError("Issue is not eligible for execution")
    blocks = re.findall(r"```agent-task\s*\n(.*?)\n```", issue.get("body", ""), re.S)
    if len(blocks) != 1:
        raise PolicyError("exactly one fenced agent-task JSON block is required")
    try:
        task = json.loads(blocks[0])
    except json.JSONDecodeError as exc:
        raise PolicyError("invalid task JSON") from exc
    if not isinstance(task, dict) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", task.get("task_id", "")):
        raise PolicyError("invalid task_id")
    for name in ("allowed_paths", "forbidden_paths", "requirements", "acceptance", "evidence", "test_ids", "depends_on"):
        if not isinstance(task.get(name), list):
            raise PolicyError(f"task.{name} must be a list")
    for name in ("allowed_paths", "requirements", "acceptance", "evidence", "test_ids"):
        if not task[name]:
            raise PolicyError(f"task.{name} cannot be empty")
    for name in ("risk", "rollback"):
        if not isinstance(task.get(name), str) or not task[name].strip():
            raise PolicyError(f"task.{name} is required")
    for name in ("allowed_paths", "forbidden_paths"):
        task[name] = [safe_path(p) for p in task[name]]
    if any(type(x) is not int or x <= 0 or x == issue["number"] for x in task["depends_on"]):
        raise PolicyError("invalid dependencies")
    if not all(isinstance(x, str) and x.strip() for x in task["requirements"]):
        raise PolicyError("requirements must be non-empty strings")
    for name, field in (("acceptance", "criterion"), ("evidence", "requirement")):
        if not all(isinstance(x, dict) and isinstance(x.get("id"), str) and x["id"] and isinstance(x.get(field), str) and x[field].strip() for x in task[name]):
            raise PolicyError(f"invalid {name}")
        if len({x["id"] for x in task[name]}) != len(task[name]):
            raise PolicyError(f"duplicate {name} IDs")
    approved = {x["id"] for x in config.get("test_commands", [])}
    if any(not isinstance(x, str) or x not in approved for x in task["test_ids"]):
        raise PolicyError("Issue references an unapproved test command")
    return {"issue": issue["number"], "repository": repo, "task": task, "action": event["label"]["name"], "event_id": canonical_digest(event)}


def check_changes(root: Path, files: list[str], task: dict) -> None:
    for file in files:
        p = safe_path(file)
        if any(under(p, x) for x in (*PROTECTED, *task["forbidden_paths"])):
            raise PolicyError(f"protected path changed: {p}")
        if any(part.casefold() in {x.casefold() for x in SECRET_NAMES}
               or part.casefold().startswith(".env.")
               or part.casefold().endswith((".pem", ".key", ".pfx")) for part in PurePosixPath(p).parts):
            raise PolicyError(f"sensitive path changed: {p}")
        if not any(under(p, x) for x in task["allowed_paths"]):
            raise PolicyError(f"outside allowed scope: {p}")
        target = root / p
        if any(parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()) for parent in (target, *target.parents) if parent != root.parent):
            raise PolicyError("symlink / junction paths cannot be published")
        if target.exists():
            if not target.is_file() or target.stat().st_size > 8 * 1024 * 1024:
                raise PolicyError("non-file or oversized change requires human review")
            if SECRET_PATTERN.search(target.read_bytes()):
                raise PolicyError(f"possible secret in {p}")


def require_live(config: dict, scope: str) -> dict:
    """A local authorization artifact must be supplied by a human outside this run."""
    if config.get("live_enabled") is not True:
        raise PolicyError("live_enabled is false; no external execution or sending is authorized")
    path = config.get("authorization_file")
    if not path or not Path(path).is_file():
        raise PolicyError("explicit user authorization file is missing")
    auth = json.loads(Path(path).read_text(encoding="utf-8"))
    expires = datetime.fromisoformat(auth.get("expires_at", "").replace("Z", "+00:00"))
    if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
        raise PolicyError("authorization expired")
    scopes = auth.get("scopes")
    if not isinstance(scopes, list) or not all(isinstance(item, str) for item in scopes):
        raise PolicyError("authorization scopes must be a string array")
    if auth.get("repository") != config.get("repository") or scope not in scopes:
        raise PolicyError("authorization repository or scope mismatch")
    for name in ("isolated_environment_verified", "provider_hard_budget_confirmed"):
        if auth.get(name) is not True:
            raise PolicyError(f"live execution blocked: {name}")
    if not isinstance(auth.get("budget_limit_usd"), (int, float)) or isinstance(auth.get("budget_limit_usd"), bool) or not 0 < auth["budget_limit_usd"] <= config["limits"]["max_budget_usd"]:
        raise PolicyError("invalid authorized budget")
    # These attestations do not implement isolation/quota: a deployer must supply and test them.
    return auth
