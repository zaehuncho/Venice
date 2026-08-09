#!/usr/bin/env python3
"""Appearance-CORRECTED synthetic dataset for the sub-pixel METER READER (sim-to-real fix v2).

The original synth_meter_reader_data.py paints a LIGHT-grey FILLED track; validation against
real framedump crops showed the resulting model OVER-READS real fill by ~+20-25% because the
REAL meter has a DARK/near-black empty track above the red fill, inside a thick light-grey
BEVELED frame (see logs/diagnostics/reader_sim2real). This generator renders that corrected
appearance so the synthetic half generalises to real across the FULL fill range (real located
crops barely cover fill<40%, so we cannot rely on real data alone there).

Changes vs the original render:
  * empty track interior  : DARK (~near-black) instead of light grey (150).
  * frame                 : 2-3px light-grey bevel (was 1px dark border).
  * bar proportions       : wider bar / taller track to match the real Arrow2 meter.
  * green window          : thin (2-10% of track) to match the real neon sliver (was up to 22%,
                            which made high fills visually ambiguous and inflated high-fill MAE).

Labels + npz schema are IDENTICAL to synth_meter_reader_data.py
(labels=[fill_frac, green_top_frac, green_bottom_frac]; fill_frac is framing-invariant bar
travel) so the same trainer consumes it. Composited onto real framedump backgrounds with the
same mild capture augmentation.

USAGE:
  py tools/diagnostics/gen_meter_reader_synth_v2.py --n 16000 --out datasets/meter_reader_synth_v2 --preview
"""
from __future__ import annotations

import argparse
import os
import random
import sys

import cv2
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from synth_meter_gen import composite  # noqa: E402
from synth_meter_reader_data import _load_bgs, _bg_crop, _augment  # noqa: E402

RED_BGR = (28, 18, 222)
GREEN_BGR = (40, 245, 60)
DARK_BGR = (26, 24, 24)      # real empty track: dark / near-black
FRAME_BGR = (188, 188, 190)  # light-grey beveled frame
LEG_BGR = (150, 150, 150)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def render_meter_v2(crop_h, crop_w, rng, fill_bgr=RED_BGR):
    """Render one appearance-corrected meter into a crop_h x crop_w BGR layer + alpha.
    Returns (layer, alpha, labels) with the SAME label semantics as the original render_meter."""
    layer = np.zeros((crop_h, crop_w, 3), np.uint8)
    alpha = np.zeros((crop_h, crop_w), np.float32)

    # geometry: wider bar + taller track than the original, to match the real Arrow2 meter
    tw = rng.randint(int(crop_w * 0.30), max(int(crop_w * 0.30) + 1, int(crop_w * 0.50)))
    th = rng.randint(int(crop_h * 0.55), int(crop_h * 0.85))
    tx = rng.randint(2, max(3, crop_w - tw - 2))
    ty = rng.randint(2, max(3, crop_h - th - 8))
    bottom = ty + th                                   # notch row (fill bottom anchor)

    green_frac = rng.uniform(0.02, 0.10)               # thin neon sliver (real windows are thin)
    green_h = max(2, int(round(th * green_frac)))
    green_top = ty
    green_bottom = ty + green_h

    fill_pct = rng.uniform(0.0, 1.06)
    fill_h = int(round(_clamp(fill_pct, 0.0, 1.0) * th))
    fill_top = bottom - fill_h

    def jit(c, k=16):
        return tuple(int(_clamp(v + rng.randint(-k, k), 0, 255)) for v in c)

    red = jit(fill_bgr, 16)
    green = jit(GREEN_BGR, 22)
    dark = jit(DARK_BGR, 10)
    frame = jit(FRAME_BGR, 22)

    fw = rng.choice([2, 2, 3])                          # frame thickness

    # 1) DARK empty track interior + light-grey beveled frame
    cv2.rectangle(layer, (tx, ty), (tx + tw, bottom), dark, -1)
    cv2.rectangle(layer, (tx, ty), (tx + tw, bottom), frame, fw)
    alpha[max(0, ty - fw):bottom + fw, max(0, tx - fw):tx + tw + fw] = rng.uniform(0.85, 0.98)

    # 2) thin neon green make-window at the very top
    cv2.rectangle(layer, (tx + fw, green_top), (tx + tw - fw, green_bottom), green, -1)

    # 3) red fill from the notch up to fill_top (never over the green cap)
    rf_top = max(fill_top, green_bottom)
    if rf_top < bottom:
        cv2.rectangle(layer, (tx + fw, rf_top), (tx + tw - fw, bottom), red, -1)

    # 4) chevron notch at the bottom (V into the dark bar) + grey legs beneath
    cxm = tx + tw // 2
    vh = max(3, tw // 3)
    notch = np.array([[tx + fw, bottom], [cxm, bottom - vh], [tx + tw - fw, bottom]], np.int32)
    cv2.fillPoly(layer, [notch], dark)                  # notch cut is dark (matches interior)
    cv2.polylines(layer, [notch], False, frame, 1)
    leg_h = rng.randint(4, 12)
    cv2.line(layer, (tx + fw, bottom), (cxm, bottom + leg_h), LEG_BGR, 1)
    cv2.line(layer, (tx + tw - fw, bottom), (cxm, bottom + leg_h), LEG_BGR, 1)
    alpha[bottom:_clamp(bottom + leg_h + 1, 0, crop_h), tx:tx + tw] = np.maximum(
        alpha[bottom:_clamp(bottom + leg_h + 1, 0, crop_h), tx:tx + tw], 0.5)

    # 5) soften the widget a touch (real meter is anti-aliased over the bg)
    k = rng.choice([1, 3])
    if k > 1:
        layer = cv2.GaussianBlur(layer, (k, k), 0)
        alpha = cv2.GaussianBlur(alpha, (k, k), 0)

    labels = dict(
        fill_pct=round(float(_clamp(fill_pct, 0.0, 1.0)) * 100.0, 2),
        green_top_row=green_top / crop_h, green_bottom_row=green_bottom / crop_h,
    )
    return layer, alpha, labels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=16000)
    ap.add_argument("--out", default="datasets/meter_reader_synth_v2")
    ap.add_argument("--bg-glob", default="logs/diagnostics/framedump/f*_raw.png")
    ap.add_argument("--img-h", type=int, default=96)
    ap.add_argument("--img-w", type=int, default=32)
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--preview", action="store_true")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    bgs = _load_bgs(args.bg_glob)
    if not bgs:
        print(f"[warn] no backgrounds matched {args.bg_glob}; flat/noise backgrounds")
    os.makedirs(args.out, exist_ok=True)

    imgs = np.empty((args.n, args.img_h, args.img_w, 3), np.uint8)
    labels = np.empty((args.n, 3), np.float32)
    split = np.zeros((args.n,), np.uint8)
    preview = []

    for i in range(args.n):
        crop_h = rng.randint(80, 240)                   # wider size range (loc_mem coast crops are large)
        crop_w = rng.randint(40, 150)
        layer, alpha, lab = render_meter_v2(crop_h, crop_w, rng)
        bg = _bg_crop(bgs, crop_h, crop_w, rng)
        img = composite(bg, layer, alpha)
        img, gt_frac, gb_frac = _augment(
            img, lab["green_top_row"], lab["green_bottom_row"], crop_h, crop_w, rng)
        img = cv2.resize(img, (args.img_w, args.img_h), interpolation=cv2.INTER_AREA)
        imgs[i] = img
        labels[i] = (float(np.clip(lab["fill_pct"] / 100.0, 0.0, 1.0)),
                     float(np.clip(gt_frac, 0.0, 1.0)), float(np.clip(gb_frac, 0.0, 1.0)))
        if args.preview and len(preview) < 36:
            vis = cv2.resize(img, (args.img_w * 3, args.img_h * 3), interpolation=cv2.INTER_NEAREST)
            H3 = args.img_h * 3
            cv2.line(vis, (0, int(labels[i][1] * H3)), (vis.shape[1], int(labels[i][1] * H3)), (0, 255, 0), 1)
            preview.append(vis)

    n_val = int(round(args.n * args.val_frac))
    idx = list(range(args.n)); rng.shuffle(idx)
    for j in idx[:n_val]:
        split[j] = 1

    out_npz = os.path.join(args.out, "meter_reader_data.npz")
    np.savez_compressed(out_npz, images=imgs, labels=labels, split=split, img_h=args.img_h, img_w=args.img_w)
    fill = labels[:, 0]
    assert bool((labels[:, 1] <= labels[:, 2]).all()), "green_top must be <= green_bottom"
    print(f"wrote {out_npz}: N={args.n} train={args.n - n_val} val={n_val} store={args.img_h}x{args.img_w}")
    print(f"  fill_frac min={fill.min():.3f} max={fill.max():.3f} mean={fill.mean():.3f}")
    if args.preview and preview:
        cols = 6
        rows = [np.hstack(preview[r:r + cols]) for r in range(0, len(preview) - len(preview) % cols, cols)]
        if rows:
            cv2.imwrite(os.path.join(args.out, "_preview.png"), np.vstack(rows))


if __name__ == "__main__":
    main()
