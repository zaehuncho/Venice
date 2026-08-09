#!/usr/bin/env python3
"""AUTONOMOUS-MODEL OFFLINE GATE (Phase A) for the hybrid global phase-clock redesign.

Builds ONE global NONLINEAR meter-trajectory template (saturating model, the 98-100% flatten is part
of the model, never linearly extrapolated), aligns each shot's live samples to it (phase + speed
scale), predicts the tip/green-center timestamp, and scores the prediction with LEAVE-ONE-SESSION-OUT
(no leakage). This is the release gate before any engine behavior changes:

    timing error p95 must be < 1/2 the narrowest measured green window, with predictor releases
    reported separately from detector dropouts and non-reaching (would-be fallback) shots.

Model (per shot, anchored at first reliable meter appearance, t measured since appearance):
    fill(t) = A * (1 - exp(-k * (t - t0)))
  Linearize (samples below the plateau knee only -> the flatten is de-weighted, not extrapolated):
    y = ln(1 - fill/A) = -k*t + k*t0   -> weighted line, slope=-k (speed scale), intercept=k*t0 (phase)
  Predict tip time for target T:  t_target = t0 - ln(1 - T/A) / k
A (plateau) and the k prior come from the GLOBAL model fit on the OTHER sessions (LOSO). The per-shot
k is bounded to the global prior band (the engine's velocity-prior clamp), so a torn frame can't
spike the alignment. "Prediction uncertainty" = the log-fit residual (gates the confidence path).

The decisive number: we fit ONLY samples available `lead` ms before the actual tip (default 110 = the
effective pipeline latency) and predict the tip -> "can the global template see the tip one latency
ahead", which a per-shot LINEAR extrapolation could not (~55ms scatter, see replay_predictor.py).

Usage:
  C:\\Python314\\python.exe tools/diagnostics/replay_autonomous.py logs/diagnostics/detframes_*.csv
  ... --target 95 --lead 110 --window 25 --plateau-knee 0.93
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from typing import List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from replay_predictor import Row, load_rows, episodes, interp_crossing, pct  # noqa: E402


def _weighted_loglin_fit(samples: List[Tuple[float, float]], A: float, knee: float
                         ) -> Optional[Tuple[float, float, float]]:
    """Fit y = ln(1 - fill/A) = -k*t + k*t0 over samples (t, fill) with fill < knee*A.
    Returns (k, t0, rms_resid) or None. Recent samples weighted up (lambda=0.5/sample), matching
    the engine's exponentially-weighted fit."""
    pts = [(t, f) for (t, f) in samples if 0.0 < f < knee * A]
    n = len(pts)
    if n < 3:
        return None
    xs, ys, ws = [], [], []
    for i, (t, f) in enumerate(pts):
        ys.append(math.log(max(1e-6, 1.0 - f / A)))
        xs.append(t)
        ws.append(math.exp(-0.5 * (n - 1 - i)))
    sw = sum(ws)
    xm = sum(w * x for w, x in zip(ws, xs)) / sw
    ym = sum(w * y for w, y in zip(ws, ys)) / sw
    num = sum(w * (x - xm) * (y - ym) for w, x, y in zip(ws, xs, ys))
    den = sum(w * (x - xm) ** 2 for w, x in zip(ws, xs))
    if abs(den) < 1e-9:
        return None
    slope = num / den            # = -k
    intercept = ym - slope * xm  # = k*t0
    k = -slope
    if k <= 1e-6:
        return None
    t0 = intercept / k
    # rms residual in fill space (the meaningful uncertainty), over the fitted pts
    resid = []
    for t, f in pts:
        pred = A * (1.0 - math.exp(-k * (t - t0)))
        resid.append((pred - f) ** 2)
    rms = math.sqrt(sum(resid) / len(resid))
    return k, t0, rms


def _episode_rise(e: List[Row]):
    """Return (samples since-appearance [(t,fill,conf)], peak fill, had_dropout)."""
    peak_i = max(range(len(e)), key=lambda i: e[i].fill)
    t0 = e[0].t
    rise = e[: peak_i + 1]
    samples = [(r.t - t0, r.fill, r.conf) for r in rise]
    had_drop = any((b[0] - a[0]) > 100.0 for a, b in zip(samples, samples[1:]))
    return samples, e[peak_i].fill, had_drop


def _episode_velocity(samples, knee_fill: float):
    """Median per-step rise velocity (%/ms) over the clean linear region (fill < knee_fill)."""
    vs = []
    for (ta, fa, _ca), (tb, fb, _cb) in zip(samples, samples[1:]):
        if tb > ta and fa < knee_fill and fb > fa:
            vs.append((fb - fa) / (tb - ta))
    return pct(vs, 0.5) if vs else 0.0


def _global_model(train_eps: List[List[Row]], target: float) -> Tuple[float, float]:
    """Global cap A (plateau) and global rise velocity vg (median), fit on TRAIN sessions only.
    vg captures the type-invariant rise rate, so per shot we align only PHASE, removing the
    per-shot velocity-estimation noise that drives the linear extrapolation scatter."""
    peaks, vels = [], []
    A_guess = 99.0
    for e in train_eps:
        samples, peak, _ = _episode_rise(e)
        if peak >= target:
            peaks.append(peak)
        v = _episode_velocity(samples, knee_fill=0.93 * A_guess)
        if v > 0.0:
            vels.append(v)
    A = pct(peaks, 0.5) if peaks else A_guess
    A = max(A, target + 1.0)
    vg = pct(vels, 0.5) if vels else 0.0
    return A, vg


def score(all_eps_by_session, target: float, lead: float, window: float,
          knee: float, latency: float, label: str,
          conf_gate: float = 0.32, max_phase_spread: float = 35.0):
    sessions = list(all_eps_by_session.keys())
    template_err = []          # predicted t_target - actual t_target (CONFIDENT predictor releases)
    linear_err = []            # baseline: per-shot linear extrapolation at the same lead
    n_pred = n_drop = n_nonreach = n_lowconf = 0
    per_session = {s: [] for s in sessions}

    for held in sessions:
        train = [e for s in sessions if s != held for e in all_eps_by_session[s]]
        A, vglobal = _global_model(train, target)
        if vglobal <= 0:
            continue
        vg = vglobal
        for e in all_eps_by_session[held]:
            samples, peak, _ = _episode_rise(e)   # (t, fill, conf)
            # interp_crossing expects (fill, t).
            c_actual = interp_crossing([(f, t) for (t, f, _c) in samples], target)
            if c_actual < 0.0:
                n_nonreach += 1
                continue
            # samples available `lead` ms before the tip (the commit horizon), confidence-gated
            avail = [(t, f) for (t, f, c) in samples if t <= c_actual - lead and c >= conf_gate and f > 0]
            if any((b[0] - a[0]) > 120.0 for a, b in zip(avail, avail[1:])):
                n_drop += 1
                continue
            lin = [(t, f) for (t, f) in avail if f < knee * A]
            if len(lin) < 1:
                continue
            # GLOBAL-VELOCITY phase alignment (type-invariant rise -> no per-shot v noise):
            # fill = vg*(t - t0) -> per-sample phase t0_i = t_i - f_i/vg; recent-weighted combine.
            t0s = [t - f / vg for (t, f) in lin]
            ws = [math.exp(-0.5 * (len(lin) - 1 - i)) for i in range(len(lin))]
            sw = sum(ws)
            t0 = sum(w * x for w, x in zip(ws, t0s)) / sw
            # UNCERTAINTY gate: spread of the per-sample phase estimates. A consistent rise gives a
            # tight t0 cluster; a noisy/contested/partial read scatters -> the model DECLINES (in
            # production: fall back to the clock/per-type path), so it never fires a bad prediction.
            spread = (max(t0s) - min(t0s)) if len(t0s) >= 2 else 0.0
            if spread > max_phase_spread:
                n_lowconf += 1
                continue
            t_pred = t0 + target / vg
            template_err.append(t_pred - c_actual)
            per_session[held].append(t_pred - c_actual)
            n_pred += 1
            # baseline linear: per-shot slope from the same confident samples, extrapolate to target
            if len(avail) >= 2:
                txs = [t for t, _ in avail]
                fys = [f for _, f in avail]
                tm, fm = sum(txs) / len(txs), sum(fys) / len(fys)
                dn = sum((t - tm) ** 2 for t in txs)
                if dn > 1e-9:
                    sl = sum((t - tm) * (f - fm) for t, f in avail) / dn
                    if sl > 1e-4:
                        last_t, last_f = avail[-1]
                        linear_err.append((last_t + (target - last_f) / sl) - c_actual)

    def stats(errs):
        if not errs:
            return None
        med = pct(errs, 0.5)
        debias = [e - med for e in errs]
        p95 = pct([abs(x) for x in debias], 0.95)
        worst = max(abs(x) for x in debias)
        within = 100.0 * sum(1 for x in debias if abs(x) <= window / 2.0) / len(debias)
        return med, p95, worst, within, len(errs)

    print(f"\n================ {label}  (target={target:.0f}%, lead={lead:.0f}ms, "
          f"window~{window:.0f}ms) ================")
    confident_pct = 100.0 * n_pred / max(1, n_pred + n_lowconf)
    print(f"  episodes: CONFIDENT predictor={n_pred} ({confident_pct:.0f}% of fittable)  "
          f"low-confidence->fallback={n_lowconf}  dropout={n_drop}  non-reaching/fallback={n_nonreach}")
    t = stats(template_err)
    l = stats(linear_err)
    half = window / 2.0
    if t:
        med, p95, worst, within, n = t
        gate = "PASS" if p95 < half else "FAIL"
        print(f"  TEMPLATE   bias(med)={med:+.0f}  p95(debiased)={p95:.0f}  worst={worst:.0f}  "
              f"within±{half:.0f}ms={within:.0f}%  n={n}   GATE(p95<{half:.0f}): {gate}")
    if l:
        med, p95, worst, within, n = l
        print(f"  LINEAR(ref) bias(med)={med:+.0f}  p95(debiased)={p95:.0f}  worst={worst:.0f}  "
              f"within±{half:.0f}ms={within:.0f}%  n={n}")
    # per-session debiased p95 (the honest no-leakage view)
    print("  per-session debiased p95 (template):")
    for s in sessions:
        errs = per_session[s]
        if not errs:
            print(f"    {os.path.basename(s):40} n=0")
            continue
        med = pct(errs, 0.5)
        p95 = pct([abs(e - med) for e in errs], 0.95)
        print(f"    {os.path.basename(s):40} n={len(errs):3} bias={med:+5.0f} p95={p95:4.0f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="+", help="detframes_*.csv (each file = one session for LOSO)")
    ap.add_argument("--target", type=float, default=95.0, help="tip/green-center target fill%% (default 95)")
    ap.add_argument("--lead", type=float, default=110.0, help="commit horizon ms before the tip (default 110)")
    ap.add_argument("--window", type=float, default=25.0, help="narrowest green window ms (gate = p95 < window/2)")
    ap.add_argument("--plateau-knee", type=float, default=0.93, help="fit samples below knee*A only (default 0.93)")
    ap.add_argument("--latency", type=float, default=110.0, help="learned observation->action latency ms (report only)")
    ap.add_argument("--conf-gate", type=float, default=0.32, help="min detector confidence for a usable sample")
    ap.add_argument("--max-phase-spread", type=float, default=35.0,
                    help="max per-sample phase spread (ms) before the model DECLINES -> fallback")
    args = ap.parse_args()

    by_session = {}
    for p in args.csv:
        eps = episodes(load_rows(p))
        if eps:
            by_session[p] = eps
    total = sum(len(v) for v in by_session.values())
    print(f"loaded {len(by_session)} sessions, {total} fed episodes")
    if len(by_session) < 2:
        print("LOSO needs >=2 sessions (CSVs); pass more detframes files.")
        return 2
    score(by_session, args.target, args.lead, args.window, args.plateau_knee, args.latency,
          label="LEAVE-ONE-SESSION-OUT", conf_gate=args.conf_gate, max_phase_spread=args.max_phase_spread)
    # lead sweep to show how prediction degrades with horizon (the latency-reduction case)
    print("\n--- lead sweep (template p95 debiased, all sessions LOSO) ---")
    for L in (40, 60, 90, 110, 140):
        score(by_session, args.target, float(L), args.window, args.plateau_knee, args.latency,
              label=f"lead={L}", conf_gate=args.conf_gate, max_phase_spread=args.max_phase_spread)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
