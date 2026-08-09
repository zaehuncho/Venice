#!/usr/bin/env python3
"""Compare the learned bar detector vs HSV on hard frames (wood floor / mid-shot) + render detections.

Runs models/orion_bar_n.pt and the HSV find_stamina_bar over a spread of frames per clip; reports the
detection rate of each and saves a few annotated frames (green=learned, red=HSV) to eyeball. The win
condition: the learned detector finds bars on the wood-floor/vertical/mid-jump frames HSV misses.
Usage: C:\\Python314\\python.exe tools/diagnostics/eval_bar_detector.py
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    os.environ.setdefault("YOLO_VERBOSE", "False")
    import cv2
    from ultralytics import YOLO
    import stamina_lock as SL

    if not os.path.exists("models/orion_bar_n.pt"):
        print("no models/orion_bar_n.pt yet - train first")
        return 1
    bar = YOLO("models/orion_bar_n.pt")
    U = r"C:/Users/Administrator/.claude/uploads/6c02bdee-81f8-4af7-8256-c327f3d49019"
    clips = sorted(glob.glob(U + "/*master_playlist.mp4"))
    OUT = "logs/diagnostics/userclips"
    learned_hit = hsv_hit = total = 0
    saved = 0
    for clip in clips:
        cap = cv2.VideoCapture(clip)
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        tag = os.path.basename(clip)[:8]
        for fi in np.linspace(int(n * 0.1), n - 1, 12).astype(int):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(fi))
            ok, fr = cap.read()
            if not ok:
                continue
            total += 1
            r = bar.predict(fr, verbose=False, conf=0.25)[0]
            lbox = None
            if r.boxes is not None and len(r.boxes):
                b = int(r.boxes.conf.cpu().numpy().argmax())
                x1, y1, x2, y2 = r.boxes.xyxy.cpu().numpy()[b].astype(int)
                lbox = (x1, y1, x2 - x1, y2 - y1); learned_hit += 1
            hbox = SL.find_stamina_bar(fr)
            if hbox:
                hsv_hit += 1
            if lbox and not hbox and saved < 6:           # learned found what HSV missed
                ann = fr.copy()
                cv2.rectangle(ann, (lbox[0], lbox[1]), (lbox[0] + lbox[2], lbox[1] + lbox[3]), (0, 255, 0), 3)
                cv2.putText(ann, "learned bar (HSV missed)", (lbox[0], max(20, lbox[1] - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                cv2.imwrite(f"{OUT}/bardet_{tag}_f{int(fi)}.png", ann); saved += 1
        cap.release()
    print(f"frames {total}: learned-detector hits {learned_hit} ({100*learned_hit//max(total,1)}%), "
          f"HSV hits {hsv_hit} ({100*hsv_hit//max(total,1)}%)")
    print(f"saved {saved} frames where the learned detector found a bar HSV missed -> {OUT}/bardet_*.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
