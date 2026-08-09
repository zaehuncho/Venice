#!/usr/bin/env python3
"""Check if stamina bar miss frames are replays/cutscenes or real misses."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cv2
import numpy as np

def check_yellow_blue(frame):
    """Count yellow+blue stamina bar colored pixels."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    ym = cv2.inRange(hsv, (15, 80, 120), (40, 255, 255))
    bm = cv2.inRange(hsv, (90, 80, 90), (128, 255, 255))
    return int(ym.sum() / 255), int(bm.sum() / 255)

def main():
    vpath = r"C:\Users\Administrator\Videos\2026-06-06 12-47-20.mp4"
    cap = cv2.VideoCapture(vpath)
    miss_frames = [2642, 4622, 4702, 4809, 6200, 6441]
    hit_frames = [3000, 3500, 4000, 5500, 6000]
    print("Miss frames (yellow_px, blue_px):")
    for fi in miss_frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ok, fr = cap.read()
        if not ok: continue
        yp, bp = check_yellow_blue(fr)
        # Check if it looks like a replay (gray/dark frame, no court)
        gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        brightness = float(gray.mean())
        print(f"  f{fi}: yellow={yp} blue={bp} brightness={brightness:.0f}")
    print("\nHit frames (yellow_px, blue_px):")
    for fi in hit_frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ok, fr = cap.read()
        if not ok: continue
        yp, bp = check_yellow_blue(fr)
        gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        brightness = float(gray.mean())
        print(f"  f{fi}: yellow={yp} blue={bp} brightness={brightness:.0f}")
    cap.release()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
