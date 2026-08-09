#!/usr/bin/env python3
"""Hand-orientation release detector — tests whether the forearm/hand ANGLE
at release is a tighter signal than wrist position.

Extracts elbow→wrist vector angle (forearm orientation) from the existing
YOLO pose model's COCO keypoints, plus optionally MediaPipe Hands on the
player crop for true hand/finger keypoints.

Measures the angular snap (max |d(angle)/dt|) localizability vs the meter edge.
Target: <80ms IQR (per Claude's decision gate).

Usage:
    C:\\Python314\\python.exe tools/diagnostics/hand_angle_localize.py "<video>" [count] [start] [handed] [--mediapipe] [--meter_color Red]
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

# COCO keypoint indices
KP_SHOULDER_L, KP_SHOULDER_R = 5, 6
KP_ELBOW_L, KP_ELBOW_R = 7, 8
KP_WRIST_L, KP_WRIST_R = 9, 10
KP_HIP_L, KP_HIP_R = 11, 12
KP_CONF_FLOOR = 0.30

DEBOUNCE = 18
SEARCH_WINDOW = 30  # frames after meter edge to search for release event


def angle_between(x1, y1, x2, y2):
    """Angle of vector (x1,y1)->(x2,y2) in degrees, 0=right, 90=up, 180=left, 270=down."""
    return math.degrees(math.atan2(-(y2 - y1), x2 - x1))  # negate Y because screen Y is inverted


def process_clip(video, count, start, handed, meter_color, use_mediapipe=False):
    """Process a clip and measure forearm/hand angle localizability."""
    # Load pose model directly for full keypoint access
    pose_model_path = os.path.join(ROOT, "models", "orion_pose2k_n.pt")
    if not os.path.exists(pose_model_path):
        pose_model_path = os.path.join(ROOT, "models", "yolo11n-pose.pt")
    pose_yolo = YOLO(pose_model_path)

    # Player detector for lock
    player_model = YOLO(os.path.join(ROOT, "models", "orion_player_detect_v9.pt"))

    # Meter detector
    mcfg = load_detector_config(os.path.join(ROOT, "settings.json"))
    mcfg.meter_style = "Arrow2"
    mcfg.meter_color = meter_color
    mcfg.auto_meter_color = False
    meter = MeterDetector(os.path.join(ROOT, "meter_styles"), mcfg)
    meter.set_active_style("Arrow2")

    # MediaPipe Hands (optional, new tasks API)
    mp_hands = None
    if use_mediapipe:
        try:
            import mediapipe as mp
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision as mp_vision

            model_path = os.path.join(ROOT, "models", "hand_landmarker.task")
            if not os.path.exists(model_path):
                print(f"  [warn] hand_landmarker.task not found at {model_path}")
                use_mediapipe = False
            else:
                base_options = mp_python.BaseOptions(model_asset_path=model_path)
                options = mp_vision.HandLandmarkerOptions(
                    base_options=base_options,
                    running_mode=mp_vision.RunningMode.VIDEO,
                    num_hands=2,
                    min_hand_detection_confidence=0.3,
                    min_hand_presence_confidence=0.3,
                    min_tracking_confidence=0.3,
                )
                mp_hands = mp_vision.HandLandmarker.create_from_options(options)
        except Exception as e:
            print(f"  [warn] MediaPipe HandLandmarker init failed: {e}")
            use_mediapipe = False

    # Determine shooting side
    is_right = str(handed).strip().lower().startswith("r")
    sh_kp = KP_SHOULDER_R if is_right else KP_SHOULDER_L
    el_kp = KP_ELBOW_R if is_right else KP_ELBOW_L
    wr_kp = KP_WRIST_R if is_right else KP_WRIST_L

    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)

    print(f"  {os.path.basename(video)} | {fps:.0f}fps | window {start}..{start+count} | handed={handed} | mp={use_mediapipe}")

    # Per-frame data
    forearm_angles = []  # elbow→wrist angle
    upper_arm_angles = []  # shoulder→elbow angle
    wrist_pos = []  # (x, y) for reference
    hand_angles_mp = []  # MediaPipe hand angle (wrist→index_fingertip)
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
        if fed and hasattr(r, 'bbox') and r.bbox and len(r.bbox) >= 4 and r.bbox[2] > 0:
            bx, by, bw, bh = r.bbox
            player_box = (int(bx), int(by), int(bw), int(bh))
        else:
            # Run v9 periodically to maintain lock
            if n % 10 == 0 or player_box is None:
                det = player_model.predict(frame, conf=0.3, verbose=False)
                if det and det[0].boxes is not None and len(det[0].boxes) > 0:
                    cls = det[0].boxes.cls.cpu().numpy().astype(int)
                    xyxy = det[0].boxes.xyxy.cpu().numpy()
                    players = xyxy[cls == 0] if len(cls) > 0 else np.array([]).reshape(0, 4)
                    if len(players) > 0:
                        # Pick largest or nearest to last player_box
                        if player_box is not None:
                            px, py = player_box[0] + player_box[2] / 2, player_box[1] + player_box[3] / 2
                            dists = [abs((b[0]+b[2])/2 - px) + abs((b[1]+b[3])/2 - py) for b in players]
                            best = np.argmin(dists)
                        else:
                            areas = [(b[2]-b[0]) * (b[3]-b[1]) for b in players]
                            best = np.argmax(areas)
                        b = players[best]
                        player_box = (int(b[0]), int(b[1]), int(b[2]-b[0]), int(b[3]-b[1]))

        # Pose detection on full frame (or crop if we have player box)
        if player_box is not None:
            px, py, pw, ph = player_box
            # Pad the crop a bit
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

        # Extract keypoints
        fa = float('nan')  # forearm angle
        ua = float('nan')  # upper arm angle
        wx, wy = float('nan'), float('nan')
        hmp = float('nan')  # mediapipe hand angle

        if pose_result and pose_result[0].keypoints is not None:
            kpts = pose_result[0].keypoints.data.cpu().numpy()  # (N, 17, 3)
            if kpts is not None and len(kpts) > 0:
                # Pick the person with highest mean keypoint confidence
                confs = kpts[..., 2].mean(axis=-1)  # (N,)
                best_person = np.argmax(confs)
                kp = kpts[best_person]  # (17, 3)

                # Adjust for crop offset
                kp_x = kp[:, 0] + x1
                kp_y = kp[:, 1] + y1
                kp_c = kp[:, 2]

                # Forearm angle: elbow → wrist
                if kp_c[el_kp] >= KP_CONF_FLOOR and kp_c[wr_kp] >= KP_CONF_FLOOR:
                    fa = angle_between(kp_x[el_kp], kp_y[el_kp], kp_x[wr_kp], kp_y[wr_kp])
                    wx, wy = kp_x[wr_kp], kp_y[wr_kp]

                # Upper arm angle: shoulder → elbow
                if kp_c[sh_kp] >= KP_CONF_FLOOR and kp_c[el_kp] >= KP_CONF_FLOOR:
                    ua = angle_between(kp_x[sh_kp], kp_y[sh_kp], kp_x[el_kp], kp_y[el_kp])

        # MediaPipe Hands on the crop
        if use_mediapipe and mp_hands is not None and player_box is not None:
            try:
                import mediapipe as mp
                crop_rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=crop_rgb)
                mp_result = mp_hands.detect_for_video(mp_image, n)
                if mp_result.hand_landmarks:
                    # Pick the first hand (wrist=0, index fingertip=8)
                    hl = mp_result.hand_landmarks[0]
                    w = hl[0]
                    tip = hl[8]
                    hmp = angle_between(w.x, w.y, tip.x, tip.y)
            except Exception:
                pass

        forearm_angles.append(fa)
        upper_arm_angles.append(ua)
        wrist_pos.append((wx, wy))
        hand_angles_mp.append(hmp)

        if fed and not prev_meter and (seq - last_shot) > DEBOUNCE:
            shot_edges.append(n)
            last_shot = seq
        prev_meter = fed
        n += 1

    cap.release()
    if mp_hands is not None:
        try:
            mp_hands.close()
        except Exception:
            pass

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
        "forearm_angle": prep(forearm_angles),
        "upper_arm_angle": prep(upper_arm_angles),
        "hand_angle_mp": prep(hand_angles_mp) if use_mediapipe else (None, None, None),
    }

    # Also compute wrist-Y for reference (same as Claude's flick test)
    wy_arr = np.array([p[1] if not np.isnan(p[1]) else np.nan for p in wrist_pos])
    signals["wrist_y_ref"] = prep(wy_arr.tolist())

    # Measure localizability for each signal
    print(f"\n  {'Signal':<20} {'n':>4} {'Median':>8} {'IQR':>8} {'±40ms':>8} {'±80ms':>8}")
    print("  " + "-" * 60)

    results = {}
    for name, (val, vel, acc) in signals.items():
        if val is None:
            continue

        events = {"apex": [], "velpeak": [], "accelpeak": []}
        for e in shot_edges:
            lo, hi = e, min(len(val), e + SEARCH_WINDOW)
            if hi - lo < 6:
                continue
            seg_val = val[lo:hi]
            seg_vel = vel[lo:hi] if vel is not None else np.zeros(hi - lo)
            seg_acc = acc[lo:hi] if acc is not None else np.zeros(hi - lo)

            # For angles: the "apex" is the angle minimum (or maximum — depends on convention)
            # For wrist-Y: apex = minimum Y (highest point)
            # For angles: the snap = max |accel|; the peak = max |velocity|
            apex = lo + int(np.argmin(seg_val))
            velpeak = lo + int(np.argmin(seg_vel)) if vel is not None else apex  # max change
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
            print(f"  {label:<20} {len(offs):>4} {med:>+7.0f}ms {iqr:>7.0f}ms {f'{w40}/{len(offs)}':>8} {f'{w80}/{len(offs)}':>8}")

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("count", type=int, nargs="?", default=3000)
    ap.add_argument("start", type=int, nargs="?", default=11940)
    ap.add_argument("handed", nargs="?", default="Right")
    ap.add_argument("--meter_color", default="Red")
    ap.add_argument("--mediapipe", action="store_true", help="Also test MediaPipe Hands")
    args = ap.parse_args()

    print(f"HAND-ANGLE LOCALIZABILITY TEST")
    print(f"Target: <80ms IQR (decision gate for input-hook build)")
    print(f"{'='*70}")

    results = process_clip(args.video, args.count, args.start, args.handed, args.meter_color, args.mediapipe)

    # Verdict
    print(f"\n{'='*70}")
    print("VERDICT:")
    best_iqr = min((r["iqr"] for r in results.values()), default=9999)
    if best_iqr < 80:
        print(f"  BEST IQR = {best_iqr:.0f}ms < 80ms → ESCALATE to input-hook build")
    elif best_iqr < 200:
        print(f"  BEST IQR = {best_iqr:.0f}ms < 200ms → marginal, may be worth pursuing with closed-loop")
    else:
        print(f"  BEST IQR = {best_iqr:.0f}ms ≥ 200ms → pose timing confirmed out, ship meter")


if __name__ == "__main__":
    main()
