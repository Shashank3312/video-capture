"""
Phase 3: find where a person's NAME is spoken in a recorded clip.

The recorded-file counterpart to watch_local_video.py, and the thing
Phase 3's "done when" is judged on: flag a real name-mention in a test
clip without excessive false positives.

Speech-to-text is done by faster-whisper, translating to English
rather than transcribing the source language, and names are matched
fuzzily - both decisions are explained, with the measurements behind
them, in name_mentions.py.

Usage:
    python listen_for_name.py <media_path> "<person name>"
        [--model small] [--fuzzy 0.82] [--task translate]
        [--show-transcript]

Any file ffmpeg can read works, video or audio - faster-whisper pulls
the audio track itself, so a .mp4 can be passed directly.

Needs PYTHONUTF8=1 set: transcripts routinely contain characters the
default Windows console encoding can't print.
"""

import argparse
import sys
import time
from pathlib import Path

from name_mentions import (
    DEFAULT_FUZZY_THRESHOLD,
    DEFAULT_MODEL_SIZE,
    load_model,
    scan_segments,
    transcribe,
)


def format_timestamp(seconds: float) -> str:
    minutes = int(seconds // 60)
    secs = seconds - minutes * 60
    return f"{minutes:02d}:{secs:05.2f}"


def main():
    parser = argparse.ArgumentParser(description="Phase 3 - find spoken name mentions in a recording")
    parser.add_argument("media_path", type=Path)
    parser.add_argument("name", help='the person to listen for, e.g. "Ram Charan"')
    parser.add_argument("--model", default=DEFAULT_MODEL_SIZE, help="tiny/base/small/medium (default small)")
    parser.add_argument(
        "--task",
        default="translate",
        choices=["translate", "transcribe"],
        help="translate to English (default, and much faster on Telugu) or keep the source language",
    )
    parser.add_argument("--fuzzy", type=float, default=DEFAULT_FUZZY_THRESHOLD)
    parser.add_argument("--show-transcript", action="store_true", help="print every line, not just the hits")
    args = parser.parse_args()

    if not args.media_path.exists():
        print(f"File not found: {args.media_path}")
        sys.exit(1)

    print(f"Loading the {args.model} model (downloads on first use)...")
    t0 = time.time()
    model = load_model(args.model)
    print(f"ready in {time.time() - t0:.0f}s\n")

    print(f"Listening for: {args.name!r}")
    t0 = time.time()
    try:
        segments, info = transcribe(str(args.media_path), model, task=args.task)
    except Exception as exc:
        print(f"Couldn't read audio from that file: {exc}")
        sys.exit(1)
    elapsed = time.time() - t0

    audio_seconds = segments[-1].end if segments else 0.0
    speed = f"{audio_seconds / elapsed:.1f}x real time" if elapsed else "n/a"
    print(f"Language: {info.language} (confidence {info.language_probability:.2f})")
    print(f"Transcribed {audio_seconds:.0f}s of audio in {elapsed:.0f}s - {speed}\n")

    if args.show_transcript:
        for seg in segments:
            print(f"[{format_timestamp(seg.start)}] {seg.text.strip()}")
        print()

    mentions = scan_segments(segments, args.name, args.fuzzy)

    print("--- Name mentions ---")
    if not mentions:
        print(f"{args.name!r} was never mentioned.")
        print("If you expected it, try --model medium, or lower --fuzzy a little.")
        return

    for m in mentions:
        how = "exact" if m.score >= 1.0 else f"close match on {m.matched!r}, {m.score:.2f}"
        print(f"[{format_timestamp(m.start)}] {how}")
        print(f"            {m.text}")
    print(f"\n{len(mentions)} mention(s) of {args.name!r}.")


if __name__ == "__main__":
    main()
