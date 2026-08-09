"""Locate the frame windows in a recording that actually CONTAIN shots, using the
shot METER as ground truth (where the Purple Arrow2 meter appears = a shot happened).

Why: the gym recordings are mostly no-shot footage (meters are sparse, ~1% of frames),
so the SKELE pose pipeline must be evaluated on windows that have real shots — otherwise
you measure landmark firing on footage with nothing to fire on. Run this first, then point
eval_skele_pipeline.py at the densest window.

Usage:
    C:\\Python314\\python.exe tools/diagnostics/find_shot_windows.py [video.mp4 ...]

For the SKELE (no-meter) track — pairs with eval_skele_pipeline.py. See
memory nexusvision-nometer-pose-mode.
"""
import os
os.environ.setdefault("YOLO_VERBOSE", "False")
import sys
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from meter_detector import MeterDetector, load_detector_config

STEP = 20  # sample every Nth frame (sequential grab, not seek, to stay fast)


def scan(video: str):
    cfg = load_detector_config(os.path.join(ROOT, "settings.json"))
    cfg.meter_style = "Arrow2"
    cfg.meter_color = "Purple"
    cfg.auto_meter_color = False  # mirror the live Purple-locked path
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
    print(f"{os.path.basename(video)}: {total} frames | meter-sample hits {len(hits)} "
          f"(~{pct:.0f}% present) | range {hits[0] if hits else '-'}..{hits[-1] if hits else '-'}")
    if hits:
        best_start, best_cnt = 0, 0
        for h in hits:
            cnt = sum(1 for x in hits if h <= x < h + 2500)
            if cnt > best_cnt:
                best_cnt, best_start = cnt, h
        print(f"   densest 2500-frame window: start={best_start} ({best_cnt} hits "
              f"≈ {best_cnt} shots — run eval_skele_pipeline.py here)")


if __name__ == "__main__":
    vids = sys.argv[1:] or [
        r"C:\Users\Administrator\Videos\NBA 2K26_20260324195410.mp4",
    ]
    for v in vids:
        scan(v)
