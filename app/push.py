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

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_app = None
_import_error = None


def _service_account_path() -> Path | None:
    """Find the Firebase service account key, if there is one.

    Env var first so it can live outside the repo entirely, then the
    conventional filenames Firebase hands out - all of which are
    gitignored, because this key can send notifications as the project.
    """
    from_env = os.environ.get("FIREBASE_CREDENTIALS")
    if from_env and Path(from_env).is_file():
        return Path(from_env)
    for pattern in ("*firebase-adminsdk*.json", "*serviceAccount*.json", "*service-account*.json"):
        for candidate in sorted(PROJECT_ROOT.glob(pattern)):
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


def send(tokens: list[str], title: str, body: str, link: str | None = None) -> tuple[int, str | None]:
    """Notify every registered phone. Returns (sent_count, error)."""
    if not tokens:
        return 0, "no phone has registered for notifications yet"
    problem = configure()
    if problem:
        return 0, problem

    from firebase_admin import messaging

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
