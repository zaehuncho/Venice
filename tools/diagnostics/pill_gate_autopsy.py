"""Per-gate autopsy of the shipped CV proposer ON THE PILL'S OWN COLUMN.

pill_locator_replay.py answers "did it propose?".  This answers "where exactly did the
PILL die?", by re-running the shipped `_find` pipeline stage by stage (the same OpenCV
calls, the same constants, read straight off a live MeterContourLocator) and following
only the component that sits on the ground-truth meter, instead of whatever component
happened to win the frame.

Stages, in the order meter_locator_cv._find runs them:
  g1 achromatic-bright mask     V >= ORION_CV_WHITE_V_MIN, spread <= ORION_CV_CHANNEL_SPREAD_MAX
  g2 vertical gap close         ORION_CV_GAP_CLOSE (+2 rows, as shipped)
  g3 vertical support opening   ORION_CV_MIN_SUPPORT
  g4 component width            ORION_CV_COL_W_MIN .. ORION_CV_COL_W_MAX  (+ solidity 0.35)
  g8 lone column                ORION_CV_LONE_GAP_PX
  g5 green tip 100 px @720p above the white bottom
  g9 shape (floor, solidity, row-width spread, edge sd)

READ-ONLY.  Nothing is written except the report (and --sheet).

Usage: python tools/diagnostics/pill_gate_autopsy.py --scale 0.66667 [--limit N]
"""
from __future__ import annotations
import argparse, collections, csv, os, sys

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, REPO)
os.environ.setdefault("ORION_METER_PROPOSER", "cv")

import cv2                                      # noqa: E402
import numpy as np                              # noqa: E402
import meter_locator_cv as mlc                  # noqa: E402

DS = os.path.join(REPO, "datasets", "meter2k27_pill_park")
OUT = os.path.join("D:", os.sep, "NexusVision", "pill_check")


def gt_rows(cls="pos"):
    with open(os.path.join(DS, "audit.csv"), newline="") as f:
        return [r for r in csv.DictReader(f) if r["cls"] == cls]


def autopsy(loc, im, gt, relax=None):
    """Return (stage_reached, detail) for the component sitting on the GT meter."""
    relax = relax or {}
    H, W = im.shape[:2]
    s = H / 720.0
    by0 = int(max(0, (loc._band_top - 0.12) * H))
    by1 = int(min(H, (loc._band_bot + 0.03) * H))
    sub = im[by0:by1, :]
    gx0, gy0, gx1, gy1 = gt
    gcx = 0.5 * (gx0 + gx1)
    gb = gy1 - by0                    # GT bottom in sub coords

    b, g, r = cv2.split(sub)
    mx = cv2.max(cv2.max(b, g), r)
    mn = cv2.min(cv2.min(b, g), r)
    v_min = relax.get("v_min", loc.v_min)
    bright = cv2.threshold(mx, v_min - 1, 255, cv2.THRESH_BINARY)[1]
    spread = cv2.subtract(mx, mn)
    neutral = cv2.threshold(spread, loc.spread_max, 255, cv2.THRESH_BINARY_INV)[1]
    white = cv2.bitwise_and(bright, neutral)
    # does the GT column carry ANY bright pixel at all?
    wx0 = int(max(0, gcx - 16 * s)); wx1 = int(min(sub.shape[1], gcx + 16 * s))
    wy0 = int(max(0, gy0 - by0)); wy1 = int(min(sub.shape[0], gy1 - by0))
    if cv2.countNonZero(white[wy0:wy1, wx0:wx1]) == 0:
        return "g1_no_bright", {}

    gc = max(1, int(round(relax.get("gap_close", loc.gap_close) * s))) + 2
    kc = np.ones((gc, 1), np.uint8)
    white = cv2.erode(cv2.dilate(white, kc, anchor=(0, gc // 2)), kc, anchor=(0, (gc - 1) // 2))
    sup = max(2, int(round(relax.get("min_support", loc.min_support) * s)))
    ks = np.ones((sup, 1), np.uint8)
    col = cv2.dilate(cv2.erode(white, ks, anchor=(0, sup // 2)), ks, anchor=(0, (sup - 1) // 2))
    if cv2.countNonZero(col[wy0:wy1, wx0:wx1]) == 0:
        return "g3_no_support", {}
    n, labels, stats, _ = cv2.connectedComponentsWithStats(col, connectivity=8)
    # the component on the GT meter: overlaps its centre-x band and its rows
    best = None
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if x + w < gcx - 16 * s or x > gcx + 16 * s:
            continue
        if y + h < wy0 or y > wy1:
            continue
        if best is None or area > best[4]:
            best = (x, y, w, h, area, i)
    if best is None:
        return "g3_no_component", {}
    x, y, w, h, area, i = best
    det = {"w": w, "h": h, "area": area, "sol": round(area / max(1.0, w * h), 2),
           "s": round(s, 3)}
    wmin = relax.get("col_w_min", loc.col_w_min) * s
    wmax = loc.col_w_max * s
    if not (wmin <= w <= wmax):
        det["gate"] = "w %d not in %.1f..%.1f" % (w, wmin, wmax)
        return "g4_width", det
    if area < 0.35 * w * h:
        det["gate"] = "solidity %.2f < 0.35" % (area / max(1.0, w * h))
        return "g4_solidity", det
    # gate 8 lone
    lone_gap = loc.lone_gap * s
    twin = False
    for j in range(1, n):
        if j == i:
            continue
        jx, jy, jw, jh, _ja = (int(v) for v in stats[j])
        if jw < 3 or jh < sup:
            continue
        gap = max(jx - (x + w), x - (jx + jw))
        ov = min(y + h, jy + jh) - max(y, jy)
        if (gap <= lone_gap and ov >= 0.6 * min(h, jh)
                and 0.5 * h <= jh <= 1.6 * h and jw >= 4 * s):
            twin = True
            break
    if twin:
        return "g8_not_lone", det
    # gate 5 green tip
    cx = x + w * 0.5
    wbot = y + h
    tip_gap = relax.get("tip_gap", loc.tip_gap) * s
    gtop_prior = wbot - tip_gap
    tx0 = int(max(0, cx - loc.tip_dx_max * s))
    tx1 = int(min(sub.shape[1], cx + loc.tip_dx_max * s + 1))
    ty0 = int(max(0, gtop_prior - loc.tip_tol * s))
    ty1 = int(max(0, gtop_prior + loc.tip_tol * s + 6 * s))
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    gm = cv2.inRange(hsv[ty0:ty1, tx0:tx1], loc._GREEN_LO, loc._GREEN_HI)
    ngreen = int(cv2.countNonZero(gm))
    det["tip_px"] = ngreen
    det["tip_prior_row"] = int(gtop_prior)
    conf = 0.90
    if ngreen >= loc.tip_px_min:
        gy = np.flatnonzero(gm.sum(axis=1) > 0)
        gtop = ty0 + int(gy.min())
        if abs(gtop - gtop_prior) > loc.tip_tol * s:
            gtop = int(round(gtop_prior))
    elif 0.85 * tip_gap <= h <= 1.08 * tip_gap:
        gtop = int(round(gtop_prior))
        conf = 0.75
    else:
        return "g5_no_tip", det
    # gate 9 shape
    before = dict(loc.stats)
    ok = loc._meter_shaped(labels, i, x, y, w, h, area, hsv, cx, gtop, conf, s, None,
                           relax.get("shape_min_h"))
    d = {k: loc.stats[k] - before.get(k, 0) for k in loc.stats if loc.stats[k] - before.get(k, 0)}
    if not ok:
        det["gate9"] = ",".join(sorted(d))
        return "g9_" + (",".join(sorted(d)) or "shape"), det
    det["conf"] = conf
    return "ACCEPT", det


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=float, default=0.66667)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--clip", default="")
    ap.add_argument("--relax", default="", help="k=v,... : v_min, gap_close, min_support, "
                                                "col_w_min, tip_gap, shape_min_h")
    a = ap.parse_args()
    relax = {}
    for kv in a.relax.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            relax[k.strip()] = float(v)
    loc = mlc.MeterContourLocator()
    rows = gt_rows()
    if a.clip:
        rows = [r for r in rows if r["clip"] == a.clip]
    if a.limit:
        rows = rows[: a.limit]
    tally = collections.Counter()
    bydet = collections.defaultdict(list)
    for r in rows:
        p = os.path.join(DS, "images", r["split"], "%s_%05d.png" % (r["clip"], int(r["idx"])))
        im = cv2.imread(p)
        if im is None:
            continue
        gt = [float(r[k]) for k in ("x0", "y0", "x1", "y1")]
        if abs(a.scale - 1.0) > 1e-6:
            im = cv2.resize(im, None, fx=a.scale, fy=a.scale, interpolation=cv2.INTER_AREA)
            gt = [v * a.scale for v in gt]
        stage, det = autopsy(loc, im, gt, relax)
        tally[stage] += 1
        if det:
            bydet[stage].append(det)
    n = sum(tally.values())
    print("")
    print("=== PILL gate autopsy  scale=%s  relax=%s  (%d GT frames)" % (a.scale, relax or "-", n))
    print("%-24s %6s %7s   detail (median)" % ("died at", "count", "share"))
    for stage, c in tally.most_common():
        ds = bydet.get(stage) or []
        extra = ""
        if ds:
            ws = [d["w"] for d in ds if "w" in d]
            hs = [d["h"] for d in ds if "h" in d]
            sol = [d["sol"] for d in ds if "sol" in d]
            tp = [d["tip_px"] for d in ds if "tip_px" in d]
            extra = "w=%.0f h=%.0f sol=%.2f" % (np.median(ws), np.median(hs), np.median(sol))
            if tp:
                extra += " tip_px=%.0f" % np.median(tp)
            if ds[0].get("gate"):
                extra += "  [%s]" % ds[0]["gate"]
        print("%-24s %6d %6.1f%%   %s" % (stage, c, 100.0 * c / max(1, n), extra))
    return 0


if __name__ == "__main__":
    sys.exit(main())
