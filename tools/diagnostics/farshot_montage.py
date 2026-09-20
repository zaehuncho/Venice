"""farshot_montage.py -- eyeball helper: montage the dumped frames around one press.

Usage:
  .venv/Scripts/python.exe tools/diagnostics/farshot_montage.py --epoch 4 --out C:\\tmp\\ep4.png
  .venv/Scripts/python.exe tools/diagnostics/farshot_montage.py --idx 1234 1240 --scale 1.0
"""
from __future__ import annotations

import argparse
import bisect
import csv
import os
import sys

import cv2
import numpy as np

DUMP = r"D:\NexusVision\framedump\session_20260912_201355"
OUT = os.path.join("logs", "diagnostics", "farshot_study")


def frame_index():
    idx = {}
    for n in os.listdir(DUMP):
        if n.startswith("f") and n.endswith("_raw.png"):
            try:
                idx[int(n[1:6])] = os.path.join(DUMP, n)
            except ValueError:
                pass
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epoch", type=int, default=None)
    ap.add_argument("--idx", nargs="+", type=int, default=None)
    ap.add_argument("--win-s", type=float, default=1.3)
    ap.add_argument("--pre-s", type=float, default=0.2)
    ap.add_argument("--scale", type=float, default=0.5)
    ap.add_argument("--cols", type=int, default=3)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rows = list(csv.DictReader(open(os.path.join(DUMP, "frames.csv"))))
    walls = [float(r["t_wall"]) for r in rows]
    files = frame_index()

    if args.idx:
        want = list(args.idx)
    else:
        p = next(r for r in csv.DictReader(open(os.path.join(OUT, "presses.csv")))
                 if int(r["epoch"]) == args.epoch)
        pt = float(p["press_wall"])
        i0 = bisect.bisect_left(walls, pt - args.pre_s)
        i1 = bisect.bisect_right(walls, pt + args.win_s)
        want = [int(rows[i]["idx"]) for i in range(i0, i1)]
        print(f"epoch {args.epoch} {p['shot_type']} {p['kind']} {p['detail']} frames {want}")

    tiles = []
    for i in want:
        im = cv2.imread(files[i])
        if im is None:
            continue
        if args.scale != 1.0:
            im = cv2.resize(im, None, fx=args.scale, fy=args.scale, interpolation=cv2.INTER_AREA)
        cv2.putText(im, f"{i}", (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        tiles.append(im)
    cols = args.cols
    rowsn = (len(tiles) + cols - 1) // cols
    h, w = tiles[0].shape[:2]
    canvas = np.zeros((rowsn * h, cols * w, 3), np.uint8)
    for k, t in enumerate(tiles):
        r, c = divmod(k, cols)
        canvas[r * h:r * h + t.shape[0], c * w:c * w + t.shape[1]] = t
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    cv2.imwrite(args.out, canvas)
    print("wrote", args.out, canvas.shape)


if __name__ == "__main__":
    sys.exit(main())
