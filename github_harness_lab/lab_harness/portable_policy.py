"""Repository-independent gates for the bounded-document capability.

No model, process or network is started here. A caller supplies the GitHub client.
Profiles describe projects; a separately approved activation binds a single run.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import uuid

from .project_profile import profile_digest, task_spec, validate_document, validate_profile

TASK_MARKER = "<!-- harness-task:v1 -->\n"
REPORT_MARKER = "<!-- harness-report:v1 -->\n"
ARM_MARKER = "<!-- harness-arm:v1 -->\n"
WORKFLOW_PATH = ".github/workflows/harness-documents.yml"
PUBLIC_BINDING = (
    "repository", "profile_sha256", "workflow_sha", "controller_sha256",
    "control_issue", "arm_comment_id", "arm_body_sha256", "nonce",
    "issued_at", "expires_at", "max_model_requests", "runner_label",
)
TASK_KEYS = frozenset((
    "schema", "repository", "profile_sha256", "nonce", "control_issue",
    "number", "task_id", "dispatch", "previous_pr", "previous_head",
    "document_path", "report_path",
))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_digest(value) -> str:
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False))


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


def strict_json(text: str):
    def bad_constant(_):
        raise ValueError("Invalid JSON constant")
    return json.loads(text, object_pairs_hook=_object, parse_constant=bad_constant)


def parse_record(body: str, marker: str) -> dict:
    if not isinstance(body, str) or len(body.encode("utf-8")) > 32768 or body.count(marker) != 1:
        raise ValueError("Missing, oversized or ambiguous machine record")
    def bad_constant(_):
        raise ValueError("Invalid JSON constant")
    decoder = json.JSONDecoder(object_pairs_hook=_object, parse_constant=bad_constant)
    record, _ = decoder.raw_decode(body.split(marker, 1)[1].lstrip())
    if not isinstance(record, dict):
        raise ValueError("Machine record must be an object")
    return record


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", value) is not None


def _positive(value):
    return type(value) is int and value > 0


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value):
        raise ValueError("UTC timestamp must have seconds and a Z suffix")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def validate_activation(profile, activation, *, now=None):
    profile = validate_profile(profile)
    if not isinstance(activation, dict) or activation.get("schema") != "harness-activation/v1":
        raise ValueError("Separate activation record required")
    if activation.get("enabled") is not True:
        raise ValueError("Live execution has not been activated")
    if (activation.get("repository") != profile["repository"]
            or activation.get("profile_sha256") != profile_digest(profile)
            or activation.get("runner_label") != profile["runner_label"]):
        raise ValueError("Activation is bound to a different project profile")
    for name, length in (("workflow_sha", 40), ("controller_sha256", 64), ("arm_body_sha256", 64)):
        if not _hex(activation.get(name), length):
            raise ValueError("Activation contains an invalid digest: " + name)
    if not _positive(activation.get("control_issue")) or not _positive(activation.get("arm_comment_id")):
        raise ValueError("New control Issue and immutable owner arm are required")
    nonce = activation.get("nonce")
    if not isinstance(nonce, str) or str(uuid.UUID(nonce)) != nonce:
        raise ValueError("Activation nonce must be a canonical UUID")
    start, end = _timestamp(activation.get("issued_at")), _timestamp(activation.get("expires_at"))
    current = now or datetime.now(timezone.utc)
    if not start <= current < end or not 0 < (end - start).total_seconds() <= 5400:
        raise ValueError("Activation is not within its bounded 90-minute window")
    if type(activation.get("max_model_requests")) is not int or activation["max_model_requests"] != 2:
        raise ValueError("The document capability allows exactly two reserved requests")
    return activation


def arm_record(profile, activation):
    """Expected approved record; this function does not publish or authorize it."""
    return {
        "schema": "harness-arm/v1", "authorized": True,
        **{key: activation[key] for key in PUBLIC_BINDING
           if key not in {"arm_comment_id", "arm_body_sha256", "runner_label"}},
        "allowed_tasks": [spec["task_id"] for spec in profile["document_tasks"]],
        "stop_task_id": profile["stop_task"]["task_id"],
    }


def check_arm(client, profile, activation, *, now=None):
    validate_activation(profile, activation, now=now)
    if client.repository != profile["repository"]:
        raise ValueError("Client repository does not match the project")
    control = client.get_issue(activation["control_issue"])
    if (control.get("number") != activation["control_issue"] or control.get("state") != "open"
            or control.get("user", {}).get("login") != profile["trusted_actor"] or "pull_request" in control):
        raise ValueError("Control Issue is closed or not owned by the trusted actor")
    comments = client.list_comments(activation["control_issue"])
    expected = arm_record(profile, activation)
    bound = []
    current = now or datetime.now(timezone.utc)
    for comment in comments:
        body = comment.get("body", "")
        if comment.get("user", {}).get("login") != profile["trusted_actor"] or ARM_MARKER.strip() not in body:
            continue
        record = parse_record(body, ARM_MARKER)
        if record.get("repository") != profile["repository"]:
            raise ValueError("Foreign authorization record in this control Issue")
        if record.get("nonce") == activation["nonce"]:
            # A revocation, edit, duplicate or different record cannot be ignored.
            if (comment.get("id") != activation["arm_comment_id"] or record != expected
                    or not comment.get("created_at") or comment["created_at"] != comment.get("updated_at")
                    or digest(body) != activation["arm_body_sha256"]):
                raise ValueError("Authorization was changed, revoked or duplicated")
            bound.append(comment)
        elif record.get("authorized") is True:
            if _timestamp(record.get("expires_at")) > current:
                raise ValueError("Another active authorization conflicts with this run")
    if len(bound) != 1:
        raise ValueError("Unique immutable owner authorization was not found")
    if client.get_ref(profile["default_branch"]).get("object", {}).get("sha") != activation["workflow_sha"]:
        raise ValueError("Reviewed deployment branch changed")
    return expected


def validate_event(profile, activation, event, env):
    validate_activation(profile, activation)
    expected_env = {
        "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": profile["repository"],
        "GITHUB_EVENT_NAME": "issues", "GITHUB_ACTOR": profile["trusted_actor"],
        "GITHUB_RUN_ATTEMPT": "1", "GITHUB_WORKFLOW_SHA": activation["workflow_sha"],
        "RUNNER_OS": "Windows",
        "GITHUB_WORKFLOW_REF": profile["repository"] + "/" + WORKFLOW_PATH + "@refs/heads/" + profile["default_branch"],
    }
    if any(env.get(key) != value for key, value in expected_env.items()):
        raise ValueError("Only the approved first-attempt Windows Issue job can generate")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", env.get("GITHUB_RUN_ID", "")):
        raise ValueError("Invalid workflow run ID")
    if (event.get("action") != "opened" or event.get("sender", {}).get("login") != profile["trusted_actor"]
            or event.get("repository", {}).get("full_name") != profile["repository"]
            or event.get("repository", {}).get("private") is not True):
        raise ValueError("Event repository, author or type was not approved")
    issue = event.get("issue", {}).get("number")
    if not _positive(issue) or str(issue) != env.get("HARNESS_ISSUE"):
        raise ValueError("Event Issue identity mismatch")
    return issue


def task_snapshot(client, profile, activation, issue_number, *, expected_body_sha=None, verify_previous=True):
    if not _positive(issue_number) or issue_number == activation["control_issue"]:
        raise ValueError("Invalid task Issue")
    issue = client.get_issue(issue_number)
    record = parse_record(issue.get("body", ""), TASK_MARKER)
    if set(record) != TASK_KEYS:
        raise ValueError("Unexpected task protocol fields")
    number = record.get("number")
    spec = task_spec(profile, number)
    expected = {
        "schema": "harness-task/v1", "repository": profile["repository"],
        "profile_sha256": profile_digest(profile), "nonce": activation["nonce"],
        "control_issue": activation["control_issue"], "number": number,
        "task_id": spec["task_id"], "dispatch": True,
        "document_path": spec["document_path"], "report_path": spec["report_path"],
    }
    if (issue.get("number") != issue_number or issue.get("state") != "open" or "pull_request" in issue
            or issue.get("user", {}).get("login") != profile["trusted_actor"]
            or issue.get("title") != spec["task_id"]
            or any(record.get(key) != value or (isinstance(value, bool) and record.get(key) is not value)
                   for key, value in expected.items())):
        raise ValueError("Task Issue does not match its approved profile")
    body_sha = digest(issue["body"])
    if expected_body_sha is not None and body_sha != expected_body_sha:
        raise ValueError("Task Issue changed during execution")
    if number == 1:
        if record["previous_pr"] is not None or record["previous_head"] is not None:
            raise ValueError("First task cannot have a predecessor")
    else:
        if not _positive(record["previous_pr"]) or not _hex(record["previous_head"], 40):
            raise ValueError("Second task requires a verified previous report")
        if verify_previous:
            from .portable_publisher import verify_report_pr
            previous_pr = client.get_pr(record["previous_pr"])
            verify_report_pr(client, profile, activation, previous_pr,
                             expected_number=1, expected_head=record["previous_head"])
    return issue, record, body_sha


def validate_model_result(result):
    if (not isinstance(result, dict) or result.get("exit_code") != 0 or type(result.get("exit_code")) is not int
            or result.get("request_count") != 1 or type(result.get("request_count")) is not int
            or result.get("model") != "deepseek-flash" or result.get("version") != "0.2.1-rc.3"
            or not isinstance(result.get("session_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", result["session_id"])
            or result.get("mode") != "bounded-tool-free-headless"
            or result.get("provider") != "deepseek-account"):
        raise ValueError("Official bounded DSH result is incomplete or unsupported")
    return result


def validate_artifact(profile, activation, artifact, env):
    validate_activation(profile, activation)
    keys = set(PUBLIC_BINDING) | {
        "schema", "issue", "task_number", "task_id", "issue_body_sha256",
        "document_path", "report_path", "document", "document_sha256",
        "session_id", "model", "version", "model_requests", "exit_code",
        "guard", "usage", "run_id", "run_attempt",
    }
    if (not isinstance(artifact, dict) or set(artifact) != keys
            or len(json.dumps(artifact, ensure_ascii=False, allow_nan=False).encode()) > 65536
            or artifact.get("schema") != "harness-document/v1"
            or any(artifact.get(key) != activation[key] for key in PUBLIC_BINDING)):
        raise ValueError("Artifact binding differs from this approved activation")
    spec = task_spec(profile, artifact.get("task_number"))
    expectations = {
        "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": profile["repository"],
        "GITHUB_EVENT_NAME": "issues", "GITHUB_ACTOR": profile["trusted_actor"],
        "GITHUB_WORKFLOW_SHA": activation["workflow_sha"],
        "GITHUB_RUN_ID": artifact.get("run_id"), "GITHUB_RUN_ATTEMPT": "1",
        "HARNESS_ISSUE": str(artifact.get("issue")),
        "GITHUB_WORKFLOW_REF": profile["repository"] + "/" + WORKFLOW_PATH + "@refs/heads/" + profile["default_branch"],
    }
    if any(env.get(key) != value for key, value in expectations.items()):
        raise ValueError("Artifact is not from this workflow run and Issue")
    if (not _positive(artifact.get("issue")) or not re.fullmatch(r"[1-9][0-9]{0,19}", artifact.get("run_id", ""))
            or type(artifact.get("run_attempt")) is not int or artifact["run_attempt"] != 1
            or not _hex(artifact.get("issue_body_sha256"), 64)
            or artifact.get("task_id") != spec["task_id"]
            or artifact.get("document_path") != spec["document_path"]
            or artifact.get("report_path") != spec["report_path"]
            or artifact.get("guard") != {"request_count": 1, "visible_tool_count": 0, "violations": []}):
        raise ValueError("Artifact scope, guard or identity is invalid")
    validate_model_result({**artifact, "request_count": artifact.get("model_requests"),
                           "mode": "bounded-tool-free-headless", "provider": "deepseek-account"})
    if (type(artifact["guard"]["request_count"]) is not int
            or type(artifact["guard"]["visible_tool_count"]) is not int):
        raise ValueError("Guard counters must be integers")
    usage = artifact.get("usage")
    if (not isinstance(usage, dict)
            or not set(usage) <= {"inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "totalTokens"}
            or any(type(value) not in (int, float) or not 0 <= value <= 10**9 for value in usage.values())):
        raise ValueError("Usage must contain only bounded numeric token counts")
    document = validate_document(profile, spec["number"], artifact.get("document"))
    if document != artifact["document"] or digest(document) != artifact.get("document_sha256"):
        raise ValueError("Artifact document is not canonical or has a different hash")
    return document
