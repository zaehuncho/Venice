#!/usr/bin/env python3
"""Self-training step 3: compare the base nano vs the fine-tuned student on held-out 2K frames.

Measures detection rate, mean confidence, and mean visible keypoints on the dataset's val split (which
the student never trained on). A win = the fine-tuned nano matches/beats the base nano on 2K. See
docs/ANIMATION_ANCHOR.md.

Usage:
  C:\\Python314\\python.exe tools/training/pose_eval_compare.py \
      [--val logs/diagnostics/pose_ds/images/val] \
      [--models yolov8n-pose.pt yolo11n-pose.pt logs/diagnostics/pose_train/pose2k_n/weights/best.pt]
"""
from __future__ import annotations

import argparse
import glob
import os

import numpy as np


def evaluate(model_path: str, frames):
    from ultralytics import YOLO
    import cv2
    m = YOLO(model_path)
    det = 0
    confs = []
    kps = []
    for f in frames:
        img = cv2.imread(f)
        if img is None:
            continue
        r = m.predict(img, verbose=False, conf=0.10)[0]
        if r.boxes is not None and len(r.boxes):
            det += 1
            b = int(np.argmax(r.boxes.conf.cpu().numpy()))
            confs.append(float(r.boxes.conf.cpu().numpy()[b]))
            kps.append(int((r.keypoints.data.cpu().numpy()[b][:, 2] > 0.5).sum()))
    n = max(1, len(frames))
    return 100 * det / n, (np.mean(confs) if confs else 0.0), (np.mean(kps) if kps else 0.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--val", default="logs/diagnostics/pose_ds/images/val")
    ap.add_argument("--models", nargs="+", default=[
        "yolov8n-pose.pt", "yolo11n-pose.pt",
        "logs/diagnostics/pose_train/pose2k_n/weights/best.pt",
    ])
    args = ap.parse_args()
    os.environ.setdefault("YOLO_VERBOSE", "False")
    frames = sorted(glob.glob(os.path.join(args.val, "*.jpg")))
    print(f"held-out val frames: {len(frames)}")
    if not frames:
        print("no val frames (run the dataset + train first)")
        return 1
    print(f"{'model':58} detect%  meanConf  meanKp>0.5")
    for mp in args.models:
        if not (os.path.isfile(mp) or not mp.endswith(".pt") or "/" not in mp):
            # allow bare auto-download names (no slash); skip missing local paths
            if "/" in mp and not os.path.isfile(mp):
                print(f"{mp:58} (missing)")
                continue
        d, c, k = evaluate(mp, frames)
        print(f"{mp:58} {d:5.0f}%   {c:.2f}      {k:.1f}/17")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
