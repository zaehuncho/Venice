#!/usr/bin/env python
"""Venice reliability scoreboard: grade every session the same way (2026-09-22).

The selling point is FEWER EARLIES AND LATES than anyone else, session after session. This grades
each recorded session from the shot records alone (banner verdicts), so every timing fix shows up
as a number and the worst session of the week is as visible as the average.

    python tools/timing/scoreboard.py                 # last 7 days
    python tools/timing/scoreboard.py --days 3
    python tools/timing/scoreboard.py --session session_20260922_202346

Per session: graded shots, EXCELLENT / EARLY / LATE rates overall and for Standstill / Left Fade /
Right Fade, no-banner count, the longest LATE streak, and P(LATE | previous LATE). Sessions run
with a deliberate timing experiment (dev fire-offset sweep / list) are marked and left out of the
summary, because their misses were caused on purpose.

Read-only. It never touches Venice, settings or the logs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import statistics as st
from collections import Counter

RECORDS = r"D:\NexusVision\shot_records"
MIN_GRADED = 15
# Sessions whose shots were deliberately displaced (dev offset sweep / list); see
# docs/variance/OFFSET_SWEEP_RESULT_2026-09-22.md. Excluded from the summary.
EXPERIMENT_SESSIONS = {"session_20260922_165506", "session_20260922_172143",
                       # in-game meter left on Pill while Venice read Arrow2 (owner, 09-23)
                       "session_20260922_210220",
                       # OFFLINE match, not comparable with online play (owner, 09-23)
                       "session_20260920_170027"}
TYPES = ("Standstill", "Left Fade", "Right Fade")


def load(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def grade(rows: list[dict]) -> dict:
    by_type: dict[str, Counter] = {t: Counter() for t in TYPES}
    total = Counter()
    no_banner = 0
    streak = best_streak = 0
    after_late = Counter()
    prev = None
    for r in rows:
        if r.get("outcome") != "released":
            continue
        verdict = (r.get("banner") or {}).get("timing")
        if verdict not in ("EARLY", "EXCELLENT", "LATE"):
            no_banner += 1
            prev = None
            continue
        total[verdict] += 1
        if r.get("shot_type") in by_type:
            by_type[r["shot_type"]][verdict] += 1
        streak = streak + 1 if verdict == "LATE" else 0
        best_streak = max(best_streak, streak)
        if prev == "LATE":
            after_late[verdict] += 1
        prev = verdict
    return {"total": total, "by_type": by_type, "no_banner": no_banner,
            "longest_late_streak": best_streak, "after_late": after_late}


def pct(c: Counter, key: str) -> str:
    n = sum(c.values())
    return f"{100.0 * c[key] / n:5.1f}%" if n else "    - "


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--records", default=RECORDS)
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--session", default="")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.records, "session_*.jsonl")))
    if args.session:
        files = [f for f in files if args.session in os.path.basename(f)]
    else:
        cutoff = dt.datetime.now() - dt.timedelta(days=args.days)
        files = [f for f in files if dt.datetime.fromtimestamp(os.path.getmtime(f)) >= cutoff]

    print(f"{'session':25} {'graded':>6} {'EXC':>6} {'EARLY':>6} {'LATE':>6} | "
          f"{'SS exc':>6} {'LF exc':>6} {'LF early':>8} {'RF exc':>6} | {'nobnr':>5} "
          f"{'streak':>6} {'L|L':>6}")
    kept = []
    for f in files:
        name = os.path.basename(f)[:-6]
        g = grade(load(f))
        n = sum(g["total"].values())
        if n < MIN_GRADED and not args.session:
            continue
        tag = " (experiment)" if name in EXPERIMENT_SESSIONS else ""
        print(f"{name:25} {n:6} {pct(g['total'], 'EXCELLENT'):>6} {pct(g['total'], 'EARLY'):>6} "
              f"{pct(g['total'], 'LATE'):>6} | {pct(g['by_type']['Standstill'], 'EXCELLENT'):>6} "
              f"{pct(g['by_type']['Left Fade'], 'EXCELLENT'):>6} {pct(g['by_type']['Left Fade'], 'EARLY'):>8} "
              f"{pct(g['by_type']['Right Fade'], 'EXCELLENT'):>6} | {g['no_banner']:5} "
              f"{g['longest_late_streak']:6} {pct(g['after_late'], 'LATE'):>6}{tag}")
        if not tag:
            kept.append((name, n, g))

    if len(kept) >= 2:
        rates = [100.0 * g["total"]["EXCELLENT"] / n for _, n, g in kept]
        pooled = Counter()
        for _, _, g in kept:
            pooled.update(g["total"])
        worst = min(kept, key=lambda k: k[2]["total"]["EXCELLENT"] / k[1])
        print(f"\nSUMMARY ({len(kept)} sessions, experiments excluded): pooled EXC {pct(pooled, 'EXCELLENT')} "
              f"EARLY {pct(pooled, 'EARLY')} LATE {pct(pooled, 'LATE')} | median session EXC "
              f"{st.median(rates):.1f}% | worst session {worst[0]} "
              f"{100.0 * worst[2]['total']['EXCELLENT'] / worst[1]:.1f}% (n={worst[1]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
