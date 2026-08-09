"""Gate-attribution analysis of ORION_FRAMEDUMP raw frames (Round 33 triage).

Replays each `*_raw.png` through MeterDetector (stateful, in capture order, live
config via load_detector_config) with the `_debug` sink enabled, and reports per
frame which gate killed every candidate — so a frame with a visible meter that
ends `roi_not_found` tells us exactly WHY.

Usage:
  python tools/diagnostics/analyze_framedump.py                       # summary, all frames
  python tools/diagnostics/analyze_framedump.py --frame 0042          # deep-dive one frame
  python tools/diagnostics/analyze_framedump.py --frame 0042 --crop 920,500,40,200
        # also print masked/unmasked HSV stats of a hand-picked region (the real meter)
  python tools/diagnostics/analyze_framedump.py --dir <path>          # non-default dump dir
"""
import argparse
import glob
import os
import sys
from pathlib import Path

import cv2
import numpy as np

REPO = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, REPO)
from meter_detector import MeterDetector, load_detector_config  # noqa: E402


def hsv_stats(frame_bgr, x, y, w, h, label):
    crop = frame_bgr[y:y + h, x:x + w]
    if crop.size == 0:
        print(f"  [{label}] empty crop")
        return
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hh, ss, vv = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    sat_sel = ss > 120  # saturated pixels only (the fill, not the dark track)
    n = int(sat_sel.sum())
    print(f"  [{label}] crop ({x},{y},{w},{h}) saturated px (S>120): {n}/{crop.shape[0]*crop.shape[1]}")
    if n:
        print(f"    sat-px hue median {float(np.median(hh[sat_sel])):.1f} "
              f"p10 {float(np.percentile(hh[sat_sel], 10)):.1f} p90 {float(np.percentile(hh[sat_sel], 90)):.1f} | "
              f"sat median {float(np.median(ss[sat_sel])):.1f} | val median {float(np.median(vv[sat_sel])):.1f}")
        hist = np.bincount(hh[sat_sel].ravel(), minlength=180)
        top = np.argsort(hist)[::-1][:6]
        print("    hue histogram top bins:", ", ".join(f"H{int(b)}:{int(hist[b])}" for b in top if hist[b]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(REPO, "logs", "diagnostics", "framedump"))
    ap.add_argument("--frame", default=None, help="substring of one filename to deep-dive")
    ap.add_argument("--crop", default=None, help="x,y,w,h region to print HSV stats for (with --frame)")
    args = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(args.dir, "*_raw.png")))
    if not paths:
        print("no *_raw.png in", args.dir)
        return 1

    cfg = load_detector_config()
    det = MeterDetector(os.path.join(REPO, "meter_styles"), cfg)
    print(f"frames: {len(paths)}  cfg: conf_thr={cfg.confidence_threshold} "
          f"purity hue_max={cfg.purple_purity_hue_max} sat_min={cfg.purple_purity_sat_min} "
          f"min_px={cfg.purple_purity_min_px}")

    deep = None
    for p in paths:
        frame = cv2.imread(p)
        if frame is None:
            continue
        det._debug = {}
        res = det.detect(frame)
        dbg = det._debug
        det._debug = None
        name = os.path.basename(p)
        scan = det._last_scan or {}
        line = (f"{name}: det={int(res.detected)} fill={res.fill_pct:.1f} conf={res.confidence:.2f} "
                f"rej={res.rejection_reason or '-'} bbox={res.bbox} "
                f"cand={scan.get('cand_n')} size_ok={scan.get('cand_size_ok')} "
                f"pur_rej={scan.get('purity_rej')} med_h={scan.get('med_h')} med_s={scan.get('med_s')}")
        print(line)
        if args.frame and args.frame in name:
            deep = (name, frame, dbg)

    if deep:
        name, frame, dbg = deep
        print(f"\n=== deep dive: {name} ===")
        for g in dbg.get("gates", []):
            print("  gate:", g)
        for r in dbg.get("regions", []):
            print("  region:", r)
        cands = dbg.get("cand", [])
        print(f"  candidates ({len(cands)}):")
        for c in sorted(cands, key=lambda c: -c["w"] * c["h"])[:25]:
            print("   ", c)
        for pr in dbg.get("purity_reject", []):
            print("  purity_reject:", pr)
        if args.crop:
            x, y, w, h = (int(v) for v in args.crop.split(","))
            hsv_stats(frame, x, y, w, h, "manual crop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
