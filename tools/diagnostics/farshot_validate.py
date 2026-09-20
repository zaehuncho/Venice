"""farshot_validate.py -- offline before/after for ORION_METER_TOP_STRIP.

Three measurements, all read-only, all on real pixels from
D:\\NexusVision\\framedump\\session_20260912_201355:

  recover    Take frames that carry a CONFIRMED real meter and translate the picture UP by
             K px, so the meter rides to the top of the screen exactly as it does on a far
             shot. Report, per K, whether the shipped locator and the knob-on locator still
             acquire it (and at the right place).
  regress    Replay N frames in order through both configurations and diff the boxes.
  timing     Per-frame cost of both configurations, split into idle frames (no box) and
             armed frames (a box on this frame or the one before).

Usage:
  .venv/Scripts/python.exe tools/diagnostics/farshot_validate.py recover
  .venv/Scripts/python.exe tools/diagnostics/farshot_validate.py regress --n 6000
"""
from __future__ import annotations

import argparse
import collections
import csv
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))
from meter_locator_cv import MeterContourLocator  # noqa: E402

DUMP = r"D:\NexusVision\framedump\session_20260912_201355"
OUT = os.path.join("logs", "diagnostics", "farshot_study")


def frame_index():
    idx = {}
    for n in os.listdir(DUMP):
        if n.startswith("f") and n.endswith("_raw.png"):
            try:
                idx[int(n[1:6])] = os.path.join(DUMP, n)
            except ValueError:
                pass
    return idx


def rows_csv():
    return list(csv.DictReader(open(os.path.join(DUMP, "frames.csv"))))


def make(on: bool) -> MeterContourLocator:
    loc = MeterContourLocator()
    loc.top_strip = bool(on)
    return loc


def shift_up(img, k):
    """Translate the picture up by k px (the meter rides with it); bottom goes black."""
    if k <= 0:
        return img
    out = np.zeros_like(img)
    out[: img.shape[0] - k] = img[k:]
    return out


def cmd_recover(args):
    F = rows_csv()
    files = frame_index()
    # a CONFIRMED real meter: the production reader locked a 26-29 x 116-124 box at conf 1.0
    # with a live fill -- the geometry the landmark model was measured on.
    cand = [r for r in F if r["detected"] == "1" and float(r["conf"]) >= 0.99
            and 26 <= int(r["bbox_w"]) <= 29 and 116 <= int(r["bbox_h"]) <= 124
            and 15.0 <= float(r["fill_pct"]) <= 88.0]
    cand = cand[:: max(1, len(cand) // args.n)][: args.n]
    print(f"{len(cand)} confirmed-meter frames")

    off, on = make(False), make(True)
    # translate each frame so the meter's box TOP lands on a chosen row: that is the axis the
    # scan band censors, so it reads straight off as an acquisition floor.
    targets = [int(v) for v in args.shifts.split(",")]
    tab = {k: collections.Counter() for k in targets}
    examples = collections.defaultdict(list)
    for r in cand:
        i = int(r["idx"])
        p = files.get(i)
        if not p:
            continue
        img = cv2.imread(p)
        if img is None:
            continue
        bx, by = int(r["bbox_x"]), int(r["bbox_y"])
        for k in targets:
            if by - k < 0:
                continue
            sh = shift_up(img, by - k)
            want = (bx, k)
            for name, loc in (("off", off), ("on", on)):
                loc.reset()
                b = loc.detect_box(sh, ts=1000.0 + i)
                hit = (b is not None and abs(b[0] - want[0]) <= 10
                       and abs(b[1] - want[1]) <= 10)
                tab[k][name + ("_hit" if hit else ("_other" if b else "_miss"))] += 1
                if not hit and name == "on" and k and len(examples[k]) < 5:
                    examples[k].append((i, want, b))

    print("\nbox-top row    shipped hit/other/miss      TOP_STRIP=1 hit/other/miss")
    for k in targets:
        c = tab[k]
        n = c["off_hit"] + c["off_other"] + c["off_miss"]
        e1 = "  ".join(f"{c[x]:4d}" for x in ("off_hit", "off_other", "off_miss"))
        e2 = "  ".join(f"{c[x]:4d}" for x in ("on_hit", "on_other", "on_miss"))
        print(f"  y={k:4d} (n={n:3d})     {e1}            {e2}")
    for k in targets:
        if examples[k]:
            print(f"  knob-on still missing at shift {k}: {examples[k][:3]}")


def cmd_regress(args):
    F = rows_csv()
    files = frame_index()
    off, on = make(False), make(True)
    diffs = []
    n_off = n_on = 0
    t_off, t_on = [], []
    lo, hi = args.start, args.start + args.n
    for r in F[lo:hi]:
        i = int(r["idx"])
        p = files.get(i)
        if not p:
            continue
        img = cv2.imread(p)
        if img is None:
            continue
        ts = float(r["t_wall"])
        t0 = time.perf_counter(); a = off.detect_box(img, ts=ts); t_off.append((time.perf_counter() - t0) * 1e3)
        t0 = time.perf_counter(); b = on.detect_box(img, ts=ts); t_on.append((time.perf_counter() - t0) * 1e3)
        n_off += a is not None
        n_on += b is not None
        if a != b:
            diffs.append((i, a, b))
        if (i - lo + 1) % 1000 == 0:
            print(f"  {i - lo + 1}/{args.n}", flush=True)
    print(f"\nframes replayed        : {len(t_off)}")
    print(f"boxes  shipped (off)   : {n_off}")
    print(f"boxes  ORION_METER_TOP_STRIP=1 : {n_on}")
    print(f"frames where they differ       : {len(diffs)}")
    for d in diffs[:20]:
        print("   ", d)
    for name, t in (("off", t_off), ("on", t_on)):
        t = sorted(t)
        print(f"  {name}: p50 {t[len(t)//2]:.2f} ms  p90 {t[int(len(t)*0.9)]:.2f}  "
              f"p99 {t[int(len(t)*0.99)]:.2f}  max {t[-1]:.2f}")
    # idle vs armed split (armed == production had a box on this frame)
    armed = [k for k, r in enumerate(F[lo:hi]) if r["detected"] == "1"]
    aset = set(armed)
    for name, t in (("off", t_off), ("on", t_on)):
        idle = sorted(v for k, v in enumerate(t) if k not in aset)
        arm = sorted(v for k, v in enumerate(t) if k in aset)
        if idle:
            print(f"  {name} idle  n={len(idle):5d} p50 {idle[len(idle)//2]:.2f} p90 "
                  f"{idle[int(len(idle)*0.9)]:.2f} max {idle[-1]:.2f}")
        if arm:
            print(f"  {name} armed n={len(arm):5d} p50 {arm[len(arm)//2]:.2f} p90 "
                  f"{arm[int(len(arm)*0.9)]:.2f} max {arm[-1]:.2f}")
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "regress.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["idx", "off", "on"])
        w.writerows(diffs)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("recover")
    r.add_argument("--n", type=int, default=60)
    r.add_argument("--shifts", default="300,200,160,120,90,70,58,40,20,8")
    g = sub.add_parser("regress")
    g.add_argument("--n", type=int, default=6000)
    g.add_argument("--start", type=int, default=0)
    args = ap.parse_args()
    return cmd_recover(args) if args.cmd == "recover" else cmd_regress(args)


if __name__ == "__main__":
    sys.exit(main())
