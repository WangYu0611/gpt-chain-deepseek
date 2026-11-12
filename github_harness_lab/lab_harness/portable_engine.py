"""Shared bounded-document executor. Projects supply data, never custom Python."""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

from .portable_policy import (
    PUBLIC_BINDING, check_arm, digest, strict_json, task_snapshot,
    validate_artifact, validate_event, validate_model_result,
)
from .project_profile import repository_namespace, task_spec, validate_document, validate_profile


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _exclusive(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        stream.write(_json(value))
        stream.flush()
        os.fsync(stream.fileno())


def _atomic(path, value):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        _exclusive(temp, value)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _folder(root, relative):
    root = Path(root).resolve()
    folder = root / relative
    if not folder.resolve().is_relative_to(root):
        raise ValueError("Runtime path escaped the controller directory")
    cursor = folder
    while cursor != root:
        if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
            raise ValueError("Runtime directories cannot be links")
        cursor = cursor.parent
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def generate_task(profile, activation, event, env, *, root, client, generate):
    """Call the injected bounded provider at most once for a task.

    A reservation survives failure/crash and is never automatically cleared.
    A repository-wide exclusive lease prevents two local runners working at once.
    A crashed lease requires explicit operator reconciliation; expiry is not enough
    to assume its child process stopped. This is deliberately a single-host engine.
    """
    profile = validate_profile(profile)
    issue_number = validate_event(profile, activation, event, env)
    check_arm(client, profile, activation)
    issue, task, body_sha = task_snapshot(client, profile, activation, issue_number)
    event_issue = event.get("issue", {})
    if (event_issue.get("body") != issue["body"] or event_issue.get("title") != issue["title"]
            or event_issue.get("user", {}).get("login") != profile["trusted_actor"]
            or event_issue.get("state") != "open" or "pull_request" in event_issue):
        raise ValueError("Task changed after the original Issue event was queued")
    spec = task_spec(profile, task["number"])
    namespace = repository_namespace(profile["repository"])
    branch = f"agent/harness-{namespace}-{activation['nonce'].replace('-', '')[:12]}-{spec['number']}"
    existing = client._list("/pulls", {"state": "all", "head": profile["repository"].split("/")[0] + ":" + branch})
    if existing:
        raise ValueError("A report already exists; generation must not be billed again")
    state = _folder(root, ".runtime/portable/" + namespace)
    lease_path = state / "lease.json"
    owner = str(uuid.uuid4())
    lease = {"owner": owner, "repository": profile["repository"], "nonce": activation["nonce"],
             "issue": issue_number, "run_id": env["GITHUB_RUN_ID"]}
    try:
        _exclusive(lease_path, lease)
    except FileExistsError as error:
        raise ValueError("Repository has an active or unreconciled local lease") from error
    claim_path = None
    reservation_created = False
    try:
        folder = _folder(state, activation["nonce"] + "/" + str(spec["number"]))
        claim_path = folder / "claim.json"
        claim = {**lease, "task_id": spec["task_id"], "issue_body_sha256": body_sha,
                 "profile_sha256": activation["profile_sha256"], "state": "RESERVED",
                 "model_request_may_have_started": True}
        # Claiming before calling the provider sacrifices automatic retry to prevent
        # charging twice when the response or local persistence outcome is unknown.
        _exclusive(claim_path, claim)
        reservation_created = True
        workspace = folder / "workspace"
        workspace.mkdir()
        output = folder / "dsh"
        check_arm(client, profile, activation)
        task_snapshot(client, profile, activation, issue_number, expected_body_sha=body_sha)
        result = generate(spec["prompt"], workspace, output, activation["dsh"])
        validate_model_result(result)
        guard_path = output / "guard-report.json"
        if (Path(result.get("guard_report_path", "")).resolve() != guard_path.resolve()
                or guard_path.is_symlink() or not guard_path.is_file()
                or guard_path.stat().st_size > 32768):
            raise ValueError("The bounded provider guard report is missing or outside this run")
        guard = strict_json(guard_path.read_text(encoding="utf-8-sig"))
        if (type(guard.get("request_count")) is not int or guard["request_count"] != 1
                or type(guard.get("visible_tool_count")) is not int or guard["visible_tool_count"] != 0
                or guard.get("violations") != []):
            raise ValueError("Provider guard did not confirm one tool-free request")
        document = validate_document(profile, spec["number"], result.get("final_text"))
        check_arm(client, profile, activation)
        task_snapshot(client, profile, activation, issue_number, expected_body_sha=body_sha)
        if strict_json(lease_path.read_text(encoding="utf-8")) != lease:
            raise ValueError("Local execution lease changed")
        artifact = {
            "schema": "harness-document/v1",
            **{key: activation[key] for key in PUBLIC_BINDING},
            "issue": issue_number, "task_number": spec["number"], "task_id": spec["task_id"],
            "issue_body_sha256": body_sha, "document_path": spec["document_path"],
            "report_path": spec["report_path"], "document": document,
            "document_sha256": digest(document), "session_id": result["session_id"],
            "model": result["model"], "version": result["version"],
            "model_requests": result["request_count"], "exit_code": result["exit_code"],
            "guard": {"request_count": 1, "visible_tool_count": 0, "violations": []},
            "usage": result.get("usage") or {}, "run_id": env["GITHUB_RUN_ID"], "run_attempt": 1,
        }
        validate_artifact(profile, activation, artifact, env)
        artifact_path = folder / "result.json"
        _atomic(artifact_path, artifact)
        _atomic(claim_path, {**claim, "state": "GENERATED", "artifact_sha256": digest(_json(artifact))})
        return artifact, artifact_path
    except BaseException as error:
        if reservation_created and claim_path is not None:
            # Never record raw error/provider output, which could contain private text.
            _atomic(claim_path, {**claim, "state": "BLOCKED", "error_type": type(error).__name__})
        raise
    finally:
        # Do not remove a lease replaced by another owner or altered by an operator.
        if lease_path.exists() and strict_json(lease_path.read_text(encoding="utf-8")) == lease:
            lease_path.unlink()
