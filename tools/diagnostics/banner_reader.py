#!/usr/bin/env python3
"""Read the 2K post-shot feedback banner (top-center TEMPO box) for the no-meter calibration label.

No OCR available, but the timing GRADE is color-coded: the verdict word is GREEN for a good release
(EXCELLENT/GOOD) and shifts to yellow/orange/red for early/late. For calibration we mainly need to
flag EXCELLENT shots (green) and timestamp the banner; early-vs-late direction would need OCR/text
templates (deferred -- the user's highlight clips are mostly greens anyway). See docs/ANIMATION_ANCHOR.md.

read_banner(frame) -> dict(present, verdict in {excellent, off (yellow/red), none}, green, warm).
"""
from __future__ import annotations

import cv2
import numpy as np

# the TEMPO verdict box sits top-center; the grade word is in its lower half
_RX0, _RX1, _RY0, _RY1 = 0.45, 0.60, 0.045, 0.115


def read_banner(frame):
    H, W = frame.shape[:2]
    sub = frame[int(H * _RY0):int(H * _RY1), int(W * _RX0):int(W * _RX1)]
    if sub.size == 0:
        return {"present": False, "verdict": "none", "green": 0.0, "warm": 0.0}
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    n = sub.shape[0] * sub.shape[1]
    green = int(cv2.inRange(hsv, (40, 90, 90), (85, 255, 255)).sum() // 255) / n
    warm = int(cv2.inRange(hsv, (0, 110, 110), (35, 255, 255)).sum() // 255) / n   # yellow/orange/red
    if green > 0.05 and green >= warm:
        verdict = "excellent"
    elif warm > 0.05:
        verdict = "off"            # mistimed (early/late) -- direction needs OCR
    else:
        verdict = "none"
    return {"present": verdict != "none", "verdict": verdict,
            "green": round(green, 3), "warm": round(warm, 3)}


if __name__ == "__main__":
    import glob
    import sys
    pat = sys.argv[1] if len(sys.argv) > 1 else "bdb298db"
    U = r"C:/Users/Administrator/.claude/uploads/6c02bdee-81f8-4af7-8256-c327f3d49019"
    clip = glob.glob(U + "/" + pat + "*master_playlist.mp4")[0]
    cap = cv2.VideoCapture(clip)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    exc = off = 0
    for fi in range(0, n, 6):
        cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ok, fr = cap.read()
        if not ok:
            break
        r = read_banner(fr)
        if r["verdict"] == "excellent":
            exc += 1
        elif r["verdict"] == "off":
            off += 1
    cap.release()
    print(f"{pat}: sampled {n // 6} frames -> EXCELLENT(green) {exc}, off(warm) {off}")
