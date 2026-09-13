"""
Detector backend benchmark for Track B.

Answers with real measured numbers on THIS machine, rather than
published benchmarks from elsewhere, which DeepFace detector backend
gives the best speed/accuracy tradeoff - the decision that matters for
Phase 2, where a live stream keeps producing frames whether or not
we're ready for them.

Method:
  - Extract the sampled frames ONCE and hold them in memory, so every
    detector scores byte-identical input. (Also avoids re-seeking with
    cap.set(CAP_PROP_POS_FRAMES), which isn't frame-exact on every
    codec and burned us once already.)
  - Rebuild reference embeddings per detector, because that's how the
    real pipeline runs: the detector affects both sides of the match,
    not just the video side.
  - Time only the per-frame work, after reference loading has already
    forced the models to load - otherwise the first frame absorbs
    model-loading cost and looks artificially slow.
  - Record the best distance over ALL faces in a frame and, separately,
    the distance for only the first face returned. The gap between
    those two columns is exactly what the "we only ever check faces[0]"
    shortcut in watch_local_video.py costs in missed matches.

Usage:
    python benchmark_detectors.py <video_path> [--interval SECONDS]
        [--detectors mtcnn,retinaface,yolov11n] [--limit N]
        [--photos-dir DIR] [--model MODEL_NAME]
"""

import argparse
import statistics
import sys
import time
from pathlib import Path

import cv2
from deepface import DeepFace
from deepface.modules.verification import find_threshold

from watch_local_video import (
    DEFAULT_PHOTOS_DIR,
    DISTANCE_METRIC,
    best_distance,
    format_timestamp,
    load_capped_image,
)

# yolov8n is deliberately absent: DeepFace fetches its weights from a
# Google Drive link, which is far flakier than the GitHub release
# downloads the other YOLO variants use.
DEFAULT_DETECTORS = "mtcnn,retinaface,yolov11n,yolov11s,yolov8m,yunet,centerface"


def extract_frames(video_path: Path, interval: float, limit: int | None):
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        print(f"Couldn't open video: {video_path}")
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps:
        print("Couldn't read this video's frame rate - corrupt file or unsupported codec.")
        sys.exit(1)

    frame_step = max(1, round(fps * interval))
    frames = []
    frame_idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_idx % frame_step == 0:
            frames.append((frame_idx / fps, frame))
            if limit is not None and len(frames) >= limit:
                break
        frame_idx += 1
    cap.release()

    height, width = frames[0][1].shape[:2] if frames else (0, 0)
    print(f"Video: {video_path.name} | {width}x{height} | {fps:.2f} fps")
    print(f"Sampled {len(frames)} frames every {interval}s\n")
    return frames


def load_references(photos_dir: Path, model_name: str, detector_backend: str):
    photo_paths = [p for p in photos_dir.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
    embeddings = []
    elapsed = 0.0
    for path in photo_paths:
        t0 = time.time()
        try:
            faces = DeepFace.represent(
                img_path=load_capped_image(path),
                model_name=model_name,
                detector_backend=detector_backend,
                enforce_detection=True,
            )
        except ValueError:
            print(f"    [warn] no face detected in {path.name}, skipping")
            continue
        finally:
            elapsed += time.time() - t0
        embeddings.append(faces[0]["embedding"])
    return embeddings, elapsed


def run_detector(detector_backend: str, frames, photos_dir: Path, model_name: str):
    print(f"=== {detector_backend} ===")
    try:
        references, ref_time = load_references(photos_dir, model_name, detector_backend)
    except Exception as exc:
        print(f"    FAILED to load reference photos: {type(exc).__name__}: {exc}\n")
        return None

    if not references:
        print("    FAILED: no usable reference embeddings with this detector\n")
        return None
    print(f"    {len(references)} reference embedding(s) in {ref_time:.1f}s")

    threshold = find_threshold(model_name, DISTANCE_METRIC)
    rows = []
    for timestamp, frame in frames:
        t0 = time.time()
        try:
            faces = DeepFace.represent(
                img_path=frame,
                model_name=model_name,
                detector_backend=detector_backend,
                enforce_detection=False,
            )
        except Exception as exc:
            print(f"    [{format_timestamp(timestamp)}] frame failed: {type(exc).__name__}: {exc}")
            continue
        elapsed = time.time() - t0

        real_faces = [f for f in faces if f.get("face_confidence", 1) > 0]
        distances = [best_distance(f["embedding"], references) for f in real_faces]
        rows.append(
            {
                "timestamp": timestamp,
                "elapsed": elapsed,
                "n_faces": len(real_faces),
                "best_all": min(distances) if distances else None,
                "best_first": distances[0] if distances else None,
            }
        )

    if not rows:
        print("    FAILED: every frame errored\n")
        return None

    times = [r["elapsed"] for r in rows]
    matched_all = [r for r in rows if r["best_all"] is not None and r["best_all"] <= threshold]
    matched_first = [r for r in rows if r["best_first"] is not None and r["best_first"] <= threshold]
    result = {
        "detector": detector_backend,
        "n_refs": len(references),
        "ref_time": ref_time,
        "rows": rows,
        "threshold": threshold,
        "mean": statistics.mean(times),
        "median": statistics.median(times),
        "total": sum(times),
        "frames_with_face": sum(1 for r in rows if r["n_faces"] > 0),
        "frames_multi_face": sum(1 for r in rows if r["n_faces"] > 1),
        "max_faces": max(r["n_faces"] for r in rows),
        "matched_all": matched_all,
        "matched_first": matched_first,
    }
    print(
        f"    {len(rows)} frames | mean {result['mean']:.2f}s | median {result['median']:.2f}s "
        f"| total {result['total']:.1f}s"
    )
    print(
        f"    faces found in {result['frames_with_face']}/{len(rows)} frames "
        f"({result['frames_multi_face']} with >1 face, max {result['max_faces']})"
    )
    print(f"    matches: {len(matched_all)} checking all faces, {len(matched_first)} checking only the first\n")
    return result


def report(results, frames):
    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    header = (
        f"{'detector':<14}{'refs':>6}{'mean s':>9}{'median s':>10}"
        f"{'faces':>8}{'multi':>7}{'match-all':>11}{'match-1st':>11}"
    )
    print(header)
    print("-" * 78)
    for r in sorted(results, key=lambda r: r["mean"]):
        print(
            f"{r['detector']:<14}{r['n_refs']:>6}{r['mean']:>9.2f}{r['median']:>10.2f}"
            f"{r['frames_with_face']:>8}{r['frames_multi_face']:>7}"
            f"{len(r['matched_all']):>11}{len(r['matched_first']):>11}"
        )

    print("\nPer-frame agreement (M = match using all faces, . = no match, - = no face)")
    print("-" * 78)
    for r in results:
        line = []
        for row in r["rows"]:
            if row["n_faces"] == 0:
                line.append("-")
            elif row["best_all"] <= r["threshold"]:
                line.append("M")
            else:
                line.append(".")
        print(f"{r['detector']:<14}{''.join(line)}")
    print(f"{'timestamps':<14}{frames[0][0]:.0f}s -> {frames[-1][0]:.0f}s")


def main():
    parser = argparse.ArgumentParser(description="Benchmark DeepFace detector backends")
    parser.add_argument("video_path", type=Path)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--detectors", default=DEFAULT_DETECTORS)
    parser.add_argument("--limit", type=int, default=None, help="only benchmark the first N sampled frames")
    parser.add_argument("--photos-dir", type=Path, default=DEFAULT_PHOTOS_DIR)
    parser.add_argument("--model", default="VGG-Face")
    args = parser.parse_args()

    if not args.video_path.exists():
        print(f"Video not found: {args.video_path}")
        sys.exit(1)

    frames = extract_frames(args.video_path, args.interval, args.limit)
    results = []
    for detector in args.detectors.split(","):
        result = run_detector(detector.strip(), frames, args.photos_dir, args.model)
        if result:
            results.append(result)

    if results:
        report(results, frames)


if __name__ == "__main__":
    main()
