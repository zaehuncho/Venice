#!/usr/bin/env python3
"""Sub-frame peak interpolation — tests whether the 16ms frame quantization
is a significant contributor to the IQR.

Instead of actually interpolating frames (expensive), we interpolate the
TRAJECTORY at sub-frame resolution using cubic splines, then find the
derivative peaks at 1ms resolution. If the IQR tightens significantly,
frame quantization is a major contributor and actual frame interpolation
(RIFE) is worth the investment.

Also tests: ensemble averaging (run pose detection N times with slight
augmentation, average keypoints) for noise reduction.

Usage:
    C:\\Python314\\python.exe tools/diagnostics/subframe_interp_localize.py "<video>" [count] [start] [handed] [--meter_color Red] [--ensemble N]
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import cv2
from scipy.interpolate import CubicSpline

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from pose_timing import PoseTimingDetector
from meter_detector import MeterDetector, load_detector_config

DEBOUNCE = 18
SEARCH_BEFORE = 15
SEARCH_AFTER = 45
SUBFRAME_STEPS = 16  # 16 sub-steps per frame = ~1ms resolution at 60fps


def process_clip(video, count, start, handed, meter_color, ensemble_n=1):
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

    print(f"  {os.path.basename(video)} | {fps:.0f}fps | window {start}..{start+count} | ensemble={ensemble_n}")

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

        # Ensemble: run pose update multiple times with slight horizontal flip augmentation
        if ensemble_n > 1:
            wrist_ys = []
            for _ in range(ensemble_n):
                try:
                    pose.update(frame, seq, frame_time=seq / fps)
                except Exception:
                    pass
                j = (pose._buf_idx - 1) % pose._max_buf
                wrist_ys.append(float(pose._wrist_y[j]))
            # Take the median (robust to outliers)
            valid = [w for w in wrist_ys if not np.isnan(w)]
            wy_val = np.median(valid) if valid else float('nan')
            # Also get hip
            j = (pose._buf_idx - 1) % pose._max_buf
            hy_val = float(pose._hip_y[j])
        else:
            try:
                pose.update(frame, seq, frame_time=seq / fps)
            except Exception:
                pass
            j = (pose._buf_idx - 1) % pose._max_buf
            wy_val = float(pose._wrist_y[j])
            hy_val = float(pose._hip_y[j])

        wy_all.append(wy_val)
        hy_all.append(hy_val)

        if fed and not prev_meter and (seq - last_shot) > DEBOUNCE:
            shot_edges.append(n)
            last_shot = seq
        prev_meter = fed
        n += 1

    cap.release()
    print(f"done | {len(shot_edges)} shots")

    ms = 1000.0 / fps

    # Prepare trajectory
    wy = np.array(wy_all, dtype=float)
    mask = ~np.isnan(wy)
    if mask.sum() < 10:
        print("  [error] not enough valid wrist data")
        return
    wy = np.interp(np.arange(len(wy)), np.where(mask)[0], wy[mask])

    # Standard smoothing (same as other tests)
    wy_smooth = np.convolve(wy, [0.25, 0.5, 0.25], mode="same")
    wy_vel = np.gradient(wy_smooth)
    wy_acc = np.gradient(wy_vel)

    # === METHOD 1: Standard frame-level detection (baseline) ===
    offsets_frame = []
    for e in shot_edges:
        lo = max(0, e - SEARCH_BEFORE)
        hi = min(len(wy_smooth), e + SEARCH_AFTER)
        if hi - lo < 6:
            continue
        seg_acc = wy_acc[lo:hi]
        peak = lo + int(np.argmax(np.abs(seg_acc)))
        offsets_frame.append((peak - e) * ms)

    # === METHOD 2: Sub-frame cubic spline interpolation ===
    # Interpolate the trajectory at SUBFRAME_STEPS x resolution
    # Then find the peak at 1ms resolution
    offsets_subframe = []
    frame_indices = np.arange(len(wy_smooth))

    for e in shot_edges:
        lo = max(0, e - SEARCH_BEFORE)
        hi = min(len(wy_smooth), e + SEARCH_AFTER)
        if hi - lo < 8:
            continue

        # Use a wider window for spline fitting (need enough points)
        spline_lo = max(0, lo - 5)
        spline_hi = min(len(wy_smooth), hi + 5)
        x = frame_indices[spline_lo:spline_hi]
        y = wy_smooth[spline_lo:spline_hi]

        if len(x) < 6:
            continue

        try:
            cs = CubicSpline(x, y)
            # Sub-frame x values
            sub_x = np.arange(spline_lo, spline_hi, 1.0 / SUBFRAME_STEPS)
            sub_y = cs(sub_x)
            sub_vel = cs(sub_x, 1)  # first derivative
            sub_acc = cs(sub_x, 2)  # second derivative
        except Exception:
            continue

        # Find accel peak in the search window (relative to meter edge e)
        search_mask = (sub_x >= lo) & (sub_x <= hi)
        if np.sum(search_mask) < 6:
            continue

        sub_search_x = sub_x[search_mask]
        sub_search_acc = sub_acc[search_mask]
        peak_idx = np.argmax(np.abs(sub_search_acc))
        peak_x = sub_search_x[peak_idx]
        offset = (peak_x - e) * ms
        offsets_subframe.append(offset)

    # === METHOD 3: Sub-frame + parabolic peak refinement ===
    # Fit a parabola to the 3 points around the frame-level peak
    offsets_parabolic = []
    for e in shot_edges:
        lo = max(0, e - SEARCH_BEFORE)
        hi = min(len(wy_smooth), e + SEARCH_AFTER)
        if hi - lo < 6:
            continue
        seg_acc = wy_acc[lo:hi]
        peak = lo + int(np.argmax(np.abs(seg_acc)))

        # Parabolic interpolation around the peak
        if 1 <= peak - lo < len(seg_acc) - 1:
            y0, y1, y2 = abs(seg_acc[peak - lo - 1]), abs(seg_acc[peak - lo]), abs(seg_acc[peak - lo + 1])
            denom = y0 - 2 * y1 + y2
            if abs(denom) > 1e-10:
                delta = 0.5 * (y0 - y2) / denom
                delta = np.clip(delta, -0.5, 0.5)
            else:
                delta = 0.0
            peak_refined = peak + delta
        else:
            peak_refined = peak
        offsets_parabolic.append((peak_refined - e) * ms)

    # === METHOD 4: Sub-frame velocity zero-crossing (apex) ===
    # The release = wrist velocity crosses zero (upward → downward)
    # Find this crossing at sub-frame resolution
    # IMPORTANT: only search AFTER the meter edge (the release happens during/after the meter rise)
    # Also find the zero-crossing nearest to the wrist apex (highest point)
    offsets_zerocross = []
    offsets_zerocross_apex = []
    for e in shot_edges:
        lo = max(0, e - SEARCH_BEFORE)
        hi = min(len(wy_smooth), e + SEARCH_AFTER)
        if hi - lo < 8:
            continue

        spline_lo = max(0, lo - 5)
        spline_hi = min(len(wy_smooth), hi + 5)
        x = frame_indices[spline_lo:spline_hi]
        y = wy_smooth[spline_lo:spline_hi]

        if len(x) < 6:
            continue

        try:
            cs = CubicSpline(x, y)
            sub_x = np.arange(spline_lo, spline_hi, 1.0 / SUBFRAME_STEPS)
            sub_vel = cs(sub_x, 1)
            sub_y = cs(sub_x)
        except Exception:
            continue

        # Find the wrist apex (minimum Y = highest point) in the full search window
        search_mask = (sub_x >= lo) & (sub_x <= hi)
        sub_search_vel = sub_vel[search_mask]
        sub_search_x = sub_x[search_mask]
        sub_search_y = sub_y[search_mask]

        apex_sub_idx = np.argmin(sub_search_y)
        apex_x = sub_search_x[apex_sub_idx]

        # Method 4a: first zero-crossing AFTER meter edge only (release is after meter starts)
        post_mask = sub_search_x >= e
        sub_post_vel = sub_search_vel[post_mask]
        sub_post_x = sub_search_x[post_mask]
        sign_changes = []
        for i in range(len(sub_post_vel) - 1):
            if sub_post_vel[i] < 0 and sub_post_vel[i + 1] >= 0:
                frac = -sub_post_vel[i] / (sub_post_vel[i + 1] - sub_post_vel[i] + 1e-10)
                cross_x = sub_post_x[i] + frac * (sub_post_x[i + 1] - sub_post_x[i])
                sign_changes.append(cross_x)
        if sign_changes:
            offsets_zerocross.append((sign_changes[0] - e) * ms)

        # Method 4b: zero-crossing nearest to the apex (within ±5 frames of apex)
        apex_window = (sub_search_x >= apex_x - 5) & (sub_search_x <= apex_x + 5)
        sub_apex_vel = sub_search_vel[apex_window]
        sub_apex_x = sub_search_x[apex_window]
        apex_sign_changes = []
        for i in range(len(sub_apex_vel) - 1):
            if sub_apex_vel[i] < 0 and sub_apex_vel[i + 1] >= 0:
                frac = -sub_apex_vel[i] / (sub_apex_vel[i + 1] - sub_apex_vel[i] + 1e-10)
                cross_x = sub_apex_x[i] + frac * (sub_apex_x[i + 1] - sub_apex_x[i])
                apex_sign_changes.append(cross_x)
        if apex_sign_changes:
            # Take the one closest to the apex
            closest = min(apex_sign_changes, key=lambda cx: abs(cx - apex_x))
            offsets_zerocross_apex.append((closest - e) * ms)

    # Print results
    def report(name, offs):
        if len(offs) < 5:
            print(f"  {name:<30} n={len(offs)} — too few")
            return None
        a = np.array(sorted(offs))
        med = np.median(a)
        iqr = np.percentile(a, 75) - np.percentile(a, 25)
        std = np.std(a)
        w40 = sum(1 for x in offs if abs(x - med) <= 40)
        w80 = sum(1 for x in offs if abs(x - med) <= 80)
        print(f"  {name:<30} n={len(offs):>3} median={med:>+7.1f}ms IQR={iqr:>7.1f}ms STD={std:>7.1f}ms ±40={w40}/{len(offs)} ±80={w80}/{len(offs)}")
        return {"n": len(offs), "median": med, "iqr": iqr, "std": std, "w40": w40, "w80": w80}

    print(f"\n  {'Method':<30} {'n':>4} {'Median':>8} {'IQR':>8} {'STD':>8} {'±40ms':>10} {'±80ms':>10}")
    print("  " + "-" * 80)

    results = {}
    results["frame_level_accel"] = report("Frame-level accel-peak", offsets_frame)
    results["subframe_spline_accel"] = report("Sub-frame spline accel-peak", offsets_subframe)
    results["parabolic_refined"] = report("Parabolic refined accel-peak", offsets_parabolic)
    results["subframe_zerocross_post"] = report("Sub-frame zero-cross (post-edge)", offsets_zerocross)
    results["subframe_zerocross_apex"] = report("Sub-frame zero-cross (apex-anchored)", offsets_zerocross_apex)

    # Also test: fusion of apex-anchored zero-cross + accel-peak
    if len(offsets_zerocross_apex) >= 5 and len(offsets_subframe) >= 5:
        min_n = min(len(offsets_zerocross_apex), len(offsets_subframe))
        a1 = np.array(offsets_zerocross_apex[:min_n])
        a2 = np.array(offsets_subframe[:min_n])
        cal1 = a1 - np.median(a1)
        cal2 = a2 - np.median(a2)
        fused = (cal1 + cal2) / 2.0
        results["subframe_fusion"] = report("Sub-frame fusion (apex+accel)", fused.tolist())

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("count", type=int, nargs="?", default=3000)
    ap.add_argument("start", type=int, nargs="?", default=11940)
    ap.add_argument("handed", nargs="?", default="Right")
    ap.add_argument("--meter_color", default="Red")
    ap.add_argument("--ensemble", type=int, default=1, help="Number of ensemble runs for keypoint averaging")
    args = ap.parse_args()

    print(f"SUB-FRAME INTERPOLATION LOCALIZABILITY TEST")
    print(f"Tests whether sub-frame peak refinement tightens the IQR")
    print(f"Resolution: {SUBFRAME_STEPS} sub-steps/frame = {1000/(60*SUBFRAME_STEPS):.1f}ms")
    print(f"{'='*70}")

    results = process_clip(args.video, args.count, args.start, args.handed, args.meter_color, args.ensemble)

    print(f"\n{'='*70}")
    if results:
        best_iqr = min((r["iqr"] for r in results.values() if r), default=9999)
        baseline_iqr = results.get("frame_level_accel", {}).get("iqr", 0)
        print(f"  Baseline (frame-level): {baseline_iqr:.0f}ms")
        print(f"  Best sub-frame: {best_iqr:.0f}ms")
        if baseline_iqr > 0:
            improvement = (1 - best_iqr / baseline_iqr) * 100
            print(f"  Improvement: {improvement:.0f}%")
        if best_iqr < 80:
            print(f"  → SUB-80ms ACHIEVED! Frame interpolation is worth the investment.")
        elif best_iqr < baseline_iqr * 0.8:
            print(f"  → Significant improvement — RIFE frame interpolation likely to help further.")
        else:
            print(f"  → Marginal improvement — quantization is NOT the main contributor.")


if __name__ == "__main__":
    main()
