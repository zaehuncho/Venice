"""LANDMARK-ANCHORED FILL RULER FOR THE 2K27 "Pill (beta)" CAPSULE.

WHY THIS EXISTS (measured 2026-09-17 / 2026-09-19)
--------------------------------------------------
The Pill style routes the sidecar to the YOLO proposer (``models/orion_meter_detector.onnx``,
the 08-30 ``meter2k27_n3_pill`` net) and ``SimpleMeterReader._measure_fill_in_box`` then
measures the fill on a BOX-RELATIVE scale: ``fill = (box_bottom - top) / (box_bottom)``.
That is correct for Arrow2, whose CV contour box is fitted to the white column itself, and
WRONG for Pill, whose regressed box is padded on both ends.  Measured against the capsule's
own landmarks on ``datasets/meter2k27_pill_park`` (720p, box ~20x116 px, hand ruler =
``tools/diagnostics/pill_fill_check.hand_fill``)::

    hand%   reader-in-YOLO-box%
     4.9    11.4
    11.8    18.1
    23.5    28.2
    47.1    48.7
    78.4    78.1
    96.1    92.2
    97.1    93.0

i.e. ``reader ~= 7.5 + 0.88 * hand``.  The box carries ~8.5 px of pedestal BELOW the fill
base and ~7 px of cap ABOVE the green apex at 720p (~16 px on a ~102 px true span), so the
box ruler has BOTH an intercept and a slope error.  Consequences measured on the 2026-09-17
live Pill session (49 shots): the engine's 20 % tip-phase anchor fires at a true fill of
~14 %, i.e. ~30 ms EARLY at the Arrow2-tuned Shot Lead (the owner had to drop 269 -> 240),
and the box bottom's +/-3 px jitter enters the fill directly (vel_at_rel IQR 0.022 pp/ms vs
Arrow2's 0.003).

WHAT THIS MODULE DOES
---------------------
Re-expresses the fill against the capsule's OWN landmarks, all measured inside the same
proposer box, so the box only supplies a search region and never a scale:

    BASE   bottom row of the base-touching bright fill stack, bridging divider-sized gaps
           (the Pill fill is a LADDER of ~7-row segments split by 1-3-row dark dividers);
    APEX   top row of the connected green make-window cap (the same component and the same
           2-px/8-row apex extension ``_measure_fill_in_box`` already uses for green_end);
    TOP    the fill edge the reader already found (the rung-tolerant block's top row).

    fill = S * (base - top) / (base - apex)

``S`` (``ORION_PILL_RULER_SCALE``, default 96.0) makes this ruler EQUIVALENT to Arrow2's:
on Arrow2 the frozen meter's ``green_end`` reads 96.0 (p50 over 316 shots; p10 95.3, p90
96.7) and Arrow2's green_end IS the green dome top, so putting the apex at S=96 lands the
Pill apex exactly where Arrow2's green_end lands.  Every engine constant tuned on Arrow2 --
the 20 % phase anchor, the learned rate, the aim at ~96 -- therefore transfers unchanged.

THE LATCH (same philosophy as ``SimpleMeterReader._subpx_D``, see the block comment at
``simple_meter_reader.py`` ~1605-1720)
------------------------------------------------------------------------------------------
The NUMERATOR is two image measurements (``base`` and ``top``), so per-frame box jitter
already cancels out of it -- measured on the ep35 box-breathing episode of
session_20260917_050219 (box 114 -> 140 -> 112 px on a FROZEN meter): ``base - top`` holds
at exactly 94 px on every frame while the box ruler swings 85.8 -> 70.5 -> 87.4 %.
The DENOMINATOR (the apex->base span) is a CONSTANT of the capture, so it is latched rather
than re-measured every frame:

  * latched to the MEDIAN OF A ROLLING WINDOW of clean spans (``ORION_PILL_RULER_WINDOW``,
    default 180 frames = 3 s at 60 fps) -- a median-of-3 for the first three frames, so
    never a first-frame latch, and robust afterwards.  A literal 3-frame latch was BUILT
    AND MEASURED FIRST and it failed: see ``RulerState``;
  * re-latched only on a SUSTAINED rescale: ``ORION_PILL_RULER_RELATCH_N`` (default 8)
    consecutive measurements more than ``ORION_PILL_RULER_RELATCH`` (default 0.15 -- the
    same 15 % height-jump rule the sub-pixel ruler uses) from the latch.  Out-of-band
    measurements never enter the window, so a mis-picked cap cannot drag it;
  * carried across a lock boundary (``ORION_PILL_RULER_CARRY``, default on): a press is not
    a new meter, and the shipped session ruler already measured that re-seeding per shot is
    worse.  With it off, every lock re-measures the scale from scratch;
  * FAIL-OPEN in every failure mode: no core band, no base, no cap, an implausible span, an
    exception -- the caller's existing box-ruler triple is returned unchanged.

The green make-window is re-expressed on the SAME ruler (the ``[ORION_GREEN_SCALE_UNIFY]``
rule: a landing grade must never compare a fill and a green band measured on two rulers).

SCOPE: this module is reached ONLY from ``_measure_fill_in_box`` when the reader's tracking
style is ``pill``.  Arrow2 / Straight / Dial never enter it and are byte-identical.
"""
from __future__ import annotations

import os

import cv2
import numpy as np

__all__ = ["RulerState", "enabled", "scale", "measure"]


def _flag(name, default):
    return str(os.environ.get(name, default)).strip().lower() in ("1", "true", "yes", "on")


def _fnum(name, default):
    try:
        return float(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return float(default)


def enabled():
    """Master switch.  0 restores the shipped box ruler byte-for-byte."""
    return _flag("ORION_PILL_RULER", "1")


def scale():
    """S: where the green apex lands on the emitted scale (Arrow2's frozen green_end)."""
    return _fnum("ORION_PILL_RULER_SCALE", 96.0)


def _relatch_frac():
    return max(0.02, _fnum("ORION_PILL_RULER_RELATCH", 0.15))


def _carry():
    return _flag("ORION_PILL_RULER_CARRY", "1")


def _window():
    """Frames of clean span measurements the latch takes its median over."""
    return max(3, int(_fnum("ORION_PILL_RULER_WINDOW", 180)))


def _relatch_n():
    """Consecutive out-of-band measurements that count as a genuine rescale."""
    return max(1, int(_fnum("ORION_PILL_RULER_RELATCH_N", 8)))


# The fill/green masks below are the reader's own thresholds, deliberately duplicated rather
# than imported: this module must stay importable by the unit tests and the diagnostics
# without constructing a SimpleMeterReader.
_WHITE_V = 200
_WHITE_S = 65
_GREEN_H = (40, 85)
_GREEN_S = 90
_GREEN_V = 90
_MIN_SPAN_PX = 12.0          # a capsule shorter than this is not a 2K27 shot meter
_MIN_SPAN_FRAC = 0.35        # ...nor is a span under 35 % of the proposer's own box height


class RulerState(object):
    """The latched apex->base span (px) and the clean measurements behind it.

    MEASURED 2026-09-19 on session_20260917_050219 (7829 frames, 46 rises): a literal
    "median of the first 3 clean frames, re-latch on a >15 % jump" latch FAILED on this
    session.  At ep35 the proposer box breathed 114 -> 140 -> 112 px over ~20 frames while
    the meter stood frozen; one frame's cap measured a span of 125 (>15 % out), which
    cleared the latch, and the three re-seeding frames that followed all landed inside the
    same breathing episode, so the ruler re-latched at 111 px and held that wrong scale for
    TEN consecutive shots (green_end read 88.2 instead of 96.0, an 8 % scale error).
    A three-frame window is not a robust estimator of a session constant.

    So the latch is the MEDIAN OF A ROLLING WINDOW of clean spans (``window``, default 180
    frames = 3 s at 60 fps).  It is still a median-of-3 for the first three frames -- never
    a first-frame latch -- and it is still cleared only by a genuine, SUSTAINED rescale,
    but a burst of bad frames can no longer move it: on the same ep35 episode the emitted
    span stays 102 while the box swings 26 px.
    """

    __slots__ = ("key", "span", "hist", "last_ts", "dbg", "window",
                 "n_seed", "n_relatch", "n_latched", "n_open", "n_out",
                 "stamp_key", "stamp_gen")

    def __init__(self, window=None):
        self.stamp_key = None
        self.stamp_gen = 0
        self.key = None
        self.span = None            # emitted latched span = median(hist), or None
        self.hist = []              # clean per-frame spans, newest last, bounded by window
        self.window = int(window if window else _window())
        self.last_ts = None
        self.dbg = None
        self.n_seed = 0
        self.n_relatch = 0
        self.n_latched = 0
        self.n_open = 0
        self.n_out = 0              # consecutive out-of-band measurements

    def observe(self, span_now, relatch_frac, relatch_n):
        """Fold one clean span measurement into the latch."""
        if self.span is not None and abs(span_now - self.span) > relatch_frac * self.span:
            # Out of band.  A mis-picked cap / a breathing box is transient and must never
            # enter the window; a real rescale PERSISTS, and only then is the window wrong.
            self.n_out += 1
            if self.n_out >= relatch_n:
                self.hist = [float(span_now)]
                self.span = None
                self.n_relatch += 1
                self.n_out = 0
            return
        self.n_out = 0
        self.hist.append(float(span_now))
        if len(self.hist) > self.window:
            del self.hist[0:len(self.hist) - self.window]
        if len(self.hist) >= 3:
            if self.span is None:
                self.n_seed += 1
            self.span = float(np.median(self.hist))

    def new_lock(self, key, carry):
        self.key = key
        self.last_ts = None
        self.n_out = 0
        if not carry:
            # ORION_PILL_RULER_CARRY=0: every lock re-measures the scale from scratch.
            self.hist = []
            self.span = None


def state_for(reader):
    st = getattr(reader, "_pill_ruler", None)
    if not isinstance(st, RulerState):
        st = RulerState()
        try:
            reader._pill_ruler = st
        except Exception:
            pass
    return st


def _lock_key(reader):
    """A per-lock identity built from state the reader already maintains.

    ``_det_pixel_ruler_epoch`` is bumped by ``reset_tracking`` (a source/profile change =
    a different meter); ``_physical_shot_epoch`` changes at every press and clears at every
    release, so one shot is one lock.  Neither is written by this module.
    """
    try:
        a = int(getattr(reader, "_det_pixel_ruler_epoch", 0) or 0)
    except Exception:
        a = 0
    try:
        b = int(getattr(reader, "_physical_shot_epoch", 0) or 0)
    except Exception:
        b = 0
    return (a, b)


# ------------------------------------------------------------------ landmark measurement
def core_band(white, bh, bw):
    """Widest contiguous run of the meter's own bright CORE columns, or None.

    Same rule as ``SimpleMeterReader._rung_fill_block``: the glossy Pill fill core is ~4 of
    the ~20 box columns at 720p, so a box-wide row fraction cannot see it.
    """
    colw = white.mean(axis=0)
    cmax = float(colw.max()) if colw.size else 0.0
    if cmax <= 0.0:
        return None
    core = colw >= max(0.5 * cmax, 2.0 / max(1, bh))
    bc0 = bc1 = None
    c0 = None
    best = 0
    for c in range(bw + 1):
        on = (c < bw) and bool(core[c])
        if on and c0 is None:
            c0 = c
        elif not on and c0 is not None:
            if c - c0 > best:
                best = c - c0
                bc0, bc1 = c0, c - 1
            c0 = None
    if bc0 is None or best < 2:
        return None
    return int(bc0), int(bc1)


def fill_base(white, bh, bw, top):
    """Bottom row of the base-touching bright stack that contains ``top``, or None.

    Divider-sized gaps are bridged exactly as the reader's ladder block bridges them
    (``max(2, round(0.025 * bh))`` rows: above the 1-3-row divider, below the ~8-12-row
    empty-track tick pitch).  The stack must reach the capsule's base region; a floating
    bright band is not a fill base.
    """
    band = core_band(white, bh, bw)
    if band is None:
        return None
    bc0, bc1 = band
    bfr = white[:, bc0:bc1 + 1].mean(axis=1)
    runs = []
    rt = None
    for r in range(bh):
        if bfr[r] >= 0.5:
            if rt is None:
                rt = r
        elif rt is not None:
            runs.append((rt, r - 1))
            rt = None
    if rt is not None:
        runs.append((rt, bh - 1))
    if not runs:
        return None
    i = None
    for j, (s, e) in enumerate(runs):
        if s <= top <= e:
            i = j
            break
    if i is None:                       # the reader's edge came from another column set
        for j, (s, _e) in enumerate(runs):
            if s >= top:
                i = j
                break
    if i is None:
        return None
    gap_tol = max(2, int(round(0.025 * bh)))
    base = runs[i][1]
    j = i
    while j + 1 < len(runs) and (runs[j + 1][0] - runs[j][1] - 1) <= gap_tol:
        j += 1
        base = runs[j][1]
    base_lim = bh - max(8, int(round(0.20 * bh)))
    if base < base_lim:
        return None
    return int(base)


def green_cap_rows(reader, hsv, white):
    """(apex_row, bottom_row, px) of the connected green make-window cap, or None.

    The component selection and the 8-row / 2-px apex extension are the ones
    ``_measure_fill_in_box`` already uses to emit ``green_end``, so this apex is exactly the
    row that produced today's green band -- no second opinion about where the cap is.
    """
    Hh, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    gmask = ((Hh >= _GREEN_H[0]) & (Hh <= _GREEN_H[1]) & (S >= _GREEN_S) & (V >= _GREEN_V))
    conn = getattr(reader, "_connected_meter_cap", None)
    if not callable(conn):
        return None                  # fail closed: a second opinion about the cap is not
    try:                             # allowed, the apex must be the row green_end came from
        sel = conn(gmask, white)
    except Exception:
        return None
    gpx = int(np.count_nonzero(sel))
    if gpx < 4:
        return None
    gcnt = sel.sum(axis=1)
    grows = np.flatnonzero(gcnt >= 2)
    if not grows.size:
        return None
    g_top = int(grows[0])
    g_bot = int(grows[-1])
    gap = 0
    rr = g_top
    lim = max(0, g_top - 8)
    while rr - 1 >= lim:
        rr -= 1
        if int(gcnt[rr]) >= 2:
            g_top = rr
            gap = 0
        else:
            gap += 1
            if gap > 1:
                break
    return g_top, g_bot, gpx


def _stamp(reader, st, span):
    """Publish the estimator identity of the ruler that actually divided this frame.

    Native may only interpolate a phase crossing between samples carrying the SAME
    non-zero generation.  ``_measure_fill_in_box_legacy`` has already stamped the BOX
    ruler's identity by the time this module runs, so calling ``_stamp_fill_estimator``
    here would alternate between two identities and bump the generation twice per frame --
    no two consecutive samples would ever be interpolatable.  Assign directly instead, and
    bump the shared monotonic counter ONLY when this ruler changes:

      * a latched span holds one generation for the whole shot, and across a press that
        carries that span (the ruler did not move, so a crossing must not be lost);
      * a re-latch, or a seeding frame that divides by its own measured span, is a
        different ruler and says so.
    """
    try:
        key = ("coarse", ("pill_ruler", round(float(span), 2), int(st.span is not None)))
        if getattr(st, "stamp_key", None) != key:
            st.stamp_key = key
            reader._fill_estimator_generation = int(
                getattr(reader, "_fill_estimator_generation", 0)) + 1
            st.stamp_gen = int(reader._fill_estimator_generation)
        reader._fill_estimator_identity = key
        reader._last_fill_estimator_mode = "coarse"
        reader._last_fill_estimator_generation = int(st.stamp_gen)
    except Exception:
        pass


# ------------------------------------------------------------------------------ the ruler
def measure(reader, frame, box, ts, legacy):
    """Re-express ``legacy`` = (fill_pct, green, top_row) on the capsule's own landmarks.

    Returns a replacement triple, or ``legacy`` unchanged whenever a landmark is missing or
    implausible (fail-open: the shipped box ruler is always the floor, never a hole).
    """
    try:
        if not enabled():
            return legacy
        top = int(legacy[2])
        if top < 0:
            return legacy                      # no fill edge -> nothing to re-scale
        x, y, w, h = (int(v) for v in box)
        H, W = frame.shape[:2]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(W, x + w), min(H, y + h)
        bh, bw = y1 - y0, x1 - x0
        if bw < 3 or bh < 10 or not (0 <= top < bh):
            return legacy
        crop = frame[y0:y1, x0:x1]
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        white = (hsv[:, :, 2] >= _WHITE_V) & (hsv[:, :, 1] <= _WHITE_S)
        base = fill_base(white, bh, bw, top)
        if base is None or base <= top:
            return legacy
        # Use the reader's SUB-PIXEL edge when it measured one on this frame (it is the row
        # the emitted box-ruler fill used, on the coarse walk's first-white-row convention
        # via _subpx_bias).  The landmark ruler must never be coarser than the value it
        # replaces; on 720p Pill the sub-pixel gates do not pass and this is a no-op.
        top_f = float(top)
        q = getattr(reader, "_dbg_subpx", None)
        if (getattr(reader, "_subpx_fill", False) and isinstance(q, dict)
                and q.get("ok") and float(q.get("top_sub", -1.0)) >= 0.0):
            eb = float(q["top_sub"]) + float(getattr(reader, "_subpx_bias", 0.71))
            if abs(eb - top_f) <= 2.5 and eb < base:
                top_f = eb
        cap = green_cap_rows(reader, hsv, white)
        st = state_for(reader)
        key = _lock_key(reader)
        if key != st.key:
            st.new_lock(key, _carry())
        # A cap that touches the box's TOP EDGE is CLIPPED, not measured: `base - apex` is
        # then a lower bound on the span, not the span.  MEASURED on ep46 of
        # session_20260917_050219 idx 6856-6862: the box grew 114 -> 153 px on a frozen
        # meter, the cap ran off its top edge and read apex=0 on seven consecutive frames,
        # and those seven confirmed a re-latch to 144 px -- 15 frames of a 23 pp fill error.
        # Likewise a base on the box's bottom edge is a clipped capsule, not a landmark.
        clipped = (cap is not None and cap[0] <= 0)
        if base >= bh - 1:
            return legacy
        span_now = None
        if cap is not None and not clipped:
            s_now = float(base - cap[0])
            if s_now >= max(_MIN_SPAN_PX, _MIN_SPAN_FRAC * bh):
                span_now = s_now
        fresh = (ts is None or st.last_ts is None or float(ts) != float(st.last_ts))
        if span_now is not None and fresh:
            st.observe(span_now, _relatch_frac(), _relatch_n())
        st.last_ts = None if ts is None else float(ts)
        span = st.span if st.span is not None else span_now
        if span is None or span <= 1.0:
            st.n_open += 1
            return legacy                      # no cap yet and no latch: keep the box ruler
        if st.span is not None:
            st.n_latched += 1
        S = scale()
        fill = S * (float(base) - top_f) / float(span)
        fill = max(0.0, min(100.0, fill))
        # Is this frame's cap the capsule's own?  It must not sit on the box's top edge and
        # it must imply a span inside the latch's band.  178 of 7580 measured frames of
        # session_20260917_050219 (2.3 %) fail that -- all of them frames where the box had
        # grown 25-40 % past the capsule and the green component selected was not the cap.
        cap_ok = (cap is not None and not clipped
                  and abs(float(base - cap[0]) - float(span)) <= _relatch_frac() * float(span))
        green = legacy[1]
        if green is not None and cap is not None:
            if cap_ok:
                g_end = max(0.0, min(100.0, S * float(base - cap[0]) / float(span)))
                g_start = max(0.0, min(100.0, S * float(base - cap[1]) / float(span)))
            else:
                # The band's TOP is the apex, which on this ruler is S by construction; only
                # its measured HEIGHT survives.  Never mix a box-ruler band with this fill.
                g_end = S
                g_start = max(0.0, S - S * float(cap[1] - cap[0]) / float(span))
            if g_end < g_start:
                g_start, g_end = g_end, g_start
            green = (round(g_start, 2), round(g_end, 2),
                     round(0.5 * (g_start + g_end), 2), round(g_end - g_start, 2),
                     green[4], green[5])
        st.dbg = {"base": int(base), "top": round(top_f, 3), "top_int": int(top),
                  "apex": None if cap is None else int(cap[0]),
                  "clipped": int(clipped), "cap_ok": int(cap_ok),
                  "span": round(float(span), 3),
                  "span_now": None if span_now is None else round(span_now, 3),
                  "latched": int(st.span is not None),
                  "hist_n": len(st.hist), "n_out": int(st.n_out),
                  "n_relatch": int(st.n_relatch), "fill": round(fill, 3),
                  "box_fill": round(float(legacy[0]), 3),
                  "box_green_end": (None if legacy[1] is None
                                    else round(float(legacy[1][1]), 3)),
                  "green_end": (None if green is None
                                else round(float(green[1]), 3))}
        try:
            reader._dbg_pill_ruler = st.dbg
        except Exception:
            pass
        # Native may only interpolate a phase crossing between samples carrying the same
        # ruler identity; a re-latch is a new ruler, so it must bump the generation.
        _stamp(reader, st, span)
        return round(fill, 2), green, top
    except Exception:
        return legacy
