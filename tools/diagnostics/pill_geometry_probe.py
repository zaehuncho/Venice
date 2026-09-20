"""Measure the 2K27 PILL capsule's own geometry inside the ground-truth boxes.

READ-ONLY. Answers the questions the CV locator's gates ask:
  gate 1  how many columns inside the capsule are achromatic-bright (V>=225, spread<=25)?
  gate 2  how tall are the DARK divider rows between rungs (the gap-close budget)?
  gate 3  how tall is one rung (the min-support budget)?
  gate 4  how wide is the bright run (col_w_min/max)?
  gate 5  apex(green) -> fill base distance (tip_gap)
  gate 9  solidity / row-width spread of the component after the shipped morphology.

Source: datasets/meter2k27_pill_park (687+87 full 1920x1080 frames, GT boxes in audit.csv).
Usage: python tools/diagnostics/pill_geometry_probe.py [--scale 1.0|0.6667] [--limit N]
"""
from __future__ import annotations
import argparse, csv, os, sys
import cv2, numpy as np

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DS = os.path.join(REPO, "datasets", "meter2k27_pill_park")
OUT = r"D:\NexusVision\pill_check"

V_MIN = 225
SPREAD_MAX = 25
GREEN_LO = (38, 90, 90); GREEN_HI = (85, 255, 255)


def rows(cls="pos"):
    with open(os.path.join(DS, "audit.csv"), newline="") as f:
        for r in csv.DictReader(f):
            if r["cls"] == cls:
                yield r


def img_path(r):
    return os.path.join(DS, "images", r["split"], f"{r['clip']}_{int(r['idx']):05d}.png")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=float, default=1.0, help="1.0=1080p source, 0.66667=720p")
    ap.add_argument("--limit", type=int, default=400)
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)

    recs = list(rows())[: a.limit] if a.limit > 0 else list(rows())
    runs, gaps, widths, cols_bright, tipgaps, sols, rwcvs, fillh = [], [], [], [], [], [], [], []
    n = 0
    for r in recs:
        p = img_path(r)
        im = cv2.imread(p)
        if im is None:
            continue
        if abs(a.scale - 1.0) > 1e-6:
            im = cv2.resize(im, None, fx=a.scale, fy=a.scale, interpolation=cv2.INTER_AREA)
        H, W = im.shape[:2]
        s = H / 720.0
        x0, y0, x1, y1 = [float(r[k]) * a.scale for k in ("x0", "y0", "x1", "y1")]
        ix0, iy0 = int(max(0, x0)), int(max(0, y0))
        ix1, iy1 = int(min(W, x1)), int(min(H, y1))
        crop = im[iy0:iy1, ix0:ix1]
        if crop.size == 0:
            continue
        n += 1
        b, g, rr = cv2.split(crop)
        mx = cv2.max(cv2.max(b, g), rr); mn = cv2.min(cv2.min(b, g), rr)
        white = ((mx >= V_MIN) & ((mx.astype(np.int16) - mn.astype(np.int16)) <= SPREAD_MAX))
        # per-column bright count -> the capsule's own core band
        colcnt = white.sum(axis=0)
        if colcnt.max() == 0:
            continue
        core = np.flatnonzero(colcnt >= 0.25 * colcnt.max())
        cols_bright.append(len(core))
        band = white[:, core.min(): core.max() + 1]
        rowon = band.sum(axis=1)
        wid = rowon[rowon > 0]
        if wid.size:
            widths.append(float(np.median(wid)))
        # run/gap structure along the fill (bottom-up until the last bright row)
        on = rowon > 0
        idx = np.flatnonzero(on)
        if idx.size < 4:
            continue
        lo, hi = idx.min(), idx.max()
        seg = on[lo:hi + 1]
        # run lengths
        cur = seg[0]; cnt = 1
        for v in seg[1:]:
            if v == cur:
                cnt += 1
            else:
                (runs if cur else gaps).append(cnt)
                cur = v; cnt = 1
        (runs if cur else gaps).append(cnt)
        fillh.append(int(hi - lo + 1))
        # green apex -> fill base
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        gm = cv2.inRange(hsv, GREEN_LO, GREEN_HI)
        gy = np.flatnonzero(gm.sum(axis=1) > 0)
        if gy.size:
            tipgaps.append(float(hi - gy.min()))
    def q(v, name, unit="px"):
        if not v:
            print(f"  {name:28s} (none)"); return
        v = np.asarray(v, float)
        print(f"  {name:28s} n={len(v):4d}  p5={np.percentile(v,5):6.1f} "
              f"p50={np.median(v):6.1f} p95={np.percentile(v,95):6.1f} max={v.max():6.1f} {unit}")
    print(f"PILL geometry @scale {a.scale} ({n} GT frames, source 1920x1080)")
    q(cols_bright, "bright columns in box")
    q(widths, "median bright row width")
    q(runs, "RUNG height (bright run)")
    q(gaps, "DIVIDER height (dark gap)")
    q(fillh, "fill stack height (base..top)")
    q(tipgaps, "green apex -> fill base")
    print(f"  shipped gates @scale {a.scale}: gap_close={3*(1080*a.scale/720):.1f}(+2), "
          f"min_support={6*(1080*a.scale/720):.1f}, col_w={8*(1080*a.scale/720):.1f}"
          f"..{22*(1080*a.scale/720):.1f}, tip_gap={100*(1080*a.scale/720):.1f}"
          f"+-{5*(1080*a.scale/720):.1f}, shape_min_h={14*(1080*a.scale/720):.1f}")


if __name__ == "__main__":
    sys.exit(main() or 0)
