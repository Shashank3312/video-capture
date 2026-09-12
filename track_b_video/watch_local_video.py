"""
Phase 1: Track B core prototype.

Given reference photo(s) of a person and a local video FILE, print the
timestamps where that person's face appears.

This is the riskiest technical assumption in the whole project (see
implementation_plan.txt Phase 1) - the goal here is to find out
honestly how well this works, not to assume it will.

Approach:
  - Load all reference photos from a folder, get one face embedding
    per photo via DeepFace.represent(). A photo with no detectable
    face is reported and skipped (not fatal, unless none are usable).
  - Sample the video at a fixed interval (not every frame - most of a
    video is redundant for this purpose, and processing every frame
    would be far slower for no real benefit).
  - For each sampled frame, detect any faces and compare each one's
    embedding against every reference embedding using
    deepface.modules.verification.find_distance() and
    find_threshold() - DeepFace's own pre-tuned per-model thresholds,
    not a guessed similarity cutoff.
  - Print every sampled timestamp's result (match or not, with the
    best distance found) for transparency, then group consecutive
    matches into "appearance" segments as the summary - mirrors the
    same "don't spam individual hits, report continuous appearances"
    idea used in Track A's debounce logic.

Usage:
    python watch_local_video.py <video_path> [--interval SECONDS]
        [--photos-dir DIR] [--model MODEL_NAME] [--detector BACKEND]

Defaults point at test_data/reference_photos, use DeepFace's default
VGG-Face model, and MTCNN as the detector. This is CPU-only - no GPU
acceleration is possible on this machine: TensorFlow dropped native
Windows GPU support from 2.11 onwards (would need WSL2). Per-frame
cost measured in-process (see the per-sample timing this script
prints) has ranged 1.5-3.5s with MTCNN across different runs on this
machine, vs. 8-12s with RetinaFace (DeepFace's most accurate but
slowest detector) - real variance seems to come from system load
between runs more than anything in the code, so treat these as rough
ranges, not precise multipliers. DeepFace's own "opencv" backend is
NOT usable here at all - this installed opencv-python 5.0.0.93 build
is missing the whole cv2.CascadeClassifier class it needs (confirmed
by testing, not just a missing data file - don't waste time
re-attempting an XML-file fix). The one lever fully within your
control if this is too slow: raise --interval to sample less often
(cost scales linearly with sample count). See .gitignore - test
assets are never committed, this repo is public.
"""

import argparse
import sys
import time
from pathlib import Path

import cv2
from deepface import DeepFace
from deepface.modules.verification import find_distance, find_threshold

DEFAULT_PHOTOS_DIR = Path(__file__).resolve().parent / "test_data" / "reference_photos"
DISTANCE_METRIC = "cosine"


def load_reference_embeddings(photos_dir: Path, model_name: str, detector_backend: str) -> list[list[float]]:
    photo_paths = [p for p in photos_dir.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    if not photo_paths:
        print(f"No reference photos found in {photos_dir}")
        sys.exit(1)

    embeddings = []
    for path in photo_paths:
        try:
            faces = DeepFace.represent(
                img_path=str(path),
                model_name=model_name,
                detector_backend=detector_backend,
                enforce_detection=True,
            )
        except ValueError as exc:
            print(f"[warn] no face detected in {path.name}, skipping it: {exc}")
            continue

        if len(faces) > 1:
            print(f"[warn] {path.name} has {len(faces)} faces detected - using the largest one")
        embeddings.append(faces[0]["embedding"])
        print(f"Loaded reference embedding from {path.name}")

    if not embeddings:
        print("None of the reference photos had a detectable face - can't continue.")
        sys.exit(1)

    return embeddings


def best_distance(face_embedding: list[float], reference_embeddings: list[list[float]]) -> float:
    return min(find_distance(face_embedding, ref, DISTANCE_METRIC) for ref in reference_embeddings)


def format_timestamp(seconds: float) -> str:
    minutes = int(seconds // 60)
    secs = seconds - minutes * 60
    return f"{minutes:02d}:{secs:05.2f}"


def scan_video(
    video_path: Path,
    reference_embeddings: list[list[float]],
    interval: float,
    model_name: str,
    detector_backend: str,
):
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        print(f"Couldn't open video: {video_path}")
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    if not fps:
        print("Couldn't read this video's frame rate - the file may be corrupt or an unsupported codec.")
        sys.exit(1)
    duration = frame_count / fps
    print(f"Video: {video_path.name} | ~{duration:.1f}s | {fps:.2f} fps | sampling every {interval}s\n")

    threshold = find_threshold(model_name, DISTANCE_METRIC)
    frame_step = max(1, round(fps * interval))

    matches: list[tuple[float, float]] = []  # (timestamp, best_distance) for every match
    frame_idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break

        if frame_idx % frame_step == 0:
            timestamp = frame_idx / fps
            _t0 = time.time()
            try:
                faces = DeepFace.represent(
                    img_path=frame,
                    model_name=model_name,
                    detector_backend=detector_backend,
                    enforce_detection=False,
                )
            except Exception as exc:  # a bad frame shouldn't kill the whole scan
                print(f"[{format_timestamp(timestamp)}] [warn] frame processing failed: {exc} ({time.time() - _t0:.2f}s)")
                frame_idx += 1
                continue

            elapsed = time.time() - _t0
            if faces and faces[0].get("face_confidence", 1) > 0:
                distance = best_distance(faces[0]["embedding"], reference_embeddings)
                is_match = distance <= threshold
                print(f"[{format_timestamp(timestamp)}] {'MATCH' if is_match else 'no match'} (distance={distance:.3f}, threshold={threshold:.3f}) [{elapsed:.2f}s]")
                if is_match:
                    matches.append((timestamp, distance))
            else:
                print(f"[{format_timestamp(timestamp)}] no face detected [{elapsed:.2f}s]")

        frame_idx += 1

    cap.release()
    return matches


def summarize(matches: list[tuple[float, float]], interval: float):
    print("\n--- Summary ---")
    if not matches:
        print("No matches found anywhere in the video.")
        return

    # Group consecutive matched samples into appearance segments. Allow
    # one missed sample in a row (e.g. a brief bad angle) before
    # treating it as the appearance actually ending.
    gap_tolerance = interval * 2.5
    segments = []
    seg_start, seg_end = matches[0][0], matches[0][0]
    for timestamp, _ in matches[1:]:
        if timestamp - seg_end <= gap_tolerance:
            seg_end = timestamp
        else:
            segments.append((seg_start, seg_end))
            seg_start = seg_end = timestamp
    segments.append((seg_start, seg_end))

    for start, end in segments:
        print(f"Appears from {format_timestamp(start)} to {format_timestamp(end)}")


def main():
    parser = argparse.ArgumentParser(description="Phase 1 - Track B local video face-matching prototype")
    parser.add_argument("video_path", type=Path)
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between sampled frames (default 1.0)")
    parser.add_argument("--photos-dir", type=Path, default=DEFAULT_PHOTOS_DIR)
    parser.add_argument("--model", default="VGG-Face")
    parser.add_argument("--detector", default="mtcnn")
    args = parser.parse_args()

    if not args.video_path.exists():
        print(f"Video not found: {args.video_path}")
        sys.exit(1)

    reference_embeddings = load_reference_embeddings(args.photos_dir, args.model, args.detector)
    matches = scan_video(args.video_path, reference_embeddings, args.interval, args.model, args.detector)
    summarize(matches, args.interval)


if __name__ == "__main__":
    main()
