"""Publish reviewed code artifacts via an injected GitHub client, never run code.

Each task adds two commits to its predecessor HEAD. PRs target the pinned main
branch and therefore accumulate earlier work; merging remains a user decision.
A write-ahead journal blocks unknown writes instead of replaying them. Keep the
journal for the run; losing it is not a supported cross-machine recovery method.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
import re

from .github import GitHubClient, UncertainWrite
from .coding_contract import (
    PUBLIC_BINDING, REPORT_MARKER, WORKFLOW_PATH, canonical_digest, check_arm,
    digest, parse_record, report_path, strict_json, task_snapshot, validate_artifact,
)
from .portable_publisher import _commit, _get_ref, _journal_lock, _save_journal, _sha
from .project_profile import repository_namespace

_ARTIFACT_FIELDS = frozenset(PUBLIC_BINDING) | {
    "schema", "issue", "issue_body_sha256", "task", "changes",
    "raw_model_output_sha256", "validated_change_set_sha256", "checks", "check_status",
    "session_id", "model", "version", "model_requests", "exit_code", "guard", "usage",
    "run_id", "run_attempt",
}
_MAX_BLOB_BYTES = 4 * 1024 * 1024


def _branch(profile, activation, number):
    if type(number) is not int or not 1 <= number <= 2**31 - 1:
        raise ValueError("Invalid code task number")
    nonce = activation["nonce"].replace("-", "")[:12]
    branch = f"agent/code-{repository_namespace(profile['repository'])}-{nonce}-{number}"
    if not re.fullmatch(r"agent/code-[0-9a-f]{12}-[0-9a-f]{12}-[1-9][0-9]*", branch):
        raise ValueError("Invalid deterministic code branch")
    return branch


def _artifact_env(profile, activation, artifact):
    return {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": profile["repository"],
        "GITHUB_EVENT_NAME": "issues", "GITHUB_ACTOR": profile["trusted_actor"],
        "GITHUB_WORKFLOW_SHA": activation["workflow_sha"],
        "GITHUB_WORKFLOW_REF": f"{profile['repository']}/{WORKFLOW_PATH}@refs/heads/{profile['default_branch']}",
        "GITHUB_RUN_ID": artifact.get("run_id"), "GITHUB_RUN_ATTEMPT": "1",
        "HARNESS_ISSUE": str(artifact.get("issue")), "RUNNER_OS": "Windows"}


def _validate(profile, activation, artifact, env):
    if type(artifact) is not dict or set(artifact) != _ARTIFACT_FIELDS:
        raise ValueError("Code artifact contains missing or unexpected fields")
    return validate_artifact(profile, activation, artifact, env)


def _report(artifact, implementation, head):
    metadata = {key: copy.deepcopy(value) for key, value in artifact.items()
                if key not in {"schema", "issue", "changes"}}
    return {**metadata, "schema": "harness-code-report/v1", "source_issue": artifact["issue"],
        "changes": [{key: value for key, value in change.items() if key != "content"}
                    for change in artifact["changes"]],
        "task_number": artifact["task"]["number"], "task_id": artifact["task"]["task_id"],
        "base_sha": artifact["task"]["base_sha"], "implementation_sha": _sha(implementation),
        "head_sha": _sha(head), "artifact_sha256": canonical_digest(artifact),
        "report_path": report_path(artifact, artifact["task"]["number"]),
        "visual_acceptance": "NOT_RUN", "merge_policy": "manual-cumulative"}


def _body(report):
    body = REPORT_MARKER + json.dumps(report, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False) + "\n"
    if len(body.encode("utf-8")) > 32768:
        raise ValueError("Code report exceeds the bounded machine-record size")
    return body


def _execution_report(report):
    metadata = {key: value for key, value in report.items() if key != "head_sha"}
    metadata["schema"] = "harness-code-execution-report/v1"
    return (f"# {report['task_id']}\n\n"
        f"Controller check status: {report['check_status']}. UE visual acceptance: NOT_RUN.\n"
        "This PR targets main and accumulates earlier task commits. The user chooses whether and which PR to merge.\n"
        "This round is the incremental base_sha -> implementation_sha code change, followed by this report.\n"
        "Final HEAD is recorded in the PR body to avoid a self-referential commit hash.\n"
        "The raw model-output hash is a controller declaration; its source log remains local.\n"
        "The validated change-set digest binds the published final file contents and metadata.\n\n"
        "```json\n" + json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n```\n")


def _blob(client, sha):
    sha = _sha(sha)
    data = client.request("GET", "/git/blobs/" + sha)
    if (not isinstance(data, dict) or data.get("sha") != sha or data.get("encoding") != "base64"
            or not isinstance(data.get("content"), str) or len(data["content"]) > 6 * 1024 * 1024):
        raise ValueError("Expected a bounded Git blob")
    try:
        raw = base64.b64decode("".join(data["content"].split()), validate=True)
        text = raw.decode("utf-8")
    except (ValueError, UnicodeError) as error:
        raise ValueError("Git blob must be valid UTF-8/base64") from error
    actual = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
    if len(raw) > _MAX_BLOB_BYTES or type(data.get("size")) is not int or data["size"] != len(raw) or actual != sha:
        raise ValueError("Git blob size/hash mismatch")
    return text


def _tree(client, commit):
    data = client.request("GET", "/git/trees/" + commit["tree"]["sha"], params={"recursive": 1})
    if (data.get("truncated") is not False or type(data.get("tree")) is not list
            or len(data["tree"]) > 100000):
        raise ValueError("A complete bounded Git tree is required")
    result, seen = {}, set()
    for item in data["tree"]:
        path = item.get("path")
        if not isinstance(path, str) or not path or path in seen:
            raise ValueError("Tree contains duplicate or invalid paths")
        seen.add(path)
        if item.get("type") != "tree":
            result[path] = {key: item.get(key) for key in ("sha", "mode", "type")}
            _sha(result[path]["sha"])
    return result


def _base_entries(client, artifact, base_tree):
    modes = {}
    for change in artifact["changes"]:
        path = change["path"]
        existing = base_tree.get(path)
        for name in base_tree:
            if ((name.casefold() == path.casefold() and name != path)
                    or path.casefold().startswith(name.casefold() + "/")
                    or name.casefold().startswith(path.casefold() + "/")):
                raise ValueError("Code path collides with a base-tree entry")
        if change["operation"] == "add":
            if existing is not None or change["before_sha256"] is not None:
                raise ValueError("Added code file already exists in the exact base")
            modes[path] = "100644"
        else:
            if (existing is None or existing["type"] != "blob"
                    or existing["mode"] not in {"100644", "100755"}):
                raise ValueError("Modified code must be an existing ordinary text blob")
            original = _blob(client, existing["sha"])
            if digest(original) != change["before_sha256"]:
                raise ValueError("Code artifact before hash differs from exact base blob")
            if original == change["content"]:
                raise ValueError("A modified code file must change its content")
            modes[path] = existing["mode"]
    if report_path(artifact, artifact["task"]["number"]) in base_tree:
        raise ValueError("This task's execution report already exists in the base")
    return modes


def _compare(client, base, head, expected, commits):
    data = client.request("GET", f"/compare/{_sha(base)}...{_sha(head)}")
    files = data.get("files")
    if (data.get("status") != "ahead" or type(data.get("total_commits")) is not int
            or data["total_commits"] != commits or not isinstance(files, list)
            or len(files) != len(expected) or {item.get("filename") for item in files} != set(expected)):
        raise ValueError("Incremental commit/file scope differs from the exact artifact")
    for item in files:
        entry = expected[item["filename"]]
        if (item.get("status") != entry["status"] or item.get("sha") != entry["sha"]
                or item.get("previous_filename")):
            raise ValueError("Incremental file status/blob differs from the artifact")


def _verify_branch(client, profile, activation, artifact, head):
    task = artifact["task"]
    base = _commit(client, task["base_sha"])
    final = _commit(client, head)
    parents = final.get("parents", [])
    if final.get("message") != task["task_id"] or len(parents) != 1:
        raise ValueError("Final commit must have the task's exact title and one parent")
    implementation = _sha(parents[0].get("sha"))
    implemented = _commit(client, implementation)
    if (implemented.get("message") != task["task_id"]
            or [item.get("sha") for item in implemented.get("parents", [])] != [task["base_sha"]]):
        raise ValueError("Implementation must directly follow the exact task base")
    base_tree, code_tree, final_tree = (_tree(client, item) for item in (base, implemented, final))
    modes = _base_entries(client, artifact, base_tree)
    expected_code = copy.deepcopy(base_tree)
    changes = {}
    for change in artifact["changes"]:
        path = change["path"]
        entry = code_tree.get(path)
        if entry is None or entry["type"] != "blob" or entry["mode"] != modes[path]:
            raise ValueError("Code file type/mode changed unexpectedly")
        content = _blob(client, entry["sha"])
        if content != change["content"] or digest(content) != change["content_sha256"]:
            raise ValueError("Published code differs from the validated artifact")
        expected_code[path] = entry
        changes[path] = {"sha": entry["sha"], "status": "added" if change["operation"] == "add" else "modified"}
    if code_tree != expected_code:
        raise ValueError("Implementation tree includes unrelated changes or modes")
    report = _report(artifact, implementation, head)
    path = report["report_path"]
    entry = final_tree.get(path)
    if entry is None or entry["type"] != "blob" or entry["mode"] != "100644":
        raise ValueError("Execution report must be a new ordinary text blob")
    if _blob(client, entry["sha"]) != _execution_report(report):
        raise ValueError("Execution report is not the canonical controller report")
    if final_tree != {**code_tree, path: entry}:
        raise ValueError("Final commit must only add its execution report")
    report_change = {path: {"sha": entry["sha"], "status": "added"}}
    _compare(client, task["base_sha"], implementation, changes, 1)
    _compare(client, implementation, head, report_change, 1)
    _compare(client, task["base_sha"], head, changes | report_change, 2)
    return report


def _verify_run(client, profile, activation, artifact, *, completed):
    run_id = artifact["run_id"]
    if not isinstance(run_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", run_id):
        raise ValueError("Invalid workflow run identity")
    run = client.request("GET", "/actions/runs/" + run_id)
    if (run.get("id") != int(run_id) or type(run.get("run_attempt")) is not int or run["run_attempt"] != 1
            or run.get("head_sha") != activation["workflow_sha"] or run.get("event") != "issues"
            or run.get("repository", {}).get("full_name") != profile["repository"]
            or run.get("head_repository", {}).get("full_name") != profile["repository"]
            or run.get("actor", {}).get("login") != profile["trusted_actor"]
            or run.get("triggering_actor", {}).get("login") != profile["trusted_actor"]
            or run.get("path") not in {WORKFLOW_PATH, WORKFLOW_PATH + "@refs/heads/" + profile["default_branch"]}):
        raise ValueError("Code workflow provenance differs from the approved deployment")
    if completed:
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            raise ValueError("Code workflow has not completed successfully")
        data = client.request("GET", "/actions/runs/" + run_id + "/jobs", params={"per_page": 100})
        jobs = data.get("jobs")
        if (not isinstance(jobs, list) or type(data.get("total_count")) is not int
                or data["total_count"] != len(jobs) or len(jobs) > 100):
            raise ValueError("A complete workflow jobs result is required")
        for name in ("generate", "publish"):
            matches = [job for job in jobs if job.get("name") == name]
            if (len(matches) != 1 or matches[0].get("status") != "completed"
                    or matches[0].get("conclusion") != "success"):
                raise ValueError("Generate and publish jobs must both finish successfully")
    elif run.get("status") not in {"in_progress", "completed"} or run.get("conclusion") not in {None, "success"}:
        raise ValueError("Workflow is no longer eligible to publish")


def _pr_identity(pr, profile, activation, task, head):
    if (type(pr.get("number")) is not int or pr["number"] <= 0
            or pr.get("state") != "open" or pr.get("merged") is not False or pr.get("merged_at") is not None
            or pr.get("user", {}).get("login") != "github-actions[bot]" or pr.get("title") != task["task_id"]
            or pr.get("head", {}).get("sha") != head
            or pr.get("head", {}).get("ref") != _branch(profile, activation, task["number"])
            or pr.get("head", {}).get("repo", {}).get("full_name") != profile["repository"]
            or pr.get("base", {}).get("ref") != profile["default_branch"]
            or pr.get("base", {}).get("sha") != activation["workflow_sha"]
            or pr.get("base", {}).get("repo", {}).get("full_name") != profile["repository"]):
        raise ValueError("Code PR was closed, merged, moved or rebound")


def _guard(client, profile, activation, artifact, *, verify_previous):
    check_arm(client, profile, activation)
    _, task, _ = task_snapshot(client, profile, activation, artifact["issue"],
        expected_body_sha=artifact["issue_body_sha256"], verify_previous=verify_previous)
    if task != artifact["task"]:
        raise ValueError("Current task differs from the generated code artifact")
    return task


def verify_report_pr(client, profile, activation, pr, *, expected_number, expected_head):
    """Verify this report only, never recursively traverse the growing chain.

    The task consumer verifies its immediate predecessor through this API. This
    report's own source Issue is checked with verify_previous=False.
    """
    check_arm(client, profile, activation)
    _branch(profile, activation, expected_number)
    number = pr if type(pr) is int else pr.get("number") if isinstance(pr, dict) else None
    GitHubClient._number(number)
    head = _sha(expected_head)
    current = client.get_pr(number)
    report = parse_record(current.get("body", ""), REPORT_MARKER)
    if (report.get("schema") != "harness-code-report/v1" or report.get("task_number") != expected_number
            or not isinstance(report.get("task"), dict) or report["task"].get("number") != expected_number):
        raise ValueError("Unexpected code report protocol/task slot")
    _pr_identity(current, profile, activation, report["task"], head)
    tree = _tree(client, _commit(client, head))
    changes = report.get("changes")
    if type(changes) is not list or not 1 <= len(changes) <= 16:
        raise ValueError("Report must identify bounded code changes")
    restored = []
    for change in changes:
        if type(change) is not dict or set(change) != {"path", "operation", "before_sha256", "content_sha256"}:
            raise ValueError("Unexpected code change metadata")
        path = change["path"]
        if not isinstance(path, str) or path not in tree or tree[path]["type"] != "blob":
            raise ValueError("Reported code file is absent from the final tree")
        restored.append({**change, "content": _blob(client, tree[path]["sha"])})
    artifact = {key: copy.deepcopy(report[key]) for key in _ARTIFACT_FIELDS - {"schema", "issue", "changes"}
                if key in report}
    artifact.update(schema="harness-code-artifact/v1", issue=report.get("source_issue"), changes=restored)
    artifact = _validate(profile, activation, artifact, _artifact_env(profile, activation, artifact))
    _guard(client, profile, activation, artifact, verify_previous=False)
    expected = _verify_branch(client, profile, activation, artifact, head)
    if report != expected or current.get("body") != _body(expected):
        raise ValueError("PR report differs from exact artifact, commits or canonical body")
    _verify_run(client, profile, activation, artifact, completed=True)
    fresh = client.get_pr(number)
    _pr_identity(fresh, profile, activation, artifact["task"], head)
    if fresh.get("body") != current.get("body"):
        raise ValueError("Code report changed during verification")
    _guard(client, profile, activation, artifact, verify_previous=False)
    return expected


def publish_task(client, profile, activation, artifact, env, *, journal_root: Path):
    """Publish a PASS or explicit FAIL artifact; no merging or project execution."""
    artifact = _validate(profile, activation, artifact, env)
    task = _guard(client, profile, activation, artifact, verify_previous=True)
    _verify_run(client, profile, activation, artifact, completed=False)
    _body(_report(artifact, "0" * 40, "0" * 40))  # Size check before any write.
    branch = _branch(profile, activation, task["number"])
    base = _commit(client, task["base_sha"])
    modes = _base_entries(client, artifact, _tree(client, base))
    root = Path(journal_root).resolve()
    folder = root / repository_namespace(profile["repository"]) / activation["nonce"] / str(task["number"])
    if not folder.resolve().is_relative_to(root):
        raise ValueError("Publisher journal escaped its configured root")
    with _journal_lock(folder):
        path = folder / "code-publisher.json"
        if path.is_symlink():
            raise ValueError("Code publisher journal cannot be a symlink")
        identity = canonical_digest(artifact)
        if path.exists():
            if path.stat().st_size > 131072:
                raise ValueError("Oversized code publisher journal")
            journal = strict_json(path.read_text(encoding="utf-8"))
            if (not isinstance(journal, dict) or journal.get("artifact_sha256") != identity
                    or journal.get("branch") != branch or not isinstance(journal.get("steps"), dict)):
                raise ValueError("Code journal is bound to another artifact")
        else:
            journal = {"schema": "harness-code-publisher-journal/v1", "artifact_sha256": identity,
                       "branch": branch, "steps": {}}
            _save_journal(path, journal)

        def guard():
            _guard(client, profile, activation, artifact, verify_previous=True)
            _verify_run(client, profile, activation, artifact, completed=False)

        def write(name, operation):
            previous = journal["steps"].get(name)
            if previous:
                if previous.get("status") == "complete":
                    return previous["result"]
                raise UncertainWrite("Unknown publication write is blocked, never replayed")
            guard()
            journal["steps"][name] = {"status": "pending"}
            _save_journal(path, journal)
            response = operation()
            if not isinstance(response, dict):
                raise UncertainWrite("Code write returned no verifiable identity")
            if name == "pr":
                result = {"number": GitHubClient._number(response.get("number"))}
            elif name == "ref":
                result = {"object": {"sha": _sha(response.get("object", {}).get("sha"))}}
            else:
                result = {"sha": _sha(response.get("sha"))}
            journal["steps"][name] = {"status": "complete", "result": result}
            _save_journal(path, journal)
            return result

        reference = _get_ref(client, branch)
        if reference is None:
            if journal["steps"].get("ref", {}).get("status") == "pending":
                raise UncertainWrite("Unknown branch creation is not visible; stop for review")
            entries = []
            for index, change in enumerate(artifact["changes"]):
                blob = write(f"code_blob_{index}", lambda change=change: client.create_blob(change["content"], encoding="utf-8"))
                entries.append({"path": change["path"], "mode": modes[change["path"]], "type": "blob", "sha": _sha(blob["sha"])})
            tree = write("implementation_tree", lambda: client.create_tree(base["tree"]["sha"], entries))
            implemented = write("implementation_commit", lambda: client.create_commit(task["task_id"], _sha(tree["sha"]), [task["base_sha"]]))
            implementation = _sha(implemented["sha"])
            text = _execution_report(_report(artifact, implementation, "0" * 40))
            blob = write("report_blob", lambda: client.create_blob(text, encoding="utf-8"))
            final_tree = write("report_tree", lambda: client.create_tree(_sha(tree["sha"]), [
                {"path": report_path(activation, task["number"]), "mode": "100644", "type": "blob", "sha": _sha(blob["sha"])}]))
            final = write("report_commit", lambda: client.create_commit(task["task_id"], _sha(final_tree["sha"]), [implementation]))
            head = _sha(final["sha"])
            _verify_branch(client, profile, activation, artifact, head)
            write("ref", lambda: client.request("POST", "/git/refs", payload={"ref": "refs/heads/" + branch, "sha": head}, expected=(201,)))
            reference = _get_ref(client, branch)
            if reference is None or reference.get("object", {}).get("sha") != head:
                raise UncertainWrite("Code branch could not be read back at its exact SHA")
        else:
            head = _sha(reference.get("object", {}).get("sha"))
        report = _verify_branch(client, profile, activation, artifact, head)
        owner = profile["repository"].split("/")[0]
        prs = client._list("/pulls", {"state": "all", "head": owner + ":" + branch})
        if len(prs) > 1:
            raise ValueError("Multiple code PRs require manual reconciliation")
        if prs:
            pr_number = GitHubClient._number(prs[0].get("number"))
        else:
            pr_number = GitHubClient._number(write("pr", lambda: client.create_pr(task["task_id"], _body(report), branch,
                profile["default_branch"], draft=False))["number"])
        current = client.get_pr(pr_number)
        _pr_identity(current, profile, activation, task, head)
        if current.get("body") != _body(report):
            raise ValueError("Current code PR body differs from the verified report")
        guard()
        journal.update(status="published_verified", verified_pr={"number": pr_number, "head_sha": head})
        _save_journal(path, journal)
        return {**report, "pr": pr_number, "url": f"https://github.com/{profile['repository']}/pull/{pr_number}"}
