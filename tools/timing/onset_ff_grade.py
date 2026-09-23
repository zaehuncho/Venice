#!/usr/bin/env python
"""onset_ff_grade.py -- grade the per-shot onset feedforward against the game's own banner.

    python tools/timing/onset_ff_grade.py --records SESSION.jsonl [SESSION2.jsonl ...]
                                          [--logs logs/orion_native.log.1 logs/orion_native.log]
                                          [--since 2026-09-21T20:00] [--type Standstill]

Session JSONL supplies the attributed banner, onset, type and explicit observation interval.
Every log join requires its physical epoch AND exactly one matching record interval. Reused
epochs across launches and ambiguous overlapping sessions never share a measurement:
  * `ONSET FF: reason=... dev_ms=... applied_ms=... physical_epoch=E`  -- the engine's own
    deviation from its per-bucket reference and what it applied at the arm
  * `Release onsetff: seq=S applied_ms=A ... physical_epoch=E`  -- the displacement the release
    actually carried (0 for an in-tick release), the value the learner fences key on
Prints per-session descriptive splits using only the engine's observed deviation and release
displacement. Missing displacement is UNKNOWN, not a control shot; missing deviation is not
replaced with a hindsight median. These observational groups are confounded by the correction's
selection rule and are NOT a causal before/after estimate. Use a controlled live comparison
before changing gain. Court jitter is reported only when the record stamps a ready sampler.
HARD BOUNDARY: analysis tooling only; never imported by the engine, the orchestrator or the
sidecar; a banner read must never reach the engine.
"""
from __future__ import annotations

import argparse
import collections
from datetime import datetime, timezone
import io
import json
import math
import re
import statistics as st
from pathlib import Path

TS = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{3})Z")
KV = re.compile(r"(\w+)=([\-\w.+]+)")
BANNER = re.compile(r"BANNER VERDICT: timing=(\w+) coverage=([A-Z \-]*?) has_cov")
RECORD = re.compile(r"SHOT RECORD: epoch=(\d+) type=(\w+) ")
ONSETFF = re.compile(r"ONSET FF: reason=(\w+) ")
RELEASE_FF = re.compile(r"Release onsetff: seq=(\d+) applied_ms=([\-\d.]+)")
GROUPS = ((-999.0, -60.0, "< -60"), (-60.0, -20.0, "-60..-20"), (-20.0, 20.0, "-20..+20"),
          (20.0, 60.0, "+20..+60"), (60.0, 999.0, "> +60"))


def _t(line: str):
    m = TS.match(line)
    if not m:
        return None, None
    return (m.group(1), int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4))
            + int(m.group(5)) / 1000)


def _f(v):
    try:
        value = float(v)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def load_lines(paths):
    files = [Path(p) for p in paths if Path(p).exists()]
    files.sort(key=lambda p: io.open(p, encoding="utf-8", errors="replace").readline()[:24])
    lines = []
    for p in files:
        lines += io.open(p, encoding="utf-8", errors="replace").read().splitlines()
    return lines


def collect(lines, since: str | None, records=None):
    """Join by (record session, uint64 epoch) AND its explicit observation interval.

    JSONL records supply the attributed banner/onset. Text-only epochs restart at
    launch and cannot establish this join. Missing or conflicting release offsets
    remain UNKNOWN, never an undisplaced control observation.
    """
    if records is None:
        raise ValueError("Session JSONL records are required; log epochs alone are not unique")
    shots, raw_records, by_epoch = {}, {}, collections.defaultdict(list)
    for record in records:
        ep = record.get("epoch")
        session = record.get("session")
        start, end = _f(record.get("press_ts_ms")), _f(record.get("closed_ts_ms"))
        if (isinstance(ep, bool) or not re.fullmatch(r"[0-9]+", str(ep))
                or not 0 < int(ep) < 2**64 or not isinstance(session, str) or not session
                or start is None or end is None or start < 0 or end < start):
            raise ValueError("Invalid session identity or observation interval")
        key = (session, int(ep))
        if key in raw_records:
            if raw_records[key] != record:
                raise ValueError("Conflicting duplicate session/epoch record")
            continue
        raw_records[key] = record
        dt = datetime.fromtimestamp(start/1000, timezone.utc)
        stamp = dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        if since and stamp[:len(since)] < since:
            continue
        banner = record.get("banner") or {}
        network = record.get("network") or {}
        shots[key] = dict(session=session, epoch=int(ep), start=start, end=end,
            day=dt.date().isoformat(), t=dt.hour*3600+dt.minute*60+dt.second+dt.microsecond/1e6,
            timing=banner.get("timing") if record.get("outcome") == "released" else None,
            cov=banner.get("coverage"),
            type=str(record.get("shot_type_upgraded") or record.get("shot_type") or "").replace(" ", "_"),
            onset=_f(record.get("onset_ms")), rtt=_f(network.get("court_rtt_ms")),
            jitter=_f(network.get("court_jitter_ms")), ready=_f(network.get("court_ready")),
            range=record.get("range"), tempo=record.get("tempo"),
            distance_ft=_f(banner.get("distance_ft")),
            dev=None, ff_arm=None, ff=None, ff_reason=None, ff_conflict=False)
        by_epoch[int(ep)].append(key)
    unjoined = 0
    arms, releases = collections.defaultdict(list), collections.defaultdict(set)
    seen = set()
    for line in lines:
        if "Sidecar tail:" in line or line in seen:
            continue
        tag = ("Release onsetff:" if "Release onsetff:" in line
               else "ONSET FF:" if "ONSET FF:" in line else None)
        if tag is None:
            continue
        seen.add(line)
        kv = dict(KV.findall(line.split(tag, 1)[1]))
        epoch = kv.get("physical_epoch", "")
        try:
            if not TS.match(line):
                raise ValueError("UTC timestamp required")
            wall = datetime.fromisoformat(line[:24].replace("Z", "+00:00")).timestamp()*1000
        except ValueError:
            unjoined += 1
            continue
        matches = [key for key in by_epoch.get(int(epoch), ())
                   if shots[key]["start"] <= wall <= shots[key]["end"]] if epoch.isdigit() else []
        if len(matches) != 1:
            unjoined += 1
            continue
        key = matches[0]
        if tag == "ONSET FF:":
            arms[key].append((wall, tuple(sorted(kv.items()))))
        else:
            releases[key].add((kv.get("seq"), kv.get("scheduled"), kv.get("shot_attempt"), _f(kv.get("applied_ms"))))
    for key,s in shots.items():
        if arms[key]:
            latest = max(wall for wall, _ in arms[key])
            last = {fields for wall,fields in arms[key] if wall == latest}
            if len(last) == 1:
                kv = dict(last.pop())
                s["ff_reason"] = kv.get("reason")
                if kv.get("reason") in ("applied", "trim_bound", "one_sided", "zero"):
                    s["dev"] = _f(kv.get("dev_ms"))
                s["ff_arm"] = _f(kv.get("applied_ms"))
        if len(releases[key]) == 1:
            seq, scheduled, attempt, value = next(iter(releases[key]))
            if seq and seq.isdigit() and int(seq)>0 and scheduled in ("0", "1"):
                s["ff"] = value
        elif len(releases[key]) > 1:
            s["ff_conflict"] = True
    return shots, unjoined


def sessions(rows, gap_s=1200):
    out = []
    for s in sorted(rows, key=lambda r: (r["session"], r["day"], r["t"])):
        if (not out or s["session"] != out[-1][-1]["session"]
                or s["day"] != out[-1][-1]["day"] or s["t"] - out[-1][-1]["t"] > gap_s):
            out.append([])
        out[-1].append(s)
    return out


def split(rows):
    if not rows:
        return "        -"
    c = collections.Counter(r["timing"] for r in rows)
    n = len(rows)
    return (f"EXC {100 * c['EXCELLENT'] / n:4.0f}%  L {100 * c['LATE'] / n:3.0f}%  "
            f"E {100 * c['EARLY'] / n:3.0f}%  (n={n})")


def grade(shots: dict, shot_type: str):
    graded = [s for s in shots.values() if s["timing"] and s["type"] == shot_type
              and s["onset"] is not None and s["day"] is not None]
    for sess in sessions(graded):
        online = [s for s in sess if s["onset"] >= 0.0]
        if len(online) < 8:
            continue
        x = [s["onset"] for s in online]
        # A centred hindsight median (including future shots) is not the engine's
        # reference. Keep missing deviations out of the conditional comparison.
        engine_dev = sum(s["dev"] is not None for s in online)
        hh = int(online[0]["t"] // 3600)
        mm = int((online[0]["t"] % 3600) // 60)
        moved = sum(1 for s in online if s["ff"] is not None and s["ff"] != 0.0)
        unknown = sum(s["ff"] is None for s in online)
        print(f"== {online[0]['session']} {online[0]['day']} {hh:02d}:{mm:02d}Z  n={len(online)}  onset med {st.median(x):.0f}"
              f"  deviation from the ENGINE's reference on {engine_dev}/{len(online)}"
              f"  displaced {moved} ({100 * moved / len(online):.0f}%)"
              f"  displacement_unknown {unknown}"
              f"  wide-open {split([s for s in online if s['cov'] == 'WIDE OPEN'])}")
        print(f"   {'deviation':9s} | {'all':38s} | {'displaced':38s} | {'not displaced':38s} | jitter")
        for lo, hi, name in GROUPS:
            g = [s for s in online if s["dev"] is not None and lo <= s["dev"] < hi]
            if not g:
                continue
            jit = [s["jitter"] for s in g if s["jitter"] is not None and (s["ready"] or 0) > 0]
            jtxt = f"{st.median(jit):.1f} ms (n={len(jit)})" if jit else "-"
            print(f"   {name:9s} | {split(g):38s} | {split([s for s in g if s['ff'] is not None and s['ff'] != 0.0]):38s} "
                  f"| {split([s for s in g if s['ff'] == 0.0]):38s} | {jtxt}")
        pairs = [(s["dev"], s["jitter"]) for s in online
                 if s["dev"] is not None and s["jitter"] is not None and (s["ready"] or 0) > 0]
        if len(pairs) >= 12:
            d = [a for a, _ in pairs]
            j = [b for _, b in pairs]
            md, mj = st.mean(d), st.mean(j)
            num = sum((a - md) * (b - mj) for a, b in pairs)
            den = (sum((a - md) ** 2 for a in d) * sum((b - mj) ** 2 for b in j)) ** 0.5
            print(f"   onset deviation vs court jitter: r = {num / den if den else float('nan'):+.2f} (n={len(pairs)})")
        else:
            print("   court RTT: not stamped on enough shots to correlate (sampler had no verified court)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split(chr(10))[0])
    ap.add_argument("--logs", nargs="+", default=["logs/orion_native.log.1", "logs/orion_native.log"])
    ap.add_argument("--records", nargs="+", type=Path, required=True,
                    help="Session JSONL records supplying identity, interval, onset and attributed banner")
    ap.add_argument("--since", help="ISO prefix, e.g. 2026-09-21T20:00 (UTC, as the log stamps)")
    ap.add_argument("--type", default="Standstill")
    args = ap.parse_args()
    records = [json.loads(line) for p in args.records for line in p.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    shots, unjoined = collect(load_lines(args.logs), args.since, records)
    joined = sum(1 for s in shots.values() if s["timing"] and s["onset"] is not None)
    print(f"session/epochs seen: {len(shots)} | verdict+record joined: {joined} "
          f"| offset events without an unambiguous record interval (excluded): {unjoined}")
    grade(shots, args.type)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
