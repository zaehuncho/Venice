"""Epoch-level shot table + variance categorisation from one day of orion_native.log.

    python tools/timing/epoch_table.py --day 2026-09-01 [--log logs/orion_native.log] [--csv out.csv]

WHY. "The bot is early" is not a diagnosis. Before any timing constant moves, every released
shot has to be joined across its own log lines so the residual can be split into the
categories the fix actually depends on:

  (a) bias                  -- median landing vs the visible green window / tip
  (b) estimator noise       -- does the COMMAND phase (fill at release) predict the landing?
  (c) anchor quantisation   -- rung-to-rung witness spread of the phase anchor (c20/c25/c30/c35)
  (d) command/PIPE variance -- scheduler lateness (deltaMs) and hook ack wait, and their
                               correlation with the landing
  (e) post-command jitter   -- landing spread that is NOT explained by (b): identical command
                               phase, different freeze
  (f) identity churn        -- schedule/delivery token mismatches, every no-fire terminal and why

THE ONE NUMBER THAT MATTERS FOR THE SPLIT is corr(fill_at_rel, peak_fill) within a shot type.
If the command is issued at the same animation phase every time, fill_at_rel is ~constant and
the landing spread is downstream of the PC (transport / console registration / instrument).
If the landing tracks fill_at_rel with slope ~1, the estimator is the source.

INSTRUMENT CAVEATS (read before trusting any single row):
  * peak_fill is the fill where the meter FROZE, and the freeze rides the release (the game
    freezes the meter when it registers the press). It is the landing, not the tip.
  * green_start / green_end are per-shot reads of the green paint; their per-shot spread
    (~1.5-2.3 pp rMAD on 2026-09-01) is as large as the landing spread, so per-shot EARLY/GREEN
    verdicts from the log are +/-2 pp uncertain. Human-observed banners are the outcome
    authority; this tool is for attribution, not for claiming a green rate.
  * e_bottom_ms, e_tip_ms, win_ms, and the shift sweep use vel_at_rel as a LOCAL linear
    sensitivity only. The meter decelerates near the tip, so those values are not a calibrated
    pp-to-ms conversion and must not select a Shot Lead without a banner-counted A/B.
  * "PHASE SAMPLE" (anchor->freeze) cancels anchor noise by construction (fire = anchor + C),
    so its spread bounds latency + freeze-detection jitter only.

WHAT IT READS (append-only key=value lines; unknown fields ignored): Physical shot epoch,
Evidence-backed ownership, TIP PHASE ANCHOR CONSENSUS, PRECISE FIRE RETARGET, TIP TOKEN KILL /
HELD, TIP PHASE IMMINENT HOLD, TIP RESERVATION (promoted), Release issued / delivery identity /
timing / Scheduled fire / submit / landing, PHASE SAMPLE, Outcome identity, Shot abort identity,
SHOT NOT OWNED, DEV FIRE OFFSET DRAW / APPLIED, and the sidecar's "latency observation" lines.
Sessions split on a >600 s gap between releases
(release seq restarts per session). Shots need green_obs_n>=3, green_start>0 and a physical
vel_at_rel to be graded.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import re
import statistics as st
import sys
from typing import Iterable

NAN = float("nan")
_KV = re.compile(r"(\w+)=(-?[\w.+:/()]+)")
_SHOT = re.compile(r"shot=(.+?)\s*$")


def kv(line: str) -> dict:
    return dict(_KV.findall(line))


def fnum(d: dict, key: str, default: float = NAN) -> float:
    try:
        return float(d[key])
    except (KeyError, ValueError, TypeError):
        return default


def parse_ts(line: str) -> dt.datetime:
    return dt.datetime.strptime(line[:23], "%Y-%m-%dT%H:%M:%S.%f")


def clean(xs: Iterable) -> list:
    return [v for v in xs if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v))]


def med(xs) -> float:
    xs = clean(xs)
    return st.median(xs) if xs else NAN


def sd(xs) -> float:
    xs = clean(xs)
    return st.pstdev(xs) if len(xs) > 1 else NAN


def mad(xs) -> float:
    """Robust sd: 1.4826 * median absolute deviation."""
    xs = clean(xs)
    if not xs:
        return NAN
    m = st.median(xs)
    return 1.4826 * st.median([abs(v - m) for v in xs])


def pct(xs, p: float) -> float:
    xs = sorted(clean(xs))
    if not xs:
        return NAN
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def corr(xs, ys):
    pairs = [(a, b) for a, b in zip(xs, ys)
             if isinstance(a, (int, float)) and isinstance(b, (int, float))
             and not math.isnan(a) and not math.isnan(b)]
    if len(pairs) < 8:
        return NAN, len(pairs)
    x = [a for a, _ in pairs]
    y = [b for _, b in pairs]
    mx, my = st.mean(x), st.mean(y)
    sx, sy = st.pstdev(x), st.pstdev(y)
    if sx == 0 or sy == 0:
        return NAN, len(pairs)
    return sum((a - mx) * (b - my) for a, b in pairs) / (len(pairs) * sx * sy), len(pairs)


def slope(xs, ys):
    """OLS slope of y on x and the residual sd."""
    pairs = [(a, b) for a, b in zip(xs, ys) if not math.isnan(a) and not math.isnan(b)]
    if len(pairs) < 8:
        return NAN, NAN
    x = [a for a, _ in pairs]
    y = [b for _, b in pairs]
    mx, my = st.mean(x), st.mean(y)
    sxx = sum((a - mx) ** 2 for a in x)
    if sxx == 0:
        return NAN, NAN
    b = sum((a - mx) * (c - my) for a, c in pairs) / sxx
    res = [c - (my + b * (a - mx)) for a, c in pairs]
    return b, st.pstdev(res)


def parse(lines: Iterable[str], day: str, session_gap_s: float = 600.0):
    """Join one day's lines into released shots and no-fire terminal events.

    Returns (graded, no_fires, released). `released` has every "Release landing" row;
    `graded` is the subset with a usable green window and velocity. `no_fires`
    includes both owned-shot aborts and presses that timed out before ownership.
    """
    released = []
    cur: dict[int, dict] = {}
    by_epoch: dict[int, dict] = {}
    pending_phase = None
    last_epoch = None
    epoch_info: dict[int, dict] = {}
    no_fires = []
    pending_not_owned: dict[int, dict] = {}
    last_release_time = None
    session = 0
    dev_offset_draw_by_attempt: dict[int, float] = {}
    dev_offset_applied_by_attempt: dict[int, float] = {}

    for ln in lines:
        if not ln.startswith(day):
            continue
        try:
            t = parse_ts(ln)
        except ValueError:
            continue
        if "Physical shot epoch:" in ln:
            d = kv(ln)
            last_epoch = int(fnum(d, "epoch", 0))
            epoch_info[last_epoch] = dict(t_press=t, consensus=[], retargets=[], kills=0, held=0,
                                          imminent_hold_ms=NAN, own_first_fill=NAN, own_press_age=NAN)
        elif "Evidence-backed ownership:" in ln and last_epoch in epoch_info:
            d = kv(ln)
            epoch_info[last_epoch].update(own_first_fill=fnum(d, "first_fill"),
                                          own_press_age=fnum(d, "press_age"), own_t=t)
        elif "TIP PHASE ANCHOR CONSENSUS:" in ln and last_epoch in epoch_info:
            d = kv(ln)
            epoch_info[last_epoch]["consensus"].append(dict(
                disp=d.get("disposition"), stage=int(fnum(d, "stage", 0)), corr=fnum(d, "correction_ms"),
                rng=fnum(d, "witness_range_ms"), w20=fnum(d, "w20_ms"), w25=fnum(d, "w25_ms"),
                w30=fnum(d, "w30_ms"), w35=fnum(d, "w35_ms")))
        elif "PRECISE FIRE RETARGET:" in ln and last_epoch in epoch_info:
            d = kv(ln)
            epoch_info[last_epoch]["retargets"].append(dict(disp=d.get("disposition"),
                                                            stage=int(fnum(d, "stage", 0))))
        elif "TIP TOKEN KILL:" in ln and last_epoch in epoch_info:
            epoch_info[last_epoch]["kills"] += 1
        elif "TIP TOKEN HELD:" in ln and last_epoch in epoch_info:
            epoch_info[last_epoch]["held"] += 1
        elif "TIP PHASE IMMINENT HOLD: site=subtick" in ln and last_epoch in epoch_info:
            d = kv(ln)
            epoch_info[last_epoch].update(imminent_hold_ms=fnum(d, "held_ms"),
                                          imminent_fill=fnum(d, "fill_pct"))
        elif "TIP RESERVATION: disposition=reservation_promoted" in ln:
            d = kv(ln)
            ep = int(fnum(d, "physical_epoch", 0))
            if ep in epoch_info:
                epoch_info[ep].update(arm_source=d.get("source"), command_eta=fnum(d, "command_eta_ms"),
                                      sigma=fnum(d, "predictor_sigma_ms"), arm_fill=fnum(d, "fill_pct"),
                                      first_fill=fnum(d, "first_fill"), t_arm=t,
                                      lead_kind=d.get("lead_kind"), lead_ms=fnum(d, "lead_ms"))
        elif "DEV FIRE OFFSET DRAW:" in ln:
            d = kv(ln)
            attempt = int(fnum(d, "shot_attempt", -1))
            if attempt >= 0:
                dev_offset_draw_by_attempt[attempt] = fnum(d, "offset_ms")
        elif "DEV FIRE OFFSET APPLIED:" in ln:
            d = kv(ln)
            attempt = int(fnum(d, "shot_attempt", -1))
            if attempt >= 0:
                dev_offset_applied_by_attempt[attempt] = fnum(d, "applied_ms")
        elif "Release issued:" in ln:
            d = kv(ln)
            if last_release_time is None or (t - last_release_time).total_seconds() > session_gap_s:
                session += 1
            last_release_time = t
            seq = int(fnum(d, "seq", 0))
            m = _SHOT.search(ln)
            cur[seq] = dict(session=session, seq=seq, t_rel=t, shot=m.group(1).strip() if m else "?",
                            rel_age_ms=fnum(d, "age"))
        elif "Release delivery identity:" in ln:
            d = kv(ln)
            s = cur.get(int(fnum(d, "release_seq", -1)))
            if s is not None:
                ep = int(fnum(d, "physical_epoch", 0))
                attempt = int(fnum(d, "shot_attempt", -1))
                s["epoch"] = ep
                s["shot_attempt"] = attempt
                s["sched_token"] = int(fnum(d, "schedule_token", 0))
                s["deliv_token"] = int(fnum(d, "delivery_token", 0))
                s["deliv_stage"] = d.get("delivery_stage")
                if attempt in dev_offset_draw_by_attempt:
                    s["dev_offset_draw_ms"] = dev_offset_draw_by_attempt[attempt]
                    s["dev_offset_applied_ms"] = dev_offset_applied_by_attempt.get(
                        attempt, 0.0 if dev_offset_draw_by_attempt[attempt] == 0.0 else NAN)
                by_epoch[ep] = s
                s.update(epoch_info.get(ep, {}))
        elif "Release timing:" in ln:
            d = kv(ln)
            s = cur.get(int(fnum(d, "seq", -1)))
            if s:
                s.update(holdToRelMs=fnum(d, "holdToRelMs"), anchorAppearMs=fnum(d, "anchorAppearMs"))
        elif "Scheduled fire:" in ln:
            d = kv(ln)
            s = cur.get(int(fnum(d, "seq", -1)))
            if s:
                s.update(deltaMs=fnum(d, "deltaMs"))
        elif "Release submit:" in ln:
            d = kv(ln)
            s = cur.get(int(fnum(d, "seq", -1)))
            if s:
                s.update(hook_ack_wait_us=fnum(d, "hook_ack_wait_us"), hook_write_us=fnum(d, "hook_write_us"))
        elif "PHASE SAMPLE:" in ln:
            d = kv(ln)
            pending_phase = dict(phase_raw=fnum(d, "raw_ms"), phase_norm=fnum(d, "normalized_ms"),
                                 phase_acc=int(fnum(d, "accepted", 0)), anchor_pct=fnum(d, "anchor_pct"),
                                 phase_const_eff=fnum(d, "effective_const_ms"))
        elif "Release landing:" in ln:
            d = kv(ln)
            s = cur.get(int(fnum(d, "seq", -1)))
            if s:
                s.update(peak=fnum(d, "peak_fill"), settled=fnum(d, "settled_fill"), far=fnum(d, "fill_at_rel"),
                         travel=fnum(d, "travel_pp"), gs=fnum(d, "green_start"), ge=fnum(d, "green_end"),
                         gobs=int(fnum(d, "green_obs_n", 0)), vel=fnum(d, "vel_at_rel"),
                         frame_age=fnum(d, "frame_age_ms"), t_land=t,
                         # [ORION_RAW_TOP] raw (coarse-or-subpixel max) top and its dwell; absent on old logs
                         raw_peak=fnum(d, "raw_peak_fill"), top_hold_ms=fnum(d, "top_hold_ms"))
                if pending_phase:
                    s.update(pending_phase)
                    pending_phase = None
                released.append(s)
        elif "Outcome identity:" in ln:
            d = kv(ln)
            s = by_epoch.get(int(fnum(d, "physical_epoch", 0)))
            if s:
                s.update(armed_source=d.get("armed_source"), armed_eta=fnum(d, "command_eta_ms"),
                         armed_fill=fnum(d, "armed_fill_pct"), armed_sigma=fnum(d, "armed_sigma_ms"),
                         armed_token=int(fnum(d, "armed_schedule_token", 0)))
        elif "latency observation:" in ln:
            d = kv(ln)
            # Join at the observation's position in the stream, while ``cur``
            # still identifies the release from this process/session generation.
            # Sequence numbers restart at 1 after a sidecar/session restart.  A
            # post-pass that paired equal sequence numbers by list index could
            # therefore shift every later session when one earlier observation
            # was absent from a truncated/dropped log.  Missing evidence must
            # stay missing, never inherit another session's timing sample.
            s = cur.get(int(fnum(d, "seq", -1)))
            if s is not None and t >= s["t_rel"]:
                s["lat_total_ms"] = fnum(d, "total_ms")
                s["lat_accepted"] = int(fnum(d, "accepted", 0))
        elif "Shot abort identity:" in ln:
            d = kv(ln)
            ep = int(fnum(d, "physical_epoch", -1))
            info = epoch_info.get(ep, {})
            # Some owned abort paths emit SHOT NOT OWNED immediately before the canonical
            # Shot abort identity for the same physical epoch. They describe one press, not
            # two. Keep the richer canonical terminal and remove only a nearby pending row;
            # epochs restart across sessions, so an unbounded epoch-only de-dup is unsafe.
            pending = pending_not_owned.get(ep)
            if pending is not None and (t - pending["t"]).total_seconds() <= 2.0:
                no_fires.remove(pending)
                del pending_not_owned[ep]
            attempt = int(fnum(d, "shot_attempt", -1))
            no_fires.append(dict(t=t, epoch=ep, shot_attempt=attempt,
                                 terminal_kind="owned_abort",
                                 reason=d.get("reason"), shot_type=d.get("shot_type"),
                                 own_first_fill=info.get("own_first_fill", NAN),
                                 own_press_age=info.get("own_press_age", NAN),
                                 ever_armed=("arm_source" in info), arm_source=info.get("arm_source"),
                                 command_eta=info.get("command_eta", NAN), kills=info.get("kills", 0),
                                 dev_offset_draw_ms=dev_offset_draw_by_attempt.get(attempt, NAN),
                                 dev_offset_applied_ms=dev_offset_applied_by_attempt.get(attempt, NAN),
                                 imminent_hold_ms=info.get("imminent_hold_ms", NAN)))
        elif "SHOT NOT OWNED:" in ln:
            # This is a terminal user press too. The older grader silently omitted it because
            # no Shot abort identity is emitted before ownership, understating the 2026-09-01
            # current-log no-fire rate from 63/399 to 43/379.
            d = kv(ln)
            ep = int(fnum(d, "physical_epoch", -1))
            info = epoch_info.get(ep, {})
            event = dict(t=t, epoch=ep, terminal_kind="not_owned",
                         reason=d.get("reason"), shot_type=d.get("shot_type"),
                         hold_ms=fnum(d, "hold_ms"),
                         own_first_fill=info.get("own_first_fill", NAN),
                         own_press_age=info.get("own_press_age", NAN),
                         ever_armed=("arm_source" in info), arm_source=info.get("arm_source"),
                         command_eta=info.get("command_eta", NAN), kills=info.get("kills", 0),
                         imminent_hold_ms=info.get("imminent_hold_ms", NAN))
            no_fires.append(event)
            pending_not_owned[ep] = event

    graded = [s for s in released
              if s.get("gobs", 0) >= 3 and s.get("gs", 0) > 0 and s.get("vel", 0) > 0.05]
    for s in graded:
        v = s["vel"]
        s["e_bottom_ms"] = (s["peak"] - s["gs"]) / v
        s["e_tip_ms"] = (s["peak"] - s["ge"]) / v
        s["win_ms"] = (s["ge"] - s["gs"]) / v
        s["grade"] = "EARLY" if s["peak"] < s["gs"] else ("LATE" if s["peak"] > s["ge"] else "GREEN")
        c30 = [c for c in s.get("consensus", []) if c["stage"] == 30]
        s["c30_disp"] = c30[-1]["disp"] if c30 else "none"
        s["c30_corr"] = c30[-1]["corr"] if c30 else NAN
        s["c30_rng"] = c30[-1]["rng"] if c30 else NAN
        if c30:
            # Runtime telemetry uses -1.0 (not NaN) for an anchor witness that
            # does not exist.  Treat each adjacent pair independently: the old
            # NaN-only check subtracted -1 from a process-monotonic timestamp
            # and printed phase intervals hundreds of seconds long.  Besides
            # corrupting the report, that made a missing 20% witness suppress a
            # perfectly valid 25->30 interval (and vice versa).
            witness = c30[-1]

            def valid_witness(name: str) -> bool:
                value = witness.get(name, NAN)
                return math.isfinite(value) and value >= 0.0

            if valid_witness("w20") and valid_witness("w25"):
                s["d25_20"] = witness["w25"] - witness["w20"]
            if valid_witness("w25") and valid_witness("w30"):
                s["d30_25"] = witness["w30"] - witness["w25"]
        rt = [r for r in s.get("retargets", []) if r["disp"] == "retargeted"]
        s["retargeted30"] = any(r["stage"] == 30 for r in rt)
        s["retargeted35"] = any(r["stage"] == 35 for r in rt)
        if "own_t" in s:
            s["own_to_rel_ms"] = (s["t_rel"] - s["own_t"]).total_seconds() * 1000.0
    return graded, no_fires, released


CSV_COLS = ["session", "seq", "epoch", "shot_attempt", "shot", "grade", "peak", "gs", "ge", "e_bottom_ms", "e_tip_ms", "win_ms",
            "vel", "far", "travel", "first_fill", "own_first_fill", "own_press_age", "armed_source", "armed_fill",
            "armed_eta", "armed_sigma", "sched_token", "deliv_token", "deliv_stage", "holdToRelMs",
            "own_to_rel_ms", "deltaMs", "hook_ack_wait_us", "frame_age", "rel_age_ms", "phase_norm", "phase_acc",
            "anchor_pct", "phase_const_eff", "c30_disp", "c30_corr", "c30_rng", "d25_20", "d30_25",
            "retargeted30", "retargeted35", "imminent_hold_ms", "kills", "held", "lat_total_ms", "lat_accepted", "lead_kind",
            "lead_ms", "dev_offset_draw_ms", "dev_offset_applied_ms"]


def _row(label: str, rows: list) -> str:
    n = len(rows)
    if n == 0:
        return f"  {label:28s} n=  0"
    g = sum(1 for s in rows if s["grade"] == "GREEN")
    e = sum(1 for s in rows if s["grade"] == "EARLY")
    la = n - g - e
    pk = [s["peak"] for s in rows]
    raw = [s.get("raw_peak", NAN) for s in rows]
    raw_n = len(clean(raw))
    raw_top = (f" | raw>=99 {100 * sum(1 for v in clean(raw) if v >= 99.0) / raw_n:4.0f}% (n={raw_n})"
               if raw_n else "")
    return (f"  {label:28s} n={n:3d} green={100 * g / n:5.1f}% early={100 * e / n:5.1f}% late={100 * la / n:4.1f}% | "
            f"e_bottom med={med([s['e_bottom_ms'] for s in rows]):6.1f} rmad={mad([s['e_bottom_ms'] for s in rows]):5.1f} | "
            f"e_tip med={med([s['e_tip_ms'] for s in rows]):6.1f} | peak med={med(pk):5.2f} sd={sd(pk):4.2f} rmad={mad(pk):4.2f} | "
            f"gs med={med([s['gs'] for s in rows]):5.2f} ge med={med([s['ge'] for s in rows]):5.2f} win={med([s['win_ms'] for s in rows]):4.1f}ms" + raw_top)


def report(graded: list, no_fires: list, released: list, out=None) -> None:
    # Resolve stdout at call time (pytest's capsys swaps it after import).
    p = lambda *a: print(*a, file=out if out is not None else sys.stdout)  # noqa: E731
    types = sorted({s["shot"] for s in graded})
    sessions = sorted({s["session"] for s in graded})
    owned_abort_n = sum(1 for event in no_fires if event.get("terminal_kind") == "owned_abort")
    not_owned_n = sum(1 for event in no_fires if event.get("terminal_kind") == "not_owned")
    p(f"{len(released)} released, {len(graded)} graded, {len(no_fires)} no-fire terminals "
      f"({owned_abort_n} owned aborts, {not_owned_n} not-owned)")
    p("\n== (a) BIAS: landing vs window, per session / type (log-graded; +/-2 pp instrument) ==")
    for sid in sessions:
        rows = [s for s in graded if s["session"] == sid]
        p(_row(f"session {sid} {str(rows[0]['t_rel'])[11:16]}", rows))
    for t in types:
        p(_row(t, [s for s in graded if s["shot"] == t]))
    for sid in sessions:
        for t in types:
            rows = [s for s in graded if s["session"] == sid and s["shot"] == t]
            if len(rows) >= 8:
                p(_row(f"  s{sid} {t}", rows))

    p("\n== (b) ESTIMATOR vs (e) POST-COMMAND: does the command phase (fill at release) predict the landing? ==")
    p(f"  {'group':22s} {'n':>3s} {'rmad far':>8s} {'rmad peak':>9s} {'rmad trav':>9s} {'corr':>6s} {'slope':>6s} {'resid':>6s}")
    def grp(label, rows):
        far = [s["far"] for s in rows]
        pk = [s["peak"] for s in rows]
        tr = [s["travel"] for s in rows]
        b, res = slope(far, pk)
        p(f"  {label:22s} {len(rows):3d} {mad(far):8.2f} {mad(pk):9.2f} {mad(tr):9.2f} {corr(far, pk)[0]:6.2f} {b:6.2f} {res:6.2f}")
    grp("ALL", graded)
    for t in types:
        grp(t, [s for s in graded if s["shot"] == t])
    for sid in sessions:
        for t in types:
            rows = [s for s in graded if s["session"] == sid and s["shot"] == t]
            if len(rows) >= 8:
                grp(f"s{sid} {t}", rows)
    p("  (interpret each group: near-zero slope is consistent with downstream/instrument spread; "
      "a material slope retains upstream/estimator sensitivity)")

    p("\n== (c) ANCHOR QUANTISATION: rung-to-rung normalised witness deltas (ms) and c30 corrections ==")
    for key in ["d25_20", "d30_25", "c30_rng", "c30_corr"]:
        x = [s.get(key, NAN) for s in graded]
        p(f"  {key:8s} n={len(clean(x)):3d} med={med(x):6.2f} rmad={mad(x):5.2f} p10={pct(x, .1):6.1f} p90={pct(x, .9):6.1f} "
          f"corr(e_bottom)={corr(x, [s['e_bottom_ms'] for s in graded])[0]:6.3f}")
    for disp in sorted({s["c30_disp"] for s in graded}):
        p(_row(f"c30 {disp}", [s for s in graded if s["c30_disp"] == disp]))

    p("\n== (d) COMMAND PATH: scheduler lateness and hook ack ==")
    dm = [s.get("deltaMs", NAN) for s in graded]
    ack = [s.get("hook_ack_wait_us", NAN) for s in graded]
    p(f"  deltaMs med={med(dm):.2f} p90={pct(dm, .9):.2f} max={max(clean(dm)) if clean(dm) else NAN:.2f} corr(e_bottom)={corr(dm, [s['e_bottom_ms'] for s in graded])[0]:.3f}")
    p(f"  hook_ack_wait_us med={med(ack):.0f} p90={pct(ack, .9):.0f} corr(e_bottom)={corr(ack, [s['e_bottom_ms'] for s in graded])[0]:.3f}")
    accepted_lat_rows = [s for s in graded
                         if s.get("lat_accepted") == 1 and not math.isnan(s.get("lat_total_ms", NAN))]
    rejected_lat_rows = [s for s in graded
                         if s.get("lat_accepted") == 0 and not math.isnan(s.get("lat_total_ms", NAN))]
    for label, rows in (("accepted", accepted_lat_rows), ("rejected", rejected_lat_rows)):
        lat = [s["lat_total_ms"] for s in rows]
        peaks = [s["peak"] for s in rows]
        p(f"  sidecar command->freeze ({label}): n={len(lat)} med={med(lat):.1f} "
          f"rmad={mad(lat):.1f} corr(peak)={corr(lat, peaks)[0]:.3f}")
    pn = [s.get("phase_norm", NAN) for s in graded if s.get("phase_acc") == 1]
    p(f"  PHASE SAMPLE anchor->freeze (anchor noise cancels): n={len(clean(pn))} med={med(pn):.1f} rmad={mad(pn):.1f}")

    # The dev hook is a randomized final-deadline treatment. Report it in physical milliseconds
    # directly; do not convert the peak shift using command-time meter velocity. Human banners
    # remain the outcome endpoint, while PHASE SAMPLE is the primary mechanism check.
    offset_rows = [s for s in graded if not math.isnan(s.get("dev_offset_draw_ms", NAN))]
    if offset_rows:
        p("\n== DEV DEADLINE A/B: assigned final-deadline offset (human banners remain authoritative) ==")
        for offset in sorted({s["dev_offset_draw_ms"] for s in offset_rows}):
            rows = [s for s in offset_rows if s["dev_offset_draw_ms"] == offset]
            phase = [s.get("phase_norm", NAN) for s in rows if s.get("phase_acc") == 1]
            p(f"  offset={offset:+.2f}ms n={len(rows):3d} phase_n={len(clean(phase)):3d} "
              f"phase_med={med(phase):6.2f}ms phase_rmad={mad(phase):5.2f}ms "
              f"peak_med={med([s['peak'] for s in rows]):5.2f}pp")

    p("\n== (f) IDENTITY: tokens and every no-fire terminal ==")
    p(f"  sched!=deliv token: {sum(1 for s in released if s.get('sched_token') != s.get('deliv_token'))}  "
      f"armed!=sched token: {sum(1 for s in released if s.get('armed_token') not in (None, 0) and s.get('armed_token') != s.get('sched_token'))}  "
      f"delivery stages: {sorted({str(s.get('deliv_stage')) for s in released})}")
    reasons = sorted({str(a["reason"]) for a in no_fires})
    p("  no-fire terminals by reason: "
      + ", ".join(f"{r}={sum(1 for a in no_fires if str(a['reason']) == r)}" for r in reasons))
    dmiss = [a for a in no_fires if a["reason"] == "live_tip_deadline_missed"]
    ff = [a["own_first_fill"] for a in dmiss]
    p(f"  deadline_missed n={len(dmiss)} ever_armed={sum(1 for a in dmiss if a['ever_armed'])} "
      f"first_fill med={med(ff):.1f} p10={pct(ff, .1):.1f} p90={pct(ff, .9):.1f}  "
      f"(released first_fill med={med([s.get('own_first_fill', NAN) for s in released]):.1f} p90={pct([s.get('own_first_fill', NAN) for s in released], .9):.1f})")
    attempts = len(released) + len(no_fires)
    if attempts:
        p(f"  all no-fire share: {len(no_fires)}/{attempts} = {100 * len(no_fires) / attempts:.1f}%")
        p(f"  deadline-missed share: {len(dmiss)}/{attempts} = {100 * len(dmiss) / attempts:.1f}%")

    p("\n== LOCAL-LINEAR SENSITIVITY (NOT A TIMING PREDICTION): peak' = peak + delta*vel_at_rel ==")
    p(f"  {'delta':>6s} | " + " | ".join(f"{t[:12]:>12s}" for t in types) + " |    ALL   late")
    for delta in range(-10, 35, 5):
        cells = []
        ga = la = n = 0
        for t in types:
            rows = [s for s in graded if s["shot"] == t]
            g = sum(1 for s in rows if s["gs"] <= s["peak"] + delta * s["vel"] <= s["ge"])
            l = sum(1 for s in rows if s["peak"] + delta * s["vel"] > s["ge"])
            cells.append(f"{100 * g / len(rows) if rows else NAN:11.1f}%")
            ga += g
            la += l
            n += len(rows)
        p(f"  {delta:+5d}ms | " + " | ".join(cells) + f" | {100 * ga / n if n else NAN:5.1f}% {100 * la / n if n else NAN:5.1f}%")
    p("  WARNING: near-tip deceleration makes vel_at_rel an invalid global pp-to-ms conversion; "
      "use this only for direction/sensitivity and choose lead from human-banner A/B data.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", required=True, help="ISO date prefix of the log lines, e.g. 2026-09-01")
    ap.add_argument("--log", default="logs/orion_native.log")
    ap.add_argument("--csv", default=None, help="write the per-shot table here")
    args = ap.parse_args(argv)
    with open(args.log, encoding="utf-8", errors="replace") as fh:
        graded, no_fires, released = parse(fh, args.day)
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(CSV_COLS)
            for s in graded:
                w.writerow([s.get(c, "") for c in CSV_COLS])
    report(graded, no_fires, released)
    return 0


if __name__ == "__main__":
    sys.exit(main())
