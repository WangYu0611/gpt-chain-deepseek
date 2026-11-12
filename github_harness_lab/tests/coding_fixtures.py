"""Offline code-task fixtures. No real project data or credentials."""
import copy
from datetime import datetime, timedelta, timezone
import json

from lab_harness.coding_contract import (
    ARM_MARKER, TASK_MARKER, WORKFLOW_PATH, LIMITS, PUBLIC_BINDING, arm_record, digest,
    profile_digest, repository_namespace, validate_profile, canonical_digest,
)
from tests.portable_fixtures import BASE, PREVIOUS_HEAD, CONTROLLER, NONCE, FakeClient


def make_profile(repository="Example/alpha", branch="main", actor="Example"):
    return validate_profile({
        "schema": "harness-code-project/v1", "repository": repository,
        "default_branch": branch, "trusted_actor": actor, "source_repository": "",
        "live_enabled": False, "runner_label": "harness-code-" + repository_namespace(repository),
        "goal": "按已有任务书实现有限修复，验证具体变化并交回独立验收。",
        "allowed_paths": ["Source/Example/Widget.cpp", "Docs/Reports/{task_id}.md"],
        "context_paths": ["AGENTS.md", "Docs/Tasks/{task_id}.md"],
        "materialize_paths": ["Source", "Config", "Example.uproject"],
        "checks": [{"id": "targeted", "argv": ["C:/tools/fake-check.exe", "{workspace}", "{output}", "{task_id}"],
                    "timeout_seconds": 120, "success_patterns": ["PASS"], "failure_patterns": ["FAILED"],
                    "paths": ["Source/Example/Widget.cpp", "Docs/Reports/{task_id}.md"]}],
        "limits": copy.deepcopy(LIMITS),
    })


def bind_arm(profile, activation, client):
    body = ARM_MARKER + json.dumps(arm_record(profile, activation), ensure_ascii=False, separators=(",", ":"))
    activation["arm_body_sha256"] = digest(body)
    client.comments = [{"id": activation["arm_comment_id"], "body": body,
                        "user": {"login": profile["trusted_actor"]},
                        "created_at": activation["issued_at"], "updated_at": activation["issued_at"]}]


def fixture(number=1, *, repository="Example/alpha", branch="main", actor="Example", profile=None):
    profile = copy.deepcopy(profile) if profile is not None else make_profile(repository, branch, actor)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    activation = {"schema": "harness-code-activation/v1", "enabled": True,
        "repository": profile["repository"], "profile_sha256": profile_digest(profile),
        "workflow_sha": BASE, "controller_sha256": CONTROLLER, "control_issue": 5001,
        "arm_comment_id": 9001, "arm_body_sha256": "0" * 64, "nonce": NONCE,
        "issued_at": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": None,
        "max_tasks": None, "max_model_requests": None, "runner_label": profile["runner_label"],
        "dsh": {"fixture_only": True}}
    record = {"schema": "harness-code-task/v1", "repository": profile["repository"],
        "profile_sha256": profile_digest(profile), "nonce": NONCE, "control_issue": 5001,
        "number": number, "task_id": "TASK-M2-21" if number == 1 else "TASK-M2-21A",
        "base_sha": BASE if number == 1 else PREVIOUS_HEAD,
        "previous_pr": None if number == 1 else 6101, "previous_head": None if number == 1 else PREVIOUS_HEAD,
        "transition": "INITIAL" if number == 1 else "NEXT", "goal": "按任务书修复界面问题，保持其他行为。",
        "allowed_paths": ["Source/Example/Widget.cpp"],
        "context": [{"path": "AGENTS.md", "start_line": 1, "end_line": 50}]}
    issue = {"number": 5100 + number, "state": "open", "title": record["task_id"],
        "user": {"login": profile["trusted_actor"]},
        "body": TASK_MARKER + json.dumps(record, ensure_ascii=False, separators=(",", ":"))}
    event = {"action": "opened", "sender": {"login": profile["trusted_actor"]},
             "repository": {"full_name": profile["repository"], "private": True}, "issue": copy.deepcopy(issue)}
    env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": profile["repository"], "GITHUB_EVENT_NAME": "issues",
        "GITHUB_ACTOR": profile["trusted_actor"], "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "8001",
        "GITHUB_WORKFLOW_SHA": BASE, "RUNNER_OS": "Windows", "HARNESS_ISSUE": str(issue["number"]),
        "GITHUB_WORKFLOW_REF": f"{profile['repository']}/{WORKFLOW_PATH}@refs/heads/{profile['default_branch']}"}
    client = FakeClient(profile, activation, issue)
    bind_arm(profile, activation, client)
    return profile, activation, event, env, client


def replace_task_record(client, **changes):
    record = json.loads(client.issue["body"].split(TASK_MARKER, 1)[1])
    record.update(changes)
    client.issue["body"] = TASK_MARKER + json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    return record


def artifact_fixture(profile, activation, event, env):
    task = json.loads(event["issue"]["body"].split(TASK_MARKER, 1)[1])
    content = "// Offline code fixture\nint WidgetValue() { return 1; }\n"
    changes = [{"path": "Source/Example/Widget.cpp", "operation": "modify", "before_sha256": digest("old\n"),
                "content": content, "content_sha256": digest(content)}]
    checks = [{"id": "targeted", "status": "PASS", "exit_code": 0, "timed_out": False,
               "output_limit_exceeded": False, "stdout_sha256": digest("PASS\n"), "stderr_sha256": digest(""),
               "duration_seconds": 1.5, "reason": "Reviewed check passed"}]
    return {"schema": "harness-code-artifact/v1", **{key: activation[key] for key in PUBLIC_BINDING},
        "issue": event["issue"]["number"], "issue_body_sha256": digest(event["issue"]["body"]), "task": task,
        "changes": changes, "raw_model_output_sha256": digest(json.dumps(changes)),
        "validated_change_set_sha256": canonical_digest(changes), "checks": checks, "check_status": "PASS",
        "session_id": "session-fixture", "model": "deepseek-flash", "version": "0.2.1-rc.3",
        "model_requests": 1, "exit_code": 0, "guard": {"request_count": 1, "visible_tool_count": 0, "violations": []},
        "usage": {}, "run_id": env["GITHUB_RUN_ID"], "run_attempt": 1}
