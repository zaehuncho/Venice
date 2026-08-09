#!/usr/bin/env python3
"""TIP-TIMING: PHASE/TEMPLATE REGISTRATION prototype + COMMIT-HORIZON benchmark.

ANALYSIS ONLY. Does not touch the engine or the shipped predictors. Prototypes the #1 consensus
idea (6/6 external AIs): stop extrapolating fill; do PHASE/TEMPLATE REGISTRATION.

    fill(t) = fmin + A * g((t - t0) / T)

where g is a FIXED normalized animation shape (learned, self-supervised, from the corpus) and only
(t0, T[, A]) vary per shot. Estimate (t0, T) from the EARLY, STEEP, high-SNR part of the rise; the
tip time is t0 + T * u_tip. Claim under test: this stays sharp at the FAR (commit) horizon where the
const-accel Kalman / CNN forecaster fall apart -- which is what matters, because at ~200-260ms latency
the bot must COMMIT the release ~12-16 frames BEFORE the tip.

Benchmarks REGISTRATION against the existing KALMAN / FORECASTER / FUSION predictors (reused verbatim
from tip_predictor_eval.py) on the SAME real shots, with the metric that actually matters:
  * COMMIT-HORIZON tip error -- predicted-tip error at the frame ~200/260ms before the true tip.
  * error-vs-(time-before-tip) curve for every predictor.
  * achievable +/-20 / +/-30ms make-rate at the correctly-learned lead.
Template is LEAVE-ONE-SESSION-OUT (never fit on the test shot's session).

Run:  C:\\Python314\\python.exe tools/timing/tip_registration_eval.py
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np
from scipy.interpolate import PchipInterpolator
from scipy.optimize import least_squares

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "diagnostics"))

import fill_forecaster as ff                      # loader + CNN (reuse; do not duplicate)
import tip_predictor_eval as tpe                  # KALMAN / FORECASTER / FUSION series (reuse verbatim)

MS_PER_FRAME = ff.MS_PER_FRAME


# =================================================================================================
# Per-session shot loading (ff.load_csv_shots drops the session tag; we need it for LOSO).
# =================================================================================================
# A real 2K shot-meter animation fills in ~150-700ms. Segments whose resampled global-argmax "tip" lands
# far past that are detector artifacts (meter plateau / two merged rises / HUD wiggle), NOT one animation.
# We drop them for EVERY predictor equally (the segmenter's own raw gate is <=700ms; resampling re-picks a
# late argmax that this restores). Reported so it is not silent.
MAX_REAL_RISE_MS = 700.0


def load_shots_by_session(max_rise=MAX_REAL_RISE_MS):
    sessions = {}
    dropped = 0
    for path in sorted(glob.glob(ff.DEFAULT_CSV_GLOB)):
        s, _ = ff.load_csv_shots(path)
        keep = [x for x in s if shot_rise_dur(x) <= max_rise]
        dropped += len(s) - len(keep)
        if keep:
            sessions[os.path.basename(path)] = keep
    return sessions, dropped


def shot_rise_dur(s):
    return float(s["t"][s["tip"]] - s["t"][0])


# =================================================================================================
# TEMPLATE LEARNING (self-supervised registration).
#   Represent g on a fixed phase grid U in [0, U_MAX]; tip lands near u=1 by construction.
#   Iterate: (1) register each FULL training shot -> (t0,T) by LSQ against current g; (2) rebuild g
#   as the kernel-smoothed, isotonic (monotone) average of all time-warped curves.
# =================================================================================================
U_MAX = 1.25
U_GRID = np.linspace(0.0, U_MAX, 64)


def _amp_normalize(s):
    """y = (fill - fmin)/(peak - fmin) over the rise (0 at start, ~1 at tip). Returns t, y, fmin, A."""
    tip = s["tip"]
    t = np.asarray(s["t"][: tip + 1], float)
    f = np.asarray(s["fill"][: tip + 1], float)
    fmin = float(np.min(f))
    peak = float(f[-1])
    A = max(peak - fmin, 1e-6)
    y = (f - fmin) / A
    return t, np.clip(y, -0.05, 1.15), fmin, A


def _eval_g(g_vals, u):
    return np.interp(np.clip(u, 0.0, U_MAX), U_GRID, g_vals)


def _register_full(t, y, g_vals, t0_init, T_init):
    """Fit (t0, T) of a FULL amplitude-normalized shot to the current template g. Returns (t0, T)."""
    def resid(p):
        t0, T = p
        T = max(T, 20.0)
        u = (t - t0) / T
        return y - _eval_g(g_vals, u)

    lo = [t0_init - 0.6 * T_init, 0.4 * T_init]
    hi = [t0_init + 0.6 * T_init, 2.2 * T_init]
    p0 = [np.clip(t0_init, lo[0], hi[0]), np.clip(T_init, lo[1], hi[1])]
    try:
        r = least_squares(resid, p0, bounds=(lo, hi), max_nfev=80)
        return float(r.x[0]), float(max(r.x[1], 20.0))
    except Exception:
        return t0_init, T_init


def _isotonic(y, w):
    """Pool-adjacent-violators -> nondecreasing fit (weighted)."""
    y = y.astype(float).copy()
    w = w.astype(float).copy()
    n = len(y)
    vals = list(y)
    wts = list(w)
    idx = [[i] for i in range(n)]
    i = 0
    while i < len(vals) - 1:
        if vals[i] > vals[i + 1] + 1e-12:
            nv = (vals[i] * wts[i] + vals[i + 1] * wts[i + 1]) / (wts[i] + wts[i + 1])
            vals[i] = nv
            wts[i] += wts[i + 1]
            idx[i] += idx[i + 1]
            del vals[i + 1], wts[i + 1], idx[i + 1]
            if i > 0:
                i -= 1
        else:
            i += 1
    out = np.zeros(n)
    for v, ii in zip(vals, idx):
        for k in ii:
            out[k] = v
    return out


def learn_template(train_shots, iters=6):
    """Return (g_vals on U_GRID as a monotone PCHIP-ready array, u_tip)."""
    reg = []
    for s in train_shots:
        t, y, fmin, A = _amp_normalize(s)
        reg.append([t, y, float(t[0]), max(float(t[-1] - t[0]), 20.0)])

    # init template = linear-ish; then iterate
    g_vals = np.clip(U_GRID / 1.0, 0.0, 1.0)
    u_tip = 1.0
    for _ in range(iters):
        # (1) register each full shot to current g
        for e in reg:
            t, y, t0, T = e
            e[2], e[3] = _register_full(t, y, g_vals, t0, T)
        # (2) rebuild g: gather (u, y) from all shots, kernel-smooth onto U_GRID, isotonic-monotone
        us, ys = [], []
        tip_us = []
        for (t, y, t0, T) in reg:
            u = (t - t0) / T
            us.append(u)
            ys.append(y)
            tip_us.append(u[-1])                       # tip phase for this shot
        us = np.concatenate(us)
        ys = np.concatenate(ys)
        bw = 0.06
        new = np.zeros_like(U_GRID)
        wsum = np.zeros_like(U_GRID)
        for gi, ug in enumerate(U_GRID):
            wk = np.exp(-0.5 * ((us - ug) / bw) ** 2)
            sw = wk.sum()
            if sw > 1e-6:
                new[gi] = (wk * ys).sum() / sw
                wsum[gi] = sw
        # anchor endpoints, weighted isotonic, normalize so g(u_tip)=1
        wsum = np.maximum(wsum, 1e-6)
        new = _isotonic(new, wsum)
        new = new - new[0]
        u_tip = float(np.median(tip_us))
        gtip = max(_eval_g(new, u_tip), 1e-6)
        new = new / gtip                               # g(u_tip) == 1
        g_vals = new
    # enforce strict monotonicity for a clean inverse
    g_vals = np.maximum.accumulate(g_vals)
    g_vals = g_vals + 1e-4 * U_GRID                    # tiny slope so g is invertible
    return g_vals, u_tip


def template_spline(g_vals):
    return PchipInterpolator(U_GRID, g_vals, extrapolate=True)


# =================================================================================================
# ONLINE REGISTRATION ESTIMATOR (the predictor under test).
#   At frame k, using rise samples up to k: fit (t0, T, A) to fill via weighted robust LSQ against g,
#   with soft priors on (T, A) so fast/early shots degrade gracefully to the prior. CENSOR near-cap
#   samples (fill>=97%, g'->0). Predicted tip = t0 + T*u_tip.
# =================================================================================================
class Registration:
    def __init__(self, g_vals, u_tip, T_prior, A_prior, fmin_prior):
        self.g = template_spline(g_vals)
        self.dg = self.g.derivative()
        self.u_tip = u_tip
        self.T_prior = T_prior
        self.A_prior = A_prior
        self.fmin_prior = fmin_prior

    def _g(self, u):
        return self.g(np.clip(u, 0.0, U_MAX))

    def predict(self, t_hist, f_hist):
        """t_hist, f_hist: arrays of the rise so far (ms, fill%). Returns predicted tip time (ms) or None."""
        t = np.asarray(t_hist, float)
        f = np.asarray(f_hist, float)
        if len(t) < 3:
            return None
        fmin = float(np.min(f))
        # CENSOR near-cap samples (no timing info once the animation decelerates into the tip)
        keep = f < 97.0
        if keep.sum() < 2:                             # already capped -> almost at tip; use last samples
            keep = np.ones_like(f, bool)
        tk = t[keep]
        fk = f[keep]
        A0 = max(self.A_prior, 5.0)
        T0 = self.T_prior
        t0_0 = tk[0] - 0.05 * T0
        lamT = 1.5      # prior strength on T (in residual units of "one normalized point")
        lamA = 1.0

        def resid(p):
            t0, T, A = p
            T = max(T, 30.0)
            A = max(A, 5.0)
            u = (tk - t0) / T
            model = fmin + A * self._g(u)
            w = np.sqrt(np.abs(self.dg(np.clip(u, 0.0, U_MAX)))) + 0.05   # weight steep, high-SNR samples
            data_r = w * (fk - model) / 8.0            # /8 ~ fill-noise scale (%) -> unit residuals
            prior_r = np.array([lamT * (T - T0) / T0, lamA * (A - A0) / A0])
            return np.concatenate([data_r, prior_r])

        lo = [tk[0] - 1.2 * T0, 0.35 * T0, 0.5 * A0]
        hi = [tk[-1] + 0.2 * T0, 2.5 * T0, 1.6 * A0]
        p0 = [np.clip(t0_0, lo[0], hi[0]), T0, A0]
        try:
            r = least_squares(resid, p0, bounds=(lo, hi), loss="soft_l1", f_scale=1.0, max_nfev=60)
            t0, T, A = r.x
            return float(t0 + T * self.u_tip)
        except Exception:
            return None


def registration_series(shot, reg):
    """Match the (i, t_ms, pred_ms_to_tip, fill) shape of tpe predictor series."""
    t = shot["t"]; fill = shot["fill"]; tip = shot["tip"]
    out = []
    for i in range(3, len(t)):
        pt = reg.predict(t[: i + 1], fill[: i + 1])
        p = None if pt is None else (pt - float(t[i]))
        out.append((i, float(t[i]), p, float(fill[i])))
    return out, tip, float(t[tip])


# =================================================================================================
# COMMIT-HORIZON evaluation.
# =================================================================================================
def commit_frame_idx(series, t_tip, lead_ms):
    """Last frame whose time is <= t_tip - lead_ms (the frame at which the bot must COMMIT). None if the
    whole rise is shorter than lead_ms (fast shot: must commit before any observation)."""
    best = None
    for (i, t_i, p, fl) in series:
        if t_i <= t_tip - lead_ms:
            best = (i, t_i, p, fl)
        else:
            break
    return best


def _commit_errs(cache, L):
    """dict shot-index -> signed tip error at commit frame L ms before tip (only covered shots)."""
    out = {}
    for si, (series, tip, t_tip) in enumerate(cache):
        cf = commit_frame_idx(series, t_tip, L)
        if cf is None or cf[2] is None:
            continue
        out[si] = (cf[1] + cf[2]) - t_tip
    return out


def _stats_row(name, e, cov, tot):
    if len(e) >= 5:
        e = np.asarray(e)
        return (f"    {name:>12} {cov:>3}/{tot:<3} {np.median(e):>+7.1f} "
                f"{np.percentile(e,75)-np.percentile(e,25):>6.1f} {np.mean(np.abs(e)):>6.1f} "
                f"{np.percentile(np.abs(e),90):>6.1f}")
    return f"    {name:>12} {cov:>3}/{tot:<3}   (too few)"


def commit_horizon_table(caches, names, leads=(200.0, 260.0)):
    print("\n(A) COMMIT-HORIZON TIP ERROR  (predicted tip - true tip, at the frame ~L ms before the tip)")
    print("    err>0 = predicts LATE. 'covered' = shots whose rise reaches back to the commit frame.")
    tot = len(next(iter(caches.values())))
    for L in leads:
        print(f"\n  --- commit lead L = {int(L)}ms before tip ---")
        emaps = {name: _commit_errs(caches[name], L) for name in names}
        common = set.intersection(*[set(emaps[n].keys()) for n in names]) if names else set()
        print(f"    [all covered shots per predictor]")
        print(f"    {'predictor':>12} {'cov':>7} {'median':>8} {'IQR':>7} {'MAE':>7} {'p90|e|':>7}")
        for name in names:
            e = list(emaps[name].values())
            print(_stats_row(name, e, len(e), tot))
        print(f"    [COMMON covered set: {len(common)} shots ALL predictors reach -- apples-to-apples]")
        for name in names:
            e = [emaps[name][si] for si in common]
            print(_stats_row(name, e, len(common), tot))


def commit_makerate(caches, names, leads=(200.0, 260.0)):
    """The number the user asked for: achievable +/-20 / +/-30ms make-rate if the bot COMMITS at the
    commit horizon using the predicted tip. RAW = as-is; DEBIASED = after removing the predictor's own
    median bias (the engine's online self-correction loop learns exactly this constant -> the realistic
    ceiling). Bias removed leave-one-out to avoid self-fitting."""
    print("\n(C2) COMMIT-HORIZON MAKE-RATE  (if the bot fires 200/260ms out from the predicted tip)")
    print("     DEBIASED = predictor's median bias removed (the self-correction loop learns that constant).")
    for L in leads:
        emaps = {name: _commit_errs(caches[name], L) for name in names}
        print(f"\n  --- L = {int(L)}ms ---   {'predictor':>12} {'n':>4} "
              f"{'raw+/-20':>9} {'raw+/-30':>9} {'deb+/-20':>9} {'deb+/-30':>9}")
        for name in names:
            e = np.array(list(emaps[name].values()))
            if len(e) < 5:
                continue
            de = e - np.median(e)
            print(f"  {'':<26} {name:>12} {len(e):>4} "
                  f"{100*np.mean(np.abs(e)<=20):>7.0f}% {100*np.mean(np.abs(e)<=30):>8.0f}% "
                  f"{100*np.mean(np.abs(de)<=20):>7.0f}% {100*np.mean(np.abs(de)<=30):>8.0f}%")


def error_vs_horizon(caches, names, horizons=(80, 120, 160, 200, 240, 280, 320)):
    print("\n(B) ERROR vs TIME-BEFORE-TIP  (MAE of predicted-tip error, ms; nearest frame to each horizon)")
    hdr = "    {:>12}".format("horizon->") + "".join(f"{h:>7}" for h in horizons)
    print(hdr)
    for name in names:
        cells = []
        for h in horizons:
            errs = []
            for (series, tip, t_tip) in caches[name]:
                cand = [(abs((t_tip - t_i) - h), t_i, p) for (i, t_i, p, fl) in series if p is not None]
                cand = [c for c in cand if c[0] <= MS_PER_FRAME]   # within one frame of the horizon
                if not cand:
                    continue
                _, t_i, p = min(cand, key=lambda z: z[0])
                errs.append((t_i + p) - t_tip)
            cells.append(f"{np.mean(np.abs(errs)):>6.0f}" if len(errs) >= 5 else "    --")
        print("    {:>12}".format(name) + "".join(f"{c:>7}" for c in cells))
    print(f"    (n varies per cell; fast shots drop out at the far horizons -- that is the real story)")


# =================================================================================================
# Achievable make-rate at the correctly-learned lead (reuse tpe machinery on the same series cache).
# =================================================================================================
def make_rate_block(caches, names, lats=(70.0, 110.0, 200.0, 260.0), min_fill=80.0, med_win=3):
    print("\n(C) ACHIEVABLE MAKE-RATE at the correctly-learned lead  (fire policy = tpe fill-gate+median)")
    print(f"    {'predictor':>12} {'latency':>8} {'lead':>6} {'+/-20ms':>8} {'+/-30ms':>8} {'median':>8} {'IQR':>7}")
    print("    " + "-" * 62)
    for name in names:
        for lat in lats:
            lead, e = tpe.best_fixed_lead(caches[name], lat, min_fill, med_win, W=40)
            if len(e) == 0:
                continue
            m20 = tpe.make_at(e, 40); m30 = tpe.make_at(e, 60)
            med = float(np.median(e)); iqr = float(np.percentile(e, 75) - np.percentile(e, 25))
            print(f"    {name:>12} {int(lat):>6}ms {lead:>4.0f}ms {100*m20:>6.0f}% {100*m30:>7.0f}% "
                  f"{med:>+7.1f} {iqr:>6.1f}")


# =================================================================================================
def build_registration_cache(sessions, mode="global"):
    """LEAVE-ONE-SESSION-OUT registration series. mode:
        'global'       one global g, one global (T,A) prior.
        'global_typeT' one global g, but (T,A) prior taken from the shot's fast/med/slow tercile.
        'pertype'      separate g AND (T,A) prior per tercile.
    """
    all_shots = [s for ss in sessions.values() for s in ss]
    durs = np.array([shot_rise_dur(s) for s in all_shots])
    q33, q66 = np.percentile(durs, [33, 66])

    def dur_type(s):
        d = shot_rise_dur(s)
        return 0 if d <= q33 else (1 if d <= q66 else 2)

    def priors(pool):
        return (float(np.median([shot_rise_dur(x) for x in pool])),
                float(np.median([float(x["fill"][x["tip"]] - np.min(x["fill"][: x["tip"] + 1])) for x in pool])),
                float(np.median([float(np.min(x["fill"][: x["tip"] + 1])) for x in pool])))

    cache = []
    for held_out, test_shots in sessions.items():
        train = [s for name, ss in sessions.items() if name != held_out for s in ss]
        gglobal = learn_template(train)
        gts = {}
        if mode == "pertype":
            for ty in (0, 1, 2):
                tr_ty = [s for s in train if dur_type(s) == ty]
                if len(tr_ty) >= 8:
                    gts[ty] = learn_template(tr_ty)
        for s in test_shots:
            ty = dur_type(s)
            if mode == "pertype":
                g_vals, u_tip = gts.get(ty, gglobal)
                pool = [x for x in train if dur_type(x) == ty] or train
            elif mode == "global_typeT":
                g_vals, u_tip = gglobal
                pool = [x for x in train if dur_type(x) == ty] or train
            else:
                g_vals, u_tip = gglobal
                pool = train
            T_prior, A_prior, fmin_prior = priors(pool)
            reg = Registration(g_vals, u_tip, T_prior, A_prior, fmin_prior)
            cache.append(registration_series(s, reg))
    return cache, (q33, q66)


def main():
    print("=" * 90)
    print("TIP-TIMING PHASE/TEMPLATE REGISTRATION -- prototype + commit-horizon benchmark (analysis only)")
    print("=" * 90)
    sessions, dropped = load_shots_by_session()
    all_shots = [s for ss in sessions.values() for s in ss]
    durs = np.array([shot_rise_dur(s) for s in all_shots])
    print(f"corpus: {len(all_shots)} plausible shots / {len(sessions)} sessions "
          f"({dropped} artifact segments >{MAX_REAL_RISE_MS:.0f}ms dropped for ALL predictors) | "
          f"rise-dur median={np.median(durs):.0f}ms IQR[{np.percentile(durs,25):.0f}-{np.percentile(durs,75):.0f}]")

    # ---- baseline predictor caches (reuse tpe series verbatim) ----
    print("building KALMAN / FORECASTER / FUSION caches (reused verbatim from tip_predictor_eval)...")
    base_caches = {name: [fn(s) for s in all_shots] for name, fn in tpe.PREDICTORS.items()}

    # ---- registration caches (LOSO) ----
    print("learning LEAVE-ONE-SESSION-OUT templates + registration series...")
    reg_cache_g, (q33, q66) = build_registration_cache(sessions, mode="global")
    print(f"  fast/slow tercile cuts: <= {q33:.0f}ms (fast) .. > {q66:.0f}ms (slow)")
    reg_cache_gt, _ = build_registration_cache(sessions, mode="global_typeT")
    reg_cache_t, _ = build_registration_cache(sessions, mode="pertype")

    caches = dict(base_caches)
    caches["REG-global"] = reg_cache_g
    caches["REG-gT"] = reg_cache_gt          # global g + per-type T prior (user option 1)
    caches["REG-pertype"] = reg_cache_t
    names = ["KALMAN", "FORECASTER", "FUSION", "REG-global", "REG-gT", "REG-pertype"]

    commit_horizon_table(caches, names, leads=(200.0, 260.0))
    error_vs_horizon(caches, names)
    commit_makerate(caches, names)
    make_rate_block(caches, names)

    # ---- fast vs slow split at the commit horizon (the key caveat) ----
    print("\n(D) COMMIT-HORIZON (L=200ms) split by shot speed  (fast shots are prior-dominated)")
    tercile = {}
    for s in all_shots:
        d = shot_rise_dur(s)
        tercile[id(s)] = 0 if d <= q33 else (1 if d <= q66 else 2)
    labels = {0: "FAST", 1: "MED ", 2: "SLOW"}
    print(f"    {'predictor':>12} {'group':>6} {'cov':>7} {'median':>8} {'MAE':>7}")
    for name in names:
        for grp in (0, 1, 2):
            errs = []; cov = 0; tot = 0
            for (series, tip, t_tip), s in zip(caches[name], all_shots):
                if tercile[id(s)] != grp:
                    continue
                tot += 1
                cf = commit_frame_idx(series, t_tip, 200.0)
                if cf is None or cf[2] is None:
                    continue
                cov += 1
                errs.append((cf[1] + cf[2]) - t_tip)
            if len(errs) >= 3:
                print(f"    {name:>12} {labels[grp]:>6} {cov:>3}/{tot:<3} "
                      f"{np.median(errs):>+7.1f} {np.mean(np.abs(errs)):>6.1f}")
            else:
                print(f"    {name:>12} {labels[grp]:>6} {cov:>3}/{tot:<3}   (too few covered)")

    print("\n" + "=" * 90)
    print("Notes: baseline caches include the pre-trained CNN forecaster (trained on the full corpus incl.")
    print("test shots -> a small leak FAVORING the baselines). Registration is strict leave-one-session-out.")
    print("=" * 90)


if __name__ == "__main__":
    main()
