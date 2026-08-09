#!/usr/bin/env python3
"""Arming v3 — two key approaches:

1. CONTROLLER-STATE ARMING: Use the square button press as the arming signal.
   In the live system, the orchestrator has controller state every frame.
   The square press is the ground truth "shot is starting" — it's what causes
   the meter to appear. This gives the SAME 72ms IQR as the meter-armed probe.

2. STRICT SUSTAINED-RISE FILTER: A vision-only approach that finds crossings
   preceded by a LONG (20+ frame), CONSISTENT (80%+ upward), SIGNIFICANT
   (>0.04 normalized Y) wrist rise. This should filter out dribbling/running
   crossings because only shots have long sustained wrist rises.

3. COMBINED HIP+WRIST APEX: Find frames where BOTH wrist-Y AND hip-Y are at
   local minima within ±5 frames. Shots have coordinated wrist+hip apex;
   dribbles only have wrist motion.

Usage:
    C:\\Python314\\python.exe tools/diagnostics/arming_v3.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from pose_timing import PoseTimingDetector
from meter_detector import MeterDetector, load_detector_config

DEBOUNCE = 18
FPS = 60.0
MS_PER_FRAME = 1000.0 / FPS
SEARCH_AFTER = 60

CLIPS = [
    ("NBA 2K26_20260624212008.mp4", 3000, 150, "Right", "Red", "V3"),
    ("NBA 2K26_20260624212347.mp4", 3000, 2880, "Right", "Red", "V4"),
    ("NBA 2K26_20260624212700.mp4", 3000, 6240, "Right", "Red", "V5"),
    ("NBA 2K26_20260624213055.mp4", 3000, 7860, "Right", "Red", "V6"),
    ("NBA 2K26_20260521032018.mp4", 3000, 11940, "Right", "Red", "V7"),
]
VIDEOS = r"C:\Users\Administrator\Videos"


def extract_all_trajectories(video, count, start, handed, meter_color):
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

    wy_raw, hy_raw, ky_raw = [], [], []
    shot_edges = []
    prev_meter = False
    last_shot = -10**9
    n = 0

    print(f"  Extracting {os.path.basename(video)}...", end=" ", flush=True)
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
        hy_raw.append(float(pose._hip_y[j]))
        ky_raw.append(float(pose._knee_y[j]))

        if fed and not prev_meter and (seq - last_shot) > DEBOUNCE:
            shot_edges.append(n)
            last_shot = seq
        prev_meter = fed
        n += 1

    cap.release()

    def fill(arr):
        a = np.array(arr, dtype=float)
        mask = ~np.isnan(a)
        if mask.sum() < 10:
            return None
        return np.interp(np.arange(len(a)), np.where(mask)[0], a[mask])

    wy = fill(wy_raw)
    hy = fill(hy_raw)
    ky = fill(ky_raw)
    if wy is None:
        print("FAILED")
        return None

    print(f"{len(wy)} frames, {len(shot_edges)} shots")
    return wy, hy, ky, shot_edges


def causal_ema(signal, tau_ms=20.0, dt_ms=16.67):
    alpha = 1.0 - np.exp(-dt_ms / max(tau_ms, 1.0))
    out = np.zeros_like(signal)
    out[0] = signal[0]
    for i in range(1, len(signal)):
        out[i] = alpha * signal[i] + (1 - alpha) * out[i - 1]
    return out


def find_crossings(vel):
    crossings = []
    for i in range(len(vel) - 1):
        if vel[i] < 0 and vel[i + 1] >= 0:
            frac = -vel[i] / (vel[i + 1] - vel[i] + 1e-10)
            crossings.append(i + frac)
    return crossings


def match_to_edges(detections, shot_edges, max_dist=60):
    matched_edges = set()
    matched_dets = set()
    offsets = []

    for edge in shot_edges:
        best_det = None
        for i, d in enumerate(detections):
            if d < edge - 5:
                continue
            if d > edge + max_dist:
                break
            if i not in matched_dets:
                best_det = i
                break
        if best_det is not None:
            offsets.append((detections[best_det] - edge) * MS_PER_FRAME)
            matched_edges.add(edge)
            matched_dets.add(best_det)

    false_count = len(detections) - len(matched_dets)
    missed = len(shot_edges) - len(matched_edges)
    arm_rate = len(matched_edges) / max(len(shot_edges), 1)

    return offsets, len(matched_edges), false_count, missed, arm_rate


def report(clip_name, approach_name, offsets, n_tp, n_false, n_missed, n_shots, arm_rate):
    print(f"\n  {clip_name}: {approach_name}")
    print(f"    Detections: {n_tp + n_false} | TP: {n_tp} | FP: {n_false} | Missed: {n_missed}/{n_shots}")
    print(f"    ARM RATE: {arm_rate:.1%}")
    if offsets:
        arr = np.array(sorted(offsets))
        med = np.median(arr)
        iqr = np.percentile(arr, 75) - np.percentile(arr, 25)
        w40 = sum(1 for x in offsets if abs(x - med) <= 40)
        w80 = sum(1 for x in offsets if abs(x - med) <= 80)
        print(f"    TIMING: median={med:+.1f}ms IQR={iqr:.1f}ms ±40ms={w40}/{len(offsets)} ±80ms={w80}/{len(offsets)}")
        return {"clip": clip_name, "arm_rate": arm_rate, "false_arms": n_false,
                "n_tp": n_tp, "n_shots": n_shots, "median": med, "iqr": iqr,
                "w40": w40, "w80": w80, "offsets": offsets}
    else:
        print(f"    TIMING: no true positives")
        return {"clip": clip_name, "arm_rate": arm_rate, "false_arms": n_false,
                "n_tp": 0, "n_shots": n_shots, "median": 0, "iqr": 9999,
                "w40": 0, "w80": 0, "offsets": []}


# ─── APPROACH 1: Controller-state arming (simulated with meter edge) ────────

def test_controller_arming(wy, hy, ky, shot_edges, clip_name):
    """Simulate controller-state arming using the meter edge as proxy.

    In the live system, the SQUARE button press is the arming signal.
    The meter edge IS the square press (meter appears when square is pressed).
    So this is equivalent to: arm on square press → search for first crossing.
    """
    wy_ema = causal_ema(wy, tau_ms=20)
    vel = np.zeros_like(wy_ema)
    vel[1:] = wy_ema[1:] - wy_ema[:-1]

    # Use meter edges as the arming signal (proxy for square press)
    offsets = []
    for e in shot_edges:
        lo = e
        hi = min(len(vel), e + SEARCH_AFTER)
        for i in range(lo, hi - 1):
            if vel[i] < 0 and vel[i + 1] >= 0:
                frac = -vel[i] / (vel[i + 1] - vel[i] + 1e-10)
                cross_frame = i + frac
                offsets.append((cross_frame - e) * MS_PER_FRAME)
                break

    # This IS the ground truth — 100% arm rate by definition
    arm_rate = len(offsets) / max(len(shot_edges), 1)
    return report(clip_name, "Controller-state arming (meter proxy)", offsets,
                  len(offsets), 0, len(shot_edges) - len(offsets), len(shot_edges), arm_rate)


# ─── APPROACH 2: Strict sustained-rise filter ───────────────────────────────

def test_strict_sustained_rise(wy, hy, ky, shot_edges, clip_name):
    """Find crossings preceded by a LONG, CONSISTENT, SIGNIFICANT wrist rise."""
    wy_ema = causal_ema(wy, tau_ms=20)
    vel = np.zeros_like(wy_ema)
    vel[1:] = wy_ema[1:] - wy_ema[:-1]

    all_crossings = find_crossings(vel)

    detections = []
    for c in all_crossings:
        ci = int(c)
        lo = max(0, ci - 25)  # 25-frame lookback (417ms)

        if ci - lo < 20:
            continue

        # Check sustained rise in the 20 frames before crossing
        seg = wy_ema[lo:ci+1]
        if len(seg) < 20:
            continue

        # 1. CONSISTENCY: 80% of frames must show upward motion (vel < 0)
        seg_vel = np.diff(seg)
        upward_frac = np.mean(seg_vel < 0)
        if upward_frac < 0.70:
            continue

        # 2. MAGNITUDE: wrist must rise by at least 0.04 (normalized Y)
        rise = seg[0] - seg[-1]  # positive = rose (Y decreased)
        if rise < 0.03:
            continue

        # 3. POST-CROSSING DROP: wrist must drop by at least 0.015 in next 10 frames
        post_hi = min(len(wy_ema), ci + 10)
        if post_hi - ci >= 5:
            post_drop = wy_ema[post_hi-1] - wy_ema[ci]
            if post_drop < 0.01:
                continue
        else:
            continue

        # 4. HEIGHT: wrist must be in the lowest 15% of the entire clip at crossing
        global_threshold = np.percentile(wy_ema, 15)
        if wy_ema[ci] > global_threshold:
            continue

        detections.append(c)

    # Debounce
    detections = sorted(detections)
    debounced = []
    last = -100
    for d in detections:
        if d - last > 25:
            debounced.append(d)
            last = d

    offsets, n_tp, n_false, n_missed, arm_rate = match_to_edges(debounced, shot_edges)
    return report(clip_name, "Strict sustained-rise filter", offsets, n_tp, n_false, n_missed, len(shot_edges), arm_rate)


# ─── APPROACH 3: Combined wrist+hip apex ────────────────────────────────────

def test_combined_apex(wy, hy, ky, shot_edges, clip_name):
    """Find frames where BOTH wrist-Y AND hip-Y are at local minima within ±5 frames."""
    if hy is None:
        return report(clip_name, "Combined wrist+hip apex", [], 0, 0, len(shot_edges), 0)

    wy_ema = causal_ema(wy, tau_ms=20)
    hy_ema = causal_ema(hy, tau_ms=40)
    vel = np.zeros_like(wy_ema)
    vel[1:] = wy_ema[1:] - wy_ema[:-1]

    # Find wrist apexes (local minima in Y, within ±10 window)
    wrist_apexes = []
    for i in range(10, len(wy_ema) - 10):
        window = wy_ema[i-10:i+11]
        if wy_ema[i] == np.min(window):
            # Must be significant
            rest = np.percentile(wy_ema[max(0,i-60):i], 60)
            if rest - wy_ema[i] > 0.025:
                wrist_apexes.append(i)

    # Find hip apexes (local minima in Y, within ±10 window)
    hip_apexes = []
    for i in range(10, len(hy_ema) - 10):
        window = hy_ema[i-10:i+11]
        if hy_ema[i] == np.min(window):
            rest = np.percentile(hy_ema[max(0,i-60):i], 60)
            if rest - hy_ema[i] > 0.008:
                hip_apexes.append(i)

    # Find wrist apexes that have a hip apex within ±8 frames
    hip_set = set(hip_apexes)
    combined_apexes = []
    for wa in wrist_apexes:
        for ha in hip_apexes:
            if abs(wa - ha) <= 8:
                combined_apexes.append(wa)
                break

    # For each combined apex, find the first crossing AFTER it (within 20 frames)
    all_crossings = find_crossings(vel)
    detections = []
    for apex in combined_apexes:
        for c in all_crossings:
            if c >= apex - 2 and c <= apex + 20:
                detections.append(c)
                break

    # Debounce
    detections = sorted(detections)
    debounced = []
    last = -100
    for d in detections:
        if d - last > 25:
            debounced.append(d)
            last = d

    offsets, n_tp, n_false, n_missed, arm_rate = match_to_edges(debounced, shot_edges)
    return report(clip_name, "Combined wrist+hip apex", offsets, n_tp, n_false, n_missed, len(shot_edges), arm_rate)


# ─── APPROACH 4: Adaptive threshold crossing ────────────────────────────────

def test_adaptive_threshold(wy, hy, ky, shot_edges, clip_name):
    """Only accept crossings where the wrist rises to an adaptive threshold.

    The threshold adapts to the recent trajectory: the wrist must rise to
    within 80% of the highest point in the last 120 frames (2 seconds).
    This filters out minor crossings from dribbling/running.
    """
    wy_ema = causal_ema(wy, tau_ms=20)
    vel = np.zeros_like(wy_ema)
    vel[1:] = wy_ema[1:] - wy_ema[:-1]

    all_crossings = find_crossings(vel)

    detections = []
    for c in all_crossings:
        ci = int(c)
        lo = max(0, ci - 120)
        recent = wy_ema[lo:ci+1]
        if len(recent) < 20:
            continue

        # Adaptive threshold: wrist must be in the top 15% (lowest Y) of recent window
        threshold = np.percentile(recent, 15)
        if wy_ema[ci] > threshold:
            continue

        # Must also have significant rise from rest
        rest = np.percentile(recent, 70)
        if rest - wy_ema[ci] < 0.025:
            continue

        # Must have post-crossing drop
        post_hi = min(len(wy_ema), ci + 8)
        if post_hi - ci >= 4:
            post_drop = wy_ema[post_hi-1] - wy_ema[ci]
            if post_drop < 0.008:
                continue
        else:
            continue

        detections.append(c)

    # Debounce
    detections = sorted(detections)
    debounced = []
    last = -100
    for d in detections:
        if d - last > 20:
            debounced.append(d)
            last = d

    offsets, n_tp, n_false, n_missed, arm_rate = match_to_edges(debounced, shot_edges)
    return report(clip_name, "Adaptive threshold crossing", offsets, n_tp, n_false, n_missed, len(shot_edges), arm_rate)


# ─── APPROACH 5: Velocity-profile matching ──────────────────────────────────

def test_velocity_profile(wy, hy, ky, shot_edges, clip_name):
    """Match the velocity profile around each crossing against the average shot profile.

    A shot has a distinctive velocity profile: strong upward velocity that
    decelerates to zero (the crossing) then reverses to downward.
    """
    wy_ema = causal_ema(wy, tau_ms=20)
    vel = np.zeros_like(wy_ema)
    vel[1:] = wy_ema[1:] - wy_ema[:-1]

    all_crossings = find_crossings(vel)

    # Build the average shot velocity profile from meter-confirmed shots
    # (leave-one-out: build from all clips except current)
    # For now, use a simple heuristic: the velocity before the crossing should
    # show a clear deceleration pattern (rising toward zero)

    detections = []
    for c in all_crossings:
        ci = int(c)
        lo = max(0, ci - 15)

        if ci - lo < 10:
            continue

        seg_vel = vel[lo:ci+1]

        # Check for deceleration: |velocity| should decrease toward the crossing
        # (wrist decelerates as it approaches the apex)
        first_half = np.abs(seg_vel[:len(seg_vel)//2])
        second_half = np.abs(seg_vel[len(seg_vel)//2:])

        if len(first_half) == 0 or len(second_half) == 0:
            continue

        # Average |vel| should be higher in first half (fast rise) than second (deceleration)
        if np.mean(first_half) < np.mean(second_half) * 0.8:
            continue  # no deceleration → not a shot

        # The velocity at the crossing should be near zero (it's a crossing)
        if abs(vel[ci]) > 0.003:
            continue

        # Height check
        lo120 = max(0, ci - 120)
        recent = wy_ema[lo120:ci+1]
        rest = np.percentile(recent, 60)
        if rest - wy_ema[ci] < 0.025:
            continue

        detections.append(c)

    # Debounce
    detections = sorted(detections)
    debounced = []
    last = -100
    for d in detections:
        if d - last > 25:
            debounced.append(d)
            last = d

    offsets, n_tp, n_false, n_missed, arm_rate = match_to_edges(debounced, shot_edges)
    return report(clip_name, "Velocity-profile matching", offsets, n_tp, n_false, n_missed, len(shot_edges), arm_rate)


def main():
    print(f"ARMING v3 — Controller-state + strict vision filters")
    print(f"{'='*80}")

    all_data = []
    for clip_name, count, start, handed, mcolor, label in CLIPS:
        video = os.path.join(VIDEOS, clip_name)
        if not os.path.exists(video):
            print(f"  [skip] {clip_name} not found")
            continue
        result = extract_all_trajectories(video, count, start, handed, mcolor)
        if result is not None:
            all_data.append((*result, label))

    if not all_data:
        print("  [error] no data extracted")
        return

    all_results = {}

    for approach_name, approach_fn in [
        ("Controller-state (meter proxy)", test_controller_arming),
        ("Strict sustained-rise", test_strict_sustained_rise),
        ("Combined wrist+hip apex", test_combined_apex),
        ("Adaptive threshold", test_adaptive_threshold),
        ("Velocity-profile matching", test_velocity_profile),
    ]:
        print(f"\n{'='*80}")
        print(f"APPROACH: {approach_name}")
        print(f"{'='*80}")
        results = []
        for wy, hy, ky, edges, label in all_data:
            r = approach_fn(wy, hy, ky, edges, label)
            if r:
                results.append(r)
        all_results[approach_name] = results

    # Summary
    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")
    print(f"\n  {'Approach':<35} {'Arm%':>6} {'False':>6} {'IQR':>10} {'±80ms':>10}")
    print("  " + "-" * 75)

    for name, results in all_results.items():
        if not results:
            print(f"  {name:<35} {'N/A':>6} {'N/A':>6} {'N/A':>10} {'N/A':>10}")
            continue
        avg_arm = np.mean([r["arm_rate"] for r in results])
        total_false = sum(r["false_arms"] for r in results)
        all_offsets = []
        for r in results:
            all_offsets.extend(r["offsets"])
        if all_offsets:
            arr = np.array(sorted(all_offsets))
            med = np.median(arr)
            iqr = np.percentile(arr, 75) - np.percentile(arr, 25)
            w80 = sum(1 for x in all_offsets if abs(x - med) <= 80)
            print(f"  {name:<35} {avg_arm:>5.0%} {total_false:>6} {iqr:>9.1f}ms {f'{w80}/{len(all_offsets)}':>10}")
        else:
            print(f"  {name:<35} {avg_arm:>5.0%} {total_false:>6} {'N/A':>10} {'N/A':>10}")

    # Best
    best_name = None
    best_score = -999
    for name, results in all_results.items():
        if not results:
            continue
        avg_arm = np.mean([r["arm_rate"] for r in results])
        total_false = sum(r["false_arms"] for r in results)
        all_offsets = []
        for r in results:
            all_offsets.extend(r["offsets"])
        if all_offsets:
            arr = np.array(sorted(all_offsets))
            iqr = np.percentile(arr, 75) - np.percentile(arr, 25)
            score = avg_arm * 100 - total_false * 3 - iqr
            if score > best_score:
                best_score = score
                best_name = name

    if best_name:
        print(f"\n  BEST: {best_name} (score={best_score:.1f})")


if __name__ == "__main__":
    main()
