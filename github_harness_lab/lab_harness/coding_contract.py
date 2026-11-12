"""Pure contracts for reviewed code profiles and revocable continuous activation.

These validators do not grant authority, start processes or obtain credentials.
Issue content can select reviewed paths and goals, never execution commands.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import unicodedata
import uuid

from .github import GitHubClient
from .policy import PROTECTED, SECRET_NAMES, SECRET_PATTERN, safe_path
from .portable_policy import canonical_digest, digest, strict_json, parse_record, _hex, _positive, _timestamp, validate_model_result
from .project_profile import repository_namespace
from .repository_setup import RepositorySettings, validate_settings

SCHEMA = "harness-code-project/v1"
TASK_MARKER = "<!-- harness-code-task:v1 -->\n"
REPORT_MARKER = "<!-- harness-code-report:v1 -->\n"
ARM_MARKER = "<!-- harness-code-arm:v1 -->\n"
WORKFLOW_PATH = ".github/workflows/harness-code.yml"
PUBLIC_BINDING = (
    "repository", "profile_sha256", "workflow_sha", "controller_sha256",
    "control_issue", "arm_comment_id", "arm_body_sha256", "nonce",
    "issued_at", "expires_at", "max_model_requests", "runner_label", "max_tasks",
)
LIMITS = {"duration_seconds": None, "max_tasks": None, "max_model_requests": None,
          "max_prompt_bytes": 196608, "max_output_tokens": 8192}
PROFILE_KEYS = frozenset((
    "schema", "repository", "default_branch", "trusted_actor", "source_repository",
    "live_enabled", "runner_label", "goal", "allowed_paths", "context_paths",
    "materialize_paths", "checks", "limits",
))
TASK_KEYS = frozenset((
    "schema", "repository", "profile_sha256", "nonce", "control_issue", "number",
    "task_id", "base_sha", "previous_pr", "previous_head", "transition", "goal",
    "allowed_paths", "context",
))
CHECK_KEYS = frozenset(("id", "argv", "timeout_seconds", "success_patterns", "failure_patterns", "paths"))
_TASK_ID = re.compile(r"TASK(?:[-_][A-Za-z0-9]+)+\Z", re.ASCII)
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z", re.ASCII)
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
MAX_PROFILE_BYTES = 65536


def _object(value, keys, label):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError(label + " contains missing or unexpected fields")
    return value


def _text(value, label, *, maximum=8192, multiline=False, empty=False):
    if (not isinstance(value, str) or (not value.strip() and not empty)
            or len(value.encode("utf-8")) > maximum
            or any((unicodedata.category(c).startswith("C") and not (multiline and c in "\n\t"))
                   or unicodedata.category(c) in {"Zl", "Zp"} for c in value)):
        raise ValueError(label + " must be bounded text without invisible/control characters")
    return value


def validate_task_id(value):
    if not isinstance(value, str) or len(value) > 100 or not _TASK_ID.fullmatch(value):
        raise ValueError("Task ID must use ASCII TASK identifiers separated by '-' or '_'")
    return value


def validate_path(value, *, template=False, writable=False, require_file=True):
    """Accept an exact relative file path; paths never imply recursive access."""
    if not isinstance(value, str) or len(value) > 240:
        raise ValueError("Invalid exact project path")
    candidate = value.replace("{task_id}", "TASK-VALID") if template else value
    if "{" in candidate or "}" in candidate or safe_path(candidate) != candidate:
        raise ValueError("Only the task_id path template is supported")
    parts = candidate.split("/")
    if any(not _SEGMENT.fullmatch(part) for part in parts):
        raise ValueError("Project paths must use unambiguous ASCII segments")
    blocked = {name.casefold() for name in (*PROTECTED, *SECRET_NAMES)}
    if not writable:
        blocked.discard("agents.md")
    if any(part.casefold() in blocked or part.casefold().endswith((".pem", ".key", ".pfx")) for part in parts):
        raise ValueError("Protected or sensitive project paths are forbidden")
    if writable and candidate.casefold().startswith("docs/reports/harness/"):
        raise ValueError("Controller execution reports are not model-writable")
    # A bare folder name is not a reviewed file. Source extensions remain configurable.
    if require_file and ("." not in parts[-1] or parts[-1].endswith(".")):
        raise ValueError("Paths must name exact files, not directories")
    return value


def _paths(values, label, *, maximum=256, template=True, writable=False, empty=False, require_file=True):
    if type(values) is not list or not (0 if empty else 1) <= len(values) <= maximum:
        raise ValueError(label + " has an invalid number of exact paths")
    paths = [validate_path(value, template=template, writable=writable, require_file=require_file) for value in values]
    folded = [value.casefold() for value in paths]
    if len(set(folded)) != len(paths) or any(
        a.startswith(b + "/") or b.startswith(a + "/")
        for index, a in enumerate(folded) for b in folded[index + 1:]
    ):
        raise ValueError(label + " paths collide or overlap")
    return paths


def expand_paths(profile, task_id, key="allowed_paths"):
    validate_task_id(task_id)
    if key not in {"allowed_paths", "context_paths", "materialize_paths"}:
        raise ValueError("Unknown path scope")
    return _paths([path.replace("{task_id}", task_id) for path in profile[key]], key,
                  template=False, writable=key == "allowed_paths", empty=key != "allowed_paths",
                  require_file=key != "materialize_paths")


def validate_profile(profile):
    _object(profile, PROFILE_KEYS, "Code profile")
    if profile["schema"] != SCHEMA or profile["live_enabled"] is not False:
        raise ValueError("An inert harness-code-project/v1 profile is required")
    settings = validate_settings(RepositorySettings(repository=profile["repository"],
        default_branch=profile["default_branch"], trusted_actor=profile["trusted_actor"], source_repository=""))
    GitHubClient._branch(settings.default_branch)
    source = _text(profile["source_repository"], "source_repository", maximum=2048, empty=True)
    if source and not (PureWindowsPath(source).is_absolute() or PurePosixPath(source).is_absolute()):
        raise ValueError("source_repository must be an absolute local path or empty")
    label = "harness-code-" + repository_namespace(settings.repository)
    if profile["runner_label"] != label:
        raise ValueError("Code runner label must match this repository namespace")
    _text(profile["goal"], "Profile goal", multiline=True)
    allowed = _paths(profile["allowed_paths"], "allowed_paths", writable=True)
    context = _paths(profile["context_paths"], "context_paths", empty=True)
    materialize = _paths(profile["materialize_paths"], "materialize_paths", empty=True, require_file=False)
    _object(profile["limits"], LIMITS, "Limits")
    if any(profile["limits"][key] is not None if value is None else
           type(profile["limits"][key]) is not int or profile["limits"][key] != value
           for key, value in LIMITS.items()):
        raise ValueError("Code capability uses continuous activation with bounded per-request payloads")
    if type(profile["checks"]) is not list or not 1 <= len(profile["checks"]) <= 16:
        raise ValueError("One to sixteen reviewed checks are required")
    ids = set()
    for check in profile["checks"]:
        _object(check, CHECK_KEYS, "Check")
        identity = check["id"]
        if (not isinstance(identity, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", identity)
                or identity.casefold() in ids):
            raise ValueError("Check IDs must be unique ASCII identifiers")
        ids.add(identity.casefold())
        argv = check["argv"]
        if type(argv) is not list or not 1 <= len(argv) <= 64:
            raise ValueError("Check argv must be a nonempty reviewed argument array")
        for arg in argv:
            _text(arg, "Check argument", maximum=4096)
            if (any(name not in {"workspace", "output", "task_id"} for name in _PLACEHOLDER.findall(arg))
                    or "{" in _PLACEHOLDER.sub("", arg) or "}" in _PLACEHOLDER.sub("", arg)):
                raise ValueError("Unsupported command placeholder")
        if type(check["timeout_seconds"]) is not int or not 1 <= check["timeout_seconds"] <= 86400:
            raise ValueError("Individual check timeout must be bounded to one day")
        for key in ("success_patterns", "failure_patterns"):
            patterns = check[key]
            if type(patterns) is not list or not 0 <= len(patterns) <= 24:
                raise ValueError("Check patterns must be a bounded list")
            for pattern in patterns:
                _text(pattern, "Check literal substring", maximum=1024)
        triggers = _paths(check["paths"], "check paths", writable=True)
        if not set(triggers) <= set(allowed):
            raise ValueError("Check trigger paths must be reviewed writable paths")
    # Every writable file has at least one check; a no-check change cannot pass.
    if not set(allowed) <= {path for check in profile["checks"] for path in check["paths"]}:
        raise ValueError("Every writable path requires a reviewed check")
    result = copy.deepcopy(profile)
    result.update(repository=settings.repository, default_branch=settings.default_branch, trusted_actor=settings.trusted_actor,
                  source_repository=source, runner_label=label, allowed_paths=allowed, context_paths=context,
                  materialize_paths=materialize)
    # Templates can collide with literal paths after expansion; reject likely collisions immediately.
    for task_id in ("TASK-VALID", "TASK_M0_1"):
        for key in ("allowed_paths", "context_paths", "materialize_paths"):
            expand_paths(result, task_id, key)
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_PROFILE_BYTES:
        raise ValueError("Code profile exceeds 64 KiB")
    return result


def load_profile(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_PROFILE_BYTES + 1)
    if len(raw) > MAX_PROFILE_BYTES:
        raise ValueError("Code profile exceeds 64 KiB")
    try:
        return validate_profile(strict_json(raw.decode("utf-8-sig")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Code profile must be valid UTF-8 JSON") from error


def profile_digest(profile):
    return canonical_digest(validate_profile(profile))


def validate_activation(profile, activation, *, now=None):
    profile = validate_profile(profile)
    required = set(PUBLIC_BINDING) | {"schema", "enabled"}
    if type(activation) is not dict or not required <= set(activation) or not set(activation) <= required | {"dsh"}:
        raise ValueError("Separate code activation contains missing or unexpected fields")
    if activation["schema"] != "harness-code-activation/v1" or activation["enabled"] is not True:
        raise ValueError("Fresh code activation is required")
    if (activation["repository"] != profile["repository"] or activation["profile_sha256"] != profile_digest(profile)
            or activation["runner_label"] != profile["runner_label"]):
        raise ValueError("Activation is bound to a different code profile")
    for key, length in (("workflow_sha", 40), ("controller_sha256", 64), ("arm_body_sha256", 64)):
        if not _hex(activation[key], length):
            raise ValueError("Invalid activation digest: " + key)
    if not _positive(activation["control_issue"]) or not _positive(activation["arm_comment_id"]):
        raise ValueError("New control Issue and owner arm identities are required")
    nonce = activation["nonce"]
    try:
        valid_nonce = isinstance(nonce, str) and str(uuid.UUID(nonce)) == nonce
    except (ValueError, AttributeError):
        valid_nonce = False
    if not valid_nonce:
        raise ValueError("Activation nonce must be a canonical UUID")
    start = _timestamp(activation["issued_at"])
    current = now or datetime.now(timezone.utc)
    if start > current:
        raise ValueError("Activation cannot start in the future")
    if any(activation[key] is not None for key in ("expires_at", "max_tasks", "max_model_requests")):
        raise ValueError("Continuous code activation requires null expiry and aggregate limits")
    if "dsh" in activation and type(activation["dsh"]) is not dict:
        raise ValueError("Private DSH configuration must be an object")
    return activation


def arm_record(profile, activation):
    """Build the exact proposed owner approval record, without publishing it."""
    profile = validate_profile(profile)
    return {"schema": "harness-code-arm/v1", "authorized": True,
            **{key: activation[key] for key in PUBLIC_BINDING
               if key not in {"arm_comment_id", "arm_body_sha256", "runner_label"}},
            "goal_sha256": digest(profile["goal"])}


def check_arm(client, profile, activation, *, now=None):
    profile = validate_profile(profile)
    validate_activation(profile, activation, now=now)
    if client.repository != profile["repository"]:
        raise ValueError("Client repository differs from the approved project")
    control = client.get_issue(activation["control_issue"])
    if (control.get("number") != activation["control_issue"] or control.get("state") != "open"
            or control.get("user", {}).get("login") != profile["trusted_actor"] or "pull_request" in control):
        raise ValueError("Control Issue is not an open trusted-owner Issue")
    expected, bound = arm_record(profile, activation), []
    current = now or datetime.now(timezone.utc)
    for comment in client.list_comments(activation["control_issue"]):
        body = comment.get("body", "")
        if comment.get("user", {}).get("login") != profile["trusted_actor"] or ARM_MARKER.strip() not in body:
            continue
        record = parse_record(body, ARM_MARKER)
        if record.get("repository") != profile["repository"]:
            raise ValueError("Foreign owner authorization in control Issue")
        if record.get("nonce") == activation["nonce"]:
            if (comment.get("id") != activation["arm_comment_id"] or record != expected
                    or not comment.get("created_at") or comment["created_at"] != comment.get("updated_at")
                    or digest(body) != activation["arm_body_sha256"]
                    or not _timestamp(activation["issued_at"]) <= _timestamp(comment["created_at"]) <= current):
                raise ValueError("Owner authorization was edited, revoked, duplicated or is stale")
            bound.append(comment)
        elif record.get("authorized") is True and (record.get("expires_at") is None or _timestamp(record["expires_at"]) > current):
            raise ValueError("Another active code authorization conflicts with this run")
    if len(bound) != 1:
        raise ValueError("One immutable fresh owner authorization is required")
    if client.get_ref(profile["default_branch"]).get("object", {}).get("sha") != activation["workflow_sha"]:
        raise ValueError("Reviewed deployment branch changed")
    return expected


def validate_event(profile, activation, event, env):
    profile = validate_profile(profile)
    validate_activation(profile, activation)
    expected = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": profile["repository"],
        "GITHUB_EVENT_NAME": "issues", "GITHUB_ACTOR": profile["trusted_actor"], "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_WORKFLOW_SHA": activation["workflow_sha"], "RUNNER_OS": "Windows",
        "GITHUB_WORKFLOW_REF": profile["repository"] + "/" + WORKFLOW_PATH + "@refs/heads/" + profile["default_branch"]}
    if any(env.get(key) != value for key, value in expected.items()):
        raise ValueError("Only the reviewed first-attempt Windows code Issue job can generate")
    if not isinstance(env.get("GITHUB_RUN_ID"), str) or not re.fullmatch(r"[1-9][0-9]{0,19}", env["GITHUB_RUN_ID"]):
        raise ValueError("Invalid workflow run ID")
    if (type(event) is not dict or event.get("action") != "opened"
            or event.get("sender", {}).get("login") != profile["trusted_actor"]
            or event.get("repository", {}).get("full_name") != profile["repository"]
            or event.get("repository", {}).get("private") is not True):
        raise ValueError("Code Issue event repository, author or type was not approved")
    issue = event.get("issue", {})
    if (not _positive(issue.get("number")) or str(issue["number"]) != env.get("HARNESS_ISSUE")
            or issue.get("state") != "open" or "pull_request" in issue
            or issue.get("user", {}).get("login") != profile["trusted_actor"]):
        raise ValueError("Event Issue identity or owner mismatch")
    return issue["number"]


def validate_task_record(profile, activation, record):
    profile = validate_profile(profile)
    _object(record, TASK_KEYS, "Code task")
    expected = {"schema": "harness-code-task/v1", "repository": profile["repository"],
        "profile_sha256": profile_digest(profile), "nonce": activation["nonce"], "control_issue": activation["control_issue"]}
    if not _positive(record["control_issue"]) or any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("Code task is bound to another activation")
    number = record["number"]
    if type(number) is not int or not 1 <= number <= 2**31 - 1:
        raise ValueError("Code task number must be a positive 32-bit sequence number")
    validate_task_id(record["task_id"])
    _text(record["goal"], "Task goal", multiline=True)
    if not _hex(record["base_sha"], 40):
        raise ValueError("Task base must be an exact commit SHA")
    allowed = _paths(record["allowed_paths"], "Task allowed_paths", maximum=16, template=False, writable=True)
    if not set(allowed) <= set(expand_paths(profile, record["task_id"])):
        raise ValueError("Task writes exceed the reviewed profile")
    context = record["context"]
    if type(context) is not list or len(context) > 24:
        raise ValueError("Task context must contain at most 24 explicit slices")
    allowed_context = set(expand_paths(profile, record["task_id"], "context_paths")) | set(expand_paths(profile, record["task_id"]))
    seen = set()
    for section in context:
        _object(section, {"path", "start_line", "end_line"}, "Context slice")
        path = validate_path(section["path"])
        first, last = section["start_line"], section["end_line"]
        if (path not in allowed_context or type(first) is not int or type(last) is not int
                or not 1 <= first <= last <= 1000000 or last - first + 1 > 1200):
            raise ValueError("Context slice exceeds approved paths or line bounds")
        identity = (path, first, last)
        if identity in seen:
            raise ValueError("Duplicate context slice")
        seen.add(identity)
    if number == 1:
        if (record["base_sha"] != activation["workflow_sha"] or record["previous_pr"] is not None
                or record["previous_head"] is not None or record["transition"] != "INITIAL"):
            raise ValueError("First task must begin at the reviewed deployment SHA without a predecessor")
    else:
        if (not _positive(record["previous_pr"]) or not _hex(record["previous_head"], 40)
                or record["base_sha"] != record["previous_head"] or record["transition"] not in {"NEXT", "REWORK"}):
            raise ValueError("Successor task requires a bound predecessor head and NEXT/REWORK transition")
        if record["transition"] == "REWORK" and not re.search(r"修复|返工|纠正|\b(?:fix|repair|rework|correct)\b", record["goal"], re.I):
            raise ValueError("REWORK task must explicitly describe a repair")
    return copy.deepcopy(record)


def task_snapshot(client, profile, activation, issue_number, *, expected_body_sha=None, verify_previous=True):
    profile = validate_profile(profile)
    validate_activation(profile, activation)
    if not _positive(issue_number) or issue_number == activation["control_issue"]:
        raise ValueError("Invalid code task Issue")
    if client.repository != profile["repository"]:
        raise ValueError("Client repository differs from this project")
    issue = client.get_issue(issue_number)
    record = validate_task_record(profile, activation, parse_record(issue.get("body", ""), TASK_MARKER))
    if (not _positive(issue.get("number")) or issue.get("number") != issue_number or issue.get("state") != "open" or "pull_request" in issue
            or issue.get("user", {}).get("login") != profile["trusted_actor"] or issue.get("title") != record["task_id"]):
        raise ValueError("Code task must be an open trusted-owner Issue with exact task title")
    body_sha = digest(issue["body"])
    if expected_body_sha is not None and body_sha != expected_body_sha:
        raise ValueError("Code task changed during execution")
    if record["number"] > 1 and verify_previous:
        from .coding_publisher import verify_report_pr
        previous = verify_report_pr(client, profile, activation, client.get_pr(record["previous_pr"]),
                                    expected_number=record["number"] - 1, expected_head=record["previous_head"])
        if not isinstance(previous, dict) or previous.get("check_status") not in {"PASS", "FAIL"}:
            raise ValueError("Predecessor lacks verified check results")
        if record["transition"] == "NEXT" and previous["check_status"] != "PASS":
            raise ValueError("NEXT requires a predecessor with passing checks")
        if previous.get("task_id") == record["task_id"] and record["transition"] == "NEXT":
            raise ValueError("NEXT must have a new task identity")
    return issue, record, body_sha


def report_path(activation, number):
    if type(number) is not int or not 1 <= number <= 2**31 - 1:
        raise ValueError("Invalid report task number")
    nonce = activation.get("nonce")
    try:
        valid = isinstance(nonce, str) and str(uuid.UUID(nonce)) == nonce
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("Report path requires the exact canonical activation nonce")
    return f"Docs/Reports/Harness/{nonce}-{number}.md"


def validate_changes(profile, task, changes):
    """Validate text-only replacements; the publisher independently checks base blobs."""
    if type(changes) is not list or not 1 <= len(changes) <= 16:
        raise ValueError("A change set must contain one to sixteen exact text files")
    approved = set(task["allowed_paths"]) & set(expand_paths(profile, task["task_id"]))
    seen, total = set(), 0
    for change in changes:
        _object(change, {"path", "operation", "before_sha256", "content", "content_sha256"}, "Code change")
        path = validate_path(change["path"], writable=True)
        if path not in approved or path.casefold() in seen:
            raise ValueError("Change path is duplicated or outside the approved task")
        seen.add(path.casefold())
        operation, before = change["operation"], change["before_sha256"]
        if operation not in {"add", "modify"} or (operation == "add" and before is not None) or (operation == "modify" and not _hex(before, 64)):
            raise ValueError("Changes must add a new file or bind an exact previous content hash")
        content = change["content"]
        if not isinstance(content, str) or "\x00" in content:
            raise ValueError("Changed file content must be UTF-8 text without NUL")
        try:
            raw = content.encode("utf-8")
        except UnicodeError as error:
            raise ValueError("Changed file content must be valid UTF-8") from error
        total += len(raw)
        if len(raw) > 4 * 1024 * 1024 or total > 16 * 1024 * 1024 or SECRET_PATTERN.search(raw):
            raise ValueError("Changed content exceeds limits or contains credential-shaped data")
        if not _hex(change["content_sha256"], 64) or digest(content) != change["content_sha256"]:
            raise ValueError("Changed content hash differs from its exact UTF-8 bytes")
    return copy.deepcopy(changes)


def _validate_checks(profile, task, changes, checks, status):
    if type(checks) is not list or len(checks) != len(profile["checks"]):
        raise ValueError("Every configured check must be reported in profile order")
    changed = {change["path"] for change in changes}
    keys = {"id", "status", "exit_code", "timed_out", "output_limit_exceeded", "stdout_sha256", "stderr_sha256", "duration_seconds", "reason"}
    for configured, actual in zip(profile["checks"], checks):
        _object(actual, keys, "Check evidence")
        triggered = bool(changed & {path.replace("{task_id}", task["task_id"]) for path in configured["paths"]})
        state = actual["status"]
        if (actual["id"] != configured["id"] or state not in {"PASS", "FAIL", "NOT_RUN", "NOT_REQUIRED"}
                or triggered == (state == "NOT_REQUIRED")):
            raise ValueError("Check evidence does not match the configured scope")
        code = actual["exit_code"]
        if code is not None and type(code) is not int:
            raise ValueError("Check exit code must be an integer or null")
        if type(actual["timed_out"]) is not bool or type(actual["output_limit_exceeded"]) is not bool:
            raise ValueError("Check termination flags must be booleans")
        for key in ("stdout_sha256", "stderr_sha256"):
            if actual[key] is not None and not _hex(actual[key], 64):
                raise ValueError("Check log digest must be an exact SHA256 or null")
        elapsed = actual["duration_seconds"]
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("Check duration must be finite and nonnegative")
        _text(actual["reason"], "Check reason", maximum=512, multiline=True, empty=True)
        if state == "PASS" and (type(code) is not int or code != 0 or actual["timed_out"] or actual["output_limit_exceeded"]
                                or actual["stdout_sha256"] is None or actual["stderr_sha256"] is None):
            raise ValueError("PASS requires successful process and bounded original log evidence")
        if state in {"NOT_REQUIRED", "NOT_RUN"} and (code is not None or actual["timed_out"] or actual["output_limit_exceeded"]
                    or actual["stdout_sha256"] is not None or actual["stderr_sha256"] is not None):
            raise ValueError("An unexecuted check cannot claim process evidence")
    expected = "PASS" if all(check["status"] in {"PASS", "NOT_REQUIRED"} for check in checks) else "FAIL"
    if status != expected:
        raise ValueError("Aggregate check status disagrees with the per-check evidence")


def validate_artifact(profile, activation, artifact, env):
    profile = validate_profile(profile)
    validate_activation(profile, activation)
    keys = set(PUBLIC_BINDING) | {"schema", "issue", "issue_body_sha256", "task", "changes", "raw_model_output_sha256",
        "validated_change_set_sha256", "checks", "check_status", "session_id", "model", "version", "model_requests",
        "exit_code", "guard", "usage", "run_id", "run_attempt"}
    _object(artifact, keys, "Code artifact")
    try:
        size = len(json.dumps(artifact, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, UnicodeError) as error:
        raise ValueError("Code artifact must be finite UTF-8 JSON") from error
    if (artifact["schema"] != "harness-code-artifact/v1" or size > 24 * 1024 * 1024
            or any(artifact[key] != activation[key] for key in PUBLIC_BINDING)):
        raise ValueError("Code artifact differs from the approved activation or exceeds its size bound")
    if (not _positive(artifact["issue"]) or artifact["issue"] == activation["control_issue"]
            or not _hex(artifact["issue_body_sha256"], 64)
            or not isinstance(artifact["run_id"], str) or not re.fullmatch(r"[1-9][0-9]{0,19}", artifact["run_id"])
            or type(artifact["run_attempt"]) is not int or artifact["run_attempt"] != 1):
        raise ValueError("Invalid artifact Issue or first-attempt run identity")
    expected = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": profile["repository"], "GITHUB_EVENT_NAME": "issues",
        "GITHUB_ACTOR": profile["trusted_actor"], "GITHUB_WORKFLOW_SHA": activation["workflow_sha"],
        "GITHUB_RUN_ID": artifact["run_id"], "GITHUB_RUN_ATTEMPT": "1", "HARNESS_ISSUE": str(artifact["issue"]),
        "GITHUB_WORKFLOW_REF": profile["repository"] + "/" + WORKFLOW_PATH + "@refs/heads/" + profile["default_branch"]}
    if any(env.get(key) != value for key, value in expected.items()):
        raise ValueError("Artifact is not from the exact approved workflow run and Issue")
    task = validate_task_record(profile, activation, artifact["task"])
    changes = validate_changes(profile, task, artifact["changes"])
    if (not _hex(artifact["raw_model_output_sha256"], 64)
            or artifact["validated_change_set_sha256"] != canonical_digest(changes)):
        raise ValueError("Artifact model-output/change-set digests are invalid")
    validate_model_result({**artifact, "request_count": artifact["model_requests"],
                           "mode": "bounded-tool-free-headless", "provider": "deepseek-account"})
    guard = artifact["guard"]
    if (guard != {"request_count": 1, "visible_tool_count": 0, "violations": []}
            or type(guard.get("request_count")) is not int or type(guard.get("visible_tool_count")) is not int):
        raise ValueError("Artifact does not prove a single tool-free official model request")
    usage = artifact["usage"]
    if (type(usage) is not dict or not set(usage) <= {"inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "totalTokens"}
            or any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 10**9 for value in usage.values())):
        raise ValueError("Usage must contain bounded numeric token counters")
    _validate_checks(profile, task, changes, artifact["checks"], artifact["check_status"])
    return copy.deepcopy(artifact)
