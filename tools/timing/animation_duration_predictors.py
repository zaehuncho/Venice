"""Hunt for pre-decision predictors of per-shot anchor->stop duration.

    python tools/timing/animation_duration_predictors.py \
        --log logs/orion_native.log \
        --frames logs/diagnostics/detframes.csv \
        --start 2026-08-06T08:40:00Z --end 2026-08-06T09:05:00Z

Joins the engine's PHASE SAMPLE lines (normalized anchor->stop duration, the
quantity whose tails are the counted lates/earlies) to the per-frame fill record
(detframes.csv) and to the release markers, then:

  1. DECOMPOSES each shot's duration T into
         A = (release_epoch - frame_anchor_crossing) + rung_offset   (engine-scheduled)
         B = (frame_stop_time - release_epoch)                       (release -> observed stop)
     The meter freezes when the game REGISTERS the release (frame dumps show the
     fill freezing at ~release_fill + latency*vel, far below 100), so B is
     input-path latency + game freeze + display/capture return -- NOT shot
     animation.  If var(B) dominates, "animation duration variance" is
     mis-named and no meter-side predictor can exist.
  2. Tests every candidate early-rise predictor (inter-rung wall times,
     curvature, linear-fit wobble, fill at first sight, appear->anchor time,
     release-attribution velocity) against T and against B, with leave-one-out
     cross-validation.  A predictor is only reported as real if its LOOCV rmse
     beats the mean-only LOOCV rmse.
  3. Part-whole check: early segment E (anchor->40) vs remainder R (40->stop).
     r(E,R) > 0  -> proportional scaling would work;
     r(E,R) ~ 0  -> re-basing at the last crossing removes var(E) only;
     r(E,R) < 0  -> early slowness self-corrects; leave the constant alone.
  4. Negative controls: stop-frame gap, last-frame rise rate, settled fill,
     duplicate-frame stats at the stop, alternate stop definitions, lag-1
     autocorrelation (latency drift clusters in time; game RNG should not).

Read-only: touches no engine/reader/settings/learning files.

--- MEASURED 2026-08-06, counted batch 08:46-08:58Z (n=86 accepted, joined 86) ---

DECOMPOSITION: sd(A) = 1.9 ms, B carries 99%% of var(T). The engine's schedule
is essentially deterministic; every observed duration tail lives in B.
Replicated on the 05:45Z fragment (n=10, A sd 2.1, B 101%%).

THE LONG TAILS ARE INSTRUMENT ARTIFACTS. All three >+25 ms samples (446.2,
427.7, 409.4 = seqs 76/3/27) sit 38-67 ms past the frame-observed freeze; in
each case a SINGLE-FRAME fill spike (100.0 / 100.0 / 91.5) lands >2 pp above
the settled plateau and AutomationEngine.cpp:2321 re-dates the stop onto the
spike (tipPhaseStopStepPct = 2.0 re-open has no corroboration requirement).
Seq 83's +1.9 pp spike, under the step, was correctly ignored. All three were
accepted=1 into the phase learner.

THE SHORT TAILS ARE REAL AND PREDICTABLE. Both >-25 ms samples (334.3, 351.6 =
seqs 83/70) show a single-frame +11.2 / +8.4 pp meter JUMP mid-rise (persisting
offset, no dropped/stale frames, freeze genuinely early by both instruments),
visible 140 / 86 ms BEFORE the release. Max-step distribution across 86 shots:
median 3.9 pp, p95 6.5, only these two exceed 8 pp.

CONTINUOUS animation-speed variance is NOT observable: early slope, curvature
and wobble all fail LOOCV; E(20->40) vs remainder r = -0.02 (early segment
carries no information about the rest). d_20_40 r = +0.45 and fillAtRel
r = -0.45 survive LOOCV (14.0 -> 12.75 / 12.72 rmse) but the effect is the
part-whole/jump mechanism, not proportional stretch: rebase-at-last-crossing
(slope 1) sd 13.9 -> 12.6, and it corrects both jump shots.

Artifact-cleaned y: sd 10.3, rSD 5.6; + jump-corrected: sd 9.5, zero shots
beyond +/-27.5 ms of median.
"""

import argparse
import csv
import math
import re
import sys
from datetime import datetime, timezone

import numpy as np

# measured per-rung secant offsets are NOT hardcoded here: the offset for each
# shot is read directly off its own PHASE SAMPLE line as normalized_ms - raw_ms.

RUNG_LEVELS = [15.0, 20.0, 25.0, 30.0, 35.0, 40.0]
EARLY_FILL_MAX = 40.0     # strictly before the release (fillAtRel ~ 42.5-46.4)
EARLY_FILL_MIN = 12.0

RE_PHASE = re.compile(
    r"^(\S+?)Z\s+PHASE SAMPLE: raw_ms=([\d.]+) normalized_ms=([\d.]+) "
    r"anchor_pct=([\d.]+) accepted=(\d).*?shot_type=(.+)$")
RE_ISSUED = re.compile(
    r"^(\S+?)Z\s+Release issued: fill ([\d.]+)%.*? age=(\d+)ms.*? seq=(\d+) shot=(.+)$")
RE_ATTR = re.compile(
    r"Release attribution: seq=(\d+) .*?vel=([\d.]+) crossingEta=(-?\d+)")
RE_TIMING = re.compile(
    r"Release timing: seq=(\d+) .*?anchorAppearMs=(-?\d+) appearToRelMs=(-?\d+) "
    r"holdToRelMs=(-?\d+).*?fillAtRel=([\d.]+) peakFill=([\d.]+)")
RE_MARKER = re.compile(
    r"release marker: seq=(\d+) wall_ms=(\d+).*?measured_latency_ms=([\d.]+)")


def iso_ms(s):
    s = s.replace("Z", "")
    dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


def parse_log(path, t0, t1):
    phases, issued, attr, timing, marker = [], {}, {}, {}, {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "2026-" not in line[:8]:
                # sidecar-relayed lines still start with the native timestamp
                pass
            m = RE_PHASE.match(line)
            if m:
                ts = iso_ms(m.group(1))
                if t0 <= ts <= t1:
                    phases.append(dict(
                        ts=ts, raw=float(m.group(2)), norm=float(m.group(3)),
                        anchor_pct=float(m.group(4)), accepted=int(m.group(5)),
                        shot_type=m.group(6).strip()))
                continue
            m = RE_ISSUED.match(line)
            if m:
                ts = iso_ms(m.group(1))
                if t0 <= ts <= t1:
                    issued[int(m.group(4))] = dict(
                        ts=ts, fill=float(m.group(2)), age=float(m.group(3)),
                        shot=m.group(5).strip())
                continue
            m = RE_ATTR.search(line)
            if m and "Release attribution" in line:
                ts_head = line.split()[0]
                try:
                    ts = iso_ms(ts_head[:-1] if ts_head.endswith("Z") else ts_head)
                except ValueError:
                    continue
                if t0 <= ts <= t1:
                    attr[int(m.group(1))] = dict(
                        vel=float(m.group(2)), eta=float(m.group(3)))
                continue
            m = RE_TIMING.search(line)
            if m:
                ts_head = line.split()[0]
                try:
                    ts = iso_ms(ts_head[:-1] if ts_head.endswith("Z") else ts_head)
                except ValueError:
                    continue
                if t0 <= ts <= t1:
                    timing[int(m.group(1))] = dict(
                        anchorAppear=float(m.group(2)),
                        appearToRel=float(m.group(3)),
                        holdToRel=float(m.group(4)),
                        fillAtRel=float(m.group(5)),
                        peakFill=float(m.group(6)))
                continue
            m = RE_MARKER.search(line)
            if m:
                w = float(m.group(2))
                if t0 - 10000 <= w <= t1 + 10000:
                    marker[int(m.group(1))] = dict(
                        wall=w, mlat=float(m.group(3)))
    return phases, issued, attr, timing, marker


def load_frames(paths, t0, t1):
    rows = []
    for p in paths:
        with open(p, newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    w = float(r.get("wall_ms") or 0.0)
                except (TypeError, ValueError):
                    continue
                if not (t0 - 5000 <= w <= t1 + 5000):
                    continue
                try:
                    det = int(r.get("detected", 0) or 0)
                    fed = int(r.get("fed", 0) or 0)
                    f = float(r.get("fill_pct") or 0.0)
                except (TypeError, ValueError):
                    continue
                if det != 1 or fed != 1 or not (0.0 <= f <= 100.0):
                    continue
                rows.append(dict(
                    w=w, f=f,
                    conf=float(r.get("confidence") or 0.0),
                    dup=float(r.get("dup_pct") or 0.0),
                    uniqfps=float(r.get("uniqfps") or 0.0),
                    stale=int(r.get("stale") or 0)))
    rows.sort(key=lambda x: x["w"])
    return rows


def episodes(rows, max_gap=200.0):
    eps, cur = [], []
    for r in rows:
        if cur and (r["w"] - cur[-1]["w"]) > max_gap:
            eps.append(cur)
            cur = []
        cur.append(r)
    if cur:
        eps.append(cur)
    out = []
    for ep in eps:
        fills = [r["f"] for r in ep]
        if len(ep) < 6:
            continue
        if max(fills) - min(fills) < 25.0 or max(fills) < 55.0:
            continue
        out.append(ep)
    return out


def crossing(ep, level, t_max=None, t_min=None):
    """LAST upward interpolated crossing of `level` in (t_min, t_max] (guards
    against spliced episodes containing a previous shot's rise)."""
    best = None
    for a, b in zip(ep, ep[1:]):
        if t_max is not None and a["w"] > t_max:
            break
        if a["f"] < level <= b["f"] and b["f"] > a["f"]:
            frac = (level - a["f"]) / (b["f"] - a["f"])
            t = a["w"] + frac * (b["w"] - a["w"])
            if (t_max is None or t <= t_max) and (t_min is None or t >= t_min):
                best = t
    return best


def stop_features(ep, t_rel, settle_drop=1.0, settle_win=1200.0):
    """Time the meter freeze AFTER the release. settled = median fill over
    (t_rel, t_rel + settle_win]; stop = first interpolated upward crossing of
    settled - settle_drop at or after t_rel - 50."""
    post = [r for r in ep if t_rel < r["w"] <= t_rel + settle_win]
    if len(post) < 4:
        return None
    fills = np.array([r["f"] for r in post])
    peak = float(fills.max())
    plateau = fills[fills >= peak - 3.0]
    settled = float(np.median(plateau))
    thr = settled - settle_drop
    for i, (a, b) in enumerate(zip(ep, ep[1:])):
        if b["w"] < t_rel - 50:
            continue
        if a["f"] < thr <= b["f"] and b["f"] > a["f"]:
            frac = (thr - a["f"]) / (b["f"] - a["f"])
            t_stop = a["w"] + frac * (b["w"] - a["w"])
            gap = b["w"] - a["w"]
            rate = (b["f"] - a["f"]) / max(gap, 1e-9)
            return dict(t_stop=t_stop, stop_gap=gap, last_rate=rate,
                        settled=settled, peak=peak,
                        dup=b["dup"], uniqfps=b["uniqfps"],
                        stale_near=sum(r["stale"] for r in ep[max(0, i - 2):i + 3]))
    return None


def early_features(ep, t_rel):
    """Only frames strictly before the release and fill <= EARLY_FILL_MAX."""
    pts = [(r["w"], r["f"]) for r in ep
           if r["w"] < t_rel and EARLY_FILL_MIN <= r["f"] <= EARLY_FILL_MAX]
    out = dict(fill_first=ep[0]["f"], t_first=ep[0]["w"],
               conf_early=np.mean([r["conf"] for r in ep[:6]]))
    for L in RUNG_LEVELS:
        # the anchor->release schedule is ~151ms, so every legitimate rung
        # crossing sits within ~700ms of the release; anything further is a
        # spliced episode's previous shot
        out["c%d" % int(L)] = crossing(ep, L, t_max=t_rel, t_min=t_rel - 700.0)
    if len(pts) >= 4:
        t = np.array([p[0] for p in pts])
        f = np.array([p[1] for p in pts])
        t0 = t - t[0]
        lin = np.polyfit(t0, f, 1)
        resid = f - np.polyval(lin, t0)
        out["slope_ppms"] = float(lin[0])
        out["wobble_pp"] = float(np.sqrt(np.mean(resid ** 2)))
        if len(pts) >= 5:
            quad = np.polyfit(t0, f, 2)
            out["curv_ppms2"] = float(2.0 * quad[0])
        else:
            out["curv_ppms2"] = None
    else:
        out["slope_ppms"] = out["wobble_pp"] = out["curv_ppms2"] = None
    return out


def fisher_ci(r, n, z=1.96):
    if n < 4 or abs(r) >= 1.0:
        return (float("nan"), float("nan"))
    zr = 0.5 * math.log((1 + r) / (1 - r))
    se = 1.0 / math.sqrt(n - 3)
    lo, hi = zr - z * se, zr + z * se
    return (math.tanh(lo), math.tanh(hi))


def loocv_rmse(X, y):
    """LOOCV rmse of OLS y ~ [1, X]. X: (n,k) or None for mean-only."""
    n = len(y)
    errs = []
    for i in range(n):
        idx = np.arange(n) != i
        if X is None:
            pred = y[idx].mean()
        else:
            A = np.column_stack([np.ones(idx.sum()), X[idx]])
            coef, *_ = np.linalg.lstsq(A, y[idx], rcond=None)
            pred = float(np.dot(np.concatenate([[1.0], np.atleast_1d(X[i])]), coef))
        errs.append(y[i] - pred)
    return float(np.sqrt(np.mean(np.array(errs) ** 2)))


def sd_ci(sd, n, z_lo=0.975, z_hi=0.025):
    from scipy import stats  # optional; fall back if unavailable
    lo = sd * math.sqrt((n - 1) / stats.chi2.ppf(z_lo, n - 1))
    hi = sd * math.sqrt((n - 1) / stats.chi2.ppf(z_hi, n - 1))
    return lo, hi


def describe(name, v):
    v = np.asarray(v, dtype=float)
    v = v[~np.isnan(v)]
    if len(v) == 0:
        print("%-28s (none)" % name)
        return
    rsd = 1.4826 * np.median(np.abs(v - np.median(v)))
    print("%-28s n=%-3d med=%8.1f mean=%8.1f sd=%6.2f rSD=%6.2f min=%7.1f max=%7.1f"
          % (name, len(v), np.median(v), v.mean(), v.std(ddof=1), rsd, v.min(), v.max()))


def corr_row(name, x, y, note=""):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    ok = ~(np.isnan(x) | np.isnan(y))
    x, y = x[ok], y[ok]
    n = len(x)
    if n < 8 or x.std() == 0:
        print("%-26s n=%-3d (insufficient)" % (name, n))
        return None
    r = float(np.corrcoef(x, y)[0, 1])
    lo, hi = fisher_ci(r, n)
    # spearman
    rs = float(np.corrcoef(np.argsort(np.argsort(x)), np.argsort(np.argsort(y)))[0, 1])
    slope = r * y.std(ddof=1) / x.std(ddof=1)
    resid_sd = float(np.sqrt(max(y.var(ddof=1) * (1 - r * r), 0.0)))
    cv = loocv_rmse(x.reshape(-1, 1), y)
    cv0 = loocv_rmse(None, y)
    print("%-26s n=%-3d r=%+5.2f [%+5.2f,%+5.2f] rho=%+5.2f slope=%+8.3f "
          "residSD=%6.2f  cvRMSE=%6.2f (mean-only %6.2f, %s%.2f) %s"
          % (name, n, r, lo, hi, rs, slope, resid_sd, cv, cv0,
             "-" if cv <= cv0 else "+", abs(cv0 - cv), note))
    return dict(name=name, n=n, r=r, cv=cv, cv0=cv0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="logs/orion_native.log")
    ap.add_argument("--frames", nargs="+", default=["logs/diagnostics/detframes.csv"])
    ap.add_argument("--start", default="2026-08-06T08:40:00Z")
    ap.add_argument("--end", default="2026-08-06T09:05:00Z")
    ap.add_argument("--label", default="primary")
    ap.add_argument("--dump-csv", default=None,
                    help="write the per-shot joined table here")
    args = ap.parse_args()

    t0, t1 = iso_ms(args.start), iso_ms(args.end)
    phases, issued, attr, timing, marker = parse_log(args.log, t0, t1)
    frames = load_frames(args.frames, t0, t1)
    eps = episodes(frames)
    print("== pool '%s'  %s .. %s ==" % (args.label, args.start, args.end))
    print("phase samples: %d (accepted %d)  releases: %d  markers: %d  "
          "frame rows: %d  episodes: %d"
          % (len(phases), sum(p["accepted"] for p in phases), len(issued),
             len(marker), len(frames), len(eps)))

    # -- join ---------------------------------------------------------------
    shots = []
    for ph in phases:
        # closest preceding Release issued within 5s
        best = None
        for seq, ri in issued.items():
            if ri["ts"] <= ph["ts"] and ph["ts"] - ri["ts"] < 5000:
                if best is None or ri["ts"] > issued[best]["ts"]:
                    best = seq
        if best is None or best not in marker:
            continue
        t_rel = marker[best]["wall"]
        ep = None
        for e in eps:
            if e[0]["w"] - 150 <= t_rel <= e[-1]["w"] + 150:
                ep = e
                break
        if ep is None:
            continue
        sf = stop_features(ep, t_rel)
        if sf is None:
            continue
        ef = early_features(ep, t_rel)
        rise = [r for r in ep if r["w"] < t_rel and r["f"] >= EARLY_FILL_MIN]
        if len(rise) >= 2:
            gaps = [b["w"] - a["w"] for a, b in zip(rise, rise[1:])]
            ef["max_gap_rise"] = max(gaps)
            ef["stale_rise"] = sum(r["stale"] for r in rise)
        else:
            ef["max_gap_rise"] = float("nan")
            ef["stale_rise"] = 0
        rung = ph["anchor_pct"]
        c_rung = ef.get("c%d" % int(rung))
        off = ph["norm"] - ph["raw"]
        s = dict(ph=ph, seq=best, t_rel=t_rel, ep=ep, off=off,
                 mlat=marker[best]["mlat"], **sf, **ef)
        s.update(issued[best])
        s.update(attr.get(best, {}))
        s.update(timing.get(best, {}))
        s["c_rung"] = c_rung
        if c_rung is not None:
            s["A_norm"] = (t_rel - c_rung) + off          # engine-scheduled part
            s["T_frame"] = (s["t_stop"] - c_rung) + off   # frame-derived total
        else:
            s["A_norm"] = s["T_frame"] = float("nan")
        s["B"] = s["t_stop"] - t_rel                      # release -> observed stop
        shots.append(s)

    acc = [s for s in shots if s["ph"]["accepted"] == 1]
    print("joined shots: %d  (accepted: %d)" % (len(shots), len(acc)))
    if len(acc) < 10:
        print("too few joined shots; aborting")
        return

    y = np.array([s["ph"]["norm"] for s in acc])
    A = np.array([s.get("A_norm", float("nan")) for s in acc])
    B = np.array([s["B"] for s in acc])
    Tf = np.array([s.get("T_frame", float("nan")) for s in acc])

    print("\n-- 0. targets ---------------------------------------------------")
    describe("engine normalized_ms (y)", y)
    describe("frame-derived T (norm)", Tf)
    describe("A = anchor->release (norm)", A)
    describe("B = release->stop", B)

    print("\n-- 1. instrument agreement -------------------------------------")
    corr_row("frameT vs engine y", Tf, y, "(engine stop detector vs csv frames)")

    print("\n-- 2. THE DECOMPOSITION  y ~ A + B ------------------------------")
    ok = ~np.isnan(A)
    va, vb = A[ok].var(ddof=1), B[ok].var(ddof=1)
    cab = float(np.cov(A[ok], B[ok])[0, 1])
    vt = (A[ok] + B[ok]).var(ddof=1)
    print("var(A)=%6.1f  var(B)=%6.1f  2cov(A,B)=%+7.1f  var(A+B)=%6.1f" %
          (va, vb, 2 * cab, vt))
    print("share of var(T): A %.0f%%  B %.0f%%  cov %+.0f%%" %
          (100 * va / vt, 100 * vb / vt, 100 * 2 * cab / vt))
    corr_row("A vs B", A, B)
    corr_row("A vs y", A, y)
    corr_row("B vs y", B, y)

    print("\n-- 3. candidate predictors vs y (pre-decision only) -------------")
    d = {}
    for a, b in [(20, 25), (25, 30), (30, 35), (35, 40), (20, 30), (20, 40), (25, 40)]:
        d["d_%d_%d" % (a, b)] = np.array(
            [ (s["c%d" % b] - s["c%d" % a])
              if (s.get("c%d" % a) is not None and s.get("c%d" % b) is not None)
              else float("nan") for s in acc])
    feats = dict(d)
    feats["slope_ms_per_pp"] = np.array(
        [1.0 / s["slope_ppms"] if s.get("slope_ppms") else float("nan") for s in acc])
    feats["curv_ppms2"] = np.array(
        [s["curv_ppms2"] if s.get("curv_ppms2") is not None else float("nan") for s in acc])
    feats["wobble_pp"] = np.array(
        [s["wobble_pp"] if s.get("wobble_pp") is not None else float("nan") for s in acc])
    feats["fill_first"] = np.array([s["fill_first"] for s in acc])
    feats["appear_to_c20"] = np.array(
        [(s["c20"] - s["t_first"]) if s.get("c20") is not None else float("nan") for s in acc])
    feats["vel_attr"] = np.array([s.get("vel", float("nan")) for s in acc])
    feats["fillAtRel"] = np.array([s.get("fillAtRel", float("nan")) for s in acc])
    feats["anchorAppearMs"] = np.array([s.get("anchorAppear", float("nan")) for s in acc])
    feats["holdToRelMs"] = np.array([s.get("holdToRel", float("nan")) for s in acc])
    feats["frame_age_at_rel"] = np.array([s.get("age", float("nan")) for s in acc])
    results = []
    for k, v in feats.items():
        res = corr_row(k, v, y)
        if res:
            results.append(res)

    print("\n-- 3b. the same candidates vs B (latency leg) -------------------")
    for k in ["d_20_40", "slope_ms_per_pp", "vel_attr", "fillAtRel",
              "frame_age_at_rel", "wobble_pp"]:
        corr_row(k + " ~B", feats[k], B)

    print("\n-- 4. part-whole: early segment vs remainder --------------------")
    E = d["d_20_40"]
    R = np.array([(s["t_stop"] - s["c40"]) if s.get("c40") is not None
                  else float("nan") for s in acc])
    describe("E = 20->40 wall time", E)
    describe("R = 40->stop remainder", R)
    corr_row("E vs R", E, R, "(>0 scale, ~0 rebase, <0 leave alone)")
    corr_row("E vs T", E, y)
    R30 = np.array([(s["t_stop"] - s["c30"]) if s.get("c30") is not None
                    else float("nan") for s in acc])
    describe("R30 = 30->stop remainder", R30)

    print("\n-- 5. multivariate (LOOCV) --------------------------------------")
    combos = {
        "d2030+d3040": ["d_20_30", "d_30_35"],
        "slope+curv+wobble": ["slope_ms_per_pp", "curv_ppms2", "wobble_pp"],
        "d2040+vel+fillAtRel": ["d_20_40", "vel_attr", "fillAtRel"],
        "kitchen sink": ["d_20_30", "d_30_35", "curv_ppms2", "wobble_pp",
                          "fill_first", "vel_attr", "fillAtRel"],
    }
    for name, keys in combos.items():
        cols = [feats[k] for k in keys]
        M = np.column_stack(cols)
        ok = ~np.isnan(M).any(axis=1) & ~np.isnan(y)
        if ok.sum() < 15:
            print("%-24s n=%d (insufficient)" % (name, ok.sum()))
            continue
        cv = loocv_rmse(M[ok], y[ok])
        cv0 = loocv_rmse(None, y[ok])
        print("%-24s n=%-3d cvRMSE=%6.2f  vs mean-only %6.2f  (%s%.2f)"
              % (name, ok.sum(), cv, cv0, "-" if cv <= cv0 else "+", abs(cv0 - cv)))

    print("\n-- 6. negative controls / measurement artifacts ----------------")
    for k, v in [
        ("stop_gap (frame gap @stop)", np.array([s["stop_gap"] for s in acc])),
        ("last_rate (pp/ms @stop)", np.array([s["last_rate"] for s in acc])),
        ("settled fill", np.array([s["settled"] for s in acc])),
        ("peak fill", np.array([s["peak"] for s in acc])),
        ("dup_pct @stop", np.array([s["dup"] for s in acc])),
        ("uniqfps @stop", np.array([s["uniqfps"] for s in acc])),
        ("marker measured_latency", np.array([s["mlat"] for s in acc])),
    ]:
        corr_row(k, v, y)
    # alternate stop definitions
    for drop in (0.5, 2.0):
        alt = []
        for s in acc:
            sf = stop_features(s["ep"], s["t_rel"], settle_drop=drop)
            alt.append((sf["t_stop"] - s["t_rel"]) if sf else float("nan"))
        alt = np.array(alt)
        okd = ~np.isnan(alt)
        print("stop def settled-%.1f: sd(B_alt)=%.2f corr with B=%.3f"
              % (drop, alt[okd].std(ddof=1),
                 float(np.corrcoef(alt[okd], B[okd])[0, 1])))
    # engine-vs-frame disagreement: big |y - T_frame| on the tails would mean
    # the engine's own stop detector, not the meter, generated the tail
    disc = y - Tf
    describe("y - T_frame (instrument gap)", disc)
    corr_row("|y-Tframe| vs |y-med|", np.abs(disc), np.abs(y - np.median(y)))
    # temporal clustering
    for name, v in [("y", y), ("B", B), ("A", A)]:
        vv = v[~np.isnan(v)]
        if len(vv) > 10:
            l1 = float(np.corrcoef(vv[:-1], vv[1:])[0, 1])
            print("lag-1 autocorrelation of %-2s: %+.3f  (n=%d)" % (name, l1, len(vv)))
    tsec = np.array([s["ph"]["ts"] for s in acc])
    corr_row("session time vs y", (tsec - tsec[0]) / 1000.0, y)
    corr_row("session time vs B", (tsec - tsec[0]) / 1000.0, B)

    print("\n-- 7. per-type (Standstill only) --------------------------------")
    st = [i for i, s in enumerate(acc) if s["ph"]["shot_type"] == "Standstill"]
    if len(st) >= 15:
        ys = y[st]
        describe("y Standstill", ys)
        for k in ["d_20_40", "slope_ms_per_pp", "curv_ppms2", "vel_attr"]:
            corr_row(k + " (Standstill)", feats[k][st], ys)
        Ast, Bst = A[st], B[st]
        oks = ~np.isnan(Ast)
        vts = (Ast[oks] + Bst[oks]).var(ddof=1)
        print("Standstill var shares: A %.0f%%  B %.0f%%" %
              (100 * Ast[oks].var(ddof=1) / vts, 100 * Bst[oks].var(ddof=1) / vts))

    print("\n-- 8. the tails -------------------------------------------------")
    med = np.median(y)
    hdr = ("%-4s %-11s %6s %6s %6s %6s %6s | %6s %6s %6s %6s %5s %5s %6s"
           % ("seq", "type", "y", "Tfrm", "A", "B", "y-med", "d2040", "slope",
              "vel", "filRel", "mxgap", "stale", "settle"))
    print(hdr)
    order = np.argsort(np.abs(y - med))[::-1]
    for i in order[:12]:
        s = acc[i]
        print("%-4s %-11s %6.1f %6.1f %6.1f %6.1f %+6.1f | %6.1f %6.2f %6.3f %6.1f %5.0f %5d %6.1f"
              % (s["seq"], s["ph"]["shot_type"][:11], y[i],
                 Tf[i] if not np.isnan(Tf[i]) else -1,
                 A[i] if not np.isnan(A[i]) else -1,
                 B[i], y[i] - med,
                 feats["d_20_40"][i] if not np.isnan(feats["d_20_40"][i]) else -1,
                 feats["slope_ms_per_pp"][i] if not np.isnan(feats["slope_ms_per_pp"][i]) else -1,
                 feats["vel_attr"][i] if not np.isnan(feats["vel_attr"][i]) else -1,
                 feats["fillAtRel"][i] if not np.isnan(feats["fillAtRel"][i]) else -1,
                 s.get("max_gap_rise", float("nan")), s.get("stale_rise", 0),
                 s["settled"]))

    if args.dump_csv:
        keys = ["seq", "t_rel", "A_norm", "B", "T_frame", "off", "stop_gap",
                "last_rate", "settled", "peak"]
        with open(args.dump_csv, "w", newline="") as fh:
            wcsv = csv.writer(fh)
            wcsv.writerow(["norm", "raw", "anchor_pct", "shot_type"] + keys +
                          list(feats.keys()))
            for i, s in enumerate(acc):
                wcsv.writerow([s["ph"]["norm"], s["ph"]["raw"],
                               s["ph"]["anchor_pct"], s["ph"]["shot_type"]] +
                              [s.get(k, "") for k in keys] +
                              [feats[k][i] for k in feats])
        print("\nper-shot table -> %s" % args.dump_csv)


if __name__ == "__main__":
    sys.exit(main())
