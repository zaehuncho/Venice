#!/usr/bin/env python
"""Extract REAL meter-reader training crops from the framedump sessions (task step 2b).

The synthetic renderer paints a LIGHT-grey filled track; the REAL meter has a DARK/black
empty track above the red fill inside a thick light-grey beveled frame. A reader trained on
synthetic-only therefore OVER-READS fill on real crops (measured ~+20-25% bias). This script
harvests the EXACT located crops the serving path builds (via the same spy used by
validate_meter_reader_real) and labels each with the classical ROW-COUNTED fill from
_measure_track -- which is geometrically faithful per-crop (verified against pixels), just
quantized. Used to fine-tune the synthetic model onto the real appearance.

Only the FILL label is trusted on real crops (green bounds are frequently not-found on the
real track); the npz carries a per-sample `fill_only` mask so the combined trainer supervises
green from synthetic only.

OUTPUT (npz): images[N,96,32,3] u8, labels[N,3] f32 (green cols are placeholders),
              fill_only[N] u8 (=1), split[N] u8, img_h, img_w.

USAGE:
  py tools/diagnostics/extract_meter_reader_real_data.py --out datasets/meter_reader_real
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import cv2

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(_HERE))
for _p in (REPO, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import validate_meter_reader_real as V  # noqa: E402


def _despike(idxs, fills, thresh=15.0):
    """Drop frames whose row-count fill differs from BOTH temporal neighbours by >thresh
    (contour-split / specular single-frame spikes). Keeps label<->crop alignment (no smoothing
    of the value that survives)."""
    keep = np.ones(len(fills), bool)
    for k in range(1, len(fills) - 1):
        a, b, c = fills[k - 1], fills[k], fills[k + 1]
        if abs(b - a) > thresh and abs(b - c) > thresh and abs(a - c) < thresh:
            keep[k] = False
    return keep


def harvest(session_dir, img_h, img_w):
    det, info, rows, crops = V.collect(session_dir, 0, -1, 1)
    # group contiguous detected+located runs to despike per run
    runs = []
    cur = []
    for r in rows:
        if r["detected"] and r["has_crop"] and r["track_fill"] is not None and r["idx"] in crops:
            cur.append(r["idx"])
        else:
            if cur:
                runs.append(cur); cur = []
    if cur:
        runs.append(cur)
    imgs, fills = [], []
    kept = 0
    for run in runs:
        f = np.array([next(rr["track_fill"] for rr in rows if rr["idx"] == i) for i in run], float)
        keep = _despike(run, f)
        for i, k in zip(run, keep):
            if not k:
                continue
            c = crops[i]
            if c is None or c.size == 0:
                continue
            im = cv2.resize(c, (img_w, img_h), interpolation=cv2.INTER_AREA)
            fv = float(np.clip(next(rr["track_fill"] for rr in rows if rr["idx"] == i) / 100.0, 0.0, 1.0))
            imgs.append(im); fills.append(fv); kept += 1
    print(f"  {os.path.basename(session_dir)}: located_crops={len(crops)} kept={kept}")
    return imgs, fills


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", nargs="*", default=[
        os.path.join(REPO, "logs", "diagnostics", "framedump", "session_20260704_210801"),
        os.path.join(REPO, "logs", "diagnostics", "framedump", "session_20260704_162915"),
    ])
    ap.add_argument("--out", default=os.path.join(REPO, "datasets", "meter_reader_real"))
    ap.add_argument("--img-h", type=int, default=96)
    ap.add_argument("--img-w", type=int, default=32)
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    all_imgs, all_fills = [], []
    for s in args.sessions:
        if not os.path.isdir(s):
            print(f"  (skip missing {s})")
            continue
        imgs, fills = harvest(s, args.img_h, args.img_w)
        all_imgs += imgs; all_fills += fills

    n = len(all_imgs)
    if n == 0:
        raise SystemExit("no real crops harvested")
    images = np.stack(all_imgs).astype(np.uint8)
    fills = np.asarray(all_fills, np.float32)
    labels = np.zeros((n, 3), np.float32)
    labels[:, 0] = fills                      # fill_frac (trusted); green cols are placeholders
    fill_only = np.ones((n,), np.uint8)
    rng = np.random.default_rng(args.seed)
    split = (rng.random(n) < args.val_frac).astype(np.uint8)

    os.makedirs(args.out, exist_ok=True)
    out = os.path.join(args.out, "meter_reader_real.npz")
    np.savez_compressed(out, images=images, labels=labels, fill_only=fill_only, split=split,
                        img_h=args.img_h, img_w=args.img_w)
    print(f"wrote {out}: N={n} train={(split==0).sum()} val={(split==1).sum()}")
    print(f"  fill  min={fills.min()*100:.1f}% max={fills.max()*100:.1f}% mean={fills.mean()*100:.1f}%")
    h, _ = np.histogram(fills * 100, bins=[0, 20, 40, 60, 80, 95, 101])
    print(f"  fill hist [0-20,20-40,40-60,60-80,80-95,95-101]%: {h.tolist()}")


if __name__ == "__main__":
    main()
