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
import subprocess
import sys
import threading
from pathlib import Path

from . import push, storage

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRACK_B = PROJECT_ROOT / "track_b_video"
TRACK_A = PROJECT_ROOT / "track_a_sports"
PYTHON = sys.executable

_processes: dict[str, subprocess.Popen] = {}
_processes_lock = threading.Lock()


def build_command(job: dict) -> list[str]:
    """Turn a stored job into the command line that runs it."""
    params = job["params"]
    kind = job["kind"]

    if kind == "sports":
        return [
            PYTHON, str(TRACK_A / "watch.py"),
            str(params["match_id"]), params["player"],
            "--interval", str(params.get("poll_seconds", 60)),
        ]

    command = [
        PYTHON, str(TRACK_B / "watch_live_stream.py"), params["url"],
        "--json", "--alert-mode", params.get("alert_mode", "once"),
    ]
    if params.get("max_minutes"):
        command += ["--max-minutes", str(params["max_minutes"])]
    if params.get("cookies"):
        command += ["--cookies", params["cookies"]]
    if params.get("listen_for"):
        command += ["--listen-for", params["listen_for"]]
    if kind == "audio":
        command += ["--audio-only"]
    if params.get("photos_dir"):
        command += ["--photos-dir", params["photos_dir"]]
    return command


def _notify(job: dict, event: dict):
    """Turn an alert into the notification the user is waiting for."""
    params = job["params"]
    title = job["event_title"] or params.get("url") or "Your event"
    if event.get("kind") == "name":
        body = f"Heard {params.get('listen_for')!r}: {event.get('heard', '')[:120]}"
    else:
        body = "They're on screen now - tap to watch."

    sent, error = push.send(
        storage.list_device_tokens(),
        title=f"Don't Miss The Moment: {title[:60]}",
        body=body,
        link=params.get("url"),
    )
    if error:
        # Not fatal: the job still found what it was watching for, and
        # that stays recorded even if the phone never hears about it.
        storage.update_job(job["id"], progress=f"alert found, but no notification sent ({error})")


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

    push.send(
        storage.list_device_tokens(),
        title=f"{heading}: {title[:50]}",
        body=body,
        # Land on the app rather than the stream: there's nothing to
        # see on the stream, the useful next step is rescheduling.
        link="/",
    )


def _handle_event(job: dict, event: dict):
    kind = event.get("event")
    if kind == "started":
        storage.update_job(job["id"], event_title=event.get("title"), status=storage.RUNNING)
        job["event_title"] = event.get("title")
    elif kind == "alert":
        storage.update_job(job["id"], matched_at=event.get("at") or "", progress="found it")
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

    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
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
                progress=f"the watcher stopped unexpectedly (exit code {process.returncode})",
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
