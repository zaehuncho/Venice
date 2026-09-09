#!/usr/bin/env python
"""Detector GEOMETRY parity gate: does a candidate meter detector draw the SAME box as the
reference, or has it silently moved the fill ruler?

Why this exists (2026-09-08): the n4 low-fill fine-tune (meter2k27_n4_lowfill) was trained
on hard-set labels copied from frames.csv bbox_* -- the reader's padded, max-held OVERLAY
box (~120 px @720p) -- instead of the detector's own box (~107 px). It learned exactly that:
boxes 14 px taller, top-y -7 px, bottom +7 px, within-fill-band height sd 2-5x the shipped
n3. The reader divides fill by the detector box height, so the fill ruler moved ~13 % and
every read got noisier -- and no mAP/recall number could have caught it, because the boxes
still overlap the meter at IoU ~0.8.

Method: take the frames of a framedump session where the pipeline saw the meter
(frames.csv detected=1), sample up to N spread evenly across fill 5-95 %, run BOTH ONNX
models through the production MeterYoloLocator (same letterbox / plausibility gate / conf),
and compare box geometry on frames where the reference detects:

  * median h and w (p10/p90) per model, and the median top-y / bottom-y shift (paired)
  * per-fill-band WITHIN-band h sd per model (box height must not depend on fill)
  * recall each way (candidate detects where reference does, and vice versa) plus each
    model's plain detection rate on the sampled pool

Verdict thresholds (--th-*): median h within +-1 px, median top-y within +-1 px, within-band
h sd <= 1.25x the reference in every band, candidate recall >= reference recall - 1 pp.

Usage:
  python tools/diagnostics/detector_geometry_parity.py \
      --ref  runs/detect/logs/diagnostics/meter_train/meter2k27_n3_pill/weights/best.onnx \
      --cand runs/detect/logs/diagnostics/meter_train/meter2k27_n4_lowfill/weights/best.onnx \
      [--session logs/diagnostics/framedump/session_20260903_151910] [--n 300] [--json out.json]

Exit code 0 on PASS, 1 on FAIL, 2 on a setup error. The verdict logic (evaluate_parity) is
pure and unit-tested in tests/test_detector_geometry_parity.py without loading any model.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_SESSION = os.path.join(REPO, "logs", "diagnostics", "framedump", "session_20260903_151910")
DEFAULT_REF = os.path.join(REPO, "runs", "detect", "logs", "diagnostics", "meter_train",
                           "meter2k27_n3_pill", "weights", "best.onnx")

Box = Optional[Tuple[int, int, int, int, float]]   # (x, y, w, h, conf) or None

DEFAULT_THRESHOLDS = dict(
    median_h_px=1.0,        # |median h(cand) - median h(ref)| <= this
    median_top_px=1.0,      # |median (top_cand - top_ref)| on paired frames <= this
    band_sd_ratio=1.25,     # sd_cand <= ratio * max(sd_ref, sd_floor) in EVERY band
    sd_floor_px=0.5,        # protects against a reference band with sd ~0
    recall_drop_pp=1.0,     # cand detection rate >= ref detection rate - this
    min_band_n=8,           # bands with fewer paired frames than this are reported, not judged
)
DEFAULT_BANDS = (5, 15, 25, 35, 45, 55, 65, 75, 85, 95)


def _q(a: Sequence[float]) -> Dict[str, float]:
    arr = np.asarray(list(a), dtype=float)
    if arr.size == 0:
        return dict(n=0, median=float("nan"), p10=float("nan"), p90=float("nan"), sd=float("nan"))
    return dict(n=int(arr.size), median=float(np.median(arr)),
                p10=float(np.percentile(arr, 10)), p90=float(np.percentile(arr, 90)),
                sd=float(arr.std(ddof=0)))


def evaluate_parity(ref_boxes: Sequence[Box], cand_boxes: Sequence[Box], fills: Sequence[float],
                    thresholds: Optional[Dict[str, float]] = None,
                    bands: Sequence[float] = DEFAULT_BANDS) -> Dict:
    """Pure verdict logic. All three sequences are aligned by frame index.

    Geometry (h/w medians, per-band sd) is measured on frames where the REFERENCE detects
    (the candidate's own numbers use the candidate box on those same frames); the top-y
    shift is paired (both detect). Recall is each model's detection rate on the whole pool,
    plus the cross recalls.
    """
    th = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        th.update(thresholds)
    if not (len(ref_boxes) == len(cand_boxes) == len(fills)):
        raise ValueError("ref_boxes, cand_boxes and fills must be aligned")
    n = len(fills)
    ref_det = [b is not None for b in ref_boxes]
    cand_det = [b is not None for b in cand_boxes]
    n_ref = sum(ref_det)
    n_cand = sum(cand_det)
    n_both = sum(1 for r, c in zip(ref_det, cand_det) if r and c)

    ref_h = [b[3] for b in ref_boxes if b is not None]
    ref_w = [b[2] for b in ref_boxes if b is not None]
    cand_h_on_ref = [c[3] for r, c in zip(ref_boxes, cand_boxes) if r is not None and c is not None]
    cand_w_on_ref = [c[2] for r, c in zip(ref_boxes, cand_boxes) if r is not None and c is not None]
    cand_h_all = [b[3] for b in cand_boxes if b is not None]
    cand_w_all = [b[2] for b in cand_boxes if b is not None]
    top_shift = [c[1] - r[1] for r, c in zip(ref_boxes, cand_boxes) if r is not None and c is not None]
    bot_shift = [(c[1] + c[3]) - (r[1] + r[3]) for r, c in zip(ref_boxes, cand_boxes)
                 if r is not None and c is not None]
    h_shift = [c[3] - r[3] for r, c in zip(ref_boxes, cand_boxes) if r is not None and c is not None]

    band_rows = []
    edges = list(bands)
    for lo, hi in zip(edges[:-1], edges[1:]):
        rh, ch = [], []
        for r, c, f in zip(ref_boxes, cand_boxes, fills):
            if r is None or not (lo <= f < hi):
                continue
            rh.append(r[3])
            if c is not None:
                ch.append(c[3])
        row = dict(band=f"{lo:g}-{hi:g}", n_ref=len(rh), n_cand=len(ch),
                   ref_h_sd=float(np.std(rh)) if len(rh) >= 2 else float("nan"),
                   cand_h_sd=float(np.std(ch)) if len(ch) >= 2 else float("nan"),
                   ref_h_med=float(np.median(rh)) if rh else float("nan"),
                   cand_h_med=float(np.median(ch)) if ch else float("nan"))
        judged = len(rh) >= th["min_band_n"] and len(ch) >= th["min_band_n"]
        row["judged"] = bool(judged)
        if judged:
            denom = max(row["ref_h_sd"], th["sd_floor_px"])
            limit = th["band_sd_ratio"] * denom
            row["sd_limit"] = float(limit)
            row["ok"] = bool(row["cand_h_sd"] <= limit)
            # a zero-variance reference band with ANY candidate wobble is an infinite ratio
            row["sd_ratio"] = float(row["cand_h_sd"] / denom) if denom > 0 else (
                0.0 if row["cand_h_sd"] == 0 else float("inf"))
        else:
            row["sd_limit"] = float("nan")
            row["sd_ratio"] = float("nan")
            row["ok"] = None
        band_rows.append(row)

    ref_rate = 100.0 * n_ref / n if n else float("nan")
    cand_rate = 100.0 * n_cand / n if n else float("nan")
    ref_stats = dict(h=_q(ref_h), w=_q(ref_w))
    cand_stats = dict(h=_q(cand_h_all), w=_q(cand_w_all), h_on_ref_frames=_q(cand_h_on_ref),
                      w_on_ref_frames=_q(cand_w_on_ref))
    med_h_delta = (cand_stats["h_on_ref_frames"]["median"] - ref_stats["h"]["median"]
                   if cand_h_on_ref and ref_h else float("nan"))
    med_top_shift = float(np.median(top_shift)) if top_shift else float("nan")
    med_bot_shift = float(np.median(bot_shift)) if bot_shift else float("nan")
    med_h_shift = float(np.median(h_shift)) if h_shift else float("nan")

    checks = {}
    checks["median_h"] = dict(value=med_h_delta, limit=th["median_h_px"],
                              ok=bool(np.isfinite(med_h_delta) and abs(med_h_delta) <= th["median_h_px"]))
    checks["median_top_y"] = dict(value=med_top_shift, limit=th["median_top_px"],
                                  ok=bool(np.isfinite(med_top_shift) and abs(med_top_shift) <= th["median_top_px"]))
    judged_bands = [b for b in band_rows if b["judged"]]
    checks["band_h_sd"] = dict(
        value=max((b["sd_ratio"] for b in judged_bands), default=float("nan")),
        limit=th["band_sd_ratio"],
        ok=bool(judged_bands) and all(b["ok"] for b in judged_bands),
        failing_bands=[b["band"] for b in judged_bands if not b["ok"]],
        judged_bands=len(judged_bands))
    checks["recall"] = dict(value=cand_rate - ref_rate, limit=-th["recall_drop_pp"],
                            ok=bool(np.isfinite(cand_rate) and cand_rate >= ref_rate - th["recall_drop_pp"]))
    verdict = "PASS" if all(c["ok"] for c in checks.values()) else "FAIL"
    return dict(
        n_frames=n, n_ref_det=n_ref, n_cand_det=n_cand, n_both=n_both,
        ref_rate_pct=ref_rate, cand_rate_pct=cand_rate,
        cand_recall_on_ref_pct=(100.0 * n_both / n_ref) if n_ref else float("nan"),
        ref_recall_on_cand_pct=(100.0 * n_both / n_cand) if n_cand else float("nan"),
        ref=ref_stats, cand=cand_stats,
        paired=dict(n=len(top_shift), top_y_shift_med=med_top_shift, bottom_y_shift_med=med_bot_shift,
                    h_shift_med=med_h_shift,
                    top_y_shift=_q(top_shift), h_shift=_q(h_shift)),
        bands=band_rows, checks=checks, thresholds=th, verdict=verdict)


def sample_frames(session: str, n: int, fill_lo: float = 5.0, fill_hi: float = 95.0,
                  bands: Sequence[float] = DEFAULT_BANDS, seed: int = 1234) -> List[Dict]:
    """frames.csv rows with detected=1 and fill in [lo, hi], sampled evenly across bands."""
    path = os.path.join(session, "frames.csv")
    rows = list(csv.DictReader(open(path, newline="")))
    pool = []
    for r in rows:
        try:
            if int(r["detected"]) != 1:
                continue
            f = float(r["fill_pct"])
        except (KeyError, ValueError):
            continue
        if not (fill_lo <= f <= fill_hi):
            continue
        img = os.path.join(session, f"f{int(r['idx']):05d}_1_raw.png")
        if not os.path.isfile(img):
            continue
        pool.append(dict(idx=int(r["idx"]), fill=f, path=img, csv_bbox_h=int(float(r.get("bbox_h", 0) or 0))))
    if len(pool) <= n:
        return sorted(pool, key=lambda r: r["idx"])
    rng = np.random.default_rng(seed)
    edges = list(bands)
    buckets = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        b = [r for r in pool if lo <= r["fill"] < hi or (hi == edges[-1] and r["fill"] == hi)]
        rng.shuffle(b)
        buckets.append(b)
    out: List[Dict] = []
    # round-robin across bands so thin bands keep everything they have
    while len(out) < n and any(buckets):
        for b in buckets:
            if b and len(out) < n:
                out.append(b.pop())
    return sorted(out, key=lambda r: r["idx"])


def run_models(ref_path: str, cand_path: str, frames: List[Dict], conf: float = 0.35):
    os.environ.setdefault("ORION_METER_DETECTOR", "1")
    sys.path.insert(0, REPO)
    import cv2  # noqa: E402
    from meter_detector_yolo import MeterYoloLocator  # noqa: E402
    ref = MeterYoloLocator(model_path=ref_path, conf_thres=conf)
    cand = MeterYoloLocator(model_path=cand_path, conf_thres=conf)
    if not ref.ok:
        raise SystemExit(f"reference model failed to load: {ref_path}")
    if not cand.ok:
        raise SystemExit(f"candidate model failed to load: {cand_path}")
    ref_boxes, cand_boxes, fills = [], [], []
    for fr in frames:
        img = cv2.imread(fr["path"])
        if img is None:
            continue
        ref_boxes.append(ref.detect_box(img))
        cand_boxes.append(cand.detect_box(img))
        fills.append(fr["fill"])
    meta = dict(ref=dict(path=ref_path, provider=ref.provider, imgsz=ref.imgsz),
                cand=dict(path=cand_path, provider=cand.provider, imgsz=cand.imgsz), conf=conf)
    return ref_boxes, cand_boxes, fills, meta


def format_report(res: Dict, meta: Optional[Dict] = None, session: str = "") -> str:
    L = []
    if meta:
        L.append(f"reference : {meta['ref']['path']}  ({meta['ref']['provider']}, imgsz {meta['ref']['imgsz']})")
        L.append(f"candidate : {meta['cand']['path']}  ({meta['cand']['provider']}, imgsz {meta['cand']['imgsz']})")
    if session:
        L.append(f"session   : {session}")
    L.append(f"frames    : {res['n_frames']} sampled (pipeline detected=1, fill 5-95)  "
             f"ref detects {res['n_ref_det']} ({res['ref_rate_pct']:.1f}%)  "
             f"cand detects {res['n_cand_det']} ({res['cand_rate_pct']:.1f}%)  both {res['n_both']}")
    L.append(f"recall    : cand-on-ref {res['cand_recall_on_ref_pct']:.1f}%   ref-on-cand {res['ref_recall_on_cand_pct']:.1f}%")
    rh, rw = res["ref"]["h"], res["ref"]["w"]
    ch, cw = res["cand"]["h_on_ref_frames"], res["cand"]["w_on_ref_frames"]
    L.append(f"ref  box  : h med {rh['median']:.1f} (p10 {rh['p10']:.1f} p90 {rh['p90']:.1f})  "
             f"w med {rw['median']:.1f} (p10 {rw['p10']:.1f} p90 {rw['p90']:.1f})")
    L.append(f"cand box  : h med {ch['median']:.1f} (p10 {ch['p10']:.1f} p90 {ch['p90']:.1f})  "
             f"w med {cw['median']:.1f} (p10 {cw['p10']:.1f} p90 {cw['p90']:.1f})   [on ref-detected frames]")
    p = res["paired"]
    L.append(f"paired    : n {p['n']}  top-y shift med {p['top_y_shift_med']:+.1f} px  "
             f"bottom-y shift med {p['bottom_y_shift_med']:+.1f} px  h shift med {p['h_shift_med']:+.1f} px")
    L.append("per-band within-band h sd (px):")
    L.append(f"  {'band':>7s} {'n_ref':>5s} {'n_cand':>6s} {'ref_sd':>7s} {'cand_sd':>7s} {'limit':>6s}  ok")
    for b in res["bands"]:
        ok = "-" if b["ok"] is None else ("ok" if b["ok"] else "FAIL")
        L.append(f"  {b['band']:>7s} {b['n_ref']:>5d} {b['n_cand']:>6d} {b['ref_h_sd']:>7.2f} {b['cand_h_sd']:>7.2f} "
                 f"{b['sd_limit']:>6.2f}  {ok}")
    L.append("checks:")
    c = res["checks"]
    L.append(f"  median h delta   {c['median_h']['value']:+.2f} px   (|.| <= {c['median_h']['limit']:.1f})   "
             f"{'ok' if c['median_h']['ok'] else 'FAIL'}")
    L.append(f"  median top-y     {c['median_top_y']['value']:+.2f} px   (|.| <= {c['median_top_y']['limit']:.1f})   "
             f"{'ok' if c['median_top_y']['ok'] else 'FAIL'}")
    L.append(f"  band h sd ratio  {c['band_h_sd']['value']:.2f}x  (<= {c['band_h_sd']['limit']:.2f}x in every judged band; "
             f"{c['band_h_sd']['judged_bands']} judged)   "
             f"{'ok' if c['band_h_sd']['ok'] else 'FAIL ' + ','.join(c['band_h_sd']['failing_bands'])}")
    L.append(f"  recall delta     {c['recall']['value']:+.2f} pp   (>= {c['recall']['limit']:+.1f})   "
             f"{'ok' if c['recall']['ok'] else 'FAIL'}")
    L.append(f"VERDICT: {res['verdict']}")
    return "\n".join(L)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ref", default=DEFAULT_REF, help="reference ONNX (the shipped geometry)")
    ap.add_argument("--cand", required=True, help="candidate ONNX")
    ap.add_argument("--session", default=DEFAULT_SESSION, help="framedump session dir with frames.csv")
    ap.add_argument("--n", type=int, default=300, help="max frames to sample (spread across fill bands)")
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--json", default="", help="write the full result JSON here")
    for k, v in DEFAULT_THRESHOLDS.items():
        ap.add_argument(f"--th-{k.replace('_', '-')}", type=float, default=v, dest=f"th_{k}")
    a = ap.parse_args(argv)
    if not os.path.isfile(os.path.join(a.session, "frames.csv")):
        print(f"no frames.csv in {a.session}", file=sys.stderr)
        return 2
    frames = sample_frames(a.session, a.n, seed=a.seed)
    if not frames:
        print("no eligible frames (detected=1, fill 5-95) in session", file=sys.stderr)
        return 2
    ref_boxes, cand_boxes, fills, meta = run_models(a.ref, a.cand, frames, conf=a.conf)
    th = {k: getattr(a, f"th_{k}") for k in DEFAULT_THRESHOLDS}
    res = evaluate_parity(ref_boxes, cand_boxes, fills, th)
    print(format_report(res, meta, a.session))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(dict(meta=meta, session=a.session, result=res), fh, indent=1, default=float)
        print("json:", a.json)
    return 0 if res["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
