"""Offline durable task ledger with leases, fencing and replay protection.

GitHub is the eventual cross-agent record; this SQLite database is a local
execution cache. Export snapshots for a trusted controller to publish. This
module has no network or subprocess capabilities.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator
import uuid


STATES = frozenset({"READY", "RUNNING", "REVIEW", "REWORK", "ACCEPTED", "BLOCKED", "FAILED"})


class StateError(RuntimeError):
    pass


class LeaseLost(StateError):
    """A caller no longer owns the active, unexpired execution lease."""


class InvalidTransition(StateError):
    pass


class StateStore:
    def __init__(self, path: str | Path, *, max_reworks: int = 3, max_failures: int = 3):
        if type(max_reworks) is not int or not 0 <= max_reworks <= 3:
            raise ValueError("max_reworks must be an integer from 0 through 3")
        if type(max_failures) is not int or max_failures < 1:
            raise ValueError("max_failures must be positive")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_reworks = max_reworks
        self.max_failures = max_failures
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    issue TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    reworks INTEGER NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0,
                    active_event TEXT,
                    owner TEXT,
                    lease_until REAL NOT NULL DEFAULT 0,
                    head_sha TEXT,
                    result TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    issue TEXT NOT NULL REFERENCES tasks(issue),
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue TEXT,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS controls (
                    name TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO controls(name,value) VALUES ('paused','false');
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _now(now: float | None) -> float:
        return time.time() if now is None else float(now)

    @staticmethod
    def _key(value: Any, name: str) -> str:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError(f"{name} must be text or an integer")
        key = str(value)
        if not key.strip() or len(key) > 512 or "\x00" in key:
            raise ValueError(f"invalid {name}")
        return key

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["result"] = json.loads(item["result"])
        return item

    @classmethod
    def _task(cls, db: sqlite3.Connection, issue: str) -> dict[str, Any]:
        item = cls._decode(db.execute("SELECT * FROM tasks WHERE issue=?", (issue,)).fetchone())
        if item is None:
            raise StateError(f"unknown issue: {issue}")
        return item

    @staticmethod
    def _audit(db: sqlite3.Connection, issue: str | None, action: str, detail: dict, now: float):
        db.execute("INSERT INTO audit(issue,action,detail,created_at) VALUES (?,?,?,?)",
                   (issue, action, json.dumps(detail, sort_keys=True), now))

    @staticmethod
    def _owned(item: dict, owner: str, now: float):
        if item["status"] != "RUNNING" or not owner or item["owner"] != owner or item["lease_until"] <= now:
            raise LeaseLost("execution lease is expired or belongs to a different worker")

    def claim(self, issue: str | int, event_id: str, *, now: float | None = None,
              lease_seconds: float = 300) -> dict[str, Any] | None:
        """Claim work, or return None for paused, duplicate, busy or terminal work.

        Only the *same* event may recover an expired RUNNING lease. Recovery
        retains its attempt/checkpoints but issues a fresh owner fencing token.
        A different delivery that arrives while busy is not consumed.
        """
        issue, event_id = self._key(issue, "issue"), self._key(event_id, "event_id")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = self._now(now)
        with self._transaction() as db:
            if db.execute("SELECT value FROM controls WHERE name='paused'").fetchone()[0] == "true":
                return None
            db.execute("INSERT OR IGNORE INTO tasks(issue,status,updated_at) VALUES (?,'READY',?)", (issue, now))
            item = self._task(db, issue)
            event = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            if event is not None and event["issue"] != issue:
                raise StateError("event_id is already bound to another issue")
            if event is not None and event["state"] == "done":
                return None
            recovery = item["status"] == "RUNNING"
            if recovery:
                if item["lease_until"] > now or item["active_event"] != event_id:
                    return None
            elif item["status"] not in {"READY", "REWORK", "FAILED"}:
                return None
            elif item["failures"] >= self.max_failures:
                db.execute("UPDATE tasks SET status='BLOCKED',updated_at=? WHERE issue=?", (now, issue))
                return None
            if event is None:
                db.execute("INSERT INTO events VALUES (?,?,?,'running',?)", (event_id, issue, "work", now))
            else:
                db.execute("UPDATE events SET state='running' WHERE event_id=?", (event_id,))
            owner = uuid.uuid4().hex
            attempt = item["attempt"] if recovery else item["attempt"] + 1
            db.execute("""UPDATE tasks SET status='RUNNING',attempt=?,active_event=?,owner=?,
                lease_until=?,updated_at=? WHERE issue=?""",
                (attempt, event_id, owner, now + lease_seconds, now, issue))
            self._audit(db, issue, "recover" if recovery else "claim", {"event_id": event_id, "attempt": attempt}, now)
            return self._task(db, issue)

    def heartbeat(self, issue: str | int, owner: str, *, now: float | None = None,
                  lease_seconds: float = 300) -> dict[str, Any]:
        issue, now = self._key(issue, "issue"), self._now(now)
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._transaction() as db:
            self._owned(self._task(db, issue), owner, now)
            db.execute("UPDATE tasks SET lease_until=?,updated_at=? WHERE issue=?", (now + lease_seconds, now, issue))
            return self._task(db, issue)

    def checkpoint(self, issue: str | int, owner: str, result: dict[str, Any], *,
                   now: float | None = None) -> dict[str, Any]:
        """Persist resumable progress (for example commit SHA and PR number)."""
        issue, now = self._key(issue, "issue"), self._now(now)
        if not isinstance(result, dict):
            raise ValueError("result must be an object")
        with self._transaction() as db:
            item = self._task(db, issue)
            self._owned(item, owner, now)
            merged = item["result"] | result
            db.execute("UPDATE tasks SET result=?,updated_at=? WHERE issue=?", (json.dumps(merged), now, issue))
            return self._task(db, issue)

    def finish(self, issue: str | int, owner: str, status: str, result: dict[str, Any] | None = None,
               *, now: float | None = None) -> dict[str, Any]:
        issue, now = self._key(issue, "issue"), self._now(now)
        if status not in {"REVIEW", "FAILED", "BLOCKED"}:
            raise InvalidTransition("worker may finish only as REVIEW, FAILED or BLOCKED")
        if result is not None and not isinstance(result, dict):
            raise ValueError("result must be an object")
        with self._transaction() as db:
            item = self._task(db, issue)
            self._owned(item, owner, now)
            merged = item["result"] | (result or {})
            head = merged.get("result_commit_sha") or merged.get("head_sha") or item["head_sha"]
            if status == "REVIEW" and (not isinstance(head, str) or not head.strip()):
                raise InvalidTransition("REVIEW requires result_commit_sha or head_sha")
            failures = item["failures"] + (status == "FAILED")
            actual_status = "BLOCKED" if failures >= self.max_failures else status
            if actual_status == "BLOCKED" and status == "FAILED":
                merged["blocked_reason"] = "failure limit reached"
            db.execute("""UPDATE tasks SET status=?,failures=?,head_sha=?,result=?,owner=NULL,
                lease_until=0,active_event=NULL,updated_at=? WHERE issue=?""",
                (actual_status, failures, head, json.dumps(merged), now, issue))
            db.execute("UPDATE events SET state='done' WHERE event_id=?", (item["active_event"],))
            self._audit(db, issue, "finish", {"status": actual_status, "head_sha": head}, now)
            return self._task(db, issue)

    def request_rework(self, issue: str | int, event_id: str, head_sha: str, *,
                       now: float | None = None) -> dict[str, Any]:
        issue, event_id = self._key(issue, "issue"), self._key(event_id, "event_id")
        now = self._now(now)
        with self._transaction() as db:
            item = self._task(db, issue)
            event = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            if event is not None:
                if event["issue"] != issue or event["kind"] != "rework":
                    raise StateError("event_id already used for another action")
                return item
            if item["status"] not in {"REVIEW", "FAILED"}:
                raise InvalidTransition("rework requires REVIEW or FAILED")
            if not head_sha or head_sha != item["head_sha"]:
                raise InvalidTransition("rework report does not match current commit")
            blocked = item["reworks"] >= self.max_reworks or item["failures"] >= self.max_failures
            status = "BLOCKED" if blocked else "REWORK"
            result = item["result"]
            if blocked:
                result["blocked_reason"] = "automatic rework or failure limit reached"
            db.execute("UPDATE tasks SET status=?,reworks=?,result=?,updated_at=? WHERE issue=?",
                       (status, item["reworks"] + (not blocked), json.dumps(result), now, issue))
            db.execute("INSERT INTO events VALUES (?,?,?, ?,?)",
                       (event_id, issue, "rework", "done" if blocked else "pending", now))
            self._audit(db, issue, "request_rework", {"status": status, "head_sha": head_sha}, now)
            return self._task(db, issue)

    def accept(self, issue: str | int, event_id: str, head_sha: str, *,
               now: float | None = None) -> dict[str, Any]:
        issue, event_id = self._key(issue, "issue"), self._key(event_id, "event_id")
        now = self._now(now)
        with self._transaction() as db:
            item = self._task(db, issue)
            event = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            if event is not None:
                if event["issue"] != issue or event["kind"] != "accept":
                    raise StateError("event_id already used for another action")
                return item
            if item["status"] != "REVIEW" or not head_sha or item["head_sha"] != head_sha:
                raise InvalidTransition("acceptance requires REVIEW at exactly the reviewed commit")
            db.execute("UPDATE tasks SET status='ACCEPTED',updated_at=? WHERE issue=?", (now, issue))
            db.execute("INSERT INTO events VALUES (?,?,?,'done',?)", (event_id, issue, "accept", now))
            self._audit(db, issue, "accept", {"head_sha": head_sha}, now)
            return self._task(db, issue)

    def invalidate_acceptance(self, issue: str | int, head_sha: str, *,
                              now: float | None = None) -> dict[str, Any]:
        """A new PR commit invalidates acceptance until a fresh review occurs."""
        issue, now = self._key(issue, "issue"), self._now(now)
        if not isinstance(head_sha, str) or not head_sha:
            raise ValueError("head_sha must be nonempty")
        with self._transaction() as db:
            item = self._task(db, issue)
            if item["status"] not in {"REVIEW", "ACCEPTED"}:
                raise InvalidTransition("commit updates require REVIEW or ACCEPTED")
            if item["head_sha"] != head_sha:
                result = item["result"] | {"head_changed": True, "previous_head_sha": item["head_sha"]}
                db.execute("UPDATE tasks SET status='REVIEW',head_sha=?,result=?,updated_at=? WHERE issue=?",
                           (head_sha, json.dumps(result), now, issue))
                self._audit(db, issue, "head_changed", {"head_sha": head_sha}, now)
            return self._task(db, issue)

    def snapshot(self, issue: str | int) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            return self._decode(db.execute("SELECT * FROM tasks WHERE issue=?", (self._key(issue, "issue"),)).fetchone())

    def list_tasks(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            return [self._decode(row) for row in db.execute("SELECT * FROM tasks ORDER BY issue")]

    def _set_paused(self, paused: bool):
        with self._transaction() as db:
            db.execute("UPDATE controls SET value=? WHERE name='paused'", ("true" if paused else "false",))
            self._audit(db, None, "pause" if paused else "resume", {}, time.time())

    def pause(self):
        """Stop new claims. Active work may checkpoint and finish normally."""
        self._set_paused(True)

    def resume(self):
        self._set_paused(False)

    @property
    def paused(self) -> bool:
        with closing(self._connect()) as db:
            return db.execute("SELECT value FROM controls WHERE name='paused'").fetchone()[0] == "true"

    def export_snapshot(self) -> dict[str, Any]:
        """Create a JSON-safe replay record without live owner tokens."""
        with self._transaction() as db:
            tasks = [self._decode(row) for row in db.execute("SELECT * FROM tasks ORDER BY issue")]
            for task in tasks:
                task["owner"] = None
                task["lease_until"] = 0
            return {
                "version": 1,
                "tasks": tasks,
                "events": [dict(row) for row in db.execute("SELECT * FROM events ORDER BY event_id")],
                "paused": db.execute("SELECT value FROM controls WHERE name='paused'").fetchone()[0] == "true",
            }

    def import_snapshot(self, snapshot: dict[str, Any]):
        """Restore a trusted GitHub controller snapshot into an EMPTY cache only.

        The caller must authenticate provenance before invoking this method.
        RUNNING leases become expired and may be recovered by their same event.
        Local state is never overwritten by a possibly stale remote record.
        """
        if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
            raise ValueError("unsupported state snapshot")
        tasks, events = snapshot.get("tasks"), snapshot.get("events")
        if not isinstance(tasks, list) or not isinstance(events, list) or type(snapshot.get("paused")) is not bool:
            raise ValueError("malformed state snapshot")
        with self._transaction() as db:
            if db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]:
                raise StateError("restore requires an empty state cache")
            for item in tasks:
                issue = self._key(item["issue"], "issue")
                if item["status"] not in STATES or not isinstance(item.get("result"), dict):
                    raise ValueError("invalid task in snapshot")
                for key in ("attempt", "reworks", "failures"):
                    if type(item.get(key)) is not int or item[key] < 0:
                        raise ValueError("invalid counter in snapshot")
                if item["reworks"] > 3:
                    raise ValueError("snapshot exceeds rework limit")
                if item["status"] == "RUNNING" and not item.get("active_event"):
                    raise ValueError("RUNNING snapshot must identify its event")
                db.execute("""INSERT INTO tasks(issue,status,attempt,reworks,failures,active_event,
                    owner,lease_until,head_sha,result,updated_at) VALUES (?,?,?,?,?,?,NULL,0,?,?,?)""",
                    (issue, item["status"], item["attempt"], item["reworks"], item["failures"],
                     item.get("active_event"), item.get("head_sha"), json.dumps(item["result"]), item["updated_at"]))
            for event in events:
                if event.get("kind") not in {"work", "rework", "accept"} or event.get("state") not in {"pending", "running", "done"}:
                    raise ValueError("invalid event in snapshot")
                db.execute("INSERT INTO events VALUES (?,?,?,?,?)", (
                    self._key(event["event_id"], "event_id"), self._key(event["issue"], "issue"),
                    event["kind"], event["state"], float(event["created_at"])))
            inconsistent = db.execute("""SELECT t.issue FROM tasks t LEFT JOIN events e ON
                e.event_id=t.active_event WHERE t.status='RUNNING' AND
                (e.event_id IS NULL OR e.issue!=t.issue OR e.state!='running')""").fetchone()
            if inconsistent:
                raise ValueError("RUNNING event is missing or inconsistent")
            db.execute("UPDATE controls SET value=? WHERE name='paused'", ("true" if snapshot["paused"] else "false",))
            self._audit(db, None, "restore", {"tasks": len(tasks)}, time.time())
