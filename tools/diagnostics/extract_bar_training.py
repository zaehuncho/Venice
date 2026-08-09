#!/usr/bin/env python3
"""Extract diverse park gameplay frames for stamina bar retraining.
Samples frames evenly, skips cutscenes (detected by lack of yellow+blue pixels),
and saves frames where the bar model has any detection (even low confidence).
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cv2
import numpy as np
from ultralytics import YOLO

def extract_frames(vpath, bar_model, out_dir, prefix, max_frames=80):
    cap = cv2.VideoCapture(vpath)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    H, W = 0, 0
    
    # Sample every ~100 frames in gameplay sections
    saved = 0
    # Dense sample: every 200 frames from 10% to 90% of video
    indices = list(range(total//10, total*9//10, max(1, total//500)))
    
    for fi in indices:
        if saved >= max_frames:
            break
        cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ok, fr = cap.read()
        if not ok:
            continue
        if H == 0:
            H, W = fr.shape[:2]
        
        # Quick check: must have some yellow+blue pixels (bar colors present)
        hsv = cv2.cvtColor(fr, cv2.COLOR_BGR2HSV)
        ym = cv2.inRange(hsv, (15, 80, 120), (40, 255, 255))
        bm = cv2.inRange(hsv, (90, 80, 90), (128, 255, 255))
        yp = int(ym.sum() / 255)
        bp = int(bm.sum() / 255)
        # Skip frames with too few bar-colored pixels (cutscenes/menus)
        if yp < 200 or bp < 200:
            continue
        
        # Run model at very low conf to find bar
        r = bar_model.predict(fr, verbose=False, conf=0.01)[0]
        has_bar = False
        if r.boxes is not None and len(r.boxes):
            for bi in range(len(r.boxes)):
                x1, y1, x2, y2 = r.boxes.xyxy.cpu().numpy()[bi].astype(int)
                bw, bh = x2 - x1, y2 - y1
                if 10 <= bw <= 250 and 10 <= bh <= 250:
                    ar = max(bw, bh) / max(min(bw, bh), 1)
                    if ar >= 1.5:
                        has_bar = True
                        break
        
        if has_bar:
            cv2.imwrite(os.path.join(out_dir, f"{prefix}_f{fi:06d}.png"), fr)
            saved += 1
    
    cap.release()
    print(f"  {prefix}: extracted {saved} frames from {os.path.basename(vpath)} ({W}x{H} @ {fps:.0f}fps)")
    return saved

def main():
    out_dir = "logs/diagnostics/bar_training_frames"
    os.makedirs(out_dir, exist_ok=True)
    
    bar = YOLO("models/orion_bar_n.pt")
    
    videos = [
        (r"C:\Users\Administrator\Videos\NBA 2K26_20260324195410.mp4", "park15min"),
        (r"C:\Users\Administrator\Videos\NBA 2K26_20260618064057.mp4", "park5min_a"),
        (r"C:\Users\Administrator\Videos\NBA 2K26_20260521032018.mp4", "park5min_b"),
    ]
    
    total = 0
    for vpath, prefix in videos:
        if not os.path.exists(vpath):
            print(f"  SKIP: {vpath}")
            continue
        total += extract_frames(vpath, bar, out_dir, prefix)
    
    print(f"\nTotal extracted: {total} frames -> {out_dir}/")
    print("Next: label these in YOLO format and retrain orion_bar_n.pt")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
