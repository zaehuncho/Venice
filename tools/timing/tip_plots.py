#!/usr/bin/env python3
"""Visual proof for the tip-timing analysis. Imports tip_predictor_eval and renders a 4-panel figure:
  (1) make-rate vs latency (the dominant lever)   (2) real fill(t) curves with predicted vs actual tip
  (3) self-correction convergence (lead learned from 0)   (4) landing-error histogram at 70ms.
Saves tools/timing/tip_timing_proof.png.
"""
import os
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_predictor_eval as tpe
ff = tpe.ff

OUT = os.path.join(HERE, "tip_timing_proof.png")


def main():
    shots, _ = ff.load_csv_shots(ff.DEFAULT_CSV_GLOB)
    cache = [tpe.fusion_series(s) for s in shots]
    lats = [40, 50, 60, 70, 80, 90, 100, 110, 120]

    fig, ax = plt.subplots(2, 2, figsize=(13, 9))
    fig.suptitle("Autonomous tip-timing — pure vision (no green, no seeds), 179 real shots", fontsize=13, fontweight="bold")

    # (1) make-rate vs latency
    m20 = []; m30 = []
    for lat in lats:
        lead, e = tpe.best_fixed_lead(cache, float(lat), 80.0, 3, W=40)
        m20.append(100 * tpe.make_at(e, 40)); m30.append(100 * tpe.make_at(e, 60))
    a = ax[0, 0]
    a.plot(lats, m20, "o-", lw=2, color="#2E86FF", label="perfect tip (+/-20ms)")
    a.plot(lats, m30, "s--", lw=2, color="#38B000", label="good (+/-30ms)")
    a.axhline(100, color="#888", lw=1, ls=":", label="oracle (~100%)")
    a.set_xlabel("end-to-end latency (ms)"); a.set_ylabel("tip-hit rate (%)")
    a.set_title("(1) Make-rate is latency-limited — cut latency, win"); a.grid(alpha=0.3); a.legend(fontsize=8)
    a.invert_xaxis()

    # (2) example real fill curves (clean median-duration rises) with predicted vs actual tip
    a = ax[0, 1]
    clean = [k for k in range(len(shots))
             if 300 <= (shots[k]["t"][shots[k]["tip"]] - shots[k]["t"][0]) <= 460
             and shots[k]["tip"] >= 12]
    ex = clean[:: max(1, len(clean) // 3)][:3] if clean else [0]
    cols = ["#2E86FF", "#FF7B00", "#38B000"]
    for c, k in zip(cols, ex):
        s = shots[k]; tip = s["tip"]
        lo = int(np.argmin(s["fill"][:tip + 1]))                       # isolate the rise: from its foot
        lo = max(0, lo - 1); hi = min(len(s["fill"]), tip + 3)
        t = s["t"][lo:hi] - s["t"][lo]
        a.plot(t, s["fill"][lo:hi], "-o", color=c, alpha=0.85, lw=1.6, ms=3)
        a.axvline(s["t"][tip] - s["t"][lo], color=c, ls=":", lw=1.3)   # actual tip
        pm = {i: p for (i, _t, p, _f) in cache[k][0]}
        i5 = tip - 5
        if i5 in pm and pm[i5] is not None:                            # predicted tip time from 5 frames (~83ms) out
            a.plot((s["t"][i5] - s["t"][lo]) + pm[i5], float(s["fill"][tip]), "v", color=c, ms=11, mec="k")
    a.set_xlabel("time since rise foot (ms)"); a.set_ylabel("meter fill %")
    a.set_title("(2) Real rises: predicted tip (v) vs actual tip (:)"); a.grid(alpha=0.3)

    # (3) convergence on one real session
    a = ax[1, 0]
    sess, _ = ff.load_csv_shots(os.path.join(ff.ROOT, "logs", "diagnostics", "detframes_live_133101.csv"))
    scache = [tpe.fusion_series(s) for s in sess]
    for lat, col in ((70.0, "#2E86FF"), (90.0, "#FF7B00")):
        leads, _e = tpe.online_converge(scache, lat)
        a.plot(range(1, len(leads) + 1), leads, "-", color=col, lw=1.8, label=f"true latency {lat:.0f}ms")
    a.set_xlabel("shot # (within one live session)"); a.set_ylabel("learned lead_est (ms)")
    a.set_title("(3) Self-correction: lead learned from 0 — no hardcode"); a.grid(alpha=0.3); a.legend(fontsize=8)

    # (4) landing-error histogram at 70ms
    a = ax[1, 1]
    lead70, e70 = tpe.best_fixed_lead(cache, 70.0, 80.0, 3, W=40)
    a.hist(np.clip(e70, -120, 120), bins=np.arange(-120, 125, 16.7), color="#2E86FF", alpha=0.8, edgecolor="white")
    a.axvline(0, color="#111", lw=2); a.axvspan(-20, 20, color="#38B000", alpha=0.15, label="+/-20ms perfect")
    a.set_xlabel("release landing error vs tip (ms), 70ms latency"); a.set_ylabel("shots")
    a.set_title(f"(4) Landing errors @70ms (lead {lead70:.0f}ms)"); a.grid(alpha=0.3); a.legend(fontsize=8)

    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(OUT, dpi=120)
    print(f"[wrote {OUT}]")


if __name__ == "__main__":
    main()
