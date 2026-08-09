"""Q1/Q2 decision analysis: split the ~10ms residual into measurement vs physics,
and characterise the green window (position of aim, window dynamics, instrument
trustworthiness).  ANALYSIS ONLY -- reads logs + detframes CSVs, writes nothing
outside --dump-dir.

    python tools/timing/residual_split.py --pool batch
    python tools/timing/residual_split.py --pool late
    python tools/timing/residual_split.py --windows          # all-session green census

Builds on tools/timing/animation_duration_predictors.py (join machinery) and its
2026-08-06 decomposition: T = A (anchor->release, sd 1.9ms) + B (release->observed
freeze, 99%% of variance).

Key methods:
  * split-half stop dating: date the freeze on even-only vs odd-only capture
    frames; the disagreement measures frame-sampling + interpolation noise
    INCLUDING the component shared by engine and CSV instruments (which plain
    instrument disagreement misses because both read the same frames).
  * engine-vs-CSV stop disagreement: bounds algorithmic detector noise.
  * grid identity check (REPLACES the former "release-phase sawtooth"): the
    former analysis regressed B on (functions of) phi = t_rel - prev_frame_wall
    and read the fitted -1-slope sawtooth as a discovered 60Hz quantizer. That
    was ALGEBRA, not physics: t_stop is dated ON a capture frame, so
    B = t_stop - t_rel = k*g - phi identically -- phi appears in B with
    coefficient exactly -1 BY CONSTRUCTION and the regression can only ever
    recover the identity (same error class as the refuted travel_pp and
    (peak-fillAtRel)/vel instruments). What remains below is the honest use of
    the same arithmetic: verifying HOW exactly the stop dating is grid-locked
    (an instrument property), never inferring console behaviour from it.
  * banner triangle: counted green rate + reported window width + tip velocity
    jointly bound the landing-relevant sigma; at most two of the three measured
    values can hold simultaneously.
"""

import argparse
import math
import os
import re
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import animation_duration_predictors as adp  # noqa: E402

LOGS = ["logs/orion_native.log.1", "logs/orion_native.log"]
DIAG = "logs/diagnostics"

POOLS = {
    # name: (start, end, [frame csvs])
    "b30": ("2026-08-05T05:15:00Z", "2026-08-05T06:05:00Z",
            ["detframes_20260805_002307.csv", "detframes_20260805_002830.csv",
             "detframes_20260805_003854.csv", "detframes_20260805_004503.csv",
             "detframes_20260805_005640.csv"]),
    "frag": ("2026-08-06T05:40:00Z", "2026-08-06T05:50:00Z",
             ["detframes_20260806_004655.csv"]),
    "scatter": ("2026-08-06T06:25:00Z", "2026-08-06T07:20:00Z",
                ["detframes_20260806_021316.csv"]),
    "batch": ("2026-08-06T08:40:00Z", "2026-08-06T09:05:00Z",
              ["detframes_20260806_043752.csv"]),
    "late": ("2026-08-06T09:05:00Z", "2026-08-06T10:30:00Z",
             ["detframes_20260806_043752.csv", "detframes_20260806_045915.csv",
              "detframes_20260806_051100.csv", "detframes.csv"]),
    # the hand-counted 42/50 batch (base-30). No detframes on disk for this
    # window -- landing/issued lines only.
    "counted": ("2026-08-05T11:00:00Z", "2026-08-05T12:00:00Z", []),
    "late1": ("2026-08-06T09:50:00Z", "2026-08-06T10:00:00Z",
              ["detframes_20260806_045915.csv"]),
    "late2": ("2026-08-06T10:03:00Z", "2026-08-06T10:30:00Z",
              ["detframes_20260806_051100.csv", "detframes.csv"]),
}

RE_ISSUED_GREEN = re.compile(
    r"^(\S+?)Z\s+Release issued: fill ([\d.]+)% target ([\d.]+)% "
    r"\(green ([\d.]+)-([\d.]+)\).*? age=(\d+)ms.*? seq=(\d+) shot=(.+)$")
RE_LANDING = re.compile(
    r"^(\S+?)Z\s+Release landing: seq=(\d+) graded=(\d) peak_fill=([\d.]+) "
    r"settled_fill=([\d.]+) fill_at_rel=([\d.]+) travel_pp=([\d.]+) "
    r"green_start=([\d.]+) green_end=([\d.]+) green_center=([\d.]+) "
    r"settled_n=(\d+) green_obs_n=(\d+) green_obs_start=([\d.]+) "
    r"green_obs_width=([\d.]+).*?shot=(.+)$")
RE_TIPMEAS = re.compile(
    r"^(\S+?)Z\s+Tip phase measurement: physical_median_ms=([\d.]+) n=(\d+) "
    r"sample_ms=([\d.]+)")


def iso_ms(s):
    return adp.iso_ms(s.replace("Z", ""))


def load_frames_green(paths, t0, t1):
    """Like adp.load_frames but keeps green_center/green_confidence/mtr_phase."""
    import csv
    rows = []
    for p in paths:
        fp = os.path.join(DIAG, p)
        if not os.path.exists(fp):
            print("  (missing %s)" % fp)
            continue
        with open(fp, newline="") as fh:
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
                try:
                    gc = float(r.get("green_center_pct") or -1.0)
                    gcf = float(r.get("green_confidence") or 0.0)
                except (TypeError, ValueError):
                    gc, gcf = -1.0, 0.0
                rows.append(dict(
                    w=w, f=f,
                    conf=float(r.get("confidence") or 0.0),
                    dup=float(r.get("dup_pct") or 0.0),
                    uniqfps=float(r.get("uniqfps") or 0.0),
                    stale=int(r.get("stale") or 0),
                    gc=gc, gcf=gcf,
                    phase=r.get("mtr_phase") or ""))
    rows.sort(key=lambda x: x["w"])
    return rows


def parse_extra(logs, t0, t1):
    issued_green, landing, tipmeas = {}, {}, []
    for path in logs:
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = RE_ISSUED_GREEN.match(line)
                if m:
                    ts = iso_ms(m.group(1))
                    if t0 <= ts <= t1:
                        issued_green[int(m.group(7))] = dict(
                            ts=ts, fill=float(m.group(2)), target=float(m.group(3)),
                            g0=float(m.group(4)), g1=float(m.group(5)),
                            age=float(m.group(6)), shot=m.group(8).strip())
                    continue
                m = RE_LANDING.match(line)
                if m:
                    ts = iso_ms(m.group(1))
                    if t0 <= ts <= t1:
                        landing[int(m.group(2))] = dict(
                            ts=ts, graded=int(m.group(3)), peak=float(m.group(4)),
                            settled=float(m.group(5)), fillAtRel=float(m.group(6)),
                            travel=float(m.group(7)), gs=float(m.group(8)),
                            ge=float(m.group(9)), gcen=float(m.group(10)),
                            settled_n=int(m.group(11)), gobs_n=int(m.group(12)),
                            gobs_start=float(m.group(13)), gobs_w=float(m.group(14)),
                            shot=m.group(15).strip())
                    continue
                m = RE_TIPMEAS.match(line)
                if m:
                    ts = iso_ms(m.group(1))
                    if t0 <= ts <= t1:
                        tipmeas.append(dict(ts=ts, med=float(m.group(2)),
                                            n=int(m.group(3)), samp=float(m.group(4))))
    return issued_green, landing, tipmeas


def split_half_stop(ep, t_rel):
    """Date the stop on even-only vs odd-only frames; return the pair."""
    ev = [r for i, r in enumerate(ep) if i % 2 == 0]
    od = [r for i, r in enumerate(ep) if i % 2 == 1]
    a = adp.stop_features(ev, t_rel)
    b = adp.stop_features(od, t_rel)
    if a is None or b is None:
        return None
    return a["t_stop"], b["t_stop"]


def split_half_crossing(ep, level, t_rel):
    ev = [r for i, r in enumerate(ep) if i % 2 == 0]
    od = [r for i, r in enumerate(ep) if i % 2 == 1]
    a = adp.crossing(ev, level, t_max=t_rel, t_min=t_rel - 700.0)
    b = adp.crossing(od, level, t_max=t_rel, t_min=t_rel - 700.0)
    if a is None or b is None:
        return None
    return a, b


def sd_ci(sd, n):
    try:
        from scipy import stats
        lo = sd * math.sqrt((n - 1) / stats.chi2.ppf(0.975, n - 1))
        hi = sd * math.sqrt((n - 1) / stats.chi2.ppf(0.025, n - 1))
        return lo, hi
    except Exception:
        # normal approx: se(sd) ~ sd/sqrt(2(n-1))
        se = sd / math.sqrt(2.0 * max(n - 1, 1))
        return sd - 1.96 * se, sd + 1.96 * se


def describe(name, v, unit="ms"):
    v = np.asarray(v, dtype=float)
    v = v[~np.isnan(v)]
    if len(v) == 0:
        print("%-34s (none)" % name)
        return None
    rsd = 1.4826 * np.median(np.abs(v - np.median(v)))
    sd = v.std(ddof=1) if len(v) > 1 else float("nan")
    lo, hi = sd_ci(sd, len(v)) if len(v) > 3 else (float("nan"), float("nan"))
    print("%-34s n=%-3d med=%8.2f mean=%8.2f sd=%6.2f [%5.2f,%5.2f] rSD=%6.2f "
          "min=%7.1f max=%7.1f %s"
          % (name, len(v), np.median(v), v.mean(), sd, lo, hi, rsd,
             v.min(), v.max(), unit))
    return sd


def join_pool(pool):
    start, end, frame_files = POOLS[pool]
    t0, t1 = iso_ms(start), iso_ms(end)
    phases, issued, attr, timing, marker = {}, {}, {}, {}, {}
    ph_all = []
    for lg in LOGS:
        if not os.path.exists(lg):
            continue
        p, i, a, t, mk = adp.parse_log(lg, t0, t1)
        ph_all.extend(p)
        issued.update(i)
        attr.update(a)
        timing.update(t)
        marker.update(mk)
    # de-dup phases across rotated logs by timestamp
    seen, phases = set(), []
    for p in sorted(ph_all, key=lambda x: x["ts"]):
        k = (round(p["ts"]), p["raw"])
        if k in seen:
            continue
        seen.add(k)
        phases.append(p)
    ig, landing, tipmeas = {}, {}, []
    for lg in LOGS:
        g, l, tm = parse_extra([lg], t0, t1)
        ig.update(g)
        landing.update(l)
        tipmeas.extend(tm)
    frames = load_frames_green(frame_files, t0, t1)
    eps = adp.episodes(frames)
    print("== pool '%s' %s .. %s ==" % (pool, start, end))
    print("phase: %d (acc %d)  issued: %d  markers: %d  landings: %d  "
          "frames: %d  episodes: %d"
          % (len(phases), sum(p["accepted"] for p in phases), len(issued),
             len(marker), len(landing), len(frames), len(eps)))

    shots = []
    for ph in phases:
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
        s = dict(ph=ph, seq=best, t_rel=t_rel, ep=ep,
                 mlat=marker[best]["mlat"])
        s.update(issued[best])
        s.update(attr.get(best, {}))
        s.update(timing.get(best, {}))
        if best in ig:
            s["g0_rel"] = ig[best]["g0"]
            s["g1_rel"] = ig[best]["g1"]
        if best in landing:
            L = landing[best]
            s["land"] = L
        if ep is not None:
            sf = adp.stop_features(ep, t_rel)
            if sf is not None:
                s.update(sf)
                ef = adp.early_features(ep, t_rel)
                s.update(ef)
                rung = ph["anchor_pct"]
                c_rung = ef.get("c%d" % int(rung))
                off = ph["norm"] - ph["raw"]
                s["off"] = off
                s["c_rung"] = c_rung
                if c_rung is not None:
                    s["A_norm"] = (t_rel - c_rung) + off
                    s["T_frame"] = (s["t_stop"] - c_rung) + off
                s["B"] = s["t_stop"] - t_rel
                # split-half dating
                sh = split_half_stop(ep, t_rel)
                if sh:
                    s["stop_ev"], s["stop_od"] = sh
                shc = split_half_crossing(ep, rung, t_rel)
                if shc:
                    s["cr_ev"], s["cr_od"] = shc
                # max single-frame step during rise (jump detector)
                rise = [r for r in ep if r["w"] < t_rel and r["f"] >= 12.0]
                if len(rise) >= 3:
                    steps = [b["f"] - a["f"] for a, b in zip(rise, rise[1:])]
                    s["max_step"] = max(steps) if steps else float("nan")
                # velocity near the tip: fill 70 -> min(settled-3, 88)
                hi_zone = [r for r in ep
                           if r["f"] >= 70.0 and r["f"] <= min(s["settled"] - 3.0, 92.0)
                           and r["w"] <= s["t_stop"]]
                if len(hi_zone) >= 4:
                    tt = np.array([r["w"] for r in hi_zone])
                    ff = np.array([r["f"] for r in hi_zone])
                    s["v_tip"] = float(np.polyfit(tt - tt[0], ff, 1)[0])
                # release phase within the capture frame grid
                prev = [r for r in ep if r["w"] <= t_rel]
                nxt = [r for r in ep if r["w"] > t_rel]
                if prev and nxt:
                    s["phi"] = t_rel - prev[-1]["w"]
                    s["grid_gap"] = nxt[0]["w"] - prev[-1]["w"]
                # green window trajectory within the shot
                gvis = [(r["w"], r["gc"]) for r in ep if r["gcf"] > 0 and r["gc"] > 0]
                if gvis:
                    dec = [g for (w, g) in gvis if w <= t_rel]
                    lateg = [g for (w, g) in gvis
                             if s["t_stop"] - 300.0 <= w <= s["t_stop"] + 300.0]
                    if dec:
                        s["gstart_dec"] = 2.0 * float(np.median(dec)) - 100.0
                    if lateg:
                        s["gstart_land"] = 2.0 * float(np.median(lateg)) - 100.0
                    s["g_frames_pre"] = len(dec)
                    s["g_frames_late"] = len(lateg)
        shots.append(s)
    return shots, tipmeas, (t0, t1)


def flag(s):
    """Artifact classification. Returns a set of flags."""
    fl = set()
    y = s["ph"]["norm"]
    tf = s.get("T_frame", float("nan"))
    if not np.isnan(tf) and (y - tf) > 20.0:
        fl.add("engine_stop_artifact")      # stop re-dated onto a spike
    if s.get("max_step", 0) and not np.isnan(s.get("max_step", float("nan"))) \
            and s.get("max_step", 0) > 8.0:
        fl.add("meter_jump")
    vel = s.get("vel", float("nan"))
    eta = s.get("eta", float("nan"))
    if (not np.isnan(vel) and vel < 0.10) or (not np.isnan(eta) and eta > 350):
        fl.add("slow_meter")
    if not np.isnan(tf) and (tf - y) > 25.0:
        # the CSV stop latched a later stage (creep re-rise) that the engine's
        # quiet-window freeze correctly ignored -- a CSV-instrument failure
        fl.add("csv_stop_artifact")
    return fl


def q1(shots, pool):
    acc = [s for s in shots if s["ph"]["accepted"] == 1 and "B" in s]
    print("\njoined accepted shots with frames: %d" % len(acc))
    if len(acc) < 8:
        print("too few; skipping Q1 for this pool")
        return None
    for s in acc:
        s["flags"] = flag(s)
    art = [s for s in acc if s["flags"] - {"csv_stop_artifact"}]
    csvart = [s for s in acc if s["flags"] == {"csv_stop_artifact"}]
    clean = [s for s in acc if not s["flags"]]
    print("flagged (engine/physics): %d (%s)" % (
        len(art), ", ".join("%s:%s" % (s["seq"], "+".join(sorted(s["flags"])))
                            for s in art)))
    print("flagged (csv-instrument only): %d (%s)" % (
        len(csvart), ", ".join(str(s["seq"]) for s in csvart)))
    clean_plus = clean + csvart   # engine-valid set: y is fine on these
    print("flagged detail:")
    for s in sorted(art + csvart, key=lambda s: s["seq"]):
        print("  seq=%-3s y=%7.1f Tf=%7.1f d=%+7.1f vel=%s eta=%s maxstep=%s %s"
              % (s["seq"], s["ph"]["norm"], s.get("T_frame", float("nan")),
                 s["ph"]["norm"] - s.get("T_frame", float("nan")),
                 s.get("vel", "-"), s.get("eta", "-"),
                 ("%.1f" % s["max_step"]) if not np.isnan(s.get("max_step", float("nan"))) else "-",
                 "+".join(sorted(s["flags"]))))
    y = np.array([s["ph"]["norm"] for s in clean])
    A = np.array([s.get("A_norm", float("nan")) for s in clean])
    B = np.array([s["B"] for s in clean])
    Tf = np.array([s.get("T_frame", float("nan")) for s in clean])
    yp = np.array([s["ph"]["norm"] for s in clean_plus])

    print("\n-- Q1.S1 clean-set targets (csv-artifact shots excluded) --")
    describe("y engine normalized", y)
    describe("y engine (clean+csvart)", yp)
    describe("T_frame csv", Tf)
    describe("A anchor->release", A)
    describe("B release->stop", B)

    print("\n-- Q1.S2 instrument split (engine vs CSV, same frames) --")
    ok = ~np.isnan(Tf)
    d = y[ok] - Tf[ok]
    describe("d = y - T_frame", d)
    if ok.sum() > 8:
        cov = float(np.cov(y[ok], Tf[ok])[0, 1])
        vy, vt = y[ok].var(ddof=1), Tf[ok].var(ddof=1)
        r = cov / math.sqrt(vy * vt)
        print("var(y)=%.1f var(Tf)=%.1f cov=%.1f r=%.3f" % (vy, vt, cov, r))
        print("  -> common (shared frames + real) sd = %.2f ms" % math.sqrt(max(cov, 0)))
        print("  -> engine-specific sd = %.2f ms   csv-specific sd = %.2f ms"
              % (math.sqrt(max(vy - cov, 0)), math.sqrt(max(vt - cov, 0))))

    print("\n-- Q1.S3 split-half dating noise (shared-frame component) --")
    ds = np.array([s["stop_ev"] - s["stop_od"] for s in clean
                   if "stop_ev" in s], dtype=float)
    # robust: exclude split-half failures where one half latched a different stage
    ds_r = ds[np.abs(ds - np.median(ds)) < 60.0] if len(ds) else ds
    sd30 = describe("stop_even - stop_odd (30fps each)", ds_r)
    if sd30 and len(ds_r) > 4:
        var30 = (sd30 ** 2) / 2.0
        var60 = var30 / 4.0
        print("  -> implied 30fps stop-dating sd = %.2f ms;  60fps ~ %.2f ms"
              % (math.sqrt(var30), math.sqrt(var60)))
    dc = np.array([s["cr_ev"] - s["cr_od"] for s in clean if "cr_ev" in s],
                  dtype=float)
    dc_r = dc[np.abs(dc - np.median(dc)) < 40.0] if len(dc) else dc
    sdc = describe("anchor_even - anchor_odd", dc_r)
    if sdc and len(dc_r) > 4:
        print("  -> implied 60fps crossing-dating sd ~ %.2f ms" % (sdc / (2 * 2)))

    print("\n-- Q1.S4 B structure: physical or white? --")
    if len(B) > 10:
        l1 = float(np.corrcoef(B[:-1], B[1:])[0, 1])
        se = 1.0 / math.sqrt(len(B))
        print("lag-1 autocorr(B) = %+.3f (se %.3f)" % (l1, se))
        ts = np.array([s["ph"]["ts"] for s in clean])
        rt = float(np.corrcoef((ts - ts[0]) / 1000.0, B)[0, 1])
        print("corr(session time, B) = %+.3f" % rt)
        for nm, key in [("frame age at rel", "age"), ("dup_pct@stop", "dup"),
                        ("uniqfps@stop", "uniqfps"), ("stop frame gap", "stop_gap"),
                        ("marker mlat", "mlat"), ("v_tip", "v_tip")]:
            x = np.array([s.get(key, float("nan")) for s in clean], dtype=float)
            okx = ~np.isnan(x)
            if okx.sum() > 8 and x[okx].std() > 0:
                rr = float(np.corrcoef(x[okx], B[okx])[0, 1])
                lo, hi = adp.fisher_ci(rr, int(okx.sum()))
                print("corr(%-16s, B) = %+.3f [%+.2f,%+.2f] n=%d"
                      % (nm, rr, lo, hi, okx.sum()))
        # ------------------------------------------------------------------
        # REMOVED 2026-08-06: the "release-phase sawtooth" regression of B on
        # sin/cos(phi) (and any regression of B on phi). DO NOT REBUILD IT.
        #
        # WHY IT WAS INVALID. This script dates t_stop ON a capture frame
        # (stop_features interpolates between frames READ OFF THE SAME GRID),
        # and phi = t_rel - prev_frame_wall is measured FROM that grid. So
        #     B = t_stop - t_rel = k*g - phi        (algebraically, per shot)
        # and B contains -phi with coefficient EXACTLY -1 by construction.
        # The regression recovered "slope -0.90, near-pure 60Hz quantizer,
        # p<0.001" from pure arithmetic; the permutation test only certified
        # that the identity is not a permutation artifact. Every ceiling
        # estimate built on that read is void. Same error class as travel_pp
        # and (peak-fillAtRel)/vel: the "regressor" is a rearrangement of the
        # response, so no p-value can rescue it.
        #
        # WHAT A VALID VERSION NEEDS: a regressor that can be varied
        # INDEPENDENTLY of the response. That is exactly the dev commanded-
        # offset hook (ORION_DEV_FIRE_OFFSET_SWEEP, AutomationEngine
        # [ORION_DEV_FIRE_OFFSET]): displace the scheduled fire by a known
        # per-shot offset, join "Release devoffset: seq=.. applied_ms=.."
        # against the game's own TIMING banner, and regress banner verdict on
        # applied offset. A console-internal verdict cannot be contaminated
        # by our observation grid, so that design -- and only that design --
        # separates "our capture can't see it" from "the console didn't
        # register it".
        # ------------------------------------------------------------------
        # frame-count decomposition: B = k*g - phi + resid, where k = whole
        # frames between the last frame before release and the dated stop.
        # KEPT, but read it for what it is: with resid ~ 0 it CONFIRMS the
        # algebraic identity above, i.e. it characterises the INSTRUMENT (how
        # grid-locked this script's stop dating is, and how good the grid fit
        # g is). It says nothing about when the console registered the press.
        kk, rr_, pp_ = [], [], []
        for s in clean:
            if "phi" not in s or "grid_gap" not in s:
                continue
            g = 16.949  # nominal; refined below
            base = s["t_rel"] - s["phi"]
            x = (s["t_stop"] - base)
            kk.append(x)
            pp_.append(s["phi"])
        if len(kk) > 20:
            x = np.array(kk)
            # estimate true grid period by minimising folded spread around med gap
            best = (1e9, 16.7)
            for g in np.arange(16.5, 17.2, 0.005):
                fold = np.mod(x, g)
                # circular spread
                ang = 2 * math.pi * fold / g
                Rr = math.hypot(np.mean(np.sin(ang)), np.mean(np.cos(ang)))
                if 1 - Rr < best[0]:
                    best = (1 - Rr, g)
            g = best[1]
            k = np.round(x / g)
            resid = x - k * g
            print("grid fit g=%.3f ms; stop sits k frames after pre-release frame:"
                  % g)
            import collections
            cnt = collections.Counter(int(v) for v in k)
            print("  k hist: %s" % dict(sorted(cnt.items())))
            describe("  fold resid (t_stop vs grid)", resid)
            ph = np.array(pp_)
            print("  var(B) split: var(k*g)=%.1f var(phi)=%.1f var(resid)=%.1f "
                  "(sd %.2f/%.2f/%.2f)"
                  % ((k * g).var(ddof=1), ph.var(ddof=1), resid.var(ddof=1),
                     (k * g).std(ddof=1), ph.std(ddof=1), resid.std(ddof=1)))
        # histogram of B - median
        bb = B - np.median(B)
        edges = np.arange(-30, 34, 4)
        h, _ = np.histogram(bb, bins=edges)
        print("B-med hist 4ms bins [-30..30]: %s" % " ".join("%2d" % c for c in h))
        # skew/kurtosis
        m = bb.mean()
        s2 = bb.std(ddof=1)
        sk = float(np.mean(((bb - m) / s2) ** 3))
        ku = float(np.mean(((bb - m) / s2) ** 4)) - 3.0
        print("skew=%.2f excess kurtosis=%.2f" % (sk, ku))
    return dict(clean=clean, art=art, y=y, A=A, B=B, Tf=Tf)


def q2(shots, pool):
    withland = [s for s in shots if "land" in s]
    print("\n-- Q2 green window (pool %s, n=%d with landing) --" % (pool, len(withland)))
    if not withland:
        return
    ge = np.array([s["land"]["ge"] for s in withland])
    gs = np.array([s["land"]["gs"] for s in withland])
    gobs_w = np.array([s["land"]["gobs_w"] for s in withland])
    settled = np.array([s["land"]["settled"] for s in withland])
    fillrel = np.array([s["land"]["fillAtRel"] for s in withland])
    print("green_end == 100.00 on %d/%d landings (pinned=definitional)"
          % (int(np.sum(ge == 100.0)), len(ge)))
    print("green_start == 98.00 (clamp floor tell): %d/%d"
          % (int(np.sum(gs == 98.0)), len(gs)))
    describe("landing green_start", gs, "pp")
    describe("landing width (100-gs)", 100.0 - gs, "pp")
    # decision-time window from Release issued
    g0r = np.array([s.get("g0_rel", float("nan")) for s in withland])
    okr = ~np.isnan(g0r)
    describe("decision green_start (issued)", g0r[okr], "pp")
    describe("decision width (100-g0)", 100.0 - g0r[okr], "pp")
    if okr.sum() > 8:
        both = okr & (gs > 0)
        dd = gs[both] - g0r[both]
        describe("gstart landing - decision", dd, "pp (+=shrink from below)")
        nfl = both & (gs != 98.0)
        if nfl.sum() > 4:
            describe("  same, excl clamp-floor landings", gs[nfl] - g0r[nfl], "pp")
    # per-frame green trajectory
    gd = np.array([s.get("gstart_dec", float("nan")) for s in withland])
    gl = np.array([s.get("gstart_land", float("nan")) for s in withland])
    okg = ~np.isnan(gd) & ~np.isnan(gl)
    if okg.sum() > 4:
        describe("frame gstart @decision zone", gd[okg], "pp")
        describe("frame gstart @stop+-300ms", gl[okg], "pp")
        describe("frame delta (land - dec)", gl[okg] - gd[okg], "pp (+=shrink)")
    npre = np.array([s.get("g_frames_pre", 0) for s in withland], dtype=float)
    nlate = np.array([s.get("g_frames_late", 0) for s in withland], dtype=float)
    print("green visible pre-release on %d/%d shots (med frames %.0f); "
          "near stop on %d/%d"
          % (int(np.sum(npre > 0)), len(npre), float(np.median(npre)),
             int(np.sum(nlate > 0)), len(nlate)))
    describe("settled fill", settled, "pp")
    vt_all = np.array([s.get("v_tip", float("nan")) for s in withland])
    okv = ~np.isnan(vt_all) & (settled > 0)
    if okv.sum() > 8:
        r = float(np.corrcoef(vt_all[okv], settled[okv])[0, 1])
        print("corr(v_tip, settled) = %+.3f  (0 if freeze at fixed fill cap)" % r)
    # velocity for ms conversion: per-shot v_tip when sane, else attribution vel,
    # else pool median
    vel_attr = np.array([s.get("vel", float("nan")) for s in withland])
    v_use = np.where((vt_all > 0.08) & (vt_all < 0.5), vt_all, vel_attr)
    v_med = float(np.nanmedian(v_use)) if np.isfinite(np.nanmedian(v_use)) else 0.2
    v_use = np.where(np.isnan(v_use) | (v_use < 0.08), v_med, v_use)
    marg_top = (100.0 - settled) / v_use
    marg_bot = (settled - g0r) / v_use
    okm = (settled > 0)
    describe("displayed margin to top (ms)", marg_top[okm], "ms")
    describe("displayed margin above gstart", marg_bot[okm & okr], "ms")
    # solve sigma_land from a counted green rate given median margins
    mt = float(np.median(marg_top[okm]))
    mb = float(np.median(marg_bot[okm & okr])) if (okm & okr).sum() > 4 else float("nan")
    print("median margins: top %.1f ms, bottom %.1f ms (window %.1f ms)"
          % (mt, mb, mt + mb))
    if np.isfinite(mb):
        for label, gr, dr in [("42/44 all-driven", 42, 44),
                              ("42/42 excl slow-meter", 42, 42)]:
            p_obs = gr / dr
            # find sigma s.t. Phi(mt/s) - Phi(-mb/s) = p (point est or its floor)
            lo, hi_ = 0.5, 60.0
            from math import erf, sqrt

            def P(sig):
                return 0.5 * (1 + erf(mt / sig / sqrt(2))) \
                    - 0.5 * (1 + erf(-mb / sig / sqrt(2)))
            target = min(p_obs, 0.999)
            if P(0.51) < target:
                print("  %s: green rate above model max (margins too tight?)" % label)
                continue
            while hi_ - lo > 0.01:
                mid = 0.5 * (lo + hi_)
                if P(mid) > target:
                    lo = mid
                else:
                    hi_ = mid
            print("  banner %s (p=%.3f) -> sigma_land ~ %.1f ms at these margins"
                  % (label, p_obs, 0.5 * (lo + hi_)))
        # expected green vs aim shift, at a few sigma values
        shifts = [-10, -5, 0, 5, 10]
        print("  E[green] vs aim shift (negative = earlier), per sigma:")
        for sig in [5.0, 7.0, 9.0, 12.0]:
            row = []
            for sh in shifts:
                mtop = np.maximum(marg_top[okm & okr] + sh, 0.0)
                mbot = np.maximum(marg_bot[okm & okr] - sh, 0.0)
                pg = np.mean([0.5 * (1 + math.erf(a / sig / math.sqrt(2)))
                              - 0.5 * (1 + math.erf(-b / sig / math.sqrt(2)))
                              for a, b in zip(mtop, mbot)])
                row.append("%+3d:%.3f" % (sh, pg))
            print("    sigma %4.1f: %s" % (sig, "  ".join(row)))
    # freeze tracks release?
    oks = (settled > 0) & (fillrel > 0)
    if oks.sum() > 8:
        r = float(np.corrcoef(fillrel[oks], settled[oks])[0, 1])
        sl = r * settled[oks].std(ddof=1) / fillrel[oks].std(ddof=1)
        print("settled vs fillAtRel: r=%.3f slope=%.2f  (1.0 = freeze rides release)"
              % (r, sl))
        describe("settled - fillAtRel", settled[oks] - fillrel[oks], "pp")
    # landing position vs window (validity check)
    pos_dec = settled - g0r      # settled minus DECISION-time window start
    okp = okr & (settled > 0)
    if okp.sum() > 4:
        describe("settled - decision gstart", pos_dec[okp],
                 "pp (<0 = displayed early)")
        print("displayed-in-window (vs decision gs): %d/%d"
              % (int(np.sum(pos_dec[okp] >= 0)), int(okp.sum())))
    pos_land = settled - gs
    describe("settled - landing gstart", pos_land, "pp")
    print("displayed-in-window (vs landing gs): %d/%d"
          % (int(np.sum(pos_land >= 0)), len(pos_land)))
    # does displayed position track engine y?
    y = np.array([s["ph"]["norm"] for s in withland])
    okc = okp & ~np.isnan(y)
    if okc.sum() > 8:
        r = float(np.corrcoef(y[okc], pos_dec[okc])[0, 1])
        print("corr(y, settled-position) = %+.3f  (coherence of displayed landing)"
              % r)
    # velocity for pp->ms
    vt = np.array([s.get("v_tip", float("nan")) for s in withland])
    describe("v_tip (fill 70..~settled)", vt, "pp/ms")


def q1_triangle(res, shots, greens=42, driven=44):
    print("\n-- Q1.S5 banner triangle --")
    vt = np.array([s.get("v_tip", float("nan")) for s in shots if "B" in s])
    vt = vt[~np.isnan(vt)]
    v = float(np.median(vt)) if len(vt) else 0.185
    g0r = np.array([s.get("g0_rel", float("nan")) for s in shots])
    g0r = g0r[~np.isnan(g0r)]
    w_pp = float(np.median(100.0 - g0r)) if len(g0r) else float("nan")
    print("median tip velocity = %.3f pp/ms; median decision window = %.2f pp"
          % (v, w_pp))
    p = greens / driven
    from math import erf, sqrt

    def phi(x):
        return 0.5 * (1 + erf(x / sqrt(2)))
    # Wilson lower bound on green rate
    z = 1.96
    n = driven
    ph = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z / (1 + z * z / n) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    plo = ph - half
    print("banner: %d/%d green = %.3f (Wilson 95%% lo %.3f)" % (greens, driven, p, plo))
    for w in [2.0, w_pp, 9.35]:
        if not np.isfinite(w):
            continue
        w_ms = w / v
        # centered aim: P = 2*phi(W/2/sigma)-1 -> sigma
        # edge aim (aim at late edge, one-sided): P = phi(W/sigma)-phi(0)= phi(W/s)-0.5
        for label, inv in [
            ("centered", lambda P: (w_ms / 2.0) / max(ppf((P + 1) / 2.0), 1e-6)),
            ("late-edge", lambda P: w_ms / max(ppf(min(P + 0.5, 0.999)), 1e-6)),
        ]:
            try:
                sig = inv(p)
                sig_lo = inv(plo)
                print("  window %.2fpp=%.1fms  aim=%-9s -> sigma_land <= %.1f ms "
                      "(at Wilson-lo %.1f)" % (w, w_ms, label, sig, sig_lo))
            except Exception as e:
                print("  window %.2fpp aim=%s: %s" % (w, label, e))


def ppf(q):
    """Inverse normal CDF (Acklam approximation)."""
    if q <= 0 or q >= 1:
        return float("nan")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow = 0.02425
    if q < plow:
        ql = math.sqrt(-2 * math.log(q))
        return (((((c[0] * ql + c[1]) * ql + c[2]) * ql + c[3]) * ql + c[4]) * ql + c[5]) / \
               ((((d[0] * ql + d[1]) * ql + d[2]) * ql + d[3]) * ql + 1)
    if q > 1 - plow:
        ql = math.sqrt(-2 * math.log(1 - q))
        return -(((((c[0] * ql + c[1]) * ql + c[2]) * ql + c[3]) * ql + c[4]) * ql + c[5]) / \
               ((((d[0] * ql + d[1]) * ql + d[2]) * ql + d[3]) * ql + 1)
    ql = q - 0.5
    r = ql * ql
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * ql / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def windows_census():
    """All-session Release landing + issued-green census, segmented by hour."""
    t0 = iso_ms("2026-08-04T00:00:00Z")
    t1 = iso_ms("2026-08-07T00:00:00Z")
    ig_l, landing_l = [], []
    for lg in LOGS:
        if not os.path.exists(lg):
            continue
        with open(lg, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = RE_ISSUED_GREEN.match(line)
                if m:
                    ts = iso_ms(m.group(1))
                    if t0 <= ts <= t1:
                        ig_l.append(dict(ts=ts, g0=float(m.group(4)),
                                         g1=float(m.group(5))))
                    continue
                m = RE_LANDING.match(line)
                if m:
                    ts = iso_ms(m.group(1))
                    if t0 <= ts <= t1:
                        landing_l.append(dict(
                            ts=ts, gs=float(m.group(8)), ge=float(m.group(9)),
                            settled=float(m.group(5)),
                            fillAtRel=float(m.group(6))))
    # de-dup across rotated logs
    ig = {round(x["ts"]): x for x in ig_l}.values()
    landing = {round(x["ts"]): x for x in landing_l}.values()
    print("== all-session green window census: %d issued-green, %d landings =="
          % (len(list(ig)), len(list(landing))))
    from collections import defaultdict
    buck = defaultdict(list)
    for L in landing:
        h = datetime.fromtimestamp(L["ts"] / 1000, timezone.utc).strftime("%m-%dT%H")
        buck[h].append(L)
    print("%-9s %4s | landing gs: %5s %5s %5s | floor98 | width med | ge=100"
          % ("hour", "n", "min", "med", "max"))
    for h in sorted(buck):
        Ls = buck[h]
        gs = np.array([x["gs"] for x in Ls])
        ge = np.array([x["ge"] for x in Ls])
        print("%-9s %4d | %5.1f %5.1f %5.1f | %3d/%3d | %6.2fpp | %d/%d"
              % (h, len(Ls), gs.min(), np.median(gs), gs.max(),
                 int(np.sum(gs == 98.0)), len(gs),
                 float(np.median(100 - gs)), int(np.sum(ge == 100.0)), len(ge)))
    # decision-time (issued) windows
    buck2 = defaultdict(list)
    for G in ig:
        h = datetime.fromtimestamp(G["ts"] / 1000, timezone.utc).strftime("%m-%dT%H")
        buck2[h].append(G)
    print("\n%-9s %4s | issued gs: %5s %5s %5s | width med | g1=100"
          % ("hour", "n", "min", "med", "max"))
    for h in sorted(buck2):
        Gs = buck2[h]
        g0 = np.array([x["g0"] for x in Gs])
        g1 = np.array([x["g1"] for x in Gs])
        print("%-9s %4d | %5.1f %5.1f %5.1f | %6.2fpp | %d/%d"
              % (h, len(Gs), g0.min(), np.median(g0), g0.max(),
                 float(np.median(100 - g0)), int(np.sum(g1 >= 99.99)), len(g1)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default=None, choices=list(POOLS))
    ap.add_argument("--windows", action="store_true")
    ap.add_argument("--dump-csv", default=None)
    args = ap.parse_args()
    np.seterr(all="ignore")
    if args.windows:
        windows_census()
        return
    pools = [args.pool] if args.pool else ["batch"]
    for pool in pools:
        shots, tipmeas, _ = join_pool(pool)
        res = q1(shots, pool)
        q2(shots, pool)
        if res is not None:
            q1_triangle(res, [s for s in shots if s["ph"]["accepted"] == 1])
        if args.dump_csv:
            import csv as _csv
            keys = ["seq", "t_rel", "A_norm", "B", "T_frame", "off", "settled",
                    "peak", "stop_gap", "v_tip", "phi", "grid_gap", "max_step",
                    "g0_rel", "g1_rel", "gstart_dec", "gstart_land", "vel",
                    "eta", "age", "mlat", "fillAtRel"]
            with open(args.dump_csv, "w", newline="") as fh:
                w = _csv.writer(fh)
                w.writerow(["ts", "y", "raw", "anchor_pct", "accepted",
                            "shot_type", "flags", "land_settled", "land_gs",
                            "land_graded"] + keys)
                for s in shots:
                    L = s.get("land", {})
                    w.writerow([s["ph"]["ts"], s["ph"]["norm"], s["ph"]["raw"],
                                s["ph"]["anchor_pct"], s["ph"]["accepted"],
                                s["ph"]["shot_type"],
                                "+".join(sorted(flag(s))) if "B" in s else "",
                                L.get("settled", ""), L.get("gs", ""),
                                L.get("graded", "")] +
                               [s.get(k, "") for k in keys])
            print("dumped -> %s" % args.dump_csv)


if __name__ == "__main__":
    main()
