import pytest
from fastapi import HTTPException

from app import auth, db


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    # The real .env holds real allow-lists; never let them leak into assertions.
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)
    monkeypatch.delenv("OWNER_EMAILS", raising=False)
    monkeypatch.setattr(auth.push, "configure", lambda: None)


def _verify_returns(monkeypatch, claims):
    monkeypatch.setattr(auth.firebase_auth, "verify_id_token", lambda token: claims)


@pytest.mark.parametrize("setting,email,expected", [
    (None, "a@x.com", False), ("", "a@x.com", False), ("  ", "a@x.com", False),
    ("*", "anyone@x.com", True),
    ("a@x.com, B@x.com", "b@x.com", True), ("a@x.com", "c@x.com", False),
])
def test_email_allowed_fails_closed(setting, email, expected):
    assert auth.email_allowed(email, setting) is expected


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer", "Bearer "])
def test_missing_or_garbled_header_is_401(header):
    with pytest.raises(HTTPException) as exc:
        auth.current_user(header)
    assert exc.value.status_code == 401
    assert exc.value.detail == "sign in first"


def test_expired_token_is_401(monkeypatch):
    def boom(token):
        raise auth.firebase_auth.ExpiredIdTokenError("expired", cause=None)
    monkeypatch.setattr(auth.firebase_auth, "verify_id_token", boom)
    with pytest.raises(HTTPException) as exc:
        auth.current_user("Bearer old")
    assert exc.value.status_code == 401


def test_unconfigured_firebase_is_503_not_401(monkeypatch):
    monkeypatch.setattr(auth.push, "configure", lambda: "no key")
    with pytest.raises(HTTPException) as exc:
        auth.current_user("Bearer x")
    assert exc.value.status_code == 503
    assert exc.value.detail == "no key"


def test_unverified_email_is_403(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "*")
    _verify_returns(monkeypatch, {"uid": "u1", "email": "a@x.com", "email_verified": False})
    with pytest.raises(HTTPException) as exc:
        auth.current_user("Bearer good")
    assert exc.value.status_code == 403
    assert exc.value.detail == "this app is invite-only for now"


def test_not_on_allow_list_is_403(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "b@x.com")
    _verify_returns(monkeypatch, {"uid": "u1", "email": "a@x.com", "email_verified": True})
    with pytest.raises(HTTPException) as exc:
        auth.current_user("Bearer good")
    assert exc.value.status_code == 403


def test_unset_allow_list_locks_everyone_out(monkeypatch):
    _verify_returns(monkeypatch, {"uid": "u1", "email": "a@x.com", "email_verified": True})
    with pytest.raises(HTTPException) as exc:
        auth.current_user("Bearer good")
    assert exc.value.status_code == 403


def test_allowed_user_is_created_and_owner_flag_set(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "a@x.com,b@x.com")
    monkeypatch.setenv("OWNER_EMAILS", "a@x.com")
    _verify_returns(monkeypatch, {"uid": "u1", "email": "a@x.com", "email_verified": True, "name": "A"})
    assert auth.current_user("Bearer good") == auth.User("u1", "a@x.com", True)
    with db.connection() as conn:
        row = conn.execute("SELECT email FROM users WHERE id=%s", ("u1",)).fetchone()
    assert row is not None


def test_non_owner_flag_false(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "a@x.com,b@x.com")
    monkeypatch.setenv("OWNER_EMAILS", "a@x.com")
    _verify_returns(monkeypatch, {"uid": "u2", "email": "b@x.com", "email_verified": True})
    assert auth.current_user("Bearer good").is_owner is False


def test_check_access_settings(monkeypatch):
    assert "ALLOWED_EMAILS" in auth.check_access_settings()
    monkeypatch.setenv("ALLOWED_EMAILS", "a@x.com")
    assert auth.check_access_settings() is None
