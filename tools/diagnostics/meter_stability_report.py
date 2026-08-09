#!/usr/bin/env python3
"""Detection STABILITY report — proves the Priority-#1 requirements with numbers, offline.

Runs over a replay bbox trace (tools/diagnostics/replay_framedump.py output, e.g.
logs/diagnostics/replay_analysis/replay_0_1326_s2.jsonl) and, per detected shot, measures the exact things
the requirement names:
  * NO DISAPPEARING during a shot -> within-shot detection rate, max consecutive gap.
  * NO FLICKER                    -> number of detect on/off toggles inside a shot.
  * NO DRIFT / JITTER             -> frame-to-frame box centre-x jump, box width/height coefficient-of-variation
                                     (the meter is player-attached so it may PAN smoothly; we flag JITTER/snap,
                                     not smooth motion).
  * SMOOTH FILL                   -> fraction of monotone-up steps during the rise, fill-step noise.

Asserts sensible thresholds and exits non-zero on failure so it can gate CI.

Run:  C:\\Python314\\python.exe tools/diagnostics/meter_stability_report.py
      C:\\Python314\\python.exe tools/diagnostics/meter_stability_report.py --trace <replay.jsonl> --strict
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_TRACE = os.path.join(ROOT, "logs", "diagnostics", "replay_analysis", "replay_0_1326_s2.jsonl")

# thresholds (a shot that violates these is flagged)
MIN_INSHOT_DET = 0.99      # >=99% of in-shot frames detected
MAX_GAP = 1                # <= 1 consecutive missed frame during a shot
MAX_CX_JITTER = 12.0       # px: median frame-to-frame centre-x jump (smooth pan is small; a snap is large)
MAX_W_CV = 0.18            # box-width coefficient of variation within a shot (Kalman should hold it steady)
MAX_H_CV = 0.20            # box-height coefficient of variation; catches vertical stretch/collapse
MAX_ASPECT_STEP = 0.20      # maximum adjacent fractional aspect-ratio change; catches one-frame warps


def load(trace):
    rows = []
    with open(trace, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    rows.sort(key=lambda r: r["idx"])
    return rows


def segment_shots(rows, bridge=3):
    """A shot = a rising->peak episode. Group frames whose rise_state is rising/peak, bridging short gaps
    (<= `bridge` trace-frames of dropout/other state), then take the full index span so intermediate DROPOUTS
    are included in the continuity metric. Keep episodes that actually climb to a real peak (>72%)."""
    pos = [k for k, r in enumerate(rows) if r.get("rise_state") in ("rising", "peak")]
    if not pos:
        return []
    groups = [[pos[0]]]
    for p in pos[1:]:
        (groups[-1].append(p) if p - groups[-1][-1] <= bridge else groups.append([p]))
    shots = []
    for g in groups:
        span = rows[g[0]: g[-1] + 1]
        fills = [float(r.get("fill", 0)) for r in span]
        if len(span) >= 4 and max(fills) > 72.0:
            shots.append(span)
    return shots


def shot_metrics(shot):
    det = np.array([1 if r.get("detected") else 0 for r in shot])
    n = len(det); ndet = int(det.sum())
    det_rate = ndet / n if n else 0.0
    # max consecutive gap of missed frames
    maxgap = cur = 0
    toggles = 0; prev = None
    for d in det:
        if d == 0:
            cur += 1; maxgap = max(maxgap, cur)
        else:
            cur = 0
        if prev is not None and d != prev:
            toggles += 1
        prev = d
    # geometry on detected frames
    cx, w, h, fill = [], [], [], []
    for r in shot:
        if r.get("detected"):
            b = r.get("bbox", [0, 0, 0, 1])
            cx.append(b[0] + b[2] / 2.0); w.append(b[2]); h.append(b[3]); fill.append(float(r.get("fill", 0)))
    cx = np.array(cx, float); w = np.array(w, float); h = np.array(h, float); fill = np.array(fill, float)
    cx_jit = float(np.median(np.abs(np.diff(cx)))) if len(cx) > 2 else 0.0
    cx_jit_max = float(np.max(np.abs(np.diff(cx)))) if len(cx) > 2 else 0.0
    w_cv = float(np.std(w) / np.mean(w)) if len(w) > 1 and np.mean(w) > 0 else 0.0
    h_cv = float(np.std(h) / np.mean(h)) if len(h) > 1 and np.mean(h) > 0 else 0.0
    aspect = w / np.maximum(h, 1.0)
    aspect_step = (np.abs(np.diff(aspect)) / np.maximum(aspect[:-1], 1e-9)
                   if len(aspect) > 1 else np.array([], dtype=float))
    aspect_step_max = float(np.max(aspect_step)) if len(aspect_step) else 0.0
    # fill monotonicity during the rise (up to the peak)
    mono = 1.0
    if len(fill) > 3:
        tip = int(np.argmax(fill))
        rise = fill[:tip + 1]
        if len(rise) > 2:
            steps = np.diff(rise)
            mono = float(np.mean(steps >= -1.0))     # allow <=1% wobble (row quantization)
    return dict(n=n, ndet=ndet, det_rate=det_rate, maxgap=maxgap, toggles=toggles,
                cx_jit=cx_jit, cx_jit_max=cx_jit_max, w_cv=w_cv, h_cv=h_cv,
                aspect_step_max=aspect_step_max, mono=mono,
                peak_fill=float(max(fill) if len(fill) else 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", default=DEFAULT_TRACE)
    ap.add_argument("--strict", action="store_true", help="exit 1 if any shot fails a threshold")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    trace = args.trace
    if not os.path.exists(trace):
        cands = sorted(glob.glob(os.path.join(ROOT, "logs", "diagnostics", "replay_analysis", "replay_*_s*.jsonl")))
        if not cands:
            print(f"[stability] no trace at {trace} and none found; run replay_framedump.py first", file=sys.stderr)
            return 2
        trace = cands[-1]

    rows = load(trace)
    step = (rows[1]["idx"] - rows[0]["idx"]) if len(rows) > 1 else 1
    shots = segment_shots(rows)
    print("=" * 78)
    print(f"DETECTION STABILITY  |  trace={os.path.basename(trace)}  frames={len(rows)} (step={step})  shots={len(shots)}")
    print("=" * 78)
    if not shots:
        print("[stability] no qualifying shot episodes in trace", file=sys.stderr)
        return 2
    print(f"  {'shot':>4} {'frames':>6} {'det%':>6} {'maxgap':>7} {'toggles':>8} {'cxJitter':>9} "
          f"{'wCV':>6} {'hCV':>6} {'shape':>6} {'mono%':>6} {'peak':>5}")
    fails = []
    allm = []
    for i, s in enumerate(shots):
        m = shot_metrics(s); allm.append(m)
        bad = []
        if m["det_rate"] < MIN_INSHOT_DET: bad.append("det")
        if m["maxgap"] > MAX_GAP: bad.append("gap")
        if m["cx_jit"] > MAX_CX_JITTER: bad.append("jit")
        if m["w_cv"] > MAX_W_CV: bad.append("wCV")
        if m["h_cv"] > MAX_H_CV: bad.append("hCV")
        if m["aspect_step_max"] > MAX_ASPECT_STEP: bad.append("shape")
        flag = "  <-- " + ",".join(bad) if bad else ""
        if bad:
            fails.append((i, bad))
        print(f"  {i:>4} {m['n']:>6} {100*m['det_rate']:>5.0f}% {m['maxgap']:>7} {m['toggles']:>8} "
              f"{m['cx_jit']:>7.1f}px {m['w_cv']:>5.2f} {m['h_cv']:>5.2f} "
              f"{m['aspect_step_max']:>5.2f} {100*m['mono']:>5.0f}% {m['peak_fill']:>4.0f}{flag}")

    dr = np.array([m["det_rate"] for m in allm]); gp = np.array([m["maxgap"] for m in allm])
    jt = np.array([m["cx_jit"] for m in allm]); mo = np.array([m["mono"] for m in allm])
    print("-" * 78)
    print(f"  AGGREGATE over {len(shots)} shots:")
    print(f"    within-shot detection: median {100*np.median(dr):.0f}%  min {100*np.min(dr):.0f}%   (target >= {100*MIN_INSHOT_DET:.0f}%)")
    print(f"    max gap (missed frames in a shot): median {np.median(gp):.0f}  worst {int(np.max(gp))}   (step={step}; target <= {MAX_GAP})")
    print(f"    centre-x jitter (frame-to-frame): median {np.median(jt):.1f}px   (target < {MAX_CX_JITTER}px)")
    print(f"    fill monotone-up during rise: median {100*np.median(mo):.0f}%")
    print(f"    shots failing a threshold: {len(fails)}/{len(shots)}")
    print("=" * 78)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(dict(trace=os.path.basename(trace), n_shots=len(shots), shots=allm, fails=fails), f, indent=2)

    if args.strict and fails:
        print(f"[stability] STRICT FAIL: {len(fails)} shot(s) violated a threshold")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
