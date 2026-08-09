#!/usr/bin/env python3
"""Targeted ring scan: use player detector + meter bbox to find the user's player,
then crop tightly around their feet to look for the under-player indicator.

Also saves zoomed-in crops for visual inspection.

Usage:
    C:\\Python314\\python.exe tools/diagnostics/ring_targeted_scan.py
"""
from __future__ import annotations

import os, sys, argparse, json

import numpy as np
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from ultralytics import YOLO

VIDEOS = r"C:\Users\Administrator\Videos"
OUTPUT_DIR = os.path.join(ROOT, "tools", "diagnostics", "ring_scan_output")

CLIPS = [
    ("NBA 2K26_20260521032018.mp4", 3000, 11940, "V7"),
    ("NBA 2K26_20260624212008.mp4", 3000, 150, "V3"),
]


def scan_clip(video_path, count, start, clip_name, sample_interval=60):
    """Use player detector + meter to find user player, crop feet area."""
    from pose_timing import PoseTimingDetector

    pose = PoseTimingDetector(
        pose_model_path="models/orion_pose2k_n_v2.pt",
        bar_model_path="models/orion_bar_park.pt",
        player_model_path="models/orion_player_detect_v9.pt",
        handedness="Right",
    )

    # Also load player detector directly for bbox extraction
    player_model = YOLO("models/orion_player_detect_v9.pt")

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    results = []
    n = 0
    saved = 0

    while n < count:
        ok, frame = cap.read()
        if not ok:
            break

        if n % sample_interval == 0:
            H, W = frame.shape[:2]

            # Run player detection
            dets = player_model.predict(frame, verbose=False, conf=0.25)[0]
            if dets.boxes is None or len(dets.boxes) == 0:
                n += 1
                continue

            boxes = dets.boxes.xyxy.cpu().numpy()
            confs = dets.boxes.conf.cpu().numpy()

            # Save crops of ALL players' feet areas
            for bi, (box, conf) in enumerate(zip(boxes, confs)):
                x1, y1, x2, y2 = map(int, box)
                # Feet area: bottom 25% of player box, with some padding
                feet_top = int(y2 - (y2 - y1) * 0.25)
                feet_bot = min(H, int(y2 + 20))
                feet_left = max(0, int(x1 - 10))
                feet_right = min(W, int(x2 + 10))

                feet_crop = frame[feet_top:feet_bot, feet_left:feet_right]

                if feet_crop.shape[0] < 10 or feet_crop.shape[1] < 10:
                    continue

                # Save first few crops for visual inspection
                if saved < 30:
                    fname = f"{clip_name}_f{n:05d}_p{bi}_c{conf:.2f}.png"
                    cv2.imwrite(os.path.join(OUTPUT_DIR, fname), feet_crop)
                    saved += 1

                # Check for colored pixels in the feet crop
                hsv = cv2.cvtColor(feet_crop, cv2.COLOR_BGR2HSV)
                h_chan = hsv[:, :, 0]
                s_chan = hsv[:, :, 1]
                v_chan = hsv[:, :, 2]

                # Look for saturated, bright pixels (potential indicator colors)
                saturated = (s_chan > 100) & (v_chan > 80)

                # Group by hue
                for hue_range, color_name in [
                    ((0, 15), "red"), ((15, 35), "orange/yellow"),
                    ((35, 85), "green"), ((85, 130), "blue"),
                    ((130, 170), "purple/magenta"), ((170, 180), "red2"),
                ]:
                    hue_mask = (h_chan >= hue_range[0]) & (h_chan <= hue_range[1])
                    color_mask = saturated & hue_mask
                    count_pixels = int(color_mask.sum())

                    if count_pixels > 5:
                        # Find centroid of colored pixels
                        ys, xs = np.where(color_mask)
                        cx = int(np.mean(xs)) + feet_left
                        cy = int(np.mean(ys)) + feet_top

                        results.append({
                            "frame": n,
                            "clip": clip_name,
                            "player_idx": bi,
                            "player_conf": float(conf),
                            "color": color_name,
                            "pixel_count": count_pixels,
                            "cx": cx, "cy": cy,
                            "norm_x": cx / W, "norm_y": cy / H,
                            "player_box": [int(x1), int(y1), int(x2), int(y2)],
                        })

            # Also run pose timing to get the user player via meter/stamina
            _ = pose.update(frame, start + n, frame_time=n / fps)

        n += 1

    cap.release()
    print(f"  {clip_name}: scanned {n // sample_interval} frames, {len(results)} color hits, {saved} crops saved")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", type=str, default="all")
    args = ap.parse_args()

    print("TARGETED RING SCAN — player feet crops")
    print(f"Output: {OUTPUT_DIR}")
    print(f"{'='*80}")

    all_results = []
    for clip_file, count, start, clip_name in CLIPS:
        if args.clips != "all" and clip_name not in args.clips.split(","):
            continue
        video = os.path.join(VIDEOS, clip_file)
        if not os.path.exists(video):
            print(f"  [skip] {clip_file} not found")
            continue
        r = scan_clip(video, count, start, clip_name)
        all_results.extend(r)

    # Analyze
    if all_results:
        print(f"\n{'='*80}")
        print("COLOR HITS BY TYPE:")
        by_color = {}
        for d in all_results:
            by_color.setdefault(d["color"], []).append(d)
        for color, dets in sorted(by_color.items(), key=lambda x: -len(x[1])):
            print(f"  {color}: {len(dets)} hits")

        # Save results
        results_path = os.path.join(OUTPUT_DIR, "targeted_ring_results.json")
        with open(results_path, "w") as f:
            json.dump(all_results, f, indent=2)
        print(f"\nResults: {results_path}")
        print(f"Visual crops in: {OUTPUT_DIR} ({clip_name}_f*_p*.png)")
    else:
        print("\nNo color hits found")


if __name__ == "__main__":
    main()
