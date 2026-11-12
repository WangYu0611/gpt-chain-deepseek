"""Separate GitHub-hosted publisher for the fixed, bounded arithmetic smoke.

No model calls. Never writes main, force-pushes, merges or retries unknown writes.
The Actions token is available here, not to the model's local child process.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lab_harness.github import GitHubClient, GitHubError, UncertainWrite
from lab_harness.smoke_policy import SmokeError

REPOSITORY = "WangYu0611/chatgpt-deepseek-harness-test-20261028"
OWNER = "WangYu0611"
SOURCE_PATH = "src/arithmetic.py"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_envelope(data: dict, environment: dict) -> None:
    """Bind an artifact to this exact run and Issue before any remote write."""
    from lab_harness.smoke_policy import validate_result, validate_source
    validate_result(data)
    validate_source(data["source_code"])
    if data.get("repository") != REPOSITORY or data.get("source_path") != SOURCE_PATH:
        raise ValueError("Artifact repository or file is outside the fixed smoke")
    expected = {"run_id": environment.get("GITHUB_RUN_ID"),
                "run_attempt": environment.get("GITHUB_RUN_ATTEMPT"),
                "issue": environment.get("SMOKE_ISSUE")}
    if (environment.get("GITHUB_REPOSITORY") != REPOSITORY
            or environment.get("GITHUB_ACTIONS") != "true"
            or environment.get("GITHUB_EVENT_NAME") != "issues"
            or environment.get("GITHUB_ACTOR") != OWNER):
        raise ValueError("Publisher must run in the trusted repository Issue workflow")
    for key, value in expected.items():
        if not value or not re.fullmatch(r"[1-9][0-9]*", str(value)) or str(data.get(key)) != str(value):
            raise ValueError("Artifact does not belong to this Issue/run/attempt")
    if digest(data["source_code"]) != data["source_sha256"]:
        raise ValueError("Source digest differs from the validated artifact")
    if data.get("model_request_count") != 1 or data.get("harness_status") != "completed":
        raise ValueError("A completed single model request is required")
    if data.get("tests", {}).get("exit_code") != 0:
        raise ValueError("Local tests did not pass")
    for key in ("session_id", "model", "version"):
        if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", data[key]):
            raise ValueError("Unsafe public model metadata")


def check_current(client, data: dict) -> dict:
    issue = client.get_issue(data["issue"])
    if (issue.get("number") != data["issue"] or issue.get("state") != "open"
            or issue.get("user", {}).get("login") != OWNER or "pull_request" in issue
            or "agent:ready" not in {x.get("name") for x in issue.get("labels", [])}):
        raise ValueError("Issue is no longer an eligible owned open smoke task")
    if digest(issue.get("body") or "") != data["issue_body_sha256"]:
        raise ValueError("Issue changed after model execution")
    main = client.get_ref("main")
    if main.get("object", {}).get("sha") != data["base_sha"]:
        raise ValueError("main changed after execution; fresh evidence is required")
    return issue


def independent_tests(source: str) -> dict:
    """Re-run fixed assertions after AST validation, without passing credentials."""
    from lab_harness.smoke_policy import validate_source
    validate_source(source)
    code = '''import importlib.util
import pathlib
import unittest
spec = importlib.util.spec_from_file_location("smoke_arithmetic", pathlib.Path(__file__).with_name("arithmetic.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
class ArithmeticSmoke(unittest.TestCase):
    def test_add_positive(self): self.assertEqual(module.add(2, 3), 5)
    def test_add_negative(self): self.assertEqual(module.add(-4, 1), -3)
    def test_add_zero(self): self.assertEqual(module.add(0, 0), 0)
    def test_add_fraction(self): self.assertAlmostEqual(module.add(0.1, 0.2), 0.3)
    def test_multiply_positive(self): self.assertEqual(module.multiply(6, 7), 42)
    def test_multiply_negative(self): self.assertEqual(module.multiply(-3, 5), -15)
    def test_multiply_two_negative(self): self.assertEqual(module.multiply(-3, -5), 15)
    def test_multiply_zero(self): self.assertEqual(module.multiply(123, 0), 0)
    def test_multiply_fraction(self): self.assertAlmostEqual(module.multiply(0.5, 0.2), 0.1)
    def test_multiply_large(self): self.assertEqual(module.multiply(10**8, 3), 300000000)
unittest.main(verbosity=2)
'''
    with tempfile.TemporaryDirectory(prefix="dsh-publisher-tests-") as temporary:
        folder = Path(temporary)
        (folder / "arithmetic.py").write_text(source, encoding="utf-8")
        test = folder / "test_candidate.py"
        test.write_text(code, encoding="utf-8")
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"}}
        process = subprocess.run([sys.executable, "-I", str(test)], cwd=folder,
                                 env=environment, capture_output=True, timeout=15,
                                 encoding="utf-8", errors="replace", check=False)
    if process.returncode != 0 or not re.search(r"Ran 10 tests\b", process.stderr):
        raise ValueError("Independent cloud tests failed")
    return {"command": "python -I test_candidate.py", "count": 10, "exit_code": 0}


def _get_optional_ref(client, branch: str):
    try:
        return client.get_ref(branch)
    except GitHubError as error:
        if error.status == 404:
            return None
        raise


def _verify_commit(client, head: str, data: dict, message: str) -> None:
    commit = client.request("GET", "/git/commits/" + GitHubClient._sha(head))
    if (commit.get("message") != message or [p.get("sha") for p in commit.get("parents", [])] != [data["base_sha"]]):
        raise ValueError("Existing task branch belongs to different work; no update performed")
    comparison = client.request("GET", "/compare/" + data["base_sha"] + "..." + head)
    files = comparison.get("files", [])
    if (comparison.get("total_commits") != 1 or len(files) != 1
            or files[0].get("filename") != SOURCE_PATH
            or files[0].get("status") != "modified"):
        raise ValueError("Task commit changes files outside the single permitted source")
    content = client.request("GET", "/contents/" + SOURCE_PATH, params={"ref": head})
    if content.get("type") != "file" or content.get("encoding") != "base64":
        raise ValueError("Task source is not a regular base64 GitHub file")
    source = base64.b64decode(content["content"], validate=False).decode("utf-8")
    if digest(source) != data["source_sha256"]:
        raise ValueError("Task branch source differs from the tested artifact")


def publish(client, data: dict, *, cloud_tests: dict) -> dict:
    """Idempotent reconciliation; stable branch/marker, no write retries."""
    check_current(client, data)
    issue_number = data["issue"]
    branch = f"agent/issue-{issue_number}"
    marker = f"<!-- bounded-dsh-smoke:issue={issue_number}:source={data['source_sha256']} -->"
    message = f"agent: DSH-SMOKE-{issue_number} add multiply\n\nsource-sha256: {data['source_sha256']}\nrun: {data['run_id']}/{data['run_attempt']}"
    reference = _get_optional_ref(client, branch)
    if reference is None:
        base = client.request("GET", "/git/commits/" + data["base_sha"])
        blob = client.create_blob(data["source_code"], encoding="utf-8")
        tree = client.create_tree(base["tree"]["sha"], [{"path": SOURCE_PATH, "mode": "100644", "type": "blob", "sha": blob["sha"]}])
        commit = client.create_commit(message, tree["sha"], [data["base_sha"]])
        head = commit["sha"]
        check_current(client, data)
        client.create_ref(branch, head)
    else:
        head = reference["object"]["sha"]
    _verify_commit(client, head, data, message)
    # Include closed PRs so a rerun never creates a replacement after a human closes one.
    prs = client._list("/pulls", {"state": "all", "head": OWNER + ":" + branch})
    if len(prs) > 1:
        raise ValueError("Multiple task PRs require human reconciliation")
    if prs:
        pr = prs[0]
        if (marker not in (pr.get("body") or "") or pr.get("head", {}).get("sha") != head
                or pr.get("base", {}).get("ref") != "main"
                or pr.get("head", {}).get("repo", {}).get("full_name") != REPOSITORY):
            raise ValueError("Existing PR does not match this exact tested result")
    else:
        check_current(client, data)
        run_url = f"https://github.com/{REPOSITORY}/actions/runs/{data['run_id']}"
        summary = {key: data[key] for key in ("session_id", "model", "version", "model_request_count", "base_sha", "source_sha256")}
        summary.update(result_commit_sha=head,
                       local_tests={key: data["tests"][key] for key in ("count", "exit_code", "timed_out", "output_limit_exceeded")},
                       independent_cloud_tests=cloud_tests)
        body = (f"{marker}\n\nCloses #{issue_number}. Official DeepSeek Harness generated the arithmetic source in one bounded, tool-free request on the local Windows Runner. "
                "A trusted controller checked an AST allowlist and ran actual tests; this separate GitHub-hosted job revalidated the task and independently ran 10 tests before publication.\n\n"
                f"[Execution and sanitized evidence artifact]({run_url})\n\n```json\n{json.dumps(summary, ensure_ascii=True, indent=2)}\n```\n\n"
                "This proves only the narrow Issue → local Runner → official DeepSeek → tested PR path. ChatGPT web Scheduled Tasks, general shell-capable coding, automated rework and full unattended acceptance are NOT_RUN. No automatic merge. "
                "PR workflows created with GITHUB_TOKEN may require approval; the independent tests above are not a claim that PR CI has already run.")
        pr = client.create_pr(f"DSH-SMOKE-{issue_number}: add DeepSeek-generated multiply", body, branch, "main", draft=False)
    result = {"repository": REPOSITORY, "issue": issue_number, "pr": pr["number"],
              "url": pr["html_url"], "head_sha": head, "state": pr.get("state"),
              "cloud_tests": cloud_tests, "chatgpt_web_acceptance": "NOT_RUN", "merged": bool(pr.get("merged", False))}
    # A closed/merged existing PR is only reported, never resurrected or relabeled.
    if pr.get("state") == "open":
        comments = client.list_comments(issue_number)
        receipt = f"<!-- bounded-dsh-published:{issue_number}:{head} -->"
        if not any(receipt in (comment.get("body") or "") for comment in comments):
            client.comment(issue_number, f"{receipt}\nOfficial DeepSeek smoke produced {pr['html_url']} at `{head}`. Local and 10 independent cloud assertions passed. PR CI and ChatGPT web acceptance must be checked separately. No automatic merge.")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("publisher-result.json"))
    args = parser.parse_args()
    if args.artifact.stat().st_size > 65536:
        raise ValueError("Artifact exceeds the fixed smoke size limit")
    data = json.loads(args.artifact.read_text(encoding="utf-8"))
    validate_envelope(data, os.environ)
    tests = independent_tests(data["source_code"])
    client = GitHubClient(REPOSITORY, network_enabled=True, authorization_gate=lambda _: True,
                          token_env="GH_SMOKE_PUBLISH_TOKEN", max_attempts=2, max_pages=5)
    result = publish(client, data, cloud_tests=tests)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UncertainWrite:
        print("Publication outcome unknown. Do not re-run the model. Reconcile this branch/PR/receipt using the same artifact before retrying publication.", file=sys.stderr)
        raise SystemExit(2)
    except GitHubError as error:
        print(f"GitHub rejected publication (HTTP {error.status}). If PR creation is forbidden, enable the repository setting allowing Actions to create PRs or use the authorized connector to publish the same validated commit. No automatic retry.", file=sys.stderr)
        raise SystemExit(3)
    except (SmokeError, ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        print("Publication validation failed. Inspect the sanitized artifact and trusted configuration; no raw exception or model log was published.", file=sys.stderr)
        raise SystemExit(4)
