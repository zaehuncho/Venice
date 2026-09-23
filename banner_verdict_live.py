#!/usr/bin/env python
"""LIVE shot-verdict reader: the game's own feedback banner, read on the CV frames.

WHY THIS EXISTS
---------------
The owner tunes the release-timing slider by reading NBA 2K27's own shot-feedback panel
(TIMING: EXCELLENT / EARLY / LATE ... | COVERAGE: WIDE OPEN / LIGHT CONTEST ...) BY EYE,
mid-session, and cannot hold the tally in his head -- "can't tell if I found my value or
not because sometimes it's green".  ``tools/timing/panel_grade.py`` already grades that
panel offline and is the ONLY validated timing instrument in this project (28/28 and 51/51
against a hand grade on framedump sessions).  This module runs that SAME reader live, on
the same normalised 1280x720 BGR frame the meter detector gets, and streams one
``{"event":"banner_verdict",...}`` line per banner appearance to the native launcher.

EXACTNESS
---------
Nothing about the reading is re-implemented here.  The strip geometry, HSV thresholds,
cell splitter, 3-cell role logic, word fingerprint and the NCC acceptance bar all come
from ``panel_grade`` by import.  The one substitution is the template MATCHER: panel_grade
calls ``cv2.matchTemplate(TM_CCOEFF_NORMED)`` once per template (29 calls, ~2.8 ms) which
is far too slow for a live loop.  ``_Matcher`` precomputes each template's zero-mean unit
vector and scores all 29 with a single matrix-vector product -- TM_CCOEFF_NORMED at a
single position IS the Pearson correlation, so this is the same number to float rounding
(``tests/test_banner_verdict_live.py::test_matcher_agrees_with_panel_grade`` pins it).

COST
----
The CV loop must never pay for this.  ``submit()`` runs on the detect thread and does one
strip crop + 53 KB copy (~0.02 ms) every ``stride``-th unique frame; a daemon worker thread
with a latest-wins single-slot mailbox does the HSV/cell/NCC work.  The worker itself
short-circuits on panel_grade's own panel gate (``dark > PANEL_DARK_MIN``, ~0.07 ms) so a
plain court frame never reaches the cell splitter.

EDGE TRIGGER (one event per banner APPEARANCE)
----------------------------------------------
Panel events are cut exactly where ``panel_grade.events_from`` cuts them: a run lasts while
the strip's colour CLASS and cell LAYOUT hold, and ONE no-panel sample ends it.  In rapid
fire the game separates two panels with its white inter-panel flash rather than with blank
frames, and one boundary in the 18:53 drill was a single sample, so anything laxer merges
back-to-back shots.  Within an appearance the reader keeps the best sample and emits as
soon as a COMPLETE read repeats ``_SIG_CONFIRM`` times -- ~1 sample after the panel first
becomes legible, not when it finally leaves the screen.

COMPLETE IS A PROPERTY OF THE LAYOUT
------------------------------------
The 2-cell ``TIMING | DISTANCE`` panel has no coverage cell at all (panel_grade.cell_roles
names a white second cell ``distance``), and all 30 panels of the 2026-09-15 18:53 drill
were that layout.  The first cut of this module waited for a coverage word before flushing,
so in that layout it never flushed until the panel's DOWN edge: emits landed 2.5-3 s after
onset and back-to-back panels merged -- 16 of 30 panels reported.  ``_complete()`` now asks
what this panel CAN carry, so a 2-cell panel is done when its timing word resolves and only
a panel that really has a coverage cell waits for one.  Replayed over the drill's 824
frames: 30/30 panels, 0 phantom, onset->emit median 113 ms / max 233 ms (the two panels
panel_grade itself grades UNKNOWN take longer, because the word only clears the NCC bar
late).

THE 2026-09-15 STATIC-PANEL REGRESSION
--------------------------------------
Between 22:36:20Z and 22:36:59Z a live session sat on a shot-feedback/replay screen with
NO bot presses at all and the reader emitted 16 ``timing=EXCELLENT coverage=WIDE OPEN``
verdicts, one every 1.6-2.0 s, cycling through the same 3-4 ncc values: the SAME static
panel, re-read.  The mechanism was a TIME debounce -- the panel dropped out of the reader
for a sample or two every ~2 s, which re-armed the state machine, and the only thing left
standing between that and an event was ``debounce_ms`` = 1.5 s, which a stuck screen simply
outwaits.  Every repeat also became a customer-facing ``Shot: EXCELLENT - WIDE OPEN``
activity line and a ``ShotVerdictTally`` increment.

The guard is no longer a timer.  A verdict with the same WORD and COLOUR as the last one is
reported again only when the panel event really ended AND a NEW BOT RELEASE stands behind
it; with no release at all, a screen can sit there forever and produce exactly one verdict.
(``require_release=0`` has no release to test, so it falls back to ``debounce_ms``.)

ATTRIBUTION (whose shot was this?)
----------------------------------
A feedback panel is only the OWNER'S tally when the bot actually released a shot into it.
``note_release()`` is fed every engine release edge -- the normal ``shot_gate_release``
and the ``METER BACKSTOP`` release, which is also a real bot shot -- and the reader binds
each panel ONSET to the most recent unused release ``ATTR_MIN_MS..ATTR_MAX_MS``
(0.4-2.6 s) before it.  Replayed over the 18:53 drill, release -> panel ONSET is
995-1744 ms (n=30, median 1218), and the reader's 30 joins are IDENTICAL to the 28
panel_grade itself made (it left 2 unjoined; those attribute at 1.25 s and 1.74 s).  Each
release is consumed once, so one release can never mint two tally entries.

The onset, never the emit, is what the window tests.  Release -> EMIT reaches 3267 ms on
that drill, well past the window: testing the emit would throw the verdict away.

An unattributed verdict is NOT forwarded to the native: no activity line, no tally.  It is
logged at ERROR (not WARNING: the native relay throttles sidecar WARNINGs to 1/s and would
hide it) as ``BANNER VERDICT UNATTRIBUTED``, so a replay-screen storm is still fully
visible in the session log.  ``ORION_BANNER_VERDICT_REQUIRE_RELEASE=0`` forwards
everything again, for a session where the owner is shooting by hand.

FAIL-SOFT
---------
``panel_grade`` DOES ship: ``tools/sidecar_bundle_manifest.py`` lists it as a READER SOURCE
input (copied to the repo-root name ``orion_panel_grade`` and compiled INTO OrionSidecar.exe
by Nuitka, per docs/IP_PROTECTION_PLAN.md -- a reader never ships as readable source), and
``tools/timing/panel_templates.npz`` is a build-gated DATA input, because shipping the module
without the library would be a silent feature-off.  ``load_panel_grade()`` tries the compiled
module name first and falls back to the repo path for a dev run.
Every failure mode -- module missing, npz missing, empty library, an exception in the
worker -- still disables the reader for the session after exactly one WARNING and leaves the
detect loop byte-identical.  Gate: ``ORION_BANNER_VERDICT_LIVE`` (default 1, SHIP CONFIG
2026-09-17; see docs/SHIP_CONFIG.md).
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import sys
import threading
import time

import cv2
import numpy as np

try:                                  # optional: the panel's DISTANCE cell reader
    import banner_distance as _bd
except Exception:                     # pragma: no cover - a missing reader is not fatal
    _bd = None

logger = logging.getLogger('BannerVerdictLive')

# Every 6th unique frame ~= 10 Hz at 60 fps capture. A banner is up ~1-2 s, so this still
# samples it 10-20 times; the reader only needs ONE clean look.
DEFAULT_STRIDE = 6
# Fallback repeat guard for require_release=0 only: "the same verdict within 1.5 s is the
# same banner". With attribution on, a new RELEASE is the test instead -- a timer is
# something a stuck replay screen outwaits, and that is exactly what it did on 2026-09-15.
DEFAULT_DEBOUNCE_MS = 1500.0
# Consecutive no-panel samples before the banner counts as GONE. ONE, which is
# panel_grade.events_from's own rule (`panel[i]` false for a single frame ends the run):
# in the 18:53 drill two consecutive EXCELLENT panels were separated by exactly one
# no-panel sample, and a threshold of two merged them and lost the second shot. The
# double-count this used to guard against is now handled where it belongs -- the repeat
# guard in _flush, which needs a NEW RELEASE before the same verdict can be reported twice.
_DOWN_RUN_GONE = 1
# Consecutive no-panel samples that CLEAR the last emitted words, i.e. the panel really left
# the screen rather than dropping out of the reader for a sample.
#
# 2026-09-15 18:53 drill, 30 panels over 102 s graded by panel_grade: the BLANK gap between
# consecutive panels is 188 ms min / 437 ms median on an ~113 ms sampling cadence, and one
# boundary is a SINGLE sample. The first cut of this used 5 samples (~0.5 s) and merged
# back-to-back panels wholesale: 16 of 30 reported. It tracks _DOWN_RUN_GONE, because the
# thing that stops a persisting panel being re-reported is the release test, not a timer a
# stuck replay screen can simply outwait.
_CLEAR_RUN = 1
# Consecutive samples a COMPLETE read must repeat before it is emitted. One sample is a
# misread (the panel fades in); two is the panel. At ~100 ms per sample this fires ~1 sample
# after the panel is first legible instead of waiting for its DOWN edge 2.8 s later.
_SIG_CONFIRM = 2
# Release -> banner window. The 2026-09-15 session measured 790-1139 ms over 12 bot shots
# (median 996, sd 93); this is that distribution with ~4 sd of headroom each way, and it
# still excludes the 3.6 s+ gaps a replay screen leaves behind.
DEFAULT_ATTR_MIN_MS = 400.0
DEFAULT_ATTR_MAX_MS = 2600.0
# How many release edges to keep. Shots land ~2.5-3 s apart and the attribution window is
# 2.6 s wide, so 8 is several seconds of slack over anything that can still match.
_RELEASE_RING = 8


def _env_on(name: str, default: str = '1') -> bool:
    v = (os.environ.get(name, default) or '').strip().lower()
    return v not in ('', '0', 'false', 'no', 'off')


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.environ.get(name, '') or '').strip() or default)
    except (TypeError, ValueError):
        return float(default)


# --------------------------------------------------------------------------- panel_grade
# The template library, when panel_grade's own LIB_PATH does not resolve. A COMPILED
# orion_panel_grade has no readable source file beside it, so its
# LIB_PATH = <dir of __file__>/panel_templates.npz can point at a path Nuitka never
# created; the npz ships as data instead and is found here.
_LIB_NAME = 'panel_templates.npz'


def _library_roots():
    here = os.path.dirname(os.path.abspath(__file__))
    exe = os.path.dirname(os.path.abspath(getattr(sys, 'executable', '') or ''))
    for root in (here, os.getcwd(), getattr(sys, '_MEIPASS', ''), exe):
        if root:
            yield root


def _resolve_library(mod):
    """Point ``mod.LIB_PATH`` at the template library wherever this build put it.

    ORION_PANEL_TEMPLATES wins (an explicit operator override), then panel_grade's own
    self-relative guess, then the bundle layouts: beside the module / the executable, and
    at the repository-relative tools/timing path inside a standalone dist.
    """
    override = (os.environ.get('ORION_PANEL_TEMPLATES', '') or '').strip()
    if override and os.path.isfile(override):
        mod.LIB_PATH = override
        return mod
    if os.path.isfile(getattr(mod, 'LIB_PATH', '') or ''):
        return mod
    for root in _library_roots():
        for cand in (os.path.join(root, _LIB_NAME),
                     os.path.join(root, 'tools', 'timing', _LIB_NAME)):
            if os.path.isfile(cand):
                mod.LIB_PATH = cand
                return mod
    return mod


def load_panel_grade():
    """Import panel_grade, from the compiled bundle or from the dev repo.

    Order:
      1. ``orion_panel_grade`` -- the repo-root name the release build compiles INTO the
         sidecar (scripts/build_orion_sidecar.ps1 copies tools/timing/panel_grade.py to
         that name and passes --include-module). Shipping the grader as readable .py would
         cross docs/IP_PROTECTION_PLAN.md ("readers ship compiled"), so the bundle carries
         no source for it at all.
      2. ``tools.timing.panel_grade`` -- the repo namespace package, how the dev sidecar
         and every offline tool run.
      3. an explicit file load of ``<root>/tools/timing/panel_grade.py`` for root in the
         module directory, the CWD and sys._MEIPASS -- a source checkout reached from an
         unusual CWD.
    Returns None when none resolve: a packaged build without the module must be a quiet
    feature-off, never a detector failure.
    """
    for name in ('orion_panel_grade', 'tools.timing.panel_grade'):
        try:
            return _resolve_library(importlib.import_module(name))
        except Exception:
            continue
    for root in _library_roots():
        path = os.path.join(root, 'tools', 'timing', 'panel_grade.py')
        if not os.path.isfile(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location('orion_panel_grade_live', path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return _resolve_library(mod)
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------- matcher
class _Matcher:
    """Vectorised stand-in for ``panel_grade.match_word``.

    TM_CCOEFF_NORMED evaluated at the single valid position of an equally sized template
    and image is exactly the Pearson correlation of the two flattened vectors, so scoring
    every template at once with one matrix-vector product returns the same argmax and the
    same score (to float32 rounding) as panel_grade's per-template OpenCV loop -- at ~1/50
    of the cost.  The acceptance bar is panel_grade's own ``NCC_MATCH``.
    """

    def __init__(self, pg, masks, labels):
        self._bar = float(pg.NCC_MATCH)
        rows, keep = [], []
        for mask, label in zip(masks, labels):
            v = np.asarray(mask, dtype=np.float32).ravel()
            v = v - v.mean()
            n = float(np.linalg.norm(v))
            if n <= 0.0 or not np.isfinite(n):
                continue                      # a blank template can never match anything
            rows.append(v / n)
            keep.append(str(label))
        self._t = (np.asarray(rows, dtype=np.float32)
                   if rows else np.zeros((0, 1), np.float32))
        self.labels = keep

    def __len__(self) -> int:
        return len(self.labels)

    def match(self, mask):
        """-> (word, ncc). ``UNKNOWN`` below the bar, exactly like panel_grade."""
        if mask is None or not self.labels:
            return 'UNKNOWN', 0.0
        v = np.asarray(mask, dtype=np.float32).ravel()
        if v.size != self._t.shape[1]:
            return 'UNKNOWN', 0.0
        v = v - v.mean()
        n = float(np.linalg.norm(v))
        if n <= 0.0 or not np.isfinite(n):
            return 'UNKNOWN', 0.0
        scores = self._t @ (v / n)
        k = int(np.argmax(scores))
        best = float(scores[k])
        return (self.labels[k], best) if best >= self._bar else ('UNKNOWN', best)


# --------------------------------------------------------------------------- fast cells
# WHY THIS EXISTS (2026-09-14 live regression)
# -------------------------------------------
# The first live session with the reader armed put 62-271 ms stalls into the detect thread's
# infer histogram (one 87 ms outlier in ~1,200 samples before; 17 stalls >60 ms in ~1,100
# after), skipping 4-16 frames per shot and restarting the engine's ownership proof on
# geometry. The mechanism is the GIL, not CPU: ``panel_grade.find_cells`` contains two
# PURE-PYTHON pixel loops -- one per row to find candidate rows, one over every candidate
# PAIR (O(n^2); n reached 20 on a real HUD strip and can reach 40) -- and a Python loop of
# small numpy calls never releases the GIL. Measured 0.23-0.39 ms and 0.02-0.49 ms of
# UNINTERRUPTIBLE GIL hold per sample, on a detect thread that is raised above normal
# priority (ORION_DETECT_THREAD_PRIORITY) and competes with the preview encoder, telemetry
# and the 1 kHz input router. A normal-priority thread holding the GIL is then a priority
# inversion the Windows scheduler cannot resolve by boosting, and the detect thread's tail
# blows out far beyond the hold itself.
#
# These two functions are a FAITHFUL port of panel_grade.find_cells / cell_word_mask with
# both loops replaced by single OpenCV / BLAS calls that release the GIL:
#   candidate rows  a 1x70 erosion (a row has a run >= CELL_MIN_W iff the erosion keeps a
#                   pixel) instead of the per-row _runs() loop;
#   best row pair   one float32 gemm giving every (top, bottom) overlap count at once,
#                   then a single argmax on cover*100 + height -- row-major argmax returns
#                   the FIRST maximum, which is exactly the original's y0-outer/y1-inner
#                   scan with a strict `>` replacement rule.
# Every threshold, the cell gates, the colour classifier and the word fingerprint are
# panel_grade's own (called directly where possible). Output identity is not an argument,
# it is pinned: tests/test_banner_verdict_live.py::test_fast_cells_match_panel_grade_exactly
# diffs both paths over synthetic panels, HUD furniture and random noise.
def _planes(pg, strip):
    """panel_grade.planes(), but uint8 planes from cv2.split instead of three int64 casts."""
    h, s, v = cv2.split(cv2.cvtColor(strip, cv2.COLOR_BGR2HSV))
    return h, s, v, (s > pg.S_MIN) & (v > pg.V_MIN)


def _find_cells(pg, strip, planes):
    h, s, v, sat = planes
    frame = (s < 60) & (v > pg.FRAME_V_MIN)
    b = cv2.morphologyEx((sat | frame).astype(np.uint8), cv2.MORPH_CLOSE,
                         np.ones((1, 3), np.uint8))
    H, W = b.shape
    # candidate rows: one erosion, not H python-level run scans
    er = cv2.erode(b, np.ones((1, pg.CELL_MIN_W), np.uint8),
                   borderType=cv2.BORDER_CONSTANT, borderValue=0)
    cand = np.nonzero(cv2.reduce(er, 1, cv2.REDUCE_MAX).ravel())[0]
    b = b.astype(bool)
    if cand.size == 0:
        return []
    # best (y0, y1): one gemm for every pair's overlap count, one argmax for the winner
    top_all = b[cand] | b[np.minimum(cand + 1, H - 1)]
    bot_all = b[cand] | b[np.maximum(cand - 1, 0)]
    cover = top_all.astype(np.float32) @ bot_all.astype(np.float32).T
    hgt = cand[None, :] - cand[:, None]
    ok = (hgt >= pg.CELL_MIN_H) & (hgt <= pg.CELL_MAX_H)
    # cover <= W (440) and hgt <= 34, so cover*100 + hgt is an exact float32 lexicographic key.
    key = np.where(ok, cover * 100.0 + hgt, -1.0)
    k = int(np.argmax(key))
    if float(key.flat[k]) < 0.0:
        return []
    i, j = divmod(k, int(cand.size))
    best_cover = int(round(float(cover[i, j])))
    if best_cover < pg.CELL_MIN_W:
        return []
    y0, y1 = int(cand[i]), int(cand[j])
    # ---- from here down this is panel_grade.find_cells verbatim
    top = b[y0] | b[min(y0 + 1, H - 1)]
    bot = b[y1] | b[max(y1 - 1, 0)]
    lines = pg._runs(b[y0:y1 + 1].mean(axis=0) >= pg.LINE_FRAC)
    cells = []
    for (l0, l1), (r0, r1) in zip(lines, lines[1:]):
        x0, x1 = l0, r1
        if x1 - x0 < pg.CELL_MIN_W:
            continue
        if top[x0:x1].mean() < pg.FRAME_FRAC or bot[x0:x1].mean() < pg.FRAME_FRAC:
            continue
        iv = v[y0 + pg.BORDER_PAD:y1 + 1 - pg.BORDER_PAD, x0 + pg.BORDER_PAD:x1 - pg.BORDER_PAD]
        if iv.size == 0 or float(np.mean(iv < 70)) < 0.45:
            continue
        cells.append(dict(x0=int(x0), x1=int(x1), y0=int(y0), y1=int(y1 + 1),
                          color=pg._cell_color(h, sat, x0, x1, y0, y1 + 1)))
    return cells


def _cell_word_mask(pg, planes, cell):
    """panel_grade.cell_word_mask verbatim, with the planes handed in instead of recomputed
    (the original calls planes() once per cell -- three extra HSV conversions per read)."""
    _h, s, v, sat = planes
    ix0, ix1 = cell['x0'] + pg.BORDER_PAD, cell['x1'] - pg.BORDER_PAD
    iy0, iy1 = cell['y0'] + pg.VALUE_PAD, cell['y1'] - pg.VALUE_PAD
    if iy1 - iy0 < 6 or ix1 - ix0 < 20:
        return None
    m = sat[iy0:iy1, ix0:ix1]
    if int(m.sum()) < 25:
        m = ((s < 60) & (v > pg.WHITE_TEXT_V))[iy0:iy1, ix0:ix1]
    groups = [(a, e) for a, e in pg._runs(m.sum(axis=1) > 0) if e - a >= 5]
    if not groups:
        return None
    a, e = groups[-1]
    val = m[a:e]
    ys, xs = np.nonzero(val)
    if len(xs) < 25:
        return None
    crop = val[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.uint8) * 255
    if crop.shape[1] < 20:
        return None
    return cv2.resize(crop, (pg.MASK_W, pg.MASK_H), interpolation=cv2.INTER_AREA)


def read_cells(pg, strip, planes=None):
    """panel_grade.read_cells with the fast cell splitter and one shared planes()."""
    if planes is None:
        planes = _planes(pg, strip)
    cells = _find_cells(pg, strip, planes)
    out = []
    for c, role in zip(cells, pg.cell_roles(cells)):
        out.append(dict(role=role, color=c['color'],
                        box=(c['x0'], c['y0'], c['x1'], c['y1']),
                        # the DISTANCE cell's pixels are carried, not just its box: the
                        # word matcher has nothing to say about `23'5"`, and banner_distance
                        # needs the crop. Costs a tuple, not a copy.
                        cell=c,
                        mask=_cell_word_mask(pg, planes, c) if role != 'distance' else None))
    return out


# --------------------------------------------------------------------------- reading
def read_strip(pg, matcher, strip):
    """One panel read of a 40x440 strip, or None when no panel is up.

    Mirrors ``panel_grade.signature`` -> ``events_from`` (the dark gate) -> ``read_cells``
    -> ``match_event`` for a SINGLE frame.  Returns the event dict the emitter sends.
    """
    planes = _planes(pg, strip)
    h, s, v, strong = planes
    # panel_grade's own panel gate, counted by OpenCV so the common no-panel frame costs one
    # GIL-releasing compare instead of a numpy reduction.
    if cv2.countNonZero(cv2.compare(v, 45, cv2.CMP_LT)) <= pg.PANEL_DARK_MIN:
        return None
    cells = read_cells(pg, strip, planes)
    if not cells:
        return None
    # strip-level colour class == panel_grade.classify(signature(strip)); it is the default
    # timing_color that match_event installs before a timing cell overrides it. inRange +
    # countNonZero instead of three chained numpy boolean reductions: same counts, and the
    # work happens inside OpenCV with the GIL released.
    strong_u8 = strong.view(np.uint8)
    band = lambda lo, hi: cv2.countNonZero(
        cv2.bitwise_and(cv2.inRange(h, lo, hi), strong_u8))
    sig = dict(
        green=band(pg.GREEN[0], pg.GREEN[1]),
        red=cv2.countNonZero(cv2.bitwise_and(
            cv2.bitwise_or(cv2.inRange(h, 0, pg.RED_LO), cv2.inRange(h, pg.RED_HI, 255)),
            strong_u8)),
        yellow=band(pg.YELLOW[0], pg.YELLOW[1]),
    )
    # Only a positively observed two-cell TIMING | DISTANCE layout proves
    # absence. A lone timing cell may be a partial/fading/occluded three-cell
    # panel; reporting it absent would authorize OPEN-shot calibration.
    coverage_absent = (len(cells) == 2
                       and cells[0]['role'] == 'timing'
                       and cells[1]['role'] == 'distance'
                       and cells[1]['color'] == 'white')
    out = dict(timing='', ncc=0.0, timing_color=pg.classify(sig),
               coverage='', cov_ncc=0.0, distance_color='', has_coverage=not coverage_absent,
               distance='', distance_ft=-1.0,
               cells='|'.join(c['color'] for c in cells))
    for c in cells:
        if c['role'] == 'distance':
            if not out['distance_color']:
                out['distance_color'] = c['color']
            # [ORION_BANNER_DISTANCE 2026-09-17] READ THE CELL, not just its colour. The
            # game prints the shot's own distance here (`23'5"`), which is a free, exact
            # RANGE label for every graded shot -- the label shot_range.py's calibration
            # was specified against and could not get. banner_distance is fail-closed: an
            # unreadable cell carries no distance and NEVER delays the timing verdict
            # (_complete() is deliberately untouched).
            if not out['distance'] and _bd is not None:
                try:
                    txt, ft, _dbg = _bd.read_distance(strip, c['cell'], planes_=planes)
                    if txt:
                        out['distance'] = txt
                        out['distance_ft'] = round(float(ft), 3)
                except Exception:
                    pass
            continue
        word, score = matcher.match(c['mask'])
        # panel_grade: a lone COVERAGE panel reads a coverage word in first position; it is
        # re-roled, never scored as a timing verdict.
        role = 'coverage' if (c['role'] == 'timing' and word in pg.COVERAGE_WORDS) else c['role']
        if role == 'timing':
            out['timing'], out['ncc'], out['timing_color'] = word, score, c['color']
        elif role == 'coverage':
            # LAYOUT, not content: does this panel HAVE a coverage cell at all? The 2-cell
            # TIMING | DISTANCE layout (panel_grade.cell_roles: a white second cell is
            # `distance`) has none, and every one of the 30 panels in the 2026-09-15
            # 18:53 drill was that layout. Waiting for a coverage word there means waiting
            # forever -- which is exactly why the live reader only ever flushed on the
            # panel's DOWN edge, 2.5-3 s after onset, and merged back-to-back panels.
            out['has_coverage'] = True
        if role == 'coverage' and not out['coverage']:
            # An unreadable coverage cell reports NOTHING. panel_grade keeps "UNKNOWN" in its
            # CSV (a human reads that column); on the wire it would be a word the UI does not
            # know AND -- worse -- it would satisfy the "read is complete" test below and
            # flush a verdict while the panel was still fading in.
            if word != 'UNKNOWN':
                out['coverage'], out['cov_ncc'] = word, score
    out['green'] = bool(pg.is_green(out['timing']))
    return out


def _complete(ev) -> bool:
    """Has this read everything the PANEL'S LAYOUT can carry?

    A 2-cell TIMING | DISTANCE panel has no coverage cell at all (panel_grade.cell_roles
    names a white second cell `distance`), and all 30 panels of the 2026-09-15 18:53 drill
    were that layout -- so waiting for a coverage word there waits forever, which is exactly
    why the live reader only ever flushed on the panel's DOWN edge, 2.5-3 s after onset.
    Only a panel that really has a coverage cell waits for its word.
    """
    return bool(ev['coverage']) or not ev['has_coverage']


# --------------------------------------------------------------------------- live reader
class BannerVerdictLive:
    """Feed it the detector's frame; it emits one ``banner_verdict`` per banner.

    ``submit()`` is the ONLY method the CV loop calls and it is cheap and non-blocking.
    """

    EVENT = 'banner_verdict'

    def __init__(self, pg, matcher, emit_line=None, stride=DEFAULT_STRIDE,
                 debounce_ms=DEFAULT_DEBOUNCE_MS, log=None, start_worker=True,
                 attr_min_ms=DEFAULT_ATTR_MIN_MS, attr_max_ms=DEFAULT_ATTR_MAX_MS,
                 require_release=True):
        self._pg = pg
        self._matcher = matcher
        self._emit_line = emit_line
        self._log = log or logger
        self._stride = max(1, int(stride))
        self._debounce_ms = max(0.0, float(debounce_ms))
        self._attr_min_ms = float(attr_min_ms)
        self._attr_max_ms = float(attr_max_ms)
        self._require_release = bool(require_release)

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._pending = None          # latest-wins mailbox: (strip, frame_seq, ts_ns, epoch_ms)
        self._stopping = False
        self._thread = None
        self.enabled = True
        self._warned = False

        # cadence / stats
        self._seen = 0
        self.frames_sampled = 0
        self.panel_samples = 0
        self.verdicts = 0             # panel appearances graded (forwarded + unattributed)
        self.verdicts_forwarded = 0
        self.verdicts_unattributed = 0
        self.verdicts_ambiguous = 0
        self.repeats_suppressed = 0   # same panel, still on screen
        self.releases_seen = 0
        self.dropped = 0              # samples overwritten because the worker was busy
        self.worker_ms_total = 0.0
        self.worker_calls = 0

        # edge-trigger state machine
        self._up = False
        self._down_run = _DOWN_RUN_GONE
        self._clear_run = _CLEAR_RUN
        self._armed = False
        self._best = None
        self._appear_ms = 0.0         # epoch of the FIRST sample of this appearance (ONSET)
        self._panel_sig = None        # (colour class, cell layout) of the panel event now up
        # the modal DISTANCE over this appearance's samples (banner_distance.DistanceVote)
        self._dist = _bd.DistanceVote() if _bd is not None else None
        self._run_key = None          # the complete read currently repeating
        self._run_n = 0
        self._last_key = None
        self._last_release_seq = -1
        self._gap_since_emit = True   # has the panel been CLEARED since the last emit?
        self._last_emit_ms = -1e18

        # Release feed. Written by the sidecar's stdin thread (note_release), read by the
        # worker; its own lock so the detect thread's submit() never waits on it.
        self._rel_lock = threading.Lock()
        self._releases = []           # [{'seq': int, 'ms': float, 'used': bool}]

        if start_worker:
            self._thread = threading.Thread(target=self._run, name='banner-verdict',
                                            daemon=True)
            self._thread.start()

    # ------------------------------------------------------------------ construction
    @classmethod
    def create(cls, emit_line=None, log=None, env=None):
        """Build the reader, or return None when it is off/unavailable. Never raises."""
        log = log or logger
        env = os.environ if env is None else env
        try:
            # DEFAULT ON since 2026-09-17 (SHIP CONFIG). It shipped OFF on 2026-09-14 because
            # the first live session with the reader armed put 62-271 ms stalls into the detect
            # thread's infer histogram (it had one 87 ms outlier in ~1,200 samples before, then
            # 17 stalls >60 ms in ~1,100 after) and the engine's ownership proof started
            # restarting on geometry. Every graded session from 09-15 onward ran it ON via
            # ORION_BANNER_VERDICT_LIVE=1 on the dev launch line -- including the 09-16 20:26
            # "perfect" session and every session that calibrated banner_lead_trim -- so the
            # SOURCE default is what a packaged install must adopt: customers never run
            # run_orion.local.ps1 and would otherwise get an uncalibrated build.
            # ORION_BANNER_VERDICT_LIVE=0 is still the kill switch, and every failure mode
            # below still disables the reader fail-soft after one WARNING.
            flag = (env.get('ORION_BANNER_VERDICT_LIVE', '1') or '').strip().lower()
            if flag in ('', '0', 'false', 'no', 'off'):
                log.info('banner verdict reader: OFF (ORION_BANNER_VERDICT_LIVE=%s)', flag)
                return None
            pg = load_panel_grade()
            if pg is None:
                log.warning('banner verdict reader DISABLED: tools/timing/panel_grade.py '
                            'not importable (dev-only tooling; not in the sidecar bundle)')
                return None
            masks, labels = pg.load_library()
            matcher = _Matcher(pg, masks, labels)
            if not len(matcher):
                log.warning('banner verdict reader DISABLED: empty template library at %s',
                            getattr(pg, 'LIB_PATH', '?'))
                return None
            # _env_float reads os.environ directly; honour an injected env for callers/tests.
            def _f(name, default):
                try:
                    return float((env.get(name, '') or '').strip() or default)
                except (TypeError, ValueError):
                    return float(default)
            stride = int(_f('ORION_BANNER_VERDICT_STRIDE', DEFAULT_STRIDE))
            debounce = _f('ORION_BANNER_VERDICT_DEBOUNCE_MS', DEFAULT_DEBOUNCE_MS)
            attr_min = _f('ORION_BANNER_VERDICT_ATTR_MIN_MS', DEFAULT_ATTR_MIN_MS)
            attr_max = _f('ORION_BANNER_VERDICT_ATTR_MAX_MS', DEFAULT_ATTR_MAX_MS)
            require = (env.get('ORION_BANNER_VERDICT_REQUIRE_RELEASE', '1')
                       or '').strip().lower() not in ('0', 'false', 'no', 'off')
            live = cls(pg, matcher, emit_line=emit_line, stride=stride,
                       debounce_ms=debounce, log=log, attr_min_ms=attr_min,
                       attr_max_ms=attr_max, require_release=require)
            log.info('banner verdict reader ARMED: %d templates, stride=%d (~%.0f Hz at 60fps), '
                     'debounce=%.0fms, attribution=%.0f..%.0fms require_release=%d',
                     len(matcher), live._stride, 60.0 / live._stride, live._debounce_ms,
                     live._attr_min_ms, live._attr_max_ms, int(live._require_release))
            return live
        except Exception as exc:
            log.warning('banner verdict reader DISABLED: %s', exc, exc_info=True)
            return None

    # ------------------------------------------------------------------ release feed
    def note_release(self, seq, release_ms=0.0) -> bool:
        """Record one BOT release edge. ``seq`` identifies the shot, ``release_ms`` is the
        release command's wall clock in epoch ms -- the same axis the detector's frame
        stamps ride (the orchestrator commits capture-epoch stamps precisely so the fill
        timeline and the release timeline are directly subtractable).

        Called from the sidecar's stdin thread on every engine release, INCLUDING the
        ``METER BACKSTOP`` release: a backstop shot is still the bot's shot and its banner
        is still the owner's tally.  A press that ends in a disarm is deliberately NOT a
        release -- the bot did not shoot, so whatever panel follows is not its verdict.

        Cheap (one lock, one append) and must never raise: it runs on the channel that also
        carries the shot gate.
        """
        if not self.enabled:
            return False
        try:
            ms = float(release_ms or 0.0)
            if not (ms > 0.0) or ms != ms:            # 0, negative, or NaN
                ms = time.time() * 1000.0
            key = int(seq)
            with self._rel_lock:
                # The same edge can arrive twice (a retried command); one shot, one entry.
                for r in self._releases:
                    if r['seq'] == key and abs(r['ms'] - ms) < 1.0:
                        return False
                self._releases.append({'seq': key, 'ms': ms, 'used': False})
                if len(self._releases) > _RELEASE_RING:
                    del self._releases[:-_RELEASE_RING]
            self.releases_seen += 1
            return True
        except Exception as exc:
            self._log.debug('banner verdict note_release failed: %s', exc)
            return False

    def _match_release(self, appear_ms):
        """Return the sole eligible release, or None when absent/ambiguous."""
        return self._match_release_with_candidates(appear_ms)[0]

    def _match_release_with_candidates(self, appear_ms):
        """Return (unique release, count). Never guess between overlapping windows.

        Nothing is consumed here: a re-read or ambiguous panel must not burn a
        release that a later, independently timed panel can still claim.
        """
        try:
            t = float(appear_ms)
        except (TypeError, ValueError):
            return None, 0
        with self._rel_lock:
            eligible = [r for r in self._releases if not r['used']
                        and self._attr_min_ms <= t - r['ms'] <= self._attr_max_ms]
        return (eligible[0] if len(eligible) == 1 else None), len(eligible)

    def _last_release_age_ms(self, now_ms) -> float:
        with self._rel_lock:
            if not self._releases:
                return -1.0
            return float(now_ms) - self._releases[-1]['ms']

    # ------------------------------------------------------------------ CV-thread side
    def submit(self, frame, frame_seq=-1, frame_ts=0.0, epoch_ms=0.0) -> bool:
        """Offer one unique detector frame. Returns True when it was sampled.

        Runs on the latency-critical detect thread: cadence gate, one strip crop + copy,
        one lock.  Everything else happens on the worker.  Must never raise.
        """
        if not self.enabled:
            return False
        try:
            self._seen += 1
            if (self._seen % self._stride) != 0:
                return False
            strip = self._crop(frame)
            if strip is None:
                return False
            ts_ns = int(float(frame_ts or 0.0) * 1e9)
            ems = float(epoch_ms or 0.0)
            if ems <= 0.0:
                ems = time.time() * 1000.0
            with self._cv:
                if self._pending is not None:
                    self.dropped += 1
                self._pending = (strip, int(frame_seq), ts_ns, ems)
                self._cv.notify()
            self.frames_sampled += 1
            return True
        except Exception as exc:
            self._disable('submit failed', exc)
            return False

    def _crop(self, frame):
        """The panel strip as its own 40x440 BGR buffer.

        720p (the shipped detector contract) is an exact crop of panel_grade's constants.
        A non-720p frame is cropped proportionally and resized here; panel_grade resizes
        the WHOLE frame first, which differs only in the resampler's edge taps -- and the
        detector contract is 1280x720 unless ORION_DETECTOR_1080P=1.
        """
        if frame is None or getattr(frame, 'ndim', 0) != 3:
            return None
        pg = self._pg
        h, w = frame.shape[:2]
        if (w, h) == (1280, 720):
            # panel_grade's own crop, so the live strip is byte-identical to the offline one.
            return np.ascontiguousarray(pg.strip_from_frame(frame))
        if w < 16 or h < 16:
            return None
        sy, sx = h / 720.0, w / 1280.0
        y0, y1 = int(round(pg.Y0 * sy)), int(round(pg.Y1 * sy))
        x0, x1 = int(round(pg.X0 * sx)), int(round(pg.X1 * sx))
        y1 = min(max(y1, y0 + 1), h)
        x1 = min(max(x1, x0 + 1), w)
        sub = frame[y0:y1, x0:x1]
        return cv2.resize(sub, (pg.X1 - pg.X0, pg.Y1 - pg.Y0), interpolation=cv2.INTER_AREA)

    # ------------------------------------------------------------------ worker side
    def _run(self):
        while True:
            with self._cv:
                while self._pending is None and not self._stopping:
                    self._cv.wait()
                if self._pending is None and self._stopping:
                    return
                job = self._pending
                self._pending = None
            if not self.enabled:
                continue
            try:
                t0 = time.perf_counter()
                self.process(*job)
                self.worker_ms_total += (time.perf_counter() - t0) * 1000.0
                self.worker_calls += 1
            except Exception as exc:
                self._disable('reader failed', exc)
                return

    def process(self, strip, frame_seq, ts_ns, epoch_ms):
        """One sampled strip through the reader + edge trigger. Exposed for tests."""
        ev = read_strip(self._pg, self._matcher, strip)
        if ev is None:
            self._down_run += 1
            self._clear_run += 1
            if self._up and self._down_run >= _DOWN_RUN_GONE:
                self._up = False
                if self._armed and self._best is not None and _complete(self._best):
                    # Fallback for a panel that was legible on a SINGLE sample and then
                    # vanished. An INCOMPLETE best is dropped, never emitted: a 3-cell panel
                    # caught mid-fade would otherwise be reported without its coverage word
                    # and then reported again, properly, when the panel came back.
                    self._flush(epoch_ms)
                self._arm(0.0, armed=False)
                self._panel_sig = None
            if self._clear_run >= _CLEAR_RUN:
                # The panel really left the screen, so the next identical read is a NEW
                # panel and not the single-sample dropout that produced the repeat storm.
                self._gap_since_emit = True
            return
        self.panel_samples += 1
        self._down_run = 0
        self._clear_run = 0
        # PANEL EVENT BOUNDARY, exactly as panel_grade.events_from draws it: a run lasts
        # while the strip's colour CLASS and cell LAYOUT hold. Two shots in rapid fire are
        # often separated by the game's white inter-panel flash rather than by blank frames
        # -- the dark plate never lifts -- so an absence test alone merges them. It cost 1
        # of 30 panels in the 18:53 drill even after the clear-run was cut to 2 samples.
        # This is read off EVERY panel sample, including one whose word is still illegible.
        run_sig = (ev['timing_color'], ev['cells'])
        # BEFORE the illegible-word return below: a sample whose timing word is still
        # fading in can carry a perfectly readable distance cell, and the vote wants it.
        if self._dist is not None and (not self._up or run_sig == self._panel_sig):
            self._dist.add(ev.get('distance') or '')
        if not self._up:
            self._up = True
            self._arm(float(epoch_ms))
            self._panel_sig = run_sig
        elif run_sig != self._panel_sig:
            if self._armed and self._best is not None:
                self._flush(epoch_ms)              # the old event ended here
            self._arm(float(epoch_ms))
            self._panel_sig = run_sig
            self._gap_since_emit = True            # a boundary IS the panel leaving
        if ev['timing'] in ('', 'UNKNOWN'):
            return                                  # never invent a word; wait for a clean look
        # A verdict's IDENTITY is its word and its colour -- panel_grade's own run key plus
        # the word. Coverage is payload, not identity: a panel re-read once with and once
        # without its coverage cell is one panel, not two.
        key = (ev['timing'], ev['timing_color'])
        if not self._armed:
            # This appearance is already reported. The only way back in without the panel
            # ever going away is the game swapping one panel for the next: a real CHANGE of
            # words or of the verdict COLOUR (LATE reads red on one shot and yellow on the
            # next). In rapid fire the blank between two panels is 2-4 samples and one of
            # the 29 gaps in the drill was shorter than that, so a change counts as a new
            # panel IMMEDIATELY -- the _SIG_CONFIRM run below is what keeps a single misread
            # from becoming a verdict, and the repeat guard in _flush keeps it from becoming
            # a duplicate of the panel already reported.
            if not self._is_panel_change(key):
                self.repeats_suppressed += 1
                return
            self._arm(float(epoch_ms))
        ev = dict(ev, frame_seq=int(frame_seq), frame_ts_ns=int(ts_ns),
                  frame_epoch_ms=float(epoch_ms))
        if self._best is None or self._better(ev, self._best):
            self._best = ev
        if not _complete(ev):
            return
        if key == self._run_key:
            self._run_n += 1
        else:
            self._run_key, self._run_n = key, 1
        if self._run_n >= _SIG_CONFIRM:
            self._flush(epoch_ms)                   # fire on the FIRST confirmed read

    def _arm(self, appear_ms, armed=True):
        """Start (or end) an appearance: one place owns every per-appearance field."""
        self._armed = bool(armed)
        self._best = None
        self._run_key, self._run_n = None, 0
        self._appear_ms = float(appear_ms)
        # The distance VOTE is per appearance too. The down-edge path flushes BEFORE it
        # calls _arm(0.0, armed=False), so a reset here never eats the votes it is about
        # to report. See banner_distance.DistanceVote: three of 1,845 frames read wrong
        # (an encoder-ghosted 15'5" as 18'8"), with an NCC and a top1-top2 margin inside
        # the correct reads' range -- no per-frame gate removes them, and the mode over
        # the appearance's ~10-20 samples does (21-3 on the one event that has them).
        if self._dist is not None:
            self._dist.reset()

    def _is_panel_change(self, key) -> bool:
        """Is ``key`` a DIFFERENT panel from the last one reported, mid-presence?

        Only a different verdict or a different colour (LATE reads red on one shot and
        yellow on the next). A coverage cell that starts or stops resolving is the same
        panel read twice.
        """
        return self._last_key is not None and key != self._last_key

    @staticmethod
    def _better(a, b) -> bool:
        # `distance` is the LAST term on purpose: it breaks a tie between two samples the
        # existing ordering already calls equal, and changes nothing else.
        return ((bool(a['coverage']), a['ncc'], bool(a.get('distance')))
                > (bool(b['coverage']), b['ncc'], bool(b.get('distance'))))

    def _flush(self, now_ms: float):
        ev, self._best = self._best, None
        self._armed, self._run_key, self._run_n = False, None, 0
        if ev is None:
            return
        key = (ev['timing'], ev['timing_color'])
        # The panel's ONSET -- its first sampled frame -- is what a release has to precede.
        # NOT the emit: the emit lags onset by the confirm run, and grew to 2.5-3 s in the
        # first cut (which waited for a coverage cell a 2-cell panel never has), which both
        # pushed the attribution test toward the far edge of the window and made a stale
        # panel far more likely to still be up when the next release fired.
        appear_ms = self._appear_ms or float(ev['frame_epoch_ms'])
        emit_lag_ms = max(0.0, float(now_ms) - appear_ms)
        rel, eligible_count = self._match_release_with_candidates(appear_ms)
        # The distance the APPEARANCE agreed on, not the best sample's -- see _arm().
        dist_text, dist_ft = (ev.get('distance') or ''), ev.get('distance_ft')
        if self._dist is not None:
            voted, _n, _tot = self._dist.best()
            if voted:
                dist_text, dist_ft = voted, self._dist.feet()
        if not dist_text:
            dist_ft = None
        rel_seq = int(rel['seq']) if rel is not None else -1
        delay_ms = (appear_ms - rel['ms']) if rel is not None else -1.0
        # Same words AND same colour are a SECOND panel only if the first one really ENDED
        # (absent >= _CLEAR_RUN samples, or a panel-event boundary) AND a NEW bot release
        # stands behind this one. The release is the only honest discriminator: the
        # 2026-09-15 replay screen re-read the identical panel every ~2 s with no shot at
        # all, so any time-based floor is something a stuck screen can simply outwait.
        # Without attribution (require_release=0) there is nothing else, and debounce_ms
        # is the fallback it always was.
        new_shot = rel_seq >= 0 and rel_seq != self._last_release_seq
        cleared = self._gap_since_emit and (
            new_shot or (not self._require_release
                         and (now_ms - self._last_emit_ms) >= self._debounce_ms))
        if key == self._last_key and not cleared:
            self.repeats_suppressed += 1
            return
        self._last_key = key
        self._last_emit_ms = now_ms
        self._gap_since_emit = False
        self.verdicts += 1
        attributed = (rel is not None)
        if attributed:
            with self._rel_lock:
                rel['used'] = True
            self._last_release_seq = rel_seq
        payload = {
            'event': self.EVENT,
            'timing': ev['timing'],
            'timing_color': ev['timing_color'],
            'green': bool(ev['green']),
            'coverage': ev['coverage'],
            # [ORION_BANNER_COVERAGE_ABSENT 2026-09-19 owner] LAYOUT, not content. `coverage`
            # alone cannot tell the native which of two very different things happened:
            #   has_coverage=1, coverage=''  -> the panel HAS a coverage cell and it was
            #                                   unreadable (a fading-in 3-cell panel, UNKNOWN)
            #   has_coverage=0, coverage=''  -> the 2-cell TIMING | DISTANCE panel, which has
            #                                   no coverage cell at all and never will
            # The engine's banner trim only calibrates on OPEN / WIDE OPEN, so without this
            # field every drill panel (98 of the 281 graded releases across the 2026-09-18
            # sessions) read as "coverage unknown" and was excluded from the loop -- even
            # though a panel with NO coverage cell is a no-defender context, i.e. open.
            'has_coverage': bool(ev['has_coverage']),
            'distance_color': ev['distance_color'],
            'distance': dist_text,
            'distance_ft': round(float(dist_ft), 3) if dist_ft is not None else -1.0,
            'ncc': round(float(ev['ncc']), 3),
            'cov_ncc': round(float(ev['cov_ncc']), 3),
            'cells': ev['cells'],
            'frame_ts_ns': int(ev['frame_ts_ns']),
            'frame_epoch_ms': round(float(ev['frame_epoch_ms']), 1),
            'frame_seq': int(ev['frame_seq']),
            'seq': int(self.verdicts),
            'attributed': 1 if attributed else 0,
            'release_seq': int(rel_seq),
            'release_delay_ms': round(float(delay_ms), 1),
            'onset_ms': round(float(appear_ms), 1),
            'emit_latency_ms': round(emit_lag_ms, 1),
        }
        if eligible_count > 1:
            # Both shots are plausible on the observed onset clock. Neither may
            # teach trim or increment the native tally/shot-record join.
            self.verdicts_ambiguous += 1
            self._log.error(
                'BANNER VERDICT AMBIGUOUS: timing=%s seq=%d frame_seq=%d '
                'onset_ms=%.0f eligible_releases=%d attributed=0 (not forwarded)',
                payload['timing'], payload['seq'], payload['frame_seq'],
                payload['onset_ms'], eligible_count)
            return payload
        if not attributed and self._require_release:
            # Nobody shot this panel: a replay/feedback screen the game is sitting on, or a
            # shot the OWNER took by hand. It must not reach the native (activity line +
            # ShotVerdictTally), but it must stay visible -- at ERROR, because the native
            # relay throttles sidecar WARNINGs to 1/s and a storm would be hidden.
            self.verdicts_unattributed += 1
            age = self._last_release_age_ms(appear_ms)
            self._log.error(
                'BANNER VERDICT UNATTRIBUTED: timing=%s coverage=%s has_cov=%d ncc=%.3f '
                'cov_ncc=%.3f color=%s green=%d seq=%d frame_seq=%d attributed=0 '
                'onset_ms=%.0f emit_latency_ms=%.0f last_release_ms=%.0f '
                'window=%.0f..%.0fms (no bot release behind this panel; not forwarded)',
                payload['timing'], payload['coverage'] or '-',
                1 if payload['has_coverage'] else 0, payload['ncc'],
                payload['cov_ncc'], payload['timing_color'],
                1 if payload['green'] else 0, payload['seq'], payload['frame_seq'],
                payload['onset_ms'], payload['emit_latency_ms'],
                age, self._attr_min_ms, self._attr_max_ms)
            return payload
        self.verdicts_forwarded += 1
        # One greppable line per verdict so banner_join.py-style diagnostics can read the
        # live grade straight out of the session log. `seq` stays the VERDICT counter it
        # has always been; the release's id is `release_seq`.
        # [ORION_BANNER_COVERAGE_ABSENT 2026-09-19] `has_cov` is printed right after
        # `coverage`, because `coverage=-` alone is ambiguous: it is BOTH "this panel has no
        # coverage cell" (has_cov=0, the 2-cell drill panel) and "it has one and it was
        # unreadable" (has_cov=1). Those two take opposite paths in the engine's trim, so a
        # post-mortem has to be able to tell them apart off the line.
        self._log.info('BANNER VERDICT: timing=%s coverage=%s has_cov=%d ncc=%.3f '
                       'cov_ncc=%.3f color=%s green=%d seq=%d frame_seq=%d attributed=%d '
                       'release_seq=%d delay_ms=%.0f onset_ms=%.0f emit_latency_ms=%.0f',
                       payload['timing'], payload['coverage'] or '-',
                       1 if payload['has_coverage'] else 0, payload['ncc'],
                       payload['cov_ncc'], payload['timing_color'],
                       1 if payload['green'] else 0, payload['seq'], payload['frame_seq'],
                       payload['attributed'], payload['release_seq'],
                       payload['release_delay_ms'], payload['onset_ms'],
                       payload['emit_latency_ms'])
        if self._emit_line is not None:
            try:
                self._emit_line(json.dumps(payload, separators=(',', ':')) + '\n')
            except Exception as exc:
                # A lost verdict line is cosmetic; it must never take the reader down.
                self._log.debug('banner verdict emit failed: %s', exc)
        return payload

    # ------------------------------------------------------------------ lifecycle
    def _disable(self, what: str, exc: BaseException):
        self.enabled = False
        if not self._warned:
            self._warned = True
            self._log.warning('banner verdict reader DISABLED for this session (%s): %s',
                              what, exc, exc_info=True)

    def stop(self, timeout: float = 1.0):
        with self._cv:
            self._stopping = True
            self._pending = None
            self._cv.notify_all()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=max(0.0, float(timeout)))
        if self.worker_calls:
            self._log.info('banner verdict reader: %d verdicts (%d forwarded, %d '
                           'unattributed, %d ambiguous), %d repeat samples suppressed, %d releases, '
                           '%d panel samples, %d frames sampled, %d dropped, %.3f ms/sample',
                           self.verdicts, self.verdicts_forwarded,
                           self.verdicts_unattributed, self.verdicts_ambiguous,
                           self.repeats_suppressed,
                           self.releases_seen, self.panel_samples, self.frames_sampled,
                           self.dropped, self.worker_ms_total / self.worker_calls)
