#!/usr/bin/env python3
"""Apex-anchored arming v2 — smarter approaches to the shot-start problem.

Key insight from v1: all approaches had good arm rates (71-93%) but terrible IQR
(322-458ms) because they fired at variable times and caught random crossings.

New approaches:
1. APEX-ANCHORED: Find significant wrist apexes (high above rest), then take the
   first neg→pos crossing AFTER each apex. Apexes are rare and shot-specific.
2. HEIGHT-THRESHOLD CROSSING: Only accept crossings where wrist-Y is in the top
   20% of recent positions (filtering out dribbling/running crossings).
3. ENERGY-GATED CROSSING: Only accept crossings where total kinetic energy
   (wrist+hip+knee velocity) in preceding 30 frames exceeds a threshold.
4. COMBINED: Apex + height + energy + gather — the full signature.
5. TEMPLATE MATCH: Match the wrist-Y trajectory around each crossing against
   the average shot template (from meter-confirmed shots).

Usage:
    C:\\Python314\\python.exe tools/diagnostics/arming_solutions_v2.py
"""
from __future__ import annotations

import os
import sys
import argparse

import numpy as np
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from pose_timing import PoseTimingDetector
from meter_detector import MeterDetector, load_detector_config

DEBOUNCE = 18
FPS = 60.0
MS_PER_FRAME = 1000.0 / FPS

CLIPS = [
    ("NBA 2K26_20260624212008.mp4", 3000, 150, "Right", "Red", "V3"),
    ("NBA 2K26_20260624212347.mp4", 3000, 2880, "Right", "Red", "V4"),
    ("NBA 2K26_20260624212700.mp4", 3000, 6240, "Right", "Red", "V5"),
    ("NBA 2K26_20260624213055.mp4", 3000, 7860, "Right", "Red", "V6"),
    ("NBA 2K26_20260521032018.mp4", 3000, 11940, "Right", "Red", "V7"),
]
VIDEOS = r"C:\Users\Administrator\Videos"


def extract_all_trajectories(video, count, start, handed, meter_color):
    """Extract wrist-Y, hip-Y, knee-Y trajectories and meter shot edges."""
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
    """Find all neg→pos velocity crossings."""
    crossings = []
    for i in range(len(vel) - 1):
        if vel[i] < 0 and vel[i + 1] >= 0:
            frac = -vel[i] / (vel[i + 1] - vel[i] + 1e-10)
            crossings.append(i + frac)
    return crossings


def match_to_edges(detections, shot_edges, max_dist=60):
    """Match detections to shot edges. Returns (offsets_ms, n_matched, n_false, n_missed)."""
    matched_edges = set()
    matched_dets = set()
    offsets = []

    # For each edge, find the first detection after it within max_dist
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


# ─── APPROACH 1: Apex-anchored crossing ─────────────────────────────────────

def test_apex_anchored(wy, hy, ky, shot_edges, clip_name):
    """Find significant wrist apexes, then take first crossing after each."""
    wy_ema = causal_ema(wy, tau_ms=20)
    vel = np.zeros_like(wy_ema)
    vel[1:] = wy_ema[1:] - wy_ema[:-1]

    # Find local minima in wy_ema (apex = highest point = min Y)
    # Use a window-based approach: an apex is a point where Y is lower than
    # all points within ±15 frames
    all_crossings = find_crossings(vel)

    # Find significant apexes
    apexes = []
    rest_level = np.percentile(wy_ema, 60)  # "rest" is roughly the median position
    min_apex_height = 0.03  # wrist must rise at least this much above rest

    for i in range(15, len(wy_ema) - 15):
        window = wy_ema[i-15:i+16]
        if wy_ema[i] == np.min(window):
            apex_height = rest_level - wy_ema[i]
            if apex_height > min_apex_height:
                apexes.append(i)

    # For each apex, find the first neg→pos crossing AFTER it (within 30 frames)
    detections = []
    for apex in apexes:
        for c in all_crossings:
            if c >= apex and c <= apex + 30:
                detections.append(c)
                break

    # Debounce detections (min 25 frames apart)
    detections = sorted(detections)
    debounced = []
    last = -100
    for d in detections:
        if d - last > 25:
            debounced.append(d)
            last = d

    offsets, n_tp, n_false, n_missed, arm_rate = match_to_edges(debounced, shot_edges)
    return report(clip_name, "Apex-anchored crossing", offsets, n_tp, n_false, n_missed, len(shot_edges), arm_rate)


# ─── APPROACH 2: Height-threshold crossing ──────────────────────────────────

def test_height_threshold(wy, hy, ky, shot_edges, clip_name):
    """Only accept crossings where wrist-Y is in the top 20% of recent positions."""
    wy_ema = causal_ema(wy, tau_ms=20)
    vel = np.zeros_like(wy_ema)
    vel[1:] = wy_ema[1:] - wy_ema[:-1]

    all_crossings = find_crossings(vel)

    # Filter: at the crossing, wrist-Y must be in the top 20% (lowest Y) of the
    # preceding 120 frames (2 seconds)
    detections = []
    for c in all_crossings:
        ci = int(c)
        lo = max(0, ci - 120)
        recent = wy_ema[lo:ci+1]
        if len(recent) < 20:
            continue
        threshold = np.percentile(recent, 20)  # 20th percentile = low Y = high position
        if wy_ema[ci] <= threshold:
            # Also require significant rise: wrist must be at least 0.03 above rest
            rest = np.percentile(recent, 60)
            if rest - wy_ema[ci] > 0.02:
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
    return report(clip_name, "Height-threshold crossing", offsets, n_tp, n_false, n_missed, len(shot_edges), arm_rate)


# ─── APPROACH 3: Energy-gated crossing ──────────────────────────────────────

def test_energy_gated(wy, hy, ky, shot_edges, clip_name):
    """Only accept crossings where total kinetic energy in preceding 30 frames is high."""
    wy_ema = causal_ema(wy, tau_ms=20)
    vel = np.zeros_like(wy_ema)
    vel[1:] = wy_ema[1:] - wy_ema[:-1]

    hy_ema = causal_ema(hy, tau_ms=30) if hy is not None else np.zeros_like(wy)
    ky_ema = causal_ema(ky, tau_ms=30) if ky is not None else np.zeros_like(wy)

    hy_vel = np.zeros_like(hy_ema)
    hy_vel[1:] = hy_ema[1:] - hy_ema[:-1]
    ky_vel = np.zeros_like(ky_ema)
    ky_vel[1:] = ky_ema[1:] - ky_ema[:-1]

    all_crossings = find_crossings(vel)

    # Compute energy = sum of |velocity| over preceding 30 frames for all joints
    detections = []
    for c in all_crossings:
        ci = int(c)
        lo = max(0, ci - 30)
        wy_energy = np.sum(np.abs(vel[lo:ci+1]))
        hy_energy = np.sum(np.abs(hy_vel[lo:ci+1]))
        ky_energy = np.sum(np.abs(ky_vel[lo:ci+1]))
        total_energy = wy_energy + hy_energy + ky_energy

        # Threshold: must be in the top 30% of energy across all crossings
        # (compute dynamically)
        detections.append((c, total_energy))

    if not detections:
        return report(clip_name, "Energy-gated crossing", [], 0, 0, len(shot_edges), 0)

    # Compute energy threshold
    energies = [e for _, e in detections]
    energy_threshold = np.percentile(energies, 60)  # top 40% by energy

    # Filter by energy AND height
    filtered = []
    for c, e in detections:
        if e >= energy_threshold:
            ci = int(c)
            lo = max(0, ci - 120)
            recent = wy_ema[lo:ci+1]
            if len(recent) >= 20:
                rest = np.percentile(recent, 60)
                if rest - wy_ema[ci] > 0.02:
                    filtered.append(c)

    # Debounce
    filtered = sorted(filtered)
    debounced = []
    last = -100
    for d in filtered:
        if d - last > 25:
            debounced.append(d)
            last = d

    offsets, n_tp, n_false, n_missed, arm_rate = match_to_edges(debounced, shot_edges)
    return report(clip_name, "Energy-gated crossing", offsets, n_tp, n_false, n_missed, len(shot_edges), arm_rate)


# ─── APPROACH 4: Combined signature (apex + height + energy + gather) ───────

def test_combined_signature(wy, hy, ky, shot_edges, clip_name):
    """Combined: apex detection + height threshold + energy gate + gather check."""
    wy_ema = causal_ema(wy, tau_ms=20)
    vel = np.zeros_like(wy_ema)
    vel[1:] = wy_ema[1:] - wy_ema[:-1]

    hy_ema = causal_ema(hy, tau_ms=40) if hy is not None else np.zeros_like(wy)
    ky_ema = causal_ema(ky, tau_ms=40) if ky is not None else np.zeros_like(wy)

    hy_vel = np.zeros_like(hy_ema)
    hy_vel[1:] = hy_ema[1:] - hy_ema[:-1]
    ky_vel = np.zeros_like(ky_ema)
    ky_vel[1:] = ky_ema[1:] - ky_ema[:-1]

    all_crossings = find_crossings(vel)

    # Compute all features for each crossing
    candidates = []
    for c in all_crossings:
        ci = int(c)
        lo = max(0, ci - 45)  # lookback for signature
        lo120 = max(0, ci - 120)  # lookback for rest level

        if ci - lo < 15:
            continue

        recent = wy_ema[lo120:ci+1]
        if len(recent) < 20:
            continue

        # Height: how high is the wrist at the crossing?
        rest = np.percentile(recent, 60)
        height = rest - wy_ema[ci]
        if height < 0.025:  # must be at least 2.5% above rest
            continue

        # Sustained rise: at least 50% of last 20 frames show upward motion
        wy_seg = wy_ema[lo:ci+1]
        if len(wy_seg) < 10:
            continue
        wy_v = np.diff(wy_seg)
        upward_frac = np.mean(wy_v < 0)
        if upward_frac < 0.4:
            continue

        # Energy: total kinetic energy in preceding 30 frames
        lo30 = max(0, ci - 30)
        wy_energy = np.sum(np.abs(vel[lo30:ci+1]))
        hy_energy = np.sum(np.abs(hy_vel[lo30:ci+1]))
        total_energy = wy_energy + hy_energy

        # Gather: hip crouch in preceding 45 frames
        gather_score = 0.0
        if hy is not None and len(hy_ema[lo:ci+1]) > 10:
            hy_seg = hy_ema[lo:ci+1]
            hy_range = np.max(hy_seg) - np.min(hy_seg)
            if hy_range > 0.005:
                crouch_idx = np.argmax(hy_seg)
                if crouch_idx < len(hy_seg) * 0.7:  # crouch before the crossing
                    gather_score = min(1.0, hy_range / 0.02)

        # Post-crossing drop: wrist should drop (Y increases) after crossing
        post_lo = ci
        post_hi = min(len(wy_ema), ci + 15)
        if post_hi - post_lo > 5:
            post_drop = wy_ema[post_hi-1] - wy_ema[post_lo]
        else:
            post_drop = 0

        # Combined score
        score = (height / 0.05 * 0.3 +
                 upward_frac * 0.2 +
                 min(1.0, total_energy / 0.5) * 0.2 +
                 gather_score * 0.15 +
                 min(1.0, post_drop / 0.02) * 0.15)

        candidates.append((c, score, height, upward_frac, gather_score, total_energy, post_drop))

    if not candidates:
        return report(clip_name, "Combined signature", [], 0, 0, len(shot_edges), 0)

    # Dynamic threshold: take crossings with score > 0.4
    filtered = [(c, s) for c, s, h, u, g, e, p in candidates if s > 0.4]

    # Debounce: keep highest-scored crossing in each 25-frame window
    filtered = sorted(filtered, key=lambda x: x[0])
    debounced = []
    last = -100
    for c, s in filtered:
        if c - last > 25:
            debounced.append(c)
            last = c
        # else skip — already have a detection in this window

    offsets, n_tp, n_false, n_missed, arm_rate = match_to_edges(debounced, shot_edges)
    return report(clip_name, "Combined signature", offsets, n_tp, n_false, n_missed, len(shot_edges), arm_rate)


# ─── APPROACH 5: Template-matched crossing ──────────────────────────────────

def test_template_match(wy, hy, ky, shot_edges, clip_name, templates=None):
    """Match wrist-Y trajectory around each crossing against the average shot template."""
    wy_ema = causal_ema(wy, tau_ms=20)
    vel = np.zeros_like(wy_ema)
    vel[1:] = wy_ema[1:] - wy_ema[:-1]

    all_crossings = find_crossings(vel)

    if templates is None or clip_name not in templates:
        return report(clip_name, "Template match (no template)", [], 0, 0, len(shot_edges), 0)

    template = templates[clip_name]  # (pre_30, post_10) normalized wrist-Y around the release

    detections = []
    for c in all_crossings:
        ci = int(c)
        lo = max(0, ci - 30)
        hi = min(len(wy_ema), ci + 10)
        if hi - lo < 35:
            continue

        seg = wy_ema[lo:hi]
        # Normalize
        seg_norm = (seg - np.mean(seg)) / (np.std(seg) + 1e-8)
        template_norm = (template - np.mean(template)) / (np.std(template) + 1e-8)

        # Pad if needed
        min_len = min(len(seg_norm), len(template_norm))
        corr = np.corrcoef(seg_norm[:min_len], template_norm[:min_len])[0, 1]

        if corr > 0.6:  # high correlation with shot template
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
    return report(clip_name, "Template match", offsets, n_tp, n_false, n_missed, len(shot_edges), arm_rate)


def build_templates(all_data):
    """Build average wrist-Y template around meter-confirmed releases for each clip."""
    templates = {}
    for wy, hy, ky, edges, label in all_data:
        wy_ema = causal_ema(wy, tau_ms=20)
        vel = np.zeros_like(wy_ema)
        vel[1:] = wy_ema[1:] - wy_ema[:-1]

        # Find the release crossing for each shot edge
        segments = []
        for edge in edges:
            for i in range(edge, min(edge + 60, len(vel) - 1)):
                if vel[i] < 0 and vel[i + 1] >= 0:
                    lo = max(0, i - 30)
                    hi = min(len(wy_ema), i + 10)
                    if hi - lo >= 35:
                        segments.append(wy_ema[lo:hi])
                    break

        if len(segments) >= 5:
            # Pad to same length and average
            min_len = min(len(s) for s in segments)
            padded = np.array([s[:min_len] for s in segments])
            templates[label] = np.mean(padded, axis=0)

    return templates


# ─── APPROACH 6: Meter-edge-like detection (wrist rise onset) ───────────────

def test_wrist_rise_onset(wy, hy, ky, shot_edges, clip_name):
    """Detect the onset of sustained wrist rise as the arming signal.

    This mimics the meter edge: the moment the wrist starts rising rapidly
    and sustainedly is the "shot is starting" signal.
    """
    wy_ema = causal_ema(wy, tau_ms=30)
    vel = np.zeros_like(wy_ema)
    vel[1:] = wy_ema[1:] - wy_ema[:-1]

    # Find onset of sustained rise: velocity goes from near-zero to
    # consistently negative (upward) for 10+ frames
    arm_frames = []
    last_arm = -100
    for i in range(10, len(vel) - 10):
        # Check if velocity in the next 10 frames is consistently upward
        future_vel = vel[i:i+10]
        if np.mean(future_vel < -0.0008) > 0.7:  # 70% of next 10 frames are upward
            # And the previous 10 frames were relatively flat
            past_vel = vel[max(0,i-10):i]
            if np.mean(np.abs(past_vel)) < 0.003:  # was relatively still
                if i - last_arm > 30:
                    arm_frames.append(i)
                    last_arm = i

    # For each arm, find first zero-crossing within 60 frames
    wy_ema20 = causal_ema(wy, tau_ms=20)
    vel20 = np.zeros_like(wy_ema20)
    vel20[1:] = wy_ema20[1:] - wy_ema20[:-1]

    detections = []
    for arm_t in arm_frames:
        for i in range(arm_t, min(arm_t + 60, len(vel20) - 1)):
            if vel20[i] < 0 and vel20[i + 1] >= 0:
                frac = -vel20[i] / (vel20[i + 1] - vel20[i] + 1e-10)
                detections.append(i + frac)
                break

    offsets, n_tp, n_false, n_missed, arm_rate = match_to_edges(detections, shot_edges)
    return report(clip_name, "Wrist rise onset arming", offsets, n_tp, n_false, n_missed, len(shot_edges), arm_rate)


def main():
    ap = argparse.ArgumentParser()
    args = ap.parse_args()

    print(f"ARMING SOLUTIONS v2 — Smarter shot-start detection")
    print(f"{'='*80}")

    # Extract all data
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

    # Build templates for template matching (leave-one-out: build from OTHER clips)
    all_results = {}

    for approach_name, approach_fn in [
        ("Apex-anchored", test_apex_anchored),
        ("Height-threshold", test_height_threshold),
        ("Energy-gated", test_energy_gated),
        ("Combined signature", test_combined_signature),
        ("Wrist rise onset", test_wrist_rise_onset),
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

    # Template match (leave-one-out)
    print(f"\n{'='*80}")
    print(f"APPROACH: Template match (leave-one-out)")
    print(f"{'='*80}")
    tm_results = []
    for i, (wy, hy, ky, edges, label) in enumerate(all_data):
        # Build template from all OTHER clips
        other_data = [d for j, d in enumerate(all_data) if j != i]
        templates = build_templates(other_data)
        r = test_template_match(wy, hy, ky, edges, label, templates)
        if r:
            tm_results.append(r)
    all_results["Template match"] = tm_results

    # Summary
    print(f"\n{'='*80}")
    print("SUMMARY — All approaches compared")
    print(f"{'='*80}")
    print(f"\n  {'Approach':<25} {'Arm%':>6} {'False':>6} {'IQR':>10} {'±80ms':>10}")
    print("  " + "-" * 65)

    for name, results in all_results.items():
        if not results:
            print(f"  {name:<25} {'N/A':>6} {'N/A':>6} {'N/A':>10} {'N/A':>10}")
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
            print(f"  {name:<25} {avg_arm:>5.0%} {total_false:>6} {iqr:>9.1f}ms {f'{w80}/{len(all_offsets)}':>10}")
        else:
            print(f"  {name:<25} {avg_arm:>5.0%} {total_false:>6} {'N/A':>10} {'N/A':>10}")

    # Find best
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
        if best_score > 0:
            print(f"  → Recommended for Claude's engine arming")


if __name__ == "__main__":
    main()
