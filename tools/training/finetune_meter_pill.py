#!/usr/bin/env python
"""Fine-tune the shipped 2K27 meter detector to add the Pill (park/rec) style.

Recipe: best.pt-as-init (NOT resume=True) -- the project's established fine-tune
pattern (pose_finetune_efficient.py) -- starting from meter2k27_n2/weights/best.pt,
on the COMBINED Arrow2+Pill dataset (datasets/meter2k27_combo.yaml), with the same
gentle-AdamW/no-pose-augment schedule that train_meter_2k27.py records as the fix
for the lr-ramp divergence, at a lower lr0 (0.0003) since this is a fine-tune of an
already-converged model, not a fresh train.
"""
from __future__ import annotations
import os, sys, argparse


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="datasets/meter2k27_combo.yaml")
    ap.add_argument("--base", default="runs/detect/logs/diagnostics/meter_train/meter2k27_n2/weights/best.pt")
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--imgsz", type=int, default=1280)   # matches meter2k27_n2 training
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--name", default="meter2k27_n3_pill")
    a = ap.parse_args()
    from ultralytics import YOLO
    m = YOLO(a.base)
    m.train(
        data=a.data, epochs=a.epochs, imgsz=a.imgsz, batch=a.batch,
        project="logs/diagnostics/meter_train", name=a.name, exist_ok=True,
        patience=15, cache=False, workers=4, seed=1234,
        optimizer="AdamW", lr0=0.0003, lrf=0.05, warmup_epochs=0.5, cos_lr=True,
        # HUD element: always upright, never mirrored -> no pose augments.
        degrees=0.0, shear=0.0, perspective=0.0, flipud=0.0, fliplr=0.0,
        mosaic=0.3, scale=0.4, translate=0.1,
        # arena lighting / court colour vary hugely between venues.
        hsv_h=0.015, hsv_s=0.5, hsv_v=0.4,
    )
    print("best:", os.path.join("logs/diagnostics/meter_train", a.name, "weights", "best.pt"))


if __name__ == "__main__":
    sys.exit(main())
