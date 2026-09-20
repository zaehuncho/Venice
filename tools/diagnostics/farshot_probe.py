"""farshot_probe.py -- why did the CV locator not see the meter on these frames?

Read-only study for the owner's "can't see from beyond half court" complaint.
Replays a list of framedump indices through a LADDER of MeterContourLocator
configurations (each cumulative on the previous) and reports, per frame, the first
rung at which a box appears. The rung that first finds it names the blocking gate.

Usage:
  .venv/Scripts/python.exe tools/diagnostics/farshot_probe.py --presses miss
  .venv/Scripts/python.exe tools/diagnostics/farshot_probe.py --range 0 6000 --rungs ship
"""
from __future__ import annotations

import argparse
import bisect
import collections
import csv
import json
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))
from meter_locator_cv import MeterContourLocator  # noqa: E402

DUMP = r"D:\NexusVision\framedump\session_20260912_201355"
OUT = os.path.join("logs", "diagnostics", "farshot_study")

# --- the ladder --------------------------------------------------------------
# Each entry is (name, mutator). Mutators are applied CUMULATIVELY down the list,
# so the first rung whose locator returns a box names the gate that blocked it.
def _ship(l):
    pass


def _gate(l):
    l._gate_top = 0.0                      # acceptance gate only (scan unchanged: starts 0.08H)


def _scan(l):
    l._band_top = 0.12                     # scan from row 0 (by0 = (band_top-0.12)*H)
    l._gate_top = 0.0


def _size(l):
    l._w_min_f, l._w_max_f = 0.004, 0.08
    l._h_min_f, l._h_max_f = 0.02, 0.60


def _shape(l):
    l.shape_gate = False


def _corrob(l):
    l.tip_corroborate = False


def _lone(l):
    l.lone_gap = 0.0


def _tip(l):
    l.tip_px_min = 1
    l.tip_tol = 14.0
    l.tip_dx_max = 10.0
    l.tip_gap = 100.0


def _tipgap(l):
    # allow the tip prior to sit anywhere from 40..130 px above the white bottom by
    # dropping the fixed prior: accept the meter-tall fallback for any run >= 30 px
    l.tip_tol = 60.0


def _white(l):
    l.v_min = 200
    l.spread_max = 40
    l.min_support = 4
    l.col_w_min = 5.0
    l.col_w_max = 30.0


RUNGS = [
    ("ship", _ship),
    ("gate_top0", _gate),
    ("scan_top0", _scan),
    ("size_wide", _size),
    ("shape_off", _shape),
    ("corrob_off", _corrob),
    ("lone_off", _lone),
    ("tip_loose", _tip),
    ("tipgap_loose", _tipgap),
    ("white_loose", _white),
]


def build(upto: int) -> MeterContourLocator:
    loc = MeterContourLocator()
    for i in range(upto + 1):
        RUNGS[i][1](loc)
    return loc


def frame_index():
    names = os.listdir(DUMP)
    idx = {}
    for n in names:
        if n.endswith("_raw.png") and n.startswith("f"):
            try:
                idx[int(n[1:6])] = os.path.join(DUMP, n)
            except ValueError:
                pass
    return idx


def load_frames_csv():
    rows = []
    with open(os.path.join(DUMP, "frames.csv"), newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--presses", choices=("miss", "hit", "all", "none"), default="miss")
    ap.add_argument("--shots", choices=("blind", "seen", "all", "none"), default="none")
    ap.add_argument("--range", nargs=2, type=int, default=None)
    ap.add_argument("--win-s", type=float, default=1.5)
    ap.add_argument("--pre-s", type=float, default=0.15)
    ap.add_argument("--rungs", default="")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    rows = load_frames_csv()
    walls = [float(r["t_wall"]) for r in rows]
    files = frame_index()

    # ---- pick the frames to probe
    want = []            # (idx, label)
    if args.range:
        for i in range(args.range[0], args.range[1]):
            want.append((i, "pop"))
    if args.presses != "none":
        preses = list(csv.DictReader(open(os.path.join(OUT, "presses.csv"))))
        for p in preses:
            miss = (p["kind"] == "NOT_OWNED" and p["n_detected"] == "0")
            if args.presses == "miss" and not miss:
                continue
            if args.presses == "hit" and miss:
                continue
            pt = float(p["press_wall"])
            i0 = bisect.bisect_left(walls, pt - args.pre_s)
            i1 = bisect.bisect_right(walls, pt + args.win_s)
            for i in range(i0, i1):
                want.append((int(rows[i]["idx"]), f"ep{p['epoch']}"))
    if args.shots != "none":
        G = [r for r in csv.DictReader(open(os.path.join(DUMP, "panel_grade.csv"))) if r["word"]]
        for k, g in enumerate(G):
            t0 = float(g["t0"])
            i0 = bisect.bisect_left(walls, t0 - 2.6)
            i1 = bisect.bisect_right(walls, t0 - 0.1)
            win = rows[i0:i1]
            ndet = sum(1 for r in win if r["detected"] == "1")
            if args.shots == "blind" and ndet:
                continue
            if args.shots == "seen" and not ndet:
                continue
            for r in win:
                want.append((int(r["idx"]), f"sh{k}"))
    seen = set()
    order = []
    for i, lab in want:
        if i in seen:
            continue
        seen.add(i)
        order.append((i, lab))
    order.sort()
    print(f"probing {len(order)} frames")

    names = [n for n, _ in RUNGS]
    if args.rungs:
        names = [n for n in args.rungs.split(",") if n in names]
    locs = {n: build(names_i) for names_i, n in enumerate([r[0] for r in RUNGS]) if n in names}

    recs = []
    t_start = time.perf_counter()
    prev_stats = {n: dict(locs[n].stats) for n in names}
    tmr = {n: [] for n in names}
    for k, (i, lab) in enumerate(order):
        path = files.get(i)
        if path is None:
            continue
        img = cv2.imread(path)
        if img is None:
            continue
        ts = walls[i] if i < len(walls) else float(i) / 6.7
        rec = {"idx": i, "label": lab, "t_wall": f"{ts:.3f}",
               "prod_detected": rows[i]["detected"], "prod_rej": rows[i]["rejection"]}
        for n in names:
            loc = locs[n]
            t0 = time.perf_counter()
            b = loc.detect_box(img, ts=ts)
            tmr[n].append((time.perf_counter() - t0) * 1000.0)
            rec[n] = "" if b is None else f"{b[0]},{b[1]},{b[2]},{b[3]},{b[4]:.2f}"
            d = {kk: loc.stats[kk] - prev_stats[n].get(kk, 0) for kk in loc.stats
                 if loc.stats[kk] - prev_stats[n].get(kk, 0)}
            prev_stats[n] = dict(loc.stats)
            rec[n + "_why"] = ";".join(f"{kk}={vv}" for kk, vv in sorted(d.items())
                                       if kk not in ("calls", "full", "roi"))
        recs.append(rec)
        if (k + 1) % 200 == 0:
            print(f"  {k+1}/{len(order)}  {time.perf_counter()-t_start:.0f}s", flush=True)

    tag = args.tag or args.presses
    path = os.path.join(OUT, f"probe_{tag}.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(recs[0].keys()))
        w.writeheader()
        w.writerows(recs)
    print("wrote", path)

    # ---- per-rung found counts + timing
    print("\nrung                found   p50ms   p90ms   maxms")
    for n in names:
        found = sum(1 for r in recs if r[n])
        t = sorted(tmr[n])
        print(f"{n:18s} {found:6d}  {t[len(t)//2]:6.2f}  {t[int(len(t)*0.9)]:6.2f}  {t[-1]:6.2f}")

    # ---- first rung at which each frame is found
    print("\nfirst rung that finds a box (frames found at all):")
    c = collections.Counter()
    for r in recs:
        first = next((n for n in names if r[n]), "NEVER")
        c[first] += 1
    for n in names + ["NEVER"]:
        if c[n]:
            print(f"  {c[n]:5d}  {n}")


if __name__ == "__main__":
    sys.exit(main())
