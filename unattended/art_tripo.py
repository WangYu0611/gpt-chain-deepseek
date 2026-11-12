"""Budgeted Tripo v3 jobs; a lost POST response is never permission to retry.

The private ledger contains API output URLs. Only ``status``/method return values
are suitable for reports. ``estimated_credits`` must be an upper bound for the
selected operation, not an average; server charges beyond the cap freeze the
batch. This module neither imports the prototype nor reads a key at import time.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .secrets import LOCAL_SECRETS_NAME, read_secret
from .storage import atomic_replace, read_json, write_json

TERMINAL = {"success", "failed", "cancelled", "banned", "expired"}
TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
ENDPOINT = re.compile(r"^(generation|animations|mesh)/[a-z0-9-]+$")
HARD_CAP = Decimal("3000")


class TripoError(RuntimeError):
    """Messages deliberately exclude credentials, response bodies and URLs."""


class BudgetExceeded(TripoError):
    pass


class BatchBusy(TripoError):
    pass


class RequestConflict(TripoError):
    pass


def _money(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("credits must be a finite nonnegative number") from None
    if isinstance(value, bool) or not result.is_finite() or result < 0:
        raise ValueError("credits must be a finite nonnegative number")
    return result


def _https(url):
    if not isinstance(url, str):
        raise TripoError("expected an HTTPS URL")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise TripoError("only HTTPS URLs without embedded credentials are allowed")
    return parsed


class _NoAPIRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TripoError("API redirect refused")


class _HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _urls(value, prefix=""):
    result = {}
    if isinstance(value, dict):
        for key, item in value.items():
            result.update(_urls(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.update(_urls(item, f"{prefix}.{index}"))
    elif isinstance(value, str) and value.startswith(("https://", "http://")):
        _https(value)
        result[prefix] = value
    return result


def _file_digest(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


class TripoClient:
    def __init__(self, state_dir, key_file, budget_limit=3000,
                 base_url="https://openapi.tripo3d.com/v3", *, request_timeout=90,
                 max_download_bytes=2 * 1024**3):
        self.root = Path(state_dir).resolve()
        self.key_file = Path(key_file).resolve()
        self.budget_limit = _money(budget_limit)
        if not 0 < self.budget_limit <= HARD_CAP:
            raise ValueError("batch budget must be between 0 and 3000 credits")
        parsed = _https(base_url)
        if parsed.query or parsed.fragment:
            raise ValueError("API base must not contain query or fragment")
        self.base_url = base_url.rstrip("/")
        self.request_timeout = float(request_timeout)
        if not math.isfinite(self.request_timeout) or self.request_timeout <= 0:
            raise ValueError("request timeout must be positive")
        self.max_download_bytes = int(max_download_bytes)
        if self.max_download_bytes <= 0:
            raise ValueError("download limit must be positive")
        self.ledger_path = self.root / "ledger.private.json"
        self.lock_path = self.root / "batch.lock"

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as stream:
            # On Windows even reading byte zero before locking fails while
            # another process owns it. Lock first (locking beyond EOF is valid).
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise BatchBusy("art batch is already in use") from None
            try:
                if os.fstat(stream.fileno()).st_size == 0:
                    stream.write(b"0")
                    stream.flush()
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _load(self):
        if not self.ledger_path.exists():
            return {"version": 1, "budget_limit": float(self.budget_limit),
                    "base_url": self.base_url, "tasks": {}}
        try:
            ledger = read_json(self.ledger_path)
            if ledger["version"] != 1 or not isinstance(ledger["tasks"], dict):
                raise ValueError()
            if _money(ledger["budget_limit"]) != self.budget_limit or ledger["base_url"] != self.base_url:
                raise TripoError("existing batch budget/API configuration must not change")
            for tag, task in ledger["tasks"].items():
                if not TAG.fullmatch(tag) or task["tag"] != tag or not task["request_hash"]:
                    raise ValueError()
                _money(task["reserved_credits"])
                if task.get("actual_credits") is not None:
                    _money(task["actual_credits"])
                if task["submission"] == "intent" and not task.get("task_id"):
                    task["submission"] = "unknown"
                    task["error"] = "submission_interrupted_without_task_id"
            return ledger
        except TripoError:
            raise
        except Exception:
            raise TripoError("private ledger is invalid; refusing to submit") from None

    def _save(self, ledger):
        write_json(self.ledger_path, ledger)

    def _budget(self, ledger):
        actual = reserved = unknown = Decimal(0)
        for task in ledger["tasks"].values():
            if task.get("actual_credits") is not None:
                actual += _money(task["actual_credits"])
            amount = _money(task["reserved_credits"])
            if task["submission"] in {"unknown", "intent"} or task.get("cost_unknown"):
                unknown += amount
            else:
                reserved += amount
        used = actual + reserved + unknown
        return {"limit": float(self.budget_limit), "actual": float(actual),
                "reserved": float(reserved), "unknown": float(unknown),
                "committed": float(used), "remaining": float(max(Decimal(0), self.budget_limit - used)),
                "breached": used > self.budget_limit}

    def _public(self, task, ledger):
        # Whitelist only local identifiers and verified artifact metadata.
        budget = self._budget(ledger)
        previously_ready = (task["api_status"] == "success" and task["download_status"] == "complete"
                            and task.get("actual_credits") is not None and not budget["breached"] and not task.get("error"))
        local_verified = self._local_artifacts_valid(task) if previously_ready else None
        result = {key: task.get(key) for key in (
                    "tag", "task_id", "endpoint", "request_hash", "submission", "api_status",
                    "download_status", "actual_credits", "reserved_credits", "cost_unknown",
                    "required_outputs", "files", "error")}
        result.update(ready=previously_ready and local_verified is True,
                      local_artifacts_verified=local_verified, budget=budget)
        if previously_ready and local_verified is False:
            result["error"] = "local_artifact_verification_failed"
        return result

    def _local_artifacts_valid(self, task):
        """Read-only verification; never contacts Tripo or changes the ledger."""
        try:
            required = task["required_outputs"]
            if required is None:
                required = list(_urls(task.get("output", {})))
            if not required and task["endpoint"] != "animations/rig-check":
                return False
            for field in required:
                entry = task["files"][field]
                path = Path(entry["path"]).resolve()
                if not path.is_relative_to(self.root / "artifacts") or not path.is_file():
                    return False
                if _file_digest(path) != (entry["size"], entry["sha256"]):
                    return False
            return True
        except (OSError, ValueError, TypeError, KeyError, TripoError):
            return False

    def status(self, tag=None):
        """Report current local readiness, rehashing completed artifacts without API calls."""
        with self._locked():
            ledger = self._load()
            if tag is not None:
                if tag not in ledger["tasks"]:
                    raise TripoError("unknown local task tag")
                return self._public(ledger["tasks"][tag], ledger)
            return {"budget": self._budget(ledger),
                    "tasks": {name: self._public(task, ledger) for name, task in ledger["tasks"].items()}}

    def _api_json(self, method, path, body=None):
        try:
            if self.key_file.name == LOCAL_SECRETS_NAME:
                key = read_secret("TRIPO_API_KEY", secrets_path=self.key_file)
            else:
                key = self.key_file.read_text(encoding="utf-8-sig").strip()
        except (OSError, ValueError):
            raise TripoError("cannot read configured Tripo key file") from None
        if not key or "\n" in key or "\r" in key:
            raise TripoError("configured Tripo key is invalid")
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method,
                                         headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        opener = urllib.request.build_opener(_NoAPIRedirect())
        with opener.open(request, timeout=self.request_timeout) as response:
            payload = response.read(8 * 1024 * 1024 + 1)
        if len(payload) > 8 * 1024 * 1024:
            raise TripoError("API response exceeds limit")
        result = json.loads(payload)
        if not isinstance(result, dict):
            raise TripoError("invalid API response")
        return result

    def _resolve(self, value, ledger):
        if isinstance(value, str) and value.startswith("@"):
            tag, _, field = value[1:].partition(":")
            task = ledger["tasks"].get(tag)
            if not task or task["api_status"] != "success" or not task.get("task_id"):
                raise TripoError("input task has not succeeded")
            if field:
                output = task.get("output", {})
                for part in field.split("."):
                    if not isinstance(output, dict) or part not in output:
                        raise TripoError("input output field is absent")
                    output = output[part]
                return output
            return task["task_id"]
        if isinstance(value, dict):
            return {key: self._resolve(item, ledger) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve(item, ledger) for item in value]
        return value

    def submit(self, tag, endpoint, body, estimated_credits, *, required_outputs=None):
        if not isinstance(tag, str) or not TAG.fullmatch(tag):
            raise ValueError("invalid asset stage tag")
        if not isinstance(endpoint, str) or not ENDPOINT.fullmatch(endpoint):
            raise ValueError("invalid generation/animations/mesh endpoint")
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        estimate = _money(estimated_credits)
        if estimate == 0 and endpoint != "animations/rig-check":
            raise ValueError("paid generation requires a positive cost reservation")
        if required_outputs is not None and (not isinstance(required_outputs, (list, tuple))
                or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", item) for item in required_outputs)):
            raise ValueError("required_outputs must contain output field names")
        required = list(required_outputs) if required_outputs is not None else None
        if required == [] and endpoint != "animations/rig-check":
            raise ValueError("asset generation requires at least one output field")
        canonical = json.dumps({"endpoint": endpoint, "body": body, "required_outputs": required},
                               ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self._locked():
            ledger = self._load()
            if tag in ledger["tasks"]:
                task = ledger["tasks"][tag]
                if task["request_hash"] != digest:
                    raise RequestConflict("same task tag has a different request; use a new candidate tag")
                self._save(ledger)
                return self._public(task, ledger)
            resolved = self._resolve(body, ledger)
            budget = self._budget(ledger)
            if _money(budget["committed"]) + estimate > self.budget_limit:
                raise BudgetExceeded("batch actual + reserved + unknown costs exceed available budget")
            task = {"tag": tag, "endpoint": endpoint, "request_hash": digest, "body": body,
                    "resolved_body": resolved, "submission": "intent", "api_status": "unknown",
                    "task_id": None, "reserved_credits": float(estimate), "actual_credits": None,
                    "cost_unknown": False, "required_outputs": required, "download_status": "pending",
                    "files": {}, "created_at": time.time(), "error": None}
            ledger["tasks"][tag] = task
            self._save(ledger)  # Reserve before any externally visible POST.
            try:
                response = self._api_json("POST", "/" + endpoint, resolved)
                task_id = response.get("data", {}).get("task_id") if response.get("code") == 0 else None
                if not isinstance(task_id, str) or not TAG.fullmatch(task_id):
                    raise TripoError("submission response has no valid task ID")
            except Exception as error:
                task["submission"] = "unknown"
                task["error"] = "submission_result_unknown"
                if isinstance(error, urllib.error.HTTPError):
                    task["error"] += "_http_" + str(error.code)
                self._save(ledger)
                return self._public(task, ledger)
            task.update(task_id=task_id, submission="submitted", api_status="submitted", error=None)
            self._save(ledger)
            return self._public(task, ledger)

    def _download_one(self, url, destination):
        _https(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".part")
        digest, size = hashlib.sha256(), 0
        try:
            opener = urllib.request.build_opener(_HTTPSRedirect())
            with opener.open(urllib.request.Request(url), timeout=self.request_timeout) as response:
                _https(response.geturl())
                content_length = response.headers.get("Content-Length")
                expected = int(content_length) if content_length is not None else None
                if expected is not None and (expected <= 0 or expected > self.max_download_bytes):
                    raise TripoError("artifact size is outside download limits")
                with temporary.open("xb") as stream:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > self.max_download_bytes:
                            raise TripoError("artifact exceeds download limit")
                        stream.write(chunk)
                        digest.update(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            if size <= 0 or (expected is not None and size != expected):
                raise TripoError("artifact download is incomplete")
            atomic_replace(temporary, destination)
            return {"path": str(destination), "size": size, "sha256": digest.hexdigest()}
        finally:
            temporary.unlink(missing_ok=True)

    def _downloads(self, task, ledger):
        outputs = _urls(task.get("output", {}))
        required = task["required_outputs"]
        if required is None:
            required = list(outputs)
            if not required and task["endpoint"] != "animations/rig-check":
                raise TripoError("successful task has no downloadable output")
        if any(field not in outputs for field in required):
            raise TripoError("required output is missing")
        for field in required:
            current = task["files"].get(field)
            if current:
                path = Path(current["path"]).resolve()
                if not path.is_relative_to(self.root / "artifacts"):
                    raise TripoError("artifact escaped batch directory")
                if path.is_file() and _file_digest(path) == (current["size"], current["sha256"]):
                    continue
            url = outputs[field]
            extension = Path(urllib.parse.urlsplit(url).path).suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,8}", extension):
                extension = ".bin"
            filename = re.sub(r"[^A-Za-z0-9_-]", "_", field) + "_" + hashlib.sha256(field.encode()).hexdigest()[:8] + extension
            destination = self.root / "artifacts" / task["tag"] / filename
            if not destination.resolve().is_relative_to(self.root / "artifacts"):
                raise TripoError("artifact escaped batch directory")
            task["files"][field] = self._download_one(url, destination)
            self._save(ledger)  # A later file failure does not discard this download.
        task["download_status"] = "complete"

    def refresh(self, tag, *, download=True):
        with self._locked():
            ledger = self._load()
            if tag not in ledger["tasks"]:
                raise TripoError("unknown local task tag")
            task = ledger["tasks"][tag]
            if not task.get("task_id"):
                self._save(ledger)
                return self._public(task, ledger)
            try:
                response = self._api_json("GET", "/tasks/" + urllib.parse.quote(task["task_id"], safe=""))
                data = response.get("data")
                if response.get("code") != 0 or not isinstance(data, dict) or not isinstance(data.get("status"), str):
                    raise TripoError("invalid task status response")
                task["api_status"] = data["status"]
                task["error"] = None
                if data["status"] in TERMINAL:
                    credits = data.get("credits_consumed")
                    if credits is not None:
                        actual = _money(credits)
                        if task.get("actual_credits") is not None:
                            # A later stale response must not give spent budget
                            # back. Refund reconciliation is an explicit action.
                            actual = max(actual, _money(task["actual_credits"]))
                        task["actual_credits"] = float(actual)
                        task["reserved_credits"] = 0.0
                        task["cost_unknown"] = False
                    elif task.get("actual_credits") is None:
                        task["cost_unknown"] = True
                    if data["status"] == "success":
                        task["output"] = data.get("output") or {}
                        task["download_status"] = "pending"
                    else:
                        task["download_status"] = "not_applicable"
                self._save(ledger)  # API completion and artifact completion are separate.
            except Exception as error:
                task["error"] = "status_refresh_failed"
                if isinstance(error, urllib.error.HTTPError):
                    task["error"] += "_http_" + str(error.code)
                self._save(ledger)
                return self._public(task, ledger)
            if task["api_status"] == "success" and download:
                try:
                    self._downloads(task, ledger)
                except Exception as error:
                    task["download_status"] = "failed"
                    task["error"] = "artifact_download_failed"
                    if isinstance(error, urllib.error.HTTPError):
                        task["error"] += "_http_" + str(error.code)
                self._save(ledger)
            return self._public(task, ledger)

    def run(self, tag, endpoint, body, estimated_credits, *, poll_seconds=6,
            timeout_seconds=1500, required_outputs=None):
        """Submit/resume and poll; ``timeout_seconds`` is a polling time budget.

        It starts after submission and is checked between refresh operations.
        HTTP operations use ``request_timeout`` per socket operation, so a slow
        streaming download can exceed this polling budget. The invoking worker
        must supply the overall process wall-clock limit.
        """
        if not math.isfinite(poll_seconds) or poll_seconds < 0 or not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise ValueError("poll and timeout durations must be finite and nonnegative")
        result = self.submit(tag, endpoint, body, estimated_credits, required_outputs=required_outputs)
        deadline = time.monotonic() + timeout_seconds
        while result.get("task_id"):
            result = self.refresh(tag)
            if result["ready"] or result["api_status"] in TERMINAL or time.monotonic() >= deadline:
                break
            time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))
        return result
