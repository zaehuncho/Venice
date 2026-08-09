#!/usr/bin/env python3
"""Per-shot-type duration analysis — tests whether clustering shots by their
early motion pattern tightens the release timing IQR.

If different jumpshot bases have different press→release durations, the
pooled 200ms IQR includes between-base variance. Within-base variance
may be much tighter.

Uses k-means on the first 30 frames of wrist-Y trajectory to cluster shots,
then measures within-cluster IQR vs pooled IQR.

Usage:
    C:\\Python314\\python.exe tools/diagnostics/shot_type_cluster.py "<video>" [count] [start] [handed] [--meter_color Red]
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import cv2
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from pose_timing import PoseTimingDetector
from meter_detector import MeterDetector, load_detector_config

DEBOUNCE = 18
PRE_FRAMES = 60  # frames before meter edge to capture early motion
POST_FRAMES = 10


def process_clip(video, count, start, handed, meter_color):
    pose = PoseTimingDetector(handedness=handed)
    mcfg = load_detector_config(os.path.join(ROOT, "settings.json"))
    mcfg.meter_style = "Arrow2"
    mcfg.meter_color = meter_color
    mcfg.auto_meter_color = False
    meter = MeterDetector(os.path.join(ROOT, "meter_styles"), mcfg)
    meter.set_active_style("Arrow2")

    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)

    print(f"  {os.path.basename(video)} | {fps:.0f}fps | window {start}..{start+count}")

    wy_all = []
    hy_all = []
    shot_edges = []
    prev_meter = False
    last_shot = -10**9
    n = 0

    print("  Processing...", end=" ", flush=True)
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
        j = (pose._buf_idx - 1) % pose._max_buf
        wy_all.append(float(pose._wrist_y[j]))
        hy_all.append(float(pose._hip_y[j]))

        if fed and not prev_meter and (seq - last_shot) > DEBOUNCE:
            shot_edges.append(n)
            last_shot = seq
        prev_meter = fed
        n += 1

    cap.release()
    print(f"done | {len(shot_edges)} shots")

    ms = 1000.0 / fps

    # For each shot, extract early motion pattern (wrist-Y trajectory before meter edge)
    # and the release cue offset (wrist-Y accel-peak after meter edge)
    patterns = []
    offsets = []
    valid_shots = []

    wy_arr = np.array(wy_all)
    # Forward-fill NaNs
    mask = ~np.isnan(wy_arr)
    if mask.sum() > 10:
        wy_arr = np.interp(np.arange(len(wy_arr)), np.where(mask)[0], wy_arr[mask])
    wy_smooth = np.convolve(wy_arr, [0.25, 0.5, 0.25], mode="same")
    wy_vel = np.gradient(wy_smooth)
    wy_acc = np.gradient(wy_vel)

    for e in shot_edges:
        # Early motion pattern: 60 frames before meter edge
        lo = max(0, e - PRE_FRAMES)
        hi = e
        if hi - lo < 30:
            continue
        pattern = wy_smooth[lo:hi]
        # Normalize
        if np.std(pattern) < 1e-6:
            continue
        pattern_norm = (pattern - np.mean(pattern)) / (np.std(pattern) + 1e-8)
        patterns.append(pattern_norm)

        # Release cue: accel-peak in window after meter edge
        rlo = e
        rhi = min(len(wy_smooth), e + 45)
        if rhi - rlo < 6:
            continue
        seg_acc = wy_acc[rlo:rhi]
        accel_peak = rlo + int(np.argmax(np.abs(seg_acc)))
        offset = (accel_peak - e) * ms
        offsets.append(offset)
        valid_shots.append(e)

    if len(offsets) < 10:
        print(f"  [warn] only {len(offsets)} valid shots")
        return

    offsets = np.array(offsets)

    # Pad patterns to same length (some may be shorter if near start)
    max_len = max(p.shape[0] for p in patterns)
    padded = []
    for p in patterns:
        if p.shape[0] < max_len:
            pad = np.full(max_len - p.shape[0], p[0] if len(p) > 0 else 0)
            p = np.concatenate([pad, p])
        padded.append(p)
    patterns = np.array(padded)

    # Pooled IQR
    pooled_med = np.median(offsets)
    pooled_iqr = np.percentile(offsets, 75) - np.percentile(offsets, 25)
    pooled_w40 = sum(1 for x in offsets if abs(x - pooled_med) <= 40)
    print(f"\n  POOLED: n={len(offsets)} median={pooled_med:+.0f}ms IQR={pooled_iqr:.0f}ms ±40ms={pooled_w40}/{len(offsets)}")

    # Cluster shots by early motion pattern
    scaler = StandardScaler()
    patterns_scaled = scaler.fit_transform(patterns)

    for n_clusters in [2, 3, 4, 5]:
        km = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
        labels = km.fit_predict(patterns_scaled)

        print(f"\n  K={n_clusters} clusters:")
        within_iqrs = []
        total_w40 = 0
        for k in range(n_clusters):
            mask = labels == k
            if np.sum(mask) < 3:
                continue
            cluster_offsets = offsets[mask]
            med = np.median(cluster_offsets)
            iqr = np.percentile(cluster_offsets, 75) - np.percentile(cluster_offsets, 25)
            w40 = sum(1 for x in cluster_offsets if abs(x - med) <= 40)
            within_iqrs.append(iqr)
            total_w40 += w40
            print(f"    cluster {k}: n={np.sum(mask)} median={med:+.0f}ms IQR={iqr:.0f}ms ±40ms={w40}/{np.sum(mask)}")

        mean_iqr = np.mean(within_iqrs) if within_iqrs else 0
        print(f"    → mean within-cluster IQR = {mean_iqr:.0f}ms (pooled: {pooled_iqr:.0f}ms), total ±40ms={total_w40}/{len(offsets)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("count", type=int, nargs="?", default=3000)
    ap.add_argument("start", type=int, nargs="?", default=11940)
    ap.add_argument("handed", nargs="?", default="Right")
    ap.add_argument("--meter_color", default="Red")
    args = ap.parse_args()

    print(f"PER-SHOT-TYPE DURATION ANALYSIS")
    print(f"Tests whether clustering shots by early motion tightens IQR")
    print(f"{'='*70}")

    process_clip(args.video, args.count, args.start, args.handed, args.meter_color)


if __name__ == "__main__":
    main()
