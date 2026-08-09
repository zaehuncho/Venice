#!/usr/bin/env python3
"""Bootstrap STAMINA-BAR labels with ZERO user effort: harvest the bar boxes from temporally-consistent
tracker locks across the clips. These are the EASY frames (clear bar) -> train a first detector that may
already generalize to the hard wood/vertical frames HSV misses. The user's hand labels then fill the gaps.

Out: appends images+labels to logs/diagnostics/bar_label/ (same dataset the user labels into).
Usage: C:\\Python314\\python.exe tools/training/auto_label_bars.py [--per-clip 10]
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "diagnostics"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips-glob",
                    default=r"C:/Users/Administrator/.claude/uploads/6c02bdee-81f8-4af7-8256-c327f3d49019/*master_playlist.mp4")
    ap.add_argument("--per-clip", type=int, default=10, help="max auto-labels kept per clip")
    ap.add_argument("--out", default="logs/diagnostics/bar_label")
    args = ap.parse_args()

    os.environ.setdefault("YOLO_VERBOSE", "False")
    import cv2
    from ultralytics import YOLO
    import stamina_lock as SL

    os.makedirs(os.path.join(args.out, "images"), exist_ok=True)
    os.makedirs(os.path.join(args.out, "labels"), exist_ok=True)
    m = YOLO("models/orion_pose2k_n.pt")
    clips = sorted(glob.glob(args.clips_glob))
    kept = 0
    for clip in clips:
        cap = cv2.VideoCapture(clip)
        base = os.path.splitext(os.path.basename(clip))[0][:12]
        trk = SL.StaminaTracker(m)
        streak = 0
        saved = 0
        last = -99
        fi = 0
        while saved < args.per_clip:
            ok, fr = cap.read()
            if not ok:
                break
            res = trk.update(fr)
            if res is not None and res.get("src") == "bar" and res.get("bar"):
                streak += 1
                x, y, w, h = res["bar"]
                H, W = fr.shape[:2]
                ok_pos = (0.20 * H < y < 0.86 * H) and w < 0.5 * W and h < 0.5 * H
                if streak >= 2 and ok_pos and (fi - last) >= 5:    # consistent, sane, decorrelated
                    stem = f"auto_{base}_{fi:06d}"
                    cv2.imwrite(os.path.join(args.out, "images", stem + ".jpg"), fr, [cv2.IMWRITE_JPEG_QUALITY, 92])
                    cx, cy = (x + w / 2) / W, (y + h / 2) / H
                    with open(os.path.join(args.out, "labels", stem + ".txt"), "w") as fh:
                        fh.write(f"0 {cx:.6f} {cy:.6f} {w / W:.6f} {h / H:.6f}\n")
                    saved += 1; kept += 1; last = fi
            else:
                streak = 0
            fi += 1
        cap.release()
        print(f"  {base}: auto-labeled {saved}")
    print(f"\nDONE: {kept} auto-labels -> {args.out}  (add user hand-labels, then train_bar_detector.py)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
