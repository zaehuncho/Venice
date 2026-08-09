#!/usr/bin/env python3
"""Pose-aligned meter offset analysis — the FAIR e2e test.

For each meter-confirmed shot, find pose landmarks (wrist-Y min, wrist-Y max,
hip-Y min, knee-Y min, wrist velocity peak) and measure the time from each
landmark to the meter edge. If any landmark has a tight meter offset, that
landmark can predict meter timing. If all are wide, pose genuinely can't.

This fixes the circular e2e bug: the old e2e aligned to the meter edge (making
the meter position constant by construction). This version aligns to POSE events
and predicts the METER offset — the correct direction.

Usage:
    C:\\Python314\\python.exe tools/diagnostics/pose_aligned_meter_offset.py
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

DEBOUNCE = 18
SEARCH_BEFORE = 180  # 3s before meter edge to search for pose landmarks
SEARCH_AFTER = 30    # 0.5s after

CLIPS = [
    ("NBA 2K26_20260324195410.mp4", 12000, 2000, "Right", "Purple", "V1"),
    ("NBA 2K26_20260618064057.mp4", 12000, 1000, "Right", "Purple", "V2"),
    ("NBA 2K26_20260624212008.mp4", 3000, 150, "Right", "Red", "V3"),
    ("NBA 2K26_20260624212347.mp4", 3000, 2880, "Right", "Red", "V4"),
    ("NBA 2K26_20260624212700.mp4", 3000, 6240, "Right", "Red", "V5"),
    ("NBA 2K26_20260624213055.mp4", 3000, 7860, "Right", "Red", "V6"),
    ("NBA 2K26_20260521032018.mp4", 3000, 11940, "Right", "Red", "V7"),
]

VIDEOS = r"C:\Users\Administrator\Videos"


def find_pose_landmarks(wy, hy, ky, seqs, meter_seq, fps):
    """Find pose landmarks in the window before the meter edge.

    Returns dict of landmark_name -> frame_offset_from_meter (in ms).
    Negative = landmark before meter edge.
    """
    # Window: SEARCH_BEFORE frames before meter edge to SEARCH_AFTER after
    lo = meter_seq - SEARCH_BEFORE
    hi = meter_seq + SEARCH_AFTER
    mask = (seqs >= lo) & (seqs <= hi)
    if np.sum(mask) < 30:
        return {}

    wy_w = wy[mask]
    hy_w = hy[mask]
    ky_w = ky[mask]
    seqs_w = seqs[mask]

    # Forward-fill NaNs
    for arr in [wy_w, hy_w, ky_w]:
        nans = np.isnan(arr)
        if nans.all():
            return {}
        idx = np.where(~nans, np.arange(len(arr)), 0)
        np.maximum.accumulate(idx, out=idx)
        arr[nans] = arr[idx[nans]]

    # Meter edge position in the window
    meter_idx = np.argmin(np.abs(seqs_w - meter_seq))

    results = {}

    # 1. Wrist-Y minimum (rest position before shot = gather)
    # Search only in the first 2/3 of the window (before meter edge)
    pre_mask = seqs_w < meter_seq
    pre_wy = wy_w[pre_mask]
    pre_seqs = seqs_w[pre_mask]
    if len(pre_wy) > 10:
        # Y=0 is top, Y=1 is bottom. Wrist at rest is LOW (high Y), raised is HIGH (low Y)
        # rest = MAX wrist-Y, release = MIN wrist-Y
        rest_idx = np.argmax(pre_wy)  # max Y = lowest position = rest
        rest_seq = pre_seqs[rest_idx]
        results["wrist_rest"] = int((rest_seq - meter_seq) * 1000.0 / fps)

    # 2. Wrist-Y maximum raise (minimum Y = highest point = release point)
    # Search in full window
    release_idx = np.argmin(wy_w)  # min Y = highest point
    release_seq = seqs_w[release_idx]
    results["wrist_apex"] = int((release_seq - meter_seq) * 1000.0 / fps)

    # 3. Hip-Y minimum (jump apex = highest hip point)
    pre_hy = hy_w[pre_mask]
    pre_seqs_h = seqs_w[pre_mask]
    if len(pre_hy) > 10 and not np.all(np.isnan(pre_hy)):
        hip_apex_idx = np.argmin(pre_hy)  # min Y = highest point = jump apex
        hip_apex_seq = pre_seqs_h[hip_apex_idx]
        results["hip_apex"] = int((hip_apex_seq - meter_seq) * 1000.0 / fps)

    # 4. Knee-Y maximum (deepest crouch = gather)
    pre_ky = ky_w[pre_mask]
    pre_seqs_k = seqs_w[pre_mask]
    if len(pre_ky) > 10 and not np.all(np.isnan(pre_ky)):
        crouch_idx = np.argmax(pre_ky)  # max Y = lowest point = deepest crouch
        crouch_seq = pre_seqs_k[crouch_idx]
        results["knee_crouch"] = int((crouch_seq - meter_seq) * 1000.0 / fps)

    # 5. Wrist velocity peak (fastest upward motion = push start)
    if len(wy_w) > 5:
        wy_vel = np.diff(wy_w) * fps  # Y-units per second
        # Upward motion = negative velocity (Y decreasing)
        # Push = fastest upward = most negative velocity
        n_pre = int(np.sum(pre_mask))
        pre_vel = wy_vel[:n_pre] if len(wy_vel) >= n_pre else wy_vel[:n_pre]
        pre_seqs_v = seqs_w[:len(pre_vel)]
        if len(pre_vel) > 5:
            push_idx = np.argmin(pre_vel)  # most negative = fastest upward
            push_seq = pre_seqs_v[push_idx]
            results["wrist_vel_peak"] = int((push_seq - meter_seq) * 1000.0 / fps)

    # 6. Wrist-Y crossing threshold (crude push detection, like pose_timing.py)
    # rest_w = 75th percentile of pre-shot wrist-Y, threshold = rest_w - 0.08
    if len(pre_wy) > 10:
        rest_w = np.percentile(pre_wy, 75)
        threshold = rest_w - 0.08
        below = pre_wy < threshold
        if np.any(below):
            first_below = np.argmax(below)  # first frame below threshold
            push_thresh_seq = pre_seqs[first_below]
            results["wrist_threshold_push"] = int((push_thresh_seq - meter_seq) * 1000.0 / fps)

    return results


def process_clip(clip_name, count, start, handed, mcolor, clip_label):
    video = os.path.join(VIDEOS, clip_name)
    if not os.path.exists(video):
        print(f"  [skip] {video} not found")
        return []

    lms = []
    pose = PoseTimingDetector(handedness=handed, on_landmark=lambda lm: lms.append(lm))

    mcfg = load_detector_config(os.path.join(ROOT, "settings.json"))
    mcfg.meter_style = "Arrow2"
    mcfg.meter_color = mcolor
    mcfg.auto_meter_color = False
    meter = MeterDetector(os.path.join(ROOT, "meter_styles"), mcfg)
    meter.set_active_style("Arrow2")

    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)

    frames_data = []
    shot_appears = []
    prev_meter = False
    last_shot = -10**9
    n = 0

    print(f"  [{clip_label}] {fps:.0f}fps window {start}..{start+count} ...", end=" ", flush=True)

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

        wy = float(pose._wrist_y[(pose._buf_idx - 1) % pose._max_buf]) if pose._buf_count > 0 else float('nan')
        hy = float(pose._hip_y[(pose._buf_idx - 1) % pose._max_buf]) if pose._buf_count > 0 else float('nan')
        ky = float(pose._knee_y[(pose._buf_idx - 1) % pose._max_buf]) if pose._buf_count > 0 else float('nan')

        frames_data.append({"seq": seq, "wy": wy, "hy": hy, "ky": ky})

        if fed and not prev_meter and (seq - last_shot) > DEBOUNCE:
            shot_appears.append(seq)
            last_shot = seq
        prev_meter = fed
        n += 1

    cap.release()

    # For each shot, find pose landmarks and measure meter offset
    results = []
    wy_arr = np.array([f["wy"] if f["wy"] is not None else np.nan for f in frames_data])
    hy_arr = np.array([f["hy"] if f["hy"] is not None else np.nan for f in frames_data])
    ky_arr = np.array([f["ky"] if f["ky"] is not None else np.nan for f in frames_data])
    seqs_arr = np.array([f["seq"] for f in frames_data])

    for shot_seq in shot_appears:
        landmarks = find_pose_landmarks(wy_arr, hy_arr, ky_arr, seqs_arr, shot_seq, fps)
        if landmarks:
            results.append({
                "clip": clip_label,
                "shot_seq": shot_seq,
                "landmarks": landmarks,
            })

    print(f"{len(shot_appears)} shots, {len(results)} with landmarks")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="logs/diagnostics/pose_aligned_offsets.json")
    args = ap.parse_args()

    all_results = []
    for clip_name, count, start, handed, mcolor, clip_label in CLIPS:
        results = process_clip(clip_name, count, start, handed, mcolor, clip_label)
        all_results.extend(results)

    # Analyze each landmark
    landmark_names = set()
    for r in all_results:
        landmark_names.update(r["landmarks"].keys())

    print(f"\n{'='*80}")
    print(f"POSE-ALIGNED METER OFFSET ANALYSIS ({len(all_results)} shots)")
    print(f"{'='*80}")
    print(f"\n{'Landmark':<25} {'n':>4} {'Mean':>8} {'STD':>8} {'Median':>8} {'IQR':>8} {'±40ms':>8} {'±80ms':>8}")
    print("-" * 80)

    summary = {}
    for lm_name in sorted(landmark_names):
        offsets = [r["landmarks"][lm_name] for r in all_results if lm_name in r["landmarks"]]
        if len(offsets) < 5:
            continue
        arr = np.array(offsets)
        mean = np.mean(arr)
        std = np.std(arr)
        median = np.median(arr)
        iqr = np.percentile(arr, 75) - np.percentile(arr, 25)
        within_40 = np.sum(np.abs(arr - median) <= 40)
        within_80 = np.sum(np.abs(arr - median) <= 80)
        pct40 = 100 * within_40 / len(arr)
        pct80 = 100 * within_80 / len(arr)

        summary[lm_name] = {
            "n": len(arr), "mean": mean, "std": std, "median": median,
            "iqr": iqr, "within_40ms": f"{within_40}/{len(arr)} ({pct40:.0f}%)",
            "within_80ms": f"{within_80}/{len(arr)} ({pct80:.0f}%)",
        }

        print(f"{lm_name:<25} {len(arr):>4} {mean:>+7.0f}ms {std:>7.0f}ms {median:>+7.0f}ms {iqr:>7.0f}ms {f'{within_40}/{len(arr)} ({pct40:.0f}%)':>8} {f'{within_80}/{len(arr)} ({pct80:.0f}%)':>8}")

    # Per-clip breakdown for the best landmark
    print(f"\n{'='*80}")
    print("PER-CLIP BREAKDOWN")
    print(f"{'='*80}")

    for lm_name in sorted(landmark_names):
        offsets = [r["landmarks"][lm_name] for r in all_results if lm_name in r["landmarks"]]
        if len(offsets) < 5:
            continue
        print(f"\n  {lm_name}:")
        clip_groups = {}
        for r in all_results:
            if lm_name in r["landmarks"]:
                clip_groups.setdefault(r["clip"], []).append(r["landmarks"][lm_name])
        for clip, offs in sorted(clip_groups.items()):
            arr = np.array(offs)
            print(f"    {clip}: n={len(arr)} median={np.median(arr):+.0f}ms IQR={np.percentile(arr,75)-np.percentile(arr,25):.0f}ms STD={np.std(arr):.0f}ms")

    # Save
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({"summary": summary, "shots": all_results}, f, indent=2)
    print(f"\nSaved -> {args.out}")


if __name__ == "__main__":
    main()
