"""
Phase 5: the PostgreSQL connection pool and schema.

SQLite was fine for one user, but accounts mean several people's jobs
writing at once from worker threads, and SQLite's one-writer-at-a-time
rule had already forced a global lock around every write. PostgreSQL
removes that, and gives per-user rows real foreign keys.

The pool is created on first use, not at import: DATABASE_URL has to be
readable after `.env` loads, and tests point it at a throwaway database
before the first query. Importing this module must never connect.
"""

import os
import threading
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> ConnectionPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            # No override: a variable already set (the tests') beats .env.
            load_dotenv(PROJECT_ROOT / ".env")
            url = os.environ.get("DATABASE_URL")
            if not url:
                raise RuntimeError("DATABASE_URL is not set (put it in .env)")
            _pool = ConnectionPool(
                url,
                min_size=1,
                max_size=10,
                kwargs={"row_factory": dict_row},
                open=True,
            )
        return _pool


@contextmanager
def connection():
    """A pooled connection; commits on clean exit, rolls back on error."""
    with _get_pool().connection() as conn:
        yield conn


def init_db() -> None:
    with connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id           TEXT PRIMARY KEY,
                email        TEXT,
                display_name TEXT,
                created_at   TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                token      TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS people (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL REFERENCES users(id),
                name        TEXT NOT NULL,
                listen_for  TEXT,
                player_name TEXT,
                created_at  TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS person_photos (
                id         TEXT PRIMARY KEY,
                person_id  TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
                filename   TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL REFERENCES users(id),
                person_id   TEXT REFERENCES people(id) ON DELETE SET NULL,
                kind        TEXT NOT NULL,
                status      TEXT NOT NULL,
                params      JSONB NOT NULL,
                event_title TEXT,
                link        TEXT,
                outcome     TEXT,
                progress    TEXT,
                matched_at  TEXT,
                created_at  TEXT NOT NULL,
                finished_at TEXT,
                notified    TEXT,
                alerts_log  JSONB NOT NULL DEFAULT '[]'
            )
            """
        )
