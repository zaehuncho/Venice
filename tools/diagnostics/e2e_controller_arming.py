#!/usr/bin/env python3
"""End-to-end test: controller-state arming → zero-crossing release.

Simulates the live flow:
1. Extract pose trajectories from video clips
2. Simulate SQUARE button presses at meter-edge frames (proxy for real controller)
3. Call notify_shot_start() at each arm frame
4. Run PoseTimingDetector.update() frame-by-frame
5. Collect release landmarks and measure IQR vs meter edges

This validates the wired implementation in pose_timing.py.

Usage:
    C:\\Python314\\python.exe tools/diagnostics/e2e_controller_arming.py
"""
from __future__ import annotations

import os
import sys
import argparse

import numpy as np
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

os.environ['ORION_POSE_ZEROCROSS'] = '1'

from pose_timing import PoseTimingDetector, PoseLandmark
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


def run_e2e_test(video, count, start, handed, meter_color, clip_name):
    """Run the full end-to-end test on one clip."""
    # Set up detectors
    pose = PoseTimingDetector(
        pose_model_path="models/orion_pose2k_n_v2.pt",
        bar_model_path="models/orion_bar_park.pt",
        player_model_path="models/orion_player_detect_v9.pt",
        handedness=handed,
    )

    mcfg = load_detector_config(os.path.join(ROOT, "settings.json"))
    mcfg.meter_style = "Arrow2"
    mcfg.meter_color = meter_color
    mcfg.auto_meter_color = False
    meter = MeterDetector(os.path.join(ROOT, "meter_styles"), mcfg)
    meter.set_active_style("Arrow2")

    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)

    # First pass: find meter edges (shot start frames)
    print(f"  Pass 1 (meter edges): {os.path.basename(video)}...", end=" ", flush=True)
    shot_edges = []
    prev_meter = False
    last_shot = -10**9
    n = 0
    while n < count:
        ok, frame = cap.read()
        if not ok:
            break
        seq = start + n
        r = meter.detect(frame)
        fed = bool(r.detected and r.rejection_reason in ("", "green_not_found"))
        if fed and not prev_meter and (seq - last_shot) > DEBOUNCE:
            shot_edges.append(n)
            last_shot = seq
        prev_meter = fed
        n += 1
    cap.release()
    print(f"{len(shot_edges)} shots")

    # Second pass: run pose timing with controller-state arming
    print(f"  Pass 2 (e2e pose timing): {os.path.basename(video)}...", end=" ", flush=True)
    cap = cv2.VideoCapture(video)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)

    releases = []
    arm_idx = 0
    n = 0
    while n < count:
        ok, frame = cap.read()
        if not ok:
            break
        seq = start + n

        # Simulate square press at meter edge frames
        if arm_idx < len(shot_edges) and n == shot_edges[arm_idx]:
            pose.notify_shot_start(seq)
            arm_idx += 1

        # Feed meter bbox as player hint (like the orchestrator does)
        r = meter.detect(frame)
        fed = bool(r.detected and r.rejection_reason in ("", "green_not_found"))
        if fed and hasattr(r, 'bbox') and r.bbox and len(r.bbox) >= 4 and r.bbox[2] > 0:
            bx, by, bw, bh = r.bbox
            pose.set_player_hint((bx, by, bx + bw, by + bh))

        # Run pose timing
        landmark = pose.update(frame, seq, frame_time=seq / fps)
        if landmark is not None and landmark.kind == "release":
            releases.append((n, landmark))

        n += 1
    cap.release()
    print(f"{len(releases)} releases")

    # Match releases to shot edges
    offsets = []
    matched_edges = set()
    for rel_frame, lm in releases:
        # Find nearest shot edge
        if not shot_edges:
            continue
        nearest = min(shot_edges, key=lambda e: abs(rel_frame - e))
        dist = abs(rel_frame - nearest)
        if dist < 60 and nearest not in matched_edges:
            # Use subframe_seq if available
            if lm.subframe_seq is not None and lm.subframe_seq > 0:
                offset_ms = (lm.subframe_seq - start - nearest) * MS_PER_FRAME
            else:
                offset_ms = (rel_frame - nearest) * MS_PER_FRAME
            offsets.append(offset_ms)
            matched_edges.add(nearest)

    arm_rate = len(matched_edges) / max(len(shot_edges), 1)
    missed = len(shot_edges) - len(matched_edges)

    # Report
    print(f"\n  {clip_name}: E2E Controller-State Arming")
    print(f"    Shots: {len(shot_edges)} | Releases: {len(releases)} | Matched: {len(matched_edges)} | Missed: {missed}")
    print(f"    ARM RATE: {arm_rate:.1%}")

    if offsets:
        arr = np.array(sorted(offsets))
        med = np.median(arr)
        iqr = np.percentile(arr, 75) - np.percentile(arr, 25)
        w40 = sum(1 for x in offsets if abs(x - med) <= 40)
        w80 = sum(1 for x in offsets if abs(x - med) <= 80)
        print(f"    TIMING: median={med:+.1f}ms IQR={iqr:.1f}ms ±40ms={w40}/{len(offsets)} ±80ms={w80}/{len(offsets)}")
        return {"clip": clip_name, "arm_rate": arm_rate, "n_shots": len(shot_edges),
                "n_releases": len(releases), "n_matched": len(matched_edges),
                "median": med, "iqr": iqr, "w40": w40, "w80": w80, "offsets": offsets}
    else:
        print(f"    TIMING: no releases matched")
        return {"clip": clip_name, "arm_rate": arm_rate, "n_shots": len(shot_edges),
                "n_releases": len(releases), "n_matched": 0,
                "median": 0, "iqr": 9999, "w40": 0, "w80": 0, "offsets": []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", type=str, default="all", help="Comma-separated clip names (V3,V4,...) or 'all'")
    args = ap.parse_args()

    print(f"E2E CONTROLLER-STATE ARMING TEST")
    print(f"Validates the wired implementation: square press → arm → zero-crossing → release")
    print(f"{'='*80}")

    all_results = []
    for clip_name, count, start, handed, mcolor, label in CLIPS:
        if args.clips != "all" and label not in args.clips.split(","):
            continue
        video = os.path.join(VIDEOS, clip_name)
        if not os.path.exists(video):
            print(f"  [skip] {clip_name} not found")
            continue
        r = run_e2e_test(video, count, start, handed, mcolor, label)
        all_results.append(r)

    # Summary
    print(f"\n{'='*80}")
    print("SUMMARY — E2E Controller-State Arming")
    print(f"{'='*80}")
    print(f"\n  {'Clip':<6} {'Shots':>6} {'Releases':>9} {'Arm%':>6} {'Median':>8} {'IQR':>8} {'±40ms':>8} {'±80ms':>8}")
    print("  " + "-" * 70)

    all_offsets = []
    for r in all_results:
        all_offsets.extend(r["offsets"])
        w40_str = f"{r['w40']}/{len(r['offsets'])}"
        w80_str = f"{r['w80']}/{len(r['offsets'])}"
        print(f"  {r['clip']:<6} {r['n_shots']:>6} {r['n_releases']:>9} {r['arm_rate']:>5.0%} "
              f"{r['median']:>+7.1f}ms {r['iqr']:>7.1f}ms {w40_str:>8} {w80_str:>8}")

    if all_offsets:
        arr = np.array(sorted(all_offsets))
        med = np.median(arr)
        iqr = np.percentile(arr, 75) - np.percentile(arr, 25)
        avg_arm = np.mean([r["arm_rate"] for r in all_results])
        w40 = sum(1 for x in all_offsets if abs(x - med) <= 40)
        w80 = sum(1 for x in all_offsets if abs(x - med) <= 80)
        print(f"\n  OVERALL: arm={avg_arm:.0%} median={med:+.1f}ms IQR={iqr:.1f}ms "
              f"±40ms={w40}/{len(all_offsets)} ±80ms={w80}/{len(all_offsets)}")

        # Verdict
        if iqr < 80:
            print(f"\n  ✅ PASSES 80ms GATE — IQR={iqr:.1f}ms < 80ms")
        else:
            print(f"\n  ❌ FAILS 80ms GATE — IQR={iqr:.1f}ms >= 80ms")

        if iqr < 80 and avg_arm >= 0.8:
            print(f"  → LIVE-VIABLE: arm rate {avg_arm:.0%} + IQR {iqr:.1f}ms")
            print(f"  → Ready for Claude to wire pose_landmark event to C++ engine")


if __name__ == "__main__":
    main()
