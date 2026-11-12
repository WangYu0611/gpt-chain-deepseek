"""Synthetic repository fixtures. No real authorization, provider or credentials."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import Mock

from lab_harness.portable_policy import ARM_MARKER, PUBLIC_BINDING, TASK_MARKER, arm_record, digest
from lab_harness.project_profile import make_profile, profile_digest, task_spec

BASE = "a" * 40
PREVIOUS_HEAD = "b" * 40
CONTROLLER = "c" * 64
NONCE = "12345678-1234-4234-8234-123456789abc"
WORKFLOW_PATH = ".github/workflows/harness-documents.yml"


def document(profile, number=1):
    spec = task_spec(profile, number)
    parts = [spec["validation"]["first_line"], "", *spec["validation"]["required_headings"],
             "这是一份离线测试文档，只用于验证任务交接、文档内容和真实执行证据的核对规则，不代表项目功能已经完成。" * 2]
    count = spec["validation"]["bullet_count"]
    if count is not None:
        parts.extend("- 核对文档和报告中记录的任务与执行证据。" for _ in range(count))
    return "\n".join(parts) + "\n"


def make_generator(profile, number=1):
    def generate(prompt, workspace, out, config):
        Path(out).mkdir(parents=True, exist_ok=True)
        guard = Path(out) / "guard-report.json"
        guard.write_text(json.dumps({"request_count": 1, "visible_tool_count": 0, "violations": []}), encoding="utf-8")
        return {"guard_report_path": str(guard), "final_text": document(profile, number),
                "exit_code": 0, "request_count": 1, "session_id": "session-fixture",
                "model": "deepseek-flash", "version": "0.2.1-rc.3",
                "mode": "bounded-tool-free-headless", "provider": "deepseek-account",
                "model_called": True, "usage": {}}
    return Mock(side_effect=generate)


class FakeClient:
    def __init__(self, profile, activation, issue):
        self.repository = profile["repository"]
        self.default_branch = profile["default_branch"]
        self.control_issue = {"number": activation["control_issue"], "state": "open",
                              "user": {"login": profile["trusted_actor"]}, "title": "Offline control"}
        self.issue = issue
        self.comments = []
        self.ref_sha = activation["workflow_sha"]
        self.prs = []
        self.previous = {"number": 6101, "head": {"sha": PREVIOUS_HEAD}}
        self.issue_reads = []

    def get_issue(self, number):
        self.issue_reads.append(number)
        if number == self.control_issue["number"]:
            return copy.deepcopy(self.control_issue)
        if number == self.issue["number"]:
            return copy.deepcopy(self.issue)
        raise AssertionError(f"Unexpected fixture issue {number}")

    def list_comments(self, number):
        if number != self.control_issue["number"]:
            raise AssertionError(f"Unexpected fixture comment scope {number}")
        return copy.deepcopy(self.comments)

    def get_ref(self, branch):
        if branch != self.default_branch:
            raise AssertionError(f"Unexpected fixture branch {branch}")
        return {"object": {"sha": self.ref_sha}}

    def get_pr(self, number):
        if number != self.previous["number"]:
            raise AssertionError(f"Unexpected fixture predecessor {number}")
        return copy.deepcopy(self.previous)

    def _list(self, path, params):
        if path != "/pulls":
            raise AssertionError(f"Unexpected fixture list path {path}")
        return copy.deepcopy(self.prs)


def bind_arm(profile, activation, client):
    body = ARM_MARKER + json.dumps(arm_record(profile, activation), ensure_ascii=False, separators=(",", ":"))
    activation["arm_body_sha256"] = digest(body)
    client.comments = [{"id": activation["arm_comment_id"], "body": body,
                        "user": {"login": profile["trusted_actor"]},
                        "created_at": activation["issued_at"], "updated_at": activation["issued_at"]}]


def fixture(number=1, *, repository="Example/alpha", branch="main", actor="Example", profile=None):
    profile = copy.deepcopy(profile) if profile is not None else make_profile(repository, branch, actor)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    activation = {"schema": "harness-activation/v1", "enabled": True,
                  "repository": profile["repository"], "profile_sha256": profile_digest(profile),
                  "workflow_sha": BASE, "controller_sha256": CONTROLLER,
                  "control_issue": 5001, "arm_comment_id": 9001, "arm_body_sha256": "0" * 64,
                  "nonce": NONCE, "issued_at": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "expires_at": (now + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "max_model_requests": 2, "runner_label": profile["runner_label"], "dsh": {"fixture_only": True}}
    spec = task_spec(profile, number) if number in (1, 2) else profile["stop_task"]
    record = {"schema": "harness-task/v1", "repository": profile["repository"],
              "profile_sha256": profile_digest(profile), "nonce": NONCE, "control_issue": 5001,
              "number": number, "task_id": spec["task_id"], "dispatch": number in (1, 2),
              "previous_pr": 6101 if number > 1 else None,
              "previous_head": PREVIOUS_HEAD if number > 1 else None,
              "document_path": spec.get("document_path"), "report_path": spec.get("report_path")}
    issue = {"number": 5100 + number, "state": "open", "title": spec["task_id"],
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


def artifact_fixture(profile, activation, event, env, *, number=1):
    spec, text = task_spec(profile, number), document(profile, number)
    return {"schema": "harness-document/v1", **{key: activation[key] for key in PUBLIC_BINDING},
            "task_number": number, "task_id": spec["task_id"], "issue": event["issue"]["number"],
            "issue_body_sha256": digest(event["issue"]["body"]),
            "document_path": spec["document_path"], "report_path": spec["report_path"],
            "document": text, "document_sha256": digest(text), "model_requests": 1,
            "session_id": "session-fixture", "model": "deepseek-flash", "version": "0.2.1-rc.3",
            "exit_code": 0, "run_id": env["GITHUB_RUN_ID"], "run_attempt": 1,
            "guard": {"request_count": 1, "visible_tool_count": 0, "violations": []}, "usage": {}}
