"""hud_landmark_shots.py -- build the per-shot press/release table for the HUD landmark study.

Read-only.  Joins, for the framedump session's window:

  * `Physical shot epoch`            -> the owner's press (unix seconds, the framedump clock)
  * `SHOT NOT OWNED ... hold_ms`     -> the owner's OWN release (press + hold_ms)
  * `Release delivery identity` +
    `Release submit ... fire_epoch_ms`-> the ENGINE's release command (precise epoch)
  * `Release timing: ... shot=`       -> the shot type for engine-released shots
  * panel_grade.csv / farshot shots.csv -> the banner grade, when one joins

The point of the table is that press->release is NOT constant in this session (the engine
fired some shots ~100 ms after the press, the owner released others at the full 651/927/955 ms
hold), so a landmark that tracks the RELEASE is separable from one that tracks the PRESS.

Usage:
    .venv/Scripts/python.exe tools/diagnostics/hud_landmark_shots.py
Writes logs/diagnostics/hud_landmark_study/shots_table.csv
"""
from __future__ import annotations

import bisect
import csv
import datetime
import os
import re

LOG = os.path.join("logs", "orion_native.log.1")
DUMP = r"D:\NexusVision\framedump\session_20260912_201355"
OUT = os.path.join("logs", "diagnostics", "hud_landmark_study")
LO = datetime.datetime(2026, 9, 13, 1, 14, 0)
HI = datetime.datetime(2026, 9, 13, 1, 45, 30)
EPOCH0 = datetime.datetime(1970, 1, 1)

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3})Z\s+(.*)$")


def _unix(t: datetime.datetime) -> float:
    return (t - EPOCH0).total_seconds()


def read_log():
    presses = {}     # epoch -> (unix, intent)
    not_owned = {}   # epoch -> (hold_ms, shot_type)
    rel_ep = {}      # release_seq -> physical_epoch
    rel_fire = {}    # release_seq -> fire unix
    rel_type = {}    # release_seq -> shot type
    rel_hold = {}    # release_seq -> holdToRelMs (engine's own measure)
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
            g = re.match(r"SHOT NOT OWNED: reason=\S+ physical_epoch=(\d+) hold_ms=([\d.]+) "
                         r"shot_type=(.*?) unstamped", b)
            if g:
                not_owned[int(g.group(1))] = (float(g.group(2)), g.group(3))
                continue
            g = re.match(r"Release delivery identity: physical_epoch=(\d+) shot_attempt=\d+ "
                         r"release_seq=(\d+)", b)
            if g:
                rel_ep[int(g.group(2))] = int(g.group(1))
                continue
            g = re.match(r"Release submit: seq=(\d+) .*fire_epoch_ms=([\d.]+)", b)
            if g:
                rel_fire[int(g.group(1))] = float(g.group(2)) / 1000.0
                continue
            g = re.match(r"Release timing: seq=(\d+) .*holdToRelMs=(-?[\d.]+) .*shot=(.*?) "
                         r"pressTrimMs", b)
            if g:
                rel_type[int(g.group(1))] = g.group(3)
                rel_hold[int(g.group(1))] = float(g.group(2))
    return presses, not_owned, rel_ep, rel_fire, rel_type, rel_hold


def read_frames():
    rows = []
    with open(os.path.join(DUMP, "frames.csv"), newline="") as f:
        for r in csv.DictReader(f):
            rows.append(dict(idx=int(r["idx"]), t=float(r["t_wall"]),
                             det=int(r["detected"]), fill=float(r["fill_pct"]),
                             bx=int(r["bbox_x"]), by=int(r["bbox_y"]),
                             bw=int(r["bbox_w"]), bh=int(r["bbox_h"])))
    rows.sort(key=lambda r: r["t"])
    return rows


def read_banners():
    """farshot shots.csv: banner-graded shots with their wall clock."""
    p = os.path.join("logs", "diagnostics", "farshot_study", "shots.csv")
    out = []
    if not os.path.exists(p):
        return out
    with open(p, newline="") as f:
        for r in csv.DictReader(f):
            out.append((float(r["t0"]), r["word"], r.get("coverage", "")))
    out.sort()
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    presses, not_owned, rel_ep, rel_fire, rel_type, rel_hold = read_log()
    frames = read_frames()
    walls = [r["t"] for r in frames]
    t0, t1 = walls[0], walls[-1]
    banners = read_banners()
    btimes = [b[0] for b in banners]

    # invert: physical_epoch -> release_seq
    ep2seq = {v: k for k, v in rel_ep.items()}

    recs = []
    for ep, (pt, intent) in sorted(presses.items()):
        if not (t0 <= pt <= t1 - 2.0):
            continue
        rel = -1.0
        rel_src = "none"
        stype = "?"
        if ep in ep2seq and ep2seq[ep] in rel_fire:
            sq = ep2seq[ep]
            rel = rel_fire[sq]
            rel_src = "engine"
            stype = rel_type.get(sq, "?")
        elif ep in not_owned:
            hold, stype = not_owned[ep]
            rel = pt + hold / 1000.0
            rel_src = "owner_hold"
        # nearest banner within [press, press+3.5 s]
        word, cov, bt = "", "", -1.0
        i = bisect.bisect_left(btimes, pt)
        if i < len(btimes) and btimes[i] - pt <= 3.5:
            bt, word, cov = banners[i]
        recs.append(dict(
            epoch=ep, intent=intent, press_wall=f"{pt:.3f}",
            release_wall=(f"{rel:.3f}" if rel > 0 else ""),
            release_src=rel_src,
            hold_ms=(round((rel - pt) * 1000.0, 1) if rel > 0 else ""),
            shot_type=stype,
            banner_wall=(f"{bt:.3f}" if bt > 0 else ""), banner=word, coverage=cov,
        ))

    path = os.path.join(OUT, "shots_table.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(recs[0].keys()))
        w.writeheader()
        w.writerows(recs)
    n_rel = sum(1 for r in recs if r["release_src"] != "none")
    print(f"{path}: {len(recs)} presses, {n_rel} with a known release")
    from collections import Counter
    print(Counter(r["release_src"] for r in recs))
    print(Counter(r["shot_type"] for r in recs))
    holds = sorted(float(r["hold_ms"]) for r in recs if r["hold_ms"] != "")
    if holds:
        print(f"hold_ms: min={holds[0]:.0f} med={holds[len(holds)//2]:.0f} max={holds[-1]:.0f}")


if __name__ == "__main__":
    main()
