#!/usr/bin/env python
"""banner_distance.py -- read the DISTANCE cell of the NBA 2K27 post-shot banner.

SELF-CONTAINED.  `read_distance()` imports nothing from this repo and loads no asset file:
the digit templates are the base64 npz in `_TEMPLATES` below.  (Only `main()`, the
accuracy harness, imports panel_grade / anchor_acquire_study to walk the corpora.)

WHAT THE CELL LOOKS LIKE (720p, measured on two corpora)
-------------------------------------------------------
The panel strip is panel_grade's fixed HUD band Y 12..52, X 430..870 of a 1280x720 frame.
Inside it the DISTANCE cell is always the RIGHTMOST cell and always reads `white`:

    3-cell TIMING|COVERAGE|DISTANCE : cell box x 269..390, y 6..34/35   (strip coords)
    2-cell TIMING|DISTANCE          : cell box x 209..330, y 6..34/35
    -> full-frame 720p:  x 699..820  or  x 639..760,  y 18..46/47.  121-122 x 28-29 px.

The cell carries a grey label line "DISTANCE" (inner rows ~1..8), one blank row, then a
pure-white value line `23'5"`, 7 px tall at V>190 (9 px at V>140).  The value is CENTRED,
so its x moves with the string width -- nothing here is a fixed x window, and the read is
driven entirely off the cell box the caller already has.

THE READ
--------
1. inner box = cell box + panel_grade's own BORDER_PAD (x) / VALUE_PAD (y).
2. white mask = (S < 60) & (V > 190)   -- panel_grade.WHITE_TEXT_V, unchanged.
3. row runs of that mask; the LAST run >= 5 rows tall is the VALUE band.  This is exactly
   the band panel_grade.cell_word_mask already isolates -- it just throws it away for a
   distance cell (`mask=... if role != 'distance' else None`).
4. column runs of the band are the glyphs.  A run is a TICK MARK (' or ") when its ink
   stops inside the top 55 % of the band AND it is <= 4 px wide.  That test was 57/57 on
   the 09-12 corpus and it is what makes the read STRUCTURAL rather than positional: the
   first tick ends FEET, the digit runs after it are INCHES, trailing ticks are the inch
   mark (the `"` is 1 or 2 column runs depending on sub-pixel phase; both are handled).
   RECOVERY: on a soft / JPEG-compressed frame the foot mark can drop below V>190 while
   the digits survive (19 frames of the 09-17 corpus).  With the inch mark still present
   and no separator before the last digit, feet|inches is split at the WIDEST inter-digit
   gap, which is exactly the hole the apostrophe left (4 px vs 1-2 px elsewhere).
5. each digit run -> (band rows +/-1) x (run cols +/-1) of the V plane, resized to 12x10,
   zero-mean unit-norm, dotted against 10 class-mean templates.  A dot product of two
   zero-mean unit vectors IS TM_CCOEFF_NORMED at a single position, i.e. the same number
   panel_grade's word matcher reports.  The one-column horizontal pad matters: it keeps
   the glyph's anti-aliased shoulder, and without it a JPEG-softened 6 reads as an 8
   (that single confusion is the whole difference between 90.1 % and 100 % below).
6. sanity gates, then text = "<ft>'<in>\"" and feet = ft + in/12.

FAIL-CLOSED.  No band, no inch mark, >2 digits in a group, an ambiguous split, inches > 11,
feet > 94, a leading zero, or any digit under NCC 0.70 -> ("", None, dbg) with
dbg["reason"] set.  It never guesses; an unreadable panel simply carries no distance.

MEASURED -- main() reproduces every number
------------------------------------------
09-12 session_20260912_201355, 57 hand-labelled panels, LEAVE-ONE-PANEL-OUT templates
      (the templates never see the panel being scored)   57/57 exact, 172/172 digits
09-12 all 1240 frames of those 57 events, fade-in/out frames included  1237/1240 = 99.8 %
      The 3 misses are 3 encoder-ghosted frames of ONE event (f447/453/455 of ev20, a
      plain 15'5" smeared into 18'8").  They cannot be gated away: they score NCC 0.868
      and margin 0.031, INSIDE the correct reads' range (min 0.800 / 0.0005).  What kills
      them is agreement across samples -- see `DistanceVote` and WIRING step 6: the modal
      read of the appearance is 21-3 in favour of the truth.
09-17 the press-window dumps, 605 frames / 9 hand-labelled panels, JPEG, templates from
      09-12 ONLY -- a held-out corpus                    605/605 exact, 0 wrong, 9/9 panels
      1842 of 1845 frames over both corpora = 99.84 % per FRAME; 66/66 per PANEL.
Cost  ~0.21 ms per cell on a strip the caller already has (main() times it).

HONESTY NOTE ON TUNING.  Every normalisation variant swept (canvas 8x10..16x20, AREA /
LINEAR / CUBIC, pad 0 or 1) scores 57/57 on the 09-12 leave-one-panel-out test, so 09-12
could not choose between them.  The 1-px horizontal pad was chosen because the 09-17
corpus separates it; the 09-17 number is therefore held out for the TEMPLATES but not for
that one hyper-parameter.  With pad 0 the 09-17 number was 545/605 = 90.1 % with 41 wrong
reads (a JPEG-softened 6 read as an 8) and 19 fail-closed blanks; with pad 1 plus the
foot-mark recovery of step 4 it is 605/605 with zero wrong reads.

WIRED INTO banner_verdict_live.read_strip (the  cell branch) -> the
banner_verdict payload's distance / distance_ft -> the shot record's
banner block.  The reproduction harness is tools/diagnostics/banner_distance_study.py.
"""
from __future__ import annotations

import base64
import io
import os
import sys
import time

import cv2
import numpy as np

# ------------------------------------------------------------------ geometry / thresholds
# Copied from tools/timing/panel_grade.py so read_distance() stands alone.  The cell BOX
# comes from the caller, so only the two pads and the white threshold are duplicated.
BORDER_PAD = 5           # panel_grade.BORDER_PAD   (x step inside the cell border)
VALUE_PAD = 3            # panel_grade.VALUE_PAD    (y step inside the cell border)
WHITE_S_MAX = 60         # panel_grade: white text is (s < 60) & (v > WHITE_TEXT_V)
WHITE_V_MIN = 190        # panel_grade.WHITE_TEXT_V
S_MIN, V_MIN = 110, 110  # panel_grade.S_MIN / V_MIN -- only used by the local planes()

BAND_MIN_ROWS = 5        # panel_grade.cell_word_mask's own row-group floor
GH, GW = 12, 10          # normalised glyph canvas
GLYPH_PAD = 1            # columns of context each side of a glyph run
TICK_W_MAX = 4           # a ' or " is at most this wide ...
TICK_BOT_FRAC = 0.55     # ... and its ink stops in the top 55 % of the band
NCC_MIN = 0.70           # per-digit acceptance; the 09-12 5th percentile is 0.98
GAP_MIN = 3              # a recovered foot mark must have left a hole at least this wide
MAX_FEET = 94            # a shot cannot be longer than the court
MAX_INCHES = 11

_TEMPLATES = (
    "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"
    "ZgAAAGYSAAAAAA=="
)

_LIB = None


def _lib():
    """(T_flat, labels): 10 class-mean digit templates, zero-mean unit-norm."""
    global _LIB
    if _LIB is None:
        z = np.load(io.BytesIO(base64.b64decode(_TEMPLATES)))
        T = z["T"].astype(np.float32)
        _LIB = (np.ascontiguousarray(T.reshape(len(T), -1)), [str(x) for x in z["L"]])
    return _LIB


def planes(strip):
    """panel_grade.planes(), so this file runs standalone.  The live reader already has
    (h, s, v, sat) -- hand it in as `planes_=` and this is never called."""
    h, s, v = cv2.split(cv2.cvtColor(strip, cv2.COLOR_BGR2HSV))
    return h, s, v, (s > S_MIN) & (v > V_MIN)


def _runs(b):
    """panel_grade._runs: [(start, end)] of the True runs of a 1-D bool array."""
    b = np.asarray(b, bool)
    if not b.any():
        return []
    d = np.diff(np.concatenate(([0], b.astype(np.int8), [0])))
    return list(zip(np.nonzero(d == 1)[0].tolist(), np.nonzero(d == -1)[0].tolist()))


def _norm(gv, a, b):
    """One glyph -> zero-mean unit-norm GH x GW vector."""
    a = max(0, a - GLYPH_PAD)
    b = min(gv.shape[1], b + GLYPH_PAD)
    g = cv2.resize(gv[:, a:b], (GW, GH), interpolation=cv2.INTER_AREA).astype(np.float32)
    g -= g.mean()
    n = float(np.linalg.norm(g))
    return (g / n if n > 1e-6 else g).reshape(-1)


def _box(cell):
    """panel_grade's {x0,y0,x1,y1} cell OR banner_verdict_live's {'box': (x0,y0,x1,y1)}."""
    if "box" in cell:
        x0, y0, x1, y1 = cell["box"]
        return int(x0), int(y0), int(x1), int(y1)
    return int(cell["x0"]), int(cell["y0"]), int(cell["x1"]), int(cell["y1"])


def extract(strip_bgr, cell, planes_=None):
    """(gv, runs, band_box) for one distance cell, or (None, reason, None).

    gv   : float32 V-plane crop of the value band, one row of context each way.
    runs : [(col0, col1, is_tick)] left to right, in gv columns.
    """
    if planes_ is None:
        planes_ = planes(strip_bgr)
    _h, s, v, _sat = planes_
    x0, y0, x1, y1 = _box(cell)
    ix0, ix1 = x0 + BORDER_PAD, x1 - BORDER_PAD
    iy0, iy1 = y0 + VALUE_PAD, y1 - VALUE_PAD
    if iy1 - iy0 < 6 or ix1 - ix0 < 20:
        return None, "cell too small", None
    m = ((s < WHITE_S_MAX) & (v > WHITE_V_MIN))[iy0:iy1, ix0:ix1]
    groups = [(a, e) for a, e in _runs(m.sum(axis=1) > 0) if e - a >= BAND_MIN_ROWS]
    if not groups:
        return None, "no value band", None
    a, e = groups[-1]
    band = m[a:e]
    H = band.shape[0]
    gv = v[iy0 + max(0, a - 1):iy0 + min(m.shape[0], e + 1), ix0:ix1].astype(np.float32)
    runs = []
    for ra, re in _runs(band.sum(axis=0) > 0):
        rr = np.nonzero(band[:, ra:re].sum(axis=1) > 0)[0]
        if rr.size == 0:
            continue
        runs.append((int(ra), int(re),
                     bool(int(rr.max()) <= H * TICK_BOT_FRAC and (re - ra) <= TICK_W_MAX)))
    return gv, runs, (int(iy0 + a), int(iy0 + e), int(ix0), int(ix1))


def segment(runs):
    """(feet_runs, inch_runs, recovered) or (None, reason, False).

    The first tick before the last digit is the FOOT mark; everything before it is feet,
    everything after it that is not a tick is inches.  When the foot mark has faded out of
    the white mask, the widest inter-digit gap stands in for it (see module docstring).
    """
    digits = [r for r in runs if not r[2]]
    ticks = [i for i, r in enumerate(runs) if r[2]]
    if not digits:
        return None, "no digits", False
    if not ticks:
        return None, "no tick marks", False
    last_digit = max(i for i, r in enumerate(runs) if not r[2])
    sep = next((i for i in ticks if i < last_digit), None)
    recovered = False
    if sep is not None:
        feet = [r for r in runs[:sep] if not r[2]]
        inch = [r for r in runs[sep + 1:] if not r[2]]
    else:
        if len(digits) < 2:
            return None, "no foot mark", False
        gaps = [digits[i + 1][0] - digits[i][1] for i in range(len(digits) - 1)]
        k = int(np.argmax(gaps))
        rest = sorted(gaps, reverse=True)[1:]
        if gaps[k] < GAP_MIN or (rest and gaps[k] <= rest[0]):
            return None, "no foot mark (ambiguous gap)", False
        feet, inch, recovered = digits[:k + 1], digits[k + 1:], True
    if not feet or not inch:
        return None, "empty feet or inches", False
    if len(feet) > 2 or len(inch) > 2:
        return None, "too many digits", False
    return feet, inch, recovered


def read_distance(strip_bgr, cell, planes_=None, templates=None):
    """Read one DISTANCE cell.

    strip_bgr : the 40x440 panel strip (panel_grade.strip_from_frame output).
    cell      : a panel_grade cell dict -- {x0,y0,x1,y1,color} or {'box': (x0,y0,x1,y1)}.
    planes_   : the (h, s, v, sat) tuple the caller already computed, or None.
    templates : (T_flat, labels) override -- used by the leave-one-out harness in main().

    -> (text, feet, dbg).  e.g. ("23'5\"", 23.4166..., {...}), or ("", None, dbg) when the
    read is not certain; dbg["reason"] says why.
    """
    dbg = {"reason": ""}
    gv, runs, bandbox = extract(strip_bgr, cell, planes_)
    if gv is None:
        dbg["reason"] = runs
        return "", None, dbg
    dbg["band"] = bandbox
    dbg["runs"] = runs
    feet, inch, recovered = segment(runs)
    if feet is None:
        dbg["reason"] = inch
        return "", None, dbg
    dbg["recovered_foot_mark"] = bool(recovered)

    T, L = templates if templates is not None else _lib()
    out, sc = [], []
    for group in (feet, inch):
        digits = ""
        for ra, re, _t in group:
            r = T @ _norm(gv, ra, re)
            k = int(np.argmax(r))
            sc.append(float(r[k]))
            if r[k] < NCC_MIN:
                dbg["reason"] = "digit ncc %.3f < %.2f" % (r[k], NCC_MIN)
                dbg["ncc"] = sc
                return "", None, dbg
            digits += L[k]
        out.append(digits)
    dbg["ncc"] = sc
    dbg["ncc_min"] = min(sc)
    if len(out[0]) == 2 and out[0][0] == "0":
        dbg["reason"] = "leading zero in feet"
        return "", None, dbg
    ft, inches = int(out[0]), int(out[1])
    if ft > MAX_FEET or inches > MAX_INCHES:
        dbg["reason"] = "out of range %d'%d" % (ft, inches)
        return "", None, dbg
    return "%d'%d\"" % (ft, inches), ft + inches / 12.0, dbg


# ------------------------------------------------------------------------- fallback (F)
def distance_thumb(strip_bgr, cell, planes_=None):
    """(thumb 12x32 uint8, stats) -- the crop-stats + normalised-thumbnail fallback the
    task allows if the glyph read is ever not trusted.  It is six lines and costs nothing,
    so it ships beside the read rather than instead of it.  See FALLBACK COST in main()."""
    if planes_ is None:
        planes_ = planes(strip_bgr)
    _h, s, v, _sat = planes_
    x0, y0, x1, y1 = _box(cell)
    ix0, ix1 = x0 + BORDER_PAD, x1 - BORDER_PAD
    iy0, iy1 = y0 + VALUE_PAD, y1 - VALUE_PAD
    m = ((s < WHITE_S_MAX) & (v > WHITE_V_MIN))[iy0:iy1, ix0:ix1]
    groups = [(a, e) for a, e in _runs(m.sum(axis=1) > 0) if e - a >= BAND_MIN_ROWS]
    if not groups:
        return None, {}
    a, e = groups[-1]
    band = m[a:e]
    cols = np.nonzero(band.sum(axis=0) > 0)[0]
    if cols.size == 0:
        return None, {}
    crop = v[iy0 + a:iy0 + e, ix0 + int(cols.min()):ix0 + int(cols.max()) + 1]
    th = cv2.resize(np.asarray(crop, np.uint8), (32, 12), interpolation=cv2.INTER_AREA)
    st = dict(w=int(cols.max() - cols.min() + 1), h=int(e - a), ink=int(band.sum()),
              x=int(ix0 + int(cols.min())), y=int(iy0 + a))
    return th, st


class DistanceVote:
    """Modal distance over the samples of ONE banner appearance.

    The three wrong reads in 1845 frames are encoder-ghosted single frames whose NCC and
    top1-top2 margin sit INSIDE the correct reads' range, so no per-frame gate removes
    them -- but the banner is sampled ~10-20 times per appearance and the ghosting is not
    repeatable, so the mode is right where a single frame is not (21-3 on the one event
    that has them).  Reset it on the panel's up-edge, add() every sample, read best() at
    flush.  Costs one dict update per sample.
    """

    __slots__ = ("_n",)

    def __init__(self):
        self._n = {}

    def reset(self):
        self._n = {}

    def add(self, text):
        if text:
            self._n[text] = self._n.get(text, 0) + 1

    def best(self):
        """(text, votes, total) -- ('', 0, 0) when nothing read."""
        if not self._n:
            return "", 0, 0
        t = max(self._n, key=self._n.get)
        return t, self._n[t], sum(self._n.values())

    def feet(self):
        t = self.best()[0]
        if not t:
            return None
        ft, rest = t.split("'")
        return int(ft) + int(rest.rstrip('"')) / 12.0
