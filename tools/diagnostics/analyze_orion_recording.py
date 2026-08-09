"""Run Orion meter diagnostics against a saved gameplay recording.

This is a non-gameplay tool: it does not submit controller input. It lets us
compare the detector's accepted/rejected boxes and fill values against a known
recording before changing timing constants from live feedback.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from meter_detector import MeterDetector, load_detector_config  # noqa: E402


DEFAULT_VIDEO = Path(r"C:\Users\Administrator\Videos\2026-06-01 17-22-26.mp4")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze a recording with Orion meter detection.")
    parser.add_argument("video", nargs="?", default=str(DEFAULT_VIDEO), help="MP4/gameplay recording path")
    parser.add_argument("--style", default="Arrow2", help="Meter style to lock, e.g. Arrow2")
    parser.add_argument("--color", default="Purple", help="Meter color to detect")
    parser.add_argument("--stride", type=int, default=1, help="Analyze every Nth frame")
    parser.add_argument("--max-frames", type=int, default=0, help="Stop after N analyzed frames")
    parser.add_argument(
        "--out",
        default=str(ROOT / "logs" / "diagnostics" / "orion_recording_diagnostics.csv"),
        help="CSV output path",
    )
    parser.add_argument(
        "--overlay-dir",
        default="",
        help="If set, write annotated PNGs (bbox + fill line + green band) for detected frames here",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Segment the recording into shots and print per-shot detected-frame count, fill range, green presence",
    )
    parser.add_argument(
        "--shot-gap-frames",
        type=int,
        default=8,
        help="Consecutive non-detected frames that end one shot segment (summary)",
    )
    return parser.parse_args()


def _draw_overlay(cv2, frame, result):
    """Annotate a frame with the detector's bbox, fill top line, and green band."""
    annotated = frame.copy()
    if result.detected and result.bbox and result.bbox[2] > 0 and result.bbox[3] > 0:
        x, y, w, h = (int(v) for v in result.bbox)
        # Magenta fill bbox.
        cv2.rectangle(annotated, (x, y), (x + w, y + h), (255, 0, 255), 2)
        # Fill-top line (where the rising magenta currently reaches).
        cv2.line(annotated, (x - 6, y), (x + w + 6, y), (0, 255, 255), 1)
        # Green band (absolute rows) when present.
        gs, ge = result.green_window_start_row, result.green_window_end_row
        if gs is not None and ge is not None and gs >= 0 and ge >= 0:
            top, bot = sorted((int(gs), int(ge)))
            cv2.rectangle(annotated, (x - 4, top), (x + w + 4, bot), (0, 255, 0), 2)
        label = (
            f"fill={result.fill_pct:.0f}% conf={result.confidence:.2f} "
            f"g={result.green_window_center_pct:.0f}/{result.green_window_confidence:.2f}"
        )
        cv2.putText(annotated, label, (max(0, x - 4), max(14, y - 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1, cv2.LINE_AA)
    else:
        rej = getattr(result, "rejection_reason", "") or "no_meter"
        cv2.putText(annotated, f"REJECT: {rej}", (12, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2, cv2.LINE_AA)
    return annotated


def _print_summary(rows, fps, gap_frames):
    """Segment detected runs into shots and report per-shot stats."""
    segments = []
    current = None
    miss_streak = 0
    for r in rows:
        if r["detected"]:
            if current is None:
                current = {"start": r["frame"], "end": r["frame"], "rows": []}
            current["end"] = r["frame"]
            current["rows"].append(r)
            miss_streak = 0
        else:
            if current is not None:
                miss_streak += 1
                if miss_streak >= gap_frames:
                    segments.append(current)
                    current = None
                    miss_streak = 0
    if current is not None:
        segments.append(current)

    print("\n=== Per-shot summary ===")
    if not segments:
        print("  (no detected shot segments found)")
        return
    for i, seg in enumerate(segments, 1):
        det_rows = seg["rows"]
        fills = [r["fill_pct"] for r in det_rows]
        confs = [r["confidence"] for r in det_rows]
        green_rows = [r for r in det_rows if r["green_confidence"] > 0.0 and r["green_center_pct"] >= 0.0]
        t0 = (seg["start"] / fps * 1000.0) if fps else 0.0
        t1 = (seg["end"] / fps * 1000.0) if fps else 0.0
        green_centers = [r["green_center_pct"] for r in green_rows]
        print(
            f"  Shot {i}: frames {seg['start']}-{seg['end']} "
            f"(~{t0/1000:.2f}-{t1/1000:.2f}s) detected={len(det_rows)} "
            f"fill {min(fills):.0f}->{max(fills):.0f}% "
            f"conf[{min(confs):.2f}-{max(confs):.2f}] "
            f"green_frames={len(green_rows)}"
            + (f" green_center~{sum(green_centers)/len(green_centers):.0f}%" if green_centers else " green=NONE")
        )


def main() -> int:
    args = parse_args()
    video = Path(args.video)
    if not video.exists():
        print(f"video not found: {video}", file=sys.stderr)
        return 2

    try:
        import cv2
    except Exception as exc:  # pragma: no cover - environment diagnostic
        print(f"opencv import failed: {exc}", file=sys.stderr)
        return 3

    cfg = load_detector_config(str(ROOT / "settings.json"))
    cfg.meter_style = args.style
    cfg.meter_color = args.color
    detector = MeterDetector(str(ROOT / "meter_styles"), cfg)
    detector.set_active_style(args.style)

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        print(f"could not open video: {video}", file=sys.stderr)
        return 4

    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    overlay_dir = Path(args.overlay_dir) if args.overlay_dir else None
    if overlay_dir is not None:
        overlay_dir.mkdir(parents=True, exist_ok=True)
    summary_rows = []

    fieldnames = [
        "frame",
        "time_ms",
        "detected",
        "style",
        "color",
        "bbox",
        "fill_pct",
        "confidence",
        "velocity_pct_s",
        "accel_pct_s2",
        "eta_to_green_ms",
        "target_pct",
        "green_start_pct",
        "green_end_pct",
        "green_center_pct",
        "green_confidence",
        "release_ready",
        "rejection_reason",
    ]

    analyzed = 0
    detected = 0
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        frame_no = -1
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame_no += 1
            if args.stride > 1 and frame_no % args.stride:
                continue

            result = detector.detect(frame)
            analyzed += 1
            if result.detected:
                detected += 1
            bbox = f"{result.bbox[0]},{result.bbox[1]},{result.bbox[2]},{result.bbox[3]}" if result.detected else "-"
            if args.summary:
                summary_rows.append({
                    "frame": frame_no,
                    "detected": bool(result.detected),
                    "fill_pct": float(result.fill_pct),
                    "confidence": float(result.confidence),
                    "green_center_pct": float(result.green_window_center_pct),
                    "green_confidence": float(result.green_window_confidence),
                })
            if overlay_dir is not None and (result.detected or (frame_no % 30 == 0)):
                annotated = _draw_overlay(cv2, frame, result)
                cv2.imwrite(str(overlay_dir / f"frame_{frame_no:05d}.png"), annotated)
            writer.writerow(
                {
                    "frame": frame_no,
                    "time_ms": round((frame_no / fps) * 1000.0, 3) if fps else "",
                    "detected": int(bool(result.detected)),
                    "style": result.style,
                    "color": result.color_name,
                    "bbox": bbox,
                    "fill_pct": round(float(result.fill_pct), 3),
                    "confidence": round(float(result.confidence), 4),
                    "velocity_pct_s": round(float(result.fill_velocity_pct_s), 3),
                    "accel_pct_s2": round(float(result.fill_acceleration_pct_s2), 3),
                    "eta_to_green_ms": round(float(result.eta_to_green_center_ms), 3),
                    "target_pct": round(float(result.green_window_center_pct), 3),
                    "green_start_pct": round(float(result.green_window_start_pct), 3),
                    "green_end_pct": round(float(result.green_window_end_pct), 3),
                    "green_center_pct": round(float(result.green_window_center_pct), 3),
                    "green_confidence": round(float(result.green_window_confidence), 4),
                    "release_ready": int(bool(result.release_ready)),
                    "rejection_reason": getattr(result, "rejection_reason", ""),
                }
            )
            if args.max_frames and analyzed >= args.max_frames:
                break

    cap.release()
    rate = (detected / analyzed * 100.0) if analyzed else 0.0
    print(f"analyzed={analyzed} detected={detected} detection_rate={rate:.1f}% out={out_path}")
    if overlay_dir is not None:
        print(f"overlays written to {overlay_dir}")
    if args.summary:
        _print_summary(summary_rows, fps, args.shot_gap_frames)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
