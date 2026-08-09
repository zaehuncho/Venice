#!/usr/bin/env python3
"""Evaluate stamina bar detection rate on actual gameplay videos."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cv2
import numpy as np
from stamina_lock import find_stamina_bar, _bar_model


def eval_video(vpath, n_samples=300, start_frame=500):
    cap = cv2.VideoCapture(vpath)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    hits = miss = 0
    miss_frames = []
    lo = min(start_frame, total - 1)
    for fi in np.linspace(lo, min(total - 1, 10000), n_samples).astype(int):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(fi))
        ok, fr = cap.read()
        if not ok:
            continue
        b = find_stamina_bar(fr)
        if b:
            hits += 1
        else:
            miss += 1
            miss_frames.append(int(fi))
    cap.release()
    rate = 100 * hits / max(hits + miss, 1)
    return hits, miss, rate, miss_frames


def main():
    m = _bar_model()
    print(f"Bar model: {'YES' if m else 'NO (HSV fallback)'}")
    videos = [
        r"C:\Users\Administrator\Videos\2026-06-06 12-47-20.mp4",
        r"C:\Users\Administrator\Videos\2026-06-06 20-57-58.mp4",
    ]
    for v in videos:
        if not os.path.exists(v):
            print(f"SKIP (not found): {v}")
            continue
        hits, miss, rate, miss_frames = eval_video(v)
        print(f"\n{os.path.basename(v)}")
        print(f"  hits={hits} miss={miss} rate={rate:.1f}%")
        if miss_frames and len(miss_frames) <= 20:
            print(f"  miss frames: {miss_frames}")
        elif miss_frames:
            print(f"  miss frames (first 20): {miss_frames[:20]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
