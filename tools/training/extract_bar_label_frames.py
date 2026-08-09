#!/usr/bin/env python3
"""Extract a diverse frame set from the user clips for STAMINA-BAR labeling (-> train a bar detector).

Samples frames across all clips and court types (Park/Rec/Theatre), evenly spaced + a few extra around
motion, so the labeled set covers both bar orientations + floor colors + shooting moments. Pre-runs the
HSV detector to write a PROPOSED box per frame (so the labeler can pre-fill and you mostly just confirm).

Out: logs/diagnostics/bar_label/images/<stem>.jpg + proposals.json  (then run label_bars.py).
Usage: C:\\Python314\\python.exe tools/training/extract_bar_label_frames.py [--per-clip 12]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "diagnostics"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips-glob",
                    default=r"C:/Users/Administrator/.claude/uploads/6c02bdee-81f8-4af7-8256-c327f3d49019/*master_playlist.mp4")
    ap.add_argument("--per-clip", type=int, default=12)
    ap.add_argument("--out", default="logs/diagnostics/bar_label")
    args = ap.parse_args()

    import cv2
    import stamina_lock as SL

    os.makedirs(os.path.join(args.out, "images"), exist_ok=True)
    clips = sorted(glob.glob(args.clips_glob))
    proposals = {}
    total = 0
    for clip in clips:
        cap = cv2.VideoCapture(clip)
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        base = os.path.splitext(os.path.basename(clip))[0][:12]
        for fi in np.linspace(int(n * 0.05), n - 1, args.per_clip).astype(int):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(fi))
            ok, fr = cap.read()
            if not ok:
                continue
            stem = f"{base}_{int(fi):06d}"
            cv2.imwrite(os.path.join(args.out, "images", stem + ".jpg"), fr, [cv2.IMWRITE_JPEG_QUALITY, 92])
            bar = SL.find_stamina_bar(fr)        # HSV proposal (may be None / wrong)
            proposals[stem] = list(map(int, bar)) if bar else None
            total += 1
        cap.release()
    with open(os.path.join(args.out, "proposals.json"), "w") as fh:
        json.dump(proposals, fh, indent=0)
    print(f"extracted {total} frames -> {args.out}/images  (+ proposals.json)")
    print("next: C:\\Python314\\python.exe tools/training/label_bars.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
