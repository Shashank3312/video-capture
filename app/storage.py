"""
Where users, devices and watch jobs live (PostgreSQL, see db.py).

Phase 4 kept jobs in SQLite because it was one user and a file beat a
server. Phase 5 has accounts, so every job and device token now belongs
to a user, and reads that come from the API are filtered by owner.

A job is the unit of work the whole app is built around: "watch this
event for this person and tell me the moment they show up". It always
ends in one of the three outcomes the plan calls for, and never just
goes quiet:

    MATCHED  - found them, notification sent
    EXPIRED  - the time limit ran out and they never appeared
    FAILED   - something broke: bad link, stream died, API down
"""

import uuid
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from .db import connection

PENDING = "pending"
RUNNING = "running"
MATCHED = "matched"
EXPIRED = "expired"
FAILED = "failed"
FINISHED_STATES = (MATCHED, EXPIRED, FAILED)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_user(user_id: str, email: str, display_name: str | None) -> None:
    """Create the user on first sight; later calls refresh email/name."""
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO users (id, email, display_name, created_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE
                SET email = EXCLUDED.email, display_name = EXCLUDED.display_name
            """,
            (user_id, email, display_name, _now()),
        )


def create_job(user_id: str, kind: str, params: dict, person_id: str | None = None) -> str:
    job_id = uuid.uuid4().hex[:12]
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, user_id, person_id, kind, status, params, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (job_id, user_id, person_id, kind, PENDING, Jsonb(params), _now()),
        )
    return job_id


def update_job(job_id: str, **fields) -> None:
    if not fields:
        return
    if fields.get("status") in FINISHED_STATES:
        fields.setdefault("finished_at", _now())
    # Dicts and lists go in as JSONB; callers pass real objects, never
    # pre-dumped JSON strings (those would be stored as a JSON string).
    values = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
    assignments = ", ".join(f"{key} = %s" for key in fields)
    with connection() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id = %s", (*values, job_id))


def get_job(job_id: str, user_id: str | None = None) -> dict | None:
    """user_id=None is the worker's view (no owner filter). With a
    user_id, someone else's job is indistinguishable from a missing one."""
    query = "SELECT * FROM jobs WHERE id = %s"
    args: tuple = (job_id,)
    if user_id is not None:
        query += " AND user_id = %s"
        args = (job_id, user_id)
    with connection() as conn:
        return conn.execute(query, args).fetchone()


def list_jobs(user_id: str, limit: int = 50) -> list[dict]:
    with connection() as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
        ).fetchall()


def add_alert(job_id: str, entry: dict) -> None:
    """Append one alert, with its time, to the job's history.

    Atomic in the database rather than read-modify-write in Python, so
    two alerts landing together can't overwrite each other."""
    with connection() as conn:
        conn.execute(
            "UPDATE jobs SET alerts_log = alerts_log || %s::jsonb WHERE id = %s",
            (Jsonb([entry]), job_id),
        )


def save_device(token: str, user_id: str) -> None:
    """Remember a phone so jobs know where to send notifications.

    Upsert on the token: a shared phone or a sign-out/sign-in as someone
    else moves the token to the latest user instead of notifying both."""
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO devices (token, user_id, created_at) VALUES (%s, %s, %s)
            ON CONFLICT (token) DO UPDATE SET user_id = EXCLUDED.user_id
            """,
            (token, user_id, _now()),
        )


def list_device_tokens(user_id: str) -> list[str]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT token FROM devices WHERE user_id = %s", (user_id,)
        ).fetchall()
    return [row["token"] for row in rows]
