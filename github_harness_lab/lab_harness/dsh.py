"""Official headless CLI adapter. No task is sent without an explicit authorization gate."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
from typing import Any

from .process import run_process, safe_environment


class DshError(RuntimeError):
    pass


def parse_events(path: Path, *, exit_code: int, requested_session: str | None = None) -> dict[str, Any]:
    """Only the documented new-format terminal contract is accepted; prose cannot pass."""
    session = None
    final = None
    saw_error = False
    event_count = 0
    try:
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise DshError("Malformed DSH event")
            if final is not None:
                raise DshError("DSH emitted data after its terminal final event")
            event_count += 1
            if event["type"] == "session":
                if event_count != 1 or session is not None:
                    raise DshError("DSH must open with exactly one session event")
                session = event.get("sessionId")
                if not isinstance(session, str) or not session.strip():
                    raise DshError("DSH session identifier is missing")
            elif event["type"] == "final":
                final = event
            elif event["type"] == "error":
                saw_error = True
    except (OSError, UnicodeError, ValueError) as error:
        raise DshError("Cannot read valid DSH NDJSON output") from error
    if exit_code != 0 or saw_error or final is None or session is None:
        raise DshError("DSH failed or did not emit a complete session/final stream")
    if final.get("sessionId") != session or (requested_session is not None and session != requested_session):
        raise DshError("DSH final/requested session does not match")
    reason = final.get("reason")
    if not isinstance(reason, dict) or reason.get("kind") != "completed":
        raise DshError("DSH final reason is not completed")
    if not isinstance(final.get("text"), str):
        raise DshError("DSH terminal response must be a string")
    # Model prose and raw tool events are deliberately excluded from machine evidence.
    return {"session_id": session, "exit_code": exit_code, "final_kind": reason["kind"],
            "event_count": event_count}


class DshRunner:
    def __init__(self, config: dict[str, Any], *, authorized: bool = False):
        self.config = config
        self.authorized = authorized is True

    def command(self, session_id: str | None = None) -> list[str]:
        prefix = self.config.get("command", [self.config.get("executable", "dsh")])
        if not isinstance(prefix, list) or not prefix or not all(isinstance(v, str) and v for v in prefix):
            raise DshError("dsh.command must be a nonempty argv list")
        # A node runtime plus official JS CLI entrypoint is useful for Windows npm shims.
        if len(prefix) > 2 or (len(prefix) == 2 and Path(prefix[1]).suffix.lower() not in {".js", ".mjs", ".cjs"}):
            raise DshError("Only a native CLI or native runtime plus JS CLI entrypoint is supported")
        argv = [*prefix, "--profile", "headless", "--permission-mode", "workspace-write",
                "--approval-policy", "never", "--json"]
        if session_id is not None:
            if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", session_id):
                raise DshError("Invalid persisted session identifier")
            argv.extend(["--session-id", session_id])
        argv.append("-")
        return argv

    def run(self, prompt: str, cwd: Path, output_dir: Path, session_id: str | None = None) -> dict[str, Any]:
        if not self.authorized:
            raise DshError("Actual model sending is disabled; explicit deployment authorization is required")
        if not isinstance(prompt, str) or not prompt.strip():
            raise DshError("A nonempty task prompt is required")
        cwd = Path(cwd).resolve()
        home = Path(self.config.get("home", "")).expanduser()
        if not self.config.get("home") or not home.is_absolute() or not home.is_dir():
            raise DshError("An existing, explicit, isolated dsh.home directory is required")
        home = home.resolve()
        if home == cwd or home.is_relative_to(cwd):
            raise DshError("DSH_HOME must be outside the agent workspace")
        if (cwd / ".env").exists():
            raise DshError("Workspace .env would override isolated credentials; remove it before execution")
        if self.config.get("budget_enforced_externally") is not True:
            raise DshError("A verified external API spending cap is required; a timeout is not a cost cap")
        model = self.config.get("model")
        version = self.config.get("verified_version")
        if not isinstance(model, str) or not model.strip() or not isinstance(version, str) or not version.strip():
            raise DshError("Record verified model selection and harness version before live use")
        environment = {"DSH_HOME": str(home)}
        for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"):
            if os.environ.get(key):
                environment[key] = os.environ[key]
        if "DEEPSEEK_API_KEY" not in environment:
            raise DshError("DEEPSEEK_API_KEY must be supplied only in the launch environment")
        result = run_process(self.command(session_id), cwd=cwd, output_dir=Path(output_dir),
                             timeout_seconds=float(self.config.get("timeout_seconds", 3600)),
                             environment=safe_environment(extra=environment), stdin=prompt.encode("utf-8"),
                             max_output_bytes=int(self.config.get("max_output_bytes", 16 * 1024 * 1024)),
                             secrets=[environment["DEEPSEEK_API_KEY"]])
        if result.timed_out or result.output_limit_exceeded:
            raise DshError("DSH hit its time/output limit; the process tree was stopped")
        parsed = parse_events(result.stdout_path, exit_code=result.exit_code, requested_session=session_id)
        parsed.update(model=model, version=version, model_identity_source="operator_verified_profile",
                      stdout_path=str(result.stdout_path), stderr_path=str(result.stderr_path))
        return parsed

    def doctor(self) -> dict[str, Any]:
        """Read-only inventory. Does not even invoke dsh --help automatically."""
        command = self.command()
        executable = shutil.which(command[0]) or (str(Path(command[0]).resolve()) if Path(command[0]).is_file() else None)
        return {"executable": executable, "available": executable is not None,
                "command_preview": command, "sends_model_request": False,
                "help_command": [*self.config.get("command", [self.config.get("executable", "dsh")]), "--profile", "headless", "--help"],
                "version_command": [*self.config.get("command", [self.config.get("executable", "dsh")]), "--version"]}
