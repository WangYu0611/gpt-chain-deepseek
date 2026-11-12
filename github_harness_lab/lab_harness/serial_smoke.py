"""Two small document tasks, published by real web GPT and executed by DSH.

Only trusted fixed document specs go to the tool-free official model. Markdown
is validated and written, never executed. GitHub publication runs separately.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from .github import GitHubClient, GitHubError
from .smoke_policy import REPOSITORY, ACTOR, digest

EXPERIMENT = "GPTDSH_SERIAL_M0"
TASK_MARKER = "<!-- serial-task:v1 -->\n"
REPORT_MARKER = "<!-- serial-report:v1 -->\n"
ARM_MARKER = "<!-- serial-arm:v1 -->\n"
SPECS = {
    1: "写一份简短中文项目简介。第一行必须是 # TASK_M0_1，包含 ## 项目目标 和 ## 完成标准 两个小节；说明这是验证 GPT 连续派工与 DSH 执行的测试项目。正文至少40字。只输出Markdown，不要代码围栏、HTML、外部链接。",
    2: "写一份简短中文待办清单。第一行必须是 # TASK_M0_2，包含 ## 待办清单 小节和恰好三条以 - 开头的待办事项；至少40字。只输出Markdown，不要代码围栏、HTML、外部链接。",
}


def parse_record(body: str, marker: str) -> dict:
    if not isinstance(body, str) or body.count(marker) != 1 or len(body.encode()) > 16000:
        raise ValueError("Missing or ambiguous record")
    data, _ = json.JSONDecoder().raw_decode(body.split(marker, 1)[1].lstrip())
    if not isinstance(data, dict):
        raise ValueError("Record must be an object")
    return data


def validate_document(text: str, number: int) -> str:
    if type(number) is not int or number not in SPECS or not isinstance(text, str):
        raise ValueError("Only two fixed document tasks are executable")
    text = text.replace("\r\n", "\n").strip() + "\n"
    if not 40 <= len(text) <= 3000 or len(text.encode("utf-8")) > 12000:
        raise ValueError("Document length differs from the fixed task")
    if not text.startswith(f"# TASK_M0_{number}\n") or any(x in text for x in ("```", "<", ">", "\x00")):
        raise ValueError("Document title or plain Markdown policy failed")
    if re.search(r"(?i)(?:https?|file|javascript|data):", text):
        raise ValueError("External content is outside this document task")
    if number == 1 and not all(h in text for h in ("## 项目目标\n", "## 完成标准\n")):
        raise ValueError("Required project sections missing")
    if number == 2 and ("## 待办清单\n" not in text or len(re.findall(r"^- .+", text, re.M)) != 3):
        raise ValueError("Exactly three checklist entries are required")
    return text


def fresh(expiry):
    if not isinstance(expiry, str) or not expiry.endswith("Z") or datetime.fromisoformat(expiry.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
        raise ValueError("Experiment deadline passed")


def task_snapshot(client, issue_number, nonce, *, expected_body_sha=None):
    issue = client.get_issue(issue_number)
    record = parse_record(issue.get("body", ""), TASK_MARKER)
    number = record.get("number")
    if (type(number) is not int or number not in (1, 2) or issue.get("number") != issue_number
            or issue.get("state") != "open" or issue.get("user", {}).get("login") != ACTOR
            or "pull_request" in issue or issue.get("title") != f"TASK_M0_{number}"
            or record.get("experiment") != EXPERIMENT or record.get("nonce") != nonce
            or record.get("control_issue") != 5 or record.get("dispatch") is not True):
        raise ValueError("Only the two authorized web-created task Issues are executable")
    body_sha = digest(issue["body"])
    if expected_body_sha is not None and body_sha != expected_body_sha:
        raise ValueError("Task changed during execution")
    if number == 2:
        previous = record.get("previous_pr")
        if type(previous) is not int or previous <= 0:
            raise ValueError("TASK_M0_2 needs the first real report")
        pr = client.get_pr(previous)
        report = parse_record(pr.get("body", ""), REPORT_MARKER)
        if (pr.get("user", {}).get("login") != "github-actions[bot]"
                or pr.get("head", {}).get("repo", {}).get("full_name") != REPOSITORY
                or report.get("experiment") != EXPERIMENT or report.get("nonce") != nonce
                or report.get("task_number") != 1 or report.get("checks") != "PASS"
                or pr.get("head", {}).get("sha") != record.get("previous_head")
                or report.get("head_sha") != record.get("previous_head")):
            raise ValueError("Predecessor report does not match the prior completed document")
    return issue, record, body_sha


def check_arm(client, config):
    fresh(config["expires_at"])
    comments = client.list_comments(5)
    arm = next((c for c in comments if c.get("id") == config["arm_comment_id"]), None)
    if (not arm or arm.get("user", {}).get("login") != ACTOR
            or not arm.get("created_at") or arm.get("created_at") != arm.get("updated_at")
            or digest(arm["body"]) != config["arm_body_sha256"]):
        raise ValueError("Immutable owner authorization is missing")
    data = parse_record(arm["body"], ARM_MARKER)
    if (data.get("authorized") is not True or data.get("experiment") != EXPERIMENT
            or data.get("nonce") != config["nonce"] or data.get("workflow_sha") != config["workflow_sha"]
            or data.get("expires_at") != config["expires_at"] or data.get("max_model_requests") != 2):
        raise ValueError("Authorization differs from local configuration")
    if client.get_ref("main")["object"]["sha"] != config["workflow_sha"]:
        raise ValueError("Reviewed main changed")


def generate_task(config, event, env, *, root, client, generate):
    if (config.get("enabled") is not True or config.get("experiment") != EXPERIMENT
            or env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REPOSITORY") != REPOSITORY
            or env.get("GITHUB_EVENT_NAME") != "issues" or env.get("GITHUB_ACTOR") != ACTOR
            or env.get("GITHUB_RUN_ATTEMPT") != "1" or env.get("GITHUB_WORKFLOW_SHA") != config["workflow_sha"]
            or event.get("action") != "opened" or event.get("sender", {}).get("login") != ACTOR
            or event.get("repository", {}).get("private") is not True
            or event.get("repository", {}).get("full_name") != REPOSITORY):
        raise ValueError("Only an authorized new Issue can invoke the model")
    check_arm(client, config)
    issue_number = event["issue"]["number"]
    issue, task, body_sha = task_snapshot(client, issue_number, config["nonce"])
    number = task["number"]
    branch = f"agent/serial-{config['nonce'][:8]}-m0-{number}"
    if client._list("/pulls", {"state": "all", "head": ACTOR + ":" + branch}):
        raise ValueError("Task already has a report; do not rebill")
    folder = root / ".runtime" / "serial-output" / config["nonce"] / str(number)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "claim.json").open("x", encoding="utf-8") as stream:
        json.dump({"issue": issue_number, "run_id": env["GITHUB_RUN_ID"]}, stream)
    workspace = folder / "workspace"
    workspace.mkdir()
    result = generate(SPECS[number], workspace, folder / "dsh", config["dsh"])
    if result.get("exit_code") != 0 or result.get("request_count") != 1:
        raise ValueError("A real successful single DSH request is required")
    document = validate_document(result["final_text"], number)
    check_arm(client, config)
    task_snapshot(client, issue_number, config["nonce"], expected_body_sha=body_sha)
    artifact = {"schema": "serial-document/v1", "experiment": EXPERIMENT, "nonce": config["nonce"],
                "repository": REPOSITORY, "issue": issue_number, "task_number": number,
                "task_id": f"TASK_M0_{number}", "issue_body_sha256": body_sha,
                "base_sha": config["workflow_sha"], "expires_at": config["expires_at"],
                "document_path": f"docs/TASK_M0_{number}.md", "document": document,
                "document_sha256": digest(document), "session_id": result["session_id"],
                "model": result["model"], "version": result["version"], "model_requests": result["request_count"],
                "exit_code": result["exit_code"], "usage": result.get("usage"),
                "run_id": env["GITHUB_RUN_ID"], "run_attempt": env["GITHUB_RUN_ATTEMPT"],
                "arm_comment_id": config["arm_comment_id"], "arm_body_sha256": config["arm_body_sha256"]}
    (folder / "result.json").write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    return artifact, folder / "result.json"


def validate_artifact(data, env):
    if (data.get("schema") != "serial-document/v1" or data.get("experiment") != EXPERIMENT
            or data.get("repository") != REPOSITORY or env.get("GITHUB_REPOSITORY") != REPOSITORY
            or env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_EVENT_NAME") != "issues"
            or env.get("GITHUB_ACTOR") != ACTOR or str(data.get("issue")) != env.get("SERIAL_ISSUE")
            or data.get("base_sha") != env.get("GITHUB_WORKFLOW_SHA")
            or str(data.get("run_id")) != env.get("GITHUB_RUN_ID")
            or str(data.get("run_attempt")) != env.get("GITHUB_RUN_ATTEMPT")
            or data.get("model_requests") != 1 or data.get("exit_code") != 0):
        raise ValueError("Artifact is not from this authorized job")
    number = data.get("task_number")
    document = validate_document(data.get("document"), number)
    if (data.get("document_path") != f"docs/TASK_M0_{number}.md" or document != data["document"]
            or digest(document) != data.get("document_sha256")):
        raise ValueError("Document scope/hash mismatch")
    fresh(data["expires_at"])
    return document


def publish_task(client, data, env):
    document = validate_artifact(data, env)
    check_arm(client, {**data, "workflow_sha": data["base_sha"]})
    issue, task, _ = task_snapshot(client, data["issue"], data["nonce"], expected_body_sha=data["issue_body_sha256"])
    number = data["task_number"]
    if task["number"] != number:
        raise ValueError("Report task number differs from the authorized Issue")
    branch = f"agent/serial-{data['nonce'][:8]}-m0-{number}"
    message = f"DSH: complete TASK_M0_{number}\n\nsource: {data['document_sha256']}\nrun: {data['run_id']}"
    try:
        head = client.get_ref(branch)["object"]["sha"]
    except GitHubError as error:
        if error.status != 404:
            raise
        head = None
    if head is None:
        parent = client.request("GET", "/git/commits/" + data["base_sha"])
        blob = client.create_blob(document, encoding="utf-8")
        tree = client.create_tree(parent["tree"]["sha"], [{"path": data["document_path"], "mode": "100644", "type": "blob", "sha": blob["sha"]}])
        commit = client.create_commit(message, tree["sha"], [data["base_sha"]])
        head = commit["sha"]
        check_arm(client, {**data, "workflow_sha": data["base_sha"]})
        client.request("POST", "/git/refs", payload={"ref": "refs/heads/" + branch, "sha": head}, expected=(201,))
    commit = client.request("GET", "/git/commits/" + head)
    comparison = client.request("GET", f"/compare/{data['base_sha']}...{head}")
    if (commit.get("message") != message or [p["sha"] for p in commit["parents"]] != [data["base_sha"]]
            or len(comparison.get("files", [])) != 1 or comparison["files"][0]["filename"] != data["document_path"]):
        raise ValueError("Existing branch differs from this document task")
    content = client.request("GET", "/contents/" + data["document_path"], params={"ref": head})
    fetched = base64.b64decode(content["content"]).decode("utf-8")
    if validate_document(fetched, number) != document or digest(fetched) != data["document_sha256"]:
        raise ValueError("Actual committed document differs from the validated result")
    report = {key: data[key] for key in ("experiment", "nonce", "issue", "task_number", "task_id", "document_path", "document_sha256", "session_id", "model", "version", "model_requests", "exit_code", "run_id", "run_attempt")}
    report.update(schema="serial-report/v1", head_sha=head, checks="PASS", scope="one Markdown document; no generated code executed")
    receipt = REPORT_MARKER + json.dumps(report, ensure_ascii=False, separators=(",", ":"))
    body = f"## DSH 任务报告：TASK_M0_{number}\n\n关联 Issue #{data['issue']}。官方 DSH 已生成文档；独立云发布器已重新下载精确提交文件并验证标题、章节、长度和内容范围。\n\n{receipt}\n\n无需合并此 PR。由网页 GPT 读取文档和报告，验收后发布下一个任务。"
    prs = client._list("/pulls", {"state": "all", "head": ACTOR + ":" + branch})
    if len(prs) > 1:
        raise ValueError("Duplicate task reports")
    if prs:
        pr = prs[0]
        if pr.get("state") != "open" or pr.get("head", {}).get("sha") != head or parse_record(pr.get("body", ""), REPORT_MARKER) != report:
            raise ValueError("Existing report differs; no automatic replacement")
    else:
        pr = client.create_pr(f"{EXPERIMENT}: TASK_M0_{number} 完成报告", body, branch, "main", draft=False)
    marker = f"<!-- serial-report-link:{data['nonce']}:{number} -->"
    if not any(marker in (c.get("body") or "") for c in client.list_comments(data["issue"])):
        client.comment(data["issue"], f"{marker}\nTASK_M0_{number} 已完成。文档与真实 DSH 执行报告：{pr['html_url']}\nhead: `{head}`；结构检查 PASS。等待网页 GPT 接收报告并发布下一任务。")
    if not any((c.get("body") or "") == receipt for c in client.list_comments(pr["number"])):
        client.comment(pr["number"], receipt)
    print(json.dumps(report, ensure_ascii=False))
    return {"pr": pr["number"], "url": pr["html_url"], "head_sha": head, "task_id": data["task_id"]}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("generate", "publish"))
    parser.add_argument("--event", type=Path)
    parser.add_argument("--artifact", type=Path)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    client = GitHubClient(REPOSITORY, network_enabled=True, token_env="GH_SERIAL_TOKEN",
                          authorization_gate=lambda op: args.command == "publish" or op == "github-read")
    if args.command == "generate":
        from .bounded_dsh import run
        config = json.loads((root / ".private" / "serial-smoke.json").read_text(encoding="utf-8"))
        event = json.loads(args.event.read_text(encoding="utf-8-sig"))
        result, artifact = generate_task(config, event, dict(os.environ), root=root, client=client, generate=run)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
                stream.write("artifact_path=" + str(artifact) + "\n")
        print(json.dumps({k: result[k] for k in ("task_id", "session_id", "model_requests", "exit_code")}))
    else:
        if args.artifact.stat().st_size > 32768:
            raise ValueError("Artifact too large")
        result = publish_task(client, json.loads(args.artifact.read_text(encoding="utf-8")), dict(os.environ))
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "BLOCKED", "reason": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
