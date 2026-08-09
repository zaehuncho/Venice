#!/usr/bin/env python3
"""Scan a video for meter-dense windows with a specified color.
Wrapper around find_shot_windows logic but with configurable meter color."""
import os, sys, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from meter_detector import MeterDetector, load_detector_config
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STEP = 20

def scan(video, color):
    cfg = load_detector_config(os.path.join(ROOT, "settings.json"))
    cfg.meter_style = "Arrow2"
    cfg.meter_color = color
    cfg.auto_meter_color = False
    det = MeterDetector(os.path.join(ROOT, "meter_styles"), cfg)
    det.set_active_style("Arrow2")

    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        print(f"{os.path.basename(video)}: cannot open")
        return
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    hits, f = [], 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if f % STEP == 0:
            r = det.detect(frame)
            if r.detected and r.rejection_reason in ("", "green_not_found"):
                hits.append(f)
        f += 1
    cap.release()
    pct = 100.0 * len(hits) * STEP / max(total, 1)
    print(f"{os.path.basename(video)}: {total} frames | {color} meter-sample hits {len(hits)} "
          f"(~{pct:.0f}% present) | range {hits[0] if hits else '-'}..{hits[-1] if hits else '-'}")
    if hits:
        # Find top 3 densest 3000-frame windows
        windows = []
        for h in hits:
            cnt = sum(1 for x in hits if h <= x < h + 3000)
            windows.append((cnt, h))
        windows.sort(reverse=True)
        seen = []
        for cnt, start in windows[:10]:
            if all(abs(start - s) > 3000 for s in seen):
                seen.append(start)
                print(f"   densest 3000-frame window: start={start} ({cnt} hits "
                      f"≈ {cnt} shots — run eval_skele_pipeline.py here)")
                if len(seen) >= 3:
                    break

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--color", default="Red")
    args = ap.parse_args()
    scan(args.video, args.color)
