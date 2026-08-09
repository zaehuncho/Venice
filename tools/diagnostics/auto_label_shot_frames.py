#!/usr/bin/env python3
"""Auto-label shot-animation frames where v9 failed, using the pose model.

For each frame with an empty label (v9 missed), runs the pose model to find
persons, picks the largest/most-central one, and writes a Player (class 0) box.
Also runs the stamina bar detector to add Stamina (class 1) labels where found.

This bridges the gap: v9's pseudo-labels cover frames where it already works,
but the frames where it FAILS (shot animations) are the ones we need labeled
for retraining. The pose model can find persons even mid-animation.

Usage:
    C:\\Python314\\python.exe tools\\diagnostics\\auto_label_shot_frames.py
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="logs/diagnostics/player_shot_label")
    ap.add_argument("--pose_model", default="models/orion_pose2k_n_v2.pt")
    ap.add_argument("--bar_model", default="models/orion_bar_park.pt")
    args = ap.parse_args()

    from ultralytics import YOLO

    img_dir = os.path.join(args.data, "images", "train")
    lbl_dir = os.path.join(args.data, "labels", "train")

    pose = YOLO(os.path.join(ROOT, args.pose_model))
    bar_det = YOLO(os.path.join(ROOT, args.bar_model))

    # Find empty-label files
    all_lbls = sorted(glob.glob(os.path.join(lbl_dir, "*.txt")))
    empty = [f for f in all_lbls if os.path.getsize(f) == 0]
    print(f"Empty labels to auto-fill: {len(empty)}")

    filled = 0
    for lbl_path in empty:
        stem = os.path.splitext(os.path.basename(lbl_path))[0]
        img_path = os.path.join(img_dir, stem + ".jpg")
        if not os.path.exists(img_path):
            continue

        frame = cv2.imread(img_path)
        if frame is None:
            continue
        H, W = frame.shape[:2]

        lines = []

        # 1. Pose model to find Player boxes
        r = pose.predict(frame, verbose=False, conf=0.25, imgsz=640)[0]
        if r.boxes is not None and len(r.boxes):
            xyxy = r.boxes.xyxy.cpu().numpy()
            confs = r.boxes.conf.cpu().numpy()
            # Pick largest person by area
            areas = (xyxy[:, 2] - xyxy[:, 0]) * (xyxy[:, 3] - xyxy[:, 1])
            # Filter: must be reasonably sized (not tiny background persons)
            mask = areas > (W * H * 0.01)  # at least 1% of frame
            if mask.any():
                xyxy_f = xyxy[mask]
                areas_f = areas[mask]
                confs_f = confs[mask]
                bi = int(np.argmax(areas_f))
                x1, y1, x2, y2 = xyxy_f[bi]
                cx = (x1 + x2) / 2 / W
                cy = (y1 + y2) / 2 / H
                w = (x2 - x1) / W
                h = (y2 - y1) / H
                lines.append(f"0 {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")

                # Also check for other large persons -> label as Unplayable (class 2)
                for i in range(len(xyxy_f)):
                    if i == bi:
                        continue
                    if areas_f[i] > (W * H * 0.02):
                        x1, y1, x2, y2 = xyxy_f[i]
                        cx = (x1 + x2) / 2 / W
                        cy = (y1 + y2) / 2 / H
                        w = (x2 - x1) / W
                        h = (y2 - y1) / H
                        lines.append(f"2 {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")

        # 2. Bar detector for Stamina (class 1)
        rb = bar_det.predict(frame, verbose=False, conf=0.3, imgsz=640)[0]
        if rb.boxes is not None and len(rb.boxes):
            xywh = rb.boxes.xywh.cpu().numpy()
            cls_b = rb.boxes.cls.cpu().numpy().astype(int)
            for i in range(len(cls_b)):
                x, y, w, h = xywh[i]
                lines.append(f"1 {x/W:.6f} {y/H:.6f} {w/W:.6f} {h/H:.6f}")

        if lines:
            with open(lbl_path, "w") as f:
                f.write("\n".join(lines))
            filled += 1

    print(f"\nAuto-filled {filled}/{len(empty)} empty labels")
    print(f"Remaining empty: {len(empty) - filled}")

    # Stats on all labels now
    all_lbls = sorted(glob.glob(os.path.join(lbl_dir, "*.txt")))
    pos = sum(1 for f in all_lbls if os.path.getsize(f) > 0)
    print(f"\nFinal: {pos}/{len(all_lbls)} frames with labels")

    # Count class distribution
    class_counts = {0: 0, 1: 0, 2: 0, 3: 0}
    for f in all_lbls:
        if os.path.getsize(f) == 0:
            continue
        for line in open(f):
            parts = line.strip().split()
            if parts:
                c = int(parts[0])
                class_counts[c] = class_counts.get(c, 0) + 1
    print(f"Class distribution: {class_counts}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
