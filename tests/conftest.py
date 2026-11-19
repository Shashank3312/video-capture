"""
Test harness. Tests run against TEST_DATABASE_URL, which is wiped
between tests - so this must never fall back to the real DATABASE_URL.
Point DATABASE_URL at the test database before any `app` import,
because app.db reads it on first use.
"""

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

if not os.environ.get("TEST_DATABASE_URL"):
    raise RuntimeError(
        "TEST_DATABASE_URL is not set (put it in .env). Refusing to run: "
        "tests truncate every table and must not touch the real database."
    )
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

from app import db, storage  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    db.init_db()
    with db.connection() as conn:
        conn.execute("TRUNCATE users, devices, people, person_photos, jobs CASCADE")


@pytest.fixture
def two_users():
    storage.upsert_user("uid-a", "a@example.com", None)
    storage.upsert_user("uid-b", "b@example.com", None)
    return "uid-a", "uid-b"
