#!/usr/bin/env python3
"""Validate the tip-phase corroboration veto against observed meter stops.

Question this answers: on the deadline-missed aborts where the phase member
had a DATED anchor and a schedulable tip (phase_tip_eta - lead > 0) but the
same-tick decision selected the sampler (phase_primary=0) and rejected the
shot as missed -- was the phase estimate right, or was the sampler?

Method:
  1. Scan orion_native.log{,.1} for live_tip_deadline_missed aborts whose
     same-tick TIP RESERVATION carried phase_tip_eta_ms >= 0 while
     source is a sampler-chain member and phase_primary=0.
  2. For each, find the detframes CSV covering that wall-clock instant
     (wall_ms column, epoch ms) and extract the fed/fresh fill samples for
     the following 2.5s -- the meter keeps rising after the abort because
     the human's pass-through press completes the shot, and the meter's
     stop is animation-driven (release does not move the meter).
  3. Date the ACTUAL stop with the engine's own estimator constants
     (growth step 2.0pp re-opens, 100ms quiet confirms) and compare it with
     each member's predicted absolute tip.

err = predicted - actual: negative = member predicted the tip EARLY.
Offline stop reads ~9-10ms later than the engine's own stop detector
(TIMING_BUDGET_PICKUP_PROMPT.md); direction and tens-of-ms magnitude are
what this instrument is valid for.

NEVER uses session_20260804_032333 (two runs spliced).
Read-only; creates nothing outside stdout.
"""
from __future__ import annotations

import csv
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2}\.\d{3})Z\s+(.*)$")
SAMPLER_SOURCES = {"sampler", "sampler+registration_near",
                   "registration+sampler_far", "registration_far_disagreement",
                   "sampler_near_disagreement"}

def kv(line, key, cast=float, default=None):
    m = re.search(rf"{re.escape(key)}=([^\s]+)", line)
    if not m:
        return default
    try:
        return cast(m.group(1))
    except (TypeError, ValueError):
        return default

def parse_events(log_paths):
    events = []
    releases = []
    last_res = None
    for path in log_paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = TS_RE.match(line)
                if not m:
                    continue
                ts = datetime.strptime(
                    f"{m.group(1)}T{m.group(2)}", "%Y-%m-%dT%H:%M:%S.%f"
                ).replace(tzinfo=timezone.utc).timestamp()
                body = m.group(3)
                if body.startswith("TIP RESERVATION:") and "source=" in body:
                    last_res = dict(
                        ts=ts, source=kv(body, "source", str),
                        tip_eta=kv(body, "tip_eta_ms"),
                        phase_tip_eta=kv(body, "phase_tip_eta_ms"),
                        phase_primary=kv(body, "phase_primary", int),
                        phase_anchor_pct=kv(body, "phase_anchor_pct"),
                        smp_tip_eta=kv(body, "smp_tip_eta_ms"),
                        smp_sigma=kv(body, "smp_sigma_ms"),
                        lead=kv(body, "lead_ms"),
                        fill=kv(body, "fill_pct"))
                elif body.startswith("Release issued:"):
                    m2 = re.search(r"shot=(.+?)\s*$", body)
                    releases.append(dict(
                        wall_ms=ts * 1000.0, seq=kv(body, "seq", int),
                        shot=m2.group(1).strip().replace(" ", "_")
                        if m2 else None, target_mode=None))
                elif body.startswith("Release attribution:"):
                    seq = kv(body, "seq", int)
                    for rel in reversed(releases[-4:]):
                        if rel["seq"] == seq and ts * 1000.0 - rel["wall_ms"] < 5000:
                            rel["target_mode"] = kv(body, "targetMode", str)
                            break
                elif (body.startswith("Shot abort identity:")
                      and "reason=live_tip_deadline_missed" in body):
                    shot = kv(body, "shot_type", str)
                    r = last_res
                    if (r and (ts - r["ts"]) < 0.06
                            and r["source"] in SAMPLER_SOURCES
                            and r.get("phase_tip_eta") is not None
                            and r["phase_tip_eta"] >= 0
                            and (r.get("phase_primary") or 0) == 0):
                        events.append(dict(
                            abort_wall_ms=r["ts"] * 1000.0, shot=shot, **r))
    return events, releases

def index_csvs(diag_dir):
    out = []
    for p in sorted(diag_dir.glob("detframes_2026080[456]*.csv")):
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                rdr = csv.DictReader(fh)
                first = next(rdr, None)
                if not first or "wall_ms" not in first:
                    continue
                w0 = float(first["wall_ms"])
            with open(p, "rb") as fh:
                fh.seek(0, 2)
                sz = fh.tell()
                fh.seek(max(0, sz - 4096))
                tail = fh.read().decode("utf-8", errors="replace").splitlines()
            w1 = w0
            for ln in reversed(tail):
                parts = ln.split(",")
                if len(parts) > 14:
                    try:
                        w1 = float(parts[13])
                        break
                    except ValueError:
                        continue
            out.append((w0, w1, p))
        except (OSError, StopIteration):
            continue
    return out

def observed_stop(csv_path, t0_ms, t1_ms):
    """Engine-mirroring stop estimator over fed fresh samples in [t0,t1]."""
    samples = []
    with open(csv_path, encoding="utf-8", errors="replace") as fh:
        rdr = csv.DictReader(fh)
        for row in rdr:
            try:
                w = float(row["wall_ms"])
            except (KeyError, ValueError):
                continue
            if w < t0_ms - 500.0:
                continue
            if w > t1_ms:
                break
            try:
                fed = int(row["fed"])
                det = int(row["detected"])
                stale = int(row["stale"])
                fill = float(row["fill_pct"])
            except (KeyError, ValueError):
                continue
            if det == 1 and stale == 0 and (fed == 1 or fed == -1):
                samples.append((w, fill))
    if len(samples) < 4:
        return None, samples
    # Stop = last sample that grew the running max by >= 2.0pp, confirmed by
    # >= 100ms with no further such growth (engine constants step/quiet).
    run_max = samples[0][1]
    stop_w = samples[0][0]
    for w, fill in samples[1:]:
        if fill >= run_max + 2.0:
            run_max = fill
            stop_w = w
    confirm = any(w - stop_w >= 100.0 for w, _ in samples)
    if not confirm:
        return None, samples
    return (stop_w, run_max), samples

def main():
    root = Path(__file__).resolve().parents[2]
    logs = [root / "logs" / "orion_native.log.1",
            root / "logs" / "orion_native.log"]
    events, releases = parse_events(logs)
    csvs = index_csvs(root / "logs" / "diagnostics")
    print(f"vetoed-but-dated deadline-miss aborts found: {len(events)}")
    print(f"detframes csvs indexed: {len(csvs)}")

    # BASELINE: successful phase-timed releases graded the same way. The
    # release command is submitted at predicted_tip - lead, so
    # (release_wall + lead) is the fired member's own absolute tip claim.
    # err vs the offline observed stop for these SUCCESSFUL shots defines
    # what a healthy prediction reads on this instrument (the phase member
    # carries a deliberate aim-preservation offset, so the healthy value is
    # NOT zero).
    LEAD_MS = 300.0
    base_errs = {}
    for rel in releases:
        if rel["target_mode"] != "meter_tip_phase" or not rel["shot"]:
            continue
        t = rel["wall_ms"]
        cover = [p for (w0, w1, p) in csvs if w0 - 1000 <= t <= w1 + 1000]
        if not cover:
            continue
        res, _ = observed_stop(cover[0], t, t + 1500.0)
        if res is None:
            continue
        stop_w, plateau = res
        if plateau < 85.0:
            continue
        base_errs.setdefault(rel["shot"], []).append((t + LEAD_MS) - stop_w)
    print("\nbaseline: (release_wall + lead) - observed_stop on SUCCESSFUL "
          "phase-timed releases")
    for shot, errs in sorted(base_errs.items()):
        print(f"  {shot:<12} n={len(errs):>3} median={statistics.median(errs):+7.1f}ms"
              f"  sd={statistics.stdev(errs) if len(errs) > 1 else float('nan'):6.1f}")
    print()
    phase_errs, smp_errs = [], []
    for ev in events:
        t = ev["abort_wall_ms"]
        stamp = datetime.fromtimestamp(t / 1000.0, tz=timezone.utc)
        cover = [p for (w0, w1, p) in csvs if w0 - 1000 <= t <= w1 + 1000]
        if not cover:
            print(f"{stamp:%m-%d %H:%M:%S} {ev['shot']:<7} rung="
                  f"{ev['phase_anchor_pct']:.0f} phase_eta="
                  f"{ev['phase_tip_eta']:6.1f} smp_eta={ev['smp_tip_eta']:6.1f}"
                  f"  -- no csv coverage")
            continue
        res, samples = observed_stop(cover[0], t, t + 2500.0)
        if res is None:
            print(f"{stamp:%m-%d %H:%M:%S} {ev['shot']:<7} rung="
                  f"{ev['phase_anchor_pct']:.0f} phase_eta="
                  f"{ev['phase_tip_eta']:6.1f} smp_eta={ev['smp_tip_eta']:6.1f}"
                  f"  -- csv={cover[0].name} n={len(samples)} no confirmed stop")
            continue
        stop_w, plateau = res
        phase_err = (t + ev["phase_tip_eta"]) - stop_w
        smp_err = (t + ev["smp_tip_eta"]) - stop_w
        phase_errs.append(phase_err)
        smp_errs.append(smp_err)
        print(f"{stamp:%m-%d %H:%M:%S} {ev['shot']:<7} rung="
              f"{ev['phase_anchor_pct']:.0f} fill={ev['fill']:5.1f} "
              f"plateau={plateau:5.1f} phase_err={phase_err:+7.1f}ms "
              f"smp_err={smp_err:+7.1f}ms  csv={cover[0].name}")
    if phase_errs:
        print()
        print(f"n graded: {len(phase_errs)}")
        print(f"phase member:   median err {statistics.median(phase_errs):+7.1f}ms"
              f"   median |err| {statistics.median([abs(x) for x in phase_errs]):6.1f}ms")
        print(f"sampler member: median err {statistics.median(smp_errs):+7.1f}ms"
              f"   median |err| {statistics.median([abs(x) for x in smp_errs]):6.1f}ms")
        print("\nerr = predicted_tip - observed_stop; negative = predicted "
              "EARLY. Offline stop reads ~9-10ms late vs the engine's own "
              "stop, so subtract ~10ms from both for absolute reading; the "
              "COMPARISON between members is unaffected.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
