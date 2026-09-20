#!/usr/bin/env python
"""reencode_gate_sweep.py -- measure candidate DECODER-ROUTE margins, do not guess them.

reencode_gate_study.py showed the only gate that moves under H.264 is the GREEN TIP
confirmation (gate 5): TIP_PX_MIN = 3 px of HSV-green in a +-5-row / +-6-col window. This
script re-runs the shipped locator over the same decoded frames with candidate relaxations
and reports, for each, what it BUYS (first-sight lock on true meters) and what it COSTS
(locks on frames the pristine locator refused -- the false-lock budget the shape gate exists
to protect).

Every variant is applied to a locator INSTANCE, never to the module: capture-card behaviour
is untouched by construction. First-sight mode (reset before every frame) is used throughout
because the bridged/tracking path already survives compression at 99-100%.

Usage:  .venv/Scripts/python.exe tools/quality/reencode_gate_sweep.py [--rungs a,b]
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time

import numpy as np
import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reencode_gate_study as S                                     # noqa: E402

sys.path.insert(0, S._REPO)
import meter_locator_cv as mlc                                      # noqa: E402
from remote_play_orchestrator import _normalize_detector_frame      # noqa: E402

OUT = S.OUT_DIR

# name -> instance attribute overrides (None value = leave shipped)
VARIANTS = [
    ("shipped", {}),
    ("tip_px_min=2", dict(tip_px_min=2)),
    ("tip_px_min=1", dict(tip_px_min=1)),
    ("green_S>=60 only", dict(_GREEN_LO=(38, 60, 90))),
    ("green_V>=60 only", dict(_GREEN_LO=(38, 90, 60))),
    ("green_sv>=60", dict(_GREEN_LO=(38, 60, 60))),
    ("green_sv>=45", dict(_GREEN_LO=(38, 45, 45))),
    ("tip_win 7x8", dict(tip_tol=7.0, tip_dx_max=8.0)),
    ("px2+sv60", dict(tip_px_min=2, _GREEN_LO=(38, 60, 60))),
    ("corroborate=off", dict(tip_corroborate=False)),
    ("px2+sv60+corrOFF", dict(tip_px_min=2, _GREEN_LO=(38, 60, 60), tip_corroborate=False)),
]


def build(overrides):
    L = mlc.MeterContourLocator()
    for k, v in overrides.items():
        setattr(L, k, v)
    return L


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rungs", default="balanced_720p_4M,performance_720p_12M,quality_1080p_12M")
    ap.add_argument("--dup", type=int, default=9)
    ap.add_argument("--samples", default="head,tail")
    ap.add_argument("--pristine", action="store_true")
    args = ap.parse_args()

    rows = S._read_frames_csv(os.path.join(S.SRC_DIR, "frames.csv"))
    blocks, true_runs, _ = S.pick_blocks(rows)
    order = [i for s, e in blocks for i in range(s, e + 1)]
    ts_by_idx = {int(r["idx"]): float(r["t_ms"]) / 1000.0 for r in rows}

    # GT / NEG from the study's own per-frame table (pristine column)
    pf = list(csv.DictReader(open(os.path.join(OUT, "per_frame.csv"), newline="",
                                  encoding="utf-8")))
    pris = {int(r["idx"]): r for r in pf if r["cond"] == "pristine" and r["sample"] == "pristine"}

    def f(v, d=None):
        try:
            return float(v)
        except Exception:
            return d

    GT, NEG = [], []
    for i, r in pris.items():
        ok = (r.get("m_found") == "1" and f(r.get("m_h"), 0) >= 14
              and f(r.get("m_rw_cv"), 9) <= 0.35
              and max(f(r.get("m_edge_sd_l"), 9), f(r.get("m_edge_sd_r"), 9)) <= 1.2
              and f(r.get("m_sol"), 0) >= 0.75 and f(r.get("m_tip_px"), 0) >= 3)
        if ok:
            GT.append(i)
        elif r["found"] == "0":
            NEG.append(i)
    GTs, NEGs = set(GT), set(NEG)
    pbox = {i: (f(pris[i]["bx"]), f(pris[i]["by"]), f(pris[i]["bw"]), f(pris[i]["bh"]))
            for i in pris if pris[i]["found"] == "1"}
    # split the GT by reader fill: acquisition (rising) vs the washed-out plateau
    RISE = [i for i in GT if 0 <= f(pris[i].get("fill"), -1) < 70]
    PLAT = [i for i in GT if f(pris[i].get("fill"), -1) >= 85]

    if args.pristine:
        locs = {("pristine", vn): build(ov) for vn, ov in VARIANTS}
        hit = {k: {"gt": 0, "rise": 0, "plat": 0, "neg": 0, "off": 0} for k in locs}
        for idx in order:
            if idx not in GTs and idx not in NEGs:
                continue
            im = cv2.imread(os.path.join(S.STAGE_DIR, "f%05d.png" % idx))
            frame, _ = _normalize_detector_frame(im)
            if frame is None:
                continue
            t = ts_by_idx.get(idx, idx / 60.0)
            for vn, _ov in VARIANTS:
                L = locs[("pristine", vn)]
                L.reset()
                box = L.detect_box(frame, ts=t)
                if box is None:
                    continue
                H = hit[("pristine", vn)]
                if idx in GTs:
                    H["gt"] += 1
                    if idx in RISE:
                        H["rise"] += 1
                    if idx in PLAT:
                        H["plat"] += 1
                    p = pbox.get(idx)
                    if p and (abs(box[0] + box[2] / 2 - (p[0] + p[2] / 2)) > 25
                              or abs(box[1] + box[3] / 2 - (p[1] + p[3] / 2)) > 25):
                        H["off"] += 1
                else:
                    H["neg"] += 1
        out_rows = []
        for (samp, vn), H in hit.items():
            out_rows.append(dict(rung="pristine_png", sample=samp, variant=vn,
                                 gt_n=len(GT), gt_lock=H["gt"],
                                 gt_lock_pct=round(100.0 * H["gt"] / max(1, len(GT)), 1),
                                 rise_n=len(RISE), rise_pct=round(100.0 * H["rise"] / max(1, len(RISE)), 1),
                                 plat_n=len(PLAT), plat_pct=round(100.0 * H["plat"] / max(1, len(PLAT)), 1),
                                 displaced=H["off"], neg_n=len(NEG), false_locks=H["neg"],
                                 false_lock_pct=round(100.0 * H["neg"] / max(1, len(NEG)), 2)))
        _emit(out_rows, GT, RISE, PLAT, NEG, "sweep_pristine")
        return 0

    work = os.path.join(OUT, "_work")
    os.makedirs(work, exist_ok=True)
    out_rows = []
    for rung in [r.strip() for r in args.rungs.split(",") if r.strip()]:
        mp4 = os.path.join(work, rung + ".mp4")
        if not os.path.exists(mp4):
            print("[%s] encoding ..." % rung)
            S.encode(order, rung, args.dup, mp4, work)
        r = S.RUNGS[rung]
        want = {0: "head", args.dup - 1: "tail"}
        want = {p: n for p, n in want.items() if n in args.samples.split(",")}
        locs = {(n, vn): build(ov) for n in want.values() for vn, ov in VARIANTS}
        hit = {k: {"gt": 0, "rise": 0, "plat": 0, "neg": 0, "off": 0} for k in locs}
        print("[%s] sweeping %d variants ..." % (rung, len(VARIANTS)))
        t0 = time.time()
        k = 0
        for fr in S.decode_iter(mp4, r["w"], r["h"]):
            gi, pos = divmod(k, args.dup)
            k += 1
            if gi >= len(order) or pos not in want:
                continue
            idx = order[gi]
            if idx not in GTs and idx not in NEGs:
                continue
            frame, _ = _normalize_detector_frame(fr)
            if frame is None:
                continue
            t = ts_by_idx.get(idx, idx / 60.0)
            for vn, _ov in VARIANTS:
                L = locs[(want[pos], vn)]
                L.reset()                                   # FIRST SIGHT for every frame
                box = L.detect_box(frame, ts=t)
                if box is None:
                    continue
                H = hit[(want[pos], vn)]
                if idx in GTs:
                    H["gt"] += 1
                    if idx in RISE:
                        H["rise"] += 1
                    if idx in PLAT:
                        H["plat"] += 1
                    p = pbox.get(idx)
                    if p and (abs(box[0] + box[2] / 2 - (p[0] + p[2] / 2)) > 25
                              or abs(box[1] + box[3] / 2 - (p[1] + p[3] / 2)) > 25):
                        H["off"] += 1
                else:
                    H["neg"] += 1
        print("  %.0fs" % (time.time() - t0))
        for (samp, vn), H in hit.items():
            out_rows.append(dict(rung=rung, sample=samp, variant=vn,
                                 gt_n=len(GT), gt_lock=H["gt"],
                                 gt_lock_pct=round(100.0 * H["gt"] / max(1, len(GT)), 1),
                                 rise_n=len(RISE), rise_pct=round(100.0 * H["rise"] / max(1, len(RISE)), 1),
                                 plat_n=len(PLAT), plat_pct=round(100.0 * H["plat"] / max(1, len(PLAT)), 1),
                                 displaced=H["off"],
                                 neg_n=len(NEG), false_locks=H["neg"],
                                 false_lock_pct=round(100.0 * H["neg"] / max(1, len(NEG)), 2)))

    _emit(out_rows, GT, RISE, PLAT, NEG, "sweep")
    return 0


def _emit(out_rows, GT, RISE, PLAT, NEG, stem):
    with open(os.path.join(OUT, stem + ".csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0]))
        w.writeheader()
        w.writerows(out_rows)

    lines = ["FIRST-SIGHT knob sweep (locator state reset before every frame).",
             "GT=%d true-meter frames (rising=%d, plateau=%d), NEG=%d pristine-refused frames."
             % (len(GT), len(RISE), len(PLAT), len(NEG)),
             "'FL' = locks on NEG frames; the shipped gate scores 0 there and must stay there.",
             ""]
    lines.append("%-22s %-6s %-18s %7s %7s %7s %6s %5s" %
                 ("rung", "samp", "variant", "GT%", "rise%", "plat%", "displ", "FL"))
    for r in out_rows:
        lines.append("%-22s %-6s %-18s %7.1f %7.1f %7.1f %6d %5d" %
                     (r["rung"], r["sample"], r["variant"], r["gt_lock_pct"], r["rise_pct"],
                      r["plat_pct"], r["displaced"], r["false_locks"]))
    txt = "\n".join(lines)
    with open(os.path.join(OUT, "sweep.txt"), "w", encoding="utf-8") as fh:
        fh.write(txt)
    print("\n" + txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
