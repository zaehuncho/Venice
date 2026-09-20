#!/usr/bin/env python
"""banner_distance_study.py -- the reproduction harness for banner_distance.read_distance.

Walks the two read-only corpora, rebuilds the digit templates leave-one-PANEL-out, and
prints every accuracy number in banner_distance.py's docstring.  Read-only; writes nothing.

  py tools/diagnostics/banner_distance_study.py
"""
from __future__ import annotations

import base64
import io
import os
import sys
import time

import cv2
import numpy as np

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from banner_distance import (BAND_MIN_ROWS, BORDER_PAD, GH, GLYPH_PAD, GW, NCC_MIN,   # noqa: E402
                             VALUE_PAD, WHITE_S_MAX, WHITE_V_MIN, DistanceVote,
                             _box, _lib, _norm, _runs, distance_thumb, extract, planes,
                             read_distance, segment)

REPO = _REPO
DUMP_0912 = r"D:\NexusVision\framedump\session_20260912_201355"

import csv     # noqa: E402
import glob    # noqa: E402
import re      # noqa: E402

# =========================================================================== measurement
# Everything below is the reproduction harness.  It walks the two read-only corpora and is
# the only part of this file that imports the repo.

# 57 panels of session_20260912_201355, hand-read by eye from 6x crops of the distance cell
# (panel_grade event id -> the string printed in the cell).
LAB0912 = {
    20: "15'5\"", 22: "6'4\"", 26: "15'11\"", 32: "19'6\"", 34: "24'3\"", 45: "8'2\"",
    51: "13'7\"", 55: "25'5\"", 104: "24'1\"", 106: "24'0\"", 108: "19'7\"", 110: "23'11\"",
    112: "19'2\"", 114: "25'5\"", 118: "20'8\"", 120: "24'8\"", 122: "21'1\"", 124: "23'9\"",
    126: "18'10\"", 127: "18'10\"", 129: "26'4\"", 135: "23'9\"", 137: "24'4\"",
    139: "22'4\"", 141: "24'1\"", 143: "20'1\"", 176: "26'9\"", 182: "23'5\"", 188: "26'6\"",
    190: "20'6\"", 192: "21'4\"", 194: "23'9\"", 195: "23'9\"", 197: "22'4\"",
    199: "21'11\"", 202: "7'10\"", 204: "28'3\"", 207: "18'6\"", 209: "22'1\"", 211: "7'6\"",
    213: "23'5\"", 215: "27'0\"", 217: "23'9\"", 219: "18'2\"", 221: "24'1\"", 252: "24'5\"",
    254: "24'9\"", 257: "23'2\"", 261: "25'9\"", 263: "25'3\"", 265: "26'5\"", 267: "16'1\"",
    269: "9'6\"", 274: "20'5\"", 276: "16'6\"", 278: "18'0\"", 282: "22'0\"",
}

# The 09-17 press-window dumps: one label per press window that has the banner up.  Every
# frame in a window shows the SAME panel (checked on the first / middle / last frame of
# each window), so the label applies to all 605 frames.
LAB0917 = {4: "24'9\"", 7: "23'11\"", 12: "18'6\"", 13: "18'6\"", 19: "22'6\"",
           29: "25'0\"", 49: "24'6\"", 50: "18'11\"", 53: "26'2\""}




def _feet(text):
    ft, rest = text.split("'")
    return int(ft) + int(rest.rstrip('"')) / 12.0


def _pg():
    for p in (REPO, os.path.join(REPO, "tools", "timing")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import panel_grade
    return panel_grade


def _cells_0912(pg, every_frame=False):
    """[(ev, strip, distance_cell, label)] for the hand-labelled 09-12 panels.

    The panel FRAMES come from the session's own panel_grade.csv (t0 -> frames.csv row,
    then the event's run of `frames` rows), so this walks the events the validated grader
    already cut -- it is not a second opinion about where the panels are.
    """
    import csv
    frames = list(csv.DictReader(open(os.path.join(DUMP_0912, "frames.csv"), newline="")))
    byt = {round(float(r["t_wall"]), 3): i for i, r in enumerate(frames)}
    out = []
    for r in csv.DictReader(open(os.path.join(DUMP_0912, "panel_grade.csv"), newline="")):
        ev = int(r["ev"])
        if ev not in LAB0912 or not r["cells"]:
            continue
        i0 = byt.get(round(float(r["t0"]), 3))
        if i0 is None:
            continue
        n = int(r["frames"])
        cands = range(i0, i0 + n) if every_frame else (i0 + n // 2, i0 + n // 2 + 1,
                                                       i0 + n - 1, i0)
        for cand in cands:
            if not (0 <= cand < len(frames)):
                continue
            st = pg.strip_of(DUMP_0912, frames[cand])
            if st is None:
                continue
            cells = pg.find_cells(st)
            d = [c for c, role in zip(cells, pg.cell_roles(cells)) if role == "distance"]
            if d:
                out.append((ev, st, d[0], LAB0912[ev]))
                if not every_frame:
                    break
    return out


def _cells_0917(pg):
    """[(seq, strip, distance_cell, label)] for every 09-17 press-window frame with one."""
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    from tools.diagnostics.anchor_acquire_study import index_press_dump, press_windows
    files = index_press_dump()
    out = []
    for sess in ("session_20260917_030402", "session_20260917_030758"):
        for w in press_windows(sess, files):
            if w["seq"] not in LAB0917:
                continue
            for f in w["frames"]:
                st = pg.strip_from_frame(cv2.imread(f["path"]))
                if st is None:
                    continue
                cells = pg.find_cells(st)
                d = [c for c, r in zip(cells, pg.cell_roles(cells)) if r == "distance"]
                if d:
                    out.append((w["seq"], st, d[0], LAB0917[w["seq"]]))
    return out


def _samples(pg, cells):
    """[(key, glyph vector, char)] -- every digit of a labelled panel, keyed by panel."""
    out = []
    for key, st, cell, lab in cells:
        gv, runs, _bb = extract(st, cell)
        if gv is None:
            continue
        digits = [r for r in runs if not r[2]]
        chars = [c for c in lab if c.isdigit()]
        if len(digits) != len(chars):
            print("  !! %s: %d glyph runs for %s" % (key, len(digits), lab))
            continue
        for (ra, re, _t), ch in zip(digits, chars):
            out.append((key, _norm(gv, ra, re), ch))
    return out


def _mean_templates(samples, drop_key=None):
    L = sorted(set(c for _, _, c in samples))
    T = []
    for ch in L:
        v = [g for k, g, c in samples if c == ch and k != drop_key]
        t = np.mean(v, axis=0)
        t -= t.mean()
        T.append(t / np.linalg.norm(t))
    return np.ascontiguousarray(np.array(T, np.float32)), L


def _report(name, res):
    n = len(res)
    if not n:
        print("  %s: nothing to score" % name)
        return
    ok = sum(1 for got, want, _ in res if got == want)
    wrong = [(g, w, k) for g, w, k in res if g and g != w]
    blank = [(g, w, k) for g, w, k in res if not g]
    errs = [abs(_feet(g) - _feet(w)) for g, w, _ in res if g]
    print("  %-42s n=%-4d exact %d/%d = %5.1f%%  WRONG %d  blank(fail-closed) %d" %
          (name, n, ok, n, 100.0 * ok / n, len(wrong), len(blank)))
    if errs:
        e = np.array(errs)
        print("      |ft err| over the %d non-blank reads: max %.3f  mean %.4f  p95 %.3f"
              % (len(errs), e.max(), e.mean(), np.percentile(e, 95)))
    for g, w, k in wrong[:10]:
        print("      WRONG %-9s want %-9s got %s" % (k, w, g))
    seen = set()
    for g, w, k in blank:
        if k not in seen:
            seen.add(k)
            print("      blank %-9s want %-9s (%d frames)"
                  % (k, w, sum(1 for _, _, k2 in blank if k2 == k)))


def main():
    pg = _pg()
    print("=" * 84)
    print("A.  ROI")
    print("=" * 84)
    c12 = _cells_0912(pg)
    xs = [(c["x0"], c["y0"], c["x1"], c["y1"]) for _, _, c, _ in c12]
    print("  strip  = panel_grade Y %d..%d  X %d..%d of the normalised 1280x720 frame"
          % (pg.Y0, pg.Y1, pg.X0, pg.X1))
    print("  cell   = STRIP x %d..%d  y %d..%d   (w %d..%d, h %d..%d)" % (
        min(b[0] for b in xs), max(b[2] for b in xs),
        min(b[1] for b in xs), max(b[3] for b in xs),
        min(b[2] - b[0] for b in xs), max(b[2] - b[0] for b in xs),
        min(b[3] - b[1] for b in xs), max(b[3] - b[1] for b in xs)))
    print("         = FRAME x %d..%d  y %d..%d" % (
        pg.X0 + min(b[0] for b in xs), pg.X0 + max(b[2] for b in xs),
        pg.Y0 + min(b[1] for b in xs), pg.Y0 + max(b[3] for b in xs)))

    print()
    print("=" * 84)
    print("C.  ACCURACY")
    print("=" * 84)
    s12 = _samples(pg, c12)
    print("  09-12 glyph corpus: %d digits over %d panels  %s" % (
        len(s12), len(c12),
        {ch: sum(1 for _, _, c in s12 if c == ch) for ch in sorted(set(c for _, _, c in s12))}))

    # ---- 09-12 leave-one-PANEL-out: the templates never see the panel being scored.
    res, derr = [], 0
    for ev, st, cell, lab in c12:
        T, L = _mean_templates(s12, drop_key=ev)
        res.append((read_distance(st, cell, templates=(T, L))[0], lab, "ev%d" % ev))
        for k, g, ch in s12:
            if k == ev:
                derr += int(L[int(np.argmax(T @ g))] != ch)
    print("  09-12 per-DIGIT, leave-one-panel-out: %d wrong of %d" % (derr, len(s12)))
    _report("09-12 LOPO, 57 hand-labelled panels", res)

    # ---- 09-12 every frame of those events, with the SHIPPED blob (in-sample templates)
    all12 = _cells_0912(pg, every_frame=True)
    r12 = [(read_distance(st, c)[0], lab, "ev%d" % ev) for ev, st, c, lab in all12]
    _report("09-12 ALL frames of those events (in-sample)", r12)
    v12 = {}
    for got, lab, k in r12:
        v12.setdefault((k, lab), DistanceVote()).add(got)
    okv = sum(1 for (k, lab), v in v12.items() if v.best()[0] == lab)
    print("  09-12 per-PANEL DistanceVote over those frames: %d/%d" % (okv, len(v12)))

    # ---- 09-17: held out.  Templates are 09-12 only; another session, JPEG, 2-cell panels.
    c17 = _cells_0917(pg)
    npan = len(set(s for s, _, _, _ in c17))
    _report("09-17 HELD OUT (%d panels, JPEG)" % npan,
            [(read_distance(st, c)[0], lab, "seq%d" % s) for s, st, c, lab in c17])
    vote = {}
    for s, st, c, lab in c17:
        got = read_distance(st, c)[0]
        vote.setdefault((s, lab), {}).setdefault(got, 0)
        vote[(s, lab)][got] += 1
    pv = 0
    for (s, lab), cnt in sorted(vote.items()):
        cand = {g: n for g, n in cnt.items() if g}
        top = max(cand, key=cand.get) if cand else ""
        pv += int(top == lab)
        print("      seq%-3d want %-9s vote %-9s %s   %s"
              % (s, lab, top or "(none)", "OK " if top == lab else "BAD", cnt))
    print("  09-17 per-PANEL majority vote: %d/%d" % (pv, len(vote)))

    print()
    print("=" * 84)
    print("D.  COST")
    print("=" * 84)
    strips = [(st, c) for _, st, c, _ in c12]
    pls = [planes(st) for st, _ in strips]
    REP = 60
    for label, fn in (("read_distance, planes handed in",
                       lambda st, c, pl: read_distance(st, c, planes_=pl)),
                      ("read_distance, planes computed here",
                       lambda st, c, pl: read_distance(st, c)),
                      ("planes() alone (the caller already pays this)",
                       lambda st, c, pl: planes(st))):
        t0 = time.perf_counter()
        for _ in range(REP):
            for (st, c), pl in zip(strips, pls):
                fn(st, c, pl)
        print("  %-46s %.4f ms" % (label,
                                   (time.perf_counter() - t0) / (REP * len(strips)) * 1000.0))

    print()
    print("=" * 84)
    print("F.  FALLBACK COST (thumbnail -- only if the read were ever not trusted)")
    print("=" * 84)
    import json
    th, stt = distance_thumb(*strips[0])
    print("  thumb %s  stats %s" % (th.shape, stt))
    print("  JSON, int list : %d bytes" % len(json.dumps({"distance_thumb": th.ravel().tolist(),
                                                          "distance_stats": stt})))
    print("  JSON, base64   : %d bytes" % len(json.dumps(
        {"distance_thumb_b64": base64.b64encode(th.tobytes()).decode(),
         "distance_stats": stt})))
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ===========================================================================
# WIRING -- what to paste into banner_verdict_live.py (NOT done by this file)
# ===========================================================================
#
# 1) import, beside the other imports at the top of banner_verdict_live.py:
#
#        from distance_read import read_distance, DistanceVote
#
#    Ship it the way panel_grade ships: add the module to tools/sidecar_bundle_manifest.py
#    as a READER SOURCE input so Nuitka compiles it into OrionSidecar.exe.  It needs NO
#    data gate -- the templates live inside the module, so there is no npz to lose.
#
# 2) banner_verdict_live.read_cells() (line ~401).  The cell dict it builds carries a box
#    but no pixels, so read_strip cannot reach the cell.  Attach the raw panel_grade cell:
#
#        out.append(dict(role=role, color=c['color'],
#                        box=(c['x0'], c['y0'], c['x1'], c['y1']),
#                        cell=c,                                     # <-- ADD
#                        mask=_cell_word_mask(pg, planes, c) if role != 'distance' else None))
#
#    (`cell=c` is belt and braces -- read_distance accepts either dict shape, so passing
#     the existing dict with its `box` key would work too.)
#
# 3) banner_verdict_live.read_strip() (line ~415).  Seed the two keys in the `out = dict(...)`
#    literal, beside distance_color:
#
#        distance='', distance_ft=-1.0,
#
#    and replace the distance branch of the `for c in cells:` loop
#
#        if c['role'] == 'distance':
#            if not out['distance_color']:
#                out['distance_color'] = c['color']
#            continue
#
#    with
#
#        if c['role'] == 'distance':
#            if not out['distance_color']:
#                out['distance_color'] = c['color']
#            if not out['distance']:
#                txt, ft, _dbg = read_distance(strip, c['cell'], planes_=planes)
#                if txt:
#                    out['distance'], out['distance_ft'] = txt, round(float(ft), 3)
#            continue
#
# 4) banner_verdict_live._flush() (line ~888).  Two more keys in `payload`, beside
#    'distance_color':
#
#        'distance': ev['distance'],
#        'distance_ft': float(ev['distance_ft']),
#
# 5) banner_verdict_live._better() (line ~843) decides which sample of one appearance is
#    kept.  It currently reads
#
#        return (bool(a['coverage']), a['ncc']) > (bool(b['coverage']), b['ncc'])
#
#    Append the distance LAST, so the existing ordering is untouched whenever both samples
#    agree about it:
#
#        return (bool(a['coverage']), a['ncc'], bool(a['distance'])) > \
#               (bool(b['coverage']), b['ncc'], bool(b['distance']))
#
#    _complete() must NOT change: a distance that fails to read has to be allowed to hold
#    nothing back -- the panel's timing word is still what makes a read complete.
#
# 6) OPTIONAL but recommended -- the modal read of the appearance.  Three frames in 1845
#    are encoder-ghosted into a wrong but confident read, and no per-frame gate separates
#    them (their NCC and margin sit inside the correct reads' range).  The banner is
#    sampled 10-20 times per appearance, so the mode fixes it.  In BannerVerdictLive:
#
#      __init__ (line ~498):   self._dist = DistanceVote()
#      _arm()   (line ~827):   self._dist.reset()
#          _arm is the one place that owns every per-appearance field, and it is called on
#          the up-edge, on a panel-event boundary, and after a flush -- exactly the three
#          places the vote must start over.  The down-edge path flushes BEFORE it calls
#          _arm(0.0, armed=False), so the flush still sees the votes.
#      process() (line ~793, immediately AFTER the `run_sig` / boundary block and BEFORE
#          the `if ev['timing'] in ('', 'UNKNOWN'): return` line, so a sample whose timing
#          word is not yet legible still contributes its distance):
#                              self._dist.add(ev['distance'])
#      _flush() (line ~888), replacing the two payload keys from step 4:
#                              txt, votes, total = self._dist.best()
#                              'distance': txt,
#                              'distance_ft': (self._dist.feet() or -1.0),
#                              'distance_votes': '%d/%d' % (votes, total),
#
#    Measured: per FRAME 1842/1845 = 99.84 %; per PANEL with the vote 66/66.
#
# 7) panel_grade offline (optional, the same two calls): attach `cell=c` in read_cells(),
#    set e['distance'] / e['distance_ft'] from the distance cell in match_event(), and add
#    the two columns to the CSV writer in main().
