"""farshot_hunter.py -- gate-free hunt for ANY meter-like white column + green cap.

Deliberately NOT the shipped locator: it drops every band / size / shape / lone gate and
only keeps the two physical landmarks (an achromatic bright column, a green blob above it)
with WIDE tolerances, so a meter drawn at a different SCALE or a different tip gap still
shows up. Prints the geometry distribution so we can tell "the meter is drawn smaller /
higher / with a different gap" apart from "the meter is not drawn at all".

Usage:
  .venv/Scripts/python.exe tools/diagnostics/farshot_hunter.py --frames 716-746
  .venv/Scripts/python.exe tools/diagnostics/farshot_hunter.py --shots blind --out hunt_blind.csv
"""
from __future__ import annotations

import argparse
import bisect
import collections
import csv
import os
import sys

import cv2
import numpy as np

DUMP = r"D:\NexusVision\framedump\session_20260912_201355"
OUT = os.path.join("logs", "diagnostics", "farshot_study")

GREEN_LO = (38, 70, 70)
GREEN_HI = (85, 255, 255)

V_MIN = 225
SPREAD_MAX = 25
SUP = 4
W_LO, W_HI = 4, 34
H_LO, H_HI = 12, 175
GAP_LO, GAP_HI = 25, 150      # green top above white bottom
DX = 9


def frame_index():
    idx = {}
    for n in os.listdir(DUMP):
        if n.startswith("f") and n.endswith("_raw.png"):
            try:
                idx[int(n[1:6])] = os.path.join(DUMP, n)
            except ValueError:
                pass
    return idx


def hunt(img):
    """Return a list of (x, y, w, h, gap, green_px, solidity) candidates."""
    b, g, r = cv2.split(img)
    mx = cv2.max(cv2.max(b, g), r)
    mn = cv2.min(cv2.min(b, g), r)
    white = cv2.bitwise_and(
        cv2.threshold(mx, V_MIN - 1, 255, cv2.THRESH_BINARY)[1],
        cv2.threshold(cv2.subtract(mx, mn), SPREAD_MAX, 255, cv2.THRESH_BINARY_INV)[1])
    k = np.ones((5, 1), np.uint8)
    white = cv2.erode(cv2.dilate(white, k, anchor=(0, 2)), k, anchor=(0, 2))
    ks = np.ones((SUP, 1), np.uint8)
    col = cv2.dilate(cv2.erode(white, ks, anchor=(0, SUP // 2)), ks, anchor=(0, (SUP - 1) // 2))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(col, connectivity=8)
    if n <= 1:
        return []
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    gmask = cv2.inRange(hsv, GREEN_LO, GREEN_HI)
    H, W = img.shape[:2]
    out = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if not (W_LO <= w <= W_HI and H_LO <= h <= H_HI):
            continue
        if area < 0.30 * w * h:
            continue
        cx = x + w * 0.5
        wbot = y + h
        x0 = int(max(0, cx - DX)); x1 = int(min(W, cx + DX + 1))
        y0 = int(max(0, wbot - GAP_HI)); y1 = int(max(0, wbot - GAP_LO))
        if y1 <= y0 or x1 <= x0:
            continue
        sub = gmask[y0:y1, x0:x1]
        rowsum = sub.sum(axis=1) // 255
        rows = np.flatnonzero(rowsum >= 2)
        if rows.size == 0:
            continue
        gtop = y0 + int(rows.min())
        out.append((x, y, w, h, wbot - gtop, int(sub.sum() // 255), area / float(w * h)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", default="")
    ap.add_argument("--shots", choices=("blind", "seen", "none"), default="none")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    files = frame_index()
    F = list(csv.DictReader(open(os.path.join(DUMP, "frames.csv"))))
    walls = [float(r["t_wall"]) for r in F]

    want = []
    if args.frames:
        for part in args.frames.split(","):
            if "-" in part:
                a, b = part.split("-")
                want += list(range(int(a), int(b) + 1))
            else:
                want.append(int(part))
    if args.shots != "none":
        G = [r for r in csv.DictReader(open(os.path.join(DUMP, "panel_grade.csv"))) if r["word"]]
        for g in G:
            t0 = float(g["t0"])
            i0 = bisect.bisect_left(walls, t0 - 2.6)
            i1 = bisect.bisect_right(walls, t0 - 0.1)
            win = F[i0:i1]
            ndet = sum(1 for r in win if r["detected"] == "1")
            if (args.shots == "blind") != (ndet == 0):
                continue
            want += [int(r["idx"]) for r in win]
    want = sorted(set(want))
    print(f"hunting {len(want)} frames")

    recs = []
    gaps = collections.Counter()
    widths = collections.Counter()
    for i in want:
        p = files.get(i)
        if not p:
            continue
        img = cv2.imread(p)
        if img is None:
            continue
        for (x, y, w, h, gap, gpx, sol) in hunt(img):
            recs.append(dict(idx=i, x=x, y=y, w=w, h=h, gap=gap, gpx=gpx,
                             sol=round(sol, 2), cy=round((y + h / 2) / img.shape[0], 3),
                             prod_det=F[i]["detected"]))
            gaps[gap] += 1
            widths[w] += 1

    print(f"candidates: {len(recs)} over {len(want)} frames")
    print("gap (white_bottom - green_top) histogram, top 25:")
    for k, v in sorted(gaps.items()):
        if v >= max(2, len(recs) // 200):
            print(f"   gap={k:4d}  n={v}")
    print("column width histogram:", sorted(widths.items()))
    if args.out:
        os.makedirs(OUT, exist_ok=True)
        path = os.path.join(OUT, args.out)
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(recs[0].keys()))
            w.writeheader()
            w.writerows(recs)
        print("wrote", path)


if __name__ == "__main__":
    sys.exit(main())
