#!/usr/bin/env python3
"""Prepare training data for a potential learned end-to-end model (pose-seq -> release-time).

Extracts fixed-length pose sequences (wrist-Y, hip-Y, knee-Y) centered on each
meter-confirmed shot, with the meter frame as the target label. This creates
a dataset that maps pose trajectories -> meter timing, which a small MLP/1D-CNN
could learn to predict.

Output: JSON with {sequences: [...], targets: [...], metadata: {...}}

Usage:
    C:\\Python314\\python.exe tools\\diagnostics/prepare_e2e_data.py [--out logs/diagnostics/e2e_data.json]
"""
from __future__ import annotations

import argparse
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
SEQ_BEFORE = 90   # 1.5s before meter edge at 60fps
SEQ_AFTER = 30    # 0.5s after

CLIPS = [
    ("NBA 2K26_20260324195410.mp4", 12000, 2000, "Right", "Purple"),
    ("NBA 2K26_20260618064057.mp4", 12000, 1000, "Right", "Purple"),
    ("NBA 2K26_20260624212008.mp4", 3000, 150, "Right", "Red"),
    ("NBA 2K26_20260624212347.mp4", 3000, 2880, "Right", "Red"),
    ("NBA 2K26_20260624212700.mp4", 3000, 6240, "Right", "Red"),
    ("NBA 2K26_20260624213055.mp4", 3000, 7860, "Right", "Red"),
    ("NBA 2K26_20260521032018.mp4", 3000, 11940, "Right", "Red"),
]

VIDEOS = r"C:\Users\Administrator\Videos"


def process_clip(clip_name, count, start, handed, mcolor):
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

    print(f"  [{clip_name[:25]}] {fps:.0f}fps window {start}..{start+count} ...", end=" ", flush=True)

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

        frames_data.append({
            "seq": seq,
            "wy": wy if not np.isnan(wy) else None,
            "hy": hy if not np.isnan(hy) else None,
            "ky": ky if not np.isnan(ky) else None,
        })

        if fed and not prev_meter and (seq - last_shot) > DEBOUNCE:
            shot_appears.append(seq)
            last_shot = seq
        prev_meter = fed
        n += 1

    cap.release()

    # Build sequences for each shot
    sequences = []
    for shot_seq in shot_appears:
        lo = shot_seq - SEQ_BEFORE
        hi = shot_seq + SEQ_AFTER
        window = [fd for fd in frames_data if lo <= fd["seq"] <= hi]
        if len(window) < 60:
            continue

        wy = np.array([v["wy"] if v["wy"] is not None else np.nan for v in window])
        hy = np.array([v["hy"] if v["hy"] is not None else np.nan for v in window])
        ky = np.array([v["ky"] if v["ky"] is not None else np.nan for v in window])
        seqs = np.array([v["seq"] for v in window])

        # Forward-fill NaNs
        for arr in [wy, hy, ky]:
            mask = np.isnan(arr)
            if mask.all():
                continue
            idx = np.where(~mask, np.arange(len(arr)), 0)
            np.maximum.accumulate(idx, out=idx)
            arr[mask] = arr[idx[mask]]

        # Normalize: subtract mean, divide by std (per-sequence)
        wy_n = (wy - np.nanmean(wy)) / (np.nanstd(wy) + 1e-8)
        hy_n = (hy - np.nanmean(hy)) / (np.nanstd(hy) + 1e-8)
        ky_n = (ky - np.nanmean(ky)) / (np.nanstd(ky) + 1e-8)

        # Target: frame offset of meter edge relative to window start
        meter_offset = shot_seq - seqs[0]  # in frames

        sequences.append({
            "clip": clip_name,
            "shot_seq": shot_seq,
            "meter_offset_frames": int(meter_offset),
            "meter_offset_ms": float(meter_offset * 1000.0 / fps),
            "seq_len": len(window),
            "wy_norm": wy_n.tolist(),
            "hy_norm": hy_n.tolist(),
            "ky_norm": ky_n.tolist(),
            "fps": fps,
        })

    print(f"{len(shot_appears)} shots, {len(sequences)} sequences")
    return sequences


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="logs/diagnostics/e2e_data.json")
    args = ap.parse_args()

    all_sequences = []
    for clip_name, count, start, handed, mcolor in CLIPS:
        seqs = process_clip(clip_name, count, start, handed, mcolor)
        all_sequences.extend(seqs)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({
            "n_sequences": len(all_sequences),
            "seq_before": SEQ_BEFORE,
            "seq_after": SEQ_AFTER,
            "features": ["wy_norm", "hy_norm", "ky_norm"],
            "target": "meter_offset_frames",
            "sequences": all_sequences,
        }, f, indent=2)

    print(f"\nSaved {len(all_sequences)} sequences -> {args.out}")
    print(f"Target distribution (meter_offset_frames):")
    offsets = [s["meter_offset_frames"] for s in all_sequences]
    arr = np.array(offsets)
    print(f"  mean {np.mean(arr):.1f}  std {np.std(arr):.1f}  min {np.min(arr)}  max {np.max(arr)}")


if __name__ == "__main__":
    main()
