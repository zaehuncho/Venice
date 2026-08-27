#!/usr/bin/env python
"""Measure the NBA 2K26 WHITE meter across a framedump session.

One static frame cannot say which region is the RISING FILL and which is the
fixed make-window: it only shows a green block above a white block. A SEQUENCE
settles it trivially -- the region whose extent CHANGES from frame to frame is
the fill; the one that holds still is the window.

Also reports the numbers a White band row has to be built from:
  * white fill    : saturation ceiling + value floor  (achromatic, bright)
  * green window  : does the SHIPPED cap mask still find it? (_G / _NEON)
  * meter frame   : how much headroom a white value floor really has
  * background    : worst-case competing white in the frame (court lines, text)

Usage:
    python tools/diagnostics/white_meter_probe.py <session_dir> [--roi x0 y0 x1 y1]

<session_dir> holds fNNNNN_D_raw.png as written by run_orion.framedump.ps1
(D:\\VeniceTraining\\framedump\\session_<timestamp>).
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys

import cv2
import numpy as np

_FRAME_RE = re.compile(r"f(\d{5})_([01])_raw\.png$")

# The SHIPPED cap masks -- do not retune here; the point is to test whether the
# new meter still satisfies what the reader already looks for.
_G_LO, _G_HI = (38, 90, 90), (85, 255, 255)
_NEON_LO, _NEON_HI = (48, 140, 140), (70, 255, 255)

# Candidate white-fill gate (meter_detector._COLOR_PURITY_GATES["White"]).
_WHITE_SAT_MAX, _WHITE_VAL_MIN = 70, 205


def frames(session_dir: str):
    out = []
    for p in glob.glob(os.path.join(session_dir, "f*_raw.png")):
        m = _FRAME_RE.search(os.path.basename(p))
        if m:
            out.append((int(m.group(1)), p))
    return [p for _, p in sorted(out)]


def analyse(img, roi):
    """Return per-frame geometry of the green and white runs in the ROI."""
    x0, y0, x1, y1 = roi
    sub = img[y0:y1, x0:x1]
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)

    green = cv2.inRange(hsv, np.array(_G_LO, np.uint8), np.array(_G_HI, np.uint8))
    neon = cv2.inRange(hsv, np.array(_NEON_LO, np.uint8), np.array(_NEON_HI, np.uint8))
    white = cv2.inRange(hsv,
                        np.array((0, 0, _WHITE_VAL_MIN), np.uint8),
                        np.array((179, _WHITE_SAT_MAX, 255), np.uint8))

    def run(mask, frac=0.30):
        rows = np.flatnonzero(mask.mean(axis=1) / 255.0 >= frac)
        if rows.size == 0:
            return None
        return int(rows[0]), int(rows[-1]), int(rows.size)

    return {
        "green": run(green),
        "neon": run(neon),
        "white": run(white),
        "green_px": int(green.sum() // 255),
        "white_px": int(white.sum() // 255),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir")
    ap.add_argument("--roi", nargs=4, type=int, metavar=("X0", "Y0", "X1", "Y1"),
                    help="meter ROI; default = auto from the largest green blob")
    ap.add_argument("--limit", type=int, default=400)
    args = ap.parse_args()

    paths = frames(args.session_dir)
    if not paths:
        print(f"no f*_raw.png frames in {args.session_dir}")
        return 1
    print(f"{len(paths)} frames in {args.session_dir}")

    roi = tuple(args.roi) if args.roi else None
    if roi is None:
        # Auto-ROI: biggest tall green blob across a sample of frames.
        best = None
        for p in paths[: min(len(paths), 60)]:
            img = cv2.imread(p)
            if img is None:
                continue
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            g = cv2.inRange(hsv, np.array(_G_LO, np.uint8), np.array(_G_HI, np.uint8))
            n, _, stats, _ = cv2.connectedComponentsWithStats(g, 8)
            for i in range(1, n):
                x, y, w, h, a = stats[i]
                if h >= 2 * max(w, 1) and a >= 40 and (best is None or a > best[4]):
                    best = (x, y, w, h, a)
        if best is None:
            print("no tall green blob found - pass --roi explicitly")
            return 2
        x, y, w, h, _ = best
        pad_x, pad_y = max(12, w), max(40, 2 * h)
        roi = (max(0, x - pad_x), max(0, y - pad_y), x + w + pad_x, y + h + pad_y)
        print(f"auto ROI = {roi} (from green blob {best[:4]})")

    print("\n frame | green rows      | white rows      | g_px  w_px")
    print("-------+-----------------+-----------------+-----------")
    g_tops, w_tops, g_h, w_h = [], [], [], []
    for p in paths[: args.limit]:
        img = cv2.imread(p)
        if img is None:
            continue
        r = analyse(img, roi)
        g, w = r["green"], r["white"]
        gs = f"y{g[0]:3d}..{g[1]:3d} n={g[2]:3d}" if g else "      --       "
        ws = f"y{w[0]:3d}..{w[1]:3d} n={w[2]:3d}" if w else "      --       "
        print(f" {os.path.basename(p)[1:6]} | {gs} | {ws} | {r['green_px']:5d} {r['white_px']:5d}")
        if g:
            g_tops.append(g[0]); g_h.append(g[2])
        if w:
            w_tops.append(w[0]); w_h.append(w[2])

    print("\n=== WHICH REGION IS THE FILL? (the one that MOVES) ===")
    for name, tops, hs in (("green", g_tops, g_h), ("white", w_tops, w_h)):
        if not tops:
            print(f"  {name:5s}: never seen")
            continue
        print(f"  {name:5s}: top edge range {min(tops)}..{max(tops)} "
              f"(spread {max(tops)-min(tops)}px), height {min(hs)}..{max(hs)} "
              f"(spread {max(hs)-min(hs)}px)")
    print("  -> the region with the LARGE height/top spread is the rising fill;")
    print("     the region that holds still is the fixed make-window.")

    print("\n=== CAP MASK SURVIVAL (must be non-zero on gameplay frames) ===")
    print(f"  frames with a green cap run: {len(g_tops)}/{min(len(paths), args.limit)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
