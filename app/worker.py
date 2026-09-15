"""
Phase 4: running a watch job and turning it into a notification.

Jobs run as SUBPROCESSES of the existing watchers, not as threads in
the API server, for three practical reasons:

  - The watchers load TensorFlow and Whisper models worth hundreds of
    megabytes. A process exit frees that reliably; a thread in a
    long-lived server does not.
  - A crash deep in native code takes down the job, not the server.
  - Cancelling is just terminating the process. Interrupting a thread
    mid-inference is not really possible.

The watchers emit one JSON event per line under --json, so nothing
here has to parse prose. Events map to job state, and an alert becomes
the push notification the user is actually waiting for.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv

from . import push, storage

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRACK_B = PROJECT_ROOT / "track_b_video"
TRACK_A = PROJECT_ROOT / "track_a_sports"
PYTHON = sys.executable

load_dotenv(PROJECT_ROOT / ".env")


def default_cookies() -> str | None:
    """YouTube cookies to fall back on when a job doesn't carry any.

    A job started from the phone has no way to supply a cookies file,
    and YouTube's bot check is inconsistent rather than absent - it
    let test jobs through and will refuse a later one for no visible
    reason. Falling back to the local export means a phone-started job
    doesn't fail at 3am on the night it mattered.
    """
    configured = os.environ.get("YT_COOKIES")
    if configured and Path(configured).is_file():
        return configured
    local = PROJECT_ROOT / "cookies.txt"
    return str(local) if local.is_file() else None

_processes: dict[str, subprocess.Popen] = {}
_processes_lock = threading.Lock()

LOGS_DIR = Path(__file__).resolve().parent / "logs"

# Noise the ML stack prints on every run, which would otherwise be
# reported to the user as the reason their job failed.
_NOISE = ("oneDNN", "cpu_feature_guard", "TF_ENABLE", "absl::", "WARNING", "warnings.warn",
          "Cannot reuse", "lz4", "I/O operation", "Exception ignored", "local_rendezvous",
          "huggingface", "symlink", "Developer Mode", "To enable")


def _last_error(log_path: Path) -> str:
    """The most useful line from a crashed watcher's stderr."""
    try:
        lines = [ln.strip() for ln in log_path.read_text(encoding="utf-8", errors="replace").splitlines()]
    except OSError:
        return "no error output was captured"
    real = [ln for ln in lines if ln and not any(n in ln for n in _NOISE)]
    if not real:
        return "no error output was captured"
    # The exception line is the last one, and the most informative.
    return real[-1][:300]


def build_command(job: dict) -> list[str]:
    """Turn a stored job into the command line that runs it."""
    params = job["params"]
    kind = job["kind"]

    if kind == "sports":
        command = [
            PYTHON, str(TRACK_A / "watch.py"),
            str(params["match_id"]), params["player"],
            "--json",
            "--interval", str(params.get("poll_seconds", 60)),
        ]
        if params.get("max_minutes"):
            command += ["--max-minutes", str(params["max_minutes"])]
        return command

    command = [
        PYTHON, str(TRACK_B / "watch_live_stream.py"), params["url"],
        "--json", "--alert-mode", params.get("alert_mode", "once"),
    ]
    if params.get("max_minutes"):
        command += ["--max-minutes", str(params["max_minutes"])]
    cookies = params.get("cookies") or default_cookies()
    if cookies:
        command += ["--cookies", cookies]
    if params.get("listen_for"):
        command += ["--listen-for", params["listen_for"]]
    if kind == "audio":
        command += ["--audio-only"]
    if params.get("photos_dir"):
        command += ["--photos-dir", params["photos_dir"]]
    return command


def _scoreboard_url(params: dict) -> str | None:
    """Cricbuzz's live scoreboard for a sports job.

    The bare match id resolves fine (no slug needed - confirmed by
    request, not assumed), which matters because watch.py never fetches
    the slug and this must not depend on it doing so.
    """
    match_id = params.get("match_id")
    return f"https://www.cricbuzz.com/live-cricket-scores/{match_id}" if match_id else None


def _notify(job: dict, event: dict):
    """Turn an alert into the notification the user is waiting for."""
    params = job["params"]
    title = job["event_title"] or params.get("url") or "Your event"
    where = event.get("stream_position")
    if event.get("kind") == "name":
        body = f"Heard {params.get('listen_for')!r}: {event.get('heard', '')[:120]}"
    elif event.get("kind") == "sport":
        body = f"{event.get('heard', 'They are on now')} - tap to watch the scoreboard."
    else:
        body = "They're on screen now - tap to watch."
    if where:
        body += f" ({where} into the stream)"

    sent, error = push.send(
        storage.list_device_tokens(),
        title=f"Don't Miss The Moment: {title[:60]}",
        body=body,
        # Prefer the link that seeks to the moment itself. A
        # notification gets read minutes later, by which point the live
        # edge has moved on and is no longer where the thing happened.
        # Sports jobs have neither a seek_url nor a url param - they
        # carry match_id/player instead - so without the scoreboard
        # fallback a real cricket alert shipped with NO tap-through at
        # all, silently failing the one thing the plan requires: "a
        # tap-through link back to the live stream or scoreboard".
        link=event.get("seek_url") or params.get("url") or _scoreboard_url(params),
    )
    # Record delivery in its own field: the job finishing will overwrite
    # progress, and "did they actually get told" must not be the thing
    # that gets lost. A failure here isn't fatal - the job still found
    # what it was watching for.
    storage.update_job(
        job["id"],
        notified=f"sent to {sent} device(s)" if not error else f"NOT sent: {error}",
    )


def _notify_outcome(job: dict, status: str, reason: str):
    """Tell the user a job ended without finding anything."""
    params = job["params"]
    title = job["event_title"] or params.get("url") or "Your event"
    looked_for = params.get("listen_for") or params.get("player") or "them"

    if status == storage.EXPIRED:
        heading = "Time's up - they never showed"
        body = f"Watched for {looked_for} and the time ran out. Tap to watch again."
    else:
        heading = "The watch stopped"
        body = f"{reason}. Tap to try again."

    sent, error = push.send(
        storage.list_device_tokens(),
        title=f"{heading}: {title[:50]}",
        body=body,
        # Land on the app rather than the stream: there's nothing to
        # see on the stream, the useful next step is rescheduling.
        link=push.app_link("/"),
    )
    storage.update_job(
        job["id"],
        notified=f"sent to {sent} device(s)" if not error else f"NOT sent: {error}",
    )


def _handle_event(job: dict, event: dict):
    kind = event.get("event")
    if kind == "started":
        storage.update_job(job["id"], event_title=event.get("title"), status=storage.RUNNING)
        job["event_title"] = event.get("title")
    elif kind == "alert":
        at = event.get("at") or ""
        heard = event.get("heard")
        storage.add_alert(
            job["id"],
            {
                "at": at,
                "kind": event.get("kind", "face"),
                # For a name, the sentence it was heard in is the
                # useful detail - it's what tells you whether the hit
                # was really about your person.
                "detail": heard[:140] if heard else "on screen",
                # Where in the stream, which survives long after the
                # wall-clock time stops meaning anything to anyone.
                "position": event.get("stream_position"),
                "seek_url": event.get("seek_url"),
                # Keep how confident it was. Without this, a solid
                # match and a borderline one that squeaked under the
                # threshold look identical afterwards, and a report of
                # "it matched the wrong person" can't be investigated.
                "score": event.get("distance", event.get("score")),
            },
        )
        storage.update_job(job["id"], matched_at=at, progress=f"found them at {at}" if at else "found them")
        _notify(job, event)
    elif kind == "progress":
        storage.update_job(job["id"], progress=f"listened to {event.get('audio_seconds', 0):.0f}s of audio")
    elif kind == "frame" and event.get("faces"):
        storage.update_job(job["id"], progress=f"watching - {event['faces']} face(s) in view")
    elif kind == "finished":
        outcome = event.get("outcome", "failed")
        status = {
            "matched": storage.MATCHED,
            "expired": storage.EXPIRED,
        }.get(outcome, storage.FAILED)
        reason = event.get("reason") or outcome
        storage.update_job(job["id"], status=status, outcome=outcome, progress=reason)

        # A job that ends WITHOUT finding anything still has to say so.
        # Silence is indistinguishable from the watcher having crashed,
        # and "they never turned up" is itself the answer the user has
        # been waiting on - the plan is explicit that every job surfaces
        # its outcome.
        if status in (storage.EXPIRED, storage.FAILED):
            _notify_outcome(job, status, reason)


def run_job(job_id: str):
    """Run one job to completion. Blocks, so call it on a thread."""
    job = storage.get_job(job_id)
    if job is None:
        return

    try:
        command = build_command(job)
    except KeyError as exc:
        storage.update_job(job_id, status=storage.FAILED, outcome="failed",
                           progress=f"the job is missing {exc}")
        return

    storage.update_job(job_id, status=storage.RUNNING, progress="starting up")
    env = {**dict(__import__("os").environ), "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}

    # Keep stderr rather than discarding it. A crash in the watcher
    # surfaced only as "exit code 1" with the reason thrown away, which
    # made a real bug (numpy bool breaking the JSON encoder) invisible
    # from the app - the traceback only appeared by running the command
    # again by hand.
    log_path = LOGS_DIR / f"{job_id}.log"
    LOGS_DIR.mkdir(exist_ok=True)
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=log_path.open("w", encoding="utf-8"),
            text=True, encoding="utf-8", errors="replace", env=env,
            cwd=str(TRACK_B),
        )
    except Exception as exc:
        storage.update_job(job_id, status=storage.FAILED, outcome="failed",
                           progress=f"couldn't start the watcher: {exc}")
        return

    with _processes_lock:
        _processes[job_id] = process

    saw_finish = False
    try:
        for line in process.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue  # model-loading chatter from TensorFlow etc.
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "finished":
                saw_finish = True
            _handle_event(job, event)
    finally:
        process.wait()
        with _processes_lock:
            _processes.pop(job_id, None)

    # A watcher that dies without a "finished" event still has to
    # resolve to something - going quiet is the one outcome the plan
    # rules out.
    if not saw_finish:
        current = storage.get_job(job_id)
        if current and current["status"] not in storage.FINISHED_STATES:
            storage.update_job(
                job_id, status=storage.FAILED, outcome="failed",
                progress=f"the watcher stopped unexpectedly: {_last_error(log_path)}",
            )


def start_job(job_id: str):
    threading.Thread(target=run_job, args=(job_id,), daemon=True).start()


def cancel_job(job_id: str) -> bool:
    with _processes_lock:
        process = _processes.get(job_id)
    if process is None:
        return False
    process.terminate()
    storage.update_job(job_id, status=storage.FAILED, outcome="cancelled", progress="cancelled")
    return True
