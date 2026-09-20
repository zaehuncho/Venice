"""The owner's hypothesis in its own shape: a phase that is UNKNOWN per session but STABLE
within one, so that pooling across sessions would hide it.

Three tests the pooled fold cannot do:
  A. PER-SESSION AMPLITUDE. Statistic = n-weighted mean of the per-session first-harmonic
     amplitude of P(green) against the release phase. Null = permute verdicts within session
     (and shot type), so each session keeps its own green rate and its own phase distribution.
  B. SPLIT-HALF CONSISTENCY. Within each session, fit the green-phase angle on the first half
     of the shots and on the second half; statistic = n-weighted mean cos(angle1 - angle2)
     (1 = the safe phase is the same at the start and the end of the session, 0 = unrelated).
     This is the property a phase tracker would have to exploit.
  C. PERIOD SCAN. The same per-session amplitude at other candidate periods: 4 ms (250 Hz
     USB), 8.333 ms (120 Hz), 16.667 ms (console frame), 33.333/33.366 ms (the Remote Play
     stream cadence measured 2026-09-03), plus a fine sweep, in case the input is registered
     on a cadence other than the render frame.
  D. CONVERGENCE. If an effect of size A did exist, how many graded shots would a circular-mean
     tracker need to place the safe phase to +-3 ms?

Read-only.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
from collections import defaultdict

import numpy as np

P_MS = 1000.0 / 60.0
TWO_PI = 2.0 * math.pi


def F(r, k, d=float("nan")):
    try:
        return float(r.get(k, ""))
    except (TypeError, ValueError):
        return d


def perm_within(y, strata, nperm, rng):
    Y = np.tile(y, (nperm, 1))
    g = defaultdict(list)
    for i, s in enumerate(strata):
        g[s].append(i)
    for idx in g.values():
        if len(idx) > 1:
            j = np.array(idx)
            Y[:, j] = rng.permuted(Y[:, j], axis=1)
    return Y


def per_session_amp(theta, Y, sess_idx):
    """Y: (m, n) outcomes. Returns (m,) n-weighted mean per-session amplitude."""
    e = np.exp(1j * theta)
    tot = 0.0
    acc = np.zeros(Y.shape[0])
    for idx in sess_idx:
        sub = Y[:, idx]
        z = np.mean((sub - sub.mean(axis=1, keepdims=True)) * e[idx][None, :], axis=1)
        acc += len(idx) * 2.0 * np.abs(z)
        tot += len(idx)
    return acc / tot


def per_session_splithalf(theta, Y, sess_idx):
    tot = 0.0
    acc = np.zeros(Y.shape[0])
    for idx in sess_idx:
        h = len(idx) // 2
        if h < 4:
            continue
        a, b = idx[:h], idx[h:]
        za = np.mean((Y[:, a] - Y[:, a].mean(axis=1, keepdims=True)) * np.exp(1j * theta[a])[None, :], axis=1)
        zb = np.mean((Y[:, b] - Y[:, b].mean(axis=1, keepdims=True)) * np.exp(1j * theta[b])[None, :], axis=1)
        with np.errstate(invalid="ignore"):
            c = np.cos(np.angle(za) - np.angle(zb))
        c = np.nan_to_num(c)
        acc += len(idx) * c
        tot += len(idx)
    return acc / max(tot, 1.0)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default=r"D:\NexusVision\poll_phase\shots.csv")
    ap.add_argument("--out", default=r"D:\NexusVision\poll_phase")
    ap.add_argument("--perms", type=int, default=4000)
    args = ap.parse_args(argv)

    rows = [r for r in csv.DictReader(open(args.shots, encoding="utf-8"))
            if r.get("banner") in ("EXCELLENT", "LATE", "EARLY")]
    L = []
    P = L.append
    rng = np.random.default_rng(90210)

    def prep(sub, phase_key, period=P_MS, use_grid=True):
        if use_grid:
            v = np.array([F(r, phase_key) for r in sub])
        else:
            v = np.array([F(r, "command_issued_ms") for r in sub])
        ok = np.isfinite(v)
        s2 = [r for r, o in zip(sub, ok) if o]
        theta = ((v[ok] % period) / period) * TWO_PI
        sess = defaultdict(list)
        for i, r in enumerate(s2):
            sess[r["session"]].append(i)
        sess_idx = [np.array(v2) for v2 in sess.values() if len(v2) >= 8]
        strata = [r["session"] + "|" + r.get("shot_type", "") for r in s2]
        y = np.array([1.0 if r["banner"] == "EXCELLENT" else 0.0 for r in s2])
        return theta, y, strata, sess_idx

    P("## A. Per-session first-harmonic amplitude (a phase that differs by session)")
    P("| phase variable | n | sessions | observed | null median | null p95 | p |")
    P("|---|---|---|---|---|---|---|")
    for key, per, grid in (("phase_frame_ms", P_MS, True),
                           ("phase_engine_ms", P_MS, True),
                           ("phase_wall_ms", P_MS, True)):
        theta, y, strata, sess_idx = prep(rows, key, per, grid)
        obs = per_session_amp(theta, y[None, :], sess_idx)[0]
        Y = perm_within(y, strata, args.perms, rng)
        null = per_session_amp(theta, Y, sess_idx)
        p = float((np.sum(null >= obs) + 1) / (args.perms + 1))
        P("| %s | %d | %d | %.3f | %.3f | %.3f | %.3f |" % (
            key, len(y), len(sess_idx), obs, float(np.median(null)), float(np.percentile(null, 95)), p))

    P("")
    P("## B. Split-half consistency of the per-session green phase")
    P("Statistic = n-weighted mean cos(peak angle in the first half - peak angle in the second half).")
    P("| phase variable | n | sessions | observed | null median | null p95 | p |")
    P("|---|---|---|---|---|---|---|")
    for key in ("phase_frame_ms", "phase_engine_ms"):
        theta, y, strata, sess_idx = prep(rows, key)
        obs = per_session_splithalf(theta, y[None, :], sess_idx)[0]
        Y = perm_within(y, strata, args.perms, rng)
        null = per_session_splithalf(theta, Y, sess_idx)
        p = float((np.sum(null >= obs) + 1) / (args.perms + 1))
        P("| %s | %d | %d | %+.3f | %+.3f | %+.3f | %.3f |" % (
            key, len(y), len(sess_idx), obs, float(np.median(null)), float(np.percentile(null, 95)), p))

    P("")
    P("## C. Period scan on the raw engine clock (per-session amplitude)")
    P("| period ms | equivalent Hz | observed | null p95 | p |")
    P("|---|---|---|---|---|")
    for per in (4.0, 8.3335, 11.111, 16.6667, 20.0, 25.0, 33.3333, 33.366, 50.0):
        theta, y, strata, sess_idx = prep(rows, "command_issued_ms", per, use_grid=False)
        obs = per_session_amp(theta, y[None, :], sess_idx)[0]
        Y = perm_within(y, strata, 1500, rng)
        null = per_session_amp(theta, Y, sess_idx)
        p = float((np.sum(null >= obs) + 1) / 1501)
        P("| %.4f | %.2f | %.3f | %.3f | %.3f |" % (per, 1000.0 / per, obs,
                                                    float(np.percentile(null, 95)), p))

    P("")
    P("## D. Convergence a tracker would need, IF an effect existed")
    P("Simulated: P(green) = base + (A/2) cos(theta - theta*), phases drawn uniformly, estimator = "
      "circular mean of e^{i theta} weighted by (green - base). Shots needed for the estimate to be "
      "within +-3 ms of theta* 68 % / 90 % of the time.")
    P("| swing A | shots for 68 % within +-3 ms | shots for 90 % within +-3 ms |")
    P("|---|---|---|")
    base = float(np.mean([1.0 if r["banner"] == "EXCELLENT" else 0.0 for r in rows]))
    tol = 3.0 / P_MS * TWO_PI
    for A in (0.10, 0.20, 0.30, 0.50, 0.80, 1.00):
        need = {}
        for n in (10, 20, 30, 50, 75, 100, 150, 200, 300, 500, 800, 1200, 2000, 4000):
            th = rng.random((600, n)) * TWO_PI
            pr = np.clip(base + (A / 2.0) * np.cos(th - 1.0), 0.02, 0.98)
            g = (rng.random((600, n)) < pr).astype(float)
            z = np.mean((g - g.mean(axis=1, keepdims=True)) * np.exp(1j * th), axis=1)
            err = np.abs((np.angle(z) - 1.0 + math.pi) % TWO_PI - math.pi)
            frac = float(np.mean(err <= tol))
            for lvl in (0.68, 0.90):
                if lvl not in need and frac >= lvl:
                    need[lvl] = n
            if len(need) == 2:
                break
        P("| %.0f pp | %s | %s |" % (A * 100, need.get(0.68, ">4000"), need.get(0.90, ">4000")))

    txt = "\n".join(L)
    with open(os.path.join(args.out, "persession.md"), "w", encoding="utf-8") as fh:
        fh.write(txt + "\n")
    print(txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
