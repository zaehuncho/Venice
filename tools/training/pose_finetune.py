#!/usr/bin/env python3
"""Self-training step 2: fine-tune a fast STUDENT pose model on the pseudo-labeled 2K dataset.

Adapts a nano pose model to the 2K avatar domain (built by pose_pseudolabel_dataset.py) so it detects
the player as reliably as the x-pose teacher but at nano speed. See docs/ANIMATION_ANCHOR.md.

Usage:
  C:\\Python314\\python.exe tools/training/pose_finetune.py [--data logs/diagnostics/pose_ds/pose2k.yaml] \
      [--student yolo11n-pose.pt] [--epochs 40] [--imgsz 640] [--name pose2k_n]
"""
from __future__ import annotations

import argparse
import os


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="logs/diagnostics/pose_ds/pose2k.yaml")
    ap.add_argument("--student", default="yolo11n-pose.pt")
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--name", default="pose2k_n")
    args = ap.parse_args()

    os.environ.setdefault("YOLO_VERBOSE", "False")
    if not os.path.isfile(args.data):
        print(f"no dataset yaml: {args.data} (run pose_pseudolabel_dataset.py first)")
        return 2
    from ultralytics import YOLO

    m = YOLO(args.student)
    m.train(
        data=args.data, epochs=args.epochs, imgsz=args.imgsz, batch=args.batch,
        device=0, project="logs/diagnostics/pose_train", name=args.name, exist_ok=True,
        patience=15, verbose=False, plots=False,
    )
    best = f"logs/diagnostics/pose_train/{args.name}/weights/best.pt"
    print(f"DONE. fine-tuned student weights: {best}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
