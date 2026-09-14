"""
Phase 2: Track B on a real live stream.

Watches a live YouTube stream and alerts the moment the person in the
reference photos appears, so nobody has to sit through the whole
event waiting for them.

The matching itself is Phase 1's, imported rather than copied
(match_frame in watch_local_video.py), so both paths can't drift
apart. What's new here is everything around it:

  - Resolving a YouTube Live link to a playable stream URL with
    yt-dlp. No separate ffmpeg binary is needed: the opencv-python
    wheel bundles FFmpeg, so cv2.VideoCapture opens the HLS URL
    directly.

  - Staying at the LIVE EDGE. A live stream keeps producing frames
    whether or not we're ready, and the capture holds the ones we
    haven't read. Read them in order and the watcher drifts further
    behind real time with every frame, eventually alerting about a
    moment that passed minutes ago - which defeats the whole point.
    So a reader thread continuously drains the capture and keeps only
    the newest frame; the matching loop always picks up from there and
    lets everything in between go. Sampling is paced by how long
    matching actually takes, not by a fixed timer.

  - Alerting once per appearance, not once per matching frame - the
    same debounce idea as Track A. An appearance ends only after
    several consecutive non-matching frames, so one bad angle mid-shot
    doesn't split it into two alerts.

  - Failing loudly. A link that isn't live, a stream that ends, a
    stream that drops mid-watch: say so and exit, rather than hanging
    or dying silently at 3am.

Usage:
    python watch_live_stream.py <youtube_url> [--photos-dir DIR]
        [--model MODEL_NAME] [--detector BACKEND]
        [--min-face-area PERCENT] [--max-minutes N]

Needs PYTHONUTF8=1 set, like everything else DeepFace touches here.
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Silence FFmpeg's "Cannot reuse HTTP connection for different host"
# chatter. YouTube serves live segments from rotating CDN hosts, so
# this fires constantly and buries the actual output - hundreds of
# lines in a five minute run. Must be set before cv2 loads its FFmpeg
# plugin, hence before the import below.
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")

import cv2
from deepface.modules.verification import find_threshold

from name_mentions import DEFAULT_MODEL_SIZE
from watch_local_video import (
    DEFAULT_MIN_FACE_AREA,
    DEFAULT_PHOTOS_DIR,
    DISTANCE_METRIC,
    load_reference_embeddings,
    match_frame,
)

# How many consecutive non-matching frames end an appearance. One
# missed frame is usually just a turned head mid-shot, and re-alerting
# for that would be exactly the notification spam this project exists
# to avoid.
MISSES_TO_END_APPEARANCE = 3

# Consecutive failed reads before we call the stream dead. A live
# stream hiccups; a handful in a row means it's actually gone.
READ_FAILURES_TO_GIVE_UP = 30

# Set by --json. The Phase 4 worker (app/worker.py) runs this script as
# a subprocess and reads events off stdout, so it needs structured
# output; a person at a terminal needs prose. One switch, one emit()
# below, rather than two copies of every message.
EMIT_JSON = False


def emit(event: str, human: str | None = None, **data):
    if EMIT_JSON:
        print(json.dumps({"event": event, **data}), flush=True)
    elif human is not None:
        print(human, flush=True)


class LiveFrameReader:
    """Keeps only the newest frame from a live stream.

    The whole point is to throw frames away. Matching is slower than
    real time on a busy frame, and every frame we don't read stays
    queued in the capture - so reading in order means falling further
    behind the live edge with no way to catch up.
    """

    def __init__(self, stream_url: str):
        self._cap = cv2.VideoCapture(stream_url)
        if not self._cap.isOpened():
            raise RuntimeError("Couldn't open the stream URL (the stream may have ended or be geo-blocked).")
        self._lock = threading.Lock()
        self._frame = None
        self._frame_id = 0
        self._stopped = threading.Event()
        self.failed = None
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self):
        consecutive_failures = 0
        while not self._stopped.is_set():
            ok, frame = self._cap.read()
            if not ok:
                consecutive_failures += 1
                if consecutive_failures >= READ_FAILURES_TO_GIVE_UP:
                    self.failed = "stream ended or dropped"
                    break
                time.sleep(0.1)
                continue
            consecutive_failures = 0
            with self._lock:
                self._frame = frame
                self._frame_id += 1
        self._cap.release()

    def latest(self):
        """Newest frame and its id, or (None, id) if none has arrived."""
        with self._lock:
            if self._frame is None:
                return None, self._frame_id
            return self._frame.copy(), self._frame_id

    def stop(self):
        self._stopped.set()
        self._thread.join(timeout=2.0)


class NameListener:
    """Listens for the person's NAME being spoken, on its own thread.

    Separate from the frame loop on purpose. Transcribing a window of
    audio takes several seconds, and doing that inline would freeze
    face matching for the duration - the video signal is the primary
    one and must stay responsive.

    Audio is consumed in fixed windows rather than continuously: STT
    needs a few seconds of context to be any good, and per-word
    streaming would cost far more for worse text.
    """

    def __init__(self, audio_url: str, name: str, model_size: str, window_seconds: float):
        self.name = name
        self.window_seconds = window_seconds
        self._audio_url = audio_url
        self._model_size = model_size
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._pending: list = []          # Mentions found but not yet reported
        self.failed = None
        self.windows_done = 0
        self.heard_words = 0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        try:
            import av
            import numpy as np

            from name_mentions import load_model, scan_segments

            model = load_model(self._model_size)
            container = av.open(self._audio_url)
            stream = container.streams.audio[0]
            # Whisper wants 16kHz mono float32; the stream is neither.
            resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)

            buffer = []
            buffered_samples = 0
            target_samples = int(16000 * self.window_seconds)

            for frame in container.decode(stream):
                if self._stopped.is_set():
                    break
                for resampled in resampler.resample(frame):
                    chunk = resampled.to_ndarray().flatten().astype("float32")
                    buffer.append(chunk)
                    buffered_samples += len(chunk)

                if buffered_samples < target_samples:
                    continue

                audio = np.concatenate(buffer)
                buffer, buffered_samples = [], 0

                segments, _ = model.transcribe(audio, task="translate", beam_size=5)
                segments = list(segments)
                mentions = scan_segments(segments, self.name)
                with self._lock:
                    self.windows_done += 1
                    self.heard_words += sum(len(s.text.split()) for s in segments)
                    if mentions:
                        self._pending.extend(mentions)
        except Exception as exc:
            self.failed = f"{type(exc).__name__}: {exc}"

    def drain(self) -> list:
        """Hand back any mentions found since the last call."""
        with self._lock:
            found, self._pending = self._pending, []
        return found

    def stop(self):
        self._stopped.set()


def resolve_stream_url(
    youtube_url: str,
    browser: str | None = None,
    cookie_file: Path | None = None,
) -> tuple[str, str | None, str]:
    """Resolve a YouTube link to a directly playable stream URL."""
    try:
        import yt_dlp
    except ModuleNotFoundError:
        print("yt-dlp isn't installed. Install it with:")
        print("    .venv\\Scripts\\python.exe -m pip install yt-dlp")
        sys.exit(1)

    # Ask for VIDEO ONLY. YouTube serves live streams as separate
    # video-only and audio-only renditions with no combined one, so
    # yt-dlp's "best" - which means best stream carrying both - matches
    # nothing at all and fails with "Requested format is not
    # available". Video-only also keeps the frame path lean; the audio
    # rendition is picked out separately below, and only when
    # --listen-for asked for it. Capped at 720p because a face gets
    # cropped and resized to 224px anyway, so a larger rendition costs
    # decode time for no accuracy.
    options = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestvideo[height<=720]/bestvideo/best",
    }
    # YouTube gates a lot of streams behind an anti-bot check that only
    # a signed-in session gets past, so yt-dlp has to borrow cookies
    # from a browser or a cookies.txt export. Nothing is sent anywhere
    # except to YouTube, and nothing is stored by this script.
    if browser:
        options["cookiesfrombrowser"] = (browser,)
    if cookie_file:
        options["cookiefile"] = str(cookie_file)

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
    except Exception as exc:
        message = str(exc)
        print(f"Couldn't read that link: {message}")
        if "cookie" in message.lower():
            print(
                "\nThat's a cookie problem, not a problem with the link.\n"
                "  - 'Could not copy ... cookie database' means the browser is "
                "still running and holding the file open. Close it completely "
                "(check the task manager - it often lingers in the background) "
                "and try again.\n"
                "  - If it still fails, Chrome and Edge encrypt their cookie "
                "store on Windows in a way yt-dlp frequently can't read at all. "
                "Export a cookies.txt with a 'Get cookies.txt' browser extension "
                "and pass --cookies path\\to\\cookies.txt instead. That path "
                "always works and doesn't care which browser you use."
            )
        elif "not a bot" in message or "Sign in to confirm" in message:
            print(
                "\nYouTube is asking this request to prove it's not a bot, which "
                "only a signed-in session can do. Re-run with the browser you're "
                "signed into YouTube on, e.g.:\n"
                "    --browser firefox        (or chrome / edge / brave)\n"
                "or pass --cookies path\\to\\cookies.txt from a 'Get cookies.txt' "
                "browser extension."
            )
        else:
            print("Check the URL is a real, public, currently-live YouTube stream.")
        sys.exit(1)

    if not info.get("is_live"):
        print(f"'{info.get('title', youtube_url)}' is not currently live.")
        print("This watcher is for live streams. Use watch_local_video.py for a recorded file.")
        sys.exit(1)

    stream_url = info.get("url")
    if not stream_url:
        print("yt-dlp resolved the page but gave no playable stream URL.")
        sys.exit(1)

    # The audio rendition comes out of the same metadata rather than a
    # second network round trip: info["formats"] holds every rendition,
    # and the audio-only ones are those with no video codec.
    # An audio rendition is one with no VIDEO codec. Don't also demand
    # that acodec be set: YouTube's HLS audio formats report acodec as
    # None rather than a codec name, so requiring it excluded exactly
    # the formats being looked for.
    audio_url = None
    audio_only = [
        f for f in info.get("formats", [])
        if f.get("vcodec") in (None, "none") and f.get("acodec") != "none" and f.get("url")
    ]
    if audio_only:
        audio_url = audio_only[-1]["url"]  # last is the highest quality yt-dlp listed

    return stream_url, audio_url, info.get("title", "(untitled stream)")


def watch(
    youtube_url: str,
    reference_embeddings: list[list[float]],
    model_name: str,
    detector_backend: str,
    min_face_area: float,
    max_minutes: float | None,
    browser: str | None = None,
    cookie_file: Path | None = None,
    listen_for: str | None = None,
    audio_model: str = "small",
    audio_window: float = 30.0,
    audio_only: bool = False,
    alert_mode: str = "cooldown",
    cooldown_minutes: float = 5.0,
):
    stream_url, audio_url, title = resolve_stream_url(youtube_url, browser, cookie_file)
    emit("started", f"Watching: {title}", title=title, url=youtube_url)

    listener = None
    if listen_for:
        if not audio_url:
            emit("warning", f"[warn] no audio rendition available, can't listen for {listen_for!r}.",
                 message=f"no audio rendition available for {listen_for!r}")
            if audio_only:
                emit("finished", "Nothing left to watch with, stopping.",
                     outcome="failed", reason="the stream has no audio track to listen to")
                sys.exit(1)
        else:
            also = "" if audio_only else "Also "
            how_often = {
                "once": "alerting once, then stopping",
                "cooldown": f"alerting at most once every {cooldown_minutes:g} min",
                "every": "alerting on every mention",
            }[alert_mode]
            emit("listening",
                 f"{also}listening for {listen_for!r} (every {audio_window:g}s of audio, {how_often})",
                 listen_for=listen_for, window=audio_window, alert_mode=alert_mode)
            listener = NameListener(audio_url, listen_for, audio_model, audio_window)
            listener.start()
    if not EMIT_JSON:
        print()

    # In audio-only mode nothing decodes video at all - no frames, no
    # face detection, and no reference photos were ever needed.
    reader = None
    if not audio_only:
        try:
            reader = LiveFrameReader(stream_url)
        except RuntimeError as exc:
            emit("finished", str(exc), outcome="failed", reason=str(exc))
            sys.exit(1)

    threshold = find_threshold(model_name, DISTANCE_METRIC)
    started = time.time()
    appearing = False
    misses = 0
    appearance_started_at = None
    alerts = 0
    name_alerts = 0
    last_name_alert_at = None
    windows_reported = 0
    last_frame_id = -1
    done = False

    outcome, reason = None, None
    try:
        while True:
            if reader and reader.failed:
                emit("stream_stopped", f"\nStream stopped: {reader.failed}.", reason=reader.failed)
                reason = reader.failed
                break
            if max_minutes is not None and (time.time() - started) / 60 >= max_minutes:
                emit("time_limit", f"\nReached the {max_minutes:g} minute limit, stopping.",
                     minutes=max_minutes)
                reason = f"the {max_minutes:g} minute limit ran out"
                break

            # Name mentions are a weaker signal than a face, so they're
            # reported plainly and never start or end an appearance -
            # the debounce state belongs to the video alone.
            if listener:
                for mention in listener.drain():
                    now = time.time()
                    if alert_mode == "cooldown" and last_name_alert_at is not None:
                        if now - last_name_alert_at < cooldown_minutes * 60:
                            continue
                    name_alerts += 1
                    last_name_alert_at = now
                    how = "" if mention.score >= 1.0 else f" (heard as {mention.matched!r})"
                    emit("alert", f"\n*** HEARD IT{how}: \"{mention.text}\" ***\n",
                         kind="name", heard=mention.text, matched=mention.matched,
                         score=round(mention.score, 3), listen_for=listen_for)
                    if alert_mode == "once":
                        # Told you once, so the job is done - same as
                        # Track A's watcher, which exits on its alert
                        # rather than carrying on.
                        emit("task_completed", "Task completed.")
                        done = True
                        break
                if done:
                    outcome, reason = "matched", f"heard {listen_for!r}"
                    break

                # Say something as each window is transcribed. Silence
                # for minutes on end is indistinguishable from a hang,
                # and in audio-only mode there's no frame output at all.
                if listener.windows_done > windows_reported:
                    windows_reported = listener.windows_done
                    clock = datetime.now().strftime("%H:%M:%S")
                    heard_seconds = windows_reported * audio_window
                    emit("progress",
                         f"[{clock}] listened to {heard_seconds:.0f}s of audio "
                         f"({listener.heard_words} words), no {listen_for!r} yet"
                         if not name_alerts
                         else f"[{clock}] listened to {heard_seconds:.0f}s of audio",
                         audio_seconds=heard_seconds, words=listener.heard_words)

                if listener.failed:
                    emit("warning", f"[warn] listening stopped: {listener.failed}",
                         message=listener.failed)
                    listener = None

            if reader is None:
                # Audio-only: nothing to do here but let the listener
                # thread work and report what it finds.
                if listener is None:
                    emit("stream_stopped", "Nothing left listening, stopping.",
                         reason="the audio listener stopped")
                    reason = "the audio listener stopped"
                    break
                time.sleep(0.5)
                continue

            frame, frame_id = reader.latest()
            if frame is None or frame_id == last_frame_id:
                time.sleep(0.2)  # nothing new decoded yet
                continue
            last_frame_id = frame_id

            clock = datetime.now().strftime("%H:%M:%S")
            t0 = time.time()
            try:
                distance, checked, too_small = match_frame(
                    frame, reference_embeddings, model_name, detector_backend, min_face_area
                )
            except Exception as exc:  # one bad frame shouldn't end the watch
                emit("warning", f"[{clock}] [warn] frame failed: {exc}", message=str(exc))
                continue
            elapsed = time.time() - t0

            is_match = distance is not None and distance <= threshold
            if distance is None:
                detail = f"{too_small} too small" if too_small else "no face"
                emit("frame", f"[{clock}] {detail} [{elapsed:.1f}s]",
                     match=False, faces=0, too_small=too_small, seconds=round(elapsed, 2))
            else:
                emit("frame",
                     f"[{clock}] {'MATCH' if is_match else 'no match'} "
                     f"(distance={distance:.3f}, faces={checked}) [{elapsed:.1f}s]",
                     match=is_match, distance=round(distance, 3), faces=checked,
                     seconds=round(elapsed, 2))

            if is_match:
                misses = 0
                if not appearing:
                    appearing = True
                    appearance_started_at = time.time()
                    alerts += 1
                    emit("alert", f"\n*** ALERT: they're on screen now - {clock} ***\n",
                         kind="face", at=clock, distance=round(distance, 3))
                    if alert_mode == "once":
                        emit("task_completed", "Task completed.")
                        outcome, reason = "matched", "they appeared on screen"
                        break
            elif appearing:
                misses += 1
                if misses >= MISSES_TO_END_APPEARANCE:
                    seconds = time.time() - appearance_started_at
                    emit("appearance_end",
                         f"[{clock}] appearance ended (about {seconds:.0f}s on screen)\n",
                         seconds=round(seconds))
                    appearing = False
                    misses = 0
    except KeyboardInterrupt:
        emit("stopped", "\nStopped.")
        reason = "stopped by hand"
    finally:
        if reader:
            reader.stop()
        if listener:
            listener.stop()

    watched = (time.time() - started) / 60
    if audio_only:
        summary = f"Listened for {watched:.1f} minutes, heard it {name_alerts} time(s)"
    else:
        summary = f"Watched {watched:.1f} minutes, alerted {alerts} time(s) on the face"
        if listen_for:
            summary += f", {name_alerts} time(s) on what was said"

    # Every job resolves as one of three things, and says which. Going
    # quiet is the one outcome the plan rules out - if the person never
    # turned up, that is itself the answer the user is waiting for.
    if outcome is None:
        if alerts or name_alerts:
            outcome = "matched"
        elif reason and "limit ran out" in reason:
            outcome = "expired"
            reason = "the time limit ran out and they never appeared"
        else:
            outcome = "failed"
            reason = reason or "the watch ended unexpectedly"

    emit("finished", summary + ".", outcome=outcome, reason=reason,
         face_alerts=alerts, name_alerts=name_alerts, minutes=round(watched, 1))
    return outcome


def main():
    parser = argparse.ArgumentParser(description="Phase 2 - Track B live stream watcher")
    parser.add_argument("youtube_url")
    parser.add_argument("--photos-dir", type=Path, default=DEFAULT_PHOTOS_DIR)
    parser.add_argument("--model", default="VGG-Face")
    parser.add_argument("--detector", default="yolov11m")
    parser.add_argument("--min-face-area", type=float, default=DEFAULT_MIN_FACE_AREA)
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=None,
        help="stop after this many minutes (default: watch until the stream ends)",
    )
    parser.add_argument(
        "--browser",
        default=None,
        help="browser to borrow YouTube cookies from (chrome, firefox, edge, brave) "
        "- needed when YouTube demands a signed-in session",
    )
    parser.add_argument(
        "--cookies",
        type=Path,
        default=None,
        help="path to a cookies.txt export, as an alternative to --browser",
    )
    parser.add_argument(
        "--listen-for",
        default=None,
        metavar="TEXT",
        help='also alert when this is said - a name or any phrase, e.g. --listen-for "Ram Charan"',
    )
    parser.add_argument(
        "--audio-only",
        action="store_true",
        help="listen only, don't watch: no face matching and no reference photos needed. "
        "Requires --listen-for",
    )
    parser.add_argument(
        "--audio-model",
        default=DEFAULT_MODEL_SIZE,
        help=f"whisper model for --listen-for (default {DEFAULT_MODEL_SIZE}; tiny and base mangle names)",
    )
    parser.add_argument(
        "--audio-window",
        type=float,
        default=30.0,
        help="seconds of audio transcribed at a time (default 30)",
    )
    parser.add_argument(
        "--alert-mode",
        default="cooldown",
        choices=["once", "cooldown", "every"],
        help="what to do when there's a hit: 'once' alerts and then exits (the job is done, "
        "same as Track A), 'cooldown' (default) waits --cooldown-minutes before it can alert "
        "again, 'every' reports every single hit",
    )
    parser.add_argument(
        "--cooldown-minutes",
        type=float,
        default=5.0,
        help="minutes of silence between alerts when --alert-mode cooldown (default 5)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit one JSON event per line instead of prose - how the Phase 4 worker reads this",
    )
    args = parser.parse_args()

    global EMIT_JSON
    EMIT_JSON = args.json

    # Check this before the slow reference-photo pass, so a typo in the
    # path doesn't cost a minute of model loading to find out about.
    if args.cookies and not args.cookies.is_file():
        if args.cookies.is_dir():
            print(f"That's a folder, not a file: {args.cookies}")
            print("--cookies needs the exported file itself, e.g. ...\\Downloads\\cookies.txt")
        else:
            print(f"No cookies file at: {args.cookies}")
            print(
                "Point --cookies at the file your browser extension actually saved "
                "(usually in Downloads, often named cookies.txt or "
                "youtube.com_cookies.txt - note Windows may have added a second "
                ".txt). The path in the docs is only an example, not a real "
                "location."
            )
        sys.exit(1)

    if args.audio_only and not args.listen_for:
        print("--audio-only needs --listen-for: there'd be nothing to listen for otherwise.")
        print('e.g. --audio-only --listen-for "Ram Charan"')
        sys.exit(1)

    # Audio-only skips the reference photos entirely - no face is being
    # matched, so requiring photos would be asking for something that
    # is never used.
    reference_embeddings = []
    if not args.audio_only:
        reference_embeddings = load_reference_embeddings(args.photos_dir, args.model, args.detector)

    watch(
        args.youtube_url,
        reference_embeddings,
        args.model,
        args.detector,
        args.min_face_area,
        args.max_minutes,
        args.browser,
        args.cookies,
        args.listen_for,
        args.audio_model,
        args.audio_window,
        args.audio_only,
        args.alert_mode,
        args.cooldown_minutes,
    )


if __name__ == "__main__":
    main()
