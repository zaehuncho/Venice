#!/usr/bin/env python
"""SIM-TO-REAL validation for the sub-pixel METER READER (models/meter_reader.pt).

Runs the live-faithful MeterDetector (via replay_framedump.build_detector) over the real
framedump sessions and captures, per frame and WITHOUT editing meter_detector.py:
  * reader_crop   -- the EXACT located crop the serving path would hand meter_reader_infer
                     (captured by an instance-level spy installed on det._reader that returns
                     None, so the served fill stays the classical row-counted value).
  * track_fill    -- the pre-override ROW-COUNTED fill from _measure_track (instance wrapper).
  * served fill   -- res.fill_pct (post stability validator; what actually feeds velocity/tip).

Then it runs the trained reader on every captured crop and asks the launch question:
  does the CNN fill track the real rising meter SMOOTHLY at sub-1% resolution, vs the
  ~1-2% row-count staircase?

Metrics:
  * agreement MAE  |model_fill - row_count_fill|  (crop-semantics correct on REAL crops)
  * row-count resolution  = median gap between adjacent DISTINCT row-count fill values in a rise
  * local-linearity residual RMS (row-count vs model) over sliding windows of a steady rise
    -> the smoothness / effective-resolution win.

Read-only w.r.t. all tracked source. Writes only under logs/diagnostics/ (JSONL + a PNG).

USAGE:
  ORION_METER_READER unset is fine (we install our own spy).
  py tools/diagnostics/validate_meter_reader_real.py --session <dir> --start 0 --end 1326 --step 1
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(_HERE))
for _p in (REPO, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# replay_framedump sets the session locator env at import and gives us a faithful build.
import replay_framedump as RF  # noqa: E402
import cv2  # noqa: E402


class _CropSpy:
    """Stands in for det._reader. Records the exact reader_crop the serving path builds and
    returns None so the detector keeps its classical row-counted fill (we score the model
    ourselves, offline, on the captured crop)."""

    enabled = True

    def __init__(self):
        self.last = None

    def read(self, crop):
        try:
            self.last = crop.copy()
        except Exception:
            self.last = None
        return None


def _install_spies(det):
    spy = _CropSpy()
    det._reader = spy
    orig_mt = det._measure_track  # bound method

    def _mt_spy(frame, match):
        tm = orig_mt(frame, match)
        try:
            _mt_spy.last_fill = float(tm.fill_pct)
        except Exception:
            _mt_spy.last_fill = None
        return tm

    _mt_spy.last_fill = None
    det._measure_track = _mt_spy  # instance attr shadows the class method
    return spy, _mt_spy


def collect(session_dir, start, end, step):
    det, info = RF.build_detector()
    RF._install_clock()
    spy, mt_spy = _install_spies(det)
    frames = RF.list_frames(session_dir)
    if not frames:
        raise SystemExit(f"no frames in {session_dir}")
    max_idx = max(frames)
    end = max_idx if end < 0 else min(end, max_idx)
    rows = []
    crops = {}
    for i in range(start, end + 1, step):
        if i not in frames:
            continue
        bgr = cv2.imread(frames[i][0], cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        spy.last = None
        mt_spy.last_fill = None
        RF._CLOCK.tick()
        res = det.detect(bgr)
        rec = {
            "idx": i,
            "detected": bool(res.detected),
            "served_fill": round(float(res.fill_pct), 4),
            "track_fill": (round(float(mt_spy.last_fill), 4) if mt_spy.last_fill is not None else None),
            "has_crop": spy.last is not None,
            "bbox": [int(v) for v in (res.bbox if res.bbox else (0, 0, 0, 0))],
            "zone": det.last_debug.get("zone", ""),
            "reject": res.rejection_reason,
        }
        rows.append(rec)
        if spy.last is not None:
            crops[i] = spy.last
    return det, info, rows, crops


def run_model(crops):
    """Run the trained reader on captured crops. Returns {idx: fill_pct}."""
    from meter_reader_infer import MeterReader
    rd = MeterReader()
    if not rd.enabled:
        raise SystemExit("MeterReader disabled (model missing?) -- train first")
    out = {}
    for idx, crop in crops.items():
        r = rd.read(crop)
        out[idx] = (float(r["fill_pct"]) if r else None)
    return rd, out


def find_rising_segments(rows, model_fill, min_len=8, min_span=15.0):
    """Contiguous runs of detected frames with a located crop whose row-count fill rises overall."""
    segs = []
    cur = []
    for r in rows:
        ok = r["detected"] and r["has_crop"] and r["track_fill"] is not None \
            and model_fill.get(r["idx"]) is not None
        if ok:
            cur.append(r["idx"])
        else:
            if len(cur) >= min_len:
                segs.append(cur)
            cur = []
    if len(cur) >= min_len:
        segs.append(cur)
    # keep segments that actually sweep a meaningful rise
    keep = []
    for s in segs:
        tf = [next(rr["track_fill"] for rr in rows if rr["idx"] == i) for i in s]
        if (max(tf) - min(tf)) >= min_span:
            keep.append(s)
    return keep


def rise_core(idxs, track):
    """Isolate the monotonic RISE inside a segment (low fill -> first peak): where tip timing
    happens. Returns the index-slice [a,b] of `idxs` covering rise start -> first frame at
    >=90% of the segment peak. The flat post-shot tail (row-count ~50%) is excluded."""
    t = np.asarray(track, float)
    if len(t) < 3:
        return 0, len(t)
    pk = int(np.argmax(t))
    peak = t[pk]
    # rise start = last index at/before pk that is <= 40% of peak (or the segment start)
    a = 0
    for k in range(pk, -1, -1):
        if t[k] <= max(0.4 * peak, t.min() + 5.0):
            a = k
            break
    b = pk + 1
    return a, max(a + 2, b)


def local_linear_residual(idxs, vals, win=9):
    """RMS residual of vals to a locally-fitted line over sliding windows (a steady rise is
    ~linear over a short span, so this residual is the jitter/quantization, not the trend)."""
    idxs = np.asarray(idxs, float)
    vals = np.asarray(vals, float)
    res = []
    n = len(vals)
    if n < win:
        win = n
    for c in range(n):
        lo = max(0, c - win // 2)
        hi = min(n, lo + win)
        lo = max(0, hi - win)
        xs = idxs[lo:hi]
        ys = vals[lo:hi]
        if len(xs) >= 3 and np.ptp(xs) > 0:
            a, b = np.polyfit(xs, ys, 1)
            res.append(ys[c - lo] - (a * idxs[c] + b))
    return float(np.sqrt(np.mean(np.square(res)))) if res else float("nan")


def distinct_gap(vals):
    """Median gap between adjacent DISTINCT values -> the quantization granularity."""
    u = np.unique(np.round(np.asarray(vals, float), 3))
    if len(u) < 2:
        return float("nan")
    return float(np.median(np.diff(u)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=os.path.join(
        REPO, "logs", "diagnostics", "framedump", "session_20260704_210801"))
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int, default=-1)
    ap.add_argument("--step", type=int, default=1)
    ap.add_argument("--win", type=int, default=9, help="sliding window for local-linear residual")
    ap.add_argument("--out", default=os.path.join(REPO, "logs", "diagnostics", "reader_sim2real"))
    ap.add_argument("--plot", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    det, info, rows, crops = collect(args.session, args.start, args.end, args.step)
    print("=== DETECTOR BUILD ===")
    for k, v in info.items():
        print(f"  {k:16s} {v}")
    n_det = sum(r["detected"] for r in rows)
    print(f"\nframes={len(rows)}  detected={n_det}  located_crops={len(crops)}  session={os.path.basename(args.session)}")

    rd, model_fill = run_model(crops)
    # overall agreement on all located frames
    pairs = [(r["track_fill"], model_fill[r["idx"]]) for r in rows
             if r["has_crop"] and r["track_fill"] is not None and model_fill.get(r["idx"]) is not None]
    if pairs:
        tf = np.array([p[0] for p in pairs]); mf = np.array([p[1] for p in pairs])
        agree_mae = float(np.mean(np.abs(mf - tf)))
        agree_med = float(np.median(np.abs(mf - tf)))
        bias = float(np.mean(mf - tf))
        print(f"\n=== AGREEMENT on {len(pairs)} located crops (model vs row-count) ===")
        print(f"  MAE={agree_mae:.3f}%  median|err|={agree_med:.3f}%  bias(model-row)={bias:+.3f}%")

    segs = find_rising_segments(rows, model_fill)
    print(f"\n=== RISING SEGMENTS: {len(segs)} ===")
    by_idx = {r["idx"]: r for r in rows}
    agg = {"row_res": [], "mod_res": [], "row_gap": [], "seg_mae": []}
    ragg = {"row_res": [], "mod_res": [], "row_gap": [], "mae": []}  # RISE-core only
    seg_recs = []
    for si, s in enumerate(segs):
        tf = [by_idx[i]["track_fill"] for i in s]
        mf = [model_fill[i] for i in s]
        row_res = local_linear_residual(s, tf, args.win)
        mod_res = local_linear_residual(s, mf, args.win)
        gap = distinct_gap(tf)
        mae = float(np.mean(np.abs(np.array(mf) - np.array(tf))))
        # rise-core (exclude the flat post-shot tail)
        a, b = rise_core(s, tf)
        rs, rtf, rmf = s[a:b], tf[a:b], mf[a:b]
        r_rowres = local_linear_residual(rs, rtf, min(args.win, len(rs)))
        r_modres = local_linear_residual(rs, rmf, min(args.win, len(rs)))
        r_gap = distinct_gap(rtf)
        r_mae = float(np.mean(np.abs(np.array(rmf) - np.array(rtf))))
        agg["row_res"].append(row_res); agg["mod_res"].append(mod_res)
        agg["row_gap"].append(gap); agg["seg_mae"].append(mae)
        ragg["row_res"].append(r_rowres); ragg["mod_res"].append(r_modres)
        ragg["row_gap"].append(r_gap); ragg["mae"].append(r_mae)
        seg_recs.append({"seg": si, "idx0": s[0], "idx1": s[-1], "n": len(s),
                         "fill_span": round(max(tf) - min(tf), 2),
                         "row_gap_pct": round(gap, 3), "row_linres_pct": round(row_res, 3),
                         "model_linres_pct": round(mod_res, 3), "model_vs_row_mae_pct": round(mae, 3),
                         "rise_idx0": rs[0], "rise_idx1": rs[-1], "rise_n": len(rs),
                         "rise_row_linres_pct": round(r_rowres, 3), "rise_model_linres_pct": round(r_modres, 3),
                         "rise_mae_pct": round(r_mae, 3)})
        print(f"  seg{si:02d} f{s[0]}-{s[-1]} n={len(s):3d} span={max(tf)-min(tf):5.1f}%"
              f" | rise f{rs[0]}-{rs[-1]}: row_gap={r_gap:.2f}% row_linres={r_rowres:.2f}%"
              f" model_linres={r_modres:.2f}% mae={r_mae:.2f}%")

    def _mn(x):
        x = [v for v in x if v == v]
        return float(np.mean(x)) if x else float("nan")

    print("\n=== SUMMARY: RISE PHASE (tip-timing window; flat post-shot tail excluded) ===")
    print(f"  row-count quantization gap   : {_mn(ragg['row_gap']):.3f} %")
    print(f"  row-count local-linear resid : {_mn(ragg['row_res']):.3f} % (staircase jitter)")
    print(f"  MODEL     local-linear resid : {_mn(ragg['mod_res']):.3f} % (sub-pixel smoothness)")
    if _mn(ragg['mod_res']) > 0:
        print(f"  smoothness improvement       : {_mn(ragg['row_res'])/_mn(ragg['mod_res']):.2f}x lower residual")
    print(f"  model<->row agreement MAE     : {_mn(ragg['mae']):.3f} %")
    print("\n=== SUMMARY: WHOLE located segment (incl. post-shot tail) ===")
    print(f"  row-count local-linear resid : {_mn(agg['row_res']):.3f} %")
    print(f"  MODEL     local-linear resid : {_mn(agg['mod_res']):.3f} %")
    print(f"  model<->row agreement MAE     : {_mn(agg['seg_mae']):.3f} %")

    out_json = os.path.join(args.out, "reader_sim2real.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"info": info, "n_frames": len(rows), "n_detected": n_det,
                   "n_located_crops": len(crops),
                   "agreement_mae_pct": (agree_mae if pairs else None),
                   "agreement_bias_pct": (bias if pairs else None),
                   "segments": seg_recs,
                   "rise_mean_row_gap_pct": _mn(ragg['row_gap']),
                   "rise_mean_row_linres_pct": _mn(ragg['row_res']),
                   "rise_mean_model_linres_pct": _mn(ragg['mod_res']),
                   "rise_mean_model_vs_row_mae_pct": _mn(ragg['mae']),
                   "whole_mean_row_linres_pct": _mn(agg['row_res']),
                   "whole_mean_model_linres_pct": _mn(agg['mod_res']),
                   "whole_mean_model_vs_row_mae_pct": _mn(agg['seg_mae'])}, f, indent=2)
    print(f"\nwrote {out_json}")

    if args.plot and segs:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            k = min(6, len(segs))
            fig, axes = plt.subplots(k, 1, figsize=(11, 2.6 * k), squeeze=False)
            for r, s in enumerate(segs[:k]):
                ax = axes[r][0]
                tf = [by_idx[i]["track_fill"] for i in s]
                mf = [model_fill[i] for i in s]
                ax.step(s, tf, where="mid", color="#d1495b", label="row-count (classical)", linewidth=1.4)
                ax.plot(s, mf, color="#2e86ab", label="CNN sub-pixel", linewidth=1.6)
                ax.set_ylabel("fill %"); ax.set_title(f"seg{r} f{s[0]}-{s[-1]}", fontsize=9)
                if r == 0:
                    ax.legend(fontsize=8, loc="upper left")
            axes[-1][0].set_xlabel("frame idx")
            fig.tight_layout()
            png = os.path.join(args.out, "reader_sim2real.png")
            fig.savefig(png, dpi=110)
            print(f"wrote {png}")
        except Exception as exc:
            print(f"(plot skipped: {exc})")


if __name__ == "__main__":
    main()
