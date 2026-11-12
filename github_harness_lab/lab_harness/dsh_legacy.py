"""Explicit compatibility for the inspected official Desktop 0.2.1-rc.3 CLI.

This does not weaken the default adapter or wire live execution into the CLI.
The installed release reports completion in status/turn_end, not final.reason.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .dsh import DshError
from .process import run_process, safe_environment


VERIFIED_VERSION = "0.2.1-rc.3"


def parse_legacy_events(path: Path, *, exit_code: int, requested_session: str | None = None) -> dict[str, Any]:
    session = None
    active_turn = None
    last_reason = None
    final = None
    event_count = 0
    try:
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            event_count += 1
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise DshError("Malformed legacy DSH event")
            if final is not None:
                raise DshError("Data after the terminal legacy final event")
            kind = event["type"]
            if event_count == 1 and kind != "session":
                raise DshError("Legacy stream must open with a session")
            if kind == "session":
                if session is not None or event_count != 1:
                    raise DshError("Duplicate legacy session")
                session = event.get("sessionId")
                if not isinstance(session, str) or not session.strip():
                    raise DshError("Missing legacy session identity")
            elif kind == "error":
                raise DshError("Legacy DSH reported an error")
            elif kind == "status":
                if event.get("truncated"):
                    raise DshError("Truncated status cannot prove legacy completion")
                phase = event.get("phase")
                if phase in {"turn_start", "turn_end"}:
                    turn = event.get("turn")
                    if not ((type(turn) is int and turn >= 0) or (isinstance(turn, str) and turn)):
                        raise DshError("Invalid legacy turn identity")
                    if phase == "turn_start":
                        if active_turn is not None:
                            raise DshError("Overlapping legacy turns")
                        active_turn, last_reason = turn, None
                    else:
                        if active_turn is None or turn != active_turn:
                            raise DshError("Legacy turn_end does not match its turn_start")
                        reason = event.get("reason")
                        if not isinstance(reason, dict) or not isinstance(reason.get("kind"), str):
                            raise DshError("Legacy completion reason is missing")
                        active_turn, last_reason = None, reason["kind"]
                elif phase in {"step_start", "step_end"} and active_turn is None:
                    raise DshError("Legacy step is outside a turn")
            elif kind == "final":
                if event.get("truncated") or not isinstance(event.get("text"), str):
                    raise DshError("Invalid legacy final event")
                final = event
        if type(exit_code) is not int or exit_code != 0 or session is None or final is None:
            raise DshError("Legacy process failed or stream is incomplete")
        if active_turn is not None or last_reason != "completed":
            raise DshError("Legacy terminal turn did not complete")
        if requested_session is not None and requested_session != session:
            raise DshError("Legacy resumed session differs from the requested identity")
    except (OSError, UnicodeError, ValueError) as error:
        raise DshError("Cannot read valid legacy DSH NDJSON") from error
    return {"session_id": session, "exit_code": exit_code, "final_kind": last_reason,
            "event_count": event_count, "completion_source": "status.turn_end.reason",
            "protocol": "desktop-0.2.1-rc.3"}


class LegacyDshRunner:
    """Opt-in, separately gated adapter; no credentials are read by this wrapper."""
    def __init__(self, config: dict[str, Any], *, authorized: bool = False):
        self.config = config
        self.authorized = authorized is True

    def command(self, invocation_patch: Path, session_id: str | None = None) -> list[str]:
        install = Path(self.config["desktop_install"]).resolve()
        executable = install / "DeepSeek Harness.exe"
        archive = install / "resources/app.asar"
        if not executable.is_file() or not archive.is_file():
            raise DshError("The inspected official Desktop installation is missing")
        entry = archive / "dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"
        argv = [str(executable), "--expose-internals", str(entry)]
        for raw in self.config.get("patches", []):
            patch = Path(raw).resolve()
            if not patch.is_file():
                raise DshError("Trusted invocation overlay does not exist")
            argv.extend(["--patch", str(patch)])
        argv.extend(["--patch", str(invocation_patch), "--profile", "headless", "--json"])
        if session_id is not None:
            if not isinstance(session_id, str) or not session_id or "\x00" in session_id:
                raise DshError("Invalid legacy session identity")
            argv.extend(["--session-id", session_id])
        return [*argv, "-"]

    def run(self, prompt: str, cwd: Path, output_dir: Path, session_id: str | None = None) -> dict[str, Any]:
        if not self.authorized:
            raise DshError("Legacy model sending requires explicit authorization")
        if self.config.get("verified_version") != VERIFIED_VERSION:
            raise DshError("Legacy adapter is pinned to the inspected Desktop version")
        if self.config.get("isolated_environment_verified") is not True:
            raise DshError("The execution environment has not been verified as isolated")
        if self.config.get("budget_enforced_externally") is not True:
            raise DshError("A verified external spending cap is required")
        if not isinstance(prompt, str) or not prompt.strip():
            raise DshError("A nonempty prompt is required")
        cwd = Path(cwd).resolve()
        home = Path(self.config.get("home", "")).expanduser()
        if not self.config.get("home") or not home.is_absolute() or not home.is_dir():
            raise DshError("Explicit existing DSH_HOME is required")
        home = home.resolve()
        if home == cwd or home.is_relative_to(cwd) or (cwd / ".env").exists():
            raise DshError("Unsafe DSH_HOME or workspace credential override")
        if not isinstance(self.config.get("model"), str) or not self.config["model"].strip():
            raise DshError("Record the verified profile model")
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        patch = output_dir / "invocation-policy.yml"
        # Per-invocation overlay; never changes the user's installed/saved profile.
        patch.write_text("- id: sandbox-policy\n  config:\n    mode: workspace-write\n- id: approval\n  config:\n    policy: never\n- id: llm-retry\n  disabled: true\n", encoding="utf-8")
        environment = safe_environment(extra={"DSH_HOME": str(home)})
        environment.update(ELECTRON_RUN_AS_NODE="1", DSH_PERMISSION_MODE="workspace-write")
        result = run_process(self.command(patch, session_id), cwd=cwd, output_dir=output_dir,
                             timeout_seconds=float(self.config.get("timeout_seconds", 120)),
                             max_output_bytes=int(self.config.get("max_output_bytes", 1048576)),
                             environment=environment, stdin=prompt.encode("utf-8"))
        if result.timed_out or result.output_limit_exceeded:
            raise DshError("Legacy process exceeded its time or output limit")
        parsed = parse_legacy_events(result.stdout_path, exit_code=result.exit_code,
                                     requested_session=session_id)
        return parsed | {"model": self.config["model"], "version": VERIFIED_VERSION,
                         "model_identity_source": "operator_verified_profile",
                         "stdout_path": str(result.stdout_path), "stderr_path": str(result.stderr_path)}
