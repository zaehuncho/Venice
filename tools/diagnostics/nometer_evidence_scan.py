"""Gate-free evidence scan: was a meter DRAWN in this press window at all?

Independent of MeterContourLocator's gates. Reports, per frame, the white
neutral-bright columns in the meter band and the green blobs (the tip is a
saturated green triangle that nothing else on a basketball court draws in
that band). If both exist in the right relative geometry, a meter was drawn.
READ-ONLY.
"""
from __future__ import annotations
import os, sys, json
import numpy as np, cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from nometer_window_forensics import load_records, load_frames_csv, frame_paths, DUMP  # noqa

V_MIN, SPREAD_MAX, GAP_CLOSE = 225, 25, 3
GREEN_LO, GREEN_HI = (38, 90, 90), (85, 255, 255)
GREEN_LO_LOOSE = (35, 50, 60)          # the 09-15 tipless finding: decoder-washed tips
BAND_TOP, BAND_BOT = 0.08, 0.62
GAP_LO, GAP_HI = 92.0, 115.0   # locator gate 5: green top sits ~100 px above the white bottom        # scan rows, fraction of H (locator band + gate_top)


def white_columns(img, s=1.0):
    H, W = img.shape[:2]
    y0, y1 = int(BAND_TOP * H), int(BAND_BOT * H)
    sub = img[y0:y1]
    mx = sub.max(axis=2).astype(np.int16); mn = sub.min(axis=2).astype(np.int16)
    m = ((mx >= V_MIN) & ((mx - mn) <= SPREAD_MAX)).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((GAP_CLOSE, 1), np.uint8))
    n, lab, st, cen = cv2.connectedComponentsWithStats(m, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if 5 <= w <= 26 and h >= 6 and h / max(1.0, w) >= 0.8:
            out.append((int(x), int(y + y0), int(w), int(h), int(a)))
    return sorted(out, key=lambda c: -c[3])


def green_blobs(img, loose=False):
    H, W = img.shape[:2]
    y0, y1 = int(BAND_TOP * H), int(BAND_BOT * H)
    hsv = cv2.cvtColor(img[y0:y1], cv2.COLOR_BGR2HSV)
    lo = GREEN_LO_LOOSE if loose else GREEN_LO
    m = cv2.inRange(hsv, np.array(lo, np.uint8), np.array(GREEN_HI, np.uint8))
    n, lab, st, cen = cv2.connectedComponentsWithStats(m, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if a >= 4 and w <= 40 and h <= 40:
            out.append((int(x), int(y + y0), int(w), int(h), int(a)))
    return sorted(out, key=lambda c: -c[4])


def meter_evidence(img):
    """-> (best_pair or None, n_white, n_green). A pair = a white column whose bottom
    sits ~100 px below a green blob within +-6 px in x (the locator's own landmarks,
    with a WIDE tolerance and no shape/lone/outline gate)."""
    cols = white_columns(img)
    grn = green_blobs(img)
    best = None
    for (x, y, w, h, a) in cols:
        cx = x + w * 0.5
        wbot = y + h
        for (gx, gy, gw, gh, ga) in grn:
            gcx = gx + gw * 0.5
            gap = wbot - gy
            if abs(gcx - cx) <= 10 and GAP_LO <= gap <= GAP_HI and gw <= 20 and gh <= 26:
                cand = (x, y, w, h, gx, gy, gw, gh, ga, gap)
                if best is None or ga > best[8]:
                    best = cand
    return best, cols, grn


def sweep(epochs):
    recs = load_records(); fr = load_frames_csv(recs)
    for ep in epochs:
        r = recs[ep]; fd = r["framedump"]; press = r["press_ts_ms"] / 1000.0
        files = frame_paths(ep, fd["first_idx"], fd["last_idx"])
        print(f"\n### epoch {ep} {r['shot_type']} hold={r.get('release_after_press_ms')} "
              f"onset={r.get('onset_ms')} frames={len(files)}")
        first_pair = None; npair = 0
        rows = []
        for i, p in files:
            row = fr.get(i)
            t = (float(row["t_wall"]) - press) * 1000.0 if row else float("nan")
            img = cv2.imread(p)
            if img is None:
                continue
            pair, cols, grn = meter_evidence(img)
            det = "1" if (row and row["detected"] == "1") else "0"
            if pair:
                npair += 1
                if first_pair is None:
                    first_pair = (i, t, pair)
            rows.append((i, t, det, pair, len(cols), len(grn)))
        print(f"  frames with a meter-shaped white+green pair: {npair}/{len(rows)}")
        if first_pair:
            i, t, pr = first_pair
            print(f"  FIRST pair idx={i} t=+{t:.0f}ms col=({pr[0]},{pr[1]},{pr[2]},{pr[3]}) "
                  f"tip=({pr[4]},{pr[5]}) tip_px={pr[8]} gap={pr[9]}")
        else:
            print("  NO meter-shaped pair anywhere in the window (gate-free scan)")
        for i, t, det, pair, nc, ng in rows:
            tag = "PAIR" if pair else "    "
            px = pair[8] if pair else 0
            print(f"    f{i} t={t:+8.1f} live_det={det} {tag} tip_px={px:3d} cols={nc:2d} green={ng:2d}")


if __name__ == "__main__":
    sweep([int(a) for a in sys.argv[1:]] or [7, 35, 50])
