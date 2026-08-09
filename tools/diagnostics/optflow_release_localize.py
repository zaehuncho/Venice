#!/usr/bin/env python3
"""Optical flow release detector — tests whether dense optical flow at the
hand/ball region is a sharper release signal than keypoint positions.

The release is a sudden motion-field discontinuity: the ball shoots up,
the hand snaps. Dense flow may localize this sharper than noisy keypoints.

Uses Farneback dense optical flow on the player crop, measures the
vertical motion magnitude in the upper body region.

Usage:
    C:\\Python314\\python.exe tools/diagnostics/optflow_release_localize.py "<video>" [count] [start] [handed] [--meter_color Red]
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from meter_detector import MeterDetector, load_detector_config
from ultralytics import YOLO

DEBOUNCE = 18
SEARCH_BEFORE = 15
SEARCH_AFTER = 45


def process_clip(video, count, start, handed, meter_color):
    """Process a clip and measure optical flow release localizability."""
    player_model = YOLO(os.path.join(ROOT, "models", "orion_player_detect_v9.pt"))

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

    shot_edges = []
    prev_meter = False
    last_shot = -10**9
    n = 0
    prev_crop = None
    player_box = None

    # Per-frame flow signals
    flow_up_mag = []  # upward motion magnitude in upper body
    flow_total_mag = []  # total motion magnitude
    flow_divergence = []  # motion divergence (expansion = ball leaving)

    print("  Processing...", end=" ", flush=True)
    while n < count:
        ok, frame = cap.read()
        if not ok:
            break
        seq = start + n

        # Meter detection
        r = meter.detect(frame)
        fed = bool(r.detected and r.rejection_reason in ("", "green_not_found"))

        # Player detection
        det = player_model.predict(frame, conf=0.25, verbose=False)
        player_box = None
        if det and det[0].boxes is not None and len(det[0].boxes) > 0:
            cls = det[0].boxes.cls.cpu().numpy().astype(int)
            xyxy = det[0].boxes.xyxy.cpu().numpy()
            players = xyxy[cls == 0] if len(cls) > 0 else np.array([]).reshape(0, 4)
            if len(players) > 0:
                if fed and hasattr(r, 'bbox') and r.bbox and len(r.bbox) >= 4 and r.bbox[2] > 0:
                    bx, by, bw, bh = r.bbox
                    mx, my = bx + bw / 2, by + bh / 2
                    dists = [abs((b[0]+b[2])/2 - mx) + abs((b[1]+b[3])/2 - my) for b in players]
                    best = np.argmin(dists)
                else:
                    areas = [(b[2]-b[0]) * (b[3]-b[1]) for b in players]
                    best = np.argmax(areas)
                b = players[best]
                player_box = (int(b[0]), int(b[1]), int(b[2]-b[0]), int(b[3]-b[1]))

        # Compute optical flow on player crop
        up_mag = float('nan')
        total_mag = float('nan')
        div = float('nan')

        if player_box is not None:
            px, py, pw, ph = player_box
            # Crop the upper body region (top 60% of player box)
            x1 = max(0, px - 20)
            y1 = max(0, py - 20)
            x2 = min(frame.shape[1], px + pw + 20)
            y2 = min(frame.shape[0], py + int(ph * 0.7) + 20)
            crop = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
            crop = cv2.resize(crop, (160, 160))

            if prev_crop is not None and prev_crop.shape == crop.shape:
                # Farneback dense optical flow
                flow = cv2.calcOpticalFlowFarneback(
                    prev_crop, crop, None,
                    pyr_scale=0.5, levels=3, winsize=15,
                    iterations=3, poly_n=5, poly_sigma=1.2, flags=0
                )
                fx, fy = flow[..., 0], flow[..., 1]

                # Upward motion = negative fy (y decreases = up)
                # Focus on upper half of crop (where hands/ball are)
                upper = slice(0, 80)
                up_motion = -fy[upper]  # positive = upward
                up_mag = float(np.percentile(up_motion, 95))  # 95th percentile = strongest upward motion

                total_mag = float(np.sqrt(np.mean(fx**2 + fy**2)))

                # Divergence (expansion = ball separating)
                # Compute simple divergence: d(fx)/dx + d(fy)/dy
                div_x = np.gradient(fx, axis=1)
                div_y = np.gradient(fy, axis=0)
                div = float(np.percentile(np.abs(div_x + div_y), 95))

            prev_crop = crop
        else:
            prev_crop = None

        flow_up_mag.append(up_mag)
        flow_total_mag.append(total_mag)
        flow_divergence.append(div)

        if fed and not prev_meter and (seq - last_shot) > DEBOUNCE:
            shot_edges.append(n)
            last_shot = seq
        prev_meter = fed
        n += 1

    cap.release()
    print(f"done | {len(shot_edges)} shots")

    # Prepare signals
    def prep(arr):
        a = np.array(arr, dtype=float)
        m = ~np.isnan(a)
        if m.sum() > 10:
            a = np.interp(np.arange(len(a)), np.where(m)[0], a[m])
        else:
            return None, None, None
        a = np.convolve(a, [0.25, 0.5, 0.25], mode="same")
        return a, np.gradient(a), np.gradient(np.gradient(a))

    ms = 1000.0 / fps
    signals = {
        "flow_up_mag": prep(flow_up_mag),
        "flow_total": prep(flow_total_mag),
        "flow_divergence": prep(flow_divergence),
    }

    # Measure localizability
    print(f"\n  {'Signal':<25} {'n':>4} {'Median':>8} {'IQR':>8} {'±40ms':>8} {'±80ms':>8}")
    print("  " + "-" * 65)

    results = {}
    for name, (val, vel, acc) in signals.items():
        if val is None:
            continue

        events = {"apex": [], "velpeak": [], "accelpeak": []}
        for e in shot_edges:
            lo = max(0, e - SEARCH_BEFORE)
            hi = min(len(val), e + SEARCH_AFTER)
            if hi - lo < 6:
                continue
            seg_val = val[lo:hi]
            seg_vel = vel[lo:hi] if vel is not None else np.zeros(hi - lo)
            seg_acc = acc[lo:hi] if acc is not None else np.zeros(hi - lo)

            # For upward motion: release = peak upward motion
            apex = lo + int(np.argmax(seg_val))  # max upward = release
            velpeak = lo + int(np.argmax(seg_vel)) if vel is not None else apex
            accelpeak = lo + int(np.argmax(np.abs(seg_acc))) if acc is not None else apex

            events["apex"].append((apex - e) * ms)
            events["velpeak"].append((velpeak - e) * ms)
            events["accelpeak"].append((accelpeak - e) * ms)

        for event_name, offs in events.items():
            if len(offs) < 5:
                continue
            a = np.array(sorted(offs))
            med = np.median(a)
            iqr = np.percentile(a, 75) - np.percentile(a, 25)
            w40 = sum(1 for x in offs if abs(x - med) <= 40)
            w80 = sum(1 for x in offs if abs(x - med) <= 80)
            label = f"{name}_{event_name}"
            results[label] = {"n": len(offs), "median": med, "iqr": iqr, "within_40": w40, "within_80": w80}
            print(f"  {label:<25} {len(offs):>4} {med:>+7.0f}ms {iqr:>7.0f}ms {f'{w40}/{len(offs)}':>8} {f'{w80}/{len(offs)}':>8}")

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("count", type=int, nargs="?", default=3000)
    ap.add_argument("start", type=int, nargs="?", default=11940)
    ap.add_argument("handed", nargs="?", default="Right")
    ap.add_argument("--meter_color", default="Red")
    args = ap.parse_args()

    print(f"OPTICAL FLOW RELEASE LOCALIZABILITY TEST")
    print(f"Target: <80ms IQR (decision gate for input-hook build)")
    print(f"{'='*70}")

    results = process_clip(args.video, args.count, args.start, args.handed, args.meter_color)

    print(f"\n{'='*70}")
    print("VERDICT:")
    best_iqr = min((r["iqr"] for r in results.values()), default=9999)
    if best_iqr < 80:
        print(f"  BEST IQR = {best_iqr:.0f}ms < 80ms → ESCALATE to input-hook build!")
    elif best_iqr < 200:
        print(f"  BEST IQR = {best_iqr:.0f}ms < 200ms → marginal, worth pursuing")
    else:
        print(f"  BEST IQR = {best_iqr:.0f}ms ≥ 200ms → optical flow also floored")


if __name__ == "__main__":
    main()
