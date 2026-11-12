"""Single authorized Issue -> official DSH -> validated local result artifact.

GitHub credentials are used only by this trusted controller, never the model.
Remote publication runs separately. This is a fixed arithmetic integration test,
not a replacement for the general Worker's unfinished isolation/recovery work.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from .github import GitHubClient
from .process import run_process, safe_environment
from .smoke_policy import ACTOR, REPOSITORY, SCHEMA, SmokeError, TRUSTED_TESTS, check_issue, digest, validate_result, validate_source


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def check_context(config: dict, event: dict, environment: dict) -> int:
    number = config.get("issue_number")
    if type(number) is not int or number <= 0 or config.get("repository") != REPOSITORY:
        raise SmokeError("An exact local smoke configuration is required")
    if (environment.get("GITHUB_ACTIONS") != "true" or environment.get("GITHUB_REPOSITORY") != REPOSITORY
            or environment.get("GITHUB_EVENT_NAME") != "issues"
            or environment.get("GITHUB_ACTOR") != ACTOR
            or event.get("repository", {}).get("full_name") != REPOSITORY
            or event.get("repository", {}).get("private") is not True
            or event.get("sender", {}).get("login") != ACTOR
            or event.get("action") != "labeled" or event.get("label", {}).get("name") != "agent:ready"):
        raise SmokeError("Only the trusted repository Issue label event is supported")
    expires = config.get("expires_at", "")
    if not expires.endswith("Z"):
        raise SmokeError("The one-shot configuration requires an explicit UTC expiry")
    try:
        if datetime.fromisoformat(expires.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
            raise SmokeError("The one-shot configuration has expired")
    except ValueError:
        raise SmokeError("Invalid configuration expiry") from None
    if config.get("usage_authorization") != "user-authorized-single-request":
        raise SmokeError("This smoke requires explicit single-request usage authorization")
    check_issue(event.get("issue", {}), number, config.get("issue_body_sha256", ""))
    return number


def execute(config, event, environment, *, root: Path, client, generate):
    number = check_context(config, event, environment)
    for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        if not environment.get(key, "").isdigit():
            raise SmokeError("Missing Actions run identity")
    issue = client.get_issue(number)
    check_issue(issue, number, config["issue_body_sha256"])
    base = client.get_ref("main")["object"]["sha"]
    if base != config.get("base_sha"):
        raise SmokeError("Main changed since the reviewed one-shot configuration")
    original = client.request("GET", "/contents/src/arithmetic.py", params={"ref": base})
    if original.get("encoding") != "base64":
        raise SmokeError("Expected an ordinary source file")
    try:
        source = base64.b64decode(original["content"]).decode("utf-8")
    except (KeyError, ValueError, UnicodeError):
        raise SmokeError("Invalid source response") from None
    if digest(source) != config.get("original_source_sha256"):
        raise SmokeError("The base source differs from the reviewed fixture")
    folder = root / ".runtime" / "smoke-output" / str(number)
    folder.mkdir(parents=True, exist_ok=True)
    # Exclusive durable claim BEFORE the first model request. An interrupted or
    # repeated run is stopped for reconciliation; it never bills another call.
    claim = folder / "claim.json"
    try:
        with claim.open("x", encoding="utf-8") as stream:
            json.dump({"issue": number, "run_id": environment["GITHUB_RUN_ID"], "claimed_at": now(), "phase": "claimed"}, stream)
    except FileExistsError:
        raise SmokeError("This Issue was already claimed; inspect its artifact before retrying") from None
    workspace = folder / "workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "__init__.py").write_text("", encoding="utf-8")
    started = now()
    prompt = ("Implement the fixed arithmetic task below. Return ONLY a Python module, no explanation. "
              "Keep add(left, right) as return left + right and add multiply(left, right), returning the product. "
              "Exactly these two functions; each body is a single return arithmetic expression. "
              "No imports, tools, file access, calls, decorators, annotations, loops, tests or markdown.\n"
              "Current source:\n" + source)
    if len(prompt.encode("utf-8")) > 2048:
        raise SmokeError("Fixed prompt exceeded its byte bound")
    result = generate(prompt, workspace, folder / "dsh", config["dsh"])
    canonical = validate_source(result["final_text"])
    (workspace / "src" / "arithmetic.py").write_text(canonical, encoding="utf-8")
    (workspace / "test_smoke.py").write_text(TRUSTED_TESTS, encoding="utf-8")
    process = run_process([sys.executable, "-I", "-c", "import sys,runpy;sys.path.insert(0,'.');runpy.run_path('test_smoke.py',run_name='__main__')"],
                          cwd=workspace, output_dir=folder / "tests", timeout_seconds=20,
                          environment=safe_environment(), max_output_bytes=65536)
    logs = process.stdout_path.read_text(encoding="utf-8") + process.stderr_path.read_text(encoding="utf-8")
    if process.exit_code != 0 or process.timed_out or process.output_limit_exceeded or "Ran 7 tests" not in logs or "\nOK" not in logs:
        raise SmokeError("Real trusted arithmetic tests did not pass")
    # Recheck the Issue and source revision after the billed call; stale tasks
    # may retain local logs but cannot produce a publishable artifact.
    check_issue(client.get_issue(number), number, config["issue_body_sha256"])
    if client.get_ref("main")["object"]["sha"] != base:
        raise SmokeError("Main changed during execution")
    evidence = {"schema": SCHEMA, "repository": REPOSITORY, "issue": number,
                "issue_body_sha256": config["issue_body_sha256"], "base_sha": base,
                "source_path": "src/arithmetic.py", "source_code": canonical, "source_sha256": digest(canonical),
                "session_id": result["session_id"], "model": result["model"], "version": result["version"],
                "model_request_count": result["request_count"], "harness_status": "completed",
                "tests": {"exit_code": process.exit_code, "count": 7, "timed_out": process.timed_out,
                          "output_limit_exceeded": process.output_limit_exceeded, "log_sha256": digest(logs),
                          "summary": "Ran 7 trusted arithmetic tests; OK"},
                "run_id": environment["GITHUB_RUN_ID"], "run_attempt": environment["GITHUB_RUN_ATTEMPT"],
                "runner_name": environment.get("RUNNER_NAME", "unknown"), "started_at": started, "finished_at": now(),
                "scope": "fixed task, model tools disabled; not a general coding sandbox",
                "cost_policy": "user authorized usage; one model request, max 512 output tokens; not a currency guarantee"}
    validate_result(evidence)
    write_json(folder / "result.json", evidence)
    write_json(claim, {"issue": number, "run_id": environment["GITHUB_RUN_ID"], "phase": "completed", "source_sha256": evidence["source_sha256"]})
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    path = args.config or root / ".private" / "live-smoke.json"
    try:
        from .bounded_dsh import run as generate_once
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        event = json.loads(args.event.read_text(encoding="utf-8-sig"))
        client = GitHubClient(REPOSITORY, network_enabled=True, token_env="GH_SMOKE_READ_TOKEN",
                              authorization_gate=lambda operation: operation == "github-read")
        result = execute(config, event, dict(os.environ), root=root, client=client, generate=generate_once)
        print(json.dumps({"status": "completed", "issue": result["issue"], "session_id": result["session_id"], "tests": 7}))
        return 0
    except Exception as error:
        # Raw provider exceptions can contain account data. Details stay in the
        # bounded local logs; GitHub receives a sanitized class/reason only.
        reason = str(error) if isinstance(error, SmokeError) else type(error).__name__
        print(json.dumps({"status": "BLOCKED", "reason": reason}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
