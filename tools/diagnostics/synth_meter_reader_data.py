#!/usr/bin/env python3
"""Synthetic dataset for the SUB-PIXEL METER READER (models/meter_reader.pt) — the read-jitter fix.

WHY: the classical reader COUNTS red rows, so fill% is quantized to ~1-2% at 720p (a ~1-2px meter). That
staircase noise flows straight into velocity -> tip prediction -> release jitter, and the contested "fade"
green sliver (~2-3% of the bar) is below the row-counting floor. A tiny CNN that regresses CONTINUOUS
sub-pixel fill + the true green-window bounds from the located crop removes that quantization and gives
window-relative targeting.

Training data is FREE and PERFECTLY labeled: reuse tools/diagnostics/synth_meter_gen.py.render_meter, which
renders a meter into a crop and returns rows (as fractions of crop height) for fill_pct / green_top / green_bottom.
We render at VARIED crop sizes/scales (sim the locator handing a loose or tight box), composite onto real
framedump backgrounds, add mild capture-style augmentation (blur / brightness / noise / a small pad), then
resize every crop to a fixed store size. Labels are geometry FRACTIONS so they survive the resize.

The meter is a TALL vertical bar, so the store/model input is taller than wide (default 96x32 HxW) — vertical
resolution is exactly what sub-pixel fill needs (this differs from a square-ish input on purpose).

OUTPUT (npz, --out dir):
  images  uint8  [N, H, W, 3]  BGR, store size
  labels  float32[N, 3]        = [fill_frac(0..1), green_top_frac(0..1), green_bottom_frac(0..1)]
  split   uint8  [N]           0=train, 1=val

USAGE:
  python tools/diagnostics/synth_meter_reader_data.py --n 8000 --out datasets/meter_reader_synth
  # small smoke:  python tools/diagnostics/synth_meter_reader_data.py --n 400 --out datasets/meter_reader_synth
"""
from __future__ import annotations

import argparse
import glob
import os
import random
import sys

import cv2
import numpy as np

# render_meter / composite live next to this file
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from synth_meter_gen import composite, render_meter  # noqa: E402


def _load_bgs(bg_glob):
    return glob.glob(bg_glob) if bg_glob else []


def _bg_crop(bgs, h, w, rng):
    """A random ROI-sized crop from a real framedump; falls back to flat/noise if none load."""
    for _ in range(6):
        if not bgs:
            break
        f = rng.choice(bgs)
        img = cv2.imread(f)
        if img is None:
            continue
        H, W = img.shape[:2]
        if H <= h or W <= w:
            img = cv2.resize(img, (max(w + 1, W), max(h + 1, H)))
            H, W = img.shape[:2]
        y = rng.randint(0, H - h)
        x = rng.randint(0, W - w)
        return img[y:y + h, x:x + w].copy()
    base = np.full((h, w, 3), rng.randint(30, 130), np.uint8)
    noise = np.random.default_rng(rng.randint(0, 1 << 30)).integers(-20, 20, base.shape, dtype=np.int16)
    return np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def _augment(img, green_top, green_bottom, crop_h, crop_w, rng):
    """Mild capture-style augmentation. Photometric aug leaves geometry alone; a small vertical pad shifts
    the crop framing, so the green ROW fractions are recomputed (fill_frac is a bar-travel fraction and is
    framing-invariant, so it is NOT touched here). Returns (img, green_top_frac, green_bottom_frac)."""
    # 1) blur (soft capture / anti-alias)
    if rng.random() < 0.5:
        k = rng.choice([3, 5])
        img = cv2.GaussianBlur(img, (k, k), 0)
    # 2) brightness / contrast (court lighting)
    if rng.random() < 0.6:
        alpha = rng.uniform(0.75, 1.25)
        beta = rng.uniform(-25.0, 25.0)
        img = np.clip(img.astype(np.float32) * alpha + beta, 0, 255).astype(np.uint8)
    # 3) sensor noise
    if rng.random() < 0.4:
        noise = np.random.default_rng(rng.randint(0, 1 << 30)).normal(0, rng.uniform(2.0, 8.0), img.shape)
        img = np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    # 4) small pad (loose/tight locator box) -> recompute vertical fractions
    gt_frac, gb_frac = green_top, green_bottom
    if rng.random() < 0.5:
        pt = rng.randint(0, max(1, int(crop_h * 0.15)))
        pb = rng.randint(0, max(1, int(crop_h * 0.15)))
        pl = rng.randint(0, max(1, int(crop_w * 0.15)))
        prr = rng.randint(0, max(1, int(crop_w * 0.15)))
        img = cv2.copyMakeBorder(img, pt, pb, pl, prr, cv2.BORDER_REPLICATE)
        new_h = crop_h + pt + pb
        gt_frac = (pt + green_top * crop_h) / new_h
        gb_frac = (pt + green_bottom * crop_h) / new_h
    return img, float(gt_frac), float(gb_frac)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8000)
    ap.add_argument("--out", default="datasets/meter_reader_synth")
    ap.add_argument("--bg-glob", default="logs/diagnostics/framedump/f*_raw.png")
    ap.add_argument("--img-h", type=int, default=96, help="store/model input height (tall vertical bar)")
    ap.add_argument("--img-w", type=int, default=32, help="store/model input width")
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--preview", action="store_true", help="also write OUT/_preview.png (6x6 contact sheet)")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    bgs = _load_bgs(args.bg_glob)
    if not bgs:
        print(f"[warn] no backgrounds matched {args.bg_glob}; using flat/noise backgrounds")
    os.makedirs(args.out, exist_ok=True)

    imgs = np.empty((args.n, args.img_h, args.img_w, 3), np.uint8)
    labels = np.empty((args.n, 3), np.float32)
    split = np.zeros((args.n,), np.uint8)
    preview = []

    for i in range(args.n):
        # varied scale: the meter is a tall thin bar; sample a range of crop framings
        crop_h = rng.randint(80, 200)
        crop_w = rng.randint(40, 110)
        layer, alpha, lab = render_meter(crop_h, crop_w, rng)
        bg = _bg_crop(bgs, crop_h, crop_w, rng)
        img = composite(bg, layer, alpha)

        img, gt_frac, gb_frac = _augment(
            img, lab["green_top_row"], lab["green_bottom_row"], crop_h, crop_w, rng)

        img = cv2.resize(img, (args.img_w, args.img_h), interpolation=cv2.INTER_AREA)
        imgs[i] = img
        # target 1 = bar-travel fill fraction (framing-invariant); 2/3 = green window row fractions
        labels[i] = (
            float(np.clip(lab["fill_pct"] / 100.0, 0.0, 1.0)),
            float(np.clip(gt_frac, 0.0, 1.0)),
            float(np.clip(gb_frac, 0.0, 1.0)),
        )
        if args.preview and len(preview) < 36:
            vis = cv2.resize(img, (args.img_w * 3, args.img_h * 3), interpolation=cv2.INTER_NEAREST)
            H3 = args.img_h * 3
            cv2.line(vis, (0, int(labels[i][1] * H3)), (vis.shape[1], int(labels[i][1] * H3)), (0, 255, 0), 1)
            cv2.line(vis, (0, int(labels[i][2] * H3)), (vis.shape[1], int(labels[i][2] * H3)), (0, 200, 0), 1)
            preview.append(vis)

    # held-out val split
    n_val = int(round(args.n * args.val_frac))
    idx = list(range(args.n))
    rng.shuffle(idx)
    for j in idx[:n_val]:
        split[j] = 1

    out_npz = os.path.join(args.out, "meter_reader_data.npz")
    np.savez_compressed(out_npz, images=imgs, labels=labels, split=split,
                        img_h=args.img_h, img_w=args.img_w)

    # sanity summary (also used by the verification step)
    fill = labels[:, 0]
    gtop = labels[:, 1]
    gbot = labels[:, 2]
    assert float(fill.min()) >= 0.0 and float(fill.max()) <= 1.0, "fill_frac out of [0,1]"
    assert bool((gtop <= gbot).all()), "green_top must be <= green_bottom for every sample"
    print(f"wrote {out_npz}: N={args.n} train={args.n - n_val} val={n_val} store={args.img_h}x{args.img_w}")
    print(f"  fill_frac  min={fill.min():.3f} max={fill.max():.3f} mean={fill.mean():.3f}")
    print(f"  green_top  min={gtop.min():.3f} max={gtop.max():.3f}")
    print(f"  green_bot  min={gbot.min():.3f} max={gbot.max():.3f}  (green_top<=green_bot: OK)")

    if args.preview and preview:
        cols = 6
        rows = [np.hstack(preview[r:r + cols]) for r in range(0, len(preview) - len(preview) % cols, cols)]
        if rows:
            cv2.imwrite(os.path.join(args.out, "_preview.png"), np.vstack(rows))


if __name__ == "__main__":
    main()
