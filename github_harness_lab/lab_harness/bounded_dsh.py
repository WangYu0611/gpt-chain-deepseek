"""One tool-free official DSH Headless turn for the fixed integration fixture.

This is deliberately not a general autonomous coding worker. It uses a fresh
profile, a trusted public-hook guard, and the official desktop account provider.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from .dsh import DshError
from .dsh_legacy import VERIFIED_VERSION, parse_legacy_events
from .process import ProcessResult, run_process, safe_environment


MAX_PROMPT_BYTES = 4096
MAX_INPUT_BYTES = 8192
MAX_OUTPUT_TOKENS = 512
PROFILE = "bounded-smoke"
GUARD = Path(__file__).resolve().parent.parent / "live_tools/one_request_guard.mjs"
_ACCOUNT_LISTING_DIAGNOSTIC = re.compile(
    r"\[llm-deepseek-account\] model listing from https://api\.deepseek\.com/models "
    r"loaded [0-9]{1,6} model\(s\), skipped [0-9]{1,6} row\(s\)"
)


def _persist_process_result(result: ProcessResult, path: Path) -> None:
    """Preserve supervisor facts even if parsing or policy checks fail next."""
    path.write_text(json.dumps({
        "exit_code": result.exit_code, "timed_out": result.timed_out,
        "output_limit_exceeded": result.output_limit_exceeded,
        "stdout_path": str(result.stdout_path), "stderr_path": str(result.stderr_path),
    }, indent=2), encoding="utf-8")


def normalize_events(raw_path: Path, normalized_path: Path) -> int:
    """Remove only the inspected provider's one exact metadata diagnostic.

    The installed account provider logs its model-listing notice on stdout.
    Unknown non-JSON lines are errors, and raw output is never rewritten.
    """
    kept: list[str] = []
    removed = 0
    final_seen = False
    try:
        for line in Path(raw_path).read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            if _ACCOUNT_LISTING_DIAGNOSTIC.fullmatch(line):
                if not kept or final_seen:
                    raise DshError("Provider metadata outside the active event stream")
                removed += 1
                continue
            try:
                event = json.loads(line)
            except ValueError as error:
                raise DshError("Unknown non-JSON output in official DSH stream") from error
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise DshError("Invalid event in official DSH stream")
            final_seen |= event["type"] == "final"
            kept.append(line)
    except (OSError, UnicodeError) as error:
        raise DshError("Cannot read official DSH output") from error
    Path(normalized_path).write_text("\n".join(kept) + "\n", encoding="utf-8")
    return removed


def validate_config(config: dict[str, Any], *, preflight_only: bool = False) -> None:
    if config.get("verified_version") != VERIFIED_VERSION:
        raise DshError("Bounded DSH requires the inspected 0.2.1-rc.3 version")
    if config.get("model", "deepseek-flash") != "deepseek-flash":
        raise DshError("Only the fixed deepseek-flash route is supported")
    timeout = config.get("timeout_seconds", 120)
    if type(timeout) not in (int, float) or not 1 <= timeout <= 120:
        raise DshError("Bounded DSH timeout must be 1..120 seconds")
    if not preflight_only:
        authorization = config.get("usage_authorization", {})
        if (not isinstance(authorization, dict) or
                authorization.get("source") != "explicit_user_request" or
                authorization.get("scope") != "single_issue_smoke" or
                not isinstance(authorization.get("user_text"), str) or
                not authorization["user_text"].strip()):
            raise DshError("A recorded user authorization for this single-issue model usage is required")


def build_profile(home: Path, *, credentials_path: Path, report_path: Path,
                  preflight_only: bool) -> Path:
    """Compose an empty-root profile; no user/home overlays or extra consumers."""
    home = Path(home).resolve()
    if home.exists():
        raise DshError("The bounded profile home must be fresh for every invocation")
    profile = home / "profiles" / PROFILE
    profile.mkdir(parents=True)
    manifest = {"name": "dsh-profile-bounded-smoke", "private": True,
                "dependencies": {}, "dsh": {"profile": {"bundles": []}}}
    (profile / "package.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    rows: list[dict[str, Any]] = []

    def plugin(identity: str, package: str, config: dict[str, Any] | None = None,
               inject: list[str] | None = None):
        row: dict[str, Any] = {"id": identity, "name": package}
        if config is not None:
            row["config"] = config
        if inject is not None:
            row["inject"] = inject
        rows.append(row)

    for identity, package in [
        ("timer", "cordis-plugin-timer"), ("llm", "dsh-llm"),
        ("session", "dsh-session"), ("session-projection", "dsh-session-projection"),
        ("agent", "dsh-agent"),
    ]:
        plugin(identity, "@deepseek-ai/" + package)
    plugin("system-prompt", "@deepseek-ai/dsh-system-prompt", {
        "includeHarnessIdentity": False, "includeRuntimeContext": False,
        "personaPrefix": "Return only the requested task output. No tools are available."})
    plugin("tools", "@deepseek-ai/dsh-tools", {"mode": "native"})
    plugin("agent-loop", "@deepseek-ai/dsh-agent-loop", {"agents": [], "maxParallelToolCalls": 1})
    plugin("agent-default-model", "@deepseek-ai/dsh-agent-default-model", {
        "provider": "deepseek-account", "model": "deepseek-flash", "reasoningEffort": "off"})
    plugin("credentials", "@deepseek-ai/dsh-credentials-local", {
        "path": str(credentials_path.resolve()), "watch": False})
    plugin("authorization", "@deepseek-ai/dsh-authorization")
    plugin("deepseek-account", "@deepseek-ai/dsh-deepseek-account-platform", {"desktopPlatform": "win32"})
    plugin("llm-deepseek-account", "@deepseek-ai/dsh-llm-deepseek-account", {
        "baseURL": "https://api.deepseek.com/anthropic", "reasoningEffort": "off",
        "streamIdleTimeoutMs": 90000,
        "modelOverrides": {"deepseek-flash": {"maxTokens": MAX_OUTPUT_TOKENS}}})
    plugin("sessions", "@deepseek-ai/dsh-session-persistence-jsonl", {
        "root": str(home / "sessions"), "compression": "none"})
    plugin("bounded-smoke-policy", GUARD.as_uri(), {
        "provider": "deepseek-account", "model": "deepseek-flash",
        "maxRequests": 1, "maxTokens": MAX_OUTPUT_TOKENS, "maxInputBytes": MAX_INPUT_BYTES,
        "reportPath": str(report_path.resolve()), "preflightOnly": preflight_only})
    plugin("headless-runner", "@deepseek-ai/dsh-headless", {"task": "-", "json": True},
           inject=["boundedSmokePolicy"])
    # JSON is a YAML subset. No interpolated JS, arbitrary package paths, or secrets.
    (profile / "cordis.patch.yml").write_text(json.dumps([{"insert": rows}], indent=2), encoding="utf-8")
    return profile


def _command(config: dict[str, Any]) -> list[str]:
    install = Path(config["desktop_install"]).resolve()
    executable = install / "DeepSeek Harness.exe"
    archive = install / "resources/app.asar"
    if not executable.is_file() or not archive.is_file():
        raise DshError("The official Desktop CLI installation is missing")
    entry = archive / "dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"
    return [str(executable), "--expose-internals", str(entry), "--profile", PROFILE]


def run(prompt: str, cwd: Path, out: Path, config: dict[str, Any], *,
        preflight_only: bool = False) -> dict[str, Any]:
    validate_config(config, preflight_only=preflight_only)
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise DshError("The fixed smoke prompt must be 1..4096 UTF-8 bytes")
    cwd, out = Path(cwd).resolve(), Path(out).resolve()
    if not cwd.is_dir() or (cwd / ".env").exists():
        raise DshError("A dedicated existing working directory without .env is required")
    credential = Path(config["credentials_path"]).expanduser().resolve()
    if not credential.is_file() or credential.is_relative_to(cwd):
        raise DshError("An existing official account credential file outside the task workspace is required")
    command = _command(config)
    out.mkdir(parents=True, exist_ok=True)
    home = out / "dsh-home"
    report_path = out / "guard-report.json"
    build_profile(home, credentials_path=credential, report_path=report_path, preflight_only=preflight_only)
    environment = safe_environment(extra={"DSH_HOME": str(home)})
    environment.update(ELECTRON_RUN_AS_NODE="1")
    version_result = run_process(command[:-2] + ["--version"], cwd=cwd, output_dir=out / "version",
                                 timeout_seconds=15, max_output_bytes=16384, environment=environment)
    _persist_process_result(version_result, out / "version" / "process-result.json")
    if (version_result.exit_code != 0 or version_result.timed_out or version_result.output_limit_exceeded or
            version_result.stdout_path.read_text(encoding="utf-8-sig").strip() != VERIFIED_VERSION):
        raise DshError("The installed CLI version changed; re-inspect before model dispatch")
    # The only credential read occurs inside the official account service.
    result = run_process(command, cwd=cwd, output_dir=out,
                         timeout_seconds=float(config.get("timeout_seconds", 120)),
                         max_output_bytes=1048576, environment=environment,
                         stdin=prompt.encode("utf-8"))
    process_result_path = out / "process-result.json"
    _persist_process_result(result, process_result_path)
    if result.timed_out or result.output_limit_exceeded:
        raise DshError("Bounded official DSH exceeded time or log limits")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DshError("The required bounded policy did not produce a valid report") from error
    if report.get("violations") or report.get("visible_tool_count") != 0:
        raise DshError("Bounded DSH policy refused the run")
    common = {"model": "deepseek-flash", "version": VERIFIED_VERSION,
              "provider": "deepseek-account", "mode": "bounded-tool-free-headless",
              "request_count": report.get("request_count"), "usage": report.get("usage"),
              "cost_amount": None, "cost_note": "User authorized this bounded usage; no currency hard cap asserted",
              "guard_report_path": str(report_path), "stdout_path": str(result.stdout_path),
              "stderr_path": str(result.stderr_path), "exit_code": result.exit_code,
              "process_result_path": str(process_result_path)}
    if preflight_only:
        if report.get("preflight_validated") is not True or report.get("request_count") != 0:
            raise DshError("No-network preflight did not reach the rejecting guard")
        return common | {"preflight": "PASS", "model_called": False}
    if report.get("request_count") != 1:
        raise DshError("Expected exactly one dispatched model request")
    normalized_path = out / "normalized-events.jsonl"
    removed = normalize_events(result.stdout_path, normalized_path)
    parsed = parse_legacy_events(normalized_path, exit_code=result.exit_code)
    events = [json.loads(line) for line in normalized_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if any(event.get("type") in {"tool_call", "tool_result"} for event in events):
        raise DshError("Tool events are forbidden in a bounded smoke result")
    return common | parsed | {"final_text": events[-1]["text"], "model_called": True,
                              "normalized_events_path": str(normalized_path),
                              "provider_metadata_lines_removed": removed}
