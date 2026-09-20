#!/usr/bin/env python
"""Judge the trained 2K27 meter detector on the SAME metric the shipped reader loses on.

The bar to beat is concrete. Measured over the 2026-08-27 hoodie session, the shipped
colour reader's published box sat on the real meter 17.8% of the time (65-74% on the
previous day's easier footage with identical code). If this detector cannot clear that
by a wide margin on the SAME frames, it does not get wired in.

Ground truth here is the high-precision auto-labeller, which abstains on anything
ambiguous -- so this measures agreement on frames where the meter is unarguable. It is
NOT a recall measure against frames the labeller skipped; those are reported separately
as "found something where the labeller abstained", which is informative but unverified.
"""
from __future__ import annotations
import argparse, glob, os, sys
import cv2, numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from label_meter_2k27 import label as gt_label


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", required=True)
    ap.add_argument("--session", default="logs/diagnostics/framedump/session_20260827_122443")
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--imgsz", type=int, default=1280)
    ap.add_argument("--stride", type=int, default=3)
    a = ap.parse_args()
    from ultralytics import YOLO
    m = YOLO(a.weights)
    fs = sorted(glob.glob(os.path.join(a.session, "f*_raw.png")))[::a.stride]
    hit = miss = nodet = 0
    extra = 0            # detector fired where the labeller abstained (unverified)
    abstain = 0
    for p in fs:
        img = cv2.imread(p)
        if img is None:
            continue
        gt = gt_label(img)
        res = m.predict(p, imgsz=a.imgsz, conf=a.conf, verbose=False)[0]
        has = res.boxes is not None and len(res.boxes) > 0
        if gt is None:
            abstain += 1
            if has:
                extra += 1
            continue
        if not has:
            nodet += 1
            continue
        b = res.boxes.xyxy.cpu().numpy(); c = res.boxes.conf.cpu().numpy()
        x1, y1, x2, y2 = b[int(np.argmax(c))]
        gx = (gt[0] + gt[2]) / 2.0
        if abs((x1 + x2) / 2.0 - gx) <= 25:
            hit += 1
        else:
            miss += 1
    tot = hit + miss + nodet
    print(f"session: {os.path.basename(a.session)}   frames scored: {tot}  (labeller abstained on {abstain})")
    if tot:
        print(f"  detector box ON the meter : {hit:5d} ({100*hit/tot:5.1f}%)")
        print(f"  detector box elsewhere    : {miss:5d} ({100*miss/tot:5.1f}%)")
        print(f"  detector found nothing    : {nodet:5d} ({100*nodet/tot:5.1f}%)")
    print(f"  [unverified] fired on {extra}/{abstain} frames the labeller skipped")
    print(f"  SHIPPED COLOUR READER on this session: 17.8% on-meter")


if __name__ == "__main__":
    sys.exit(main())
