"""Contact sheet per press window: 12 frames, press-100 .. release+300 ms.

Each tile is the full frame (scaled) over a native-resolution crop of the meter
region / the shooter, with the live reader's box drawn when it had one and the
gate-free white+green landmark pair drawn when one exists. READ-ONLY.
"""
from __future__ import annotations
import os, sys
import numpy as np, cv2

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))
from nometer_window_forensics import load_records, load_frames_csv, frame_paths, OUT  # noqa
from nometer_evidence_scan import meter_evidence  # noqa

TILE_W, TILE_H = 480, 270
CROP_W, CROP_H = 480, 230

# x0,y0 of the native-res crop; None = follow the meter/the eventual meter site
CROP = {7: (900, 250), 35: (1000, 250), 50: (850, 200), 34: (750, 250), 13: (700, 250)}


def sheet(ep, recs, fr, n=12):
    r = recs[ep]; fd = r["framedump"]; press = r["press_ts_ms"] / 1000.0
    rel = r.get("release_after_press_ms") or 650.0
    lo, hi = -100.0, rel + 300.0
    files = frame_paths(ep, fd["first_idx"], fd["last_idx"])
    cand = []
    for i, p in files:
        row = fr.get(i)
        if row is None:
            continue
        t = (float(row["t_wall"]) - press) * 1000.0
        if lo <= t <= hi:
            cand.append((i, p, t, row))
    if not cand:
        return None
    step = max(1, len(cand) // n)
    pick = cand[::step][:n]
    while len(pick) < n and len(cand) >= n:
        pick.append(cand[-1])
    cx0, cy0 = CROP.get(ep, (900, 250))
    tiles = []
    for i, p, t, row in pick:
        img = cv2.imread(p)
        if img is None:
            continue
        ann = img.copy()
        pair, _, _ = meter_evidence(img)
        det = row["detected"] == "1"
        if det:
            bx, by, bw, bh = (int(row["bbox_x"]), int(row["bbox_y"]),
                              int(row["bbox_w"]), int(row["bbox_h"]))
            cv2.rectangle(ann, (bx, by), (bx + bw, by + bh), (0, 255, 0), 2)
        if pair:
            x, y, w, h, gx, gy, gw, gh, ga, gap = pair
            cv2.rectangle(ann, (x - 8, gy - 6), (x + w + 8, y + h + 4), (0, 200, 255), 2)
        top = cv2.resize(ann, (TILE_W, TILE_H))
        crop = ann[cy0:cy0 + CROP_H, cx0:cx0 + CROP_W].copy()
        if crop.shape[:2] != (CROP_H, CROP_W):
            crop = cv2.copyMakeBorder(crop, 0, CROP_H - crop.shape[0], 0,
                                      max(0, CROP_W - crop.shape[1]), cv2.BORDER_CONSTANT)
        tile = np.vstack([top, crop])
        lab = f"f{i} t={t:+.0f}ms det={'Y' if det else 'n'} pair={'Y' if pair else 'n'}"
        if pair:
            lab += f" tip_px={pair[8]}"
        cv2.rectangle(tile, (0, 0), (TILE_W, 22), (0, 0, 0), -1)
        cv2.putText(tile, lab, (5, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
        cv2.rectangle(tile, (0, 0), (TILE_W - 1, tile.shape[0] - 1), (60, 60, 60), 1)
        tiles.append(tile)
    while len(tiles) % 4:
        tiles.append(np.zeros_like(tiles[0]))
    grid = np.vstack([np.hstack(tiles[k:k + 4]) for k in range(0, len(tiles), 4)])
    hdr = np.zeros((44, grid.shape[1], 3), np.uint8)
    cv2.putText(hdr, f"epoch {ep}  {r['shot_type']}  hold={rel:.0f}ms  "
                     f"live_onset={r.get('onset_ms')}  session_20260917_030758  "
                     f"(green=reader box, orange=gate-free white+green landmark pair)",
                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    out = os.path.join(OUT, f"contact_ep{ep}.png")
    cv2.imwrite(out, np.vstack([hdr, grid]))
    return out


if __name__ == "__main__":
    recs = load_records(); fr = load_frames_csv(recs)
    for a in (sys.argv[1:] or ["7", "35", "50"]):
        print(sheet(int(a), recs, fr))
