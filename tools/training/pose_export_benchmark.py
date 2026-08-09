#!/usr/bin/env python3
"""Step 3: quantify the low-latency path for the pose anchor (if it ever runs live).

Benchmarks the fine-tuned nano pose model under the latency levers from docs/ANIMATION_ANCHOR.md:
full-frame vs ROI-crop input, FP32 vs FP16, and (optionally) a TensorRT engine export. The target:
single-digit-ms so the pose anchor could run live without defeating the autogreener's latency.

Usage:
  C:\\Python314\\python.exe tools/training/pose_export_benchmark.py \
      [--model models/orion_pose2k_n.pt] [--runs 100] [--roi 256] [--full 640] [--trt]
"""
from __future__ import annotations

import argparse
import os
import time

import numpy as np


def bench(model, img, imgsz, half, runs):
    # warmup
    for _ in range(8):
        model.predict(img, imgsz=imgsz, half=half, verbose=False, device=0)
    t0 = time.perf_counter()
    for _ in range(runs):
        model.predict(img, imgsz=imgsz, half=half, verbose=False, device=0)
    return (time.perf_counter() - t0) / runs * 1000.0  # ms/frame


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="models/orion_pose2k_n.pt")
    ap.add_argument("--runs", type=int, default=100)
    ap.add_argument("--roi", type=int, default=256, help="ROI-crop input size (player cropped from the meter bbox)")
    ap.add_argument("--full", type=int, default=640)
    ap.add_argument("--trt", action="store_true", help="also export + benchmark a TensorRT FP16 engine")
    args = ap.parse_args()

    os.environ.setdefault("YOLO_VERBOSE", "False")
    try:
        import torch
        from ultralytics import YOLO
    except Exception as exc:
        print(f"need torch + ultralytics: {exc}")
        return 2
    mp = args.model if os.path.isfile(args.model) else "yolo11n-pose.pt"
    print(f"model={mp}  cuda={torch.cuda.is_available()}  device={torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'cpu'}")
    m = YOLO(mp)

    full_frame = np.random.randint(0, 255, (1080, 1920, 3), dtype=np.uint8)
    roi_crop = np.random.randint(0, 255, (args.roi, args.roi, 3), dtype=np.uint8)

    print(f"\n{'config':38} ms/frame   ~fps")
    for label, img, sz, half in [
        (f"full {args.full}px  FP32", full_frame, args.full, False),
        (f"full {args.full}px  FP16", full_frame, args.full, True),
        (f"ROI  {args.roi}px  FP32", roi_crop, args.roi, False),
        (f"ROI  {args.roi}px  FP16 (the live recipe)", roi_crop, args.roi, True),
    ]:
        try:
            ms = bench(m, img, sz, half, args.runs)
            print(f"{label:38} {ms:6.2f}    {1000.0/ms:5.0f}")
        except Exception as e:
            print(f"{label:38} (failed: {str(e)[:50]})")

    if args.trt:
        try:
            print("\nexporting TensorRT FP16 engine (ROI imgsz)...")
            eng = m.export(format="engine", half=True, imgsz=args.roi, device=0, verbose=False)
            me = YOLO(eng)
            ms = bench(me, roi_crop, args.roi, True, args.runs)
            print(f"{'TensorRT ROI ' + str(args.roi) + 'px FP16':38} {ms:6.2f}    {1000.0/ms:5.0f}")
        except Exception as e:
            print(f"TensorRT export/bench failed (TRT may not match cu126): {str(e)[:120]}")
    print("\nNote: live anchor would also run on a SEPARATE thread, SHOT-GATED (only when the meter is up),")
    print("so even a few ms is absorbed by the lead (the anchor is an EARLY signal). Default live = classical.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
