#!/usr/bin/env python3
"""Diagnose meter and stamina bar detection on park gameplay videos."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cv2
import numpy as np

def scan_video(vpath, n_frames=20):
    cap = cv2.VideoCapture(vpath)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    H, W = 0, 0
    
    # Sample frames from middle of video (gameplay)
    indices = np.linspace(total//4, total*3//4, n_frames).astype(int)
    
    os.makedirs("logs/diagnostics/park_debug", exist_ok=True)
    
    for i, fi in enumerate(indices):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(fi))
        ok, fr = cap.read()
        if not ok: continue
        if H == 0:
            H, W = fr.shape[:2]
        # Save downscaled frame for inspection
        small = cv2.resize(fr, (W//3, H//3))
        cv2.imwrite(f"logs/diagnostics/park_debug/frame_{i:02d}_f{fi}.png", small)
    
    cap.release()
    print(f"  {os.path.basename(vpath)}: {W}x{H} @ {fps}fps, {total} frames")
    print(f"  Saved {n_frames} frames to logs/diagnostics/park_debug/")

def main():
    videos = [
        r"C:\Users\Administrator\Videos\NBA 2K26_20260324195410.mp4",
        r"C:\Users\Administrator\Videos\NBA 2K26_20260618064057.mp4",
        r"C:\Users\Administrator\Videos\NBA 2K26_20260521032018.mp4",
    ]
    for v in videos:
        if os.path.exists(v):
            scan_video(v)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
