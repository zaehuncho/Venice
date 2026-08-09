"""[ORION_STOP_SUBFRAME] Offline validation for sub-frame end-of-rise stop dating.

    python tools/timing/stop_subframe_eval.py
    python tools/timing/stop_subframe_eval.py --frames logs/diagnostics/detframes_20260806_*.csv
    python tools/timing/stop_subframe_eval.py --no-corroborate

Replays the ENGINE'S OWN stop detector (AutomationEngine.cpp [ORION_TIP_PHASE], step/quiet =
2.0pp/100ms, optional [ORION_STOP_CORROBORATE] pending-reopen logic) over the recorded per-frame
fill record, in two variants:

  snapped   -- the shipped dating: stop = wall time of the sample that tripped the step
               threshold (exactly what feeds the phase learner today), and
  subframe  -- the stop_dating_subframe estimator: intersect a line fit through the trailing
               strictly-rising samples with the settled-plateau median, clamped to the observed
               below/at-plateau straddle (mirrors AutomationEngine::refinePhaseStopSubframeMs).

and reports:

  1. SPLIT-HALF DATING NOISE (the residual_split.py method): date the stop on even-only vs
     odd-only capture frames; the disagreement measures frame-sampling+estimator noise
     including the component shared by engine and CSV instruments. Reported for the engine
     replica snapped vs subframe, plus the CSV instrument (adp.stop_features) for continuity
     with the previously reported 5.2ms number.
  2. SNAP BIAS: median (snapped - subframe corner). This is the measured value of
     kStopSubframeRecenterMs in AutomationEngine.cpp -- the constant that keeps the learned
     phase constant's MEAN unchanged when the flag flips (flipping the flag must not re-aim).
  3. THE "6.04ms ANIMATION FLOOR", re-derived with sub-frame dating on BOTH endpoints
     (anchor = interpolated rung crossing, stop = subframe corner): within-shot-type spread of
     anchor->stop under snapped vs subframe dating, plus the variance-subtraction of the
     measured dating noise. The floor was measured between a sub-frame anchor and a
     FRAME-SNAPPED stop; if it collapses here it was instrument, not physics.

CAVEAT: existing detframes CSVs carry wall_ms at integer ms (the %.0f truncation fixed
2026-08-06), which adds ~0.29ms rms to every dated time here. That floor is common to both
variants, so the snapped-vs-subframe comparison stands; absolute noise numbers are ~0.3ms
pessimistic.

Read-only: touches no engine/reader/settings/learning files.
"""

import argparse
import glob
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import animation_duration_predictors as adp  # noqa: E402

LOGS = ["logs/orion_native.log.1", "logs/orion_native.log"]

# RemapConfig defaults (AutomationEngine.h) -- the engine's stop detector constants.
STEP_PCT = 2.0
QUIET_MS = 100.0
# refinePhaseStopSubframeMs constants (AutomationEngine.cpp [ORION_STOP_SUBFRAME]).
PLATEAU_MARGIN_PCT = 1.0
MIN_RISE_SLOPE = 0.05
RISE_FIT_SAMPLES = 4


def engine_snapped_stop(post, run_max0, corroborate=True):
    """Replica of the AutomationEngine end-of-rise detector on (w, f) samples after release.
    Returns (t_stop, confirmed) or (None, False). `post` must be time-sorted."""
    run_max = run_max0
    t_stop = None
    confirmed = False
    pend_ms = None
    pend_fill = None
    pend_prior = None
    for r in post:
        f, w = r["f"], r["w"]
        if not confirmed:
            if corroborate:
                if f > run_max + STEP_PCT:
                    if pend_ms is not None and f > pend_prior:
                        run_max = max(f, pend_fill)
                        t_stop = pend_ms
                        pend_ms = None
                    else:
                        pend_ms, pend_fill, pend_prior = w, f, run_max
                elif f > run_max:
                    run_max = f
                    pend_ms = None
                else:
                    pend_ms = None
            else:
                if f > run_max + STEP_PCT:
                    run_max = f
                    t_stop = w
                elif f > run_max:
                    run_max = f
            if t_stop is not None and w - t_stop >= QUIET_MS:
                confirmed = True
    return t_stop, confirmed


def subframe_stop(post, t_snap):
    """Replica of AutomationEngine::refinePhaseStopSubframeMs on the same samples.
    Returns the refined corner time or None (caller falls back to t_snap)."""
    if t_snap is None or len(post) < 4:
        return None
    plateau = [r["f"] for r in post if t_snap < r["w"] <= t_snap + QUIET_MS]
    if len(plateau) < 2:
        return None
    plateau_pct = float(np.median(plateau))
    ceiling = plateau_pct - PLATEAU_MARGIN_PCT
    lo_idx = None
    for i, r in enumerate(post):
        if r["w"] <= t_snap and r["f"] < ceiling:
            lo_idx = i
    if lo_idx is None:
        return None
    t_lo = post[lo_idx]["w"]
    t_hi = None
    for r in post[lo_idx + 1:]:
        if r["f"] >= ceiling:
            t_hi = r["w"]
            break
    if t_hi is None or t_hi <= t_lo:
        return None
    # trailing strictly-rising run ending at lo_idx
    rise = [post[lo_idx]]
    i = lo_idx - 1
    while i >= 0 and len(rise) < RISE_FIT_SAMPLES:
        if post[i]["f"] < rise[-1]["f"] - 1e-9 and post[i]["w"] < rise[-1]["w"]:
            rise.append(post[i])
            i -= 1
        else:
            break
    if len(rise) < 2:
        return None
    t = np.array([r["w"] for r in rise], dtype=float)
    f = np.array([r["f"] for r in rise], dtype=float)
    t0 = t.min()
    A = np.column_stack([np.ones(len(t)), t - t0])
    coef, *_ = np.linalg.lstsq(A, f, rcond=None)
    slope = float(coef[1])
    if not np.isfinite(slope) or slope < MIN_RISE_SLOPE:
        return None
    t_corner = t0 + (plateau_pct - float(coef[0])) / slope
    if not np.isfinite(t_corner):
        return None
    return float(np.clip(t_corner, t_lo, t_hi))


def date_stop(ep, t_rel, corroborate, refine):
    """Full pipeline on one (episode, release): seed run max at the release fill like
    startPostReleaseMeterCapture does, then snap (and optionally refine)."""
    pre = [r for r in ep if r["w"] <= t_rel]
    post = [r for r in ep if r["w"] > t_rel]
    if not pre or len(post) < 4:
        return None, None
    run_max0 = pre[-1]["f"]
    t_snap, confirmed = engine_snapped_stop(post, run_max0, corroborate)
    if t_snap is None or not confirmed:
        return None, None
    t_sub = subframe_stop(post, t_snap) if refine else None
    return t_snap, t_sub


def describe(name, v, unit="ms"):
    v = np.asarray(v, dtype=float)
    v = v[~np.isnan(v)]
    if len(v) < 2:
        print("%-40s (n=%d, too few)" % (name, len(v)))
        return None
    rsd = 1.4826 * np.median(np.abs(v - np.median(v)))
    print("%-40s n=%-4d med=%8.2f mean=%8.2f sd=%6.2f rSD=%6.2f min=%7.1f max=%7.1f %s"
          % (name, len(v), np.median(v), v.mean(), v.std(ddof=1), rsd,
             v.min(), v.max(), unit))
    return float(v.std(ddof=1))


def robust(v, cut=60.0):
    v = np.asarray(v, dtype=float)
    v = v[~np.isnan(v)]
    if len(v) == 0:
        return v
    return v[np.abs(v - np.median(v)) < cut]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", nargs="+", default=None,
                    help="detframes CSVs (default: the on-disk Aug-04..06 sessions)")
    ap.add_argument("--no-corroborate", action="store_true",
                    help="replicate the pre-stop_reopen_corroborate detector")
    ap.add_argument("--anchor-level", type=float, default=30.0)
    args = ap.parse_args()
    np.seterr(all="ignore")
    corroborate = not args.no_corroborate

    frames_files = args.frames
    if not frames_files:
        frames_files = sorted(
            glob.glob("logs/diagnostics/detframes_2026080[456]_*.csv"))
        if os.path.exists("logs/diagnostics/detframes.csv"):
            frames_files.append("logs/diagnostics/detframes.csv")
    frames_files = [f for f in frames_files if os.path.exists(f)]
    print("frames files (%d): %s" % (len(frames_files),
                                     ", ".join(os.path.basename(f) for f in frames_files)))
    rows = adp.load_frames(frames_files, -math.inf, math.inf)
    if not rows:
        print("no frame rows; nothing to do")
        return
    t0, t1 = rows[0]["w"] - 10_000, rows[-1]["w"] + 10_000
    eps = adp.episodes(rows)
    print("frame rows: %d  episodes: %d  span: %.1f h"
          % (len(rows), len(eps), (t1 - t0) / 3.6e6))

    phases, issued, marker = [], {}, {}
    for lg in LOGS:
        if not os.path.exists(lg):
            continue
        p, i, _a, _t, mk = adp.parse_log(lg, t0, t1)
        phases.extend(p)
        issued.update(i)
        marker.update(mk)
    # de-dup phases across rotated logs
    seen, ph_dedup = set(), []
    for p in sorted(phases, key=lambda x: x["ts"]):
        k = (round(p["ts"]), p["raw"])
        if k not in seen:
            seen.add(k)
            ph_dedup.append(p)
    phases = ph_dedup
    print("log join: %d phase lines, %d issued, %d markers"
          % (len(phases), len(issued), len(marker)))

    # ---- per-release dating ------------------------------------------------
    shots = []
    for seq, mk in sorted(marker.items()):
        t_rel = mk["wall"]
        ep = next((e for e in eps
                   if e[0]["w"] - 150 <= t_rel <= e[-1]["w"] + 150), None)
        if ep is None:
            continue
        t_snap, t_sub = date_stop(ep, t_rel, corroborate, refine=True)
        if t_snap is None:
            continue
        s = dict(seq=seq, t_rel=t_rel, ep=ep, t_snap=t_snap, t_sub=t_sub)
        # anchor: interpolated LAST upward crossing of the rung before the release
        s["t_anchor"] = adp.crossing(ep, args.anchor_level,
                                     t_max=t_rel, t_min=t_rel - 700.0)
        # shot type via the engine's own PHASE SAMPLE line (closest one within 5s after rel)
        ph = next((p for p in phases if 0 <= p["ts"] - t_rel < 5000), None)
        s["shot_type"] = ph["shot_type"] if ph else "unknown"
        # split-half on the SAME episode: even/odd frames -> full pipeline each half
        ev = [r for i2, r in enumerate(ep) if i2 % 2 == 0]
        od = [r for i2, r in enumerate(ep) if i2 % 2 == 1]
        for tag, half in (("ev", ev), ("od", od)):
            hs, hb = date_stop(half, t_rel, corroborate, refine=True)
            s["snap_" + tag] = hs
            s["sub_" + tag] = (hb if hb is not None else hs)
        csv_sh = None
        a = adp.stop_features(ev, t_rel)
        b = adp.stop_features(od, t_rel)
        if a is not None and b is not None:
            csv_sh = a["t_stop"] - b["t_stop"]
        s["csv_sh"] = csv_sh
        shots.append(s)
    print("dated releases: %d  (subframe refinable: %d)"
          % (len(shots), sum(1 for s in shots if s["t_sub"] is not None)))
    if len(shots) < 8:
        print("too few joined releases; aborting")
        return

    # ---- 1. split-half dating noise ---------------------------------------
    print("\n== 1. split-half stop-dating noise (even vs odd frames, 30fps halves) ==")
    d_snap = robust([s["snap_ev"] - s["snap_od"] for s in shots
                     if s.get("snap_ev") is not None and s.get("snap_od") is not None])
    d_sub = robust([s["sub_ev"] - s["sub_od"] for s in shots
                    if s.get("sub_ev") is not None and s.get("sub_od") is not None])
    d_csv = robust([s["csv_sh"] for s in shots if s.get("csv_sh") is not None])
    for name, d in (("engine SNAPPED even-odd", d_snap),
                    ("engine SUBFRAME even-odd", d_sub),
                    ("csv instrument (stop_features)", d_csv)):
        sd = describe(name, d)
        if sd and len(d) > 4:
            sd30 = sd / math.sqrt(2.0)
            sd60 = sd30 / 2.0
            print("    -> implied per-instrument 30fps sd = %.2f ms;  60fps ~ %.2f ms"
                  % (sd30, sd60))

    # ---- 2. snap bias (the recenter constant) ------------------------------
    print("\n== 2. snap bias: snapped - subframe corner (full 60fps rate) ==")
    bias = np.array([s["t_snap"] - s["t_sub"] for s in shots
                     if s["t_sub"] is not None], dtype=float)
    describe("t_snap - t_corner", bias)
    if len(bias) > 4:
        print("    -> kStopSubframeRecenterMs (AutomationEngine.cpp) should be the median: %.1f ms"
              % float(np.median(bias)))

    # ---- 3. the 'animation floor', both endpoints sub-frame ----------------
    print("\n== 3. anchor->stop spread, snapped vs subframe stop (anchor level %.0f) =="
          % args.anchor_level)
    have = [s for s in shots if s["t_anchor"] is not None]
    print("shots with a witnessed %.0f crossing: %d" % (args.anchor_level, len(have)))
    T_snap = np.array([s["t_snap"] - s["t_anchor"] for s in have], dtype=float)
    T_sub = np.array([(s["t_sub"] if s["t_sub"] is not None else s["t_snap"])
                      - s["t_anchor"] for s in have], dtype=float)
    describe("T snapped (all types pooled)", robust(T_snap, 80))
    describe("T subframe (all types pooled)", robust(T_sub, 80))
    # within-shot-type (the 6.04 was measured within-session AND within-type)
    types = sorted({s["shot_type"] for s in have})
    print("\nper shot type (n>=8):")
    pooled_var = {"snap": [], "sub": []}
    for ty in types:
        idx = [i for i, s in enumerate(have) if s["shot_type"] == ty]
        if len(idx) < 8:
            continue
        vs = robust(T_snap[idx], 80)
        vb = robust(T_sub[idx], 80)
        if len(vs) < 8 or len(vb) < 8:
            continue
        rs = 1.4826 * np.median(np.abs(vs - np.median(vs)))
        rb = 1.4826 * np.median(np.abs(vb - np.median(vb)))
        print("  %-14s n=%-3d  snapped sd=%5.2f rSD=%5.2f   subframe sd=%5.2f rSD=%5.2f"
              % (ty, len(vs), vs.std(ddof=1), rs, vb.std(ddof=1), rb))
        pooled_var["snap"].append((len(vs), vs.std(ddof=1)))
        pooled_var["sub"].append((len(vb), vb.std(ddof=1)))
    for key, label in (("snap", "SNAPPED"), ("sub", "SUBFRAME")):
        if pooled_var[key]:
            num = sum((n - 1) * sd * sd for n, sd in pooled_var[key])
            den = sum(n - 1 for n, sd in pooled_var[key])
            print("pooled within-type %s sd = %.2f ms (df %d)"
                  % (label, math.sqrt(num / den), den))
    # variance subtraction: what part of the pooled spread is stop-dating noise?
    if len(d_sub) > 4 and len(d_snap) > 4 and pooled_var["snap"] and pooled_var["sub"]:
        sd60_snap = (np.std(d_snap, ddof=1) / math.sqrt(2.0)) / 2.0
        sd60_sub = (np.std(d_sub, ddof=1) / math.sqrt(2.0)) / 2.0
        for key, sd60, label in (("snap", sd60_snap, "SNAPPED"),
                                 ("sub", sd60_sub, "SUBFRAME")):
            num = sum((n - 1) * sd * sd for n, sd in pooled_var[key])
            den = sum(n - 1 for n, sd in pooled_var[key])
            tot = math.sqrt(num / den)
            surv = math.sqrt(max(tot * tot - sd60 * sd60, 0.0))
            print("%s: pooled within-type %.2f ms - dating noise %.2f ms (quad) "
                  "-> surviving 'floor' %.2f ms" % (label, tot, sd60, surv))
    print("\nNOTE: two grid-dated endpoints yield sqrt(2*g^2/12)=6.8ms BY CONSTRUCTION at "
          "g=16.7ms; any 'floor' at or below the dating noise is instrument, not animation.")


if __name__ == "__main__":
    main()
