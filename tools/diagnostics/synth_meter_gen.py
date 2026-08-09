#!/usr/bin/env python3
"""Synthetic NBA 2K park shot-meter generator -> labeled ROI dataset for the multi-head ROI reader (model #2).

WHY: a from-scratch detector overfit on 88 single-session frames (mAP 0.135) — DATA is the bottleneck, not the
method. The shot meter is a near-deterministic widget (grey track + neon-green cap + red fill rising from a
chevron notch), so we can RENDER it at every fill / green-window (contest) / scale (zoom) / color / position and
composite onto REAL backgrounds, getting thousands of perfectly-labeled crops with zero capture grind. This is
the broad-coverage half (esp. the CONTESTED green sliver the classical green-scan struggles with); fine-tune on
real `meter_autolabel.py` crops for the sim-to-real gap.

OUTPUT: fixed-size ROI crops (the region the classical locator hands the reader) + a labels.jsonl, where each
line = {file, present, notch_row, fill_top_row, green_top_row, green_bottom_row, fill_pct, bbox} with rows
normalized to the crop height (0..1). Negatives (present=0) are background/distractor crops for the present head.

Appearance is matched to a real 720p crop (realmeter_*.png): track ~20px wide / ~130px tall, green cap ~top
15-22%, pure-red fill (Hue~0/179 Sat~255), neon green (~#31FF1F), a small grey unfilled gap, a chevron notch.

USAGE:
  python tools/diagnostics/synth_meter_gen.py --n 4000 --out datasets/meter_roi_synth \
         --bg-glob "logs/diagnostics/framedump/f*_raw.png" [--neg-frac 0.25] [--preview]
"""
import argparse, glob, json, os, random
import cv2
import numpy as np

# --- appearance constants (720p reference; matched to realmeter_*.png) ----------------------------------------
RED_BGR        = (28, 18, 222)     # bright pure red (BGR); Hue~0/179, Sat~255 in HSV
MAGENTA_BGR    = (170, 20, 235)    # purple/magenta meter fill (Hue~159; measured H148-170 med163 from a live
                                   # purple batch) -- the meter colour varies by the game setting, so the
                                   # locator trains on BOTH to learn shape not hue [[meter-color-diversity]]
GREEN_BGR      = (40, 245, 60)     # neon make-window green (~#31FF1F)
TRACK_BGR      = (150, 150, 150)   # unfilled track grey
BORDER_BGR     = (90, 90, 90)      # track outline
NOTCH_LEG_BGR  = (170, 170, 170)   # the grey chevron legs under the bar


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def render_meter(crop_h, crop_w, rng, fill_bgr=RED_BGR):
    """Render one meter into a crop_h x crop_w BGR layer + alpha mask. Returns (layer, alpha, labels).
    fill_bgr picks the fill colour (RED_BGR or MAGENTA_BGR) so the synthetic set covers both meter colours."""
    layer = np.zeros((crop_h, crop_w, 3), np.uint8)
    alpha = np.zeros((crop_h, crop_w), np.float32)

    # --- geometry (scale = zoom) ---
    tw = rng.randint(max(8, crop_w // 6), max(12, crop_w // 3))          # track width
    th = rng.randint(int(crop_h * 0.42), int(crop_h * 0.82))             # track height (notch->cap top)
    tx = rng.randint(2, max(3, crop_w - tw - 2))                         # left
    ty = rng.randint(2, max(3, crop_h - th - 8))                         # top (cap top)
    bottom = ty + th                                                     # the notch row

    # --- green make-window (contest variation): a thin sliver (contested) to a fat band (open) at the cap ---
    green_frac = rng.uniform(0.02, 0.22)                                 # 2% (contested) .. 22% (wide-open)
    green_h = max(2, int(round(th * green_frac)))
    green_top = ty
    green_bottom = ty + green_h

    # --- red fill: rises from the notch up to fill_top; fill_pct over the FULL track (notch->cap top) ---
    fill_pct = rng.uniform(0.0, 1.06)                                    # 0..>100 (a touch of overshoot)
    fill_h = int(round(_clamp(fill_pct, 0.0, 1.0) * th))
    fill_top = bottom - fill_h

    # color jitter (per-sample lighting / court)
    def jit(c, k=18):
        return tuple(int(_clamp(v + rng.randint(-k, k), 0, 255)) for v in c)
    red, green, track, border = jit(fill_bgr), jit(GREEN_BGR, 22), jit(TRACK_BGR, 30), jit(BORDER_BGR, 25)

    # 1) track background (unfilled grey) + border
    cv2.rectangle(layer, (tx, ty), (tx + tw, bottom), track, -1)
    cv2.rectangle(layer, (tx, ty), (tx + tw, bottom), border, 1)
    alpha[ty:bottom, tx:tx + tw] = rng.uniform(0.82, 0.96)              # the widget is slightly translucent

    # 2) green make-window cap at the very top
    cv2.rectangle(layer, (tx + 1, green_top), (tx + tw - 1, green_bottom), green, -1)

    # 3) red fill from the notch up to fill_top (clipped to below the green cap)
    rf_top = max(fill_top, green_bottom)                                # red never paints over the green cap
    if rf_top < bottom:
        cv2.rectangle(layer, (tx + 1, rf_top), (tx + tw - 1, bottom), red, -1)

    # 4) chevron notch at the bottom (the V the locator keys on) + grey legs under it
    cxm = tx + tw // 2
    notch = np.array([[tx + 1, bottom], [cxm, bottom - max(3, tw // 3)], [tx + tw - 1, bottom]], np.int32)
    cv2.fillPoly(layer, [notch], track)
    leg_h = rng.randint(4, 12)
    cv2.line(layer, (tx + 1, bottom), (cxm, bottom + leg_h), NOTCH_LEG_BGR, 1)
    cv2.line(layer, (tx + tw - 1, bottom), (cxm, bottom + leg_h), NOTCH_LEG_BGR, 1)
    alpha[bottom:_clamp(bottom + leg_h + 1, 0, crop_h), tx:tx + tw] = np.maximum(
        alpha[bottom:_clamp(bottom + leg_h + 1, 0, crop_h), tx:tx + tw], 0.5)

    # 5) anti-alias the whole widget a touch (the real meter is soft over the bg)
    k = rng.choice([1, 3])
    if k > 1:
        layer = cv2.GaussianBlur(layer, (k, k), 0)
        alpha = cv2.GaussianBlur(alpha, (k, k), 0)

    labels = dict(present=1,
                  notch_row=bottom / crop_h, fill_top_row=fill_top / crop_h,
                  green_top_row=green_top / crop_h, green_bottom_row=green_bottom / crop_h,
                  fill_pct=round(float(_clamp(fill_pct, 0.0, 1.0)) * 100.0, 2),
                  bbox=[tx / crop_w, ty / crop_h, (tx + tw) / crop_w, bottom / crop_h])
    return layer, alpha, labels


def composite(bg, layer, alpha):
    a = alpha[..., None]
    return (bg.astype(np.float32) * (1 - a) + layer.astype(np.float32) * a).astype(np.uint8)


def bg_crop(bgs, h, w, rng):
    """A random ROI-sized crop from a real framedump background (the meter's real visual context)."""
    for _ in range(8):
        f = rng.choice(bgs)
        img = cv2.imread(f)
        if img is None:
            continue
        H, W = img.shape[:2]
        if H <= h or W <= w:
            img = cv2.resize(img, (max(w + 1, W), max(h + 1, H)))
            H, W = img.shape[:2]
        y = rng.randint(0, H - h); x = rng.randint(0, W - w)
        return img[y:y + h, x:x + w].copy()
    return np.full((h, w, 3), rng.randint(40, 120), np.uint8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=4000)
    ap.add_argument("--out", default="datasets/meter_roi_synth")
    ap.add_argument("--bg-glob", default="logs/diagnostics/framedump/f*_raw.png")
    ap.add_argument("--crop-h", type=int, default=160)
    ap.add_argument("--crop-w", type=int, default=64)
    ap.add_argument("--neg-frac", type=float, default=0.25)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--preview", action="store_true", help="also write a 6x6 contact sheet to OUT/_preview.png")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    bgs = glob.glob(args.bg_glob)
    if not bgs:
        raise SystemExit(f"no backgrounds matched {args.bg_glob}")
    os.makedirs(args.out, exist_ok=True)
    img_dir = os.path.join(args.out, "images"); os.makedirs(img_dir, exist_ok=True)

    preview = []
    with open(os.path.join(args.out, "labels.jsonl"), "w", encoding="utf-8") as lf:
        for i in range(args.n):
            bg = bg_crop(bgs, args.crop_h, args.crop_w, rng)
            if rng.random() < args.neg_frac:                            # NEGATIVE: background / distractor only
                img = bg
                lab = dict(present=0, notch_row=-1, fill_top_row=-1, green_top_row=-1,
                           green_bottom_row=-1, fill_pct=-1, bbox=[-1, -1, -1, -1])
            else:                                                       # POSITIVE: composite a meter
                layer, a, lab = render_meter(args.crop_h, args.crop_w, rng)
                img = composite(bg, layer, a)
            name = f"s{i:06d}.png"
            cv2.imwrite(os.path.join(img_dir, name), img)
            lab["file"] = f"images/{name}"
            lf.write(json.dumps(lab) + "\n")
            if args.preview and len(preview) < 36:
                vis = img.copy()
                if lab["present"]:
                    bb = lab["bbox"]; H, W = vis.shape[:2]
                    cv2.rectangle(vis, (int(bb[0] * W), int(bb[1] * H)), (int(bb[2] * W), int(bb[3] * H)), (255, 0, 255), 1)
                    cv2.line(vis, (0, int(lab["green_bottom_row"] * H)), (W, int(lab["green_bottom_row"] * H)), (0, 255, 0), 1)
                    cv2.line(vis, (0, int(lab["fill_top_row"] * H)), (W, int(lab["fill_top_row"] * H)), (0, 255, 255), 1)
                preview.append(cv2.resize(vis, (args.crop_w * 2, args.crop_h * 2), interpolation=cv2.INTER_NEAREST))
    if args.preview and preview:
        cols = 6
        rows = [np.hstack(preview[r:r + cols]) for r in range(0, len(preview) - len(preview) % cols, cols)]
        if rows:
            cv2.imwrite(os.path.join(args.out, "_preview.png"), np.vstack(rows))
    print(f"wrote {args.n} samples to {args.out} (neg_frac={args.neg_frac}); labels.jsonl + images/")


if __name__ == "__main__":
    main()
