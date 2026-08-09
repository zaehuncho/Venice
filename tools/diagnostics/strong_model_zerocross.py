#!/usr/bin/env python3
"""Test the stronger pose model (yolo11s) with the causal zero-crossing detector.

Runs the same causal EMA zero-crossing test but with orion_pose2k_s.pt
instead of the default orion_pose2k_n.pt to measure keypoint precision improvement.

Usage:
    C:\\Python314\\python.exe tools/diagnostics/strong_model_zerocross.py "<video>" [count] [start] [handed] [--meter_color Red]
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from pose_timing import PoseTimingDetector
from meter_detector import MeterDetector, load_detector_config

DEBOUNCE = 18
SEARCH_AFTER = 60


def process_clip(video, count, start, handed, meter_color, model_path):
    pose = PoseTimingDetector(handedness=handed, pose_model_path=model_path)
    mcfg = load_detector_config(os.path.join(ROOT, "settings.json"))
    mcfg.meter_style = "Arrow2"
    mcfg.meter_color = meter_color
    mcfg.auto_meter_color = False
    meter = MeterDetector(os.path.join(ROOT, "meter_styles"), mcfg)
    meter.set_active_style("Arrow2")

    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)

    print(f"  {os.path.basename(video)} | {fps:.0f}fps | model={os.path.basename(model_path)}")

    wy_raw = []
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
        wy_raw.append(float(pose._wrist_y[j]))

        if fed and not prev_meter and (seq - last_shot) > DEBOUNCE:
            shot_edges.append(n)
            last_shot = seq
        prev_meter = fed
        n += 1

    cap.release()
    print(f"done | {len(shot_edges)} shots")

    ms = 1000.0 / fps

    wy = np.array(wy_raw, dtype=float)
    mask = ~np.isnan(wy)
    if mask.sum() < 10:
        print("  [error] not enough valid wrist data")
        return
    wy = np.interp(np.arange(len(wy)), np.where(mask)[0], wy[mask])

    # Causal EMA with different tau values
    def causal_ema(signal, tau_ms):
        alpha = 1.0 - np.exp(-16.67 / tau_ms)
        out = np.zeros_like(signal)
        out[0] = signal[0]
        for i in range(1, len(signal)):
            out[i] = alpha * signal[i] + (1 - alpha) * out[i - 1]
        return out

    def causal_vel(signal):
        v = np.zeros_like(signal)
        v[1:] = signal[1:] - signal[:-1]
        return v * fps

    print(f"\n  {'Method':<40} {'n':>4} {'Median':>8} {'IQR':>8} {'±40ms':>10} {'±80ms':>10}")
    print("  " + "-" * 90)

    results = {}
    for tau in [20, 30, 40]:
        wy_ema = causal_ema(wy, tau)
        vel = causal_vel(wy_ema)

        offsets = []
        for e in shot_edges:
            lo = e
            hi = min(len(vel), e + SEARCH_AFTER)
            if hi - lo < 4:
                continue
            seg_vel = vel[lo:hi]
            crossings = []
            for i in range(len(seg_vel) - 1):
                if seg_vel[i] < 0 and seg_vel[i + 1] >= 0:
                    frac = -seg_vel[i] / (seg_vel[i + 1] - seg_vel[i] + 1e-10)
                    cross_frame = lo + i + frac
                    crossings.append(cross_frame)
            if crossings:
                offsets.append((crossings[0] - e) * ms)

        label = f"Causal EMA τ={tau}ms (strong model)"
        if len(offsets) >= 5:
            a = np.array(sorted(offsets))
            med = np.median(a)
            iqr = np.percentile(a, 75) - np.percentile(a, 25)
            w40 = sum(1 for x in offsets if abs(x - med) <= 40)
            w80 = sum(1 for x in offsets if abs(x - med) <= 80)
            print(f"  {label:<40} {len(offsets):>4} {med:>+7.1f}ms {iqr:>7.1f}ms {f'{w40}/{len(offsets)}':>10} {f'{w80}/{len(offsets)}':>10}")
            results[label] = {"n": len(offsets), "median": med, "iqr": iqr, "w40": w40, "w80": w80}

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("count", type=int, nargs="?", default=3000)
    ap.add_argument("start", type=int, nargs="?", default=11940)
    ap.add_argument("handed", nargs="?", default="Right")
    ap.add_argument("--meter_color", default="Red")
    ap.add_argument("--model", default=os.path.join(ROOT, "models", "orion_pose2k_s.pt"))
    args = ap.parse_args()

    print(f"STRONG MODEL ZERO-CROSSING TEST")
    print(f"Model: {args.model}")
    print(f"{'='*70}")

    results = process_clip(args.video, args.count, args.start, args.handed, args.meter_color, args.model)

    print(f"\n{'='*70}")
    if results:
        best_iqr = min((r["iqr"] for r in results.values()), default=9999)
        print(f"  Best IQR with strong model: {best_iqr:.1f}ms")
        if best_iqr < 80:
            print(f"  → SUB-80ms confirmed with stronger model!")


if __name__ == "__main__":
    main()
