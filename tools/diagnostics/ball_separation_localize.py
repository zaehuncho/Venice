#!/usr/bin/env python3
"""Ball-separation release detector — tests whether tracking the BASKETBALL
leaving the hand is a sharper release signal than body keypoints.

The release = the ball separating from the hand. That IS the actual event.
The ball is a high-contrast orange blob → detectable via:
  1. v9 model class 3 (Zbasketball)
  2. HSV orange color filtering (fallback/supplement)

Measures ball-separation-frame vs meter edge IQR.
Target: <80ms IQR (per Claude's decision gate).

Usage:
    C:\\Python314\\python.exe tools/diagnostics/ball_separation_localize.py "<video>" [count] [start] [handed] [--meter_color Red]
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from pose_timing import PoseTimingDetector
from meter_detector import MeterDetector, load_detector_config
from ultralytics import YOLO

# COCO keypoints
KP_WRIST_L, KP_WRIST_R = 9, 10
KP_ELBOW_L, KP_ELBOW_R = 7, 8
KP_CONF_FLOOR = 0.25

DEBOUNCE = 18
SEARCH_WINDOW = 45  # frames after meter edge to search (wider — ball separation may be slightly after)
SEARCH_BEFORE = 15  # frames before meter edge too

# HSV range for orange basketball (2K ball is bright orange)
ORANGE_LOW = np.array([5, 80, 80])
ORANGE_HIGH = np.array([25, 255, 255])


def detect_ball_hsv(frame, player_box=None):
    """Detect the basketball via HSV color filtering. Returns (cx, cy, area) or None."""
    if player_box is not None:
        px, py, pw, ph = player_box
        pad = max(pw, ph) // 2
        x1 = max(0, px - pad)
        y1 = max(0, py - pad)
        x2 = min(frame.shape[1], px + pw + pad)
        y2 = min(frame.shape[0], py + ph + pad)
        roi = frame[y1:y2, x1:x2]
        offset_x, offset_y = x1, y1
    else:
        roi = frame
        offset_x, offset_y = 0, 0

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, ORANGE_LOW, ORANGE_HIGH)
    # Clean up
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    # Find the most circular, reasonably-sized blob
    best = None
    best_score = 0
    for c in contours:
        area = cv2.contourArea(c)
        if area < 50 or area > 50000:
            continue
        perimeter = cv2.arcLength(c, True)
        if perimeter < 1:
            continue
        circularity = 4 * math.pi * area / (perimeter * perimeter)
        if circularity < 0.3:
            continue
        x, y, w, h = cv2.boundingRect(c)
        aspect = float(w) / max(h, 1)
        if aspect < 0.5 or aspect > 2.0:
            continue
        # Score: prefer larger, more circular blobs
        score = area * circularity
        if score > best_score:
            best_score = score
            best = (x + w / 2 + offset_x, y + h / 2 + offset_y, area)

    return best


def process_clip(video, count, start, handed, meter_color):
    """Process a clip and measure ball-separation localizability."""
    # Load models
    pose_model_path = os.path.join(ROOT, "models", "orion_pose2k_n.pt")
    if not os.path.exists(pose_model_path):
        pose_model_path = os.path.join(ROOT, "models", "yolo11n-pose.pt")
    pose_yolo = YOLO(pose_model_path)

    player_model = YOLO(os.path.join(ROOT, "models", "orion_player_detect_v9.pt"))

    # Meter detector
    mcfg = load_detector_config(os.path.join(ROOT, "settings.json"))
    mcfg.meter_style = "Arrow2"
    mcfg.meter_color = meter_color
    mcfg.auto_meter_color = False
    meter = MeterDetector(os.path.join(ROOT, "meter_styles"), mcfg)
    meter.set_active_style("Arrow2")

    is_right = str(handed).strip().lower().startswith("r")
    wr_kp = KP_WRIST_R if is_right else KP_WRIST_L

    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)

    print(f"  {os.path.basename(video)} | {fps:.0f}fps | window {start}..{start+count} | handed={handed}")

    # Per-frame data
    ball_positions = []  # (cx, cy, area) or None
    ball_v9_positions = []  # from v9 class 3
    wrist_positions = []  # (x, y) or None
    shot_edges = []
    prev_meter = False
    last_shot = -10**9
    n = 0
    player_box = None

    print("  Processing...", end=" ", flush=True)
    while n < count:
        ok, frame = cap.read()
        if not ok:
            break
        seq = start + n

        # Meter detection
        r = meter.detect(frame)
        fed = bool(r.detected and r.rejection_reason in ("", "green_not_found"))

        # Player detection (v9)
        det = player_model.predict(frame, conf=0.25, verbose=False)
        player_box = None
        ball_v9 = None
        if det and det[0].boxes is not None and len(det[0].boxes) > 0:
            cls = det[0].boxes.cls.cpu().numpy().astype(int)
            xyxy = det[0].boxes.xyxy.cpu().numpy()
            confs = det[0].boxes.conf.cpu().numpy()

            # Player (class 0)
            players = xyxy[cls == 0] if len(cls) > 0 else np.array([]).reshape(0, 4)
            if len(players) > 0:
                if fed and hasattr(r, 'bbox') and r.bbox and len(r.bbox) >= 4 and r.bbox[2] > 0:
                    # Pick player nearest to meter
                    bx, by, bw, bh = r.bbox
                    mx, my = bx + bw / 2, by + bh / 2
                    dists = [abs((b[0]+b[2])/2 - mx) + abs((b[1]+b[3])/2 - my) for b in players]
                    best = np.argmin(dists)
                else:
                    areas = [(b[2]-b[0]) * (b[3]-b[1]) for b in players]
                    best = np.argmax(areas)
                b = players[best]
                player_box = (int(b[0]), int(b[1]), int(b[2]-b[0]), int(b[3]-b[1]))

            # Basketball (class 3)
            balls = xyxy[cls == 3] if len(cls) > 0 else np.array([]).reshape(0, 4)
            ball_confs = confs[cls == 3] if len(cls) > 0 else np.array([])
            if len(balls) > 0:
                # Pick highest confidence ball
                best_ball = np.argmax(ball_confs)
                bb = balls[best_ball]
                ball_v9 = (float((bb[0]+bb[2])/2), float((bb[1]+bb[3])/2), float(ball_confs[best_ball]))

        ball_v9_positions.append(ball_v9)

        # HSV ball detection
        ball_hsv = detect_ball_hsv(frame, player_box)
        ball_positions.append(ball_hsv)

        # Pose detection for wrist position
        if player_box is not None:
            px, py, pw, ph = player_box
            pad = max(pw, ph) // 4
            x1 = max(0, px - pad)
            y1 = max(0, py - pad)
            x2 = min(frame.shape[1], px + pw + pad)
            y2 = min(frame.shape[0], py + ph + pad)
            crop = frame[y1:y2, x1:x2]
            pose_result = pose_yolo.predict(crop, verbose=False, conf=0.1)
        else:
            pose_result = pose_yolo.predict(frame, verbose=False, conf=0.1)
            x1, y1 = 0, 0

        wx, wy = float('nan'), float('nan')
        if pose_result and pose_result[0].keypoints is not None:
            kpts = pose_result[0].keypoints.data.cpu().numpy()
            if len(kpts) > 0:
                confs = kpts[..., 2].mean(axis=-1)
                best_person = np.argmax(confs)
                kp = kpts[best_person]
                if kp[wr_kp, 2] >= KP_CONF_FLOOR:
                    wx = float(kp[wr_kp, 0] + x1)
                    wy = float(kp[wr_kp, 1] + y1)

        wrist_positions.append((wx, wy))

        if fed and not prev_meter and (seq - last_shot) > DEBOUNCE:
            shot_edges.append(n)
            last_shot = seq
        prev_meter = fed
        n += 1

    cap.release()
    print(f"done | {len(shot_edges)} shots")

    # Compute ball-separation signals
    ms = 1000.0 / fps

    # Signal 1: Ball-wrist distance (HSV ball)
    ball_wrist_dist = []
    for i in range(len(ball_positions)):
        bp = ball_positions[i]
        wx, wy = wrist_positions[i]
        if bp is not None and not np.isnan(wx):
            d = math.sqrt((bp[0] - wx)**2 + (bp[1] - wy)**2)
            ball_wrist_dist.append(d)
        else:
            ball_wrist_dist.append(float('nan'))

    # Signal 2: Ball-wrist distance (v9 ball)
    ball_v9_dist = []
    for i in range(len(ball_v9_positions)):
        bp = ball_v9_positions[i]
        wx, wy = wrist_positions[i]
        if bp is not None and not np.isnan(wx):
            d = math.sqrt((bp[0] - wx)**2 + (bp[1] - wy)**2)
            ball_v9_dist.append(d)
        else:
            ball_v9_dist.append(float('nan'))

    # Signal 3: Ball vertical velocity (HSV ball) — ball shoots up at release
    ball_y_hsv = np.array([bp[1] if bp is not None else np.nan for bp in ball_positions])
    ball_vel_hsv = np.gradient(ball_y_hsv) * fps  # px/s, negative = upward

    # Signal 4: Ball vertical velocity (v9 ball)
    ball_y_v9 = np.array([bp[1] if bp is not None else np.nan for bp in ball_v9_positions])
    ball_vel_v9 = np.gradient(ball_y_v9) * fps

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

    signals = {
        "ball_wrist_dist_hsv": prep(ball_wrist_dist),
        "ball_wrist_dist_v9": prep(ball_v9_dist),
        "ball_vel_hsv": (prep(ball_vel_hsv.tolist())[0], prep(ball_vel_hsv.tolist())[1], prep(ball_vel_hsv.tolist())[2]) if any(not np.isnan(v) for v in ball_vel_hsv) else (None, None, None),
        "ball_vel_v9": (prep(ball_vel_v9.tolist())[0], prep(ball_vel_v9.tolist())[1], prep(ball_vel_v9.tolist())[2]) if any(not np.isnan(v) for v in ball_vel_v9) else (None, None, None),
    }

    # Also compute ball-Y directly (upward motion = release)
    signals["ball_y_hsv"] = prep(ball_y_hsv.tolist())
    signals["ball_y_v9"] = prep(ball_y_v9.tolist())

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
            hi = min(len(val), e + SEARCH_WINDOW)
            if hi - lo < 6:
                continue
            seg_val = val[lo:hi]
            seg_vel = vel[lo:hi] if vel is not None else np.zeros(hi - lo)
            seg_acc = acc[lo:hi] if acc is not None else np.zeros(hi - lo)

            # For distance: release = distance INCREASES (ball leaves hand) → max distance
            # For ball-Y: release = ball goes UP (Y decreases) → min Y
            # For ball-vel: release = max upward velocity (most negative)
            if "dist" in name:
                apex = lo + int(np.argmax(seg_val))  # max distance = separation
            elif "vel" in name:
                apex = lo + int(np.argmin(seg_val))  # most negative = fastest upward
            else:
                apex = lo + int(np.argmin(seg_val))  # min Y = highest point

            velpeak = lo + int(np.argmin(seg_vel)) if vel is not None else apex
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

    # Also report ball detection rates
    hsv_detected = sum(1 for b in ball_positions if b is not None)
    v9_detected = sum(1 for b in ball_v9_positions if b is not None)
    print(f"\n  Ball detection: HSV {hsv_detected}/{len(ball_positions)} ({100*hsv_detected//max(len(ball_positions),1)}%), v9 {v9_detected}/{len(ball_v9_positions)} ({100*v9_detected//max(len(ball_v9_positions),1)}%)")

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("count", type=int, nargs="?", default=3000)
    ap.add_argument("start", type=int, nargs="?", default=11940)
    ap.add_argument("handed", nargs="?", default="Right")
    ap.add_argument("--meter_color", default="Red")
    args = ap.parse_args()

    print(f"BALL-SEPARATION LOCALIZABILITY TEST")
    print(f"Target: <80ms IQR (decision gate for input-hook build)")
    print(f"{'='*70}")

    results = process_clip(args.video, args.count, args.start, args.handed, args.meter_color)

    print(f"\n{'='*70}")
    print("VERDICT:")
    best_iqr = min((r["iqr"] for r in results.values()), default=9999)
    if best_iqr < 80:
        print(f"  BEST IQR = {best_iqr:.0f}ms < 80ms → ESCALATE to input-hook build!")
    elif best_iqr < 200:
        print(f"  BEST IQR = {best_iqr:.0f}ms < 200ms → marginal, worth pursuing with closed-loop")
    else:
        print(f"  BEST IQR = {best_iqr:.0f}ms ≥ 200ms → ball tracking also floored")


if __name__ == "__main__":
    main()
