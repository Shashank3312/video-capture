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
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
from deepface.modules.verification import find_threshold

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


def resolve_stream_url(
    youtube_url: str,
    browser: str | None = None,
    cookie_file: Path | None = None,
) -> tuple[str, str]:
    """Resolve a YouTube link to a directly playable stream URL."""
    try:
        import yt_dlp
    except ModuleNotFoundError:
        print("yt-dlp isn't installed. Install it with:")
        print("    .venv\\Scripts\\python.exe -m pip install yt-dlp")
        sys.exit(1)

    # Prefer a mid-size rendition: 1080p costs more to decode for no
    # accuracy gain once faces are cropped and resized to 224px anyway.
    options = {
        "quiet": True,
        "no_warnings": True,
        "format": "best[height<=720]/best",
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
    return stream_url, info.get("title", "(untitled stream)")


def watch(
    youtube_url: str,
    reference_embeddings: list[list[float]],
    model_name: str,
    detector_backend: str,
    min_face_area: float,
    max_minutes: float | None,
    browser: str | None = None,
    cookie_file: Path | None = None,
):
    stream_url, title = resolve_stream_url(youtube_url, browser, cookie_file)
    print(f"Watching: {title}\n")

    try:
        reader = LiveFrameReader(stream_url)
    except RuntimeError as exc:
        print(exc)
        sys.exit(1)

    threshold = find_threshold(model_name, DISTANCE_METRIC)
    started = time.time()
    appearing = False
    misses = 0
    appearance_started_at = None
    alerts = 0
    last_frame_id = -1

    try:
        while True:
            if reader.failed:
                print(f"\nStream stopped: {reader.failed}.")
                break
            if max_minutes is not None and (time.time() - started) / 60 >= max_minutes:
                print(f"\nReached the {max_minutes:g} minute limit, stopping.")
                break

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
                print(f"[{clock}] [warn] frame failed: {exc}")
                continue
            elapsed = time.time() - t0

            is_match = distance is not None and distance <= threshold
            if distance is None:
                detail = f"{too_small} too small" if too_small else "no face"
                print(f"[{clock}] {detail} [{elapsed:.1f}s]")
            else:
                print(
                    f"[{clock}] {'MATCH' if is_match else 'no match'} "
                    f"(distance={distance:.3f}, faces={checked}) [{elapsed:.1f}s]"
                )

            if is_match:
                misses = 0
                if not appearing:
                    appearing = True
                    appearance_started_at = time.time()
                    alerts += 1
                    print(f"\n*** ALERT: they're on screen now - {clock} ***\n")
            elif appearing:
                misses += 1
                if misses >= MISSES_TO_END_APPEARANCE:
                    seconds = time.time() - appearance_started_at
                    print(f"[{clock}] appearance ended (about {seconds:.0f}s on screen)\n")
                    appearing = False
                    misses = 0
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        reader.stop()

    watched = (time.time() - started) / 60
    print(f"Watched {watched:.1f} minutes, alerted {alerts} time(s).")


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
    args = parser.parse_args()

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
    )


if __name__ == "__main__":
    main()
