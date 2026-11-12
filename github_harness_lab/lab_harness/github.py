"""Gated GitHub REST primitives, tested with an injected offline transport.

This module is intentionally not wired to the CLI. It does not implement a
publisher, distributed lease, permission attestation or remote reconciliation.
API references: https://docs.github.com/en/rest/git and
https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api
"""
from __future__ import annotations

from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import json
import math
import os
import re
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .policy import safe_path


class GitHubError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class NetworkDisabled(GitHubError):
    pass


class UncertainWrite(GitHubError):
    """Do not replay: reconcile remote state using a stable marker first."""


class IncompleteResult(GitHubError):
    """No partial list is returned when pagination cannot prove completeness."""


class RateLimited(GitHubError):
    def __init__(self, wait_seconds: float, *, status: int | None = None):
        super().__init__("GitHub rate limit requires a later retry", status=status)
        self.retry_after = wait_seconds


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Fail closed even for a same-host repository rename. A human must
        # update the configured repository; credentials never follow redirects.
        return None


def urllib_transport(request: Request, timeout: float, max_bytes: int) -> Response:
    """The sole network boundary; clients must gate before reaching this call."""
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        stream = opener.open(request, timeout=timeout)
    except HTTPError as exc:
        stream = exc
    with stream:
        return Response(stream.code, dict(stream.headers.items()), stream.read(max_bytes + 1))


class GitHubClient:
    def __init__(self, repository: str, *, network_enabled: bool = False,
                 authorization_gate: Callable[[str], object] | None = None,
                 token_env: str = "GH_TOKEN", transport: Callable | None = None,
                 timeout_seconds: float = 15, max_attempts: int = 3,
                 max_pages: int = 10, max_items: int = 1000,
                 max_response_bytes: int = 8 * 1024 * 1024,
                 max_retry_wait_seconds: float = 60,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time):
        if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("repository must be owner/name")
        if any(part in {".", ".."} for part in repository.split("/")):
            raise ValueError("invalid repository")
        for value in (timeout_seconds, max_retry_wait_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("timeouts must be finite positive numbers")
        for value in (max_attempts, max_pages, max_items, max_response_bytes):
            if type(value) is not int or value < 1:
                raise ValueError("request bounds must be positive integers")
        if max_attempts > 5 or timeout_seconds > 60 or max_retry_wait_seconds > 60:
            raise ValueError("request/retry bounds exceed the supported limit")
        self.repository = repository
        self.network_enabled = network_enabled
        self.authorization_gate = authorization_gate
        self.token_env = token_env
        self._transport = urllib_transport if transport is None else transport
        self.timeout_seconds, self.max_attempts = timeout_seconds, max_attempts
        self.max_pages, self.max_items = max_pages, max_items
        self.max_response_bytes = max_response_bytes
        self.max_retry_wait_seconds = max_retry_wait_seconds
        self._sleep, self._clock = sleep, clock
        self._base = "https://api.github.com/repos/" + repository

    def __repr__(self):
        return f"GitHubClient(repository={self.repository!r}, network_enabled={self.network_enabled is True})"

    def _gate(self, write: bool):
        if self.network_enabled is not True or self.authorization_gate is None:
            raise NetworkDisabled("HTTP is disabled; explicit network enablement and authorization gate are required")
        if self.authorization_gate("github-write" if write else "github-read") is False:
            raise NetworkDisabled("authorization gate rejected HTTP")

    def _url(self, path: str, params: dict | None = None) -> str:
        if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
                or not re.fullmatch(r"/[A-Za-z0-9_./-]*", path)
                or any(part in {".", ".."} for part in path.split("/"))):
            raise ValueError("endpoint must be a relative repository path")
        url = self._base + path
        return url + ("?" + urlencode(params) if params else "")

    def _retry_wait(self, response: Response, attempt: int) -> float:
        headers = {key.lower(): value for key, value in response.headers.items()}
        value = headers.get("retry-after")
        try:
            if value is not None:
                try:
                    delay = float(value)
                except ValueError:
                    delay = parsedate_to_datetime(value).timestamp() - self._clock()
            elif headers.get("x-ratelimit-remaining") == "0":
                delay = float(headers["x-ratelimit-reset"]) - self._clock() if "x-ratelimit-reset" in headers else 60
            else:
                delay = 60 * (2 ** attempt) if response.status in {403, 429} else 2 ** attempt
            return max(0, delay) if math.isfinite(delay) else float("inf")
        except (ValueError, TypeError, OverflowError):
            return 60 * (2 ** attempt)

    def _exchange(self, method: str, url: str, payload: dict | None,
                  expected: tuple[int, ...]) -> tuple[object, dict[str, str]]:
        write = method != "GET"
        # URL is built internally or validated against this exact origin/path
        # before following pagination. No external callback can change it.
        self._gate(write)
        token = os.environ.get(self.token_env, "")
        if not token or any(char in token for char in "\r\n\x00"):
            raise GitHubError("a valid token environment variable is required")
        encoded = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        headers = {"Accept": "application/vnd.github+json", "Authorization": "Bearer " + token,
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "github-harness-lab/0.1"}
        if encoded is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        for attempt in range(self.max_attempts if not write else 1):
            self._gate(write)  # Expiring authorization is rechecked before every retry.
            request = Request(url, data=encoded, headers=headers, method=method)
            try:
                response = self._transport(request, self.timeout_seconds, self.max_response_bytes)
            except (OSError, URLError, TimeoutError):
                if write:
                    raise UncertainWrite("GitHub write response is uncertain; reconcile before retrying") from None
                if attempt + 1 >= self.max_attempts:
                    raise GitHubError("GitHub read transport failed after bounded retries") from None
                self._sleep(2 ** attempt)
                continue
            except Exception:
                error = UncertainWrite if write else GitHubError
                raise error("transport failed without a usable response; no operation was replayed") from None
            if not isinstance(response, Response) or not isinstance(response.body, bytes):
                error = UncertainWrite if write else GitHubError
                raise error("invalid transport response; no operation was replayed")
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            if len(response.body) > self.max_response_bytes:
                error = UncertainWrite if write else IncompleteResult
                raise error("GitHub response exceeded the byte limit", status=response.status)
            if response.status in expected:
                try:
                    data = json.loads(response.body) if response.body else None
                except (UnicodeDecodeError, json.JSONDecodeError):
                    error = UncertainWrite if write else GitHubError
                    raise error("GitHub returned an invalid JSON response", status=response.status) from None
                if isinstance(data, dict) and data.get("truncated") is True:
                    raise IncompleteResult("GitHub returned a truncated result", status=response.status)
                return data, response_headers
            if 300 <= response.status < 400:
                error = UncertainWrite if write else GitHubError
                raise error("GitHub redirect refused; confirm repository configuration", status=response.status)
            limited = response.status == 429 or (response.status == 403 and (
                "retry-after" in response_headers or response_headers.get("x-ratelimit-remaining") == "0"
                or b"secondary rate limit" in response.body.lower()))
            transient = limited or 500 <= response.status <= 599
            if write and (transient or 200 <= response.status < 300):
                raise UncertainWrite("GitHub write outcome requires reconciliation; no automatic retry", status=response.status)
            if not write and transient:
                delay = self._retry_wait(response, attempt)
                if delay > self.max_retry_wait_seconds or attempt + 1 >= self.max_attempts:
                    if limited:
                        raise RateLimited(delay, status=response.status)
                    raise GitHubError("GitHub read failed after bounded retries", status=response.status)
                self._sleep(delay)
                continue
            # Error bodies can echo user input or credentials. Do not expose them.
            raise GitHubError("GitHub request was rejected", status=response.status)
        raise GitHubError("bounded request attempts exhausted")

    def request(self, method: str, path: str, *, params: dict | None = None,
                payload: dict | None = None, expected: tuple[int, ...] = (200,)):
        method = method.upper()
        if method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
            raise ValueError("unsupported HTTP method")
        return self._exchange(method, self._url(path, params), payload, expected)[0]

    def _list(self, path: str, params: dict | None = None) -> list:
        url = self._url(path, {**(params or {}), "per_page": 100})
        endpoint = urlsplit(url).path
        seen, result = set(), []
        for _ in range(self.max_pages):
            if url in seen:
                raise IncompleteResult("pagination loop refused")
            seen.add(url)
            data, headers = self._exchange("GET", url, None, (200,))
            if not isinstance(data, list):
                raise IncompleteResult("expected a complete JSON list")
            if len(result) + len(data) > self.max_items:
                raise IncompleteResult("pagination item limit reached")
            result.extend(data)
            matches = re.findall(r'<([^>]+)>\s*;\s*rel="([^"]+)"', headers.get("link", ""))
            next_links = [link for link, relation in matches if "next" in relation.split()]
            if not next_links:
                if "next" in headers.get("link", ""):
                    raise IncompleteResult("malformed pagination Link header")
                return result
            if len(next_links) != 1:
                raise IncompleteResult("ambiguous pagination Link header")
            url = next_links[0]
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.netloc != "api.github.com" or parsed.path != endpoint or parsed.fragment:
                raise IncompleteResult("pagination escaped the configured repository endpoint")
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            query = dict(pairs)
            if len(query) != len(pairs) or not set(query) <= ({"page", "per_page"} | set(params or {})):
                raise IncompleteResult("unsupported pagination query")
            if any(query.get(key) != str(value) for key, value in (params or {}).items()):
                raise IncompleteResult("pagination changed the requested filters")
        raise IncompleteResult("pagination page limit reached")

    @staticmethod
    def _number(value: int) -> int:
        if type(value) is not int or value <= 0:
            raise ValueError("Issue/PR number must be positive")
        return value

    @staticmethod
    def _sha(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
            raise ValueError("expected a full lowercase Git SHA-1")
        return value

    @staticmethod
    def _branch(value: str) -> str:
        if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)
                or any(part in {"", ".", ".."} for part in value.split("/"))
                or ".." in value or "@{" in value or value.endswith((".", "/", ".lock"))):
            raise ValueError("unsupported branch name")
        return value

    def get_issue(self, number: int):
        return self.request("GET", f"/issues/{self._number(number)}")

    def get_pr(self, number: int):
        return self.request("GET", f"/pulls/{self._number(number)}")

    def list_comments(self, number: int):
        return self._list(f"/issues/{self._number(number)}/comments")

    def list_prs(self, branch: str):
        return self._list("/pulls", {"state": "open", "head": self.repository.split("/")[0] + ":" + self._branch(branch)})

    def create_pr(self, title: str, body: str, head: str, base: str, *, draft: bool = True):
        return self.request("POST", "/pulls", payload={"title": title, "body": body,
                            "head": self._branch(head), "base": self._branch(base), "draft": draft}, expected=(201,))

    def comment(self, number: int, body: str):
        return self.request("POST", f"/issues/{self._number(number)}/comments", payload={"body": body}, expected=(201,))

    def update_labels(self, number: int, labels: list[str]):
        if not isinstance(labels, list) or not all(isinstance(item, str) and item for item in labels):
            raise ValueError("labels must be a string array")
        # PUT replaces all labels; caller must preserve unrelated labels and
        # reconcile concurrent human edits before invoking this primitive.
        return self.request("PUT", f"/issues/{self._number(number)}/labels", payload={"labels": labels})

    def create_blob(self, content: str, *, encoding: str = "base64"):
        if encoding not in {"base64", "utf-8"}:
            raise ValueError("unsupported blob encoding")
        return self.request("POST", "/git/blobs", payload={"content": content, "encoding": encoding}, expected=(201,))

    def create_tree(self, base_tree: str, entries: list[dict]):
        self._sha(base_tree)
        if not isinstance(entries, list) or not entries:
            raise ValueError("tree changes must be nonempty")
        checked = []
        for item in entries:
            path = safe_path(item["path"])
            if item.get("type") != "blob" or item.get("mode") not in {"100644", "100755"}:
                raise ValueError("only ordinary reviewed blob entries are supported")
            sha = item.get("sha")
            if sha is not None:
                self._sha(sha)
            if set(item) != {"path", "mode", "type", "sha"}:
                raise ValueError("tree entries must explicitly identify their blob SHA or deletion")
            checked.append({"path": path, "mode": item["mode"], "type": "blob", "sha": sha})
        if len({item["path"] for item in checked}) != len(checked):
            raise ValueError("duplicate tree paths")
        return self.request("POST", "/git/trees", payload={"base_tree": base_tree, "tree": checked}, expected=(201,))

    def create_commit(self, message: str, tree: str, parents: list[str]):
        if not isinstance(parents, list) or len(parents) != 1:
            raise ValueError("task commits must have one verified parent")
        return self.request("POST", "/git/commits", payload={"message": message, "tree": self._sha(tree),
                            "parents": [self._sha(parents[0])]}, expected=(201,))

    def get_ref(self, branch: str):
        return self.request("GET", "/git/ref/heads/" + self._branch(branch))

    def create_ref(self, branch: str, sha: str):
        branch = self._branch(branch)
        if not re.fullmatch(r"agent/issue-[1-9][0-9]*", branch):
            raise ValueError("writes are restricted to task branches")
        return self.request("POST", "/git/refs", payload={"ref": "refs/heads/" + branch, "sha": self._sha(sha)}, expected=(201,))

    def update_ref(self, branch: str, sha: str):
        branch = self._branch(branch)
        if not re.fullmatch(r"agent/issue-[1-9][0-9]*", branch):
            raise ValueError("writes are restricted to task branches")
        return self.request("PATCH", "/git/refs/heads/" + branch, payload={"sha": self._sha(sha), "force": False})
