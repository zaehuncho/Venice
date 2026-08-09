#!/usr/bin/env python3
"""Test retrained park bar model on all videos."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cv2
import numpy as np
from ultralytics import YOLO

def test_model(model_path, vpath, n_samples=300, start_frame=500):
    m = YOLO(model_path)
    cap = cv2.VideoCapture(vpath)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    lo = min(start_frame, total - 1)
    
    for conf in [0.30, 0.10, 0.05, 0.01]:
        hits = miss = 0
        for fi in np.linspace(lo, min(total - 1, 15000), n_samples).astype(int):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(fi))
            ok, fr = cap.read()
            if not ok: continue
            r = m.predict(fr, verbose=False, conf=conf)[0]
            if r.boxes is not None and len(r.boxes):
                # Filter plausible bars
                found = False
                for bi in range(len(r.boxes)):
                    x1, y1, x2, y2 = r.boxes.xyxy.cpu().numpy()[bi].astype(int)
                    bw, bh = x2 - x1, y2 - y1
                    if 10 <= bw <= 250 and 10 <= bh <= 250:
                        ar = max(bw, bh) / max(min(bw, bh), 1)
                        if ar >= 1.5:
                            found = True
                            break
                if found:
                    hits += 1
                else:
                    miss += 1
            else:
                miss += 1
        rate = 100 * hits / max(hits + miss, 1)
        print(f"  conf={conf:.2f}: {hits}/{hits+miss} ({rate:.1f}%)")
    cap.release()

def main():
    model = "models/orion_bar_park.pt"
    videos = [
        r"C:\Users\Administrator\Videos\NBA 2K26_20260324195410.mp4",
        r"C:\Users\Administrator\Videos\NBA 2K26_20260618064057.mp4",
        r"C:\Users\Administrator\Videos\NBA 2K26_20260521032018.mp4",
        r"C:\Users\Administrator\Videos\2026-06-06 12-47-20.mp4",
    ]
    for v in videos:
        if not os.path.exists(v): continue
        print(f"\n=== {os.path.basename(v)} ===")
        test_model(model, v)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
