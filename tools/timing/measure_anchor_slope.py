"""Measure tipPhaseConstantSlopeMsPerPct from recorded per-frame fill.

    python tools/timing/measure_anchor_slope.py logs/diagnostics/detframes*.csv

RUN THIS BEFORE ANY CHANGE TO tipPhaseAnchorPct. The shipped constant is -5.235,
and measured 2026-08-05 (n=21) the true 20->30 slope is **-5.830**, so moving the
base anchor 30 -> 20 needs a **58.3ms** shift across all four coupled terms, not
the 52.35ms the shipped constant implies. Using the shipped value would fire the
bot ~6ms EARLY on every shot -- invisible until a counted batch catches it.

Measured 2026-08-05, n=21 shots with a witnessed 30% crossing:

    level 20:  -5.830 ms/pp   (rSD 0.44)
    level 25:  -5.776          (rSD 0.45)
    level 35:  -5.583          (rSD 0.56)
    level 40:  -5.428          (rSD 0.33)

Smooth across 20-40, so extrapolation is sound -- but the shipped constant is also
mildly wrong at the EXISTING ladder rungs (35, 40), costing ~1.7 / ~1.9ms on the
No-Dip shots that are dated there today.

Note the rSD: ~0.44 ms/pp at level 20 means the shift itself varies +-4.4ms shot
to shot over a 10pp move. A single constant is only ever right on average.

--- method ---



The slope claims: dating a shot at level L instead of the base anchor 30 leaves
slope*(L-30) ms MORE animation remaining. Between two levels that is simply the
wall time the meter spends travelling from L to 30 -- no tip estimate needed:

    slope(L->30) = -(t_30 - t_L) / (30 - L)   ms per pp

So the engine's -5.235 is testable directly against recorded per-frame fill.
"""
import csv
import sys
import numpy as np

LEVELS = [20.0, 25.0, 30.0, 35.0, 40.0]


def load(path):
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                if int(r.get("detected", 0) or 0) != 1:
                    continue
                if int(r.get("fed", 1) or 0) != 1:
                    continue
                t = float(r.get("wall_ms") or r.get("t_ms") or 0.0)
                f = float(r.get("fill_pct") or 0.0)
            except (TypeError, ValueError):
                continue
            if t <= 0.0 or not (0.0 <= f <= 100.0):
                continue
            rows.append((t, f))
    rows.sort(key=lambda p: p[0])
    return rows


def segment(rows, max_gap_ms=200.0):
    """Split into contiguous detection runs, then keep those that look like a rise."""
    runs, cur = [], []
    for t, f in rows:
        if cur and (t - cur[-1][0]) > max_gap_ms:
            runs.append(cur)
            cur = []
        cur.append((t, f))
    if cur:
        runs.append(cur)
    shots = []
    for run in runs:
        fills = [f for _, f in run]
        if len(run) < 8:
            continue
        # a real rise starts low and reaches well past the top level we probe
        if min(fills[:3]) > 18.0:
            continue
        if max(fills) < 45.0:
            continue
        shots.append(run)
    return shots


def crossing(run, level):
    """First upward straddling crossing of `level`, linearly interpolated."""
    for (t0, f0), (t1, f1) in zip(run, run[1:]):
        if f0 < level <= f1 and f1 > f0:
            frac = (level - f0) / (f1 - f0)
            return t0 + frac * (t1 - t0)
    return None


def main(paths):
    per_pair = {}
    n_shots = 0
    for p in paths:
        rows = load(p)
        for run in segment(rows):
            xs = {L: crossing(run, L) for L in LEVELS}
            if xs[30.0] is None:
                continue
            n_shots += 1
            for L in LEVELS:
                if L == 30.0 or xs[L] is None:
                    continue
                # ms of animation between L and the base anchor, signed like the engine's slope
                slope = -(xs[30.0] - xs[L]) / (30.0 - L)
                per_pair.setdefault(L, []).append(slope)

    print("shots with a witnessed 30%% crossing: %d" % n_shots)
    print()
    print("%-8s %5s %9s %9s %9s %9s" % ("level", "n", "median", "rSD", "p10", "p90"))
    for L in LEVELS:
        v = per_pair.get(L)
        if not v:
            print("%-8.0f %5s %9s" % (L, 0, "(none)"))
            continue
        a = np.array(v)
        rsd = 1.4826 * np.median(np.abs(a - np.median(a)))
        print("%-8.0f %5d %9.3f %9.3f %9.3f %9.3f"
              % (L, len(a), np.median(a), rsd,
                 np.percentile(a, 10), np.percentile(a, 90)))
    print()
    print("engine constant: tipPhaseConstantSlopeMsPerPct = -5.235")
    below = per_pair.get(20.0)
    if below:
        a = np.array(below)
        med = np.median(a)
        print("measured 20->30 slope: %.3f ms/pp  (engine -5.235, delta %+.3f)"
              % (med, med - (-5.235)))
        print("implied constant shift for anchor 30->20: %.1f ms (engine assumes 52.35)"
              % (abs(med) * 10.0))


if __name__ == "__main__":
    main(sys.argv[1:])
