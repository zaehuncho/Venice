#!/usr/bin/env python3
"""Extract per-shot wrist-Y trajectories aligned to meter rising-edges.

For each meter-confirmed shot, extracts a window of wrist-Y, hip-Y, knee-Y,
and wrist-velocity centered on the meter appearance. Outputs:
  1. A JSON with per-shot trajectories (for template matching / DTW)
  2. A per-shot CSV with push/release timing vs meter offset
  3. Prints summary: which shots are STD outliers, mean template shape

Usage:
    C:\\Python314\\python.exe tools\\diagnostics\\shot_trajectory_analyzer.py VIDEO COUNT START HANDED [--meter_color Red]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

import numpy as np
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from pose_timing import PoseTimingDetector
from meter_detector import MeterDetector, load_detector_config

SHOT_MATCH_FRAMES = 30
DEBOUNCE = 18
WINDOW_BEFORE = 60   # frames before meter edge
WINDOW_AFTER = 60    # frames after meter edge


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("count", type=int)
    ap.add_argument("start", type=int)
    ap.add_argument("handed", default="Right")
    ap.add_argument("--meter_color", default="Purple")
    ap.add_argument("--out", default="logs/diagnostics/shot_trajectories")
    args = ap.parse_args()

    video = args.video
    count = args.count
    start = args.start
    handed = args.handed

    lms = []
    pose = PoseTimingDetector(handedness=handed, on_landmark=lambda lm: lms.append(lm))

    mcfg = load_detector_config(os.path.join(ROOT, "settings.json"))
    mcfg.meter_style = "Arrow2"
    mcfg.meter_color = args.meter_color
    mcfg.auto_meter_color = False
    meter = MeterDetector(os.path.join(ROOT, "meter_styles"), mcfg)
    meter.set_active_style("Arrow2")

    cap = cv2.VideoCapture(video)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)

    # Collect per-frame data
    frames_data = []  # list of (seq, meter_detected, wrist_y, hip_y, knee_y, wrist_vel)
    shot_appears = []
    prev_meter = False
    last_shot = -10**9
    n = 0

    print(f"{os.path.basename(video)} | {fps:.0f}fps | window {start}..{start+count} | handedness={handed}")
    t0 = time.time()

    while n < count:
        ok, frame = cap.read()
        if not ok:
            break
        seq = start + n
        r = meter.detect(frame)
        fed = bool(r.detected and r.rejection_reason in ("", "green_not_found"))

        if fed and hasattr(r, 'bbox') and r.bbox and len(r.bbox) >= 4 and r.bbox[2] > 0:
            bx, by, bw, bh = r.bbox
            pose.set_player_hint((bx, by, bx + bw, by + bh))

        try:
            pose.update(frame, seq, frame_time=seq / fps)
        except Exception:
            pass

        # Extract wrist/hip/knee from pose internal buffers
        wy = float(pose._wrist_y[(pose._buf_idx - 1) % pose._max_buf]) if pose._buf_count > 0 else float('nan')
        hy = float(pose._hip_y[(pose._buf_idx - 1) % pose._max_buf]) if pose._buf_count > 0 else float('nan')
        ky = float(pose._knee_y[(pose._buf_idx - 1) % pose._max_buf]) if pose._buf_count > 0 else float('nan')
        # Compute wrist velocity from last two frames
        if pose._buf_count >= 2:
            prev_wy = float(pose._wrist_y[(pose._buf_idx - 2) % pose._max_buf])
            prev_t = float(pose._t[(pose._buf_idx - 2) % pose._max_buf])
            cur_t = float(pose._t[(pose._buf_idx - 1) % pose._max_buf])
            dt = cur_t - prev_t
            wv = (wy - prev_wy) / dt if dt > 0 and not np.isnan(wy) and not np.isnan(prev_wy) else float('nan')
        else:
            wv = float('nan')

        frames_data.append({
            "seq": seq,
            "meter": fed,
            "wy": wy if not np.isnan(wy) else None,
            "hy": hy if not np.isnan(hy) else None,
            "ky": ky if not np.isnan(ky) else None,
            "wv": wv if not np.isnan(wv) else None,
        })

        if fed and not prev_meter and (seq - last_shot) > DEBOUNCE:
            shot_appears.append(seq)
            last_shot = seq
        prev_meter = fed
        n += 1

    cap.release()
    elapsed = time.time() - t0
    print(f"processed {n} frames in {elapsed:.0f}s | {len(shot_appears)} meter rising-edges")

    # Collect landmarks
    pushes = [(l.frame_seq, l.confidence) for l in lms if getattr(l, "kind", "") == "push"]
    releases = [(l.frame_seq, l.confidence) for l in lms if getattr(l, "kind", "") == "release"]

    # Build per-shot trajectory windows
    shots = []
    for si, shot_seq in enumerate(shot_appears):
        lo = shot_seq - WINDOW_BEFORE
        hi = shot_seq + WINDOW_AFTER
        window = [fd for fd in frames_data if lo <= fd["seq"] <= hi]
        if not window:
            continue

        # Find nearest push/release
        nearest_push = min(pushes, key=lambda p: abs(p[0] - shot_seq)) if pushes else None
        nearest_rel = min(releases, key=lambda r: abs(r[0] - shot_seq)) if releases else None
        push_match = nearest_push and abs(nearest_push[0] - shot_seq) <= SHOT_MATCH_FRAMES
        rel_match = nearest_rel and abs(nearest_rel[0] - shot_seq) <= SHOT_MATCH_FRAMES

        push_offset_ms = (shot_seq - nearest_push[0]) * 1000.0 / fps if push_match else None
        rel_offset_ms = (shot_seq - nearest_rel[0]) * 1000.0 / fps if rel_match else None

        # Extract trajectory arrays (relative to meter frame)
        wy_traj = []
        hy_traj = []
        ky_traj = []
        wv_traj = []
        seqs = []
        for fd in window:
            rel_frame = fd["seq"] - shot_seq
            seqs.append(rel_frame)
            wy_traj.append(fd["wy"])
            hy_traj.append(fd["hy"])
            ky_traj.append(fd["ky"])
            wv_traj.append(fd["wv"])

        shots.append({
            "shot_idx": si,
            "meter_seq": shot_seq,
            "push_seq": nearest_push[0] if push_match else None,
            "push_offset_ms": push_offset_ms,
            "release_seq": nearest_rel[0] if rel_match else None,
            "release_offset_ms": rel_offset_ms,
            "has_push": bool(push_match),
            "has_release": bool(rel_match),
            "seqs": seqs,
            "wy": wy_traj,
            "hy": hy_traj,
            "ky": ky_traj,
            "wv": wv_traj,
        })

    # Summary
    push_offsets = [s["push_offset_ms"] for s in shots if s["push_offset_ms"] is not None]
    rel_offsets = [s["release_offset_ms"] for s in shots if s["release_offset_ms"] is not None]

    print(f"\n--- Per-shot breakdown ({len(shots)} shots) ---")
    print(f"{'#':<4} {'Meter':<8} {'Push':<8} {'Rel':<8} {'PushOff':<10} {'RelOff':<10} {'Push?':<6} {'Rel?':<6}")
    for s in shots:
        poff = f"{s['push_offset_ms']:+.0f}ms" if s['push_offset_ms'] is not None else "—"
        roff = f"{s['release_offset_ms']:+.0f}ms" if s['release_offset_ms'] is not None else "—"
        pseq = str(s['push_seq']) if s['push_seq'] else "—"
        rseq = str(s['release_seq']) if s['release_seq'] else "—"
        print(f"{s['shot_idx']:<4} {s['meter_seq']:<8} {pseq:<8} {rseq:<8} {poff:<10} {roff:<10} {'Y' if s['has_push'] else 'N':<6} {'Y' if s['has_release'] else 'N':<6}")

    if push_offsets:
        arr_p = np.array(push_offsets)
        print(f"\nPush offset: mean {np.mean(arr_p):+.0f}ms  STD {np.std(arr_p):.0f}ms  (n={len(arr_p)})")
        print(f"  median {np.median(arr_p):+.0f}ms  IQR {np.percentile(arr_p,75)-np.percentile(arr_p,25):.0f}ms (p25={np.percentile(arr_p,25):+.0f} p75={np.percentile(arr_p,75):+.0f})")
        within_40 = np.sum(np.abs(arr_p - np.median(arr_p)) <= 40)
        within_80 = np.sum(np.abs(arr_p - np.median(arr_p)) <= 80)
        print(f"  within ±40ms of median: {within_40}/{len(arr_p)} ({100*within_40/len(arr_p):.0f}%)")
        print(f"  within ±80ms of median: {within_80}/{len(arr_p)} ({100*within_80/len(arr_p):.0f}%)")
        # Outliers (> 1.5 STD)
        mean_p = np.mean(arr_p)
        std_p = np.std(arr_p)
        outliers = [s for s in shots if s["push_offset_ms"] is not None and abs(s["push_offset_ms"] - mean_p) > 1.5 * std_p]
        if outliers:
            outlier_ids = [s['shot_idx'] for s in outliers]
            outlier_offs = [f"{s['push_offset_ms']:+.0f}ms" for s in outliers]
            print(f"  Outliers (>1.5 sigma): shots {outlier_ids} at {outlier_offs}")
    if rel_offsets:
        print(f"Release offset: mean {np.mean(rel_offsets):+.0f}ms  STD {np.std(rel_offsets):.0f}ms  (n={len(rel_offsets)})")

    # Build average template from detected shots
    detected = [s for s in shots if s["has_push"] and s["wy"]]
    if detected:
        template_len = 80  # 40 frames before push, 40 after
        template_wy = []
        for s in detected:
            push_rel = s["push_seq"] - s["meter_seq"]
            wy = s["wy"]
            seqs = s["seqs"]
            push_idx = None
            for i, sq in enumerate(seqs):
                if sq == (s["push_seq"] - s["meter_seq"]):
                    push_idx = i
                    break
            if push_idx is None:
                continue
            lo_i = max(0, push_idx - 40)
            hi_i = min(len(wy), push_idx + 40)
            segment = [v if v is not None else float('nan') for v in wy[lo_i:hi_i]]
            if len(segment) >= 20:
                template_wy.append(segment)

        if template_wy:
            min_len = min(len(t) for t in template_wy)
            arr = np.array([t[:min_len] for t in template_wy], dtype=float)
            mean_traj = np.nanmean(arr, axis=0)
            std_traj = np.nanstd(arr, axis=0)
            print(f"\nTemplate (push-aligned, {len(template_wy)} shots, {min_len} frames):")
            print(f"  Mean wrist-Y range: {np.nanmin(mean_traj):.3f} .. {np.nanmax(mean_traj):.3f}")
            print(f"  STD at each frame: mean {np.nanmean(std_traj):.3f}, max {np.nanmax(std_traj):.3f}")

    # Save
    clip_name = os.path.splitext(os.path.basename(video))[0]
    os.makedirs(args.out, exist_ok=True)

    json_path = os.path.join(args.out, f"{clip_name}_shots.json")
    with open(json_path, "w") as f:
        json.dump({
            "clip": os.path.basename(video),
            "fps": fps,
            "start": start,
            "count": count,
            "handedness": handed,
            "meter_color": args.meter_color,
            "n_shots": len(shots),
            "n_pushes": len(pushes),
            "n_releases": len(releases),
            "push_offset_mean_ms": float(np.mean(push_offsets)) if push_offsets else None,
            "push_offset_std_ms": float(np.std(push_offsets)) if push_offsets else None,
            "release_offset_mean_ms": float(np.mean(rel_offsets)) if rel_offsets else None,
            "release_offset_std_ms": float(np.std(rel_offsets)) if rel_offsets else None,
            "shots": shots,
        }, f, indent=2)
    print(f"\nSaved -> {json_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
