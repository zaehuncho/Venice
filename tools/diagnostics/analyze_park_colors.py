#!/usr/bin/env python3
"""Analyze park video frames: check for meter colors, stamina bar colors, and UI elements."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cv2
import numpy as np

def analyze_frame(frame, label=""):
    H, W = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    
    # Check for meter colors (green/yellow/orange/red)
    green = cv2.inRange(hsv, (35, 50, 50), (85, 255, 255))
    yellow = cv2.inRange(hsv, (15, 80, 120), (40, 255, 255))
    orange = cv2.inRange(hsv, (10, 100, 100), (20, 255, 255))
    red1 = cv2.inRange(hsv, (0, 80, 80), (10, 255, 255))
    red2 = cv2.inRange(hsv, (170, 80, 80), (180, 255, 255))
    red = red1 | red2
    blue = cv2.inRange(hsv, (90, 80, 90), (128, 255, 255))
    
    # Check for gray meter background
    gray_meter = cv2.inRange(hsv, (0, 0, 15), (179, 55, 100))
    
    print(f"  {label}:")
    print(f"    green={int(green.sum()/255):>6} yellow={int(yellow.sum()/255):>6} orange={int(orange.sum()/255):>6} red={int(red.sum()/255):>6} blue={int(blue.sum()/255):>6}")
    print(f"    gray_meter={int(gray_meter.sum()/255):>6}")
    
    # Find largest green contour (meter)
    green_cnts = cv2.findContours(green, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
    if green_cnts:
        c = max(green_cnts, key=cv2.contourArea)
        x,y,w,h = cv2.boundingRect(c)
        area = cv2.contourArea(c)
        if area > 50:
            print(f"    largest_green: x={x} y={y} w={w} h={h} area={area:.0f}")
    
    # Find yellow+blue contours (stamina bar)
    yb = yellow | blue
    yb_dilated = cv2.dilate(yb, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15)))
    yb_cnts = cv2.findContours(yb_dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
    yb_boxes = []
    for c in yb_cnts:
        x,y,w,h = cv2.boundingRect(c)
        if w >= 15 and h >= 15:
            ysum = int(yellow[y:y+h, x:x+w].sum())
            bsum = int(blue[y:y+h, x:x+w].sum())
            if ysum > 0 and bsum > 0:
                yb_boxes.append((x,y,w,h, ysum, bsum))
    if yb_boxes:
        yb_boxes.sort(key=lambda b: min(b[4], b[5]), reverse=True)
        for b in yb_boxes[:3]:
            print(f"    stamina_candidate: x={b[0]} y={b[1]} w={b[2]} h={b[3]} y_px={b[4]} b_px={b[5]}")

def main():
    videos = [
        (r"C:\Users\Administrator\Videos\NBA 2K26_20260324195410.mp4", "15min"),
        (r"C:\Users\Administrator\Videos\NBA 2K26_20260618064057.mp4", "5min_1"),
        (r"C:\Users\Administrator\Videos\NBA 2K26_20260521032018.mp4", "5min_2"),
    ]
    for vpath, label in videos:
        if not os.path.exists(vpath): continue
        cap = cv2.VideoCapture(vpath)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        print(f"\n=== {label}: {os.path.basename(vpath)} ===")
        # Sample 5 frames from gameplay
        for fi in np.linspace(total//4, total*3//4, 5).astype(int):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(fi))
            ok, fr = cap.read()
            if not ok: continue
            analyze_frame(fr, f"frame {fi}")
        cap.release()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
