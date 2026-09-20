#!/usr/bin/env python
"""Old-vs-new evaluation of the 2K27 meter detector on the LOW-FILL hard set.

Grades each model THE WAY THE RUNTIME USES IT -- through meter_detector_yolo.MeterYoloLocator
(ONNX, same letterbox at the model's own imgsz, NMS, plausibility gate ON) -- against the
cv2-verified boxes of tools/training/mine_lowfill_hardset.py (manifest.csv).

  recall : per fill bucket (fill_est 0-10 / 10-20 / 20-30 / 30-50 / 50+), hit = a detection
           with IoU >= 0.30 against the manifest box; split by source
           (propagated = frames the shipped pipeline MISSED, detected_lowfill = frames it
           found at low fill) and by split (heldout = sessions never trained on,
           val = by-shot model-selection split, train = seen in training).
  fp     : detections on manifest negatives (no meter within 1.5 s of the frame, guard
           clean) -- every detection is a false positive.

Usage:
  eval_meter_lowfill.py --models old=PATH new=PATH [...] --hardset DIR --conf 0.35 0.25
                        [--splits heldout val] [--csv out.csv]
"""
from __future__ import annotations
import argparse, csv, os, sys, time, collections
import cv2, numpy as np

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)

BUCKETS = [(0, 10), (10, 20), (20, 30), (30, 50), (50, 101)]


def bucket(f):
    for lo, hi in BUCKETS:
        if lo <= f < hi:
            return f"{lo}-{hi if hi < 101 else '100'}"
    return "?"


def iou(a, b):
    ax0, ay0, ax1, ay1 = a; bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0); ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    ua = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / ua if ua > 0 else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", required=True, help="name=path.onnx ...")
    ap.add_argument("--hardset", default=os.path.join(REPO, "runs", "detect", "logs", "diagnostics",
                                                      "meter_train", "lowfill_hardset"))
    ap.add_argument("--conf", type=float, nargs="+", default=[0.35, 0.25])
    ap.add_argument("--splits", nargs="+", default=["heldout", "val", "train"])
    ap.add_argument("--neg-splits", nargs="+", default=["heldout", "heldout_neg"])
    ap.add_argument("--csv", default="")
    ap.add_argument("--max-neg", type=int, default=0)
    a = ap.parse_args()
    from meter_detector_yolo import MeterYoloLocator
    models = []
    for spec in a.models:
        name, path = spec.split("=", 1)
        loc = MeterYoloLocator(model_path=path, conf_thres=min(a.conf))
        if not loc.ok:
            print("FATAL: could not load", path); return 2
        print(f"# {name}: {path} provider={loc.provider} imgsz={loc.imgsz}", flush=True)
        models.append((name, loc))
    man = list(csv.DictReader(open(os.path.join(a.hardset, "manifest.csv"))))
    pos = [m for m in man if m["source"] != "negative" and m["split"] in a.splits]
    neg = [m for m in man if m["source"] == "negative" and m["split"] in a.neg_splits]
    if a.max_neg:
        neg = neg[:a.max_neg]
    print(f"# positives: {len(pos)}  negatives: {len(neg)}", flush=True)
    rows = []
    lat = collections.defaultdict(list)
    # ---- positives: one inference per model per frame at the LOWEST conf, then threshold
    # offline (the locator returns the single best box above its conf_thres; grading at a
    # higher conf just asks whether that box's conf clears it).
    for i, m in enumerate(pos):
        img = cv2.imread(os.path.join(a.hardset, "images", m["split"], m["image"]))
        gt = (float(m["x"]), float(m["y"]), float(m["x"]) + float(m["w"]), float(m["y"]) + float(m["h"]))
        rec = dict(kind="pos", split=m["split"], source=m["source"], session=m["session"], idx=m["idx"],
                   fill_est=float(m["fill_est"]), bucket=bucket(float(m["fill_est"])),
                   old_detected=m["old_detected"])
        for name, loc in models:
            t0 = time.perf_counter()
            d = loc.detect_box(img)
            lat[name].append((time.perf_counter() - t0) * 1000.0)
            if d is None:
                rec[f"{name}_conf"] = 0.0; rec[f"{name}_iou"] = 0.0
            else:
                x, y, w, h, c = d
                rec[f"{name}_conf"] = float(c); rec[f"{name}_iou"] = iou((x, y, x + w, y + h), gt)
        rows.append(rec)
        if (i + 1) % 200 == 0:
            print(f"  {i + 1}/{len(pos)} positives", flush=True)
    for i, m in enumerate(neg):
        img = cv2.imread(os.path.join(a.hardset, "images", m["split"], m["image"]))
        rec = dict(kind="neg", split=m["split"], source="negative", session=m["session"], idx=m["idx"],
                   fill_est=-1, bucket="", old_detected=0)
        for name, loc in models:
            d = loc.detect_box(img)
            rec[f"{name}_conf"] = 0.0 if d is None else float(d[4]); rec[f"{name}_iou"] = 0.0
            rec[f"{name}_box"] = "" if d is None else f"{d[0]},{d[1]},{d[2]},{d[3]}"
        rows.append(rec)
        if (i + 1) % 500 == 0:
            print(f"  {i + 1}/{len(neg)} negatives", flush=True)
    if a.csv:
        keys = []
        for r in rows:
            for k in r:
                if k not in keys:
                    keys.append(k)
        with open(a.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys, restval=""); w.writeheader(); w.writerows(rows)

    # ---- report
    names = [n for n, _ in models]
    def hit(r, n, c):
        return r[f"{n}_conf"] >= c and r[f"{n}_iou"] >= 0.30
    print("\n## Recall on hard-set positives (hit = box IoU >= 0.30 with the verified box)\n")
    for split in a.splits:
        for source in ("propagated", "detected_lowfill"):
            sub = [r for r in rows if r["kind"] == "pos" and r["split"] == split and r["source"] == source]
            if not sub:
                continue
            print(f"### split={split} source={source} (n={len(sub)})\n")
            hdr = "| fill bucket | n | " + " | ".join(f"{n} @{c:.2f}" for c in a.conf for n in names) + " |"
            print(hdr); print("|" + "---|" * (2 + len(a.conf) * len(names)))
            for b in [f"{lo}-{hi if hi < 101 else '100'}" for lo, hi in BUCKETS] + ["ALL"]:
                g = sub if b == "ALL" else [r for r in sub if r["bucket"] == b]
                if not g:
                    continue
                cells = []
                for c in a.conf:
                    for n in names:
                        k = sum(1 for r in g if hit(r, n, c))
                        cells.append(f"{k}/{len(g)} ({100.0 * k / len(g):.0f}%)")
                print(f"| {b} | {len(g)} | " + " | ".join(cells) + " |")
            print()
    negs = [r for r in rows if r["kind"] == "neg"]
    if negs:
        print(f"## False positives on {len(negs)} no-meter frames (splits {a.neg_splits})\n")
        print("| session | n | " + " | ".join(f"{n} @{c:.2f}" for c in a.conf for n in names) + " |")
        print("|" + "---|" * (2 + len(a.conf) * len(names)))
        for sess in sorted({r["session"] for r in negs}) + ["ALL"]:
            g = negs if sess == "ALL" else [r for r in negs if r["session"] == sess]
            cells = [str(sum(1 for r in g if r[f"{n}_conf"] >= c)) for c in a.conf for n in names]
            print(f"| {sess} | {len(g)} | " + " | ".join(cells) + " |")
        for n in names:
            ex = sorted([r for r in negs if r[f"{n}_conf"] >= min(a.conf)], key=lambda r: -r[f"{n}_conf"])[:8]
            if ex:
                print(f"\n{n} FP examples (session idx conf box): " +
                      "; ".join(f"{r['session']} {r['idx']} {r[f'{n}_conf']:.2f} [{r[f'{n}_box']}]" for r in ex))
    print("\n## Latency (ms / frame, locator end-to-end incl. letterbox)\n")
    for n in names:
        v = np.array(lat[n][5:] or lat[n])
        print(f"- {n}: median {np.median(v):.1f} ms, p90 {np.percentile(v, 90):.1f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
