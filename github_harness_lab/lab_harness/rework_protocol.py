"""Pure, fail-closed replay of a bounded GitHub rework journal.

There are no network, model, filesystem or publication operations here. Fetch
the complete journal and its evidence with authenticated APIs before calling.
GitHub comment authorship does NOT prove a ChatGPT scheduled task ran: live
replay additionally requires a provider-specific evidence verifier. Offline
replay deliberately cannot attest an automatic end-to-end loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import copy
import hashlib
import json
import re
from typing import Callable, Iterable


SCHEMA = "harness-rework-journal/v1"
MARKER = "<!-- harness-rework-journal:v1 -->\n"
MAX_COMMENT_BYTES = 32768


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class Policy:
    repository: str
    issue: int
    pr: int
    base_sha: str
    criteria_sha256: str
    workflow_sha: str
    publisher_login: str
    reviewer_login: str
    automation_id: str
    required_criteria: tuple[str, ...]
    max_reworks: int = 3
    journal_location: str = "pr"
    loop_id: str | None = None

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise ProtocolError("Invalid repository")
        for value in (self.issue, self.pr):
            _positive(value, "Issue/PR")
        for name in ("base_sha", "workflow_sha"):
            _digest(getattr(self, name), 40, name)
        _digest(self.criteria_sha256, 64, "criteria")
        for name in ("publisher_login", "reviewer_login", "automation_id"):
            _text(getattr(self, name), name, 200)
        if (not isinstance(self.required_criteria, tuple) or not self.required_criteria
                or len(self.required_criteria) > 50
                or len(set(self.required_criteria)) != len(self.required_criteria)):
            raise ProtocolError("A nonempty unique tuple of required criteria is needed")
        for criterion in self.required_criteria:
            _text(criterion, "required criterion", 100)
        if type(self.max_reworks) is not int or not 0 <= self.max_reworks <= 3:
            raise ProtocolError("max_reworks must be an integer from 0 through 3")
        if self.journal_location not in {"pr", "issue"}:
            raise ProtocolError("journal_location must be pr or issue")
        if self.loop_id is not None:
            _text(self.loop_id, "loop ID", 200)


@dataclass(frozen=True)
class VerifiedEvidence:
    """Returned by a trusted adapter, never deserialized from a journal body.

For candidates authority must be 'github': independently verify the DSH
artifact, actual source/parent, immutable workflow, and CI tested head. For
reviews it must be 'chatgpt_web_scheduled_task': match an official hosted run
to automation ID, run ID, trigger, exact GitHub comment ID and reviewed head.
automatic=False records a manual intervention and prevents an automatic claim.
"""
    authority: str
    automatic: bool


Verifier = Callable[[dict, dict], VerifiedEvidence | None]


@dataclass
class JournalState:
    status: str = "READY"
    head_sha: str | None = None
    revision: int = -1
    last_event_id: int | None = None
    rejections: int = 0
    candidates: list[dict] = field(default_factory=list)
    reviews: list[dict] = field(default_factory=list)
    evidence_verified: bool = False
    automatic: bool = False

    def report(self) -> dict:
        completed = max(0, len(self.candidates) - 1)
        demonstrated = (self.status == "ACCEPTED" and completed >= 2
                        and self.evidence_verified and self.automatic)
        return {
            "schema": SCHEMA, "status": self.status, "head_sha": self.head_sha,
            "revision": self.revision, "last_event_id": self.last_event_id,
            "rejections": self.rejections, "reworks_completed": completed,
            "candidate_count": len(self.candidates),
            "two_automatic_reworks": "VERIFIED" if demonstrated else "NOT_VERIFIED",
            "live_evidence_verified": self.evidence_verified,
            "chatgpt_origin_verified": self.evidence_verified and bool(self.reviews),
            "all_transitions_automatic": self.automatic,
            "events": [{"id": item["event_id"], "revision": item["revision"],
                        "head_sha": item["head_sha"]} for item in self.candidates],
        }


def _text(value, name, bound=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > bound or "\x00" in value:
        raise ProtocolError("Invalid " + name)
    return value


def _positive(value, name):
    if type(value) is not int or value <= 0:
        raise ProtocolError("Invalid " + name)
    return value


def _digest(value, length, name):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{" + str(length) + "}", value):
        raise ProtocolError("Invalid " + name + " digest")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("Duplicate JSON object key")
        result[key] = value
    return result


def encode_record(record: dict) -> str:
    """Produce a comment body; this grants no publication authorization."""
    body = MARKER + json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(body.encode("utf-8")) > MAX_COMMENT_BYTES:
        raise ProtocolError("Journal comment exceeds size bound")
    return body


def _parse_comment(comment: dict, policy: Policy) -> dict | None:
    body = comment.get("body", "")
    if not isinstance(body, str) or not body.startswith(MARKER):
        return None  # Ordinary Issue discussion is not a protocol event.
    if len(body.encode("utf-8")) > MAX_COMMENT_BYTES:
        raise ProtocolError("Journal comment exceeds size bound")
    event_id = _positive(comment.get("id"), "comment ID")
    location = f"pull/{policy.pr}" if policy.journal_location == "pr" else f"issues/{policy.issue}"
    expected_url = f"https://github.com/{policy.repository}/{location}#issuecomment-{event_id}"
    if comment.get("html_url") != expected_url:
        raise ProtocolError("Journal comment is from another Issue or repository")
    if not comment.get("created_at") or comment.get("created_at") != comment.get("updated_at"):
        raise ProtocolError("Journal events must be immutable; edited events need reconciliation")
    try:
        value = json.loads(body[len(MARKER):], object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError("Non-finite JSON value")))
    except (TypeError, json.JSONDecodeError) as error:
        raise ProtocolError("Invalid journal JSON") from error
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ProtocolError("Unsupported journal schema")
    if (value.get("repository") != policy.repository or value.get("issue") != policy.issue
            or type(value.get("issue")) is not int or value.get("pr") != policy.pr
            or type(value.get("pr")) is not int or value.get("base_sha") != policy.base_sha
            or value.get("criteria_sha256") != policy.criteria_sha256):
        raise ProtocolError("Journal task or acceptance criteria changed")
    if policy.loop_id is not None and value.get("loop_id") != policy.loop_id:
        raise ProtocolError("Journal belongs to a different loop")
    kind = value.get("kind")
    login = policy.publisher_login if kind == "candidate" else policy.reviewer_login
    if kind not in {"candidate", "review"} or comment.get("user", {}).get("login") != login:
        raise ProtocolError("Untrusted journal author or event kind")
    _digest(value.get("head_sha"), 40, "head")
    if type(value.get("revision")) is not int or not 0 <= value["revision"] <= policy.max_reworks:
        raise ProtocolError("Invalid or excessive revision")
    previous = value.get("previous_event_id")
    if previous is not None:
        _positive(previous, "previous event ID")
    return value


def _candidate(record: dict, state: JournalState, policy: Policy):
    if state.status not in {"READY", "REWORK"} or record["revision"] != state.revision + 1:
        raise ProtocolError("Candidate requires an initial task or exactly one authorized rework")
    expected_parent = state.head_sha or policy.base_sha
    if record.get("parent_sha") != expected_parent or record["head_sha"] == expected_parent:
        raise ProtocolError("Candidate must be a new commit descending from the previous head")
    if record["head_sha"] in {item["head_sha"] for item in state.candidates}:
        raise ProtocolError("Candidate head was already used")
    execution, ci = record.get("execution"), record.get("ci")
    if not isinstance(execution, dict) or not isinstance(ci, dict):
        raise ProtocolError("Execution and CI evidence are required")
    for obj in (execution, ci):
        _positive(obj.get("run_id"), "Actions run ID")
        _positive(obj.get("run_attempt"), "Actions run attempt")
        if obj.get("workflow_sha") != policy.workflow_sha:
            raise ProtocolError("Evidence came from a different workflow revision")
        _stage_identity(obj)
    _text(execution.get("session_id"), "DSH session", 200)
    for key in ("source_sha256", "artifact_sha256"):
        _digest(execution.get(key), 64, key)
    if (execution.get("completed") is not True or type(execution.get("model_requests")) is not int
            or execution["model_requests"] != 1):
        raise ProtocolError("Candidate needs one completed bounded DSH request")
    if (ci.get("tested_head_sha") != record["head_sha"] or ci.get("status") != "completed"
            or ci.get("conclusion") not in {"success", "failure"}):
        raise ProtocolError("CI must have actually tested this exact head")
    for old in state.candidates:
        if (old["execution"]["source_sha256"] == execution["source_sha256"]
                or old["execution"]["session_id"] == execution["session_id"]
                or _same_stage(old["execution"], execution) or _same_stage(old["ci"], ci)):
            raise ProtocolError("Source, DSH session or Actions evidence reused across revisions")
    state.candidates.append(record)
    state.head_sha, state.revision, state.status = record["head_sha"], record["revision"], "REVIEW"


def _stage_identity(evidence: dict):
    job, stage = evidence.get("job_id"), evidence.get("stage_key")
    if job is None and stage is None:
        raise ProtocolError("Actions evidence requires a job_id or stage_key")
    if job is not None:
        _positive(job, "Actions job ID")
    if stage is not None and (not isinstance(stage, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", stage)):
        raise ProtocolError("Invalid Actions stage key")


def _same_stage(left: dict, right: dict) -> bool:
    # A single workflow DAG may contain all rounds. A distinct job/stage still
    # needs independently verified logs/artifacts for its own candidate head.
    if (left["run_id"], left["run_attempt"]) != (right["run_id"], right["run_attempt"]):
        return False
    return ((left.get("job_id") is not None and left.get("job_id") == right.get("job_id"))
            or (left.get("stage_key") is not None and left.get("stage_key") == right.get("stage_key")))


def _review(record: dict, state: JournalState, policy: Policy, *, verify_origin: bool):
    if (state.status != "REVIEW" or record["head_sha"] != state.head_sha
            or record["revision"] != state.revision):
        raise ProtocolError("Review does not match the current candidate awaiting review")
    ci = state.candidates[-1]["ci"]
    if (type(record.get("ci_run_id")) is not int or type(record.get("ci_run_attempt")) is not int
            or record["ci_run_id"] != ci["run_id"] or record["ci_run_attempt"] != ci["run_attempt"]):
        raise ProtocolError("Review does not match the current CI run/attempt")
    for key in ("job_id", "stage_key"):
        if ci.get(key) is not None and record.get("ci_" + key) != ci[key]:
            raise ProtocolError("Review does not match the current CI job/stage")
    provenance = record.get("provenance", {})
    if not isinstance(provenance, dict):
        raise ProtocolError("Invalid provenance metadata")
    if verify_origin:
        if provenance.get("automation_id") != policy.automation_id:
            raise ProtocolError("Review automation differs from the configured task")
        _text(provenance.get("run_id"), "ChatGPT hosted run ID", 200)
        if provenance.get("trigger") not in {"schedule", "github_pr_event"}:
            raise ProtocolError("Review must identify a scheduled or PR-triggered hosted run")
        if any(item["provenance"]["run_id"] == provenance["run_id"] for item in state.reviews):
            raise ProtocolError("A hosted review run cannot stand for multiple cycles")
    findings = record.get("findings")
    if not isinstance(findings, list) or not 1 <= len(findings) <= 50:
        raise ProtocolError("Review requires bounded criterion-level findings")
    criteria = set()
    statuses = []
    for finding in findings:
        if not isinstance(finding, dict):
            raise ProtocolError("Invalid review finding")
        criterion = _text(finding.get("criterion"), "criterion ID", 100)
        if criterion in criteria:
            raise ProtocolError("Duplicate review criterion")
        criteria.add(criterion)
        _text(finding.get("detail"), "finding detail")
        url = _text(finding.get("evidence_url"), "evidence URL")
        if not url.startswith(f"https://github.com/{policy.repository}/"):
            raise ProtocolError("Finding must link to repository evidence")
        if finding.get("status") not in {"PASS", "FAIL", "BLOCKED"}:
            raise ProtocolError("Invalid finding status")
        statuses.append(finding["status"])
    if criteria != set(policy.required_criteria):
        raise ProtocolError("Review must cover every configured criterion, with no substitutions")
    verdict = record.get("verdict")
    if verdict == "PASS":
        if ci["conclusion"] != "success" or set(statuses) != {"PASS"}:
            raise ProtocolError("Acceptance needs successful exact-head CI and all criteria passing")
        state.status = "ACCEPTED"
    elif verdict == "FAIL":
        if "FAIL" not in statuses or "BLOCKED" in statuses:
            raise ProtocolError("Rework requires concrete failure, not missing evidence")
        _text(record.get("rework_instruction"), "actionable rework instruction", 4000)
        state.rejections += 1
        state.status = "BLOCKED" if state.revision >= policy.max_reworks else "REWORK"
    elif verdict == "BLOCKED":
        if "BLOCKED" not in statuses:
            raise ProtocolError("Blocked review must identify missing evidence")
        state.status = "BLOCKED"
    else:
        raise ProtocolError("Invalid review verdict")
    state.reviews.append(record)


def replay_journal(policy: Policy, comments: Iterable[dict], *, mode: str = "live",
                   verifier: Verifier | None = None, observed_head_sha: str | None = None) -> JournalState:
    """Reconstruct state from a complete authenticated GitHub comment snapshot.

    Operational mode checks authenticated GitHub actor/head records without
    asserting ChatGPT origin; live mode additionally verifies provider evidence.
    Input order must be GitHub creation order (oldest first). Identical delivery
    of an event is a no-op, including a duplicate posted under a new comment ID;
    altered events, forks, gaps, stale reviews and terminal-state writes fail.
    The next writer must use the original last_event_id, re-read current head,
    and serialize writes with an external lease/CAS. This function is NOT that
    lease and cannot prove a snapshot is complete or prevent concurrent writes.
    """
    if mode not in {"live", "operational", "offline"}:
        raise ProtocolError("mode must be live, operational or offline")
    if mode == "live" and (not callable(verifier) or observed_head_sha is None):
        raise ProtocolError("Live replay needs an independent evidence verifier and current PR head")
    if mode == "operational" and observed_head_sha is None:
        raise ProtocolError("Operational replay needs the current PR head")
    if observed_head_sha is not None:
        _digest(observed_head_sha, 40, "observed head")
    state = JournalState(evidence_verified=mode == "live", automatic=mode == "live")
    ids: dict[int, str] = {}
    bodies: set[str] = set()
    for comment in comments:
        if not isinstance(comment, dict):
            raise ProtocolError("Invalid GitHub comment observation")
        record = _parse_comment(comment, policy)
        if record is None:
            continue
        event_id = comment["id"]
        fingerprint = hashlib.sha256(encode_record(record).encode()).hexdigest()
        if event_id in ids and ids[event_id] != fingerprint:
            raise ProtocolError("Previously seen event was edited")
        ids[event_id] = fingerprint
        if fingerprint in bodies:
            continue
        if record.get("previous_event_id") != state.last_event_id:
            raise ProtocolError("Journal gap, fork or noncanonical duplicate predecessor")
        if mode == "live":
            proof = verifier(copy.deepcopy(record), copy.deepcopy(comment))
            expected = "github" if record["kind"] == "candidate" else "chatgpt_web_scheduled_task"
            if (not isinstance(proof, VerifiedEvidence) or proof.authority != expected
                    or type(proof.automatic) is not bool):
                raise ProtocolError("Independent evidence verification failed")
            state.automatic = state.automatic and proof.automatic
        # Work on a fresh decoded object; caller data remains unmodified.
        record["event_id"] = event_id
        if record["kind"] == "candidate":
            _candidate(record, state, policy)
        else:
            _review(record, state, policy, verify_origin=mode == "live")
        bodies.add(fingerprint)
        state.last_event_id = event_id
    if observed_head_sha is not None and state.head_sha != observed_head_sha:
        raise ProtocolError("Current PR head differs from the journal; acceptance is invalid")
    if not state.candidates:
        state.evidence_verified = state.automatic = False
    return state
