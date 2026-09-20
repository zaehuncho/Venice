"""Reader fill vs HAND-READ fill on real 2K27 Pill frames, inside each proposer's box.

READ-ONLY.  For each sampled ground-truth frame it reports
  hand   the fill measured straight off the capsule's own landmarks: the bright fill
         stack's top row, the green dome's apex and the fill base, all read inside the
         GT box, as a percentage of the apex->base span (the number a human reads off
         the ladder);
  yolo   SimpleMeterReader._measure_fill_in_box in the SHIPPED ONNX proposer's box
         (models/orion_meter_detector.onnx -- the 08-30 meter2k27_n3_pill net), with the
         landmark ruler FORCED OFF (ORION_PILL_RULER=0) -- i.e. the shipped box ruler;
  new    the same reader call in the same YOLO box with the LANDMARK ruler ON
         (pill_fill_ruler.py, ORION_PILL_RULER=1): S*(base-top)/(base-apex), S=96;
  cv     the same reader call in meter_locator_cv's box (ORION_METER_PROPOSER=cv),
         optionally with a relaxed gate-4 width floor via --env.
A blank cell means the proposer refused the frame (no box to measure in).

The trailing table scores `new` against S/100*hand (the target ruler): the landmark ruler
is correct when the residual has NO intercept and NO slope error over 5-97 %.

Usage: python tools/diagnostics/pill_fill_check.py --n 40 --scale 0.66667
       python tools/diagnostics/pill_fill_check.py --n 40 --scale 1.0
       python tools/diagnostics/pill_fill_check.py --n 10 --env ORION_CV_COL_W_MIN=3
"""
from __future__ import annotations
import argparse, collections, csv, os, sys

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, REPO)

import cv2                                      # noqa: E402
import numpy as np                              # noqa: E402

DS = os.path.join(REPO, "datasets", "meter2k27_pill_park")
OUT = os.path.join("D:", os.sep, "NexusVision", "pill_check")
GREEN_LO = (38, 90, 90)
GREEN_HI = (85, 255, 255)


class _Cfg(object):
    """Minimal DetectorConfig stand-in: the fields the reader reads off cfg."""
    meter_style = "Pill"
    meter_color = "White"
    bar_color = "White"


def hand_fill(im, gt):
    """Fill % read off the capsule's own landmarks inside the GT box."""
    x0, y0, x1, y1 = [int(round(v)) for v in gt]
    c = im[max(0, y0):y1, max(0, x0):x1]
    if c.size == 0:
        return None, None
    b, g, r = cv2.split(c)
    mx = cv2.max(cv2.max(b, g), r)
    mn = cv2.min(cv2.min(b, g), r)
    white = (mx >= 225) & ((mx.astype(np.int16) - mn.astype(np.int16)) <= 25)
    colcnt = white.sum(axis=0)
    if colcnt.max() == 0:
        return None, None
    core = np.flatnonzero(colcnt >= 0.25 * colcnt.max())
    band = white[:, core.min(): core.max() + 1]
    rowon = np.flatnonzero(band.sum(axis=1) > 0)
    if rowon.size < 4:
        return None, None
    base = int(rowon.max())
    # the fill stack is the base-touching run, bridging divider-sized gaps (<= 4 rows)
    top = base
    r_ = base
    while r_ >= 0:
        if band[r_].sum() > 0:
            top = r_
            r_ -= 1
            continue
        k = r_
        while k >= 0 and k > r_ - 5 and band[k].sum() == 0:
            k -= 1
        if k >= 0 and k > r_ - 5 and band[k].sum() > 0:
            r_ = k
            continue
        break
    hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
    gm = cv2.inRange(hsv, GREEN_LO, GREEN_HI)
    gy = np.flatnonzero(gm.sum(axis=1) > 0)
    if gy.size == 0:
        return None, None
    apex = int(gy.min())
    span = float(base - apex)
    if span <= 1:
        return None, None
    return round(100.0 * (base - top) / span, 1), (apex, top, base)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--scale", type=float, default=0.66667)
    ap.add_argument("--clip", default="")
    ap.add_argument("--env", default="")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    for kv in a.env.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            os.environ[k.strip()] = v.strip()

    with open(os.path.join(DS, "audit.csv"), newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["cls"] == "pos"]
    if a.clip:
        rows = [r for r in rows if r["clip"] == a.clip]
    # sample across the whole fill range of one clip's longest shot
    byclip = collections.Counter(r["clip"] for r in rows)
    pick = a.clip or byclip.most_common(1)[0][0]
    rs = sorted([r for r in rows if r["clip"] == pick], key=lambda r: int(r["idx"]))
    step = max(1, len(rs) // a.n)
    sample = rs[::step][: a.n]

    os.environ["ORION_METER_PROPOSER"] = "yolo"
    import meter_detector_yolo as mdy
    import meter_locator_cv as mlc
    import pill_fill_ruler as pfr
    from simple_meter_reader import SimpleMeterReader
    yolo = mdy.MeterYoloLocator(conf_thres=0.35)
    cvl = mlc.MeterContourLocator()
    # one reader per ruler arm: the landmark ruler latches per lock, so the arms must not
    # share reader state (and the box ruler must stay byte-identical to the shipped path).
    r_old = r_new = r_cv = None
    pairs = []          # (hand, old, new) for the scoring table
    print("")
    print("clip %s  scale=%s  env=%s  S=%.1f" % (pick, a.scale, a.env or "-", pfr.scale()))
    print("%-7s %7s   %-26s %-26s %-26s"
          % ("frame", "hand%", "YOLO box -> BOX ruler%", "YOLO box -> LANDMARK ruler%",
             "CV box -> reader fill%"))
    for r in sample:
        idx = int(r["idx"])
        p = os.path.join(DS, "images", r["split"], "%s_%05d.png" % (pick, idx))
        im = cv2.imread(p)
        if im is None:
            continue
        gt = [float(r[k]) for k in ("x0", "y0", "x1", "y1")]
        if abs(a.scale - 1.0) > 1e-6:
            im = cv2.resize(im, None, fx=a.scale, fy=a.scale, interpolation=cv2.INTER_AREA)
            gt = [v * a.scale for v in gt]
        if r_old is None:
            r_old = SimpleMeterReader(im.shape[1], im.shape[0], cfg=_Cfg())
            r_new = SimpleMeterReader(im.shape[1], im.shape[0], cfg=_Cfg())
            r_cv = SimpleMeterReader(im.shape[1], im.shape[0], cfg=_Cfg())
        hf, lm = hand_fill(im, gt)
        ts = idx / 60.0
        cells = []
        vals = {}
        for name, loc, rd, ruler in (("old", yolo, r_old, "0"), ("new", yolo, r_new, "1"),
                                     ("cv", cvl, r_cv, "1")):
            box = loc.detect_box(im, ts) if getattr(loc, "accepts_ts", False) else loc.detect_box(im)
            if not box:
                cells.append("%-26s" % "(refused)")
                continue
            bx, by, bw, bh = [int(v) for v in box[:4]]
            os.environ["ORION_PILL_RULER"] = ruler
            try:
                fill, _green, _top = rd._measure_fill_in_box(im, (bx, by, bw, bh), ts)
            except Exception as e:
                cells.append("%-26s" % ("err %s" % type(e).__name__))
                continue
            vals[name] = float(fill)
            if name == "new":
                d = getattr(rd, "_dbg_pill_ruler", None) or {}
                cells.append("%5.1f  span=%s lat=%s" % (
                    fill, d.get("span", "-"), d.get("latched", "-")))
            else:
                cells.append("%5.1f  box=%d,%d %dx%d" % (fill, bx, by, bw, bh))
        if hf is not None and "old" in vals and "new" in vals:
            pairs.append((hf, vals["old"], vals["new"]))
        while len(cells) < 3:
            cells.append("%-26s" % "")
        print("%-7d %7s   %-26s %-26s %-26s"
              % (idx, "-" if hf is None else ("%.1f" % hf), cells[0], cells[1], cells[2]))
    _score(pairs, pfr.scale())
    return 0


def _score(pairs, S):
    """Residual of each ruler against the target ruler (S/100 * hand), 5-97% only."""
    use = [p for p in pairs if 5.0 <= p[0] <= 97.5]
    if len(use) < 3:
        print("\n(too few scorable frames: %d)" % len(use))
        return
    h = np.array([p[0] for p in use], dtype=float)
    o = np.array([p[1] for p in use], dtype=float)
    n = np.array([p[2] for p in use], dtype=float)
    tgt = S / 100.0 * h
    print("\nscored on %d frames with hand in [5, 97.5]   target = %.2f * hand" % (len(use), S / 100.0))
    print("%-16s %8s %8s %8s %8s %8s %8s"
          % ("ruler", "med", "mean|e|", "sd", "max|e|", "slope", "intcpt"))
    for name, v in (("BOX (shipped)", o), ("LANDMARK (new)", n)):
        e = v - tgt
        A = np.vstack([h, np.ones_like(h)]).T
        m, c = np.linalg.lstsq(A, v, rcond=None)[0]
        print("%-16s %8.2f %8.2f %8.2f %8.2f %8.3f %8.2f"
              % (name, float(np.median(e)), float(np.mean(np.abs(e))), float(np.std(e)),
                 float(np.max(np.abs(e))), float(m), float(c)))
    e = n - tgt
    print("LANDMARK within +/-1.5pp of target: %d/%d" % (int(np.sum(np.abs(e) <= 1.5)), len(use)))


if __name__ == "__main__":
    sys.exit(main())
