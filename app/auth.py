"""
Phase 5: turning a Firebase sign-in into a user.

The browser signs in with Firebase and sends the resulting ID token on
every request; this module verifies it and decides who is allowed in.
Whether a user may touch a particular job is NOT decided here - that is
the storage layer's per-user scoping, which answers 404 rather than 403
so one user can't even learn another's job ids exist.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Header, HTTPException
from firebase_admin import auth as firebase_auth

from app import push, storage

# So this works standalone (worker.py loads it too); no override, so a
# real environment variable still wins over the file.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


@dataclass(frozen=True)
class User:
    id: str
    email: str
    is_owner: bool


def email_allowed(email: str, setting: str | None) -> bool:
    """Is this email covered by an allow-list setting?

    Fails closed: unset or blank allows NOBODY. Forgetting a setting
    must never be what opens the app to strangers; opening it up takes
    the explicit "*".
    """
    if setting is None or not setting.strip():
        return False
    entries = [e.strip().lower() for e in setting.split(",") if e.strip()]
    return "*" in entries or email.strip().lower() in entries


def check_access_settings() -> str | None:
    """A startup warning when the allow-list is empty, else None."""
    if not (os.environ.get("ALLOWED_EMAILS") or "").strip():
        return "nobody can sign in until ALLOWED_EMAILS is set in .env"
    return None


def current_user(authorization: str | None = Header(None)) -> User:
    """FastAPI dependency: verify the Bearer ID token, return the user."""
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "sign in first")

    # A server that can't verify anyone is a misconfiguration, not the
    # user's fault - telling them to sign in again would loop forever.
    problem = push.configure()
    if problem:
        raise HTTPException(503, problem)

    try:
        claims = firebase_auth.verify_id_token(token)
    except Exception:
        # Expired, revoked, malformed, wrong project: to the caller they
        # are all the same thing - sign in again.
        raise HTTPException(401, "sign in first")

    email = claims.get("email") or ""
    if not claims.get("email_verified") or not email_allowed(
        email, os.environ.get("ALLOWED_EMAILS")
    ):
        raise HTTPException(403, "this app is invite-only for now")

    storage.upsert_user(claims["uid"], email, claims.get("name"))
    return User(
        claims["uid"], email, email_allowed(email, os.environ.get("OWNER_EMAILS"))
    )
