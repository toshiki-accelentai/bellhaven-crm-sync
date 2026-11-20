"""SQLite persistence for proposals, decisions and the API audit log."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

from .models import APPLIED, KINDS, NEEDS_REAPPROVAL, PENDING, REJECTED

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_key TEXT NOT NULL,
  fingerprint TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  risk INTEGER NOT NULL,
  status TEXT NOT NULL,
  body TEXT NOT NULL,           -- full proposal JSON
  reason TEXT DEFAULT '',
  result TEXT,
  recurrence INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  decided_at TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL, proposal_id INTEGER, method TEXT, url TEXT,
  request TEXT, status INTEGER, response TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL, locations INTEGER, inserted INTEGER, suppressed INTEGER, warnings TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def fingerprint(p: dict) -> str:
    """Identity of a proposal = issue + the data it was based on + what it would write.

    Free-text notes are excluded: rewording an explanation must not resurrect a decided proposal.
    """
    def strip(d):
        return {k: v for k, v in d.items() if k != "note"}
    before = {aid: strip(f) for aid, f in p["before"].items()}
    steps = [{**s, "fields": strip(s["fields"])} for s in p["steps"]]
    raw = json.dumps([p["issue_key"], before, steps], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def claim(conn: sqlite3.Connection, pid: int, statuses: tuple[str, ...]) -> bool:
    """Atomically move a proposal to 'applying' so only one executor can write it."""
    cur = conn.execute(f"UPDATE proposals SET status='applying' WHERE id=? AND status IN "
                       f"({','.join('?' * len(statuses))})", (pid, *statuses))
    conn.commit()
    return cur.rowcount == 1


def upsert_proposals(conn: sqlite3.Connection, proposals: list[dict]) -> tuple[int, int]:
    """Insert new proposals; skip any whose fingerprint was seen before (decided or pending).

    Returns (inserted, suppressed). An issue_key that was applied before and shows up again with
    a new fingerprint is flagged as a recurrence.
    """
    inserted = suppressed = 0
    for p in proposals:
        fp = fingerprint(p)
        seen = conn.execute("SELECT id, status, body FROM proposals WHERE fingerprint=?", (fp,)).fetchone()
        if seen:
            if seen["status"] in (PENDING, NEEDS_REAPPROVAL):  # undecided: refresh evidence/notes in place
                body = {**json.loads(seen["body"]), **p, "fingerprint": fp}
                conn.execute("UPDATE proposals SET body=?, status=? WHERE id=?",
                             (json.dumps(body), PENDING, seen["id"]))
            suppressed += 1
            continue
        # A newer version of the same issue supersedes an older still-pending one.
        conn.execute("UPDATE proposals SET status='superseded', decided_at=? WHERE issue_key=? AND status=?",
                     (now(), p["issue_key"], PENDING))
        recurrence = bool(conn.execute(
            "SELECT 1 FROM proposals WHERE issue_key=? AND status=?", (p["issue_key"], APPLIED)).fetchone())
        body = {**p, "fingerprint": fp, "recurrence": recurrence}
        conn.execute(
            "INSERT INTO proposals (issue_key, fingerprint, kind, risk, status, body, recurrence, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (p["issue_key"], fp, p["kind"], KINDS.index(p["kind"]), PENDING, json.dumps(body),
             int(recurrence), now()))
        inserted += 1
    conn.commit()
    return inserted, suppressed


def _row(r: sqlite3.Row) -> dict:
    d = json.loads(r["body"])
    d.update(id=r["id"], status=r["status"], reason=r["reason"] or "",
             result=json.loads(r["result"]) if r["result"] else None,
             created_at=r["created_at"], decided_at=r["decided_at"])
    return d


def list_proposals(conn: sqlite3.Connection, statuses: tuple[str, ...] | None = None) -> list[dict]:
    q = "SELECT * FROM proposals"
    args: tuple = ()
    if statuses:
        q += f" WHERE status IN ({','.join('?' * len(statuses))})"
        args = statuses
    q += " ORDER BY risk, id"
    return [_row(r) for r in conn.execute(q, args)]


def get(conn: sqlite3.Connection, pid: int) -> dict | None:
    r = conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    return _row(r) if r else None


def set_status(conn: sqlite3.Connection, pid: int, status: str, reason: str = "", result=None) -> None:
    conn.execute("UPDATE proposals SET status=?, reason=?, result=?, decided_at=? WHERE id=?",
                 (status, reason, json.dumps(result) if result is not None else None, now(), pid))
    conn.commit()


def save_progress(conn: sqlite3.Connection, pid: int, result: dict) -> None:
    """Persist partial apply progress (e.g. CHOW new id) without changing status."""
    conn.execute("UPDATE proposals SET result=? WHERE id=?", (json.dumps(result), pid))
    conn.commit()


def reject(conn: sqlite3.Connection, pid: int, reason: str = "") -> None:
    set_status(conn, pid, REJECTED, reason=reason.strip()[:500])


def audit(conn: sqlite3.Connection, proposal_id, method, url, request, status, response) -> None:
    conn.execute("INSERT INTO audit (at, proposal_id, method, url, request, status, response) VALUES (?,?,?,?,?,?,?)",
                 (now(), proposal_id, method, url, json.dumps(request), status, json.dumps(response)[:4000]))
    conn.commit()


def log_run(conn, locations, inserted, suppressed, warnings) -> None:
    conn.execute("INSERT INTO runs (at, locations, inserted, suppressed, warnings) VALUES (?,?,?,?,?)",
                 (now(), locations, inserted, suppressed, json.dumps(warnings)))
    conn.commit()


def last_location_count(conn) -> int | None:
    r = conn.execute("SELECT locations FROM runs WHERE locations > 0 ORDER BY id DESC LIMIT 1").fetchone()
    return r["locations"] if r else None
