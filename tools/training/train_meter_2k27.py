#!/usr/bin/env python
"""Train a single-class detector for the NBA 2K27 white shot meter.

WHY A DETECTOR AND NOT ANOTHER COLOUR GATE. The shipped reader finds the meter by
masking white and gating on geometry. That is adequate while the brightest narrow
vertical thing on screen IS the meter, and it collapses when it is not: measured
2026-08-27, with the player in a white hoodie the reader's published box sat on the
real meter only 17.8% of the time, against 65-74% on the previous day's footage with
the SAME code. Six discriminators were tried against that footage and every one
failed its own A/B -- green corroboration, relaxed tip geometry, green content
(62% keep / 46% admit, near chance), the rail-pair structural scan (the true meter
was not in the top-10 candidates on 79% of frames), the prior-presence veto
(20.2% -> 20.6%), and the capless breaker (undermined here: the cap shows on 3%
of frames). Colour cannot separate "narrow white rectangle" from "white hoodie"
when they are adjacent and moving together -- the information is not in the colour.

The existing locator model (orion_meter_n_v6_mycourt960.pt) was trained on the OLD
RED meter and finds NOTHING on 2K27: 0 detections across 61 frames that contain a
plainly visible meter.
"""
from __future__ import annotations
import os, sys, argparse


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="datasets/meter2k27/data.yaml")
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--imgsz", type=int, default=960)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--base", default="yolo11n.pt")
    ap.add_argument("--name", default="meter2k27_n")
    a = ap.parse_args()
    from ultralytics import YOLO
    m = YOLO(a.base)
    m.train(
        data=a.data, epochs=a.epochs, imgsz=a.imgsz, batch=a.batch,
        project="logs/diagnostics/meter_train", name=a.name, exist_ok=True,
        patience=20, cache=False, workers=4, seed=1234,
        # FIXED LOW LR, NO WARMUP RAMP. With optimizer='auto' ultralytics picked
        # AdamW at lr0=0.002; the task was essentially solved at epoch 1
        # (mAP50 0.972, mAP50-95 0.832 at the warmup lr of 0.00066) and then the
        # ramp to 0.002 DIVERGED it -- by epoch 16 mAP50 was 0.006 and early
        # stopping killed the run. The object is small, high-contrast and highly
        # consistent, so it needs a gentle constant schedule, not a hot one.
        optimizer="AdamW", lr0=0.0005, lrf=0.05, warmup_epochs=0.5, cos_lr=True,
        # The meter is a small, high-contrast, axis-aligned HUD element that is
        # ALWAYS upright and never mirrored -- so the augmentations that would
        # teach a detector to generalise over pose are pure label noise here.
        degrees=0.0, shear=0.0, perspective=0.0, flipud=0.0, fliplr=0.0,
        mosaic=0.3, scale=0.4, translate=0.1,
        # Colour jitter DOES earn its place: arena lighting and the court beneath
        # the translucent track vary enormously between venues.
        hsv_h=0.015, hsv_s=0.5, hsv_v=0.4,
    )
    print("best:", os.path.join("logs/diagnostics/meter_train", a.name, "weights", "best.pt"))


if __name__ == "__main__":
    sys.exit(main())
