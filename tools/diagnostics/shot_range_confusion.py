#!/usr/bin/env python
"""shot_range_confusion.py -- does the "3" cell agree with the banner's DISTANCE?

[ORION_SHOT_RANGE 2026-09-17] shot_range.py ships with ORION_SHOT_RANGE_CALIBRATED=0
because the confusion matrix it was specified to pass had no labels: the anchor found a
plate on 2 of 360 pickups, and the DISTANCE cell -- the oracle -- was never recorded.

Both of those are fixed now (player_anchor's ORION_ANCHOR_ACQUIRE pass, and
banner_distance.read_distance), so this tool builds the matrix:

  LABEL   the game's own DISTANCE string, read off the banner panel that follows a press
          (banner_distance), converted to feet.  `three` at >= --arc-ft.
  VERDICT shot_range.ShotRangeClassifier over the "3" cell at the plate player_anchor
          finds 40-120 ms after that press -- the live sampler's own window and its own
          geometry (shot_range.cell_stats), not a re-implementation.

Corpus: the 09-12 continuous dump, whose panel_grade.csv already cuts the panel events and
whose press table is logs/diagnostics/farshot_study/presses.csv.  A panel at t0 is joined
to the most recent press --join-lo..--join-hi seconds before it (release lands 0.6-1.1 s
after the press and the panel 1.0-1.7 s after the release).

  py tools/diagnostics/shot_range_confusion.py
  py tools/diagnostics/shot_range_confusion.py --arc-ft 23.75 --json out.json

Read-only.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import glob
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
for _p in (os.path.join(_REPO, "tools", "timing"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cv2                        # noqa: E402
import numpy as np                # noqa: E402

import banner_distance as bd      # noqa: E402
import player_anchor as pa        # noqa: E402
import shot_range as sr           # noqa: E402
from tools.diagnostics import anchor_acquire_study as S   # noqa: E402

DUMP = S.DUMP_0912


def panels(pg):
    """[(t0, feet, text)] -- every 09-12 panel whose DISTANCE cell reads cleanly."""
    frames = list(csv.DictReader(open(os.path.join(DUMP, "frames.csv"), newline="")))
    byt = {round(float(r["t_wall"]), 3): i for i, r in enumerate(frames)}
    out = []
    for r in csv.DictReader(open(os.path.join(DUMP, "panel_grade.csv"), newline="")):
        if not r.get("cells"):
            continue
        i0 = byt.get(round(float(r["t0"]), 3))
        if i0 is None:
            continue
        n = int(r["frames"])
        vote = bd.DistanceVote()
        for cand in range(i0, min(len(frames), i0 + n)):
            st = pg.strip_of(DUMP, frames[cand])
            if st is None:
                continue
            cells = pg.find_cells(st)
            d = [c for c, role in zip(cells, pg.cell_roles(cells)) if role == "distance"]
            if not d:
                continue
            txt, _ft, _dbg = bd.read_distance(st, d[0])
            vote.add(txt)
        text, votes, _tot = vote.best()
        if text and votes >= 2:
            out.append((float(r["t0"]), vote.feet(), text))
    return out


def matrix(rows, arc_ft, title, out_json=None):
    """One confusion matrix + per-class precision/recall. `rows` = [(label, verdict)]."""
    cm = {}
    for lab, ver in rows:
        cm[(lab, ver)] = cm.get((lab, ver), 0) + 1
    tot = sum(cm.values())
    print("\n%s   arc=%.1f ft   n=%d" % (title, arc_ft, tot))
    print("  label \\ verdict   three    mid  unknown")
    for lab in ("three", "mid"):
        print("  %-14s %6d %6d %8d"
              % (lab, cm.get((lab, "three"), 0), cm.get((lab, "mid"), 0),
                 cm.get((lab, "unknown"), 0)))
    ok = sum(v for (l, p), v in cm.items() if l == p)
    print("  agreement %d/%d = %.1f %%  (target 90 %%)"
          % (ok, tot, 100.0 * ok / max(1, tot)))
    for cls in ("three", "mid"):
        tp = cm.get((cls, cls), 0)
        fp = sum(v for (l, p), v in cm.items() if p == cls and l != cls)
        fn = sum(v for (l, p), v in cm.items() if l == cls and p != cls)
        print("  %-5s precision %s   recall %s"
              % (cls,
                 "%.3f (%d/%d)" % (tp / (tp + fp), tp, tp + fp) if tp + fp else "n/a (0/0)",
                 "%.3f (%d/%d)" % (tp / (tp + fn), tp, tp + fn) if tp + fn else "n/a (0/0)"))
    return cm


def live_records(arc_ft, records_dir=S.RECORDS, calibrated=True):
    """The matrix the SHOT RECORDS already carry: every press whose window produced live
    "3"-cell samples (range_cells, written at the plate player_anchor found) AND whose
    banner panel produced a DISTANCE (banner.distance_ft).

    This needs no dump replay -- the cells and the label are both in the JSONL -- so it is
    the whole recorded corpus rather than the handful of sessions whose frames survive.
    """
    clf = sr.ShotRangeClassifier(calibrated=calibrated)
    rows, detail = [], []
    for path in sorted(glob.glob(os.path.join(records_dir, "*.jsonl"))):
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                ft = (rec.get("banner") or {}).get("distance_ft")
                cells = rec.get("range_cells")
                if ft is None or not cells:
                    continue
                res = clf.classify([(c[0], c[1], c[2], c[3]) for c in cells])
                lab = "three" if float(ft) >= arc_ft else "mid"
                rows.append((lab, res.get("range") or "unknown"))
                detail.append(dict(session=os.path.basename(path)[:-6], seq=rec.get("seq"),
                                   feet=round(float(ft), 1), label=lab,
                                   verdict=res.get("range"), evidence=res.get("evidence"),
                                   dark=res.get("dark"), bright=res.get("bright"),
                                   mean=res.get("mean"),
                                   shot_type=rec.get("shot_type")))
    return rows, detail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live-records", action="store_true",
                    help="build the matrix from the recorded range_cells + banner DISTANCE "
                         "in D:/NexusVision/shot_records instead of replaying the 09-12 dump")
    ap.add_argument("--arc-ft", type=float, default=22.0,
                    help="the label boundary. 22.0 = the CORNER three (the shortest shot "
                         "that can be behind the arc); 23.75 = the top of the key")
    ap.add_argument("--join-lo", type=float, default=1.5)
    ap.add_argument("--join-hi", type=float, default=3.4)
    ap.add_argument("--lo-ms", type=float, default=40.0)
    ap.add_argument("--hi-ms", type=float, default=120.0)
    ap.add_argument("--plate-window-ms", type=float, default=900.0,
                    help="how far into the press the anchor may look for the plate")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    if a.live_records:
        rows, detail = live_records(a.arc_ft)
        matrix(rows, a.arc_ft, "LIVE RECORDS (recorded cells vs the banner's DISTANCE)")
        for lab in ("three", "mid"):
            d = [r["dark"] for r in detail if r["label"] == lab and r["dark"] is not None]
            b = [r["bright"] for r in detail
                 if r["label"] == lab and r["bright"] is not None]
            if d:
                print("  %-5s n=%-3d dark p10/p50/p90 %.3f/%.3f/%.3f  bright %.3f/%.3f/%.3f"
                      % (lab, len(d), *np.percentile(d, [10, 50, 90]),
                         *np.percentile(b, [10, 50, 90])))
        print("\n  the sub-arc presses, one line each (the population the verdict must "
              "separate):")
        for r in sorted(detail, key=lambda z: z["feet"]):
            if r["label"] == "mid":
                print("    %5.1f ft -> %-7s dark=%-6s bright=%-6s %s seq=%s %s"
                      % (r["feet"], r["verdict"], r["dark"], r["bright"], r["session"],
                         r["seq"], r["shot_type"]))
        if a.json:
            with open(a.json, "w") as f:
                json.dump(detail, f, indent=1)
            print("wrote", a.json)
        return

    for p in (_REPO, os.path.join(_REPO, "tools", "timing")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import panel_grade as pg

    rows = {r["idx"]: r for r in S._frames_csv(os.path.join(DUMP, "frames.csv"))}
    order = sorted(rows.values(), key=lambda r: r["t"])
    times = [r["t"] for r in order]
    names = {}
    for name in os.listdir(DUMP):
        if name.endswith("_raw.png"):
            names[int(name[1:6])] = os.path.join(DUMP, name)

    presses = []
    with open(S.PRESSES_0912, newline="") as f:
        for r in csv.DictReader(f):
            try:
                presses.append(float(r["press_wall"]))
            except (KeyError, TypeError, ValueError):
                pass
    presses.sort()

    pan = panels(pg)
    print("panels with a readable DISTANCE: %d" % len(pan))

    clf = sr.ShotRangeClassifier()
    A = pa.ANCHOR
    pa.reset_all(keep_identity=False)
    out = []
    for t0, feet, text in pan:
        j = bisect.bisect_left(presses, t0)
        cand = [p for p in presses[max(0, j - 6):j] if a.join_lo <= t0 - p <= a.join_hi]
        if not cand:
            continue
        press = cand[-1]
        A.reset(keep_identity=True)
        pa.ARM.note_press(1, press, "")
        samples = []
        lo = bisect.bisect_left(times, press - 0.02)
        for r in order[lo:lo + 40]:
            ms = (r["t"] - press) * 1000.0
            if ms > a.plate_window_ms:
                break
            img = cv2.imread(names.get(r["idx"], ""))
            if img is None:
                continue
            anc = A.update(img, ts=r["t"], armed=True)
            if anc is not None and a.lo_ms <= ms <= a.hi_ms:
                st = sr.cell_stats(img, anc.icon_x, anc.icon_y, anc.scale)
                if st is not None:
                    samples.append((ms, st[0], st[1], st[2]))
            # teach the anchor exactly what the locator teaches it
            if S.plausible_box(r) and r["conf"] >= 0.90 and r["fill"] >= 20.0:
                A.note_meter(img, r["box"], anc, True)
        res = clf.classify(samples)
        out.append(dict(t0=t0, press=press, feet=feet, text=text,
                        label=("three" if feet >= a.arc_ft else "mid"),
                        verdict=res.get("evidence") or "unknown",
                        n=res.get("samples") or 0,
                        dark=res.get("dark"), bright=res.get("bright")))

    labelled = [r for r in out if r["n"] >= 2]
    print("joined presses: %d   with >= 2 cell samples: %d" % (len(out), len(labelled)))
    cm = {}
    for r in labelled:
        cm[(r["label"], r["verdict"])] = cm.get((r["label"], r["verdict"]), 0) + 1
    print("\n  label \\ verdict   three    mid  unknown")
    for lab in ("three", "mid"):
        print("  %-14s %6d %6d %8d"
              % (lab, cm.get((lab, "three"), 0), cm.get((lab, "mid"), 0),
                 cm.get((lab, "unknown"), 0)))
    ok = sum(v for (l, p), v in cm.items() if l == p)
    tot = sum(cm.values())
    print("\n  agreement %d/%d = %.1f %%  (target 90 %%)"
          % (ok, tot, 100.0 * ok / max(1, tot)))
    for lab in ("three", "mid"):
        d = [r["dark"] for r in labelled if r["label"] == lab and r["dark"] is not None]
        b = [r["bright"] for r in labelled if r["label"] == lab and r["bright"] is not None]
        if d:
            print("  %-5s n=%-3d dark p10/p50/p90 %.3f/%.3f/%.3f  bright %.3f/%.3f/%.3f"
                  % (lab, len(d), *np.percentile(d, [10, 50, 90]),
                     *np.percentile(b, [10, 50, 90])))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(out, f, indent=1)
        print("wrote", a.json)


if __name__ == "__main__":
    main()
