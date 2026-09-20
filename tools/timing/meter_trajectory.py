#!/usr/bin/env python
"""Per-shot METER TIMELINE from the sidecar's detframes CSV, joined to the engine release and
the banner verdict. The instrument that separates "our command was off" from "the game's
meter did something else" on a miss.

WHY
---
The engine's own per-shot numbers (fill@release, hold->release, the phase sample) are the same
on greens and lates (measured 09-03 and again 09-13), so they cannot explain a miss. The
60 fps detframes CSV (ORION_DETCSV=1, logs/diagnostics/detframes.csv, rotated per session to
detframes_<stamp>.csv) carries every fill sample with a wall clock, which lets us reconstruct,
for each release:

  * t20 .. t90   crossing times of the fill (ms after the 20 % crossing = the phase anchor);
                 the meter's own timeline. On 09-13 it repeats to sd ~2-5 ms shot to shot,
                 so a shot whose t80 is 40 ms off is a DIFFERENT animation or a hitch, not noise.
  * cmd-t20      when the engine's command went out relative to the anchor (sd ~3 ms).
  * freeze-cmd   when the meter stopped rising on the capture, relative to the command: the only
                 post-command witness we have (transport + console + capture). Core sd ~4 ms.
  * freezefill   the fill at the freeze (the reader's landing; ruler noise ~1.4 pp, so it does
                 NOT separate verdicts - the banner does).
  * bounce       descent after the peak (the meter bounces off the top on a LATE release).

USAGE
-----
  python tools/timing/meter_trajectory.py <detframes.csv> <t_lo> <t_hi> [panel_grade.csv]
     t_lo/t_hi: ISO UTC bounds of the session in the engine log (e.g. 2026-09-13T01:15 2026-09-13T01:50)
     engine logs default to logs/orion_native.log + .log.1 (--log to override, repeatable)

Reads the same "Release timing:" lines panel_grade.py joins on, so seq numbers line up.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as _dt
import os
import re
import statistics as st
from collections import defaultdict

KV = re.compile(r"(\w+)=([^\s]+)")


def ts2ms(s: str) -> float:
    return _dt.datetime.strptime(s[:23], "%Y-%m-%dT%H:%M:%S.%f").replace(
        tzinfo=_dt.timezone.utc).timestamp() * 1000.0


def engine_releases(logs, t_lo, t_hi):
    rel, modes = [], {}
    for p in logs:
        if not os.path.isfile(p):
            continue
        for line in open(p, encoding="utf-8", errors="replace"):
            ts = line[:24]
            if ts < t_lo or ts > t_hi:
                continue
            if "Release tempo:" in line:
                m = re.search(r"seq=(\d+) mode=(\w+)", line)
                if m:
                    modes[int(m.group(1))] = m.group(2)
            if "Release timing:" not in line:
                continue
            d = dict(KV.findall(line))
            m = re.search(r"shot=(.+?) pressTrimMs", line)
            rel.append({"ms": ts2ms(ts), "seq": int(d["seq"]), "shot": m.group(1) if m else "?",
                        "fillAtRel": float(d.get("fillAtRel", "nan"))})
    rel.sort(key=lambda r: r["ms"])
    for r in rel:
        r["mode"] = modes.get(r["seq"], "?")
    return rel


def verdicts(grade_csv):
    v = {}
    if grade_csv and os.path.isfile(grade_csv):
        for r in csv.DictReader(open(grade_csv, encoding="utf-8")):
            if r["seq"] and r["word"]:
                v[int(r["seq"])] = ((r.get("timing_color") or r["color"]) + " " + r["word"],
                                    r.get("coverage", ""))
    return v


def load_points(det_csv, lo, hi):
    pts = []
    with open(det_csv, encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            try:
                w = float(row["wall_ms"])
            except ValueError:
                continue
            if w < lo or w > hi or row["detected"] != "1":
                continue
            pts.append((w, float(row["fill_pct"])))
    pts.sort()
    return pts


def cross(seg, level):
    for (t0, f0), (t1, f1) in zip(seg, seg[1:]):
        if f0 < level <= f1 and f1 > f0:
            return t0 + (level - f0) * (t1 - t0) / (f1 - f0)
    return None


def analyse(rel, pts, verd):
    W = [p[0] for p in pts]
    rows = []
    for r in rel:
        a = bisect.bisect_left(W, r["ms"] - 450)
        b = bisect.bisect_right(W, r["ms"] + 700)
        seg = pts[a:b]
        if not seg:
            continue
        ic = bisect.bisect_right([p[0] for p in seg], r["ms"]) - 1
        if ic < 0:
            continue
        # walk back from the command to the start of the rise that contains it. A single
        # >3 pp drop (a box jump) ends the walk; those shots print with fewer crossings.
        j = ic
        while j > 0 and seg[j - 1][1] <= seg[j][1] + 3.0 and seg[j][1] > 2.0:
            j -= 1
        rise = seg[j:]
        t20 = cross(rise, 20.0)
        if t20 is None:
            rows.append(dict(seq=r["seq"], shot=r["shot"], mode=r["mode"], v=verd.get(r["seq"], ("", ""))[0],
                             cov=verd.get(r["seq"], ("", ""))[1], missing=True))
            continue
        after = [p for p in rise if p[0] >= r["ms"]]
        # freeze = first post-command sample the fill never exceeds by >1.0 pp in the next 4
        tf = ff = None
        for k in range(len(after) - 4):
            if max(p[1] for p in after[k + 1:k + 5]) <= after[k][1] + 1.0:
                tf, ff = after[k]
                break
        peak = max((p[1] for p in after), default=float("nan"))
        tail = [p[1] for p in after if tf is not None and p[0] >= tf][:12]
        bounce = (peak - min(tail)) if tail else float("nan")
        k = bisect.bisect_right([p[0] for p in rise], r["ms"]) - 1
        rows.append(dict(seq=r["seq"], shot=r["shot"], mode=r["mode"], v=verd.get(r["seq"], ("", ""))[0],
                         cov=verd.get(r["seq"], ("", ""))[1], missing=False,
                         fcmd=rise[k][1] if k >= 0 else float("nan"), cmd=r["ms"] - t20,
                         t40=_rel(cross(rise, 40.0), t20), t60=_rel(cross(rise, 60.0), t20),
                         t80=_rel(cross(rise, 80.0), t20), t90=_rel(cross(rise, 90.0), t20),
                         fz=(tf - r["ms"]) if tf is not None else float("nan"),
                         ffill=ff if ff is not None else float("nan"), bounce=bounce))
    return rows


def _rel(x, t20):
    return (x - t20) if x is not None else float("nan")


def _med(xs):
    xs = [x for x in xs if x == x]
    return st.median(xs) if xs else float("nan")


def _sd(xs):
    xs = [x for x in xs if x == x]
    return st.pstdev(xs) if len(xs) > 1 else float("nan")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("detframes")
    ap.add_argument("t_lo")
    ap.add_argument("t_hi")
    ap.add_argument("grade", nargs="?", default=None, help="panel_grade.csv of the same session")
    ap.add_argument("--log", action="append", default=None)
    a = ap.parse_args()
    logs = a.log or ["logs/orion_native.log", "logs/orion_native.log.1"]
    rel = engine_releases(logs, a.t_lo, a.t_hi)
    if not rel:
        print("no releases in window")
        return 1
    pts = load_points(a.detframes, rel[0]["ms"] - 1000, rel[-1]["ms"] + 1500)
    rows = analyse(rel, pts, verdicts(a.grade))
    print(f"{'seq':>3} {'shot':<11}{'mode':<12}{'verdict':<15}{'cov':<14}{'fcmd':>5}{'cmd':>5}{'t40':>5}{'t60':>5}"
          f"{'t80':>5}{'t90':>5}{'frz':>5}{'ffill':>6}{'bnc':>5}")
    for x in rows:
        if x["missing"]:
            print(f"{x['seq']:>3} {x['shot'][:11]:<11}{x['mode']:<12}{x['v']:<15}{x['cov'][:13]:<14}  (no clean rise: box jump / no meter)")
            continue
        print(f"{x['seq']:>3} {x['shot'][:11]:<11}{x['mode']:<12}{x['v']:<15}{x['cov'][:13]:<14}{x['fcmd']:>5.1f}{x['cmd']:>5.0f}"
              f"{x['t40']:>5.0f}{x['t60']:>5.0f}{x['t80']:>5.0f}{x['t90']:>5.0f}{x['fz']:>5.0f}{x['ffill']:>6.1f}{x['bounce']:>5.1f}")
    good = [x for x in rows if not x["missing"]]
    g = defaultdict(list)
    for x in good:
        if x["v"]:
            g["EXCELLENT" if "EXCELLENT" in x["v"] else x["v"]].append(x)
    if g:
        print("\nby verdict (median / sd, ms after the 20 % crossing; frz = freeze - command):")
        for k, rs in sorted(g.items(), key=lambda kv: -len(kv[1])):
            print(f"  {k:<14} n={len(rs):>2}  cmd {_med([x['cmd'] for x in rs]):4.0f}/{_sd([x['cmd'] for x in rs]):3.1f}"
                  f"  t80 {_med([x['t80'] for x in rs]):4.0f}/{_sd([x['t80'] for x in rs]):4.1f}"
                  f"  t90 {_med([x['t90'] for x in rs]):4.0f}  frz {_med([x['fz'] for x in rs]):4.0f}/{_sd([x['fz'] for x in rs]):4.1f}"
                  f"  ffill {_med([x['ffill'] for x in rs]):5.1f}/{_sd([x['ffill'] for x in rs]):3.1f}  bounce {_med([x['bounce'] for x in rs]):4.1f}")
    bt = defaultdict(list)
    for x in good:
        bt[(x["mode"], x["shot"])].append(x)
    print("\nby mode / shot type (t80 and freeze medians / sd):")
    for (mode, shot), rs in sorted(bt.items(), key=lambda kv: (kv[0][0], -len(kv[1]))):
        print(f"  {mode:<12}{shot:<12} n={len(rs):>2}  t80 {_med([x['t80'] for x in rs]):4.0f}/{_sd([x['t80'] for x in rs]):4.1f}"
              f"  t90 {_med([x['t90'] for x in rs]):4.0f}  frz {_med([x['fz'] for x in rs]):4.0f}/{_sd([x['fz'] for x in rs]):4.1f}"
              f"  cmd {_med([x['cmd'] for x in rs]):4.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
