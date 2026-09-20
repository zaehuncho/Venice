#!/usr/bin/env python
"""banner_join.py -- per-shot GAME VERDICT joined to the engine's own numbers (offline diagnostic).

    python tools/timing/banner_join.py --day 2026-09-09 --events out/events_A.csv out/events_B.csv \
        [--logs logs/orion_native.log.1 logs/orion_native.log] [--csv out/joined.csv]

Input events come from `tools/timing/banner_reader.py scan --session <framedump dir> --out ...`
(the reader recognises 2K27's TIMING panel: EXCELLENT / EARLY / LATE with a severity colour --
white = slight, yellow, red = severe; verified 2026-09-08 on session_20260903_133124/151910).
The join is FIFO by time: a banner belongs to the latest engine release 0.2..3.5 s before its
onset (verdict fade-in is court dependent), each release used once. The engine side is read from
`Release timing` / `PHASE SAMPLE` / `Release landing` / `TIP RESERVATION` lines of orion_native.log
(rotated files may be passed too; the day filter is the log's UTC day).

Output: verdict counts, the median of every engine variable per verdict class, and for LATE and
EARLY the separation from GREEN in units of GREEN's robust spread (d = (med_x - med_green)/rMAD).
Read it like this: a variable with |d| >= 2 for a verdict class is a CAUSE the engine can see
(2026-09-03: severe lates fired at fill 54 vs 40.5 -> a late COMMAND, d=+5); a class with every
|d| < 1 is the residual the engine cannot see from its own numbers -- that class needs a frame-level
instrument, not a tuning change.

HARD BOUNDARY (owner directive, same as banner_reader.py): analysis tooling only; never imported by
the engine/orchestrator/sidecar; a banner read must never reach the engine.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import math
import re
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

KV = re.compile(r"(\w+)=(-?[\d.]+)")
COLS = [("far", "fill@release"), ("peak", "landing"), ("raw_ms", "anchor->freeze ms"),
        ("frame_age", "frame_age ms"), ("stop_shift", "stop_shift ms"), ("hold2rel", "hold->rel ms"),
        ("appear2rel", "appear->rel ms"), ("travel", "travel pp"), ("vel", "vel@rel"), ("lead", "lead ms")]


def _ts(line: str) -> float:
    return datetime.fromisoformat(line[:24].replace("Z", "+00:00")).timestamp()


def parse_releases(paths, day):
    shots = []
    cur = None
    lead = float("nan")
    for line in itertools.chain.from_iterable(open(p, encoding="utf-8", errors="replace") for p in paths):
        if not line.startswith(day):
            continue
        if "TIP RESERVATION: disposition=reservation_promoted" in line:
            lead = float(dict(KV.findall(line)).get("lead_ms", "nan"))
        elif "Release timing:" in line:
            d = dict(KV.findall(line))
            cur = dict(seq=int(float(d["seq"])), t_rel=_ts(line), lead=lead,
                       hold2rel=float(d.get("holdToRelMs", "nan")), appear2rel=float(d.get("appearToRelMs", "nan")),
                       shot=line.split("shot=")[-1].strip())
            shots.append(cur)
        elif "PHASE SAMPLE:" in line and cur is not None and "raw_ms" not in cur:
            d = dict(KV.findall(line))
            cur.update(raw_ms=float(d["raw_ms"]), stop_shift=float(d.get("stop_snap_shift_ms", "nan")),
                       anchor_pct=float(d.get("anchor_pct", "nan")))
        elif "Release landing:" in line and cur is not None and "peak" not in cur:
            d = dict(KV.findall(line))
            cur.update(peak=float(d["peak_fill"]), settled=float(d.get("settled_fill", "nan")),
                       far=float(d["fill_at_rel"]), gs=float(d.get("green_start", "nan")),
                       ge=float(d.get("green_end", "nan")), vel=float(d.get("vel_at_rel", "nan")),
                       frame_age=float(d.get("frame_age_ms", "nan")), travel=float(d.get("travel_pp", "nan")))
    return shots


def parse_events(paths):
    local_tz = datetime.now().astimezone().tzinfo
    out = []
    for f in paths:
        with open(f, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                t = datetime.fromisoformat(r["onset_time"])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=local_tz)
                out.append(dict(t=t.timestamp(), word=r["word"], color=r["color"],
                                score=float(r.get("word_score", "nan") or "nan"), n=int(r.get("n_frames", 0) or 0),
                                event_id=r.get("event_id", "")))
    out.sort(key=lambda e: e["t"])
    return out


def join(events, shots, lo=0.2, hi=3.5):
    shots = sorted(shots, key=lambda s: s["t_rel"])
    used, joined = set(), []
    for e in events:
        cands = [s for s in shots if lo <= e["t"] - s["t_rel"] <= hi and id(s) not in used]
        if not cands:
            joined.append((e, None))
            continue
        s = max(cands, key=lambda s: s["t_rel"])
        used.add(id(s))
        joined.append((e, s))
    return joined


def verdict(e):
    return "GREEN" if e["word"] == "EXCELLENT" else f"{e['word']}-{e['color']}"


def rmad(v):
    m = st.median(v)
    return 1.4826 * st.median([abs(x - m) for x in v]) or 1e-9


def _vals(rows, k):
    return [s[k] for s in rows if k in s and isinstance(s[k], float) and not math.isnan(s[k])]


def report(joined, out=sys.stdout):
    ok = [(e, s) for e, s in joined if s is not None and "peak" in s and "raw_ms" in s]
    print(f"banner events {len(joined)}, joined {len(ok)}, unmatched banners {sum(1 for e, s in joined if s is None)}", file=out)
    print("verdicts:", Counter(verdict(e) for e, s in ok).most_common(), file=out)
    groups = defaultdict(list)
    for e, s in ok:
        groups[verdict(e)].append(s)
    print(f"\n{'verdict':16s} n   " + "  ".join(f"{lab:>18s}" for _, lab in COLS), file=out)
    for g in sorted(groups, key=lambda k: (k != "GREEN", k)):
        d = groups[g]
        cells = []
        for k, _ in COLS:
            v = _vals(d, k)
            cells.append(f"{st.median(v):18.2f}" if v else f"{'-':>18s}")
        print(f"{g:16s} {len(d):2d}  " + "  ".join(cells), file=out)
    green = groups.get("GREEN", [])
    late = [s for k, d in groups.items() if k.startswith("LATE") for s in d]
    early = [s for k, d in groups.items() if k.startswith("EARLY") for s in d]
    print(f"\nseparation from GREEN (d in GREEN rMAD units): green n={len(green)} late n={len(late)} early n={len(early)}", file=out)
    for k, lab in COLS:
        gv = _vals(green, k)
        if len(gv) < 4:
            continue
        line = f"  {lab:18s} green med {st.median(gv):8.2f} rMAD {rmad(gv):6.2f}"
        for name, rows in (("late", late), ("early", early)):
            v = _vals(rows, k)
            if len(v) >= 3:
                line += f" | {name} med {st.median(v):8.2f} (d={(st.median(v) - st.median(gv)) / rmad(gv):+.2f})"
        print(line, file=out)
    # severity split for lates: which lates did the engine cause (late command) vs not
    if late and green:
        far_g = st.median(_vals(green, "far")); far_r = rmad(_vals(green, "far"))
        cmd_late = [s for s in late if s.get("far", 0) - far_g > 2.0 * far_r]
        print(f"\nlates with a LATE COMMAND (fill@release > green + 2 rMAD): {len(cmd_late)}/{len(late)}; "
              f"the rest are the residual the engine's numbers do not see", file=out)
    print("\nby shot type:", {t: Counter(verdict(e) for e, s in ok if s["shot"] == t).most_common(4)
                             for t in sorted(set(s["shot"] for e, s in ok))}, file=out)
    print("\nseq shot        verdict        far   land  a2f  age  stop  lead", file=out)
    for e, s in sorted(ok, key=lambda es: es[1]["t_rel"]):
        print(f"{s['seq']:>3} {s['shot'][:10]:10s} {verdict(e):14s} {s['far']:5.1f} {s['peak']:6.2f} {s['raw_ms']:4.0f} "
              f"{s.get('frame_age', float('nan')):4.1f} {s.get('stop_shift', float('nan')):6.1f} {s.get('lead', float('nan')):5.0f}", file=out)
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", required=True, help="UTC day prefix of the engine log lines, e.g. 2026-09-09")
    ap.add_argument("--events", nargs="+", required=True, help="banner_reader scan CSV(s)")
    ap.add_argument("--logs", nargs="+", default=["logs/orion_native.log.1", "logs/orion_native.log"])
    ap.add_argument("--csv", help="write the joined per-shot rows here")
    a = ap.parse_args(argv)
    import os
    logs = [p for p in a.logs if os.path.isfile(p)]
    shots = parse_releases(logs, a.day)
    events = parse_events(a.events)
    joined = join(events, shots)
    ok = report(joined)
    if a.csv:
        keys = ["seq", "shot", "verdict", "word", "color", "t_rel"] + [k for k, _ in COLS] + ["gs", "ge", "settled", "anchor_pct"]
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(keys)
            for e, s in ok:
                row = dict(s); row.update(verdict=verdict(e), word=e["word"], color=e["color"])
                w.writerow([row.get(k, "") for k in keys])
        print(f"\nwrote {len(ok)} rows -> {a.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
