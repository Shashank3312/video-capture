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
    Any reference photo is downscaled first if larger than
    MAX_REFERENCE_DIMENSION per side - a full-resolution poster/promo
    photo (e.g. a 3400x5100 IMDb poster) made MTCNN take many minutes
    on a single reference photo during real testing, since detector
    cost scales with image size; a smaller version detects the same
    face in seconds with no meaningful accuracy loss.
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
VGG-Face model, and yolov11m as the detector. This is CPU-only - no
GPU acceleration is possible on this machine: TensorFlow dropped
native Windows GPU support from 2.11 onwards (would need WSL2).

Detector choice came from benchmark_detectors.py, run on a real 113s
clip - run it again rather than trusting these numbers if the machine
or the footage changes. Mean/median seconds per sampled frame, and
how many of 57 sampled frames matched:

    retinaface   16.48 / 16.10    11 matches   most sensitive, far too
                                               slow for live use
    yolov11m      1.23 /  0.92     9 matches   the default
    mtcnn         1.59 /  1.41     9 matches   was the default
    yolov11n      0.95 /  0.56     8 matches   fastest usable option
    yunet         0.43 /  0.43     0 matches   found a face in only
                                               12 of 57 frames
    centerface    2.48 /  2.28     5 matches   also crashes on some
                                               frames (DeepFace bug)

Read those match counts as a band, not a ranking: yolov11s scored
below both the smaller yolov11n and the larger yolov11m, which is not
a real size/accuracy ordering, just single-video noise. What the
numbers do support is that retinaface/yolov11m/mtcnn/yolov11n all
catch the same main appearances, and yolov11m gets mtcnn's accuracy
noticeably faster.

Mean sits well above median for the yolo backends because per-frame
cost scales with how many faces are in the frame (up to 15 here) -
every detected face gets its own embedding pass.

Faces too small to identify are skipped before the embedding pass -
see DEFAULT_MIN_FACE_AREA. Measured on the same 57-frame clip, all
three paths finding exactly the same matches:

    one fused represent() call per frame      160.0s
    split detect/embed, no size filter        118.4s
    split detect/embed, skipping < 0.3%        61.9s

If this is still too slow, the levers are --interval (cost scales
linearly with sample count), --detector yolov11n, and raising
--min-face-area (but read its note before trusting a bigger number).
See .gitignore - test assets are never committed, this repo is public.
"""

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from deepface import DeepFace
from deepface.modules.verification import find_distance, find_threshold

DEFAULT_PHOTOS_DIR = Path(__file__).resolve().parent / "test_data" / "reference_photos"
DISTANCE_METRIC = "cosine"

# Face detectors like MTCNN scale in cost with image size - a
# full-resolution poster/promo photo (e.g. a 3400x5100 IMDb poster)
# can take many minutes on a single reference photo, vs. seconds at a
# smaller size, with no meaningful accuracy loss for this use case.
MAX_REFERENCE_DIMENSION = 1600

# Skip faces smaller than this percent of the frame. Two reasons, and
# the speed one is the lesser of them: a face this small in a 720p
# frame is roughly 50px across, well under the 224px the recognition
# model wants, so its embedding is unreliable and as likely to produce
# a false positive as a real hit. It also happens to cut the cost of
# exactly the frames that cost most - a 16-face crowd shot drops to
# about 7 faces worth checking.
#
# 0.3 was chosen against measured data, not picked for feel: across
# two different sets of reference photos, the smallest face that ever
# produced a real match measured 0.90% of the frame, so this leaves
# roughly 3x of margin. Don't raise it without re-measuring - an
# audience-skipping rule based on face COUNT was considered first and
# rejected, because this project's own test footage matches most
# strongly in 14- and 16-face frames (a stage/press shot, where the
# target is in the crowd).
DEFAULT_MIN_FACE_AREA = 0.3


def load_capped_image(path: Path, max_dimension: int = MAX_REFERENCE_DIMENSION) -> np.ndarray:
    img = Image.open(path).convert("RGB")
    width, height = img.size
    if max(width, height) > max_dimension:
        scale = max_dimension / max(width, height)
        img = img.resize((round(width * scale), round(height * scale)), Image.LANCZOS)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def load_reference_embeddings(photos_dir: Path, model_name: str, detector_backend: str) -> list[list[float]]:
    photo_paths = [p for p in photos_dir.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
    if not photo_paths:
        print(f"No reference photos found in {photos_dir}")
        sys.exit(1)

    embeddings = []
    for path in photo_paths:
        try:
            faces = DeepFace.represent(
                img_path=load_capped_image(path),
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


def match_frame(
    frame,
    reference_embeddings: list[list[float]],
    model_name: str,
    detector_backend: str,
    min_face_area: float,
) -> tuple[float | None, int, int]:
    """Best distance to the reference face in one frame.

    Returns (best_distance, faces_checked, faces_too_small); the
    distance is None when there was nothing worth checking. Detection
    and embedding are split on purpose - see DEFAULT_MIN_FACE_AREA.
    Shared with the Phase 2 live watcher, so both paths stay identical.
    """
    detected = DeepFace.extract_faces(
        img_path=frame,
        detector_backend=detector_backend,
        enforce_detection=False,
        color_face="bgr",
        normalize_face=False,
    )

    frame_area = frame.shape[0] * frame.shape[1]
    candidates, too_small = [], 0
    for face in detected:
        if face.get("confidence", 1) <= 0:
            continue
        area = face["facial_area"]["w"] * face["facial_area"]["h"]
        if 100.0 * area / frame_area < min_face_area:
            too_small += 1
            continue
        candidates.append(face)

    if not candidates:
        return None, 0, too_small

    # One batched call: DeepFace runs the whole list through the
    # recognition model in a single forward pass. Calling it per face
    # measured 6.1s against 2.5s on a 16-face frame.
    batch = DeepFace.represent(
        img_path=[face["face"] for face in candidates],
        model_name=model_name,
        detector_backend="skip",
        enforce_detection=False,
    )
    if len(candidates) == 1:
        batch = [batch]

    distances = [best_distance(faces[0]["embedding"], reference_embeddings) for faces in batch]
    return min(distances), len(distances), too_small


def scan_video(
    video_path: Path,
    reference_embeddings: list[list[float]],
    interval: float,
    model_name: str,
    detector_backend: str,
    min_face_area: float,
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
                distance, checked, too_small = match_frame(
                    frame, reference_embeddings, model_name, detector_backend, min_face_area
                )
            except Exception as exc:  # a bad frame shouldn't kill the whole scan
                print(f"[{format_timestamp(timestamp)}] [warn] frame processing failed: {exc} ({time.time() - _t0:.2f}s)")
                frame_idx += 1
                continue

            elapsed = time.time() - _t0
            skipped_note = f", {too_small} too small" if too_small else ""
            if distance is not None:
                is_match = distance <= threshold
                print(
                    f"[{format_timestamp(timestamp)}] {'MATCH' if is_match else 'no match'} "
                    f"(distance={distance:.3f}, threshold={threshold:.3f}, faces={checked}{skipped_note}) [{elapsed:.2f}s]"
                )
                if is_match:
                    matches.append((timestamp, distance))
            elif too_small:
                print(f"[{format_timestamp(timestamp)}] skipped - {too_small} face(s), all too small [{elapsed:.2f}s]")
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
    parser.add_argument("--detector", default="yolov11m")
    parser.add_argument(
        "--min-face-area",
        type=float,
        default=DEFAULT_MIN_FACE_AREA,
        help=(
            "skip faces smaller than this percent of the frame "
            f"(default {DEFAULT_MIN_FACE_AREA}; use 0 to check every face)"
        ),
    )
    args = parser.parse_args()

    if not args.video_path.exists():
        print(f"Video not found: {args.video_path}")
        sys.exit(1)

    reference_embeddings = load_reference_embeddings(args.photos_dir, args.model, args.detector)
    matches = scan_video(
        args.video_path,
        reference_embeddings,
        args.interval,
        args.model,
        args.detector,
        args.min_face_area,
    )
    summarize(matches, args.interval)


if __name__ == "__main__":
    main()
