"""Landmark-anchored Pill fill ruler (pill_fill_ruler.py + the `pill` hook in
SimpleMeterReader._measure_fill_in_box).

The 2K27 Pill proposer box is a REGRESSED box: it carries ~8.5 px of pedestal below the
capsule's fill base and ~7 px of cap above the green dome at 720p, so the shipped
box-relative ruler reads ``7.5 + 0.88 * true`` (measured on datasets/meter2k27_pill_park)
and the engine's 20 % tip-phase anchor fires ~30 ms early.  These tests render that exact
geometry -- a capsule with EXPLICIT pedestal/cap padding, ladder dividers, a green dome --
and lock in:

  * the landmark ruler removes BOTH the intercept and the slope error (S * true/100);
  * box-bottom jitter no longer moves the fill (the box ruler does move);
  * divider gaps are bridged when the base is located;
  * the apex/base span latches from the MEDIAN of the first 3 clean frames -- never the
    first frame -- and re-latches only on a > 15 % height jump;
  * S is the single scale knob (ORION_PILL_RULER_SCALE) and puts the apex exactly where
    Arrow2's frozen green_end lands;
  * ORION_PILL_RULER=0 and every non-`pill` style are byte-identical to the shipped path.
"""
import numpy as np
import pytest

import pill_fill_ruler as pfr
from simple_meter_reader import SimpleMeterReader

# ---- measured Pill geometry @720p (proposer box ~20x116, core cols 8-11, pitch ~8) ----
BH, BW = 116, 20
CORE0, CORE1 = 8, 12
PAD_TOP = 7                 # rows of box ABOVE the green apex (measured cap padding)
PAD_BOT = 9                 # rows of box BELOW the fill base (measured pedestal)
PITCH = 8
DIV_H = 2


class _Cfg(object):
    meter_style = "Pill"
    meter_color = "White"
    confidence_threshold = 0.32


def _render(edge_row, bh=BH, bw=BW, pad_top=PAD_TOP, pad_bot=PAD_BOT,
            dividers=True, green=True, core0=CORE0, core1=CORE1):
    """BGR capsule patch.  apex = pad_top, fill base row = bh - pad_bot - 1."""
    patch = np.zeros((bh, bw, 3), np.uint8)
    patch[:] = (40, 42, 45)
    base_row = bh - pad_bot                      # first row BELOW the fill
    patch[pad_top:base_row, core0:core1] = (60, 65, 70)      # empty track
    e = int(round(edge_row))
    patch[e:base_row, core0:core1] = (250, 252, 253)         # fill core
    if dividers:
        for d0 in range(base_row - PITCH - DIV_H, pad_top + 3, -(PITCH + DIV_H)):
            if d0 >= e:
                patch[d0:d0 + DIV_H, core0:core1] = (100, 105, 110)
    if green:
        patch[pad_top:pad_top + 3, core0 - 1:core1 + 1] = (60, 200, 60)
    patch[base_row:base_row + 3, core0:core1] = (168, 170, 171)   # base furniture
    return patch


def _landmarks(bh=BH, pad_top=PAD_TOP, pad_bot=PAD_BOT):
    """(apex_row, base_row) as the ruler must measure them."""
    return pad_top, bh - pad_bot - 1


def _frame_with_box(patch, x=600, y=300, pad=40):
    H = patch.shape[0] + 2 * pad + y
    W = patch.shape[1] + 2 * pad + x
    f = np.full((H, W, 3), 35, np.uint8)
    f[y:y + patch.shape[0], x:x + patch.shape[1]] = patch
    return f, (x, y, patch.shape[1], patch.shape[0])


def _reader(style="Pill"):
    cfg = _Cfg()
    cfg.meter_style = style
    return SimpleMeterReader(1280, 720, cfg=cfg)


def _measure(r, patch, ts=None, x=600, y=300):
    frame, box = _frame_with_box(patch, x=x, y=y)
    return r._measure_fill_in_box(frame, box, ts=ts)


def _warm(r, ts0=0.0, **kw):
    """Three clean frames so the span latch is seeded (median-of-3, never frame 1)."""
    for k in range(3):
        _measure(r, _render(60 - k, **kw), ts=ts0 + k / 60.0, **{})
    return r


# ------------------------------------------------------------------ the ruler itself
def test_landmark_ruler_removes_box_intercept_and_slope():
    """new == S/100 * true, with no intercept and no slope error, while the shipped box
    ruler carries both (that IS the ~30 ms early anchor)."""
    r = _reader()
    _warm(r)
    S = pfr.scale()
    apex, base = _landmarks()
    true_v, new_v = [], []
    for k, edge in enumerate(range(base - 8, apex + 3, -6)):
        fill, _g, top = _measure(r, _render(edge), ts=1.0 + k / 60.0)
        true = 100.0 * (base - edge) / float(base - apex)
        assert top >= 0, edge
        true_v.append(true)
        new_v.append(fill)
        assert abs(fill - S / 100.0 * true) <= 1.5, (edge, true, fill)
    m, c = np.polyfit(np.array(true_v), np.array(new_v), 1)
    assert abs(m - S / 100.0) <= 0.02, m
    assert abs(c) <= 1.5, c


def test_box_ruler_is_the_biased_one():
    """Control: with the landmark ruler off, the SAME frames read 7-9 pp high at low
    fill (the measured `7.5 + 0.88 * hand`).  Without this the test above proves nothing."""
    r = _reader()
    apex, base = _landmarks()
    edge = base - 12                               # true ~12 %
    true = 100.0 * (base - edge) / float(base - apex)
    pfr_on, _g, _t = _measure(r, _render(edge), ts=5.0)
    r_off = _reader()
    import os
    os.environ["ORION_PILL_RULER"] = "0"
    try:
        box_fill, _g2, _t2 = _measure(r_off, _render(edge), ts=5.0)
    finally:
        os.environ.pop("ORION_PILL_RULER", None)
    assert box_fill > true + 4.0, (box_fill, true)
    assert abs(pfr_on - pfr.scale() / 100.0 * true) <= 2.0, (pfr_on, true)


def test_box_bottom_jitter_does_not_move_the_landmark_fill():
    """+/-3 px of box-bottom jitter (the measured proposer wobble) is a 2.5 pp swing on
    the box ruler and must be ~0 on the landmark ruler: the numerator and the denominator
    are both capsule landmarks."""
    r = _reader()
    _warm(r)
    apex, base = _landmarks()
    edge = base - 45
    new_vals, box_vals = [], []
    import os
    for k, jit in enumerate((-3, -1, 0, 1, 3)):
        patch = _render(edge, bh=BH + jit, pad_bot=PAD_BOT + jit)
        new_vals.append(_measure(r, patch, ts=10.0 + k / 60.0)[0])
        os.environ["ORION_PILL_RULER"] = "0"
        try:
            box_vals.append(_measure(_reader(), patch, ts=10.0 + k / 60.0)[0])
        finally:
            os.environ.pop("ORION_PILL_RULER", None)
    assert max(new_vals) - min(new_vals) <= 0.6, new_vals
    assert max(box_vals) - min(box_vals) >= 1.5, box_vals


def test_divider_gaps_are_bridged_to_the_capsule_base():
    """The ladder's 2-row dividers must not truncate the base search: the measured base
    is the capsule's, with or without dividers."""
    apex, base = _landmarks()
    for dividers in (True, False):
        patch = _render(base - 60, dividers=dividers)
        hsv_white = _white(patch)
        got = pfr.fill_base(hsv_white, patch.shape[0], patch.shape[1], base - 60)
        assert got == base, (dividers, got, base)


def _white(patch):
    import cv2
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    return (hsv[:, :, 2] >= 200) & (hsv[:, :, 1] <= 65)


def test_green_band_rides_the_new_ruler():
    """The make window must be published on the SAME ruler as the fill, so its top lands
    on S (Arrow2's frozen green_end)."""
    r = _reader()
    _warm(r)
    apex, base = _landmarks()
    _f, green, _t = _measure(r, _render(base - 50), ts=20.0)
    assert green is not None
    assert abs(green[1] - pfr.scale()) <= 1.2, green


# ------------------------------------------------------------------------ the latch
def test_span_latch_needs_three_frames_and_takes_their_median():
    """Never a first-frame latch: the box and the cap are least reliable at acquisition."""
    r = _reader()
    apex, base = _landmarks()
    spans = []
    for k, pad in enumerate((9, 11, 10)):          # base moves -> span 100, 98, 99
        _measure(r, _render(base - 40, pad_bot=pad), ts=30.0 + k / 60.0)
        d = r._dbg_pill_ruler
        spans.append(d["span_now"])
        assert d["latched"] == (1 if k >= 2 else 0), (k, d)
    st = r._pill_ruler
    assert st.span == float(np.median(spans[:3])), (st.span, spans)


def test_a_burst_of_bad_apex_frames_cannot_move_the_latch():
    """THE ep35 REGRESSION (session_20260917_050219): the proposer box breathed
    114 -> 140 -> 112 px over ~20 frames on a FROZEN meter, the cap mis-measured, and a
    3-frame latch re-seeded onto a span 9 px too long -- then held that wrong scale for ten
    shots.  A rolling-median latch must ride straight through it."""
    r = _reader()
    apex, base = _landmarks()
    for k in range(40):                            # a healthy window of the real span
        _measure(r, _render(base - 40), ts=200.0 + k / 60.0)
    good = r._pill_ruler.span
    fills = []
    for k in range(10):                            # cap 9 px high: span reads 9 px short
        patch = _render(base - 40, pad_top=PAD_TOP + 9)
        fills.append(_measure(r, patch, ts=201.0 + k / 60.0)[0])
    assert r._pill_ruler.span == good, (r._pill_ruler.span, good)
    assert r._pill_ruler.n_relatch == 0
    # and the emitted fill is the one the good ruler gives, on every burst frame
    want = pfr.scale() * (base - (base - 40)) / good
    assert max(abs(f - want) for f in fills) <= 0.05, (fills, want)


def test_a_cap_clipped_by_the_box_edge_cannot_teach_the_latch():
    """THE ep46 REGRESSION: when the proposer box grew past the capsule the green cap ran
    off the box's TOP edge and measured apex=0 for seven consecutive frames; those seven
    confirmed a re-latch onto a 41 %-too-long span and cost 15 frames of a 23 pp error.
    A cap on the box edge is clipped, so it is not a span measurement at all."""
    r = _reader()
    for k in range(30):
        _measure(r, _render(60), ts=300.0 + k / 60.0)
    good = r._pill_ruler.span
    for k in range(12):                            # apex sits on row 0 = clipped
        patch = _render(60, pad_top=0)
        fill, green, _t = _measure(r, patch, ts=301.0 + k / 60.0)
    assert r._pill_ruler.span == good, (r._pill_ruler.span, good)
    assert r._pill_ruler.n_relatch == 0 and r._pill_ruler.n_out == 0
    assert r._dbg_pill_ruler["clipped"] == 1 and r._dbg_pill_ruler["cap_ok"] == 0
    # the band still rides THIS ruler -- its top is the apex, i.e. S by construction
    assert green is not None and abs(green[1] - pfr.scale()) < 1e-6, green
    assert 0.0 < green[1] - green[0] <= pfr.scale(), green


def test_relatch_only_on_a_sustained_rescale():
    """Jitter and short bursts keep the latch; a SUSTAINED > 15 % height change (a real
    camera rescale) drops it."""
    r = _reader()
    _warm(r)
    for k in range(10):                            # a healthy window first
        _measure(r, _render(60), ts=39.0 + k / 60.0)
    st = r._pill_ruler
    latched = st.span
    _measure(r, _render(60, pad_bot=PAD_BOT + 3), ts=40.0)
    assert r._pill_ruler.span == latched and r._pill_ruler.n_relatch == 0
    big = int(round(BH * 1.4))
    big_patch = _render(int(big * 0.55), bh=big, pad_bot=int(PAD_BOT * 1.4))
    for k in range(7):                             # under the confirmation count
        _measure(r, big_patch, ts=41.0 + k / 60.0)
    assert r._pill_ruler.span == latched and r._pill_ruler.n_relatch == 0
    _measure(r, big_patch, ts=41.2)                # the 8th confirms it
    assert r._pill_ruler.n_relatch == 1
    assert r._pill_ruler.span is None or r._pill_ruler.span != latched


def test_new_lock_carries_and_revalidates_the_previous_span():
    """A press is a new lock; carrying the measured scale avoids paying the seeding delay
    again (ORION_PILL_RULER_CARRY, the _subpx_carry_pending philosophy)."""
    r = _reader()
    _warm(r)
    span = r._pill_ruler.span
    r._physical_shot_epoch = 7                     # a new lock key
    _measure(r, _render(50), ts=50.0)
    d = r._dbg_pill_ruler
    assert d["latched"] == 1 and abs(d["span"] - span) < 1e-6, (d, span)
    assert r._pill_ruler.key == (r._det_pixel_ruler_epoch, 7)


def test_carry_off_reseeds_the_new_lock(monkeypatch):
    monkeypatch.setenv("ORION_PILL_RULER_CARRY", "0")
    r = _reader()
    _warm(r)
    r._physical_shot_epoch = 9
    _measure(r, _render(50), ts=60.0)
    assert r._dbg_pill_ruler["latched"] == 0


def test_scale_knob_is_the_only_scale(monkeypatch):
    """S=100 makes the ruler read the hand fill outright; S=96 scales it by 0.96."""
    apex, base = _landmarks()
    edge = base - 50
    true = 100.0 * (base - edge) / float(base - apex)
    got = {}
    for S in ("100", "96"):
        monkeypatch.setenv("ORION_PILL_RULER_SCALE", S)
        r = _reader()
        _warm(r)
        got[S] = _measure(r, _render(edge), ts=70.0)[0]
    assert abs(got["100"] - true) <= 1.5, (got, true)
    assert abs(got["96"] - 0.96 * true) <= 1.5, (got, true)


# ------------------------------------------------------------- fail-open / no-op paths
def test_no_green_cap_falls_back_to_the_box_ruler(monkeypatch):
    """A capsule whose dome is not visible and no latch yet = no ruler: the shipped box
    value must come through untouched, never a hole."""
    r = _reader()
    patch = _render(60, green=False)
    got = _measure(r, patch, ts=80.0)
    monkeypatch.setenv("ORION_PILL_RULER", "0")
    want = _measure(_reader(), patch, ts=80.0)
    assert got == want, (got, want)


def test_flag_off_is_byte_identical(monkeypatch):
    r_on = _reader()
    monkeypatch.setenv("ORION_PILL_RULER", "0")
    r_off = _reader()
    monkeypatch.delenv("ORION_PILL_RULER")
    apex, base = _landmarks()
    for k, edge in enumerate(range(base - 6, apex + 4, -7)):
        patch = _render(edge)
        got_on = _measure(r_on, patch, ts=90.0 + k / 60.0)
        monkeypatch.setenv("ORION_PILL_RULER", "0")
        got_off = _measure(r_off, patch, ts=90.0 + k / 60.0)
        monkeypatch.delenv("ORION_PILL_RULER")
        if k >= 3:                                  # once the latch is warm they differ
            assert got_on[0] != got_off[0], (edge, got_on, got_off)
        assert got_off[2] == got_on[2]              # the EDGE row is never touched


@pytest.mark.parametrize("style", ["Arrow2", "Straight", "Dial", "", None])
def test_non_pill_styles_are_untouched(style):
    """The hook is reachable only from _tracking_meter_style == 'pill'.  Every other
    style must measure the SAME Pill patch exactly as the shipped reader does."""
    import os
    cfg = _Cfg()
    cfg.meter_style = style
    r = SimpleMeterReader(1280, 720, cfg=cfg)
    os.environ["ORION_PILL_RULER"] = "0"
    try:
        r_ref = SimpleMeterReader(1280, 720, cfg=cfg)
        apex, base = _landmarks()
        for k, edge in enumerate(range(base - 6, apex + 4, -9)):
            patch = _render(edge)
            assert _measure(r_ref, patch, ts=100.0 + k / 60.0) == \
                   _measure(r, patch, ts=100.0 + k / 60.0)
    finally:
        os.environ.pop("ORION_PILL_RULER", None)
    assert getattr(r, "_dbg_pill_ruler", None) is None
    assert getattr(r, "_pill_ruler", None) is None


def test_arrow2_solid_ribbon_never_enters_the_ruler():
    """A style name is not the gate that matters live -- prove the Arrow2 SHAPE measured
    under a Pill style still fails open rather than inventing a span."""
    r = _reader()
    bh, bw = 107, 24
    patch = np.zeros((bh, bw, 3), np.uint8)
    patch[:] = (40, 42, 45)
    patch[:100, 5:bw - 5] = (30, 70, 140)
    patch[40:100, 5:bw - 5] = (250, 252, 253)
    patch[100:103, 5:bw - 5] = (168, 170, 171)
    fill, _g, top = _measure(r, patch, ts=110.0)
    assert top >= 0
    # no green dome -> no span -> the box ruler's own value, unchanged
    import os
    os.environ["ORION_PILL_RULER"] = "0"
    try:
        want = _measure(_reader(), patch, ts=110.0)
    finally:
        os.environ.pop("ORION_PILL_RULER", None)
    assert (fill, top) == (want[0], want[2])


def test_estimator_generation_is_keyed_on_the_ruler_not_the_lock():
    """Native may only interpolate a phase crossing between samples on ONE ruler.  A
    latched span must hold its generation across frames AND across a press that carries
    it; a real re-latch must bump it."""
    r = _reader()
    _warm(r)
    gens = []
    for k, edge in enumerate((60, 57, 54)):
        _measure(r, _render(edge), ts=120.0 + k / 60.0)
        gens.append(r._last_fill_estimator_generation)
    assert len(set(gens)) == 1 and gens[0] > 0, gens
    r._physical_shot_epoch = 3                       # a press carrying the same span
    _measure(r, _render(50), ts=121.0)
    assert r._last_fill_estimator_generation == gens[0]
    big = int(round(BH * 1.4))
    big_patch = _render(int(big * 0.55), bh=big, pad_bot=int(PAD_BOT * 1.4))
    for k in range(9):
        _measure(r, big_patch, ts=122.0 + k / 60.0)
    assert r._last_fill_estimator_generation != gens[0]


def test_module_helpers_refuse_garbage():
    """core_band / fill_base must fail closed, never guess."""
    empty = np.zeros((BH, BW), dtype=bool)
    assert pfr.core_band(empty, BH, BW) is None
    assert pfr.fill_base(empty, BH, BW, 40) is None
    floating = np.zeros((BH, BW), dtype=bool)
    floating[20:40, CORE0:CORE1] = True             # never reaches the capsule base
    assert pfr.fill_base(floating, BH, BW, 20) is None
