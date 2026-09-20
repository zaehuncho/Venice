#!/usr/bin/env python3
"""Before/after evaluation harness for the 2K27 meter detector.

Grades a model THE WAY THE RUNTIME USES IT -- through meter_detector_yolo.MeterYoloLocator
(same letterbox, conf 0.35, NMS, plausibility gate ON) -- against dome-anchored ground
truth, NOT against teacher-label mAP (project memory: teacher-label val mAP is rigged
and must never be cited).

Modes:
  pill       datasets/meter2k27_pill_park/audit.csv positives = GT (dome-verified,
             visually audited). Decodes each clip, evaluates every GT frame at native
             1080p AND downscaled 1280x720 (the live capture pipeline resolution).
             A hit = detection with IoU >= 0.30 vs the GT box (a detection elsewhere
             on the frame is counted separately as off_target).
  framedump  detection rate over every *_raw.png of the given 720p sessions (Arrow2
             regression arm). With --ref-model, also reports like-for-like agreement:
             frames where ref detects, current detects, both, and median IoU on both.
  fp         no-meter session: every detection is a false positive.

Usage:
  eval_meter_detector.py pill --model X.onnx --clips-dir DIR [--csv out.csv]
  eval_meter_detector.py framedump --model X.onnx SESSION_DIR [...] [--ref-model Y.onnx]
  eval_meter_detector.py fp --model X.onnx SESSION_DIR
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import sys

import cv2
import numpy as np

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)


def make_locator(path):
    from meter_detector_yolo import MeterYoloLocator
    loc = MeterYoloLocator(model_path=path)
    if not loc.ok:
        print(f"FATAL: locator failed to load {path}"); sys.exit(2)
    print(f"# model={path} provider={loc.provider} imgsz={loc.imgsz}")
    return loc


def iou(a, b):
    ax0, ay0, ax1, ay1 = a; bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    ua = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / ua if ua > 0 else 0.0


def run_pill(a):
    loc = make_locator(a.model)
    audit = os.path.join(REPO, "datasets", "meter2k27_pill_park", "audit.csv")
    gt = {}
    for r in csv.DictReader(open(audit)):
        if r["cls"] == "pos":
            gt.setdefault(r["clip"], {})[int(r["idx"])] = tuple(float(r[k]) for k in ("x0", "y0", "x1", "y1"))
    rows = []
    tot = {"1080": [0, 0, 0], "720": [0, 0, 0]}   # frames, hits, off_target
    for clip, frames in sorted(gt.items()):
        vids = glob.glob(os.path.join(a.clips_dir, clip + "*.mp4"))
        if not vids:
            print(f"skip {clip}: clip not found"); continue
        cap = cv2.VideoCapture(vids[0])
        stat = {"1080": [0, 0, 0], "720": [0, 0, 0]}
        want = sorted(frames)
        idx = -1
        while want:
            ok, fr = cap.read()
            if not ok:
                break
            idx += 1
            if idx != want[0]:
                continue
            want.pop(0)
            g = frames[idx]
            for tag, img, s in (("1080", fr, 1.0),
                                ("720", cv2.resize(fr, (1280, 720), interpolation=cv2.INTER_AREA), 2.0 / 3.0)):
                det = loc.detect_box(img)
                stat[tag][0] += 1
                hit = off = 0
                if det is not None:
                    x, y, w, h, conf = det
                    gs = tuple(v * s for v in g)
                    if iou((x, y, x + w, y + h), gs) >= 0.30:
                        hit = 1
                    else:
                        off = 1
                stat[tag][1] += hit; stat[tag][2] += off
                rows.append(dict(clip=clip, idx=idx, res=tag, hit=hit, off=off,
                                 conf=round(float(det[4]), 3) if det else ""))
        cap.release()
        for tag in ("1080", "720"):
            n, h, o = stat[tag]
            tot[tag][0] += n; tot[tag][1] += h; tot[tag][2] += o
            print(f"{clip} @{tag}: {h}/{n} = {100.0*h/max(n,1):.1f}%  off_target={o}")
    for tag in ("1080", "720"):
        n, h, o = tot[tag]
        print(f"TOTAL @{tag}: {h}/{n} = {100.0*h/max(n,1):.1f}%  off_target={o}")
    if a.csv:
        with open(a.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["clip", "idx", "res", "hit", "off", "conf"])
            w.writeheader(); w.writerows(rows)
    return 0


def run_framedump(a):
    loc = make_locator(a.model)
    ref = make_locator(a.ref_model) if a.ref_model else None
    for sess in a.sessions:
        files = sorted(glob.glob(os.path.join(sess, "f*_raw.png")))
        n = det = both = ref_det = ref_only = new_only = 0
        ious = []
        for f in files:
            img = cv2.imread(f)
            if img is None:
                continue
            n += 1
            d = loc.detect_box(img)
            det += d is not None
            if ref is not None:
                rd = ref.detect_box(img)
                ref_det += rd is not None
                if d is not None and rd is not None:
                    both += 1
                    ious.append(iou((d[0], d[1], d[0]+d[2], d[1]+d[3]),
                                    (rd[0], rd[1], rd[0]+rd[2], rd[1]+rd[3])))
                elif rd is not None:
                    ref_only += 1
                elif d is not None:
                    new_only += 1
        line = f"{os.path.basename(sess)}: frames={n} det={det} ({100.0*det/max(n,1):.2f}%)"
        if ref is not None:
            line += (f"  ref_det={ref_det} ({100.0*ref_det/max(n,1):.2f}%) both={both} "
                     f"ref_only={ref_only} new_only={new_only} "
                     f"iou_med={np.median(ious):.3f} iou_p10={np.percentile(ious,10):.3f}" if ious else
                     f"  ref_det={ref_det} both=0")
        print(line)
    return 0


def run_fp(a):
    loc = make_locator(a.model)
    for sess in a.sessions:
        files = sorted(glob.glob(os.path.join(sess, "f*_raw.png")))
        n = fp = 0
        examples = []
        for f in files:
            img = cv2.imread(f)
            if img is None:
                continue
            n += 1
            d = loc.detect_box(img)
            if d is not None:
                fp += 1
                if len(examples) < 12:
                    examples.append((os.path.basename(f), tuple(int(v) for v in d[:4]), round(float(d[4]), 3)))
        print(f"{os.path.basename(sess)}: FP {fp}/{n} = {100.0*fp/max(n,1):.3f}%")
        for e in examples:
            print("   ", e)
    return 0


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("pill"); p.add_argument("--model", required=True)
    p.add_argument("--clips-dir", required=True); p.add_argument("--csv", default="")
    f = sp.add_parser("framedump"); f.add_argument("--model", required=True)
    f.add_argument("--ref-model", default=""); f.add_argument("sessions", nargs="+")
    g = sp.add_parser("fp"); g.add_argument("--model", required=True); g.add_argument("sessions", nargs="+")
    a = ap.parse_args()
    return dict(pill=run_pill, framedump=run_framedump, fp=run_fp)[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
