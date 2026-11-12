"""Post one bounded GitHub-only receipt for a trusted label event.

Never executes an Issue body, calls a model, or automatically retries a write.
"""
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


REPOSITORY = "WangYu0611/chatgpt-deepseek-harness-test-20261028"
TRUSTED_ACTOR = "WangYu0611"
LABEL = "integration:probe"


def receipt(event, run_id):
    if event.get("repository", {}).get("full_name") != REPOSITORY:
        raise ValueError("repository mismatch")
    if event.get("action") != "labeled" or event.get("label", {}).get("name") != LABEL:
        raise ValueError("not the explicit probe label event")
    if event.get("sender", {}).get("login") != TRUSTED_ACTOR:
        raise ValueError("untrusted label actor")
    issue = event.get("issue", {})
    number = issue.get("number")
    if type(number) is not int or number < 1 or "pull_request" in issue or issue.get("state") != "open":
        raise ValueError("expected an open Issue with a positive number")
    if not isinstance(run_id, str) or not run_id.isascii() or not run_id.isdecimal() or int(run_id) < 1:
        raise ValueError("invalid Actions run ID")
    run_url = f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"
    body = (
        "<!-- GITHUB_LINK_PROBE_RECEIPT -->\n"
        "### GitHub linkage probe received\n\n"
        f"- Mode: `github-only`\n- Issue: #{number}\n"
        f"- Actions run: {run_url}\n"
        "- Result: trusted Issue label event reached GitHub Actions and this receipt write.\n"
        "- DeepSeek Harness: **NOT_RUN**\n"
        "- Windows self-hosted Runner: **NOT_RUN**\n"
        "- ChatGPT web Scheduled Task acceptance: **NOT_RUN**\n\n"
        "This receipt proves only the GitHub event-to-workflow-to-comment path. "
        "It is not evidence of a complete unattended development loop."
    )
    return number, run_url, body


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise ValueError("probe can run only in the dedicated GitHub Actions repository")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    number, run_url, body = receipt(event, os.environ["GITHUB_RUN_ID"])
    token = os.environ.get("GH_PROBE_TOKEN", "")
    if not token or any(c in token for c in "\r\n\x00"):
        raise ValueError("missing or invalid workflow token")
    request = Request(
        f"https://api.github.com/repos/{REPOSITORY}/issues/{number}/comments",
        data=json.dumps({"body": body}).encode("utf-8"),
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        method="POST",
    )
    # A lost response may still mean the comment was posted. Never blindly POST again.
    try:
        with build_opener(NoRedirect()).open(request, timeout=20) as response:
            if response.status != 201:
                raise RuntimeError("unexpected response; reconcile the Issue before retrying")
            raw = response.read(65537)
            if len(raw) > 65536:
                raise RuntimeError("oversized response; reconcile the Issue before retrying")
            result = json.loads(raw)
            if type(result.get("id")) is not int or result["id"] < 1:
                raise RuntimeError("missing receipt identity; reconcile the Issue before retrying")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("receipt response uncertain or rejected; inspect Actions and Issue before retrying") from None
    print(json.dumps({"mode": "github-only", "issue": number, "run_url": run_url,
                      "receipt_comment_id": result["id"], "deepseek": "NOT_RUN"}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, RuntimeError) as error:
        # Do not print HTTP headers, response bodies or token-containing tracebacks.
        print("Probe stopped: " + str(error), file=sys.stderr)
        raise SystemExit(1)
