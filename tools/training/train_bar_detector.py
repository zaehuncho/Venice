#!/usr/bin/env python3
"""Train a dedicated STAMINA-BAR detector (YOLO, 1 class) from the labeled frames.

A learned detector handles what HSV color can't: wood floors (yellow-ish), both bar orientations,
motion blur during the jump -> a robust through-shot user lock for the no-meter calibration. After
training, point stamina_lock at models/orion_bar_n.pt to replace the HSV find_stamina_bar.

Usage: C:\\Python314\\python.exe tools/training/train_bar_detector.py [--epochs 120] [--imgsz 960]
"""
from __future__ import annotations

import argparse
import glob
import os


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="logs/diagnostics/bar_label/bars.yaml")
    ap.add_argument("--base", default="yolo11n.pt", help="detection (not pose) base")
    ap.add_argument("--epochs", type=int, default=120)
    ap.add_argument("--imgsz", type=int, default=960, help="larger -> small bar resolves better")
    ap.add_argument("--name", default="bar_n")
    args = ap.parse_args()

    os.environ.setdefault("YOLO_VERBOSE", "False")
    from ultralytics import YOLO

    labels = glob.glob("logs/diagnostics/bar_label/labels/*.txt")
    pos = sum(1 for f in labels if os.path.getsize(f) > 0)
    print(f"labeled files: {len(labels)} ({pos} with a bar box)")
    if pos < 20:
        print(f"only {pos} positive labels - label ~40+ frames (label_bars.py) before training for a robust detector")
        return 1
    m = YOLO(args.base)
    m.train(data=args.data, epochs=args.epochs, imgsz=args.imgsz, batch=16, device=0,
            workers=0,                          # Windows: avoid DataLoader worker crashes
            project="logs/diagnostics/bar_train", name=args.name, patience=25,
            degrees=0, shear=0, perspective=0,  # a HUD bar doesn't rotate/skew in world space
            mosaic=0.5, scale=0.3)
    best = f"logs/diagnostics/bar_train/{args.name}/weights/best.pt"
    if os.path.exists(best):
        import shutil
        os.makedirs("models", exist_ok=True)
        shutil.copy(best, "models/orion_bar_n.pt")
        print(f"DONE -> models/orion_bar_n.pt  (wire into stamina_lock.find_stamina_bar)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
