"""Read-only preflight snapshots for a future, separately commissioned controller.

Not wired to CLI execution/publication. A snapshot is observational, not an
atomic GitHub lease: callers must revalidate immediately before every write.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re

from .github import GitHubClient, GitHubError
from .policy import PolicyError, canonical_digest, parse_event


def _ref_sha(client: GitHubClient, branch: str, *, optional: bool = False) -> str | None:
    try:
        ref = client.get_ref(branch)
    except GitHubError as exc:
        if optional and exc.status == 404:
            return None
        raise
    if (not isinstance(ref, dict) or ref.get("ref") != "refs/heads/" + branch
            or ref.get("object", {}).get("type") != "commit"
            or not re.fullmatch(r"[0-9a-f]{40}", ref.get("object", {}).get("sha", ""))):
        raise PolicyError("remote branch did not resolve to a verified commit")
    return ref["object"]["sha"]


def snapshot_current_task(client: GitHubClient, config: dict, issue: int) -> dict:
    """Read/validate one ready or rework Issue and its unique open task PR.

    Only trusted comments are included as potential controller journal inputs;
    they are not automatically interpreted as state. An existing PR must carry
    ``<!-- harness-issue:N -->`` in its body and match this repository's task
    branch and configured base. This function performs GETs only.
    """
    if client.repository != config.get("repository"):
        raise PolicyError("client repository mismatch")
    current = client.get_issue(issue)
    if not isinstance(current, dict) or current.get("number") != issue:
        raise PolicyError("remote Issue identity mismatch")
    labels = {item.get("name") if isinstance(item, dict) else item for item in current.get("labels", [])}
    eligible = labels & {"agent:ready", "agent:rework"}
    if len(eligible) != 1:
        raise PolicyError("preflight requires one ready or rework state")
    # parse_event provides the same schema/scope/trust checks as an event. The
    # synthetic sender is the authenticated Issue author, not a webhook claim.
    author = current.get("user", {}).get("login", "")
    event = {"repository": {"full_name": client.repository}, "sender": {"login": author},
             "action": "labeled", "label": {"name": next(iter(eligible))}, "issue": current}
    parsed = parse_event(event, config)
    branch = f"agent/issue-{issue}"
    base_branch = config.get("default_branch", config.get("base_branch", "main"))
    base_sha = _ref_sha(client, base_branch)
    task_sha = _ref_sha(client, branch, optional=True)
    prs = client.list_prs(branch)
    if not isinstance(prs, list) or len(prs) > 1:
        raise PolicyError("multiple or invalid task PRs require human reconciliation")
    trusted = {name.casefold() for name in config.get("trusted_actors", [])}
    pr_record = None
    if prs:
        number = prs[0].get("number")
        if type(number) is not int or number <= 0:
            raise PolicyError("invalid remote PR identity")
        pr = client.get_pr(number)
        if (pr.get("number") != number or pr.get("state") != "open"
                or pr.get("user", {}).get("login", "").casefold() not in trusted
                or pr.get("head", {}).get("ref") != branch
                or pr.get("head", {}).get("repo", {}).get("full_name") != client.repository
                or pr.get("base", {}).get("repo", {}).get("full_name") != client.repository
                or pr.get("base", {}).get("ref") != base_branch
                or not task_sha or pr.get("head", {}).get("sha") != task_sha
                or f"<!-- harness-issue:{issue} -->" not in (pr.get("body") or "")):
            raise PolicyError("PR ownership, linkage, branch or head mismatch")
        pr_record = {"number": number, "head_sha": task_sha, "base_branch": base_branch,
                     "updated_at": pr.get("updated_at"), "url": pr.get("html_url")}
    comments = client.list_comments(issue)
    trusted_comments = [{"id": item.get("id"), "author": item.get("user", {}).get("login"),
                         "body": item.get("body", ""), "updated_at": item.get("updated_at")}
                        for item in comments if item.get("user", {}).get("login", "").casefold() in trusted]
    # Detect common races within this read sequence. This is still not a
    # compare-and-swap transaction and does not authorize subsequent writes.
    reread = client.get_issue(issue)
    for field in ("number", "body", "labels", "state", "updated_at"):
        if reread.get(field) != current.get(field):
            raise PolicyError("Issue changed during preflight; retry from a fresh snapshot")
    if _ref_sha(client, base_branch) != base_sha or _ref_sha(client, branch, optional=True) != task_sha:
        raise PolicyError("branch head changed during preflight")
    task = parsed["task"]
    return {"version": 1, "observed_at": datetime.now(timezone.utc).isoformat(),
            "repository": client.repository, "issue": issue, "issue_updated_at": current.get("updated_at"),
            "state_label": parsed["action"], "task_id": task["task_id"],
            "task_digest": canonical_digest(task),
            "criteria_digest": canonical_digest({"acceptance": task["acceptance"], "evidence": task["evidence"]}),
            "branch": branch, "base_branch": base_branch, "base_sha": base_sha,
            "head_sha": task_sha, "pr": pr_record, "trusted_comments": trusted_comments,
            "untrusted_comment_count": len(comments) - len(trusted_comments),
            "label_actor_verified": False, "atomic_lease": False, "publication_authorized": False}
