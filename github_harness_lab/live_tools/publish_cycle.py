"""Bounded cloud publisher and PR-comment waiter for the disclosed rework demo.

No model calls. Every mutation is confined to the fixed task branch, PR,
head-bound status, or journal. Unknown writes stop for later reconciliation.
Operational journal replay never attests ChatGPT hosted-run provenance.
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
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lab_harness.github import GitHubClient, GitHubError, UncertainWrite
from lab_harness.smoke_policy import validate_source
from lab_harness.rework_protocol import Policy, SCHEMA, MARKER, encode_record, replay_journal

REPOSITORY = "WangYu0611/chatgpt-deepseek-harness-test-20261028"
OWNER = "WangYu0611"
PUBLISHER = "github-actions[bot]"
LOOP_ID = "DSH-REWORK-002"
SOURCE_PATH = "src/arithmetic.py"
STATUS_CONTEXT = "dsh-cycle/acceptance-harness"
CRITERIA = ("arithmetic-correctness", "scope-safety", "execution-evidence")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _required(env, name):
    value = env.get(name)
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n\x00"):
        raise ValueError("Missing or unsafe trusted workflow setting: " + name)
    return value


def _id(value, name):
    if not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ValueError("Invalid " + name)
    return int(value)


def _sha(value, length=40):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{" + str(length) + "}", value):
        raise ValueError("Invalid SHA digest")
    return value


def branch_name(issue):
    return "agent/rework-002-issue-" + str(_id(issue, "issue"))


def validate_envelope(data: dict, env: dict, *, read_only=False) -> None:
    from lab_harness.cycle_policy import validate_cycle_artifact
    validate_cycle_artifact(data)
    if (env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REPOSITORY") != REPOSITORY
            or env.get("GITHUB_ACTOR") != OWNER or env.get("GITHUB_EVENT_NAME") not in {"issues", "workflow_dispatch"}
            or data.get("repository") != REPOSITORY or data.get("loop_id") != LOOP_ID):
        raise ValueError("Wrong workflow authority")
    mapping = {"issue": "CYCLE_ISSUE", "loop_id": "CYCLE_LOOP_ID", "revision": "CYCLE_REVISION",
               "base_sha": "CYCLE_BASE_SHA", "parent_sha": "CYCLE_PARENT_SHA",
               "issue_body_sha256": "CYCLE_ISSUE_BODY_SHA256", "workflow_sha": "CYCLE_WORKFLOW_SHA",
               "run_id": "GITHUB_RUN_ID", "run_attempt": "GITHUB_RUN_ATTEMPT"}
    for field, name in mapping.items():
        if str(data.get(field)) != _required(env, name):
            raise ValueError("Artifact differs from trusted workflow context: " + field)
    if env.get("GITHUB_WORKFLOW_SHA") != data["workflow_sha"]:
        raise ValueError("Workflow SHA is not the actual executing workflow")
    revision = data["revision"]
    if revision == 0 and data["parent_sha"] != data["base_sha"]:
        raise ValueError("Initial candidate parent must be the reviewed base")
    if data.get("execution_stage") != f"generate-{revision}" or data.get("ci_stage") != f"publish-{revision}":
        raise ValueError("Artifact has wrong per-revision stage identity")
    _required(env, "CYCLE_AUTOMATION_ID")
    _id(_required(env, "CYCLE_ARTIFACT_ID"), "artifact ID")
    accepted_jobs = {f"publish-{revision}"}
    if read_only:
        accepted_jobs.add(f"wait-{revision}")
    if env.get("GITHUB_JOB", "").replace("_", "-") not in accepted_jobs:
        raise ValueError("Wrong publisher job")


def policy_for(data, pr, env):
    return Policy(repository=REPOSITORY, issue=data["issue"], pr=pr, base_sha=data["base_sha"],
                  criteria_sha256=data["issue_body_sha256"], workflow_sha=data["workflow_sha"],
                  publisher_login=PUBLISHER, reviewer_login=OWNER,
                  automation_id=_required(env, "CYCLE_AUTOMATION_ID"), required_criteria=CRITERIA, max_reworks=2, loop_id=LOOP_ID)


def independent_tests(source: str, *, emit_log=False) -> dict:
    """Run the trusted suite; use a deterministic, exact-source receipt.

    Raw tracebacks contain temporary paths and elapsed times. They are not an
    idempotency key; this receipt preserves actual exit/count/failed test IDs.
    """
    from lab_harness.cycle_policy import test_source
    actual = test_source(source)
    if emit_log:
        print(actual["log"])
    result = {"count": actual["count"], "exit_code": actual["exit_code"],
              "status": actual["conclusion"], "failed_cases": actual["failed_tests"],
              "source_sha256": digest(source), "command": "python -I trusted-10-assertion-suite"}
    result["receipt_sha256"] = digest(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return result


def check_current(client, data):
    issue = client.get_issue(data["issue"])
    if (issue.get("number") != data["issue"] or issue.get("state") != "open" or "pull_request" in issue
            or issue.get("user", {}).get("login") != OWNER
            or digest(issue.get("body") or "") != data["issue_body_sha256"]
            or "agent:cycle-ready" not in {x.get("name") for x in issue.get("labels", [])}):
        raise ValueError("Issue differs from the authorized task")
    if client.get_ref("main").get("object", {}).get("sha") != data["base_sha"]:
        raise ValueError("main changed after the reviewed configuration")
    return issue


def optional_ref(client, branch):
    try:
        return client.get_ref(branch)["object"]["sha"]
    except GitHubError as exc:
        if exc.status == 404:
            return None
        raise


def source_at(client, head):
    value = client.request("GET", "/contents/" + SOURCE_PATH, params={"ref": _sha(head)})
    if value.get("type") != "file" or value.get("encoding") != "base64":
        raise ValueError("Candidate source must be an ordinary file")
    source = base64.b64decode(value["content"]).decode("utf-8")
    if validate_source(source) != source:
        raise ValueError("Candidate source is not canonical bounded arithmetic")
    return source


def _message(data):
    return (f"agent: {LOOP_ID} revision {data['revision']}\n\n"
            f"source-sha256: {data['source_sha256']}\n"
            f"run: {data['run_id']}/{data['run_attempt']}/{data['execution_stage']}\n"
            f"parent: {data['parent_sha']}\n")


def verify_commit(client, head, data):
    commit = client.request("GET", "/git/commits/" + _sha(head))
    if (commit.get("message") != _message(data)
            or [p.get("sha") for p in commit.get("parents", [])] != [data["parent_sha"]]):
        raise ValueError("Existing commit is not this exact candidate")
    comparison = client.request("GET", f"/compare/{data['parent_sha']}...{head}")
    if (comparison.get("total_commits") != 1 or len(comparison.get("files", [])) != 1
            or comparison["files"][0].get("filename") != SOURCE_PATH
            or comparison["files"][0].get("status") != "modified"):
        raise ValueError("Candidate must change only the permitted source file")
    source = source_at(client, head)
    if digest(source) != data["source_sha256"]:
        raise ValueError("Exact-head source differs from the model artifact")
    return source


def _pr_marker(data):
    return f"<!-- bounded-dsh-cycle:{LOOP_ID}:issue={data['issue']} -->"


def _get_pr(client, data):
    prs = client._list("/pulls", {"state": "all", "head": OWNER + ":" + branch_name(data["issue"])})
    if len(prs) > 1:
        raise ValueError("Multiple cycle PRs require reconciliation")
    if not prs:
        return None
    pr = prs[0]
    if (_pr_marker(data) not in (pr.get("body") or "") or pr.get("state") != "open"
            or pr.get("base", {}).get("ref") != "main" or pr.get("merged_at")
            or pr.get("head", {}).get("repo", {}).get("full_name") != REPOSITORY
            or pr.get("head", {}).get("ref") != branch_name(data["issue"])):
        raise ValueError("Existing PR is foreign, closed or merged")
    return pr


def _replay(client, data, pr, env, head):
    comments = client.list_comments(pr["number"])
    state = replay_journal(policy_for(data, pr["number"], env), comments, mode="operational", observed_head_sha=head)
    return state


def _ensure_status(client, head, data, tests):
    target = f"https://github.com/{REPOSITORY}/actions/runs/{data['run_id']}/attempts/{data['run_attempt']}"
    description = f"revision {data['revision']}: {tests['count']} assertions, {len(tests['failed_cases'])} failed"
    statuses = client._list(f"/commits/{head}/statuses")
    matched = [s for s in statuses if s.get("context") == STATUS_CONTEXT]
    if matched:
        value = matched[0]
        if (value.get("creator", {}).get("login") != PUBLISHER or value.get("state") != tests["status"]
                or value.get("target_url") != target or value.get("description") != description):
            raise ValueError("Candidate has conflicting CI status")
        return value
    return client.request("POST", f"/statuses/{head}", payload={"state": tests["status"],
                          "context": STATUS_CONTEXT, "description": description, "target_url": target}, expected=(201,))


def _candidate_record(data, pr, head, tests, previous_id, artifact_sha, artifact_id, status_id):
    return {"schema": SCHEMA, "kind": "candidate", "repository": REPOSITORY,
            "issue": data["issue"], "pr": pr, "base_sha": data["base_sha"],
            "criteria_sha256": data["issue_body_sha256"], "head_sha": head,
            "revision": data["revision"], "parent_sha": data["parent_sha"], "previous_event_id": previous_id,
            "loop_id": LOOP_ID, "fault_injection": data["fault_injection"],
            "model_source_sha256": data["model_source_sha256"],
            "execution": {"run_id": int(data["run_id"]), "run_attempt": int(data["run_attempt"]),
                          "stage_key": data["execution_stage"], "workflow_sha": data["workflow_sha"],
                          "session_id": data["session_id"], "source_sha256": data["source_sha256"],
                          "artifact_sha256": artifact_sha, "artifact_id": artifact_id,
                          "completed": True, "model_requests": 1,
                          "exit_code": data["harness_exit_code"], "model": data["model"], "version": data["version"]},
            "ci": {"run_id": int(data["run_id"]), "run_attempt": int(data["run_attempt"]),
                   "stage_key": data["ci_stage"], "workflow_sha": data["workflow_sha"],
                   "tested_head_sha": head, "status": "completed", "conclusion": tests["status"],
                   "context": STATUS_CONTEXT, "status_id": status_id, "tests": tests}}


def publish(client, data, env, *, artifact_sha256):
    validate_envelope(data, env)
    _sha(artifact_sha256, 64)
    check_current(client, data)
    print("Sanitized DSH receipt: " + json.dumps({key: data[key] for key in (
        "session_id", "model", "version", "model_request_count", "harness_status", "harness_exit_code",
        "model_source_sha256", "source_sha256", "fault_injection", "execution_stage")}, sort_keys=True))
    # Source safety is mandatory; a business failure is deliberately publishable.
    if independent_tests(data["model_source_code"])["status"] != "success":
        raise ValueError("Pre-injection model output must pass the disclosed demo tests")
    independent_tests(data["source_code"])
    branch = branch_name(data["issue"])
    head = optional_ref(client, branch)
    pr = _get_pr(client, data)
    previous_id = None
    existing_candidate = None
    if data["revision"] == 0:
        if head is None and pr is not None:
            raise ValueError("PR exists without its expected branch")
        if head is not None:
            verify_commit(client, head, data)
            if pr is not None:
                comments = client.list_comments(pr["number"])
                if any((c.get("body") or "").startswith(MARKER) for c in comments):
                    state = replay_journal(policy_for(data, pr["number"], env), comments, mode="operational", observed_head_sha=head)
                    existing_candidate = state.candidates[-1]
    else:
        if head is None or pr is None:
            raise ValueError("Rework requires the existing task PR and branch")
        if head == data["parent_sha"]:
            state = _replay(client, data, pr, env, head)
            if state.status != "REWORK" or state.revision != data["revision"] - 1:
                raise ValueError("No current eligible failed review authorizes this rework")
            previous_id = state.last_event_id
        else:
            verify_commit(client, head, data)
            comments = client.list_comments(pr["number"])
            # Publication may have updated the ref before its journal comment.
            try:
                state = replay_journal(policy_for(data, pr["number"], env), comments, mode="operational", observed_head_sha=head)
                existing_candidate = state.candidates[-1]
                previous_id = existing_candidate["previous_event_id"]
            except ValueError:
                state = replay_journal(policy_for(data, pr["number"], env), comments, mode="operational", observed_head_sha=data["parent_sha"])
                if state.status != "REWORK" or state.revision != data["revision"] - 1:
                    raise ValueError("Incomplete publication has no matching rework authorization")
                previous_id = state.last_event_id
    if head is None or head == data["parent_sha"]:
        parent = client.request("GET", "/git/commits/" + data["parent_sha"])
        blob = client.create_blob(data["source_code"], encoding="utf-8")
        tree = client.create_tree(parent["tree"]["sha"], [{"path": SOURCE_PATH, "mode": "100644", "type": "blob", "sha": blob["sha"]}])
        commit = client.create_commit(_message(data), tree["sha"], [data["parent_sha"]])
        candidate = _sha(commit["sha"])
        check_current(client, data)
        # The REST ref API has no expected-SHA parameter. Re-read plus a commit
        # with exactly the old parent and force=false rejects any diverged race.
        observed = optional_ref(client, branch)
        if observed != head:
            raise ValueError("Task branch moved before publication")
        if head is None:
            client.request("POST", "/git/refs", payload={"ref": "refs/heads/" + branch, "sha": candidate}, expected=(201,))
        else:
            if _replay(client, data, pr, env, head).last_event_id != previous_id:
                raise ValueError("Rework journal changed before publication")
            client.request("PATCH", "/git/refs/heads/" + branch, payload={"sha": candidate, "force": False})
        head = candidate
    source = verify_commit(client, head, data)
    # Fixed AST-validated arithmetic only: print the independent actual assertion
    # output so the completed publisher job supplies inspectable CI evidence.
    print(f"Exact tested head: {head}; stage: {data['ci_stage']}")
    tests = independent_tests(source, emit_log=True)  # downloaded by exact Git SHA
    check_current(client, data)
    if optional_ref(client, branch) != head:
        raise ValueError("Candidate became stale before PR/CI publication")
    if pr is None:
        body = (_pr_marker(data) + "\n\n" + f"Closes #{data['issue']}. This is a disclosed bounded fault-injection experiment. "
                "Official DeepSeek outputs are preserved by digest; revisions 0 and 1 inject known arithmetic faults, "
                "revision 2 applies no injection. A real ChatGPT web task must review each candidate journal below. "
                "No automatic merge. The head-bound dsh-cycle/acceptance-harness status is the experiment's independent CI; "
                "the separate pull_request workflow may remain approval-required. Web automation provenance is not inferred from comments.\n")
        pr = client.create_pr(f"{LOOP_ID}: demonstrate two automatic reworks", body, branch, "main", draft=True)
    fresh = client.get_pr(pr["number"])
    if fresh.get("head", {}).get("sha") != head or fresh.get("state") != "open":
        raise ValueError("PR head changed before CI publication")
    status = _ensure_status(client, head, data, tests)
    record = _candidate_record(data, pr["number"], head, tests, previous_id, artifact_sha256,
                               _id(env["CYCLE_ARTIFACT_ID"], "artifact ID"), _id(status.get("id"), "status ID"))
    if existing_candidate is not None:
        comparable = {k: v for k, v in existing_candidate.items() if k != "event_id"}
        if comparable != record:
            raise ValueError("Existing journal candidate differs from this artifact")
        journal_id = existing_candidate["event_id"]
    else:
        check_current(client, data)
        if client.get_pr(pr["number"]).get("head", {}).get("sha") != head:
            raise ValueError("PR head moved before the candidate journal receipt")
        # A final complete snapshot guards against duplicate comments or reviews
        # appearing while status was written; unknown POST outcomes never replay.
        comments = client.list_comments(pr["number"])
        matches = [c for c in comments if c.get("body") == encode_record(record)]
        if matches:
            journal_id = matches[0]["id"]
        else:
            if data["revision"] > 0:
                state = replay_journal(policy_for(data, pr["number"], env), comments, mode="operational", observed_head_sha=data["parent_sha"])
                if state.last_event_id != previous_id or state.status != "REWORK":
                    raise ValueError("Journal changed before candidate receipt")
            elif any((c.get("body") or "").startswith(MARKER) for c in comments):
                raise ValueError("Unexpected initial journal events")
            journal_id = client.comment(pr["number"], encode_record(record))["id"]
    state = _replay(client, data, pr, env, head)
    return {"repository": REPOSITORY, "issue": data["issue"], "pr": pr["number"], "url": pr["html_url"],
            "loop_id": LOOP_ID, "revision": data["revision"], "head_sha": head, "parent_sha": data["parent_sha"], "journal_id": journal_id,
            "ci_status_id": status["id"], "tests": tests, "journal": state.report(),
            "chatgpt_origin_verified": False, "merged": False}


def inspect_review(client, data, env, publication):
    validate_envelope(data, env, read_only=True)
    check_current(client, data)
    pr = _get_pr(client, data)
    if not pr or pr["number"] != publication["pr"] or pr["head"]["sha"] != publication["head_sha"]:
        raise ValueError("Review target is stale")
    state = _replay(client, data, pr, env, publication["head_sha"])
    candidate = state.candidates[-1]
    if (candidate["revision"] != data["revision"] or candidate["event_id"] != publication["journal_id"]
            or candidate["ci"]["run_id"] != int(data["run_id"])
            or candidate["ci"]["run_attempt"] != int(data["run_attempt"])
            or candidate["ci"]["stage_key"] != data["ci_stage"]):
        raise ValueError("Review journal is not the current stage")
    statuses = client._list(f"/commits/{publication['head_sha']}/statuses")
    matching = [s for s in statuses if s.get("context") == STATUS_CONTEXT]
    if (not matching or matching[0].get("id") != publication["ci_status_id"]
            or matching[0].get("creator", {}).get("login") != PUBLISHER
            or matching[0].get("state") != candidate["ci"]["conclusion"]):
        raise ValueError("Latest exact-head CI status differs from the candidate journal")
    if state.status == "REVIEW":
        return None
    review = state.reviews[-1]
    return {"status": state.status, "needs_rework": state.status == "REWORK", "head_sha": state.head_sha,
            "revision": state.revision, "review_id": review["event_id"],
            "rework_instruction": review.get("rework_instruction", ""), "review": review,
            "journal": state.report(), "chatgpt_origin_verified": False}


def wait_review(client, data, env, publication, *, timeout_seconds=1200, poll_seconds=15,
                clock=time.monotonic, sleep=time.sleep):
    if not 1 <= timeout_seconds <= 1200 or not 1 <= poll_seconds <= 15:
        raise ValueError("Review waiting is bounded to twenty minutes")
    deadline = clock() + timeout_seconds
    while True:
        if clock() >= deadline:
            raise TimeoutError("No eligible actual PR review arrived before the deadline")
        result = inspect_review(client, data, env, publication)
        if clock() > deadline:
            raise TimeoutError("Review evidence arrived after the deadline")
        if result is not None:
            return result
        left = deadline - clock()
        if left <= 0:
            raise TimeoutError("No eligible actual PR review arrived before the deadline")
        sleep(min(poll_seconds, left))


def _json_file(path):
    if path.stat().st_size > 65536:
        raise ValueError("Input evidence is oversized")
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8-sig")), hashlib.sha256(raw).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["publish", "wait-review"])
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--publication", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args(argv)
    data, sha = _json_file(args.artifact)
    validate_envelope(data, os.environ, read_only=args.command == "wait-review")
    client = GitHubClient(REPOSITORY, network_enabled=True, token_env="GH_CYCLE_TOKEN",
                          authorization_gate=lambda operation: args.command == "publish" or operation == "github-read",
                          max_attempts=2, max_pages=5, max_items=500)
    if args.command == "publish":
        result = publish(client, data, os.environ, artifact_sha256=sha)
    else:
        if args.publication is None:
            raise ValueError("Waiting requires the exact publisher receipt")
        publication, _ = _json_file(args.publication)
        result = wait_review(client, data, os.environ, publication, timeout_seconds=args.timeout)
    args.output.write_text(json.dumps(result, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8", newline="\n") as stream:
            for key in ("pr", "head_sha", "parent_sha", "journal_id", "needs_rework", "review_id", "status"):
                if key in result:
                    value = str(result[key]).lower() if type(result[key]) is bool else str(result[key])
                    if any(c in value for c in "\r\n"):
                        raise ValueError("Unsafe output")
                    stream.write(key + "=" + value + "\n")
            if args.command == "publish":
                stream.write("publication=" + json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n")
    print(json.dumps({k: result[k] for k in ("pr", "head_sha", "status", "needs_rework", "chatgpt_origin_verified") if k in result}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UncertainWrite:
        print("BLOCKED: unknown GitHub write outcome; reconcile the same artifact, never re-run generation.", file=sys.stderr)
        raise SystemExit(2)
    except (ValueError, KeyError, TypeError, OSError, GitHubError, subprocess.SubprocessError):
        print("BLOCKED: cycle evidence/context failed validation; no raw provider or credential data published.", file=sys.stderr)
        raise SystemExit(3)
