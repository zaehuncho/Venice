"""farshot_press_join.py -- join engine presses to the framedump's own rows.

Read-only. Builds the miss population for the "meter not seen from beyond half court"
study: every `Physical shot epoch` in session_20260912_201355's window, its outcome
(PHASE SAMPLE / press_unanswered_no_meter / ownership_proof_incomplete), and the
framedump rows in [press, press + WIN_S].

Usage:
    .venv/Scripts/python.exe tools/diagnostics/farshot_press_join.py
Writes logs/diagnostics/farshot_study/presses.csv
"""
from __future__ import annotations

import bisect
import collections
import csv
import datetime
import os
import re
import sys

LOG = os.path.join("logs", "orion_native.log.1")
DUMP = r"D:\NexusVision\framedump\session_20260912_201355"
OUT = os.path.join("logs", "diagnostics", "farshot_study")
LO = datetime.datetime(2026, 9, 13, 1, 14, 40)
HI = datetime.datetime(2026, 9, 13, 1, 44, 55)
WIN_S = 1.5
EPOCH0 = datetime.datetime(1970, 1, 1)

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3})Z\s+(.*)$")


def _unix(t: datetime.datetime) -> float:
    return (t - EPOCH0).total_seconds()


def read_events():
    presses = {}           # epoch -> (unix_ts, intent)
    outcome = {}           # epoch -> (kind, detail, hold_ms, shot_type, unix_ts)
    phase = []             # (unix_ts, text)
    with open(LOG, "rb") as f:
        for raw in f:
            s = raw.decode("utf-8", "replace").rstrip("\r\n")
            m = _TS.match(s)
            if not m:
                continue
            t = datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S.%f")
            if not (LO <= t <= HI):
                continue
            u, b = _unix(t), m.group(2)
            g = re.match(r"Physical shot epoch: epoch=(\d+) intent=(\S+)", b)
            if g:
                presses.setdefault(int(g.group(1)), (u, g.group(2)))
                continue
            g = re.match(r"SHOT NOT OWNED: reason=(\S+) physical_epoch=(\d+) hold_ms=([\d.]+) "
                         r"shot_type=(.*?) unstamped", b)
            if g:
                outcome[int(g.group(2))] = ("NOT_OWNED", g.group(1), float(g.group(3)),
                                            g.group(4), u)
                continue
            if b.startswith("PHASE SAMPLE"):
                phase.append((u, b))
    return presses, outcome, phase


def read_frames():
    rows = []
    with open(os.path.join(DUMP, "frames.csv"), newline="") as f:
        for r in csv.DictReader(f):
            rows.append((float(r["t_wall"]), int(r["idx"]), int(r["detected"]),
                         float(r["fill_pct"]), int(r["bbox_x"]), int(r["bbox_y"]),
                         int(r["bbox_w"]), int(r["bbox_h"]), r["rejection"]))
    rows.sort()
    return rows


def main():
    os.makedirs(OUT, exist_ok=True)
    presses, outcome, phase = read_events()
    rows = read_frames()
    t0, t1 = rows[0][0], rows[-1][0]
    import bisect
    walls = [r[0] for r in rows]

    recs = []
    for ep, (pt, intent) in sorted(presses.items()):
        if not (t0 - 1.0 <= pt <= t1):
            continue
        kind = "OWNED/other"
        detail = ""
        hold = -1.0
        stype = "?"
        if ep in outcome:
            _, detail, hold, stype, _ = outcome[ep]
            kind = "NOT_OWNED"
        else:
            # a PHASE SAMPLE within 1.5 s of the press means the engine owned the shot
            for u, b in phase:
                if 0.0 <= u - pt <= 1.6:
                    kind = "PHASE_SAMPLE"
                    detail = b[:120]
                    break
        i0 = bisect.bisect_left(walls, pt)
        i1 = bisect.bisect_right(walls, pt + WIN_S)
        win = rows[i0:i1]
        det = [w for w in win if w[2] == 1]
        recs.append(dict(
            epoch=ep, press_wall=f"{pt:.3f}", kind=kind, detail=detail, hold_ms=hold,
            shot_type=stype, n_frames=len(win),
            first_idx=(win[0][1] if win else -1), last_idx=(win[-1][1] if win else -1),
            n_detected=len(det),
            first_det_idx=(det[0][1] if det else -1),
            first_det_ms=(round((det[0][0] - pt) * 1000.0, 1) if det else -1),
            det_y=(det[0][5] if det else -1), det_h=(det[0][7] if det else -1),
            rejections=";".join(sorted({w[8] for w in win if w[8]})),
        ))

    path = os.path.join(OUT, "presses.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(recs[0].keys()))
        w.writeheader()
        w.writerows(recs)
    print(f"wrote {path}  ({len(recs)} presses inside the dump)")

    import collections
    c = collections.Counter((r["kind"], r["detail"] if r["kind"] == "NOT_OWNED" else "") for r in recs)
    for k, v in c.most_common():
        print(f"  {v:4d}  {k}")
    print()
    print("press outcome  x  did the DUMP show a detection within 1.5 s?")
    c2 = collections.Counter((r["kind"] + ":" + (r["detail"] or ""), r["n_detected"] > 0) for r in recs)
    for k, v in sorted(c2.items()):
        print(f"  {v:4d}  {k}")
    shots(rows, walls, [float(r["press_wall"]) for r in recs])


def shots(rows, walls, press_ts):
    """The other half of the taxonomy: every GRADED shot (the game's own banner), whether the
    detector saw a meter for it, and whether an owner press preceded it. A banner with no
    press in [-3.5, -0.8] s is not the owner's shot -- in Rec/Theater the feedback panel also
    renders for team-mates, and no meter is drawn on the owner's screen for those."""
    G = [r for r in csv.DictReader(open(os.path.join(DUMP, "panel_grade.csv"))) if r["word"]]
    pts = sorted(press_ts)
    out, tab = [], collections.Counter()
    for k, g in enumerate(G):
        t0 = float(g["t0"])
        i0 = bisect.bisect_left(walls, t0 - 2.6)
        i1 = bisect.bisect_right(walls, t0 - 0.1)
        win = rows[i0:i1]
        det = [w for w in win if w[2] == 1]
        own = any(t0 - 3.5 <= p <= t0 - 0.8 for p in pts)
        tab[("own_press" if own else "no_press", "blind" if not det else "seen")] += 1
        out.append(dict(shot=k, t0=f"{t0:.3f}", word=g["word"], coverage=g.get("coverage", ""),
                        first_idx=win[0][1] if win else -1, last_idx=win[-1][1] if win else -1,
                        n_frames=len(win), n_detected=len(det), owner_press=int(own),
                        first_det_y=(det[0][5] if det else -1),
                        first_det_h=(det[0][7] if det else -1)))
    path = os.path.join(OUT, "shots.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    print(f"\nwrote {path}  ({len(out)} graded shots)")
    print("owner press?  x  did the detector see a meter?")
    for kk, v in sorted(tab.items()):
        print(f"  {v:4d}  {kk}")


if __name__ == "__main__":
    sys.exit(main())
