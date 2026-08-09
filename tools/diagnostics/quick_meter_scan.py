#!/usr/bin/env python3
"""Quick scan a video for meter hits with a given color."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from meter_detector import MeterDetector, load_detector_config
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
video = sys.argv[1]
color = sys.argv[2] if len(sys.argv) > 2 else "Purple"

cfg = load_detector_config(os.path.join(ROOT, "settings.json"))
cfg.meter_style = "Arrow2"
cfg.meter_color = color
cfg.auto_meter_color = False
m = MeterDetector(os.path.join(ROOT, "meter_styles"), cfg)
m.set_active_style("Arrow2")

cap = cv2.VideoCapture(video)
total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
fps = cap.get(cv2.CAP_PROP_FPS) or 60.0

hits = 0
total = 0
hit_frames = []
for i in range(0, total_frames, 60):
    cap.set(cv2.CAP_PROP_POS_FRAMES, i)
    ok, f = cap.read()
    if not ok:
        break
    total += 1
    r = m.detect(f)
    if r.detected:
        hits += 1
        hit_frames.append(i)

cap.release()
pct = 100 * hits / max(total, 1)
print(f"{os.path.basename(video)}: {total_frames} frames | {color} meter: {hits}/{total} hits ({pct:.0f}%)")
if hit_frames:
    print(f"  hit frames (sampled): {hit_frames[:20]}...")
