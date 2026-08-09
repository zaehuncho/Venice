#!/usr/bin/env python3
"""Test bar model at various confidence levels on park videos."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cv2
import numpy as np
from ultralytics import YOLO

def main():
    bar = YOLO("models/orion_bar_n.pt")
    videos = [
        r"C:\Users\Administrator\Videos\NBA 2K26_20260324195410.mp4",
        r"C:\Users\Administrator\Videos\NBA 2K26_20260618064057.mp4",
        r"C:\Users\Administrator\Videos\NBA 2K26_20260521032018.mp4",
    ]
    for vpath in videos:
        if not os.path.exists(vpath): continue
        cap = cv2.VideoCapture(vpath)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        print(f"\n=== {os.path.basename(vpath)} ===")
        for conf in [0.30, 0.10, 0.05, 0.01]:
            hits = 0
            boxes_info = []
            frames_tested = 0
            for fi in np.linspace(total//4, total*3//4, 50).astype(int):
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(fi))
                ok, fr = cap.read()
                if not ok: continue
                frames_tested += 1
                r = bar.predict(fr, verbose=False, conf=conf)[0]
                if r.boxes is not None and len(r.boxes):
                    hits += 1
                    if len(boxes_info) < 3:
                        for bi in range(min(len(r.boxes), 3)):
                            x1,y1,x2,y2 = r.boxes.xyxy.cpu().numpy()[bi].astype(int)
                            c = float(r.boxes.conf.cpu().numpy()[bi])
                            boxes_info.append((fi, x1, y1, x2-x1, y2-y1, c))
            print(f"  conf={conf:.2f}: {hits}/{frames_tested} ({100*hits//max(frames_tested,1)}%)")
            for fi, x, y, w, h, c in boxes_info[:3]:
                print(f"    f{fi}: x={x} y={y} w={w} h={h} conf={c:.3f}")
        cap.release()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
