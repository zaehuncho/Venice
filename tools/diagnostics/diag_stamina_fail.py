#!/usr/bin/env python3
"""Diagnose stamina bar detection failures on gameplay videos."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cv2
import numpy as np

def main():
    from ultralytics import YOLO
    bar = YOLO("models/orion_bar_n.pt")
    vpath = r"C:\Users\Administrator\Videos\2026-06-06 12-47-20.mp4"
    cap = cv2.VideoCapture(vpath)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    H, W = 0, 0

    # Test at various confidence thresholds
    for conf_thresh in [0.30, 0.20, 0.10, 0.05]:
        hits = 0
        frames_tested = 0
        for fi in np.linspace(1000, 5000, 50).astype(int):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(fi))
            ok, fr = cap.read()
            if not ok: continue
            frames_tested += 1
            if H == 0:
                H, W = fr.shape[:2]
            r = bar.predict(fr, verbose=False, conf=conf_thresh)[0]
            if r.boxes is not None and len(r.boxes):
                hits += 1
        print(f"  conf={conf_thresh:.2f}: {hits}/{frames_tested} hits ({100*hits//max(frames_tested,1)}%)")
    cap.release()
    print(f"  Frame size: {W}x{H}")

    # Save a few frames with detections at conf=0.10 for visual inspection
    os.makedirs("logs/diagnostics/bar_debug", exist_ok=True)
    cap = cv2.VideoCapture(vpath)
    saved = 0
    for fi in np.linspace(1000, 5000, 100).astype(int):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(fi))
        ok, fr = cap.read()
        if not ok: continue
        r = bar.predict(fr, verbose=False, conf=0.10)[0]
        if r.boxes is not None and len(r.boxes):
            b = int(r.boxes.conf.cpu().numpy().argmax())
            x1,y1,x2,y2 = r.boxes.xyxy.cpu().numpy()[b].astype(int)
            conf_val = r.boxes.conf.cpu().numpy()[b]
            cv2.rectangle(fr, (x1,y1), (x2,y2), (0,255,0), 2)
            cv2.putText(fr, f"conf={conf_val:.2f}", (x1, max(20,y1-5)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,0), 2)
            cv2.imwrite(f"logs/diagnostics/bar_debug/det_f{fi}.png", fr)
            saved += 1
            if saved >= 5: break
    cap.release()
    print(f"  Saved {saved} detection frames to logs/diagnostics/bar_debug/")

    # Also save a few MISSED frames to see what we're missing
    cap = cv2.VideoCapture(vpath)
    saved_miss = 0
    for fi in np.linspace(1000, 5000, 100).astype(int):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(fi))
        ok, fr = cap.read()
        if not ok: continue
        r = bar.predict(fr, verbose=False, conf=0.10)[0]
        if r.boxes is None or not len(r.boxes):
            cv2.imwrite(f"logs/diagnostics/bar_debug/miss_f{fi}.png", fr)
            saved_miss += 1
            if saved_miss >= 3: break
    cap.release()
    print(f"  Saved {saved_miss} miss frames to logs/diagnostics/bar_debug/")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
