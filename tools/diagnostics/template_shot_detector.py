#!/usr/bin/env python3
"""Template-matching shot detector — cross-correlate wrist-Y trajectory with a
canonical shot template to detect shot events.

This is for shot DETECTION (finding when a shot happens), not timing.
The template is built from the 281 meter-aligned sequences in e2e_data.json.

Usage:
    C:\\Python314\\python.exe tools/diagnostics/template_shot_detector.py VIDEO COUNT START HANDED [--meter_color Red|Purple]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from pose_timing import PoseTimingDetector
from meter_detector import MeterDetector, load_detector_config

SHOT_MATCH_FRAMES = 30
DEBOUNCE = 18


def build_template(e2e_path: str, exclude_clip: str = None):
    """Build canonical shot template from e2e data.

    If exclude_clip is set, sequences from that clip are excluded (leave-one-out).
    """
    with open(e2e_path) as f:
        data = json.load(f)

    sequences = data["sequences"]
    if exclude_clip:
        sequences = [s for s in sequences if s["clip"] != exclude_clip]
        print(f"  (leave-one-out: excluded {exclude_clip}, {len(sequences)} seqs remaining)")

    if len(sequences) < 5:
        print(f"WARNING: only {len(sequences)} sequences for template")

    # Use the wy_norm (normalized wrist-Y) trajectories
    min_len = min(len(s["wy_norm"]) for s in sequences)
    wy_arr = np.array([s["wy_norm"][:min_len] for s in sequences])

    template = np.nanmean(wy_arr, axis=0)
    template_std = np.nanstd(wy_arr, axis=0)

    # Also build hip-Y template
    hy_arr = np.array([s["hy_norm"][:min_len] for s in sequences])
    hy_template = np.nanmean(hy_arr, axis=0)

    return template, template_std, hy_template, min_len, len(sequences)


def normalize_window(wy):
    """Normalize a wrist-Y window the same way as e2e data."""
    wy = np.array(wy, dtype=float)
    mask = np.isnan(wy)
    if mask.all():
        return wy
    idx = np.where(~mask, np.arange(len(wy)), 0)
    np.maximum.accumulate(idx, out=idx)
    wy[mask] = wy[idx[mask]]
    mean = np.nanmean(wy)
    std = np.nanstd(wy)
    if std < 1e-8:
        return wy - mean
    return (wy - mean) / std


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("count", type=int)
    ap.add_argument("start", type=int)
    ap.add_argument("handed", default="Right")
    ap.add_argument("--meter_color", default="Red")
    ap.add_argument("--e2e_data", default="logs/diagnostics/e2e_data.json")
    ap.add_argument("--threshold", type=float, default=0.5,
                    help="correlation threshold for shot detection")
    ap.add_argument("--exclude_clip", default=None,
                    help="clip name to exclude from template (leave-one-out)")
    args = ap.parse_args()

    e2e_path = os.path.join(ROOT, args.e2e_data)
    if not os.path.exists(e2e_path):
        print(f"ERROR: e2e data not found at {e2e_path}")
        return 1

    template, template_std, hy_template, template_len, n_seqs = build_template(e2e_path, args.exclude_clip)
    print(f"Template: {n_seqs} sequences, {template_len} frames")
    print(f"  wrist-Y range: {template.min():.2f}..{template.max():.2f}")
    print(f"  wrist-Y STD (across shots): mean {np.mean(template_std):.3f}, max {np.max(template_std):.3f}")

    # Set up detectors
    lms = []
    pose = PoseTimingDetector(handedness=args.handed, on_landmark=lambda lm: lms.append(lm))

    mcfg = load_detector_config(os.path.join(ROOT, "settings.json"))
    mcfg.meter_style = "Arrow2"
    mcfg.meter_color = args.meter_color
    mcfg.auto_meter_color = False
    meter = MeterDetector(os.path.join(ROOT, "meter_styles"), mcfg)
    meter.set_active_style("Arrow2")

    cap = cv2.VideoCapture(args.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, args.start)

    print(f"\n{os.path.basename(args.video)} | {fps:.0f}fps | window {args.start}..{args.start+args.count} | handedness={args.handed}")

    # Collect all wrist-Y values
    wy_stream = []
    meter_edges = []
    prev_meter = False
    last_shot = -10**9
    n = 0

    print("Processing frames...", end=" ", flush=True)
    while n < args.count:
        ok, frame = cap.read()
        if not ok:
            break
        seq = args.start + n

        r = meter.detect(frame)
        fed = bool(r.detected and r.rejection_reason in ("", "green_not_found"))
        if fed and hasattr(r, 'bbox') and r.bbox and len(r.bbox) >= 4 and r.bbox[2] > 0:
            bx, by, bw, bh = r.bbox
            pose.set_player_hint((bx, by, bx + bw, by + bh))

        try:
            pose.update(frame, seq, frame_time=seq / fps)
        except Exception:
            pass

        wy = float(pose._wrist_y[(pose._buf_idx - 1) % pose._max_buf]) if pose._buf_count > 0 else float('nan')
        wy_stream.append(wy)

        if fed and not prev_meter and (seq - last_shot) > DEBOUNCE:
            meter_edges.append(seq)
            last_shot = seq
        prev_meter = fed
        n += 1

    cap.release()
    print(f"done | {len(meter_edges)} meter rising-edges")

    # Cross-correlate
    wy_arr = np.array(wy_stream, dtype=float)
    half = template_len // 2

    # Slide template over wrist-Y stream
    correlations = []
    for i in range(half, len(wy_arr) - half):
        window = wy_arr[i - half : i + half + 1]
        if len(window) < template_len:
            continue
        # Check if enough non-NaN values
        valid = np.sum(~np.isnan(window))
        if valid < template_len * 0.5:
            correlations.append(0.0)
            continue
        window_norm = normalize_window(window)
        # Correlation
        if np.std(window_norm) < 1e-8:
            correlations.append(0.0)
            continue
        corr = np.corrcoef(window_norm[:template_len], template)[0, 1]
        correlations.append(corr)

    correlations = np.array(correlations)

    # Find peaks in correlation
    detected_shots = []
    i = 0
    while i < len(correlations):
        if correlations[i] >= args.threshold:
            # Find local peak
            peak_i = i
            while i + 1 < len(correlations) and correlations[i + 1] >= args.threshold:
                if correlations[i + 1] > correlations[peak_i]:
                    peak_i = i + 1
                i += 1
            # peak_i is relative to half offset
            shot_frame = args.start + peak_i + half
            detected_shots.append((shot_frame, correlations[peak_i]))
            i += DEBOUNCE  # skip ahead
        else:
            i += 1

    # Compare with meter edges
    print(f"\nTemplate matching: {len(detected_shots)} shots detected (threshold={args.threshold})")
    print(f"Meter edges: {len(meter_edges)}")

    # Match detected shots to meter edges
    matched = 0
    false_pos = 0
    for shot_frame, corr in detected_shots:
        best_dist = min(abs(shot_frame - me) for me in meter_edges) if meter_edges else 9999
        if best_dist <= SHOT_MATCH_FRAMES:
            matched += 1
        else:
            false_pos += 1

    detection_rate = 100 * matched / max(len(meter_edges), 1)
    precision = 100 * matched / max(len(detected_shots), 1)

    print(f"  Matched to meter: {matched}/{len(meter_edges)} ({detection_rate:.0f}%)")
    print(f"  False positives: {false_pos} (precision {precision:.0f}%)")

    # Compare with pose push detection
    push_shots = sum(1 for lm in lms if getattr(lm, 'lm_type', '') == 'push')
    print(f"  Pose push detections: {push_shots}")

    # Print correlation stats
    valid_corr = correlations[correlations > 0]
    if len(valid_corr) > 0:
        print(f"\nCorrelation stats: mean {np.mean(valid_corr):.2f}, max {np.max(valid_corr):.2f}, p95 {np.percentile(valid_corr, 95):.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
