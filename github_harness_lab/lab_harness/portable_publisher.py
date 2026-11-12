"""Publish bounded documents through an injected client; never execute project code.

The publisher owns two commits on one deterministic task branch. Its journal is
write-ahead: an unknown write is reconciled from an exact branch/PR or blocked,
never repeated. The supplied journal directory must survive publisher restarts.
This is not cross-VM recovery: deleting/loss of the journal is not a supported
resume procedure. Actions reruns (run_attempt > 1) remain rejected even if a
fresh machine has no journal. An unknown result with lost state needs review.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .github import GitHubClient, GitHubError, UncertainWrite
from .portable_policy import (
    PUBLIC_BINDING, REPORT_MARKER, WORKFLOW_PATH, canonical_digest, check_arm,
    parse_record, strict_json, task_snapshot, validate_artifact,
)
from .project_profile import repository_namespace, task_spec

_ARTIFACT_FIELDS = frozenset(PUBLIC_BINDING) | {
    "schema", "issue", "task_number", "task_id", "issue_body_sha256",
    "document_path", "report_path", "document", "document_sha256", "session_id",
    "model", "version", "model_requests", "exit_code", "guard", "usage",
    "run_id", "run_attempt",
}


def _sha(value):
    return GitHubClient._sha(value)


def _branch(profile, activation, number):
    task_spec(profile, number)
    nonce = activation["nonce"].replace("-", "")[:12]
    branch = f"agent/harness-{repository_namespace(profile['repository'])}-{nonce}-{number}"
    if not re.fullmatch(r"agent/harness-[0-9a-f]{12}-[0-9a-f]{12}-[12]", branch):
        raise ValueError("Invalid deterministic publication branch")
    return branch


def _artifact_env(profile, activation, artifact):
    return {
        "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": profile["repository"],
        "GITHUB_EVENT_NAME": "issues", "GITHUB_ACTOR": profile["trusted_actor"],
        "GITHUB_WORKFLOW_SHA": activation["workflow_sha"],
        "GITHUB_WORKFLOW_REF": profile["repository"] + "/" + WORKFLOW_PATH + "@refs/heads/" + profile["default_branch"],
        "GITHUB_RUN_ID": artifact.get("run_id"), "GITHUB_RUN_ATTEMPT": "1",
        "HARNESS_ISSUE": str(artifact.get("issue")),
    }


def _validate(profile, activation, artifact, env):
    if not isinstance(artifact, dict) or set(artifact) != _ARTIFACT_FIELDS:
        raise ValueError("Publication artifact has missing or unexpected fields")
    return validate_artifact(profile, activation, artifact, env)


def _report(artifact, implementation, head):
    report = {key: value for key, value in artifact.items() if key not in {"schema", "document", "issue"}}
    return {
        **report, "schema": "harness-report/v1", "source_issue": artifact["issue"],
        "artifact_sha256": canonical_digest(artifact), "base_sha": artifact["workflow_sha"],
        "implementation_sha": _sha(implementation), "head_sha": _sha(head), "checks": "PASS",
    }


def _execution_report(report):
    metadata = {key: value for key, value in report.items() if key != "head_sha"}
    metadata["schema"] = "harness-execution-report/v1"
    return (f"# {report['task_id']}\n\n"
            "Trusted controller execution report. The model produced only the separate document.\n"
            "This checks bounded document delivery; UE build, game tests and UI acceptance are NOT_RUN.\n"
            "Final HEAD is recorded in the PR report, not in this commit.\n\n"
            + "```json\n" + json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            + "\n```\n")


def _body(report):
    return REPORT_MARKER + json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def _get_ref(client, branch):
    try:
        return client.get_ref(branch)
    except GitHubError as error:
        if error.status == 404:
            return None
        raise


def _blob(client, sha):
    sha = _sha(sha)
    data = client.request("GET", "/git/blobs/" + sha)
    if (not isinstance(data, dict) or data.get("sha") != sha or data.get("encoding") != "base64"
            or not isinstance(data.get("content"), str) or len(data["content"]) > 200000):
        raise ValueError("Expected an ordinary bounded Git blob")
    try:
        raw = base64.b64decode("".join(data["content"].split()), validate=True)
        text = raw.decode("utf-8")
    except (ValueError, UnicodeError) as error:
        raise ValueError("Git blob is not valid UTF-8/base64") from error
    actual = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
    if len(raw) > 100000 or data.get("size") != len(raw) or actual != sha:
        raise ValueError("Git blob size/hash mismatch")
    return text


def _commit(client, sha):
    data = client.request("GET", "/git/commits/" + _sha(sha))
    if not isinstance(data, dict) or data.get("sha") != sha:
        raise ValueError("Commit lookup did not return the exact SHA")
    _sha(data.get("tree", {}).get("sha"))
    return data


def _files(client, base, head, expected):
    comparison = client.request("GET", "/compare/" + _sha(base) + "..." + _sha(head))
    files = comparison.get("files", [])
    if (comparison.get("status") != "ahead" or comparison.get("total_commits") != 2
            or not isinstance(files, list) or len(files) != 2
            or {item.get("filename") for item in files} != set(expected)
            or any(item.get("status") != "added" or item.get("previous_filename") for item in files)):
        raise ValueError("Final branch must contain exactly two added files and two commits")
    return {item["filename"]: _sha(item.get("sha")) for item in files}


def _verify_branch(client, profile, activation, artifact, head):
    spec = task_spec(profile, artifact["task_number"])
    final = _commit(client, head)
    parents = final.get("parents", [])
    if final.get("message") != spec["task_id"] or len(parents) != 1:
        raise ValueError("Final task commit does not have the exact subject/parent")
    implementation = _sha(parents[0].get("sha"))
    implemented = _commit(client, implementation)
    if (implemented.get("message") != spec["task_id"]
            or [item.get("sha") for item in implemented.get("parents", [])] != [activation["workflow_sha"]]):
        raise ValueError("Each task must start independently at the approved deployment SHA")
    files = _files(client, activation["workflow_sha"], head, [spec["document_path"], spec["report_path"]])
    tree = client.request("GET", "/git/trees/" + final["tree"]["sha"], params={"recursive": 1})
    if tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
        raise ValueError("Complete final tree is required")
    for path, sha in files.items():
        entries = [entry for entry in tree["tree"] if entry.get("path") == path]
        if (len(entries) != 1 or entries[0].get("type") != "blob" or entries[0].get("mode") != "100644"
                or entries[0].get("sha") != sha):
            raise ValueError("Document/report must be ordinary nonexecutable files")
    report = _report(artifact, implementation, head)
    if _blob(client, files[spec["document_path"]]) != artifact["document"]:
        raise ValueError("Published document differs from the model artifact")
    if _blob(client, files[spec["report_path"]]) != _execution_report(report):
        raise ValueError("Execution report is not the canonical trusted report")
    # The implementation commit contains the model document only; the report
    # must first appear in the final commit so ImplementationSHA is meaningful.
    first_diff = client.request("GET", "/compare/" + activation["workflow_sha"] + "..." + implementation)
    first_files = first_diff.get("files", [])
    if (first_diff.get("status") != "ahead" or first_diff.get("total_commits") != 1
            or len(first_files) != 1 or first_files[0].get("filename") != spec["document_path"]
            or first_files[0].get("status") != "added"
            or first_files[0].get("sha") != files[spec["document_path"]]):
        raise ValueError("Implementation commit must add only the model document")
    report_diff = client.request("GET", "/compare/" + implementation + "..." + head)
    report_files = report_diff.get("files", [])
    if (report_diff.get("status") != "ahead" or report_diff.get("total_commits") != 1
            or len(report_files) != 1 or report_files[0].get("filename") != spec["report_path"]
            or report_files[0].get("status") != "added"
            or report_files[0].get("sha") != files[spec["report_path"]]):
        raise ValueError("Final commit must only add the trusted execution report")
    return report


def _verify_run(client, profile, activation, artifact, *, completed):
    run_id = artifact["run_id"]
    if not re.fullmatch(r"[1-9][0-9]{0,19}", run_id):
        raise ValueError("Invalid workflow run identity")
    run = client.request("GET", "/actions/runs/" + run_id)
    expected_path = WORKFLOW_PATH + "@refs/heads/" + profile["default_branch"]
    if (run.get("id") != int(run_id) or type(run.get("run_attempt")) is not int or run["run_attempt"] != 1
            or run.get("head_sha") != activation["workflow_sha"] or run.get("event") != "issues"
            or run.get("repository", {}).get("full_name") != profile["repository"]
            or run.get("head_repository", {}).get("full_name") != profile["repository"]
            or run.get("actor", {}).get("login") != profile["trusted_actor"]
            or run.get("triggering_actor", {}).get("login") != profile["trusted_actor"]
            or run.get("path") not in {WORKFLOW_PATH, expected_path}):
        raise ValueError("Workflow provenance differs from the approved repository/event/revision")
    if completed:
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            raise ValueError("Workflow is not completed successfully")
        data = client.request("GET", "/actions/runs/" + run_id + "/jobs", params={"per_page": 100})
        jobs = data.get("jobs")
        if (not isinstance(jobs, list) or type(data.get("total_count")) is not int
                or data["total_count"] != len(jobs) or len(jobs) > 100):
            raise ValueError("Complete workflow jobs are required; partial pages are refused")
        for name in ("generate", "publish"):
            matches = [job for job in jobs if job.get("name") == name]
            if (len(matches) != 1 or matches[0].get("status") != "completed"
                    or matches[0].get("conclusion") != "success"):
                raise ValueError("Generation and publication must both have completed successfully")
    elif run.get("status") not in {"in_progress", "completed"} or run.get("conclusion") not in {None, "success"}:
        raise ValueError("Workflow is no longer eligible to publish")


def _pr_identity(pr, profile, activation, number, head):
    spec = task_spec(profile, number)
    if (type(pr.get("number")) is not int or pr["number"] <= 0
            or pr.get("state") != "open" or pr.get("merged") is not False or pr.get("merged_at") is not None
            or pr.get("user", {}).get("login") != "github-actions[bot]"
            or pr.get("title") != spec["task_id"]
            or pr.get("head", {}).get("sha") != head
            or pr.get("head", {}).get("ref") != _branch(profile, activation, number)
            or pr.get("head", {}).get("repo", {}).get("full_name") != profile["repository"]
            or pr.get("base", {}).get("ref") != profile["default_branch"]
            or pr.get("base", {}).get("sha") != activation["workflow_sha"]
            or pr.get("base", {}).get("repo", {}).get("full_name") != profile["repository"]):
        raise ValueError("PR is closed, merged, foreign or no longer bound to the task")


def _guard(client, profile, activation, artifact, *, verify_previous=False):
    check_arm(client, profile, activation)
    _, record, _ = task_snapshot(client, profile, activation, artifact["issue"],
                                expected_body_sha=artifact["issue_body_sha256"], verify_previous=verify_previous)
    if record["number"] != artifact["task_number"]:
        raise ValueError("Task sequence changed")
    return record


def verify_report_pr(client, profile, activation, pr, *, expected_number, expected_head):
    """Independently verify a current report before a later task can proceed."""
    check_arm(client, profile, activation)
    number = pr if type(pr) is int else pr.get("number") if isinstance(pr, dict) else None
    GitHubClient._number(number)
    head = _sha(expected_head)
    current = client.get_pr(number)
    _pr_identity(current, profile, activation, expected_number, head)
    report = parse_record(current.get("body", ""), REPORT_MARKER)
    if report.get("schema") != "harness-report/v1" or report.get("task_number") != expected_number:
        raise ValueError("Unexpected report protocol or task number")
    spec = task_spec(profile, expected_number)
    files = _files(client, activation["workflow_sha"], head, [spec["document_path"], spec["report_path"]])
    document = _blob(client, files[spec["document_path"]])
    artifact = {key: report[key] for key in _ARTIFACT_FIELDS - {"schema", "issue", "document"} if key in report}
    artifact.update(schema="harness-document/v1", issue=report.get("source_issue"), document=document)
    _validate(profile, activation, artifact, _artifact_env(profile, activation, artifact))
    expected = _verify_branch(client, profile, activation, artifact, head)
    if report != expected or current.get("body") != _body(expected):
        raise ValueError("PR report was edited or differs from verified artifact/commits")
    task = _guard(client, profile, activation, artifact, verify_previous=False)
    if expected_number == 2:
        # Strictly decreasing task number; this cannot recurse through task 2.
        verify_report_pr(client, profile, activation, task["previous_pr"],
                         expected_number=1, expected_head=task["previous_head"])
    _verify_run(client, profile, activation, artifact, completed=True)
    fresh = client.get_pr(number)
    _pr_identity(fresh, profile, activation, expected_number, head)
    if fresh.get("body") != current.get("body"):
        raise ValueError("PR changed during report verification")
    check_arm(client, profile, activation)
    return expected


@contextmanager
def _journal_lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "publisher.lock"
    if path.is_symlink():
        raise ValueError("Publisher lock cannot be a symlink")
    stream = path.open("a+b")
    try:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        stream.close()  # OS locking releases even after a process crash.


def _save_journal(path, record):
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix="publisher-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def publish_task(client, profile, activation, artifact, env, *, journal_root: Path):
    """Return one exact PR; no merges, main writes, code execution or model calls."""
    document = _validate(profile, activation, artifact, env)
    task = _guard(client, profile, activation, artifact, verify_previous=True)
    _verify_run(client, profile, activation, artifact, completed=False)
    spec = task_spec(profile, artifact["task_number"])
    branch = _branch(profile, activation, spec["number"])
    predecessor = client.get_pr(task["previous_pr"]) if spec["number"] == 2 else None
    root = Path(journal_root).resolve()
    folder = root / repository_namespace(profile["repository"]) / activation["nonce"] / str(spec["number"])
    if not folder.resolve().is_relative_to(root):
        raise ValueError("Publisher journal escaped its configured root")
    with _journal_lock(folder):
        path = folder / "publisher.json"
        if path.is_symlink():
            raise ValueError("Publisher journal cannot be a symlink")
        identity = canonical_digest(artifact)
        if path.exists():
            if path.stat().st_size > 131072:
                raise ValueError("Oversized publisher journal")
            journal = strict_json(path.read_text(encoding="utf-8"))
            if (not isinstance(journal, dict) or journal.get("artifact_sha256") != identity
                    or journal.get("branch") != branch or not isinstance(journal.get("steps"), dict)):
                raise ValueError("Publisher journal is bound to another artifact")
        else:
            journal = {"schema": "harness-publisher-journal/v1", "artifact_sha256": identity,
                       "branch": branch, "steps": {}}
            _save_journal(path, journal)

        def guard():
            _guard(client, profile, activation, artifact, verify_previous=False)
            if predecessor is not None:
                latest = client.get_pr(task["previous_pr"])
                _pr_identity(latest, profile, activation, 1, task["previous_head"])
                if latest.get("body") != predecessor.get("body"):
                    raise ValueError("Predecessor changed before publication")

        def write(name, operation):
            previous = journal["steps"].get(name)
            if previous is not None:
                if previous.get("status") == "complete":
                    return previous["result"]
                raise UncertainWrite("Pending publication write must be reconciled, never replayed")
            guard()
            journal["steps"][name] = {"status": "pending"}
            _save_journal(path, journal)
            # Keep pending on any exception, including failure to persist a
            # successful response. Unreachable Git objects may need inspection.
            response = operation()
            if not isinstance(response, dict):
                raise UncertainWrite("Write returned no verifiable object identity")
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
                raise UncertainWrite("Unknown branch creation is not visible; publication remains blocked")
            base = _commit(client, activation["workflow_sha"])
            blob = write("document_blob", lambda: client.create_blob(document, encoding="utf-8"))
            tree = write("implementation_tree", lambda: client.create_tree(base["tree"]["sha"], [
                {"path": spec["document_path"], "mode": "100644", "type": "blob", "sha": _sha(blob["sha"])}]))
            implemented = write("implementation_commit", lambda: client.create_commit(spec["task_id"], _sha(tree["sha"]), [activation["workflow_sha"]]))
            implementation = _sha(implemented["sha"])
            # The final SHA is excluded from the report file, so its temporary
            # placeholder here cannot introduce a self-referential commit hash.
            report_text = _execution_report(_report(artifact, implementation, "0" * 40))
            report_blob = write("report_blob", lambda: client.create_blob(report_text, encoding="utf-8"))
            final_tree = write("report_tree", lambda: client.create_tree(_sha(tree["sha"]), [
                {"path": spec["report_path"], "mode": "100644", "type": "blob", "sha": _sha(report_blob["sha"])}]))
            final = write("report_commit", lambda: client.create_commit(spec["task_id"], _sha(final_tree["sha"]), [implementation]))
            head = _sha(final["sha"])
            _verify_branch(client, profile, activation, artifact, head)
            write("ref", lambda: client.request("POST", "/git/refs", payload={"ref": "refs/heads/" + branch, "sha": head}, expected=(201,)))
            reference = _get_ref(client, branch)
            if reference is None or reference.get("object", {}).get("sha") != head:
                raise UncertainWrite("Published branch could not be read back at its exact SHA")
        else:
            head = _sha(reference.get("object", {}).get("sha"))
        report = _verify_branch(client, profile, activation, artifact, head)
        owner = profile["repository"].split("/")[0]
        prs = client._list("/pulls", {"state": "all", "head": owner + ":" + branch})
        if len(prs) > 1:
            raise ValueError("Multiple task PRs require explicit reconciliation")
        if prs:
            pr_number = GitHubClient._number(prs[0].get("number"))
        else:
            response = write("pr", lambda: client.create_pr(spec["task_id"], _body(report), branch, profile["default_branch"], draft=False))
            pr_number = GitHubClient._number(response.get("number"))
        current = client.get_pr(pr_number)
        _pr_identity(current, profile, activation, spec["number"], head)
        if current.get("body") != _body(report):
            raise ValueError("Current PR body differs from the exact publication report")
        guard()
        journal["verified_pr"] = {"number": pr_number, "head_sha": head}
        journal["status"] = "published_verified"
        _save_journal(path, journal)
        return {**report, "pr": pr_number,
                "url": f"https://github.com/{profile['repository']}/pull/{pr_number}"}
