"""Fold Orion's graded releases against the console frame grid -- the 2026-09-17 poll-phase test.

Input: the shots.csv written by tools/timing/poll_phase_probe.py (one row per released,
banner-graded vision shot, carrying the release instant on the engine clock, the per-shot
game-frame grid the engine recovered from the fill staircase, the capture cycle, and the
verdict).

Question: does the game's verdict depend on WHERE INSIDE ONE CONSOLE FRAME the release command
was issued? If it does, a per-session phase estimator can steer the fire instant to the safe
side of the poll.

Everything here is read-only. Statistics:
  * circular ("sine") effect size  A = 2*|mean((y - ybar) e^{i theta})|  -- the peak-to-trough
    swing of the best-fitting first harmonic, in units of y.
  * "sawtooth" cliff scan: the best split of the circle into two half-frame arcs.
  * null: 4000 permutations of y WITHIN session x shot-type strata (breaks only the phase
    relation, keeps every session's own green rate and every shot type's own difficulty).
  * the same folds on non-phase covariates, so the phase effect can be compared against the
    reader-noise alternative on one scale.
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


def load(path):
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    return rows


def F(r, k, default=float("nan")):
    try:
        return float(r.get(k, ""))
    except (TypeError, ValueError):
        return default


def _perm_matrix(y, strata, nperm, rng):
    Y = np.tile(y, (nperm, 1))
    groups = defaultdict(list)
    for i, s in enumerate(strata):
        groups[s].append(i)
    for idx in groups.values():
        if len(idx) < 2:
            continue
        j = np.array(idx)
        Y[:, j] = rng.permuted(Y[:, j], axis=1)
    return Y


def sine_fold(theta, y, strata, nperm=4000, seed=7):
    e = np.exp(1j * theta)
    z = np.mean((y - y.mean()) * e)
    amp = 2.0 * abs(z)
    ang = (float(np.angle(z)) % TWO_PI) * P_MS / TWO_PI
    rng = np.random.default_rng(seed)
    Y = _perm_matrix(y, strata, nperm, rng)
    Z = np.mean((Y - Y.mean(axis=1, keepdims=True)) * e[None, :], axis=1)
    null = 2.0 * np.abs(Z)
    p = float((np.sum(null >= amp) + 1) / (nperm + 1))
    return dict(amp=amp, peak_ms=ang, p=p, null_med=float(np.median(null)),
                null_p95=float(np.percentile(null, 95)))


def saw_fold(theta, y, strata, nperm=4000, seed=11, ncuts=48):
    cuts = np.linspace(0.0, TWO_PI, ncuts, endpoint=False)
    rel = (theta[None, :] - cuts[:, None]) % TWO_PI
    A = rel < math.pi                                  # (ncuts, n)
    na = A.sum(axis=1).astype(float)
    nb = (~A).sum(axis=1).astype(float)
    ok = (na >= 4) & (nb >= 4)

    def stat(Y):                                        # Y: (m, n)
        sa = Y @ A.T.astype(float)                      # (m, ncuts)
        tot = Y.sum(axis=1, keepdims=True)
        sb = tot - sa
        with np.errstate(invalid="ignore", divide="ignore"):
            d = np.abs(sa / na[None, :] - sb / nb[None, :])
        d[:, ~ok] = -np.inf
        k = np.argmax(d, axis=1)
        return d[np.arange(Y.shape[0]), k], k

    obs, k = stat(y[None, :])
    rng = np.random.default_rng(seed)
    Y = _perm_matrix(y, strata, nperm, rng)
    null, _ = stat(Y)
    p = float((np.sum(null >= obs[0]) + 1) / (nperm + 1))
    return dict(height=float(obs[0]), cliff_ms=float(cuts[k[0]] * P_MS / TWO_PI), p=p,
                null_med=float(np.median(null)))


def outcomes(rows):
    return {
        "P(EXCELLENT)": np.array([1.0 if r["banner"] == "EXCELLENT" else 0.0 for r in rows]),
        "P(LATE)": np.array([1.0 if r["banner"] == "LATE" else 0.0 for r in rows]),
        "signed(LATE-EARLY)": np.array(
            [1.0 if r["banner"] == "LATE" else (-1.0 if r["banner"] == "EARLY" else 0.0) for r in rows]),
    }


PHASES = [
    ("phase_frame_ms", "release mod one frame of the SHOT'S OWN game-frame grid"),
    ("phase_engine_ms", "release mod 16.667 on the raw engine clock"),
    ("phase_wall_ms", "fire_epoch wall stamp mod 16.667"),
]


def fold_block(rows, label, out, nperm=4000, phases=PHASES):
    if len(rows) < 20:
        out.append(f"\n### {label}: n={len(rows)} -- too few, skipped")
        return []
    strata = [r["session"] + "|" + r.get("shot_type", "") for r in rows]
    ys = outcomes(rows)
    cnt = Counter(r["banner"] for r in rows)
    out.append(f"\n### {label}   n={len(rows)}  EXC {cnt['EXCELLENT']} / LATE {cnt['LATE']} / EARLY {cnt['EARLY']}")
    out.append("| phase | outcome | sine amp | null med | null p95 | peak @ms | p | saw height | cliff @ms | p |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    recs = []
    for pk, _pd in phases:
        v = np.array([F(r, pk) for r in rows])
        ok = np.isfinite(v)
        if ok.sum() < 20:
            continue
        theta = ((v[ok] % P_MS) / P_MS) * TWO_PI
        st = [s for s, o in zip(strata, ok) if o]
        for yn, yv in ys.items():
            s = sine_fold(theta, yv[ok], st, nperm)
            w = saw_fold(theta, yv[ok], st, nperm)
            out.append("| %s | %s | %.3f | %.3f | %.3f | %.2f | %.3f | %.3f | %.2f | %.3f |" % (
                pk, yn, s["amp"], s["null_med"], s["null_p95"], s["peak_ms"], s["p"],
                w["height"], w["cliff_ms"], w["p"]))
            recs.append(dict(block=label, phase=pk, outcome=yn, n=int(ok.sum()), **s,
                             saw_height=w["height"], saw_cliff_ms=w["cliff_ms"], saw_p=w["p"]))
    return recs


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default=r"D:\NexusVision\poll_phase\shots.csv")
    ap.add_argument("--out", default=r"D:\NexusVision\poll_phase")
    ap.add_argument("--perms", type=int, default=4000)
    args = ap.parse_args(argv)

    rows = load(args.shots)
    graded = [r for r in rows if r.get("banner") in ("EXCELLENT", "LATE", "EARLY")]
    out = [f"# poll-phase fold  (shots={len(rows)}, graded={len(graded)}, perms={args.perms})"]
    recs = []
    recs += fold_block(graded, "ALL graded vision releases", out, args.perms)
    recs += fold_block([r for r in graded if r.get("shot_type") == "Standstill"], "Standstill", out, args.perms)
    recs += fold_block([r for r in graded if "Fade" in r.get("shot_type", "")], "Fades (Left+Right)", out, args.perms)
    recs += fold_block([r for r in graded if r.get("shot_type") == "Right Fade"], "Right Fade alone", out, args.perms)
    recs += fold_block([r for r in graded if r.get("fire_target") == "frame_centre"], "fire_target=frame_centre", out, args.perms)
    recs += fold_block([r for r in graded if r.get("frame_phase_lock") == "1"], "grid LOCKED at dating", out, args.perms)
    for tempo in ("normal", "slow", "quick"):
        recs += fold_block([r for r in graded if r.get("tempo") == tempo], f"tempo={tempo}", out, args.perms)

    txt = "\n".join(out)
    with open(os.path.join(args.out, "fold.md"), "w", encoding="utf-8") as fh:
        fh.write(txt + "\n")
    with open(os.path.join(args.out, "fold.json"), "w", encoding="utf-8") as fh:
        json.dump(recs, fh, indent=1)
    print(txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
