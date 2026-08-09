#!/usr/bin/env python3
"""PHASE B.1 - Shadow-mode A/B analyzer (offline; no live machine needed).

Pairs three seq-keyed telemetry lines from orion_native.log and reports, per shot type, what the
SHADOW (global-velocity autonomous model, compute-only) WOULD have done vs the live release the bot
actually fired:

  Release timing: seq= code= ... appearToRelMs= fillAtRel= peakFill= shot=   (the LIVE release)
  Shadow timing:  seq= shadowAppearToRelMs= deltaMs= shadowFill= vg= latencyMs= shot=  (the MODEL)
  Shot outcome:   seq= verdict= errorMs= ... shot=                            (the post-release grade)

Key columns:
  modelFired   = the autonomous model reached a release decision this shot (shadowAppearToRelMs>=0).
                 If it could NOT fire for fast types, that is the detector-freshness gap (the model
                 needs a fresh fill in its release band), NOT a model failure.
  deltaMs      = liveAppearToRel - shadowAppearToRel  (>0 => the model would have released EARLIER
                 than the live path; <0 => later).
  would-help   = live shot graded LATE (verdict LATE or errorMs>0) AND the model would fire earlier
                 (deltaMs>0) -> the model likely improves it. (Heuristic; meter self-grade is noisy
                 at the dead-top, so treat as directional, not proof.)

Usage:
  C:\\Python314\\python.exe tools/diagnostics/analyze_shadow.py [logs/orion_native.log] [--since 2026-06-19T18:2]
"""
from __future__ import annotations

import argparse
import re
import statistics
from collections import defaultdict

_REL = re.compile(r"Release timing: seq=(\d+) code=(\S+) .*?appearToRelMs=(-?\d+) .*?"
                  r"fillAtRel=([\d.]+) peakFill=([\d.]+) shot=(.+?)\s*$")
_SHA = re.compile(r"Shadow timing: seq=(\d+) shadowAppearToRelMs=(-?\d+) deltaMs=(-?\d+) "
                  r"shadowFill=(-?[\d.]+) vg=([\d.]+) latencyMs=(\d+) shot=(.+?)\s*$")
_OUT = re.compile(r"Shot outcome: seq=(\d+) verdict=(\S+) errorMs=(-?[\d.]+) .* shot=(.+?)\s*$")


def med(v):
    return statistics.median(v) if v else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", nargs="?", default="logs/orion_native.log")
    ap.add_argument("--since", default="", help="only lines whose timestamp startswith this prefix (e.g. a session)")
    args = ap.parse_args()

    rel, sha, out = {}, {}, {}
    with open(args.log, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if args.since and args.since not in line[:32]:   # timestamp prefix is at line start
                continue
            m = _REL.search(line)
            if m:
                rel[int(m.group(1))] = dict(code=m.group(2), appearToRel=int(m.group(3)),
                                            fillAtRel=float(m.group(4)), peakFill=float(m.group(5)),
                                            shot=m.group(6))
                continue
            m = _SHA.search(line)
            if m:
                sha[int(m.group(1))] = dict(shadowAppearToRel=int(m.group(2)), deltaMs=int(m.group(3)),
                                            shadowFill=float(m.group(4)), vg=float(m.group(5)),
                                            latencyMs=int(m.group(6)), shot=m.group(7))
                continue
            m = _OUT.search(line)
            if m:
                out[int(m.group(1))] = dict(verdict=m.group(2), errorMs=float(m.group(3)), shot=m.group(4))

    seqs = sorted(set(rel) | set(sha))
    if not seqs:
        print("no shadow/release telemetry found (is autonomous_vision_shadow enabled + a batch logged?)")
        return 1

    by_type = defaultdict(list)
    for s in seqs:
        r, h, o = rel.get(s), sha.get(s), out.get(s)
        shot = (r or h or {}).get("shot", "?")
        by_type[shot].append((s, r, h, o))

    print(f"paired {len(seqs)} shots from {args.log}"
          + (f"  (since '{args.since}')" if args.since else ""))
    print("\n=== per shot type ===")
    print(f"  {'type':12} n  modelFired  liveLATE liveEARLY liveGREEN | deltaMs(med,fired)  would-help")
    tot_help = tot_fired = tot = 0
    for shot in sorted(by_type):
        rows = by_type[shot]
        n = len(rows)
        fired = [h for _, _, h, _ in rows if h and h["shadowAppearToRel"] >= 0]
        deltas = [h["deltaMs"] for h in fired]
        late = sum(1 for _, _, _, o in rows if o and (o["verdict"] == "LATE" or o["errorMs"] > 0.5))
        early = sum(1 for _, _, _, o in rows if o and (o["verdict"] == "EARLY" or o["errorMs"] < -0.5))
        green = sum(1 for _, _, _, o in rows if o and o["verdict"] == "EXCELLENT" and abs(o["errorMs"]) <= 0.5)
        help_n = 0
        for _, r, h, o in rows:
            if h and h["shadowAppearToRel"] >= 0 and h["deltaMs"] > 0 and o and (o["verdict"] == "LATE" or o["errorMs"] > 0.5):
                help_n += 1
        tot += n; tot_fired += len(fired); tot_help += help_n
        dtxt = f"{med(deltas):+.0f}" if deltas else "  -"
        print(f"  {shot:12} {n:2}  {len(fired):2}/{n:<2}      {late:2}      {early:2}       {green:2}    | "
              f"{dtxt:>8} ({len(deltas)})        {help_n}")

    print(f"\n=== summary ({tot} shots) ===")
    print(f"  model could fire: {tot_fired}/{tot} ({100*tot_fired/tot:.0f}%)   "
          f"<- low for fast types = detector-freshness gap (model needs a fresh fill in-band)")
    print(f"  live-LATE shots the model would fire EARLIER on (would-help): {tot_help}")
    print("\n  NOTE: meter self-grade (verdict/errorMs) is unreliable at the dead-top (false EXCELLENT/")
    print("  flips) - treat as DIRECTIONAL. deltaMs>0 = model fires earlier than the live clock.")
    print("  The fix that lets the model fire for fast types is the detector lock-on consistency work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
