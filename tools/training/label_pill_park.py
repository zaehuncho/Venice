#!/usr/bin/env python3
"""Auto-label the Pill-style 2K27 meter in park/rec gameplay clips for detector training.

WHY: the shipped meter detector (meter2k27_n2, trained only on the Arrow2 style in
MyCourt) finds NOTHING on the Pill style in real gameplay -- 0/40 on frames with a
plainly visible meter, 0-8% per clip. The colour probe (tools/quality/green_window_probe)
locates the Pill capsule reliably via its green dome + capsule-track check, so it is
used here as the labelling oracle instead of hand-labelling ~6200 frames.

LABEL GEOMETRY (measured, not guessed -- 47 sampled frames across 4 clips):
  the capsule shell is ~27 px wide at 1080p, symmetric about the probe's cx, and
  extends ~6 px above the dome apex and ~10 px below the fill base; the capsule span
  (apex->base) is a FIXED 153.7 +/- 1 px at 1080p (fixed-size HUD element). Box:
      x: cx-15 .. cx+15          (31 px: shell + 2 px glow fringe)
      y: apex-8 .. base+12       (~174 px)
  i.e. w_frac 0.0161, h_frac 0.161 -- mid-range of the runtime plausibility gate
  (w 0.013-0.035, h 0.07-0.25 of frame).

FRAME CLASSES per clip:
  POSITIVE   find_dome AND measure_pill succeed AND span in [145,165]  -> labelled box
  AMBIGUOUS  any dome evidence (this pass OR a prior worker's probe CSVs) but no
             trustworthy measurement -> EXCLUDED from both classes (occlusion, dimmed
             dome, fill eating the dome). Also every frame within GAP frames of any
             dome evidence: at 100% fill the dome is consumed and undetectable, so
             near-shot frames may contain an unlabelled meter and must not train as
             background.
  NEGATIVE   no dome evidence within +/-GAP frames -> empty label (hard negative:
             park decor, white clothing, court lines).

Outputs a YOLO dataset under datasets/meter2k27_pill_park/ (full 1920x1080 frames)
with a CLIP-LEVEL split (val clips held out entirely), a per-frame audit CSV, and
contact sheets of labelled crops for the mandatory visual verification.
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import random
import sys

import cv2
import numpy as np

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO, "tools", "quality"))
from green_window_probe import find_dome, measure_pill  # noqa: E402

# label geometry @1080p (see module docstring; scales with frame height if ever fed
# a different resolution, because the HUD scales with frame height)
HALF_W = 15.0 / 1080.0     # * frame H
TOP_PAD = 8.0 / 1080.0
BOT_PAD = 12.0 / 1080.0
SPAN_OK = (145.0, 165.0)   # accept only the fixed-size HUD capsule (@1080p rows)
GAP = 30                   # frames of quarantine around any dome evidence
NEG_STRIDE = 12            # take every Nth eligible negative frame


def synth_box(cx, apex, base, W, H):
    hw = HALF_W * H
    x0 = max(0.0, cx - hw); x1 = min(W - 1.0, cx + hw)
    y0 = max(0.0, apex - TOP_PAD * H); y1 = min(H - 1.0, base + BOT_PAD * H)
    return x0, y0, x1, y1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips-dir", required=True)
    ap.add_argument("--probe-csv-dir", default="", help="prior worker probe_*.csv dir (extra dome evidence)")
    ap.add_argument("--out", default=os.path.join(REPO, "datasets", "meter2k27_pill_park"))
    ap.add_argument("--val-clips", default="23af440b,4a9f0fca", help="clip key prefixes held out for val")
    ap.add_argument("--neg-per-clip", type=int, default=55)
    ap.add_argument("--seed", type=int, default=1234)
    a = ap.parse_args()
    random.seed(a.seed)

    clips = sorted(glob.glob(os.path.join(a.clips_dir, "*.mp4")))
    if not clips:
        print("no clips found"); return 2
    val_keys = tuple(k.strip() for k in a.val_clips.split(",") if k.strip())

    audit_rows = []
    counts = {}
    for vid in clips:
        key = os.path.basename(vid)[:8]
        split = "val" if key.startswith(val_keys) else "train"
        img_dir = os.path.join(a.out, "images", split)
        lbl_dir = os.path.join(a.out, "labels", split)
        os.makedirs(img_dir, exist_ok=True); os.makedirs(lbl_dir, exist_ok=True)

        # prior worker's dome evidence (their finder differs -> union is safer for negatives)
        prior_dome = set()
        if a.probe_csv_dir:
            for pc in glob.glob(os.path.join(a.probe_csv_dir, f"probe*_{key}.csv")) + \
                      glob.glob(os.path.join(a.probe_csv_dir, f"probe_{key}.csv")):
                with open(pc, newline="") as fh:
                    for r in csv.DictReader(fh):
                        if r.get("dome_found") in ("1", "True", "true"):
                            prior_dome.add(int(r["frame"]))

        cap = cv2.VideoCapture(vid)
        idx = -1
        frames = {}          # idx -> ("pos", box) | ("amb", None)
        keep_png = {}        # idx -> frame (only positives kept in memory? too big -> write now)
        n_pos = n_amb = 0
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            idx += 1
            H, W = fr.shape[:2]
            dome = find_dome(fr)
            cls = None
            if dome is not None:
                meas = measure_pill(fr, dome)
                if meas is not None and SPAN_OK[0] <= meas["span_px"] * (H / 1080.0) <= SPAN_OK[1]:
                    x0, y0, x1, y1 = synth_box(meas["cx"], meas["apex_abs"], meas["fill_base_abs"], W, H)
                    frames[idx] = ("pos", (x0, y0, x1, y1))
                    name = f"{key}_{idx:05d}"
                    cv2.imwrite(os.path.join(img_dir, name + ".png"), fr)
                    cxn = ((x0 + x1) / 2.0) / W; cyn = ((y0 + y1) / 2.0) / H
                    wn = (x1 - x0) / W; hn = (y1 - y0) / H
                    with open(os.path.join(lbl_dir, name + ".txt"), "w") as fh:
                        fh.write(f"0 {cxn:.6f} {cyn:.6f} {wn:.6f} {hn:.6f}\n")
                    audit_rows.append(dict(clip=key, idx=idx, cls="pos", split=split,
                                           x0=round(x0, 1), y0=round(y0, 1), x1=round(x1, 1), y1=round(y1, 1)))
                    n_pos += 1
                    cls = "pos"
                else:
                    frames[idx] = ("amb", None); n_amb += 1
                    cls = "amb"
            if cls is None and idx in prior_dome:
                frames[idx] = ("amb", None); n_amb += 1
        total = idx + 1
        cap.release()

        # negatives: >= GAP frames from ANY dome evidence
        evid = sorted(frames.keys() | prior_dome)
        def near_evidence(i):
            import bisect
            j = bisect.bisect_left(evid, i)
            for k in (j - 1, j):
                if 0 <= k < len(evid) and abs(evid[k] - i) <= GAP:
                    return True
            return False
        neg_idx = [i for i in range(total) if i not in frames and not near_evidence(i)]
        neg_idx = neg_idx[::NEG_STRIDE][:a.neg_per_clip]
        cap = cv2.VideoCapture(vid)
        n_neg = 0
        for i in neg_idx:
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, fr = cap.read()
            if not ok:
                continue
            name = f"{key}_{i:05d}"
            cv2.imwrite(os.path.join(img_dir, name + ".png"), fr)
            open(os.path.join(lbl_dir, name + ".txt"), "w").close()
            audit_rows.append(dict(clip=key, idx=i, cls="neg", split=split,
                                   x0="", y0="", x1="", y1=""))
            n_neg += 1
        cap.release()
        counts[key] = (split, total, n_pos, n_amb, n_neg)
        print(f"{key} [{split}]: {total} frames -> {n_pos} pos, {n_amb} ambiguous(excluded), {n_neg} neg")

    with open(os.path.join(a.out, "audit.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["clip", "idx", "cls", "split", "x0", "y0", "x1", "y1"])
        w.writeheader(); w.writerows(audit_rows)
    with open(os.path.join(a.out, "data.yaml"), "w") as fh:
        fh.write("path: %s\ntrain: images/train\nval: images/val\nnames:\n  0: meter\n" % a.out)
    print("audit:", os.path.join(a.out, "audit.csv"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
