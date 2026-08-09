#!/usr/bin/env python3
"""Extract player crops with user-player labels from video clips.

Uses meter detection as ground truth: the player nearest the meter bbox is the USER.
All other players in the same frame are OTHER. Creates a labeled dataset for training
a user-player classifier to improve lock robustness.

Output: tools/diagnostics/user_player_dataset/
  user/   — crops of the user player (nearest meter)
  other/  — crops of other players

Usage:
    C:\\Python314\\python.exe tools/diagnostics/extract_user_player_dataset.py
"""
from __future__ import annotations

import os, sys, argparse, json

import numpy as np
import cv2
from ultralytics import YOLO

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from meter_detector import MeterDetector, load_detector_config

VIDEOS = r"C:\Users\Administrator\Videos"
OUTPUT_DIR = os.path.join(ROOT, "tools", "diagnostics", "user_player_dataset")

CLIPS = [
    ("NBA 2K26_20260624212008.mp4", 3000, 150, "Red", "V3"),
    ("NBA 2K26_20260624212347.mp4", 3000, 2880, "Red", "V4"),
    ("NBA 2K26_20260624212700.mp4", 3000, 6240, "Red", "V5"),
    ("NBA 2K26_20260624213055.mp4", 3000, 7860, "Red", "V6"),
    ("NBA 2K26_20260521032018.mp4", 3000, 11940, "Red", "V7"),
]

METER_PROXIMITY_THRESHOLD = 150  # max distance (px) between player center and meter center


def extract_clip(video_path, count, start, meter_color, clip_name, player_model, meter, sample_interval=5):
    """Extract labeled player crops from one clip."""
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)

    user_dir = os.path.join(OUTPUT_DIR, "user")
    other_dir = os.path.join(OUTPUT_DIR, "other")
    os.makedirs(user_dir, exist_ok=True)
    os.makedirs(other_dir, exist_ok=True)

    n_user = 0
    n_other = 0
    n = 0

    while n < count:
        ok, frame = cap.read()
        if not ok:
            break

        if n % sample_interval == 0:
            H, W = frame.shape[:2]

            # Detect meter
            r = meter.detect(frame)
            meter_detected = bool(r.detected and r.rejection_reason in ("", "green_not_found"))
            meter_cx = meter_cy = None
            if meter_detected and hasattr(r, 'bbox') and r.bbox and len(r.bbox) >= 4 and r.bbox[2] > 0:
                bx, by, bw, bh = r.bbox
                meter_cx = bx + bw / 2.0
                meter_cy = by + bh / 2.0

            # Detect players
            dets = player_model.predict(frame, verbose=False, conf=0.25)[0]
            if dets.boxes is None or len(dets.boxes) == 0:
                n += 1
                continue

            boxes = dets.boxes.xyxy.cpu().numpy()
            confs = dets.boxes.conf.cpu().numpy()

            # If meter detected, label nearest player as user
            user_idx = -1
            if meter_cx is not None:
                best_dist = 1e9
                for bi, box in enumerate(boxes):
                    pcx = (box[0] + box[2]) / 2.0
                    pcy = (box[1] + box[3]) / 2.0
                    d = np.sqrt((pcx - meter_cx) ** 2 + (pcy - meter_cy) ** 2)
                    if d < best_dist:
                        best_dist = d
                        user_idx = bi
                # Only label if within proximity
                if best_dist > METER_PROXIMITY_THRESHOLD:
                    user_idx = -1

            # Save crops
            for bi, (box, conf) in enumerate(zip(boxes, confs)):
                x1, y1, x2, y2 = map(int, box)
                # Add small padding
                x1 = max(0, x1 - 5)
                y1 = max(0, y1 - 5)
                x2 = min(W, x2 + 5)
                y2 = min(H, y2 + 5)

                crop = frame[y1:y2, x1:x2]
                if crop.shape[0] < 30 or crop.shape[1] < 20:
                    continue

                if bi == user_idx:
                    fname = f"{clip_name}_f{n:05d}_p{bi}_user.png"
                    cv2.imwrite(os.path.join(user_dir, fname), crop)
                    n_user += 1
                else:
                    fname = f"{clip_name}_f{n:05d}_p{bi}_other.png"
                    cv2.imwrite(os.path.join(other_dir, fname), crop)
                    n_other += 1

        n += 1

    cap.release()
    print(f"  {clip_name}: {n_user} user crops, {n_other} other crops")
    return n_user, n_other


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", type=str, default="all")
    ap.add_argument("--interval", type=int, default=5, help="Sample every N frames")
    args = ap.parse_args()

    print("USER-PLAYER DATASET EXTRACTION")
    print(f"Output: {OUTPUT_DIR}")
    print(f"{'='*80}")

    player_model = YOLO("models/orion_player_detect_v9.pt")

    mcfg = load_detector_config(os.path.join(ROOT, "settings.json"))
    mcfg.meter_style = "Arrow2"
    mcfg.meter_color = "Red"
    mcfg.auto_meter_color = False
    meter = MeterDetector(os.path.join(ROOT, "meter_styles"), mcfg)
    meter.set_active_style("Arrow2")

    total_user = 0
    total_other = 0
    for clip_file, count, start, mcolor, clip_name in CLIPS:
        if args.clips != "all" and clip_name not in args.clips.split(","):
            continue
        video = os.path.join(VIDEOS, clip_file)
        if not os.path.exists(video):
            print(f"  [skip] {clip_file} not found")
            continue
        nu, no = extract_clip(video, count, start, mcolor, clip_name, player_model, meter, args.interval)
        total_user += nu
        total_other += no

    print(f"\n{'='*80}")
    print(f"TOTAL: {total_user} user crops, {total_other} other crops")
    print(f"Ratio: {total_user / max(total_user + total_other, 1):.1%} user / "
          f"{total_other / max(total_user + total_other, 1):.1%} other")

    if total_user > 50 and total_other > 50:
        print(f"\nReady for classifier training — sufficient samples in both classes.")
    else:
        print(f"\nWARNING: need more samples (min 50 per class). Run with --interval 2 for denser sampling.")


if __name__ == "__main__":
    main()
