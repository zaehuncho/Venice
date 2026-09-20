#!/usr/bin/env python3
"""green_window_probe.py -- measure NBA 2K27's rendered GREEN make-window directly from
pixels, independently of simple_meter_reader's own green detection (which is the
instrument under audit; it is used here only as the comparison arm).

Two evidence arms:

  video      1080p gameplay footage with the Pill (capsule) meter, where the green
             make-window is rendered from shot start as a green dome at the capsule top.
             Primary arm: the band is directly visible and ~2.25x the pixel area of the
             720p framedumps.

  framedump  720p sidecar framedump sessions with the Arrow2 (banner) meter. Secondary
             arm: census of every green pixel inside the detector box per shot run, put
             on the READER'S OWN fill scale by regressing the CSV fill_pct against the
             directly measured white-fill edge row (so no reimplementation of the
             reader's denominator is needed -- the mapping is calibrated per shot from
             the reader's own outputs).

Scale convention (both arms): percent-of-meter with 0 = fill base furniture edge and
100 = meter tip (capsule inner apex / banner cap top), matching the reader's fill_pct.
Edges are measured sub-pixel via 50% crossings on column-averaged profiles, mirroring
how the reader's own sub-pixel fill edge works.

Usage:
  python tools/quality/green_window_probe.py video <path.mp4> [--out DIR] [--sheets]
  python tools/quality/green_window_probe.py framedump <session_dir> [...] [--out DIR]

Writes per-frame and per-shot CSVs into --out (default: alongside this tool under
logs/diagnostics/green_window_probe/) and prints a per-shot summary. Read-only with
respect to every input; touches nothing in the repo.
"""
from __future__ import annotations

import argparse
import csv
import glob as _glob
import math
import os
import re
import sys
from collections import defaultdict

import cv2
import numpy as np

# --------------------------------------------------------------------------- helpers

def robust_line_fit(xs, ys):
    """Least squares slope/intercept with one pass of 2.5-sigma residual rejection.
    Returns (slope, intercept, n_used, rms)."""
    xs = np.asarray(xs, float); ys = np.asarray(ys, float)
    if xs.size < 3:
        return None
    for _ in range(2):
        A = np.vstack([xs, np.ones_like(xs)]).T
        sol, *_ = np.linalg.lstsq(A, ys, rcond=None)
        pred = A @ sol
        res = ys - pred
        sd = res.std() or 1e-9
        keep = np.abs(res) <= 2.5 * sd
        if keep.all() or keep.sum() < 3:
            break
        xs, ys = xs[keep], ys[keep]
    A = np.vstack([xs, np.ones_like(xs)]).T
    sol, *_ = np.linalg.lstsq(A, ys, rcond=None)
    rms = float(np.sqrt(np.mean((ys - A @ sol) ** 2)))
    return float(sol[0]), float(sol[1]), int(xs.size), rms


def cross50(profile, lo_row, hi_row, baseline, plateau, direction):
    """Sub-pixel row where `profile` crosses (baseline+plateau)/2 between lo_row and
    hi_row (inclusive), scanning down. direction=+1: rising edge (dark->bright),
    -1: falling edge. Returns float row or None."""
    thr = 0.5 * (baseline + plateau)
    rows = range(int(lo_row), int(hi_row))
    for r in rows:
        a, b = profile[r], profile[r + 1]
        if direction > 0 and a < thr <= b:
            return r + (thr - a) / (b - a)
        if direction < 0 and a >= thr > b:
            return r + (a - thr) / (a - b)
    return None


def pctile(v, q):
    return float(np.percentile(np.asarray(v, float), q)) if len(v) else float("nan")


# =========================================================================== video arm

GE_GREEN = 5.0          # greenness-excess floor marking "some green" (G - max(B,R))


def _greenness(sub):
    f = sub.astype(np.float32)
    return f[:, :, 1] - np.maximum(f[:, :, 0], f[:, :, 2])


def find_dome(frame):
    """Locate the Pill meter's green dome. Returns (x, y, w, h, cx) or None."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    m = ((hsv[:, :, 0] >= 40) & (hsv[:, :, 0] <= 85)
         & (hsv[:, :, 1] >= 100) & (hsv[:, :, 2] >= 100)).astype(np.uint8)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
    best = None
    H, W = frame.shape[:2]
    for i in range(1, n):
        x, y, w, h, a = stats[i]
        if a < 20 or not (6 <= w <= 45) or not (3 <= h <= 30):
            continue
        if y < 120 or y > H - 200:          # HUD row / bottom scorebug
            continue
        if not (0.25 <= h / float(w) <= 1.3):   # dome: wider than tall-ish half disc
            continue
        # capsule check: a dark track or bright fill column must extend >= 60 px below
        cx = int(round(cent[i][0]))
        x0, x1 = max(0, cx - 6), min(W, cx + 7)
        col = cv2.cvtColor(frame[y + h:y + h + 90, x0:x1], cv2.COLOR_BGR2HSV)
        if col.shape[0] < 80:
            continue
        V = col[:, :, 2].astype(np.float32).mean(axis=1)
        S = col[:, :, 1].astype(np.float32).mean(axis=1)
        trackish = ((S <= 90) & ((V >= 30))).mean()
        if trackish < 0.80:
            continue
        score = a
        if best is None or score > best[0]:
            best = (score, x, y, w, h, cx)
    if best is None:
        return None
    return best[1:]


def measure_pill(frame, dome):
    """Measure capsule geometry + green band + fill edge for one frame.

    Returns dict or None. All rows are absolute frame rows (float where sub-pixel).
    Scale: pp(y) = (base - y) / (base - apex) * 100 with apex = dome top 50% edge,
    base = fill-column bottom 50% edge (the capsule's bottom inner edge).
    """
    x, y, w, h, cx = dome
    H, W = frame.shape[:2]
    top = max(0, y - 18)
    bot = min(H, y + 230)
    x0, x1 = max(0, cx - 12), min(W, cx + 13)
    sub = frame[top:bot, x0:x1]
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    ge = _greenness(sub)
    c0, c1 = max(0, (cx - x0) - 6), (cx - x0) + 7      # central columns
    geC = ge[:, c0:c1].mean(axis=1)
    V = hsv[:, :, 2].astype(np.float32)[:, c0:c1].mean(axis=1)
    S = hsv[:, :, 1].astype(np.float32)[:, c0:c1].mean(axis=1)
    nrows = geC.shape[0]

    # ---- dome rows (relative) and plateau
    dometop_rel = y - top
    grn = np.flatnonzero(geC[:min(nrows, dometop_rel + h + 14)] > GE_GREEN)
    grn = grn[(grn >= dometop_rel - 6)]
    if grn.size < 2:
        return None
    g_lo, g_hi = int(grn[0]), int(grn[-1])
    plateau = float(np.max(geC[g_lo:g_hi + 1]))
    if plateau < 25:
        return None
    baseline = 0.0
    # top edge of the dome (apex): rising 50% crossing above the dome core
    apex = cross50(geC, max(0, g_lo - 5), min(nrows - 2, g_lo + 4), baseline, plateau, +1)
    # bottom edge of the dome: falling 50% crossing below the core
    dome_bot = cross50(geC, max(0, g_hi - 5), min(nrows - 2, g_hi + 6), baseline, plateau, -1)
    if apex is None or dome_bot is None:
        return None

    # ---- fill block + capsule bottom.
    # Row classes under the dome: fill = bright/neutral (V>=120, S<=45) over central
    # cols; track = dark neutral; rungs = thin bright lines (<=3 rows).
    is_bright = (V >= 120) & (S <= 60)
    # capsule bottom: last row (below dome) in the maximal contiguous run of
    # "meter-ish" rows (bright fill OR dark track with modest saturation)
    meterish = ((V >= 25) & (S <= 100)) | is_bright
    run_end = g_hi
    r = g_hi
    while r + 1 < nrows and meterish[r + 1]:
        r += 1
    run_end = r
    if run_end - g_hi < 60:      # capsule must extend well below the dome
        return None
    # fill base: within the meter run, the bottom of the LAST long bright block
    bright_rows = np.flatnonzero(is_bright[:run_end + 1])
    bright_rows = bright_rows[bright_rows > g_hi]
    fill_top = fill_base = None
    if bright_rows.size:
        # group contiguous bright runs, keep runs > 4 rows (rungs are 1-3 rows)
        splits = np.flatnonzero(np.diff(bright_rows) > 2)
        runs = np.split(bright_rows, splits + 1)
        longruns = [rr for rr in runs if rr.size >= 5]
        if longruns:
            block = longruns[-1]
            # merge preceding long runs separated only by rung-like gaps (<=4 rows)
            for rr in reversed(longruns[:-1]):
                if block[0] - rr[-1] <= 5:
                    block = np.concatenate([rr, block])
            fill_top_i, fill_base_i = int(block[0]), int(block[-1])
            fplat = float(np.median(V[fill_top_i:min(fill_base_i + 1, fill_top_i + 12)]))
            fbase = float(np.median(V[max(g_hi + 2, fill_top_i - 8):fill_top_i])) if fill_top_i - (g_hi + 2) >= 3 else 60.0
            e = cross50(V, max(0, fill_top_i - 4), min(nrows - 2, fill_top_i + 2), fbase, fplat, +1)
            fill_top = e if e is not None else float(fill_top_i)
            e2 = cross50(V, max(0, fill_base_i - 2), min(nrows - 2, fill_base_i + 5), fplat, float(np.median(V[fill_base_i + 2:fill_base_i + 8])) if fill_base_i + 8 <= nrows else 40.0, -1)
            fill_base = e2 if e2 is not None else float(fill_base_i)
    if fill_base is None:
        # no fill yet (shot not started); use capsule run end as base estimate
        fill_base = float(run_end)
    span = fill_base - apex
    if span < 60:
        return None
    # Alternative apex anchor: the first row with ANY green in the central columns
    # (the mean-profile 50% crossing sits 1-3 px below the capsule's true inner apex
    # because the dome is rounded -- fewer green columns at its top rows pull the
    # column-mean down). The truth lies between the two; both are reported and the
    # difference is carried as measurement uncertainty.
    apex_ext = float(g_lo)
    span_ext = fill_base - apex_ext
    def pp(row):        # sub-relative rows -> percent-of-meter (mean-cross anchor)
        return (fill_base - row) / span * 100.0
    def pp_ext(row):
        return (fill_base - row) / span_ext * 100.0
    return dict(
        apex_abs=top + apex, dome_bot_abs=top + dome_bot, fill_base_abs=top + fill_base,
        fill_top_abs=(top + fill_top) if fill_top is not None else float("nan"),
        span_px=span,
        band_width_pp=100.0 - pp(dome_bot),
        band_width_pp_ext=100.0 - pp_ext(min(float(g_hi) + 1.0, dome_bot + 1.0)),
        fill_pp=pp(fill_top) if fill_top is not None else 0.0,
        dome_px=dome_bot - apex, plateau=plateau, cx=cx,
        band_width_pp_loose=(g_hi + 1 - g_lo) / span * 100.0,
        strict_rows=int(((geC[g_lo:g_hi + 1]) > 0.5 * plateau).sum()),
    )


def run_video(path, outdir, sheets=False):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        print(f"ERROR: cannot open {path}"); return 2
    fps = cap.get(cv2.CAP_PROP_FPS) or 59.94
    dt = 1000.0 / fps
    rows = []
    idx = -1
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        idx += 1
        dome = find_dome(fr)
        if dome is None:
            continue
        meas = measure_pill(fr, dome)
        if meas is None:
            continue
        meas["idx"] = idx; meas["t_ms"] = idx * dt
        rows.append(meas)
    cap.release()
    os.makedirs(outdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(path))[0]
    perframe = os.path.join(outdir, f"video_{base}_frames.csv")
    if rows:
        with open(perframe, "w", newline="") as fh:
            wcsv = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            wcsv.writeheader(); wcsv.writerows(rows)
    # ---- segment shots: consecutive dome frames, gap <= 6
    shots = []
    cur = []
    for r in rows:
        if cur and r["idx"] - cur[-1]["idx"] > 6:
            shots.append(cur); cur = []
        cur.append(r)
    if cur:
        shots.append(cur)
    shots = [s for s in shots if len(s) >= 5]
    print(f"video: {len(rows)} meter frames -> {len(shots)} shots  (fps={fps:.2f})")
    summary = []
    for si, s in enumerate(shots):
        widths = [r["band_width_pp"] for r in s]
        spans = [r["span_px"] for r in s]
        # MONOTONE RISE prefix: frames until the fill first stalls above 85 (release
        # freeze / post-release wobble must not pollute the rate fit)
        rise = []
        prev = -1e9
        for r in s:
            f = r["fill_pp"]
            if f > 85.0 and f - prev < 0.3:
                break
            rise.append(r); prev = f
        is_rise = len(rise) >= 6 and (rise[-1]["fill_pp"] - rise[0]["fill_pp"]) >= 30.0
        # PRISTINE frames: healthy dome (plateau >= 100) and the fill still clearly
        # BELOW the band (a frozen marker inside the dome eats it from below and a
        # dimmed dome loses its antialiased skirt to the absolute greenness floor)
        band_lo_est = 100.0 - float(np.median(widths))
        prist = [r for r in s if r["plateau"] >= 100 and r["fill_pp"] < band_lo_est - 3.0]
        src = prist if len(prist) >= 5 else s
        w_med = float(np.median([r["band_width_pp"] for r in src]))
        we_med = float(np.median([r["band_width_pp_ext"] for r in src]))
        wl_med = float(np.median([r["band_width_pp_loose"] for r in src]))
        rate_tip = rate_mid = None
        if is_rise:
            tipseg = [r for r in rise if 60.0 <= r["fill_pp"] <= 95.0]
            if len(tipseg) >= 3:
                fit = robust_line_fit([r["t_ms"] for r in tipseg], [r["fill_pp"] for r in tipseg])
                if fit and fit[0] > 0.02:
                    rate_tip = fit[0]
            midseg = [r for r in rise if 15.0 <= r["fill_pp"] <= 50.0]
            if len(midseg) >= 3:
                fit = robust_line_fit([r["t_ms"] for r in midseg], [r["fill_pp"] for r in midseg])
                if fit and fit[0] > 0.02:
                    rate_mid = fit[0]
        summary.append(dict(
            shot=si, n_frames=len(s), idx0=s[0]["idx"], idx1=s[-1]["idx"],
            is_rise=int(is_rise), n_pristine=len(prist),
            span_px=round(float(np.median(spans)), 1),
            fill0=round(s[0]["fill_pp"], 1),
            band_width_pp_med=round(w_med, 2),
            band_width_pp_ext_med=round(we_med, 2),
            band_width_pp_loose_med=round(wl_med, 2),
            band_width_pp_iqr=(round(pctile([r["band_width_pp"] for r in src], 25), 2),
                               round(pctile([r["band_width_pp"] for r in src], 75), 2)),
            dome_px_med=round(float(np.median([r["dome_px"] for r in src])), 1),
            plateau_med=round(float(np.median([r["plateau"] for r in src])), 0),
            peak_fill_pp=round(max(r["fill_pp"] for r in s), 1),
            rate_tip_pp_ms=round(rate_tip, 4) if rate_tip else None,
            rate_mid_pp_ms=round(rate_mid, 4) if rate_mid else None,
            band_ms=round(w_med / rate_tip, 1) if rate_tip else None,
            band_ms_ext=round(we_med / rate_tip, 1) if rate_tip else None,
        ))
    persh = os.path.join(outdir, f"video_{base}_shots.csv")
    if summary:
        with open(persh, "w", newline="") as fh:
            wcsv = csv.DictWriter(fh, fieldnames=list(summary[0].keys()))
            wcsv.writeheader(); wcsv.writerows(summary)
    for srow in summary:
        print(srow)
    if sheets and shots:
        cap = cv2.VideoCapture(path)
        for si, s in enumerate(shots):
            mids = s[len(s) // 2]
            cap.set(cv2.CAP_PROP_POS_FRAMES, mids["idx"])
            ok, fr = cap.read()
            if not ok:
                continue
            cx = int(mids["cx"]); a = int(mids["apex_abs"]); b = int(mids["fill_base_abs"])
            crop = fr[max(0, a - 25):b + 25, max(0, cx - 40):cx + 40]
            big = cv2.resize(crop, (crop.shape[1] * 4, crop.shape[0] * 4),
                             interpolation=cv2.INTER_NEAREST)
            cv2.imwrite(os.path.join(outdir, f"video_{base}_shot{si}_f{mids['idx']}.png"), big)
        cap.release()
    print(f"wrote {perframe}\n      {persh}")
    return 0


# ====================================================================== framedump arm

def load_frame_png(sess, idx):
    m = _glob.glob(os.path.join(sess, f"f{idx:05d}_*_raw.png"))
    return cv2.imread(m[0]) if m else None


def census_frame(img, bbox, pad=10):
    """Green + fill measurement inside (and just around) the detector box.
    Returns dict with green segments (inside-box rows), fill edge row, contamination."""
    bx, by, bw, bh = bbox
    H, W = img.shape[:2]
    x0, y0 = max(0, bx - pad), max(0, by - pad)
    x1, y1 = min(W, bx + bw + pad), min(H, by + bh + pad)
    sub = img[y0:y1, x0:x1]
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    Hh, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    in_c0, in_c1 = (bx - x0) + 2, (bx - x0) + bw - 2         # inside columns
    strict = ((Hh >= 38) & (Hh <= 85) & (S >= 90) & (V >= 90))
    loose = ((Hh >= 35) & (Hh <= 85) & (S >= 55) & (V >= 55))
    seg_rows_in = np.flatnonzero(strict[:, in_c0:in_c1].any(axis=1))
    # segments with gap tolerance 1
    segs = []
    if seg_rows_in.size:
        splits = np.flatnonzero(np.diff(seg_rows_in) > 2)
        for rr in np.split(seg_rows_in, splits + 1):
            px = int(strict[rr[0]:rr[-1] + 1, in_c0:in_c1].sum())
            segs.append((int(rr[0]) + y0, int(rr[-1]) + y0, px))
    out_px = int(strict.sum()) - int(strict[:, in_c0:in_c1].sum())
    # white fill edge: >=45% of inside columns V>=180 & S<=70
    fillmask = ((V >= 180) & (S <= 70))[:, in_c0:in_c1]
    frac = fillmask.mean(axis=1)
    rows = np.flatnonzero(frac >= 0.45)
    fill_edge = None
    if rows.size >= 3:
        # topmost row of the longest contiguous run
        splits = np.flatnonzero(np.diff(rows) > 2)
        runs = np.split(rows, splits + 1)
        runs.sort(key=len)
        fill_edge = int(runs[-1][0]) + y0
        fill_bottom = int(runs[-1][-1]) + y0
    else:
        fill_bottom = None
    return dict(green_segs=segs, out_px=out_px,
                loose_px=int(loose[:, in_c0:in_c1].sum()),
                fill_edge=fill_edge, fill_bottom=fill_bottom)


def run_framedump(sessions, outdir):
    os.makedirs(outdir, exist_ok=True)
    allshot = []
    for sess in sessions:
        name = os.path.basename(os.path.normpath(sess))
        csvp = os.path.join(sess, "frames.csv")
        if not os.path.isfile(csvp):
            print(f"skip {name}: no frames.csv"); continue
        rows = []
        with open(csvp, newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    rows.append(dict(idx=int(r["idx"]), t_ms=float(r["t_ms"]),
                                     t_wall=float(r["t_wall"]), det=int(r["detected"]),
                                     fill=float(r["fill_pct"]), gc=float(r["green_center_pct"]),
                                     bx=int(r["bbox_x"]), by=int(r["bbox_y"]),
                                     bw=int(r["bbox_w"]), bh=int(r["bbox_h"])))
                except Exception:
                    pass
        # shot runs: consecutive detected frames (frame gap <= 15), needing a real rise
        runs, cur = [], []
        for r in rows:
            if not r["det"] or r["bw"] < 8:
                continue
            if cur and (r["idx"] - cur[-1]["idx"] > 15 or r["t_ms"] - cur[-1]["t_ms"] > 700):
                runs.append(cur); cur = []
            cur.append(r)
        if cur:
            runs.append(cur)
        runs = [rr for rr in runs
                if max(x["fill"] for x in rr) >= 60 and sum(1 for x in rr if x["fill"] > 10) >= 6]
        print(f"{name}: {len(runs)} shot runs")
        for ri, rr in enumerate(runs):
            peak_i = int(np.argmax([x["fill"] for x in rr]))
            # measure pixels on the rise + a short tail
            meas = []
            for x in rr:
                img = load_frame_png(sess, x["idx"])
                if img is None:
                    continue
                m = census_frame(img, (x["bx"], x["by"], x["bw"], x["bh"]))
                m.update(x)
                meas.append(m)
            if len(meas) < 6:
                continue
            # calibration: reader fill_pct vs measured fill-edge row (rise frames only)
            rise = meas[:peak_i + 1]
            pairs = [(m["fill_edge"], m["fill"]) for m in rise
                     if m["fill_edge"] is not None and 10 <= m["fill"] <= 88]
            fit = robust_line_fit([p[0] for p in pairs], [p[1] for p in pairs]) \
                if len(pairs) >= 4 else None
            if fit is None or fit[0] >= 0:      # fill grows upward: slope must be negative
                continue
            slope, icpt, nfit, rms = fit
            def pp(row):
                return slope * row + icpt
            # cap: per-frame green segment nearest/above the 90pp line, height & extent
            caps, extras = [], []
            for m in meas:
                for (r0, r1, px) in m["green_segs"]:
                    lo_pp, hi_pp = pp(r1), pp(r0)      # row r0 above r1 -> higher pp
                    if hi_pp >= 80.0:
                        caps.append((m["idx"], m["t_ms"], r0, r1, px, lo_pp, hi_pp))
                    else:
                        extras.append((m["idx"], lo_pp, hi_pp, px))
            rise_caps = [c for c in caps if c[0] <= rr[peak_i]["idx"]]
            use = rise_caps if len(rise_caps) >= 3 else caps
            cap_h_px = [c[3] - c[2] + 1 for c in use]
            cap_lo = [c[5] for c in use]
            cap_hi = [c[6] for c in use]
            # local rates
            tipseg = [(x["t_ms"], x["fill"]) for x in rise if 65 <= x["fill"] <= 97]
            midseg = [(x["t_ms"], x["fill"]) for x in rise if 25 <= x["fill"] <= 60]
            rate_tip = rate_mid = None
            for seg, dst in ((tipseg, "tip"), (midseg, "mid")):
                if len(seg) >= 3:
                    f = robust_line_fit([a for a, _ in seg], [b for _, b in seg])
                    if f and f[0] > 0.02:
                        if dst == "tip":
                            rate_tip = f[0]
                        else:
                            rate_mid = f[0]
            gcs = [x["gc"] for x in rr if x["gc"] >= 0]
            shotrow = dict(
                session=name, run=ri, idx0=rr[0]["idx"], idx1=rr[-1]["idx"],
                n=len(meas), peak_fill=round(rr[peak_i]["fill"], 2),
                cal_slope_pp_per_row=round(slope, 4), cal_rms_pp=round(rms, 2), cal_n=nfit,
                cap_n=len(use),
                cap_h_px_med=round(float(np.median(cap_h_px)), 1) if cap_h_px else None,
                cap_lo_pp_med=round(float(np.median(cap_lo)), 2) if cap_lo else None,
                cap_hi_pp_med=round(float(np.median(cap_hi)), 2) if cap_hi else None,
                cap_width_pp_med=round(float(np.median([h - l for l, h in zip(cap_lo, cap_hi)])), 2) if cap_lo else None,
                extra_band_n=len(extras),
                extra_bands=";".join(f"{i}:{lo:.1f}-{hi:.1f}({px})" for i, lo, hi, px in extras[:6]),
                out_px_max=max(m["out_px"] for m in meas),
                rate_tip=round(rate_tip, 4) if rate_tip else None,
                rate_mid=round(rate_mid, 4) if rate_mid else None,
                csv_green_center_med=round(float(np.median(gcs)), 2) if gcs else None,
                t_wall0=rr[0]["t_wall"],
            )
            allshot.append(shotrow)
    outp = os.path.join(outdir, "framedump_shots.csv")
    if allshot:
        with open(outp, "w", newline="") as fh:
            wcsv = csv.DictWriter(fh, fieldnames=list(allshot[0].keys()))
            wcsv.writeheader(); wcsv.writerows(allshot)
    for s in allshot:
        print({k: s[k] for k in ("session", "run", "peak_fill", "cap_h_px_med",
                                 "cap_lo_pp_med", "cap_hi_pp_med", "cap_width_pp_med",
                                 "extra_band_n", "rate_tip", "csv_green_center_med")})
    print(f"wrote {outp} ({len(allshot)} shot rows)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sp = ap.add_subparsers(dest="cmd", required=True)
    v = sp.add_parser("video"); v.add_argument("path"); v.add_argument("--out")
    v.add_argument("--sheets", action="store_true")
    f = sp.add_parser("framedump"); f.add_argument("sessions", nargs="+"); f.add_argument("--out")
    a = ap.parse_args()
    default_out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "..", "logs", "diagnostics", "green_window_probe")
    out = os.path.abspath(a.out or default_out)
    if a.cmd == "video":
        return run_video(a.path, out, sheets=a.sheets)
    return run_framedump(a.sessions, out)


if __name__ == "__main__":
    sys.exit(main())
