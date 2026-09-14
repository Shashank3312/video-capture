"""
Phase 4: where watch jobs live.

SQLite on purpose - this is the single-user MVP, so a file next to the
code beats running a database server. Phase 5 moves to PostgreSQL when
there are real accounts (see implementation_plan.txt).

A job is the unit of work the whole app is built around: "watch this
event for this person and tell me the moment they show up". It always
ends in one of the three outcomes the plan calls for, and never just
goes quiet:

    MATCHED  - found them, notification sent
    EXPIRED  - the time limit ran out and they never appeared
    FAILED   - something broke: bad link, stream died, API down
"""

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "jobs.db"

PENDING = "pending"
RUNNING = "running"
MATCHED = "matched"
EXPIRED = "expired"
FAILED = "failed"
FINISHED_STATES = (MATCHED, EXPIRED, FAILED)

# SQLite allows one writer at a time, and jobs run on their own threads
# writing progress as they go, so serialise writes rather than letting
# them collide.
_write_lock = threading.Lock()


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id           TEXT PRIMARY KEY,
                kind         TEXT NOT NULL,
                status       TEXT NOT NULL,
                params       TEXT NOT NULL,
                event_title  TEXT,
                link         TEXT,
                outcome      TEXT,
                progress     TEXT,
                matched_at   TEXT,
                created_at   TEXT NOT NULL,
                finished_at  TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                token      TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )
            """
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_job(kind: str, params: dict) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _write_lock, _connect() as conn:
        conn.execute(
            "INSERT INTO jobs (id, kind, status, params, created_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, kind, PENDING, json.dumps(params), _now()),
        )
    return job_id


def update_job(job_id: str, **fields):
    if not fields:
        return
    if fields.get("status") in FINISHED_STATES:
        fields.setdefault("finished_at", _now())
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with _write_lock, _connect() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*fields.values(), job_id))


def get_job(job_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _as_dict(row) if row else None


def list_jobs(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_as_dict(row) for row in rows]


def _as_dict(row: sqlite3.Row) -> dict:
    job = dict(row)
    job["params"] = json.loads(job["params"])
    return job


def save_device(token: str):
    """Remember a phone so jobs know where to send notifications."""
    with _write_lock, _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO devices (token, created_at) VALUES (?, ?)",
            (token, _now()),
        )


def list_device_tokens() -> list[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT token FROM devices").fetchall()
    return [row["token"] for row in rows]
