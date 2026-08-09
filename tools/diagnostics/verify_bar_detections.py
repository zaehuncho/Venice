#!/usr/bin/env python3
"""Verify stamina bar detections at conf=0.01 are real (consistent size, smooth movement)."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cv2
import numpy as np
from stamina_lock import find_stamina_bar

def main():
    vpath = r"C:\Users\Administrator\Videos\2026-06-06 12-47-20.mp4"
    cap = cv2.VideoCapture(vpath)
    boxes = []
    for fi in np.linspace(2000, 8000, 100).astype(int):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(fi))
        ok, fr = cap.read()
        if not ok: continue
        b = find_stamina_bar(fr)
        if b:
            boxes.append((fi, b))
    cap.release()

    if len(boxes) < 2:
        print("Not enough detections"); return 1

    ws = [b[2] for _, b in boxes]
    hs = [b[3] for _, b in boxes]
    xs = [b[0] for _, b in boxes]
    ys = [b[1] for _, b in boxes]

    print(f"Detections: {len(boxes)}/100")
    print(f"Width:  mean={np.mean(ws):.0f} std={np.std(ws):.0f} min={min(ws)} max={max(ws)}")
    print(f"Height: mean={np.mean(hs):.0f} std={np.std(hs):.0f} min={min(hs)} max={max(hs)}")
    print(f"X:      mean={np.mean(xs):.0f} std={np.std(xs):.0f} min={min(xs)} max={max(xs)}")
    print(f"Y:      mean={np.mean(ys):.0f} std={np.std(ys):.0f} min={min(ys)} max={max(ys)}")

    # Check for sudden jumps (false positives would have erratic positions)
    jumps_x = [abs(xs[i+1]-xs[i]) for i in range(len(xs)-1)]
    jumps_y = [abs(ys[i+1]-ys[i]) for i in range(len(ys)-1)]
    print(f"X jumps: mean={np.mean(jumps_x):.0f} max={max(jumps_x)}")
    print(f"Y jumps: mean={np.mean(jumps_y):.0f} max={max(jumps_y)}")

    # Check aspect ratios
    ars = [w/h for _, b in boxes for w, h in [(b[2], b[3])]]
    print(f"Aspect ratio (w/h): mean={np.mean(ars):.1f} std={np.std(ars):.1f}")

    # Flag any outliers
    w_med, w_mad = np.median(ws), np.median(np.abs(np.array(ws) - np.median(ws))) * 1.4826
    h_med, h_mad = np.median(hs), np.median(np.abs(np.array(hs) - np.median(hs))) * 1.4826
    outliers = []
    for fi, b in boxes:
        if abs(b[2] - w_med) > 3 * w_mad or abs(b[3] - h_med) > 3 * h_mad:
            outliers.append((fi, b))
    print(f"\nSize outliers: {len(outliers)}")
    for fi, b in outliers[:5]:
        print(f"  f{fi}: x={b[0]} y={b[1]} w={b[2]} h={b[3]}")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
