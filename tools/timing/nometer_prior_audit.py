#!/usr/bin/env python
"""nometer_prior_audit.py -- what hold does the METER path actually use, and how far is
the press-anchored prior from it?  (offline diagnostic; reads logs only)

    python tools/timing/nometer_prior_audit.py
    python tools/timing/nometer_prior_audit.py --logs logs/orion_native.log.1 logs/orion_native.log \
        --grades "logs/diagnostics/framedump_archive/*/panel_grade.csv" \
        --out logs/diagnostics/nometer_study

WHY
===
The NO METER path (AutomationEngine::processInputTimedIdle) releases at

    hold = max(kInputTimedMinHoldMs, pressAnchoredLearnedTipMs(type) - inputTimedLeadMs)

`pressAnchoredLearnedTipMs` is fed by emitPressTipObservation(), which logs

    PRESS-TIP OBSERVATION: ... press_wall_ms=P real_tip_wall_ms=R press_to_tip_ms=D V_ms=V ...

with   R = tip_in_video - applied_delay - V   and   D = R - P.
The observation SUBTRACTS V; the meter-path predictor (autonomousTipDecision) ADDS the same V
back before it fires.  The NO METER consumer never adds it back -- and it subtracts a lead on
top.  This script measures the size of that gap against the only ground truth available: the
press->release hold the METER path actually used on the same shots.

WHAT IS MEASURED (per shot, per shot type)
==========================================
  hold_ms      = command_issued_ms ("Precise dispatch timing") - press_wall_ms
                 -> the press->release hold the VISION path chose.  Same monotonic clock;
                    cross-checked against fire_epoch_ms - t("Physical shot epoch") in UTC.
  ptt_ms       = press_to_tip_ms                   (the learner's raw sample)
  tip_seen_ms  = press_to_tip_ms + V_ms            (press -> tip as it appears ON SCREEN)
  eps_ms       = ptt_ms - hold_ms                  (what the learner adds on top of the hold)
  lead_ms      = the vision path's Shot Lead for that shot (TIP RESERVATION)

and, where a graded session exists, the game's own TIMING word joined FIFO the way
tools/timing/banner_join.py does it (banner belongs to the latest release 0.2..3.5 s before
its onset).

HARD BOUNDARY: analysis tooling only.  Never imported by the engine/orchestrator/sidecar.
"""
from __future__ import annotations

import argparse
import csv
import glob
import itertools
import math
import os
import re
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KV = re.compile(r"([A-Za-z_]\w*)=(-?[\d.]+)")
TS_LEN = 24


def _ts(line: str) -> float:
    """UTC epoch seconds of a log line header (2026-09-13T02:19:11.789Z)."""
    try:
        return datetime.fromisoformat(line[:TS_LEN].replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("nan")


def _f(d, k, default=float("nan")):
    try:
        return float(d[k])
    except (KeyError, TypeError, ValueError):
        return default


# seq/epoch counters AND the monotonic clock restart with every app launch, and the logs
# concatenate several launches.  Every cross-line join below therefore carries the log-line
# UTC of the source line and is accepted only if the two lines are within MAX_JOIN_S of each
# other -- a stale same-seq entry from an earlier launch can never be picked up.
MAX_JOIN_S = 12.0


def parse_logs(paths):
    """One record per shot that produced a PRESS-TIP OBSERVATION (a graded meter landing)."""
    press_utc = {}         # physical epoch -> (UTC of the "Physical shot epoch" line,)
    dispatch = {}          # seq -> (command_issued_ms, utc)
    submit = {}            # seq -> (fire_epoch_ms, utc)
    landing = {}           # seq -> (fields, utc)
    reservation = {}       # physical epoch -> (fields, utc)
    shotmode = {}          # seq -> (ShotMode at release, utc)
    nometer = []           # NO METER arm lines
    nometer_rel = []       # NO METER RELEASE lines
    notowned = []          # SHOT NOT OWNED lines (press_unanswered_no_meter etc.)
    records = []

    def fresh(store, key, t_obs):
        v = store.get(key)
        if v is None:
            return None
        payload, t_src = v
        if not (0.0 <= t_obs - t_src <= MAX_JOIN_S):
            return None
        return payload

    for line in itertools.chain.from_iterable(
            open(p, encoding="utf-8", errors="replace") for p in paths):
        if "PRESS-TIP OBSERVATION:" in line:
            d = dict(KV.findall(line))
            m = re.search(r"shot_type=(\S+)", line)
            rec = dict(
                t_utc=_ts(line),
                epoch=int(_f(d, "physical_epoch", -1)),
                seq=int(_f(d, "seq", -1)),
                shot_type=(m.group(1).replace("_", " ") if m else "unknown"),
                press_wall_ms=_f(d, "press_wall_ms"),
                real_tip_wall_ms=_f(d, "real_tip_wall_ms"),
                ptt_ms=_f(d, "press_to_tip_ms"),
                applied_delay_ms=_f(d, "applied_delay_ms"),
                v_ms=_f(d, "V_ms"),
                stop_confirmed=int(_f(d, "stop_confirmed", 0)),
                accepted=int(_f(d, "accepted", 0)),
                confidence=(re.search(r"confidence=(\w+)", line) or [None, "?"])[1],
                v_src=(re.search(r"v_src=(\w+)", line) or [None, "?"])[1],
            )
            # Resolve the cross-line joins NOW, while the per-launch state is still current:
            # seq / physical-epoch counters and the monotonic clock all restart on relaunch.
            t_obs = rec["t_utc"]
            cmd = fresh(dispatch, rec["seq"], t_obs)
            cmd = float("nan") if cmd is None else cmd
            rec["command_issued_ms"] = cmd
            hold = cmd - rec["press_wall_ms"] if not math.isnan(cmd) else float("nan")
            rec["hold_ms"] = hold if (not math.isnan(hold) and 0.0 < hold < 6000.0) \
                else float("nan")
            fire = fresh(submit, rec["seq"], t_obs)
            fire = float("nan") if fire is None else fire
            rec["fire_epoch_ms"] = fire
            rec["release_utc"] = fire / 1000.0 if not math.isnan(fire) else float("nan")
            pu = press_utc.get(rec["epoch"], float("nan"))
            if not math.isnan(pu) and not (0.0 <= t_obs - pu <= MAX_JOIN_S):
                pu = float("nan")
            hu = (fire - pu * 1000.0) if not (math.isnan(fire) or math.isnan(pu)) else float("nan")
            rec["hold_utc_ms"] = hu if (not math.isnan(hu) and 0.0 < hu < 6000.0) else float("nan")
            rec["tip_seen_ms"] = rec["ptt_ms"] + rec["v_ms"] if rec["v_ms"] > 0 else float("nan")
            rec["eps_ms"] = rec["ptt_ms"] - rec["hold_ms"]
            res = fresh(reservation, rec["epoch"], t_obs) or {}
            rec["lead_ms"] = res.get("lead_ms", float("nan"))
            rec["lead_source"] = res.get("lead_source", "?")
            rec.update({("land_" + k): v
                        for k, v in (fresh(landing, rec["seq"], t_obs) or {}).items()})
            rec["mode"] = (fresh(shotmode, rec["seq"], t_obs) or {}).get("mode", "?")
            records.append(rec)
        elif "Physical shot epoch:" in line:
            d = dict(KV.findall(line))
            press_utc[int(_f(d, "epoch", -1))] = _ts(line)
        elif "Precise dispatch timing:" in line:
            d = dict(KV.findall(line))
            dispatch[int(_f(d, "seq", -1))] = (_f(d, "command_issued_ms"), _ts(line))
        elif "Release submit:" in line:
            d = dict(KV.findall(line))
            submit[int(_f(d, "seq", -1))] = (_f(d, "fire_epoch_ms"), _ts(line))
        elif "Release landing:" in line:
            d = dict(KV.findall(line))
            landing[int(_f(d, "seq", -1))] = (dict(
                peak_fill=_f(d, "peak_fill"), fill_at_rel=_f(d, "fill_at_rel"),
                green_start=_f(d, "green_start"), green_end=_f(d, "green_end"),
                shot=line.split("shot=")[-1].strip()), _ts(line))
        elif "Shot state: Holding -> Releasing" in line:
            d = dict(KV.findall(line))
            m = re.search(r"mode=(\w+)", line)
            shotmode[int(_f(d, "seq", -1))] = (dict(mode=m.group(1) if m else "?"), _ts(line))
        elif "TIP RESERVATION: disposition=reservation_created" in line:
            d = dict(KV.findall(line))
            reservation[int(_f(d, "physical_epoch", -1))] = (dict(
                lead_ms=_f(d, "lead_ms"),
                lead_source=(re.search(r"lead_source=(\w+)", line) or [None, "?"])[1]), _ts(line))
        elif "NO METER: hold_ms=" in line:
            d = dict(KV.findall(line))
            # [ORION_NO_METER_V2 2026-09-14] Two line shapes.
            #   v1: "... type=<T> source=prior-lead prior_ms= prior_n= lead_ms= rhythm= arm="
            #   v2: "... type=<T> h_ref= delta= delta_src= rhythm= floor= source=v2 arm="
            # The shot type is delimited by the NEXT key on both, so anchor on that rather than
            # on `source=`, which moved to the end of the line in v2.
            m = re.search(r"type=(.+?) (?:h_ref|source)=", line)
            src = re.search(r"source=(\S+)", line)
            nometer.append(dict(t_utc=_ts(line), hold_ms=_f(d, "hold_ms"),
                                shot_type=(m.group(1) if m else "?"),
                                source=(src.group(1) if src else "?"),
                                prior_ms=_f(d, "prior_ms"), prior_n=_f(d, "prior_n"),
                                lead_ms=_f(d, "lead_ms"), rhythm=int(_f(d, "rhythm", 0)),
                                # v2 terms; NaN on a v1 line.
                                h_ref_ms=_f(d, "h_ref"), delta_ms=_f(d, "delta"),
                                delta_src=(re.search(r"delta_src=(\S+)", line)
                                           or [None, "-"])[1],
                                floored=int(_f(d, "floor", 0)),
                                arm=int(_f(d, "arm", -1))))
        elif "NO METER RELEASE:" in line:
            d = dict(KV.findall(line))
            nometer_rel.append(dict(t_utc=_ts(line), requested_ms=_f(d, "requested_ms"),
                                    actual_ms=_f(d, "actual_ms"),
                                    lateness_ms=_f(d, "lateness_ms")))
        elif "SHOT NOT OWNED:" in line:
            d = dict(KV.findall(line))
            notowned.append(dict(t_utc=_ts(line), hold_ms=_f(d, "hold_ms"),
                                 reason=(re.search(r"reason=(\S+)", line) or [None, "?"])[1],
                                 shot_type=line.split("shot_type=")[-1].split(" unstamped")[0].strip()
                                 if "shot_type=" in line else "?"))

    records.sort(key=lambda r: r["t_utc"])
    return records, nometer, nometer_rel, notowned


def parse_grades(patterns):
    """panel_grade.csv rows -> [(epoch_seconds, WORD)] sorted."""
    out = []
    for pat in patterns:
        for path in sorted(glob.glob(pat)):
            with open(path, newline="", encoding="utf-8", errors="replace") as fh:
                for r in csv.DictReader(fh):
                    word = (r.get("word") or "").strip().upper()
                    if not word or word == "UNKNOWN":
                        continue
                    try:
                        out.append((float(r["t0"]), word, os.path.basename(os.path.dirname(path))))
                    except (KeyError, TypeError, ValueError):
                        continue
    out.sort()
    return out


def join_grades(records, grades, lo=0.2, hi=3.5):
    """Same FIFO rule as banner_join.py: a banner belongs to the latest release lo..hi s before it."""
    usable = [r for r in records if not math.isnan(r.get("release_utc", float("nan")))]
    usable.sort(key=lambda r: r["release_utc"])
    used = set()
    n = 0
    for t0, word, session in grades:
        cands = [r for r in usable if lo <= t0 - r["release_utc"] <= hi and id(r) not in used]
        if not cands:
            continue
        r = max(cands, key=lambda r: r["release_utc"])
        used.add(id(r))
        r["grade"] = word
        r["grade_session"] = session
        r["grade_lag_s"] = t0 - r["release_utc"]
        n += 1
    return n


def _stats(vals):
    vals = [v for v in vals if isinstance(v, float) and not math.isnan(v)]
    if not vals:
        return dict(n=0, med=float("nan"), q1=float("nan"), q3=float("nan"), iqr=float("nan"),
                    rmad=float("nan"), within20=float("nan"), within30=float("nan"))
    vals.sort()
    med = st.median(vals)
    if len(vals) >= 4:
        q1, _, q3 = st.quantiles(vals, n=4)
    else:
        q1, q3 = vals[0], vals[-1]
    rmad = 1.4826 * st.median([abs(v - med) for v in vals])
    return dict(n=len(vals), med=med, q1=q1, q3=q3, iqr=q3 - q1, rmad=rmad,
                within20=100.0 * sum(1 for v in vals if abs(v - med) <= 20.0) / len(vals),
                within30=100.0 * sum(1 for v in vals if abs(v - med) <= 30.0) / len(vals))


def _fmt(x, w=7, p=1):
    return ("%*.*f" % (w, p, x)) if isinstance(x, float) and not math.isnan(x) else " " * (w - 1) + "-"


def summarise(records, nometer, nometer_rel, notowned, out=sys.stdout, green_words=("EXCELLENT",)):
    priors = {}
    for a in nometer:
        if a["prior_ms"] > 0:
            priors[a["shot_type"]] = (a["prior_ms"], a["prior_n"])

    ok = [r for r in records if not math.isnan(r["hold_ms"])]
    print("shots with a PRESS-TIP OBSERVATION: %d   (with a dated release: %d)"
          % (len(records), len(ok)), file=out)
    dis = [r["hold_utc_ms"] - r["hold_ms"] for r in ok
           if not math.isnan(r["hold_utc_ms"])]
    if dis:
        print("clock cross-check  hold(UTC log lines) - hold(monotonic): median %+.1f ms, "
              "IQR %.1f ms, n=%d  (both clocks agree => the monotonic join is sound)"
              % (st.median(dis), _stats(dis)["iqr"], len(dis)), file=out)
    leads = _stats([r["lead_ms"] for r in ok])
    vs = _stats([r["v_ms"] for r in ok])
    print("vision-path Shot Lead: median %.1f ms (n=%d)   V_ms (l_fixed): median %.1f ms "
          "IQR %.1f" % (leads["med"], leads["n"], vs["med"], vs["iqr"]), file=out)
    eps = _stats([r["eps_ms"] for r in ok])
    print("eps = press_to_tip - hold (all types, all shots): median %+.1f ms  IQR %.1f  n=%d"
          % (eps["med"], eps["iqr"], eps["n"]), file=out)
    graded = [r for r in ok if r.get("grade")]
    if graded:
        print("graded shots joined to a banner: %d  (%s)"
              % (len(graded), ", ".join("%s=%d" % (w, sum(1 for r in graded if r["grade"] == w))
                                        for w in sorted({r["grade"] for r in graded}))), file=out)
    print(file=out)

    hdr = ("%-12s %4s %8s %7s %7s %6s %8s %7s %8s %5s %8s %8s %8s %8s"
           % ("type", "n", "hold_med", "rMAD", "hIQR", "w30%", "ptt_med", "eps", "tipseen",
              "nG", "Ghold", "prior_ms", "bias_ms", "d_vs_SS"))
    print(hdr, file=out)
    print("-" * len(hdr), file=out)

    rows = []
    types = sorted({r["shot_type"] for r in ok})
    base = _stats([r["hold_ms"] for r in ok if r["shot_type"] == "Standstill"])["med"]
    for t in types + ["ALL"]:
        sel = ok if t == "ALL" else [r for r in ok if r["shot_type"] == t]
        g = [r for r in sel if r.get("grade") in green_words]
        h, p_, ts_, e_, l_ = (_stats([r["hold_ms"] for r in sel]),
                              _stats([r["ptt_ms"] for r in sel]),
                              _stats([r["tip_seen_ms"] for r in sel]),
                              _stats([r["eps_ms"] for r in sel]),
                              _stats([r["lead_ms"] for r in sel]))
        gh, gp = _stats([r["hold_ms"] for r in g]), _stats([r["ptt_ms"] for r in g])
        prior, prior_n = priors.get(t, (float("nan"), float("nan")))
        # The NO METER hold that reproduces the meter path's own release is prior - eps:
        # every observation that taught the prior is (that shot's hold) + eps.
        bias = -e_["med"]
        delta = h["med"] - base if t not in ("ALL",) else float("nan")
        print("%-12s %4d %s %s %s %s %s %s %s %5d %s %s %s %s"
              % (t[:12], h["n"], _fmt(h["med"], 9), _fmt(h["rmad"], 8), _fmt(h["iqr"], 8),
                 _fmt(h["within30"], 7, 0), _fmt(p_["med"], 9), _fmt(e_["med"], 8),
                 _fmt(ts_["med"], 9), gh["n"], _fmt(gh["med"], 9),
                 _fmt(prior, 9), _fmt(bias, 9), _fmt(delta, 9)), file=out)
        rows.append(dict(shot_type=t, n=h["n"],
                         hold_med=h["med"], hold_rmad=h["rmad"], hold_q1=h["q1"],
                         hold_q3=h["q3"], hold_iqr=h["iqr"], hold_within30_pct=h["within30"],
                         ptt_med=p_["med"], ptt_iqr=p_["iqr"],
                         tip_seen_med=ts_["med"], eps_med=e_["med"], eps_iqr=e_["iqr"],
                         lead_med=l_["med"], v_med=_stats([r["v_ms"] for r in sel])["med"],
                         green_n=gh["n"], green_hold_med=gh["med"], green_hold_iqr=gh["iqr"],
                         green_ptt_med=gp["med"],
                         prior_ms=prior, prior_n=prior_n,
                         prior_bias_ms=bias, delta_vs_standstill_ms=delta))
    print(file=out)
    print("hold_med  = press -> release the METER path actually used (ms).  Press-down and the", file=out)
    print("            release travel the SAME pipe, so the command latency cancels: the console", file=out)
    print("            sees exactly this hold duration.  It is the whole NO METER control target.", file=out)
    print("rMAD/hIQR = robust spread of that hold; w30% = share within +/-30 ms of the median", file=out)
    print("ptt_med   = press_to_tip_ms, the sample the press-anchored learner stores", file=out)
    print("eps       = ptt - hold.  The learner dates the tip from the meter STOP, which is our", file=out)
    print("            own release seen through the video, then subtracts V: eps is the part of", file=out)
    print("            the command->visible-stop loop that V does not cover.", file=out)
    print("tipseen   = ptt + V, i.e. press -> tip as it appears ON SCREEN (NOT a hold target)", file=out)
    print("bias_ms   = -eps: add this to the stored prior to get the meter path's own hold.", file=out)
    print("d_vs_SS   = hold_med - Standstill hold_med, the per-type offset a single slider needs", file=out)

    modes = sorted({r.get("mode", "?") for r in ok})
    if len(modes) > 1:
        print(file=out)
        print("by ShotMode at release (Rhythm/tempo shots run as TempoSquare):", file=out)
        for m in modes:
            for t in sorted({r["shot_type"] for r in ok if r.get("mode") == m}):
                s = _stats([r["hold_ms"] for r in ok
                            if r.get("mode") == m and r["shot_type"] == t])
                if s["n"] >= 4:
                    print("  mode=%-12s %-12s n=%-4d hold_med=%8.1f rMAD=%6.1f"
                          % (m, t, s["n"], s["med"], s["rmad"]), file=out)

    if nometer:
        print(file=out)
        print("NO METER arms: %d" % len(nometer), file=out)
        holds = sorted({round(a["hold_ms"], 1) for a in nometer})
        print("  hold_ms values swept: %s" % ", ".join("%.1f" % h for h in holds), file=out)
        for t in sorted({a["shot_type"] for a in nometer}):
            sub = [a for a in nometer if a["shot_type"] == t]
            # [ORION_NO_METER_V2 2026-09-14] v2 lines carry no prior and no lead — that IS the
            # fix — so they report the terms of the sum instead.
            v2 = [a for a in sub if a["source"] == "v2"]
            if v2:
                print("  type=%-12s n=%-3d h_ref swept %.0f..%.0f  delta=%.0f (%s)  "
                      "hold %.1f..%.1f" % (t, len(v2),
                                           min(a["h_ref_ms"] for a in v2),
                                           max(a["h_ref_ms"] for a in v2),
                                           v2[0]["delta_ms"], v2[0]["delta_src"],
                                           min(a["hold_ms"] for a in v2),
                                           max(a["hold_ms"] for a in v2)), file=out)
            legacy = [a for a in sub if a["source"] != "v2"]
            if legacy:
                print("  type=%-12s n=%-3d prior_ms=%.1f prior_n=%.0f  lead swept %.0f..%.0f -> "
                      "hold %.1f..%.1f  [v1 law]"
                      % (t, len(legacy), legacy[0]["prior_ms"], legacy[0]["prior_n"],
                         min(a["lead_ms"] for a in legacy),
                         max(a["lead_ms"] for a in legacy),
                         min(a["hold_ms"] for a in legacy),
                         max(a["hold_ms"] for a in legacy)), file=out)
        print("  NO METER RELEASE lines: %d of %d arms (the rest were cancelled by the player "
              "letting go early)" % (len(nometer_rel), len(nometer)), file=out)
        if nometer_rel:
            lat = _stats([r["lateness_ms"] for r in nometer_rel])
            print("  scheduler lateness: median %.3f ms, max %.3f ms  -> the TIMER is exact; "
                  "any error is the control law" % (lat["med"],
                                                    max(r["lateness_ms"] for r in nometer_rel)),
                  file=out)
    if notowned:
        print(file=out)
        short = [n for n in notowned if not math.isnan(n["hold_ms"])]
        print("SHOT NOT OWNED lines: %d (with hold_ms: %d)" % (len(notowned), len(short)), file=out)
        for reason in sorted({n["reason"] for n in notowned}):
            sub = [n for n in short if n["reason"] == reason]
            if not sub:
                continue
            s = _stats([n["hold_ms"] for n in sub])
            print("  %-34s n=%-3d hold_ms median %.1f  range %.1f..%.1f"
                  % (reason, len(sub), s["med"], min(n["hold_ms"] for n in sub),
                     max(n["hold_ms"] for n in sub)), file=out)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", nargs="+",
                    default=[os.path.join(REPO, "logs", "orion_native.log.1"),
                             os.path.join(REPO, "logs", "orion_native.log")])
    ap.add_argument("--grades", nargs="*",
                    default=[os.path.join(REPO, "logs", "diagnostics", "framedump_archive",
                                          "*", "panel_grade.csv")])
    ap.add_argument("--out", default=os.path.join(REPO, "logs", "diagnostics", "nometer_study"))
    ap.add_argument("--green-words", nargs="*", default=["EXCELLENT"])
    ap.add_argument("--min-day", default=None, help="drop shots before this UTC day (YYYY-MM-DD)")
    ap.add_argument("--v-band", nargs=2, type=float, default=None, metavar=("LO", "HI"),
                    help="keep only shots whose V_ms is in this band. eps = ptt - hold is "
                         "(loop - V), so a drifted V inflates it; 185 220 is this rig's "
                         "healthy band and gives the clean eps.")
    args = ap.parse_args(argv)

    paths = [p for p in args.logs if os.path.exists(p)]
    if not paths:
        print("no logs found: %s" % args.logs, file=sys.stderr)
        return 2
    records, nometer, nometer_rel, notowned = parse_logs(paths)
    if args.min_day:
        cut = datetime.fromisoformat(args.min_day).replace(tzinfo=timezone.utc).timestamp()
        records = [r for r in records if r["t_utc"] >= cut]
    if args.v_band:
        lo, hi = args.v_band
        before = len(records)
        records = [r for r in records if lo <= r["v_ms"] <= hi]
        print("V band %.0f..%.0f ms: kept %d of %d shots" % (lo, hi, len(records), before))
    grades = parse_grades(args.grades) if args.grades else []
    joined = join_grades(records, grades) if grades else 0
    if grades:
        print("panel_grade rows: %d, joined to a release: %d" % (len(grades), joined))

    os.makedirs(args.out, exist_ok=True)
    summary_path = os.path.join(args.out, "nometer_prior_summary.txt")

    class _Tee:
        def __init__(self, *s): self.s = s
        def write(self, x):
            for f in self.s:
                f.write(x)
        def flush(self):
            for f in self.s:
                f.flush()
    with open(summary_path, "w", encoding="utf-8") as fh:
        rows = summarise(records, nometer, nometer_rel, notowned,
                         out=_Tee(sys.stdout, fh), green_words=tuple(args.green_words))

    per_type = os.path.join(args.out, "nometer_prior_by_type.csv")
    with open(per_type, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    shot_fields = ["t_utc", "release_utc", "epoch", "seq", "shot_type", "mode", "press_wall_ms",
                   "command_issued_ms",
                   "hold_ms", "hold_utc_ms", "ptt_ms", "tip_seen_ms", "eps_ms", "v_ms", "v_src",
                   "lead_ms", "lead_source", "confidence", "accepted", "stop_confirmed",
                   "applied_delay_ms", "grade", "grade_session", "land_peak_fill",
                   "land_fill_at_rel", "land_green_start", "land_green_end", "grade_lag_s"]
    per_shot = os.path.join(args.out, "nometer_prior_shots.csv")
    with open(per_shot, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=shot_fields, extrasaction="ignore")
        w.writeheader()
        for r in records:
            w.writerow({k: r.get(k, "") for k in shot_fields})

    nm_path = os.path.join(args.out, "nometer_arms.csv")
    if nometer:
        with open(nm_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(nometer[0].keys()))
            w.writeheader()
            w.writerows(nometer)
    print("\nwrote %s\n      %s\n      %s%s"
          % (summary_path, per_type, per_shot, ("\n      " + nm_path) if nometer else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
