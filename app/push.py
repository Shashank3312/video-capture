"""
Phase 4: sending the actual notification to a phone.

Firebase Cloud Messaging, per implementation_plan.txt Phase 4. The
plan is specific about content, and it matters: a bare "they're on!"
is useless. Every notification carries what event it is, when, and a
tap-through link straight back to the stream - the tap-through IS the
product, since the whole point is to get the user watching the moment
it happens.

Configuration is deliberately optional. Everything else in the app
works without Firebase set up - jobs still run and still resolve - so
a missing credential degrades to "no push sent" rather than a crash.
"""

import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_app = None
_import_error = None


def _looks_like_service_account(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return data.get("type") == "service_account" and "private_key" in data


def _service_account_path() -> Path | None:
    """Find the Firebase service account key, if there is one.

    Env var first, so the key can live outside the repo entirely.
    Otherwise identify it by CONTENT rather than filename: Firebase
    named the real download "firebase admin SDK.json", which matched
    none of the obvious patterns. Every service account key says so
    inside, so read that instead of guessing what it's called.
    """
    from_env = os.environ.get("FIREBASE_CREDENTIALS")
    if from_env and Path(from_env).is_file():
        return Path(from_env)
    for candidate in sorted(PROJECT_ROOT.glob("*.json")):
        if _looks_like_service_account(candidate):
            return candidate
    return None


def configure() -> str | None:
    """Set up Firebase. Returns a reason string if it couldn't be."""
    global _app, _import_error
    if _app is not None:
        return None

    path = _service_account_path()
    if path is None:
        return (
            "no Firebase service account key found - drop the JSON Firebase gave you "
            "into the project folder, or set FIREBASE_CREDENTIALS to its path"
        )
    try:
        import firebase_admin
        from firebase_admin import credentials
    except ModuleNotFoundError as exc:
        _import_error = str(exc)
        return f"firebase-admin isn't installed: {exc}"

    try:
        _app = firebase_admin.initialize_app(credentials.Certificate(str(path)))
    except Exception as exc:
        return f"couldn't load {path.name}: {exc}"
    return None


def is_configured() -> bool:
    return _app is not None


_public_base_url: str | None = None


def remember_base_url(url: str):
    """Note the address the app is actually reachable at.

    Needed because a notification's tap-through link must be an
    absolute HTTPS url, and the app can't know its own public address -
    it sits behind a tunnel whose hostname changes on every restart.
    Whatever address a browser just reached us on is that address.
    """
    global _public_base_url
    if url.startswith("https://"):
        _public_base_url = url.rstrip("/")


def app_link(path: str = "/") -> str | None:
    return f"{_public_base_url}{path}" if _public_base_url else None


def send(tokens: list[str], title: str, body: str, link: str | None = None) -> tuple[int, str | None]:
    """Notify every registered phone. Returns (sent_count, error)."""
    if not tokens:
        return 0, "no phone has registered for notifications yet"
    problem = configure()
    if problem:
        return 0, problem

    from firebase_admin import messaging

    # Firebase rejects anything that isn't an absolute HTTPS url, and
    # rejects the whole send with it - so a bad link must not be able
    # to cost us the notification itself.
    if link and not link.startswith("https://"):
        link = app_link("/")

    # The link is what makes the notification actionable: tapping it
    # should land on the live event, not just open the app.
    webpush = messaging.WebpushConfig(
        fcm_options=messaging.WebpushFCMOptions(link=link) if link else None,
        notification=messaging.WebpushNotification(title=title, body=body, icon="/icon.png"),
    )
    message = messaging.MulticastMessage(
        tokens=tokens,
        notification=messaging.Notification(title=title, body=body),
        webpush=webpush,
        data={"link": link or ""},
    )
    try:
        response = messaging.send_each_for_multicast(message)
    except Exception as exc:
        return 0, f"Firebase rejected the send: {exc}"
    return response.success_count, None
