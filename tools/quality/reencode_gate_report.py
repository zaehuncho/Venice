#!/usr/bin/env python
"""reencode_gate_report.py -- turn reencode_gate_study's per_frame.csv into the answer.

Reads   logs/diagnostics/reencode_study/per_frame.csv
Writes  logs/diagnostics/reencode_study/summary.csv, metrics.csv, report.txt

GROUND TRUTH is defined from the PRISTINE measurement itself, not from the production
`detected` flag: a frame is a GT meter frame only if the shape probe at the production box
finds a white column on the pristine image AND that column passes every gate-9 threshold
(h >= 14, row-width CV <= 0.35, both edge sd <= 1.2, solidity >= 0.75) with a confirmed green
tip (>= 3 px). That refuses the production false locks the gate was built to kill, so
"survives compression" is measured on meters that were unambiguously there.
"""
from __future__ import annotations

import csv
import os
import sys

import numpy as np

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT = os.path.join(_REPO, "logs", "diagnostics", "reencode_study")

# Shipped thresholds (meter_locator_cv.py, @720p -> s = 1.0)
TH = dict(shape_min_h=14.0, rw_cv_max=0.35, edge_sd_max=1.2, sol_min=0.75,
          tip_px_min=3, spill_frac=0.25, spill_px=4.0, col_w_min=8.0, col_w_max=22.0)


def f(v, d=None):
    try:
        return float(v)
    except Exception:
        return d


def pstat(vals):
    if not vals:
        return dict(n=0)
    a = np.asarray(vals, float)
    return dict(n=int(a.size), p50=float(np.median(a)), p90=float(np.percentile(a, 90)),
                p99=float(np.percentile(a, 99)), mx=float(a.max()))


def main():
    rows = list(csv.DictReader(open(os.path.join(OUT, "per_frame.csv"),
                                    newline="", encoding="utf-8")))
    by = {}
    for r in rows:
        by.setdefault((r["cond"], r["sample"]), {})[int(r["idx"])] = r
    keys = list(by)
    pris = by[("pristine", "pristine")]
    idxs = sorted(pris)

    # ---------------- ground truth
    GT = []
    for i in idxs:
        r = pris[i]
        if r.get("m_found") != "1":
            continue
        h = f(r.get("m_h"), 0)
        rw = f(r.get("m_rw_cv"), 9)
        esd = max(f(r.get("m_edge_sd_l"), 9), f(r.get("m_edge_sd_r"), 9))
        sol = f(r.get("m_sol"), 0)
        tip = f(r.get("m_tip_px"), 0)
        if (h >= TH["shape_min_h"] and rw <= TH["rw_cv_max"] and esd <= TH["edge_sd_max"]
                and sol >= TH["sol_min"] and tip >= TH["tip_px_min"]):
            GT.append(i)
    GTs = set(GT)
    # NEG = pristine locator saw nothing AND no GT meter is present
    NEG = [i for i in idxs if pris[i]["found"] == "0" and i not in GTs]
    # pristine locator box per idx (for co-location)
    pbox = {i: (f(pris[i]["bx"]), f(pris[i]["by"]), f(pris[i]["bw"]), f(pris[i]["bh"]))
            for i in idxs if pris[i]["found"] == "1"}
    GT_locked = [i for i in GT if i in pbox]

    lines = []
    ap = lines.append
    ap("re-encode gate study -- session_20260912_201355")
    ap("frames replayed: %d   GT meter frames: %d   pristine-negative frames: %d"
       % (len(idxs), len(GT), len(NEG)))
    ap("GT frames the pristine locator also locked: %d" % len(GT_locked))
    ap("")

    # ---------------- per (cond,sample) summary
    summ = []
    order = [("pristine", "pristine"), ("pristine", "pristine_fs")]
    for c in ("control_420_qp1", "balanced_720p_4M", "performance_720p_12M",
              "quality_1080p_12M"):
        for s in ("head", "head_fs", "tail", "tail_fs"):
            if (c, s) in by:
                order.append((c, s))
    for k in order:
        if k not in by:
            continue
        D = by[k]
        gt_found = sum(1 for i in GT if D.get(i, {}).get("found") == "1")
        # co-located with the pristine locator box
        colo = disp = 0
        for i in GT_locked:
            r = D.get(i)
            if not r or r["found"] != "1":
                continue
            cx = f(r["bx"]) + f(r["bw"]) / 2
            cy = f(r["by"]) + f(r["bh"]) / 2
            px = pbox[i][0] + pbox[i][2] / 2
            py = pbox[i][1] + pbox[i][3] / 2
            if abs(cx - px) <= 25 and abs(cy - py) <= 25:
                colo += 1
            else:
                disp += 1
        fl = sum(1 for i in NEG if D.get(i, {}).get("found") == "1")
        cands = np.mean([int(D[i]["cands"]) for i in idxs if i in D]) if D else 0
        def s_(field, sub):
            return sum(int(D[i][field]) for i in sub if i in D)
        # white column dissolved entirely (gates 1-3 found nothing at the GT box)
        dissolved = sum(1 for i in GT if D.get(i, {}).get("m_found") == "0")
        rec = dict(cond=k[0], sample=k[1],
                   gt_n=len(GT), gt_found=gt_found,
                   gt_found_pct=round(100.0 * gt_found / max(1, len(GT)), 1),
                   colocated=colo, displaced=disp,
                   colo_pct=round(100.0 * colo / max(1, len(GT_locked)), 1),
                   neg_n=len(NEG), false_locks=fl,
                   false_lock_pct=round(100.0 * fl / max(1, len(NEG)), 2),
                   cands_per_frame=round(float(cands), 2),
                   col_dissolved=dissolved,
                   col_dissolved_pct=round(100.0 * dissolved / max(1, len(GT)), 1),
                   gt_no_tip=s_("no_tip", GT), gt_not_lone=s_("not_lone", GT),
                   gt_shape_short=s_("shape_short", GT),
                   gt_shape_irregular=s_("shape_irregular", GT),
                   gt_tip_spill=s_("tip_spill", GT),
                   gt_no_tip_lone=s_("no_tip_lone", GT),
                   gt_shape_bridged=s_("shape_bridged", GT),
                   neg_no_tip=s_("no_tip", NEG), neg_not_lone=s_("not_lone", NEG),
                   neg_shape_short=s_("shape_short", NEG),
                   neg_shape_irregular=s_("shape_irregular", NEG),
                   neg_tip_spill=s_("tip_spill", NEG))
        summ.append(rec)

    with open(os.path.join(OUT, "summary.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(summ[0]))
        w.writeheader()
        w.writerows(summ)

    ap("=== LOCATOR: lock / false-lock (GT = %d frames, NEG = %d frames) ===" % (len(GT), len(NEG)))
    ap("%-24s %-10s %7s %7s %8s %8s %8s %7s" %
       ("condition", "sample", "lock%", "colo%", "displ", "FL", "FL%", "cand/f"))
    for r in summ:
        ap("%-24s %-10s %7.1f %7.1f %8d %8d %8.2f %7.2f" %
           (r["cond"], r["sample"], r["gt_found_pct"], r["colo_pct"], r["displaced"],
            r["false_locks"], r["false_lock_pct"], r["cands_per_frame"]))
    ap("")
    ap("=== gate REJECTIONS charged on GT (true-meter) frames, per 100 frames ===")
    ap("%-24s %-10s %8s %8s %8s %9s %8s %8s" %
       ("condition", "sample", "no_tip", "notlone", "shrt", "irregular", "spill", "bridged"))
    for r in summ:
        n = max(1, r["gt_n"]) / 100.0
        ap("%-24s %-10s %8.1f %8.1f %8.1f %9.1f %8.1f %8.1f" %
           (r["cond"], r["sample"], r["gt_no_tip"] / n, r["gt_not_lone"] / n,
            r["gt_shape_short"] / n, r["gt_shape_irregular"] / n, r["gt_tip_spill"] / n,
            r["gt_shape_bridged"] / n))
    ap("")
    ap("=== white column DISSOLVED at the GT box (gates 1-3 found no 8-22 px column) ===")
    for r in summ:
        ap("  %-24s %-10s %5d / %d  (%.1f%%)" % (r["cond"], r["sample"], r["col_dissolved"],
                                                 r["gt_n"], r["col_dissolved_pct"]))
    ap("")

    # ---------------- metric distributions on GT frames
    METR = [("m_rw_cv", "row-width CV", TH["rw_cv_max"], "max"),
            ("edge_sd", "edge sd (px, worse side)", TH["edge_sd_max"], "max"),
            ("m_sol", "solidity", TH["sol_min"], "min"),
            ("m_tip_px", "green tip px", TH["tip_px_min"], "min"),
            ("m_w", "column width px", TH["col_w_min"], "min"),
            ("m_spill_ratio", "tip spill ratio", TH["spill_frac"], "max"),
            ("m_fv_p10", "gate1 fill max(BGR) p10", 225.0, "min"),
            ("m_fs_p90", "gate1 fill spread p90", 25.0, "max"),
            ("m_tip_s_p50", "tip HSV sat (p50 of top quartile)", 90.0, "min"),
            ("m_tip_v_p50", "tip HSV val (p50 of top quartile)", 90.0, "min"),
            ("m_tip_h_p50", "tip HSV hue (p50 of top quartile)", 38.0, "min")]
    mrows = []
    ap("=== gate-9 METRICS measured at the GT meter box (only frames where the column")
    ap("    survived gates 1-3 in that condition; thresholds in [] ) ===")
    for k in order:
        D = by[k]
        vals = {}
        for i in GT:
            r = D.get(i)
            if not r or r.get("m_found") != "1":
                continue
            vals.setdefault("m_rw_cv", []).append(f(r["m_rw_cv"], 0))
            vals.setdefault("edge_sd", []).append(max(f(r["m_edge_sd_l"], 0), f(r["m_edge_sd_r"], 0)))
            vals.setdefault("m_sol", []).append(f(r["m_sol"], 0))
            vals.setdefault("m_tip_px", []).append(f(r["m_tip_px"], 0))
            vals.setdefault("m_w", []).append(f(r["m_w"], 0))
            sp = f(r.get("m_spill_ratio"), None)
            if sp is not None:
                vals.setdefault("m_spill_ratio", []).append(sp)
            for extra in ("m_fv_p10", "m_fs_p90", "m_tip_s_p50", "m_tip_v_p50", "m_tip_h_p50"):
                v = f(r.get(extra), None)
                if v is not None:
                    vals.setdefault(extra, []).append(v)
        ap("--- %s / %s  (n=%d of %d GT)" % (k[0], k[1], len(vals.get("m_rw_cv", [])), len(GT)))
        for key, name, thr, side in METR:
            v = vals.get(key, [])
            if not v:
                continue
            st = pstat(v)
            a = np.asarray(v, float)
            fail = float((a > thr).mean() if side == "max" else (a < thr).mean()) * 100.0
            ap("    %-34s p50 %7.2f  p90 %7.2f  p99 %7.2f  worst %7.2f   fails[%s%s] %5.1f%%"
               % (name, st["p50"], st["p90"], st["p99"],
                  st["mx"] if side == "max" else float(np.min(a)),
                  "<=" if side == "max" else ">=", thr, fail))
            mrows.append(dict(cond=k[0], sample=k[1], metric=name, thr=thr, side=side,
                              n=st["n"], p50=round(st["p50"], 4), p90=round(st["p90"], 4),
                              p99=round(st["p99"], 4), mx=round(st["mx"], 4),
                              fail_pct=round(fail, 2)))
        ap("")
    with open(os.path.join(OUT, "metrics.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(mrows[0]))
        w.writeheader()
        w.writerows(mrows)

    # ---------------- WHY a meter was lost
    ap("=== LOST METERS: GT frames the PRISTINE locator found and this condition did not.")
    ap("    Counters are the locator's own, charged on exactly those frames (per-candidate,")
    ap("    so they can exceed the frame count). Compared against its own baseline sample. ===")
    CNT = ["no_tip", "not_lone", "no_tip_lone", "shape_short", "shape_irregular", "tip_spill"]
    ap("%-22s %-10s %6s %6s " % ("condition", "sample", "lost", "kept") +
       " ".join("%12s" % c for c in CNT) + "   %9s %9s %9s" % ("tip_px_p50", "rwcv_p90", "esd_p90"))
    lost_rows = []
    for k in order:
        D = by[k]
        base = pris if not k[1].endswith("_fs") else by[("pristine", "pristine_fs")]
        lost = [i for i in GT
                if base.get(i, {}).get("found") == "1" and D.get(i, {}).get("found") != "1"]
        kept = [i for i in GT
                if base.get(i, {}).get("found") == "1" and D.get(i, {}).get("found") == "1"]
        tot = {c: sum(int(D[i][c]) for i in lost if i in D) for c in CNT}
        tips = [f(D[i].get("m_tip_px"), None) for i in lost if i in D]
        tips = [v for v in tips if v is not None]
        rw = [f(D[i].get("m_rw_cv"), None) for i in lost if i in D]
        rw = [v for v in rw if v is not None]
        es = [max(f(D[i].get("m_edge_sd_l"), 0), f(D[i].get("m_edge_sd_r"), 0))
              for i in lost if i in D and D[i].get("m_found") == "1"]
        ap("%-22s %-10s %6d %6d " % (k[0], k[1], len(lost), len(kept)) +
           " ".join("%12d" % tot[c] for c in CNT) +
           "   %9.1f %9.3f %9.3f" % (float(np.median(tips)) if tips else -1,
                                     float(np.percentile(rw, 90)) if rw else -1,
                                     float(np.percentile(es, 90)) if es else -1))
        lost_rows.append(dict(cond=k[0], sample=k[1], lost=len(lost), kept=len(kept), **tot))
    with open(os.path.join(OUT, "lost.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(lost_rows[0]))
        w.writeheader()
        w.writerows(lost_rows)
    ap("")

    # ---------------- binding constraints: passed pristine, fails here
    ap("=== BINDING CONSTRAINT: GT frames that PASS the threshold on pristine pixels and")
    ap("    FAIL it in the condition (the gate that actually flips). 'gone' = the white")
    ap("    column stopped existing (gates 1-3), which no gate-9 margin can rescue. ===")

    def val(r, key):
        if key == "edge_sd":
            return max(f(r.get("m_edge_sd_l"), 0), f(r.get("m_edge_sd_r"), 0))
        return f(r.get(key), None)

    flips = []
    ap("%-22s %-9s %6s " % ("condition", "sample", "gone") +
       " ".join("%9s" % n for _, n, _, _ in
                [(a, b.split()[0][:9], c, d) for a, b, c, d in METR]))
    for k in order:
        D = by[k]
        gone = 0
        cnt = {m[0]: 0 for m in METR}
        for i in GT:
            r = D.get(i)
            if not r:
                continue
            if r.get("m_found") != "1":
                gone += 1
                continue
            for key, name, thr, side in METR:
                a = val(pris[i], key)
                b = val(r, key)
                if a is None or b is None:
                    continue
                pass_p = (a <= thr) if side == "max" else (a >= thr)
                pass_r = (b <= thr) if side == "max" else (b >= thr)
                if pass_p and not pass_r:
                    cnt[key] += 1
        ap("%-22s %-9s %6d " % (k[0], k[1], gone) +
           " ".join("%9d" % cnt[m[0]] for m in METR))
        flips.append(dict(cond=k[0], sample=k[1], gt_n=len(GT), column_gone=gone,
                          **{m[0]: cnt[m[0]] for m in METR}))
    with open(os.path.join(OUT, "flips.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(flips[0]))
        w.writeheader()
        w.writerows(flips)
    ap("")

    # ---------------- reader agreement
    ap("=== READER (SimpleMeterReader, ORION_METER_PROPOSER=cv) vs pristine ===")
    pr_fill = {i: f(pris[i].get("fill"), None) for i in idxs}
    pr_det = {i: pris[i].get("rdet") == "1" for i in idxs}
    ap("  pristine reader detections: %d frames (%d of them GT)"
       % (sum(pr_det.values()), sum(1 for i in GT if pr_det.get(i))))
    for k in order:
        if k[1] != "tail":
            continue
        D = by[k]
        both, ponly, ronly, derr = 0, 0, 0, []
        for i in idxs:
            rd = D.get(i, {}).get("rdet") == "1"
            if pr_det.get(i) and rd:
                both += 1
                a, b = pr_fill.get(i), f(D[i].get("fill"), None)
                if a is not None and b is not None:
                    derr.append(abs(a - b))
            elif pr_det.get(i):
                ponly += 1
            elif rd:
                ronly += 1
        st = pstat(derr)
        ap("  %-24s both=%d pristine-only=%d rung-only=%d  recall=%.1f%%  fill|d| pp p50=%.2f p90=%.2f max=%.2f"
           % (k[0], both, ponly, ronly, 100.0 * both / max(1, both + ponly),
              st.get("p50", 0), st.get("p90", 0), st.get("mx", 0)))
    ap("")

    # tip-frame timing: the first frame in each GT run whose reader fill crosses 85 pp.
    # The framedump cadence is ~150 ms, so one frame of difference IS ~150 ms -- this is a
    # coarse instrument and only a nonzero median would be meaningful.
    runs = []
    cur = []
    for i in GT:
        if cur and i == cur[-1] + 1:
            cur.append(i)
        else:
            if len(cur) >= 3:
                runs.append(cur)
            cur = [i]
    if len(cur) >= 3:
        runs.append(cur)

    def cross(D, run, thr=85.0):
        for i in run:
            r = D.get(i)
            if r and r.get("rdet") == "1" and f(r.get("fill"), 0) >= thr:
                return i
        return None

    ap("=== READER tip-frame timing (first frame with fill >= 85 pp) over %d GT runs ===" % len(runs))
    ap("    one framedump frame = ~150 ms, so this resolves nothing finer than that")
    for k in order:
        if k[1] != "tail":
            continue
        D = by[k]
        d = []
        miss = 0
        for run in runs:
            a, b = cross(pris, run), cross(D, run)
            if a is None:
                continue
            if b is None:
                miss += 1
                continue
            d.append(b - a)
        ap("  %-24s runs with a pristine crossing=%d  no crossing in rung=%d  "
           "delta frames: %s" % (k[0], len(d) + miss, miss,
                                 ("median %+.1f, range %+d..%+d" %
                                  (float(np.median(d)), min(d), max(d))) if d else "n/a"))
    ap("")

    txt = "\n".join(lines)
    with open(os.path.join(OUT, "report.txt"), "w", encoding="utf-8") as fh:
        fh.write(txt)
    print(txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
