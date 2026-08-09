#!/usr/bin/env python3
"""PHASE-1 PROOF GATE for the autonomous-vision timing redesign.

Question: can the LIVE per-shot predictor (the weighted-LS + quadratic-peak fit in
AutomationEngine::TemporalSampler::predictCrossingMs) extrapolate the meter's tip-crossing
ACCURATELY ENOUGH, from mid-rise samples, to time the release WITHOUT any per-type clock?

If yes, the per-type feedforward clock + per-type offset sliders are unnecessary and the live
predictor can be made the sole release authority. If the prediction scatter at the lead we'd
fire at is wider than the green window, vision-only would regress to "all late/scattered" and
the detector/feed needs work first.

Method (no console time; uses already-recorded detframes_*.csv):
  * Rebuild each shot's RISE from the FED samples (fed=1 = the orchestrator fed it to the engine
    as a fresh live sample -- the exact rows the native sampler seeds from).
  * Port predictCrossingMs / velocityPctPerMs / the sample-buffer rules 1:1 from the C++.
  * For every mid-rise sample t (fill < target), compute the predicted crossing C_pred(t) of the
    tip target from ONLY the samples up to t, and compare to the ACTUAL crossing C_actual (the
    recorded time the fill first reaches the target, linearly interpolated). Bucket the signed
    error e(t) = C_pred(t) - C_actual by the LEAD (C_actual - t) -- i.e. how far ahead we asked
    the fit to see. The release fires ~one effective-latency ahead, so read the error stats in
    the LEAD ~= 60..130 ms band.

Decision: small, unbiased error in that band (|median| small, IQR within the green window's
time width) => the live fit is sufficient; flip authority. A large or lead-growing error =>
the fit can't see far enough on this feed => Phase 4 (detector/feed) first.

Usage:
  C:\\Python314\\python.exe tools/diagnostics/replay_predictor.py logs/diagnostics/detframes_20260618_165746.csv
  ... --target 97 --leads 60,90,120 --min-rise 5
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import deque
from typing import Deque, List, Optional, Tuple

# ----------------------------------------------------------------------------------
# 1:1 port of AutomationEngine::TemporalSampler (AutomationEngine.cpp:30-278).
# ----------------------------------------------------------------------------------
LAMBDA = 0.5


class TemporalSampler:
    def __init__(self) -> None:
        self._s: Deque[Tuple[float, float]] = deque()  # (fillPct, timestampMs)

    def reset(self) -> None:
        self._s.clear()

    def add(self, fill: float, ts: float) -> None:
        if self._s:
            lf, lt = self._s[-1]
            dt = ts - lt
            if dt <= 0.0:
                return
            if dt < 5.0 and abs(fill - lf) < 0.05:
                return
            if dt > 42.0:
                self._s.clear()
            elif fill < lf - 4.0:
                return  # outlier: meter only rises within a shot
        self._s.append((fill, ts))
        while self._s and ts - self._s[0][1] > 180.0:
            self._s.popleft()
        while len(self._s) > 16:
            self._s.popleft()

    def size(self) -> int:
        return len(self._s)

    def velocity_pct_per_ms(self) -> float:
        n = len(self._s)
        if n < 2:
            return 0.0
        sw = tm = fm = 0.0
        for i, (f, t) in enumerate(self._s):
            w = math.exp(-LAMBDA * (n - 1 - i))
            sw += w
            tm += w * t
            fm += w * f
        if sw <= 1e-9:
            return 0.0
        tm /= sw
        fm /= sw
        num = den = 0.0
        for i, (f, t) in enumerate(self._s):
            w = math.exp(-LAMBDA * (n - 1 - i))
            d = t - tm
            num += w * d * (f - fm)
            den += w * d * d
        if abs(den) < 1e-9:
            return 0.0
        return num / den

    def predict_crossing_ms(self, target: float) -> float:
        n = len(self._s)
        if n < 3:
            return -1.0
        last_f, last_t = self._s[-1]
        if last_f >= target:
            return last_t

        # weighted linear fit, x centred at the latest sample (x<=0 for history)
        sw = xm = fm = 0.0
        for i, (f, t) in enumerate(self._s):
            w = math.exp(-LAMBDA * (n - 1 - i))
            x = t - last_t
            sw += w
            xm += w * x
            fm += w * f
        if sw <= 1e-9:
            return -1.0
        xm /= sw
        fm /= sw
        num = den = 0.0
        for i, (f, t) in enumerate(self._s):
            w = math.exp(-LAMBDA * (n - 1 - i))
            x = t - last_t
            num += w * (x - xm) * (f - fm)
            den += w * (x - xm) * (x - xm)
        lin_wrss = 1e300
        lin_crossing = -1.0
        if abs(den) >= 1e-9:
            slope = num / den
            intercept = fm - slope * xm
            lin_wrss = 0.0
            for i, (f, t) in enumerate(self._s):
                w = math.exp(-LAMBDA * (n - 1 - i))
                x = t - last_t
                r = f - (intercept + slope * x)
                lin_wrss += w * r * r
            if slope > 0.01:
                delta = target - intercept
                lin_crossing = last_t if delta <= 0.0 else last_t + delta / slope

        # weighted quadratic, adopted only if it cuts the weighted residual to <=80% of the line
        if n >= 4:
            m = [[0.0] * 4 for _ in range(3)]
            for i, (f, t) in enumerate(self._s):
                x = t - last_t
                weight = math.exp(-LAMBDA * (n - 1 - i))
                xs = (x * x, x, 1.0)
                for row in range(3):
                    for col in range(3):
                        m[row][col] += weight * xs[row] * xs[col]
                    m[row][3] += weight * xs[row] * f
            ok = True
            for col in range(3):
                pivot = col
                for row in range(col + 1, 3):
                    if abs(m[row][col]) > abs(m[pivot][col]):
                        pivot = row
                if abs(m[pivot][col]) < 1e-9:
                    ok = False
                    break
                if pivot != col:
                    m[col], m[pivot] = m[pivot], m[col]
                div = m[col][col]
                for k in range(col, 4):
                    m[col][k] /= div
                for row in range(3):
                    if row == col:
                        continue
                    fac = m[row][col]
                    for k in range(col, 4):
                        m[row][k] -= fac * m[col][k]
            if ok:
                a, b, c_full = m[0][3], m[1][3], m[2][3]
                c = c_full - target
                best = -1.0
                if abs(a) > 1e-9:
                    disc = b * b - 4.0 * a * c
                    if disc >= 0.0:
                        root = math.sqrt(disc)
                        for r in ((-b + root) / (2.0 * a), (-b - root) / (2.0 * a)):
                            if math.isfinite(r) and 0.0 <= r <= 1200.0 and (best < 0.0 or r < best):
                                best = r
                elif abs(b) > 1e-9:
                    r = -c / b
                    if math.isfinite(r) and 0.0 <= r <= 1200.0:
                        best = r
                quad_wrss = 0.0
                for i, (f, t) in enumerate(self._s):
                    w = math.exp(-LAMBDA * (n - 1 - i))
                    x = t - last_t
                    pred = a * x * x + b * x + c_full
                    r = pred - f
                    quad_wrss += w * r * r
                if quad_wrss <= lin_wrss * 0.80:
                    if best >= 0.0:
                        return last_t + best
                    if a < -1e-7:
                        peak_ms = -b / (2.0 * a)
                        peak_val = c_full - (b * b) / (4.0 * a)
                        if 0.0 < peak_ms <= 1200.0 and peak_val < target:
                            return last_t + peak_ms
        return lin_crossing


# ----------------------------------------------------------------------------------
# Episode reconstruction from a detframes CSV.
# ----------------------------------------------------------------------------------
class Row:
    __slots__ = ("t", "fed", "fill", "gc", "conf")

    def __init__(self, t, fed, fill, gc, conf):
        self.t = t
        self.fed = fed
        self.fill = fill
        self.gc = gc
        self.conf = conf


def load_rows(path: str) -> List[Row]:
    out: List[Row] = []
    with open(path, newline="") as fh:
        for d in csv.DictReader(fh):
            try:
                out.append(Row(
                    float(d["t_ms"]),
                    int(float(d["fed"])),
                    float(d["fill_pct"]),
                    float(d.get("green_center_pct", -1) or -1),
                    float(d.get("confidence", 0) or 0),
                ))
            except (ValueError, KeyError):
                continue
    return out


def episodes(rows: List[Row], gap_ms: float = 500.0) -> List[List[Row]]:
    """A shot's meter lifetime = a run of FED samples with no >gap_ms break between them."""
    eps: List[List[Row]] = []
    cur: List[Row] = []
    last_t: Optional[float] = None
    for r in rows:
        if r.fed != 1 or r.fill <= 0.0:
            continue
        if last_t is not None and r.t - last_t > gap_ms:
            if len(cur) >= 3:
                eps.append(cur)
            cur = []
        cur.append(r)
        last_t = r.t
    if len(cur) >= 3:
        eps.append(cur)
    return eps


def interp_crossing(samples: List[Tuple[float, float]], target: float) -> float:
    """Earliest time the recorded (t,fill) trajectory first reaches `target`, linearly
    interpolated between the bracketing samples. -1 if it never reaches target."""
    for (f0, t0), (f1, t1) in zip(samples, samples[1:]):
        if f0 < target <= f1 and t1 > t0:
            return t0 + (target - f0) / (f1 - f0) * (t1 - t0)
    return -1.0


def pct(vals: List[float], q: float) -> float:
    if not vals:
        return float("nan")
    s = sorted(vals)
    i = q * (len(s) - 1)
    lo = int(math.floor(i))
    hi = int(math.ceil(i))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


def simulate_policy(eps: List[List["Row"]], target: float, latency: float, min_rise: int,
                    reach_max_rise: float, min_pred_fill: float, slack: float):
    """Simulate the AUTHORITATIVE constant-velocity predictive release (AutomationEngine block #2)
    per episode and return per-shot landing error in ms (fireTime+latency - actualTipCrossing).

    Fire rule (matches AutomationEngine.cpp:1518-1596): the FIRST fed rise sample that is
    within reach -- (target-fill) <= clamp(v*latency,0,reach_max_rise)+slack AND fill>=min_pred_fill
    -- and whose constant-velocity ETA (target-fill)/v <= latency. The press is modelled to register
    at fireTime+latency; a positive error => lands ABOVE the tip (LATE/overshoot), negative => SHORT
    (EARLY). Bias is removable by the global latency learner; the SCATTER is the real bar."""
    # Velocity prior (engine clamps the live v to [lo,hi]*prior so a torn-frame spike can't
    # fire absurdly early/late; AutomationEngine.cpp:1330-1334). Use the global median as prior.
    all_v = []
    for e in eps:
        smp = TemporalSampler()
        for r in e[: max(range(len(e)), key=lambda i: e[i].fill) + 1]:
            smp.add(r.fill, r.t)
            if smp.size() >= 3:
                vv = smp.velocity_pct_per_ms()
                if vv > 0:
                    all_v.append(vv)
    prior = sorted(all_v)[len(all_v) // 2] if all_v else 0.0
    errs_ms = []
    fired = 0
    for e in eps:
        peak_i = max(range(len(e)), key=lambda i: e[i].fill)
        rise = e[: peak_i + 1]
        if len(rise) < min_rise:
            continue
        c_actual = interp_crossing([(r.fill, r.t) for r in rise], target)
        if c_actual < 0.0:
            continue
        smp = TemporalSampler()
        fire_t = None
        for r in rise:
            smp.add(r.fill, r.t)
            if smp.size() < 3 or r.fill >= target:
                if r.fill >= target:
                    break
                continue
            v = smp.velocity_pct_per_ms()
            if v <= 0.001:
                continue
            if prior > 0.0:
                v = min(max(v, prior * 0.5), prior * 1.6)   # engine velocity-prior clamp
            expected_rise = min(max(v * latency, 0.0), reach_max_rise)
            within = (target - r.fill) <= (expected_rise + slack) and r.fill >= min_pred_fill
            if not within:
                continue
            if (target - r.fill) / v <= latency:
                fire_t = r.t
                break
        if fire_t is None:
            continue
        fired += 1
        errs_ms.append((fire_t + latency) - c_actual)
    return errs_ms, fired


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="+", help="one or more detframes_*.csv (aggregated)")
    ap.add_argument("--target", type=float, default=95.0, help="tip target fill%% (default 95 = median green center)")
    ap.add_argument("--leads", default="40,60,90,120,150",
                    help="comma lead bands (ms) to report prediction error at (default 40,60,90,120,150)")
    ap.add_argument("--lead-tol", type=float, default=25.0, help="+/- ms bucket width around each lead")
    ap.add_argument("--min-rise", type=int, default=4, help="min fed rise samples for an episode to count")
    ap.add_argument("--green-band", type=float, default=5.0,
                    help="assumed green band fill width (%%) -> approx window time width for the verdict")
    ap.add_argument("--latency", type=float, default=110.0, help="effective latency ms for the policy sim (default 110)")
    args = ap.parse_args()

    rows: List[Row] = []
    for p in args.csv:
        rows.extend(load_rows(p))
    eps = episodes(rows)
    leads = [float(x) for x in args.leads.split(",") if x.strip()]
    print(f"loaded {len(rows)} rows from {len(args.csv)} file(s) -> {len(eps)} fed episodes; "
          f"target tip={args.target:.0f}%")

    # err_by_lead[lead] = list of signed C_pred - C_actual (ms) for mid-rise samples whose
    # actual lead falls within [lead-tol, lead+tol].
    err_by_lead = {L: [] for L in leads}
    reachable = {L: 0 for L in leads}   # samples where a fit even produced a crossing
    counted = {L: 0 for L in leads}
    velocities: List[float] = []
    crossing_eps = 0

    for e in eps:
        traj = [(r.fill, r.t) for r in e]
        # the rise = start..peak (meter only rises within a shot; recede after peak is post-shot)
        peak_i = max(range(len(e)), key=lambda i: e[i].fill)
        rise = e[: peak_i + 1]
        if len(rise) < args.min_rise:
            continue
        c_actual = interp_crossing([(r.fill, r.t) for r in rise], args.target)
        if c_actual < 0.0:
            continue  # meter never reached the tip target this shot -> can't score crossing
        crossing_eps += 1
        smp = TemporalSampler()
        for r in rise:
            smp.add(r.fill, r.t)
            if r.fill >= args.target:
                break
            if smp.size() < 3:
                continue
            v = smp.velocity_pct_per_ms()
            if v > 0.0:
                velocities.append(v)
            c_pred = smp.predict_crossing_ms(args.target)
            lead = c_actual - r.t
            for L in leads:
                if abs(lead - L) <= args.lead_tol:
                    counted[L] += 1
                    if c_pred > 0.0:
                        reachable[L] += 1
                        err_by_lead[L].append(c_pred - c_actual)

    print(f"episodes that reached the tip target (scorable): {crossing_eps}/{len(eps)}")
    if velocities:
        print(f"live rise velocity %/ms: median={pct(velocities,0.5):.3f}  "
              f"IQR[{pct(velocities,0.25):.3f},{pct(velocities,0.75):.3f}]  "
              f"(type-invariance check: a tight band supports one global model)")

    # Convert the assumed green fill-band width to a time width via the median velocity, so the
    # error IQR can be judged against the actual window the press must land in.
    vmed = pct(velocities, 0.5) if velocities else 0.0
    win_ms = (args.green_band / vmed) if vmed > 0 else float("nan")
    print(f"\napprox green-window TIME width @ band {args.green_band:.0f}%% and median v: "
          f"~{win_ms:.0f} ms  (the bar: prediction IQR should fit inside this)")

    print("\n=== CROSSING-PREDICTION ERROR by LEAD (signed C_pred - C_actual, ms) ===")
    print("  lead   n   p10    p25   median   p75    p90  | IQR  bias-removed within-win%")
    for L in leads:
        errs = err_by_lead[L]
        if not errs:
            print(f"  {L:4.0f}   0   (no mid-rise samples at this lead)")
            continue
        p10, p25, p50, p75, p90 = (pct(errs, q) for q in (0.1, 0.25, 0.5, 0.75, 0.9))
        iqr = p75 - p25
        # bias-removed: the global latency learner cancels the median; what's left is the scatter.
        within = (100.0 * sum(1 for x in errs if abs(x - p50) <= win_ms / 2.0) / len(errs)
                  if vmed > 0 else float("nan"))
        print(f"  {L:4.0f}  {len(errs):3}  {p10:6.0f} {p25:6.0f} {p50:6.0f} {p75:6.0f} {p90:6.0f}  | "
              f"{iqr:4.0f}      {within:5.0f}%")

    # Policy simulation: the actual block-2 authoritative fire rule, per-shot landing error.
    errs_ms, fired = simulate_policy(eps, args.target, args.latency, args.min_rise,
                                     reach_max_rise=39.0, min_pred_fill=42.0, slack=6.0)
    print(f"\n=== BLOCK-2 POLICY SIM (latency={args.latency:.0f}ms, reachCap=39, floor=42) ===")
    if errs_ms:
        p10, p25, p50, p75, p90 = (pct(errs_ms, q) for q in (0.1, 0.25, 0.5, 0.75, 0.9))
        iqr = p75 - p25
        landing_iqr_fill = iqr * vmed if vmed > 0 else float("nan")
        within = 100.0 * sum(1 for x in errs_ms if abs(x - p50) <= win_ms / 2.0) / len(errs_ms)
        print(f"  fired on {fired} shots; landing error ms (fire+lat - tip):")
        print(f"    p10={p10:.0f} p25={p25:.0f} median(bias)={p50:.0f} p75={p75:.0f} p90={p90:.0f}")
        print(f"    IQR={iqr:.0f}ms  (~{landing_iqr_fill:.1f}% fill spread)  "
              f"bias-removed within-window={within:.0f}%")
        print(f"    >0 = lands LATE/above tip, <0 = lands EARLY/short. Bias is learner-removable;")
        print(f"    judge IQR vs the ~{win_ms:.0f}ms window.")
    else:
        print("  policy never fired on the scored episodes (reach gate too strict for this feed)")

    print("\nread the LEAD ~= 60..130 band + the policy sim. small bias-removed scatter (IQR within")
    print("the window) => live prediction sufficient; flip authority. wide/lead-growing => feed first.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
