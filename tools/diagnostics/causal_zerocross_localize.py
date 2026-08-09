#!/usr/bin/env python3
"""Causal sub-frame zero-crossing detector — tests whether the sub-frame
velocity zero-crossing can be detected in REAL-TIME (only past data).

The cubic spline in subframe_interp_localize.py uses future data (non-causal).
This test uses a CAUSAL approach:
1. Track wrist-Y with a causal EMA
2. Compute velocity from the EMA
3. When velocity changes sign (neg→pos = release), linearly interpolate
   between the last two frames for sub-frame timing
4. Detection delay = 1 frame (16ms) — we detect the crossing one frame after

Also tests a PREDICTIVE version: extrapolate the velocity from the last
N frames to predict the crossing BEFORE it happens (0-frame delay).

Usage:
    C:\\Python314\\python.exe tools/diagnostics/causal_zerocross_localize.py "<video>" [count] [start] [handed] [--meter_color Red]
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
SEARCH_AFTER = 60  # frames after meter edge to search


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

    # Prepare trajectory
    wy = np.array(wy_raw, dtype=float)
    mask = ~np.isnan(wy)
    if mask.sum() < 10:
        print("  [error] not enough valid wrist data")
        return
    wy = np.interp(np.arange(len(wy)), np.where(mask)[0], wy[mask])

    # === Causal EMA smoothing ===
    # Simulate real-time EMA with different time constants
    def causal_ema(signal, tau_ms):
        alpha = 1.0 - np.exp(-16.67 / tau_ms)  # 60fps frame time
        out = np.zeros_like(signal)
        out[0] = signal[0]
        for i in range(1, len(signal)):
            out[i] = alpha * signal[i] + (1 - alpha) * out[i - 1]
        return out

    def causal_vel(signal):
        # Simple difference (causal)
        v = np.zeros_like(signal)
        v[1:] = signal[1:] - signal[:-1]
        return v * fps  # units/sec

    # Test different EMA time constants
    tau_values = [20, 30, 40, 50, 60, 80]

    print(f"\n  {'Method':<40} {'n':>4} {'Median':>8} {'IQR':>8} {'±40ms':>10} {'±80ms':>10}")
    print("  " + "-" * 90)

    results = {}

    for tau in tau_values:
        wy_ema = causal_ema(wy, tau)
        vel = causal_vel(wy_ema)

        # Method A: Causal linear interpolation
        # When velocity crosses zero (neg→pos), linearly interpolate between last two frames
        offsets_causal = []
        for e in shot_edges:
            lo = e
            hi = min(len(vel), e + SEARCH_AFTER)
            if hi - lo < 4:
                continue
            seg_vel = vel[lo:hi]

            crossings = []
            for i in range(len(seg_vel) - 1):
                if seg_vel[i] < 0 and seg_vel[i + 1] >= 0:
                    # Linear interpolation for zero crossing
                    frac = -seg_vel[i] / (seg_vel[i + 1] - seg_vel[i] + 1e-10)
                    cross_frame = lo + i + frac
                    crossings.append(cross_frame)

            if crossings:
                offset = (crossings[0] - e) * ms
                offsets_causal.append(offset)

        label = f"Causal EMA τ={tau}ms linear-interp"
        if len(offsets_causal) >= 5:
            a = np.array(sorted(offsets_causal))
            med = np.median(a)
            iqr = np.percentile(a, 75) - np.percentile(a, 25)
            w40 = sum(1 for x in offsets_causal if abs(x - med) <= 40)
            w80 = sum(1 for x in offsets_causal if abs(x - med) <= 80)
            print(f"  {label:<40} {len(offsets_causal):>4} {med:>+7.1f}ms {iqr:>7.1f}ms {f'{w40}/{len(offsets_causal)}':>10} {f'{w80}/{len(offsets_causal)}':>10}")
            results[label] = {"n": len(offsets_causal), "median": med, "iqr": iqr, "w40": w40, "w80": w80}

    # Method B: Predictive — extrapolate velocity from last N frames to predict crossing
    # When velocity is negative (upward) and decelerating, predict when it will cross zero
    for tau in [30, 40, 50]:
        wy_ema = causal_ema(wy, tau)
        vel = causal_vel(wy_ema)

        offsets_predict = []
        for e in shot_edges:
            lo = e
            hi = min(len(vel), e + SEARCH_AFTER)
            if hi - lo < 8:
                continue
            seg_vel = vel[lo:hi]

            # Find the frame where velocity is most negative (peak upward)
            # Then predict when it will cross zero using linear extrapolation
            min_idx = np.argmin(seg_vel[:min(20, len(seg_vel))])
            if min_idx < 2:
                continue

            # Use the last 3 frames of velocity to fit a line and predict zero crossing
            predicted_cross = None
            for i in range(min_idx + 1, min(len(seg_vel) - 1, min_idx + 20)):
                # Check if velocity is still negative
                if seg_vel[i] >= 0:
                    # Already crossed — use linear interpolation
                    frac = -seg_vel[i - 1] / (seg_vel[i] - seg_vel[i - 1] + 1e-10)
                    predicted_cross = lo + i - 1 + frac
                    break

                # Extrapolate from last 3 frames
                if i >= 3:
                    recent = seg_vel[i - 3:i + 1]
                    x = np.arange(4)
                    if np.std(recent) > 1e-8:
                        coeffs = np.polyfit(x, recent, 1)
                        if coeffs[0] > 0:  # velocity is increasing (toward zero)
                            # Predict when velocity = 0
                            # 0 = coeffs[0] * t + coeffs[1]
                            t = -coeffs[1] / coeffs[0]
                            if 0 < t < 5:  # prediction within 5 frames
                                predicted_cross = lo + i - 3 + t
                                break

            if predicted_cross is not None:
                offset = (predicted_cross - e) * ms
                offsets_predict.append(offset)

        label = f"Predictive EMA τ={tau}ms (3-frame extrap)"
        if len(offsets_predict) >= 5:
            a = np.array(sorted(offsets_predict))
            med = np.median(a)
            iqr = np.percentile(a, 75) - np.percentile(a, 25)
            w40 = sum(1 for x in offsets_predict if abs(x - med) <= 40)
            w80 = sum(1 for x in offsets_predict if abs(x - med) <= 80)
            print(f"  {label:<40} {len(offsets_predict):>4} {med:>+7.1f}ms {iqr:>7.1f}ms {f'{w40}/{len(offsets_predict)}':>10} {f'{w80}/{len(offsets_predict)}':>10}")
            results[label] = {"n": len(offsets_predict), "median": med, "iqr": iqr, "w40": w40, "w80": w80}

    # Method C: Causal quadratic fit — fit a parabola to the last 5 frames of velocity
    # and find the zero crossing
    for tau in [40, 50]:
        wy_ema = causal_ema(wy, tau)
        vel = causal_vel(wy_ema)

        offsets_quad = []
        for e in shot_edges:
            lo = e
            hi = min(len(vel), e + SEARCH_AFTER)
            if hi - lo < 8:
                continue
            seg_vel = vel[lo:hi]

            # Find peak upward velocity
            min_idx = np.argmin(seg_vel[:min(20, len(seg_vel))])
            if min_idx < 4:
                continue

            # Scan forward from the peak, fitting a quadratic to 5-frame windows
            predicted = None
            for i in range(min_idx, min(len(seg_vel) - 5, min_idx + 20)):
                if seg_vel[i] >= 0:
                    # Already crossed
                    if i > 0 and seg_vel[i - 1] < 0:
                        frac = -seg_vel[i - 1] / (seg_vel[i] - seg_vel[i - 1] + 1e-10)
                        predicted = lo + i - 1 + frac
                    break

                # Fit quadratic to last 5 frames
                if i >= 4:
                    window = seg_vel[i - 4:i + 1]
                    x = np.arange(5)
                    coeffs = np.polyfit(x, window, 2)
                    # Find roots of the quadratic
                    roots = np.roots(coeffs)
                    real_roots = [r.real for r in roots if abs(r.imag) < 1e-6 and r.real > 3 and r.real < 10]
                    if real_roots:
                        predicted = lo + i - 4 + min(real_roots)
                        break

            if predicted is not None:
                offset = (predicted - e) * ms
                offsets_quad.append(offset)

        label = f"Causal EMA τ={tau}ms (5-frame quad)"
        if len(offsets_quad) >= 5:
            a = np.array(sorted(offsets_quad))
            med = np.median(a)
            iqr = np.percentile(a, 75) - np.percentile(a, 25)
            w40 = sum(1 for x in offsets_quad if abs(x - med) <= 40)
            w80 = sum(1 for x in offsets_quad if abs(x - med) <= 80)
            print(f"  {label:<40} {len(offsets_quad):>4} {med:>+7.1f}ms {iqr:>7.1f}ms {f'{w40}/{len(offsets_quad)}':>10} {f'{w80}/{len(offsets_quad)}':>10}")
            results[label] = {"n": len(offsets_quad), "median": med, "iqr": iqr, "w40": w40, "w80": w80}

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("count", type=int, nargs="?", default=3000)
    ap.add_argument("start", type=int, nargs="?", default=11940)
    ap.add_argument("handed", nargs="?", default="Right")
    ap.add_argument("--meter_color", default="Red")
    args = ap.parse_args()

    print(f"CAUSAL SUB-FRAME ZERO-CROSSING LOCALIZABILITY TEST")
    print(f"Tests real-time (causal) detection of the velocity zero-crossing")
    print(f"{'='*70}")

    results = process_clip(args.video, args.count, args.start, args.handed, args.meter_color)

    print(f"\n{'='*70}")
    if results:
        best_iqr = min((r["iqr"] for r in results.values()), default=9999)
        best_name = min(results, key=lambda k: results[k]["iqr"])
        print(f"  Best causal: {best_name}")
        print(f"  Best IQR: {best_iqr:.1f}ms")
        if best_iqr < 80:
            print(f"  → SUB-80ms with CAUSAL detection! This is LIVE-VIABLE!")
        elif best_iqr < 120:
            print(f"  → Under 120ms — viable with input hook + prediction")
        else:
            print(f"  → Causal detection too noisy — need non-causal or frame interpolation")


if __name__ == "__main__":
    main()
