"""Poll-phase stability, drift, power and the reader-noise alternative.

Reads the shots.csv from tools/timing/poll_phase_probe.py and answers, in order:

 1. STABILITY -- is the phase of maximum green the same from shot to shot and from session to
    session? (per-session circular fit; circular autocorrelation of the release phase over a
    10-shot window; split-half agreement of the green-phase direction.)
 2. DRIFT -- how fast does the recovered console frame grid slide against the engine clock?
    Grid search on a drift rate d (ms of grid phase per second of engine time) that maximises
    the circular concentration of the per-shot grid residues inside one session. Reports the
    implied console frame period.
 3. POWER -- inject a synthetic sinusoidal phase effect of known amplitude into the observed
    verdicts and measure the detection rate at this n, so the null result carries a bound.
 4. ALTERNATIVE -- the same effect sizes for the non-phase covariates (frame age, predictor
    sigma, lead, hold, fill at release, grid sd), so "reader noise" and "poll phase" are
    compared on one scale (rank-biserial / AUC with the same within-stratum permutation null).

Read-only. No engine, reader or process is touched.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
from collections import Counter, defaultdict

import numpy as np

P_MS = 1000.0 / 60.0
TWO_PI = 2.0 * math.pi


def F(r, k, d=float("nan")):
    try:
        return float(r.get(k, ""))
    except (TypeError, ValueError):
        return d


def circ_R(x_ms, period=P_MS):
    t = (np.asarray(x_ms) % period) / period * TWO_PI
    return float(abs(np.mean(np.exp(1j * t))))


def sine_amp(theta, y):
    z = np.mean((y - y.mean()) * np.exp(1j * theta))
    return 2.0 * abs(z), (float(np.angle(z)) % TWO_PI) * P_MS / TWO_PI


def perm_matrix(y, strata, nperm, rng):
    Y = np.tile(y, (nperm, 1))
    g = defaultdict(list)
    for i, s in enumerate(strata):
        g[s].append(i)
    for idx in g.values():
        if len(idx) > 1:
            j = np.array(idx)
            Y[:, j] = rng.permuted(Y[:, j], axis=1)
    return Y


def auc(x, y):
    """AUC of continuous x separating y==1 from y==0 (0.5 = no separation)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    ok = np.isfinite(x)
    x, y = x[ok], y[ok]
    if y.sum() == 0 or y.sum() == len(y):
        return float("nan"), 0
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(len(x), dtype=float)
    ranks[order] = np.arange(1, len(x) + 1)
    # average ranks for ties
    sx = x[order]
    i = 0
    while i < len(sx):
        j = i
        while j + 1 < len(sx) and sx[j + 1] == sx[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + j + 2) / 2.0
        i = j + 1
    n1 = y.sum()
    n0 = len(y) - n1
    a = (ranks[y == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0)
    return float(a), int(len(x))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default=r"D:\NexusVision\poll_phase\shots.csv")
    ap.add_argument("--out", default=r"D:\NexusVision\poll_phase")
    ap.add_argument("--perms", type=int, default=4000)
    args = ap.parse_args(argv)

    rows = list(csv.DictReader(open(args.shots, encoding="utf-8")))
    graded = [r for r in rows if r.get("banner") in ("EXCELLENT", "LATE", "EARLY")]
    L = []
    P = L.append

    # ---------------------------------------------------------------- 1. stability
    P("## 1. Per-session phase structure")
    P("| session | n | EXC | phase R (release) | green-phase peak ms | sine amp | half-1 peak | half-2 peak | peak gap ms |")
    P("|---|---|---|---|---|---|---|---|---|")
    peaks = []
    by_sess = defaultdict(list)
    for r in graded:
        by_sess[r["session"]].append(r)
    for s in sorted(by_sess):
        sub = by_sess[s]
        v = np.array([F(r, "phase_frame_ms") for r in sub])
        ok = np.isfinite(v)
        sub = [r for r, o in zip(sub, ok) if o]
        v = v[ok]
        if len(v) < 8:
            continue
        y = np.array([1.0 if r["banner"] == "EXCELLENT" else 0.0 for r in sub])
        th = (v % P_MS) / P_MS * TWO_PI
        amp, peak = sine_amp(th, y)
        h = len(v) // 2
        a1, p1 = sine_amp(th[:h], y[:h])
        a2, p2 = sine_amp(th[h:], y[h:])
        gap = abs(((p1 - p2 + P_MS / 2) % P_MS) - P_MS / 2)
        peaks.append((s, len(v), peak, amp, gap))
        P("| %s | %d | %d | %.3f | %.2f | %.3f | %.2f | %.2f | %.2f |" % (
            s, len(v), int(y.sum()), circ_R(v), peak, amp, p1, p2, gap))
    if peaks:
        pk = np.array([p[2] for p in peaks])
        P("")
        P("Cross-session agreement of the green-phase peak: circular R = %.3f over %d sessions "
          "(R=1 all sessions agree, R~%.2f is what %d random angles give). Median |split-half peak gap| "
          "= %.2f ms (max possible %.2f)." % (
              circ_R(pk), len(pk), 1.0 / math.sqrt(len(pk)), len(pk),
              float(np.median([p[4] for p in peaks])), P_MS / 2))

    # circular autocorrelation of the release phase itself (is the phase we land on predictable?)
    P("")
    P("## 1b. Is the release phase itself predictable from the previous shots?")
    P("| session | n | lag-1 circ corr | mean |phase - median(prev 10)| ms | uniform expectation ms |")
    P("|---|---|---|---|---|---|")
    for s in sorted(by_sess):
        v = np.array([F(r, "phase_frame_ms") for r in by_sess[s]])
        v = v[np.isfinite(v)]
        if len(v) < 12:
            continue
        a = (v[:-1] % P_MS) / P_MS * TWO_PI
        b = (v[1:] % P_MS) / P_MS * TWO_PI
        num = np.mean(np.sin(a - a.mean()) * np.sin(b - b.mean()))
        den = math.sqrt(np.mean(np.sin(a - a.mean()) ** 2) * np.mean(np.sin(b - b.mean()) ** 2))
        rho = num / den if den > 0 else float("nan")
        devs = []
        for i in range(10, len(v)):
            prev = (v[i - 10:i] % P_MS) / P_MS * TWO_PI
            m = np.angle(np.mean(np.exp(1j * prev)))
            d = abs(((v[i] % P_MS) / P_MS * TWO_PI - m + math.pi) % TWO_PI - math.pi) * P_MS / TWO_PI
            devs.append(d)
        P("| %s | %d | %+.3f | %.2f | %.2f |" % (s, len(v), rho,
                                                 float(np.mean(devs)) if devs else float("nan"), P_MS / 4))

    # ---------------------------------------------------------------- 2. drift
    P("")
    P("## 2. Drift of the recovered console grid against the engine clock")
    P("| session | shots | span s | best drift ms/s | R at best | R at 0 | implied console period ms | implied Hz |")
    P("|---|---|---|---|---|---|---|---|")
    drifts = []
    all_shots_by_sess = defaultdict(list)
    for r in rows:
        all_shots_by_sess[r["session"]].append(r)
    grid = np.linspace(-3.0, 3.0, 2401)
    for s in sorted(all_shots_by_sess):
        sub = [r for r in all_shots_by_sess[s]
               if math.isfinite(F(r, "frame_phase_ms")) and math.isfinite(F(r, "anchor_ms"))]
        if len(sub) < 10:
            continue
        ph = np.array([F(r, "frame_phase_ms") for r in sub])
        t = np.array([F(r, "anchor_ms") for r in sub]) / 1000.0
        t = t - t.min()
        Rs = np.array([circ_R(ph - d * t) for d in grid])
        k = int(np.argmax(Rs))
        d = float(grid[k])
        per = P_MS / (1.0 - d / 1000.0)
        drifts.append((s, len(sub), d, float(Rs[k]), circ_R(ph)))
        P("| %s | %d | %.0f | %+.3f | %.3f | %.3f | %.4f | %.3f |" % (
            s, len(sub), t.max(), d, Rs[k], circ_R(ph), per, 1000.0 / per))
    P("")
    P("A pure 60.000 Hz console on a perfect engine clock gives drift 0 and R~1. A 59.94 Hz console "
      "read with the engine's 16.6667 ms constant gives drift +1.00 ms/s (period 16.6833).")

    # ---------------------------------------------------------------- 3. power
    P("")
    P("## 3. Power: what size of poll effect would this data set have caught?")
    v = np.array([F(r, "phase_frame_ms") for r in graded])
    ok = np.isfinite(v)
    sub = [r for r, o in zip(graded, ok) if o]
    th = (v[ok] % P_MS) / P_MS * TWO_PI
    y = np.array([1.0 if r["banner"] == "EXCELLENT" else 0.0 for r in sub])
    strata = [r["session"] + "|" + r.get("shot_type", "") for r in sub]
    rng = np.random.default_rng(31337)
    Ynull = perm_matrix(y, strata, args.perms, rng)
    e = np.exp(1j * th)
    null = 2.0 * np.abs(np.mean((Ynull - Ynull.mean(axis=1, keepdims=True)) * e[None, :], axis=1))
    crit = float(np.percentile(null, 95))
    obs = sine_amp(th, y)[0]
    P("Observed sine amplitude on the game-frame grid, P(EXCELLENT), n=%d: **%.3f**; "
      "permutation critical value (95th) **%.3f**." % (len(y), obs, crit))
    P("")
    P("| injected swing in P(green) | detection rate at alpha=0.05 |")
    P("|---|---|")
    base = y.mean()
    for A in (0.10, 0.15, 0.20, 0.30, 0.50, 0.80):
        hits = 0
        trials = 400
        for k in range(trials):
            pr = np.clip(base + (A / 2.0) * np.cos(th - 1.0), 0.02, 0.98)
            ys = (rng.random(len(th)) < pr).astype(float)
            a = sine_amp(th, ys)[0]
            hits += 1 if a > crit else 0
        P("| %.0f pp | %.0f%% |" % (A * 100, 100.0 * hits / trials))

    # ---------------------------------------------------------------- 4. alternative
    P("")
    P("## 4. The alternative: plain covariates on the same scale")
    P("AUC for separating EXCELLENT from LATE (0.50 = nothing; <0.50 = higher value -> more LATE).")
    P("| covariate | n | AUC(EXC vs LATE) | perm p | AUC(EXC vs EARLY) |")
    P("|---|---|---|---|---|")
    exc_late = [r for r in graded if r["banner"] in ("EXCELLENT", "LATE")]
    exc_early = [r for r in graded if r["banner"] in ("EXCELLENT", "EARLY")]
    covs = ["phase_frame_ms", "frame_age_ms", "predictor_sigma_ms", "lead_ms", "release_fill_pct",
            "resv_fill_pct", "command_eta_ms", "tip_eta_ms", "frame_phase_sd_ms", "frame_offset_ms",
            "frame_edges", "banner_trim_ms", "phase_const_ms", "capture_coherence", "oracle_gap_px"]
    rng2 = np.random.default_rng(4242)
    for c in covs:
        x = np.array([F(r, c) for r in exc_late])
        yy = np.array([1.0 if r["banner"] == "EXCELLENT" else 0.0 for r in exc_late])
        okc = np.isfinite(x)
        if okc.sum() < 30:
            continue
        a, n = auc(x[okc], yy[okc])
        st = [r["session"] + "|" + r.get("shot_type", "") for r, o in zip(exc_late, okc) if o]
        Y = perm_matrix(yy[okc], st, 2000, rng2)
        nulls = np.array([auc(x[okc], Y[i])[0] for i in range(400)])
        p = float((np.sum(np.abs(nulls - 0.5) >= abs(a - 0.5)) + 1) / (len(nulls) + 1))
        x2 = np.array([F(r, c) for r in exc_early])
        y2 = np.array([1.0 if r["banner"] == "EXCELLENT" else 0.0 for r in exc_early])
        ok2 = np.isfinite(x2)
        a2 = auc(x2[ok2], y2[ok2])[0] if ok2.sum() > 30 else float("nan")
        P("| %s | %d | %.3f | %.3f | %.3f |" % (c, n, a, p, a2))

    txt = "\n".join(L)
    with open(os.path.join(args.out, "stability.md"), "w", encoding="utf-8") as fh:
        fh.write(txt + "\n")
    print(txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
