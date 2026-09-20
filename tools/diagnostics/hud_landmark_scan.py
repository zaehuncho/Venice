"""hud_landmark_scan.py -- blind search for a shot-locked HUD landmark in the framedump.

Read-only.  Asks: does NBA 2K27 draw ANY element whose pixels change state at a consistent
time relative to the owner's press or to the release, other than the shot meter?

Two grids are scanned:

  ABS  8x8 px cells over the whole 1280x720 frame -- finds screen-fixed HUD (scoreboard,
       badge strip, shot-feedback banner, stamina bar when the camera is still).
  REL  8x8 px cells in a window anchored on the ball handler's NAMEPLATE (the PS-logo disc
       found by tools/diagnostics/hud_3pt_icon.py, which sits directly under the player) --
       finds player-relative HUD: the indicator ring under the feet, the stamina bar, the
       3PT icon, the meter itself.

Per cell and frame the signature is (meanB, meanG, meanR, Sobel edge energy).  A cell is
"noisy" if consecutive-frame deltas are large in quiet footage; every shot-window delta is
z-scored against that per-cell noise so the crowd, the court and the players (which change
every frame by construction) score near zero and a HUD element that switches state scores high.

Ranking: a candidate is useful only if the spread of its change time is SMALLER measured
from the release than from the press -- the session's press->release hold ranges 640..1853 ms,
so the two hypotheses are cleanly separable.

Frame pitch is ~150 ms (6.7 fps), so the midpoint estimator floor is sd = 150/sqrt(12) =
43 ms.  Anything at or below that is unresolved, not tight -- see the report.

Usage:
    .venv/Scripts/python.exe tools/diagnostics/hud_landmark_scan.py [--workers 10]
Inputs : logs/diagnostics/hud_landmark_study/shots_table.csv, icon_frames.csv
Outputs: logs/diagnostics/hud_landmark_study/landmark_abs.csv, landmark_rel.csv,
         montage/lm_*.png
"""
from __future__ import annotations

import argparse
import bisect
import csv
import os
from concurrent.futures import ProcessPoolExecutor

import cv2
import numpy as np

DUMP = r"D:\NexusVision\framedump\session_20260912_201355"
OUT = os.path.join("logs", "diagnostics", "hud_landmark_study")
CELL = 8
GW, GH = 1280 // CELL, 720 // CELL          # 160 x 90 absolute cells
REL_X0, REL_X1 = -320, 208                  # player-relative window around the nameplate
REL_Y0, REL_Y1 = -432, 80                   # (nameplate is under the feet; meter is above)
RW, RH = (REL_X1 - REL_X0) // CELL, (REL_Y1 - REL_Y0) // CELL
PRE_MS, POST_MS = 600.0, 900.0              # window = press-PRE .. release+POST


def frame_path(idx: int):
    for d in (0, 1):
        p = os.path.join(DUMP, "f%05d_%d_raw.png" % (idx, d))
        if os.path.exists(p):
            return p
    return None


def feats_abs(img):
    b = cv2.resize(img, (GW, GH), interpolation=cv2.INTER_AREA).astype(np.float32)
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    e = cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0, 3), cv2.Sobel(g, cv2.CV_32F, 0, 1, 3))
    e = cv2.resize(e, (GW, GH), interpolation=cv2.INTER_AREA)
    return np.dstack([b, e])                # (GH, GW, 4)


def feats_rel(img, cx, cy):
    x0, y0 = int(round(cx)) + REL_X0, int(round(cy)) + REL_Y0
    pad = cv2.copyMakeBorder(img, 512, 512, 512, 512, cv2.BORDER_CONSTANT, value=(0, 0, 0))
    sub = pad[y0 + 512:y0 + 512 + (REL_Y1 - REL_Y0), x0 + 512:x0 + 512 + (REL_X1 - REL_X0)]
    b = cv2.resize(sub, (RW, RH), interpolation=cv2.INTER_AREA).astype(np.float32)
    g = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
    e = cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0, 3), cv2.Sobel(g, cv2.CV_32F, 0, 1, 3))
    e = cv2.resize(e, (RW, RH), interpolation=cv2.INTER_AREA)
    return np.dstack([b, e])


def _work(job):
    idx, cx, cy = job
    p = frame_path(idx)
    if p is None:
        return idx, None, None
    img = cv2.imread(p)
    if img is None:
        return idx, None, None
    fa = feats_abs(img)
    fr = feats_rel(img, cx, cy) if cx >= 0 else None
    return idx, fa.astype(np.float16), (None if fr is None else fr.astype(np.float16))


def read_frames():
    rows = []
    with open(os.path.join(DUMP, "frames.csv"), newline="") as f:
        for r in csv.DictReader(f):
            rows.append(dict(idx=int(r["idx"]), t=float(r["t_wall"]), det=int(r["detected"])))
    rows.sort(key=lambda r: r["t"])
    return rows


def _med(v):
    v = sorted(v)
    n = len(v)
    return 0.0 if not n else (v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2]))


def _rmad(v):
    m = _med(v)
    return _med([abs(x - m) for x in v])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=10)
    a = ap.parse_args()

    shots = [r for r in csv.DictReader(open(os.path.join(OUT, "shots_table.csv"), newline=""))]
    anchors = {}
    for r in csv.DictReader(open(os.path.join(OUT, "icon_frames.csv"), newline="")):
        if float(r["ps"]) >= 0.60 and float(r["tx"]) >= 0.35:
            anchors[int(r["idx"])] = (float(r["cx"]), float(r["cy"]))

    frames = read_frames()
    walls = [f["t"] for f in frames]
    byidx = {f["idx"]: f for f in frames}

    use = [s for s in shots if s["release_src"] == "engine"]
    print(f"{len(use)} engine-released shots")

    windows = {}
    need = set()
    for s in use:
        pt = float(s["press_wall"])
        rel = float(s["release_wall"])
        i0 = bisect.bisect_left(walls, pt - PRE_MS / 1000.0)
        i1 = bisect.bisect_right(walls, rel + POST_MS / 1000.0)
        ids = [frames[i]["idx"] for i in range(i0, i1)]
        windows[s["epoch"]] = ids
        need.update(ids)
    need = sorted(need)
    print(f"{len(need)} frames")

    jobs = [(i, anchors.get(i, (-1, -1))[0], anchors.get(i, (-1, -1))[1]) for i in need]
    FA, FR = {}, {}
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for n, (idx, fa, fr) in enumerate(ex.map(_work, jobs, chunksize=4)):
            if fa is not None:
                FA[idx] = fa
            if fr is not None:
                FR[idx] = fr
            if n % 400 == 0:
                print(f"  {n}/{len(need)}", flush=True)

    for tag, store, shape in (("abs", FA, (GH, GW)), ("rel", FR, (RH, RW))):
        scan(tag, store, shape, use, windows, byidx)


def scan(tag, store, shape, use, windows, byidx):
    H, W = shape
    # --- per-cell noise: |delta| between consecutive frames in the QUIET pre-press stretch
    noise = []
    for s in use:
        pt = float(s["press_wall"])
        ids = [i for i in windows[s["epoch"]]
               if i in store and byidx[i]["t"] - pt < -0.15]
        for a, b in zip(ids, ids[1:]):
            noise.append(np.abs(store[b].astype(np.float32) - store[a].astype(np.float32)))
    if len(noise) < 10:
        print(f"[{tag}] too few quiet pairs ({len(noise)})")
        return
    N = np.stack(noise)                        # (n, H, W, 4)
    n90 = np.percentile(N, 90, axis=0) + 1.0   # per cell per feature
    print(f"[{tag}] noise model from {len(noise)} quiet consecutive pairs")

    # --- per shot: for each cell the frame of the largest normalised change
    ev_press = np.full((len(use), H, W), np.nan, np.float32)
    ev_rel = np.full((len(use), H, W), np.nan, np.float32)
    ev_z = np.zeros((len(use), H, W), np.float32)
    for si, s in enumerate(use):
        pt, rel = float(s["press_wall"]), float(s["release_wall"])
        ids = [i for i in windows[s["epoch"]] if i in store]
        if len(ids) < 6:
            continue
        seq = np.stack([store[i].astype(np.float32) for i in ids])
        d = np.abs(np.diff(seq, axis=0)) / n90[None]      # (k-1, H, W, 4)
        z = d.max(axis=3)                                  # worst feature per cell
        k = z.argmax(axis=0)
        ev_z[si] = np.take_along_axis(z, k[None], 0)[0]
        # the change happened between frame k and k+1 -> midpoint
        t = np.array([byidx[i]["t"] for i in ids])
        mid = 0.5 * (t[:-1] + t[1:])
        ev_press[si] = (mid[k] - pt) * 1000.0
        ev_rel[si] = (mid[k] - rel) * 1000.0

    ZT = 3.0
    hit = (ev_z >= ZT)
    frac = hit.mean(axis=0)
    rows = []
    for y in range(H):
        for x in range(W):
            m = hit[:, y, x]
            if m.sum() < max(6, 0.35 * len(use)):
                continue
            p = ev_press[m, y, x]
            r = ev_rel[m, y, x]
            rows.append(dict(
                grid=tag, cx=x * CELL + (REL_X0 if tag == "rel" else 0),
                cy=y * CELL + (REL_Y0 if tag == "rel" else 0),
                n=int(m.sum()), frac=round(float(frac[y, x]), 3),
                z_med=round(float(np.median(ev_z[m, y, x])), 1),
                press_med=round(float(np.median(p)), 1), press_rmad=round(_rmad(list(p)), 1),
                press_sd=round(float(np.std(p)), 1),
                rel_med=round(float(np.median(r)), 1), rel_rmad=round(_rmad(list(r)), 1),
                rel_sd=round(float(np.std(r)), 1),
            ))
    rows.sort(key=lambda r: (min(r["rel_sd"], r["press_sd"]), -r["frac"]))
    p = os.path.join(OUT, f"landmark_{tag}.csv")
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"[{tag}] {p}: {len(rows)} cells fire on >=35% of shots")
    print(f"[{tag}] top 25 by tightness (sd of the change time, ms):")
    print("      cell(x,y)   n  frac  z   |  press med/sd   release med/sd   winner")
    for r in rows[:25]:
        win = "RELEASE" if r["rel_sd"] < r["press_sd"] else "press"
        print("   %5d,%-5d %3d %5.2f %4.1f | %7.0f %6.0f   %7.0f %6.0f   %s"
              % (r["cx"], r["cy"], r["n"], r["frac"], r["z_med"],
                 r["press_med"], r["press_sd"], r["rel_med"], r["rel_sd"], win))


if __name__ == "__main__":
    main()
