#!/usr/bin/env python
"""Auto-label the NBA 2K27 white shot meter in framedump frames -> YOLO dataset.

PRECISION OVER RECALL, deliberately. A wrong box teaches the detector to find the
wrong thing, and the whole point of training is to beat the colour heuristics that
already confuse a white hoodie for the meter. So every ambiguous frame is SKIPPED:
the labeller demands exactly ONE white bar-shaped blob that carries a real green
apex above it, and abstains whenever there are none or several.

Box = the FULL meter: the green apex down to the fill floor, widened to the outline
(the reader needs the whole track, not just the lit fill).
"""
from __future__ import annotations
import glob, os, sys, json, random
import cv2, numpy as np

G_LO, G_HI = np.array((38, 90, 90), np.uint8), np.array((85, 255, 255), np.uint8)
W_LO, W_HI = np.array((0, 0, 235), np.uint8), np.array((179, 30, 255), np.uint8)


def is_gameplay(img, hsv):
    """Reject menus, loading screens and replays before looking for a meter at all.

    The first labelling pass happily found "a white blob with something green above
    it" in the pause menu and on loading art, and those boxes would have taught the
    detector to fire on UI text. A live court fills the lower frame with large,
    strongly saturated colour (blue/red paint); menus and loading screens do not.
    """
    h, w = hsv.shape[:2]
    low = hsv[int(h * 0.45):, :]
    sat = (low[:, :, 1] >= 90) & (low[:, :, 2] >= 60)
    return float(sat.mean()) >= 0.25


def label(img):
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    if not is_gameplay(img, hsv):
        return None
    n, _, st, _ = cv2.connectedComponentsWithStats(cv2.inRange(hsv, W_LO, W_HI), 8)
    cands = []
    H, W = img.shape[:2]
    for k in range(1, n):
        x, y, w, h, a = st[k]
        if not (5 <= w <= 40 and h >= 30 and a >= 150):
            continue
        if h < 3.0 * w:                       # the meter is a NARROW tall ribbon
            continue
        if a / float(max(w * h, 1)) < 0.55:   # solid, not a wispy glyph
            continue
        if y < 0.08 * H or (y + h) > 0.95 * H:   # not the banner strip, not the floor edge
            continue
        up = max(16, int(3.0 * w))
        band = hsv[max(0, y - up):y + 3, max(0, x - 8):x + w + 8]
        if band.size == 0:
            continue
        g = cv2.inRange(band, G_LO, G_HI)
        if int(g.sum() // 255) < 12:           # needs a real green apex
            continue
        rows = np.flatnonzero(g.mean(axis=1) / 255.0 >= 0.10)
        if rows.size == 0:
            continue
        # the apex must sit OVER the bar, not merely somewhere in the search band
        gm = cv2.moments(g, binaryImage=True)
        if gm["m00"] <= 0:
            continue
        gcx = max(0, x - 8) + gm["m10"] / gm["m00"]
        if abs(gcx - (x + w / 2.0)) > max(6.0, w * 0.9):
            continue
        # a drawn bar has near-constant row width; a hoodie or a glyph does not
        sub = cv2.inRange(hsv[y:y + h, x:x + w], W_LO, W_HI)
        rw = (sub > 0).sum(axis=1).astype(float)
        rw = rw[rw > 0]
        if rw.size < 8 or float(np.std(rw) / max(np.mean(rw), 1e-6)) > 0.35:
            continue
        cands.append((x, y, w, h, max(0, y - up) + int(rows[0])))
    if len(cands) != 1:                        # ambiguous -> abstain
        return None
    x, y, w, h, apex = cands[0]
    pad = max(4, int(round(w * 0.55)))
    x0, x1 = max(0, x - pad), min(img.shape[1], x + w + pad)
    y0, y1 = max(0, apex - 2), min(img.shape[0], y + h + 3)
    if (x1 - x0) < 6 or (y1 - y0) < 20:
        return None
    return x0, y0, x1, y1


def main(out_root="datasets/meter2k27"):
    dumps = sorted(glob.glob("logs/diagnostics/framedump/session_2026082*"))
    random.seed(1234)
    rows = []
    # BACKGROUND IMAGES. Trained on positives alone, the detector had never seen a
    # menu labelled "not a meter" and fired on the leaderboard/loading UI -- the same
    # white-on-dark furniture that fooled the first labelling pass. Non-gameplay
    # frames are unambiguous negatives (a shot meter cannot be on a loading screen),
    # so they are safe to assert as empty. Capped so they cannot swamp the positives.
    negs = []
    for d in dumps:
        fs = sorted(glob.glob(os.path.join(d, "f*_raw.png")))
        kept = 0
        for p in fs:
            img = cv2.imread(p)
            if img is None:
                continue
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            if not is_gameplay(img, hsv):
                negs.append(p)
                continue
            b = label(img)
            if b:
                rows.append((p, b, img.shape[1], img.shape[0]))
                kept += 1
        print(f"  {os.path.basename(d)}: {kept}/{len(fs)} labelled", flush=True)
    print(f"TOTAL labelled: {len(rows)}", flush=True)
    random.shuffle(negs)
    negs = negs[:max(1, len(rows) // 3)]          # ~25% of the set, never dominant
    print(f"background (non-gameplay) frames: {len(negs)}", flush=True)
    random.shuffle(rows)
    n_val = max(1, int(len(rows) * 0.15))
    splits = {"val": rows[:n_val], "train": rows[n_val:]}
    for split, items in splits.items():
        im_dir = os.path.join(out_root, "images", split)
        lb_dir = os.path.join(out_root, "labels", split)
        os.makedirs(im_dir, exist_ok=True); os.makedirs(lb_dir, exist_ok=True)
        for i, (p, (x0, y0, x1, y1), W, H) in enumerate(items):
            stem = f"{i:06d}"
            cv2.imwrite(os.path.join(im_dir, stem + ".png"), cv2.imread(p))
            cx, cy = (x0 + x1) / 2.0 / W, (y0 + y1) / 2.0 / H
            bw, bh = (x1 - x0) / W, (y1 - y0) / H
            with open(os.path.join(lb_dir, stem + ".txt"), "w") as fh:
                fh.write(f"0 {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}\n")
        print(f"  wrote {len(items)} -> {split}", flush=True)
    # negatives: image with an EMPTY label file is YOLO's "background" convention
    n_val_neg = max(1, int(len(negs) * 0.15))
    for split, items in (("val", negs[:n_val_neg]), ("train", negs[n_val_neg:])):
        im_dir = os.path.join(out_root, "images", split)
        lb_dir = os.path.join(out_root, "labels", split)
        for i, p in enumerate(items):
            stem = f"neg{i:06d}"
            cv2.imwrite(os.path.join(im_dir, stem + ".png"), cv2.imread(p))
            open(os.path.join(lb_dir, stem + ".txt"), "w").close()
        print(f"  wrote {len(items)} background -> {split}", flush=True)
    with open(os.path.join(out_root, "data.yaml"), "w") as fh:
        fh.write(f"path: {os.path.abspath(out_root)}\ntrain: images/train\nval: images/val\n"
                 f"names:\n  0: meter\n")
    print("dataset ready:", os.path.abspath(out_root), flush=True)


if __name__ == "__main__":
    main(*(sys.argv[1:] or []))
