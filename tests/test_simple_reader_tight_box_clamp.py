"""BOX-TIGHT translated-shape height clamp (session_20260830_191051).

The engine-facing rectangle is re-shaped at the detect() boundary by
``_tight_display_box`` (ORION_READER_BOX_TIGHT, the launcher runs mode 2). The FRESH
branch gained a detector-fill h_cap earlier (a poisoned ``_tight_src`` served 29x320..556
rects on 227 frames of session_20260830_112306), but the TRANSLATE branch -- a hold/coast
frame re-serving the last remembered tight shape, plus its ``top_row`` raise -- stayed
unbounded. Live epochs 16/32/41/42 published 25x349..420 rectangles (bottom anchored on
the real meter, top ~230px up a bright wall seam) for 100-200ms right after the cold
post-ghost acquire; the engine consumed that geometry as a teleport (meter_jump
0.16-0.26) and distrusted the early rise, which is the reader-side half of the two
live_tip_deadline_missed aborts. These tests pin the bottom-anchored clamp on the
translate branch and the untouched legacy behaviour when h_cap == 0 (colour path).
"""
import numpy as np

from simple_meter_reader import SimpleMeterReader


class _CfgWhite:
    meter_style = "Arrow2"
    meter_color = "White"
    confidence_threshold = 0.32


def _reader(monkeypatch):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    monkeypatch.setenv("ORION_METER_DETECTOR_TRACK", "0")
    r = SimpleMeterReader(1280, 720, cfg=_CfgWhite(), require_gameplay_eligibility=True)
    r._box_tight = 2
    r._tight_src = None
    return r


SERVED = [1064, 328, 25, 119]          # the raw served box (real meter, bottom = 447)
HCAP = int(round(1.6 * 119))           # the detector-fill cap the call site computes


# The hug clamp (2026-08-31, session_20260831_114137) SUPERSEDES the h_cap bound
# below. h_cap alone allowed 1.6x the served height, still 44-66px of over-reach
# above/below the meter -- the drawn box toggled 120<->176 and read as flicker. The
# hug clamp forces the drawn box to COVER the served meter span and extend no more
# than TOP_REACH/BOT_REACH (18px) past either edge, which is tighter than h_cap and
# also re-anchors a floated-off box, so these live artefacts come back TIGHTER than
# the round-2 assertions expected -- a strengthening, not a regression. The served
# box here (h=119) is meter-plausibly tall, so the clamp is active.
SERVED_TOP = SERVED[1]                  # 328
SERVED_BOT = SERVED[1] + SERVED[3]      # 447


def test_translated_tall_shape_is_hugged_to_the_served_span(monkeypatch):
    """A poisoned remembered shape (th=420, top 240px above the served box -- the live
    epoch-16/41 artefact) must come back hugging the served meter: top no more than
    TOP_REACH above the served top, base no more than BOT_REACH below the served base."""
    r = _reader(monkeypatch)
    r._tight_off = (0, -240, 29, 420)
    out = r._tight_display_box(list(SERVED), top_row=-1, h_cap=HCAP)
    # top within the reach of the served top and still covering the cap; base pulled up
    # from 508 (61px below the meter) to the served base plus the reach (465)
    assert SERVED_TOP - r._tight_top_reach <= out[1] <= SERVED_TOP
    assert out[1] + out[3] == SERVED_BOT + r._tight_bot_reach   # 465
    assert out[3] < HCAP                                         # strictly tighter than h_cap


def test_top_row_raise_is_hugged(monkeypatch):
    """A bogus top_row (the White track-top search running up a bright wall seam, live
    values 25-100 vs a real top ~330) used to stretch the translated box unbounded; the
    hug clamp pins the top to the served top minus the reach."""
    r = _reader(monkeypatch)
    r._tight_off = (0, -8, 29, 130)
    out = r._tight_display_box(list(SERVED), top_row=100, h_cap=HCAP)
    assert out[1] == SERVED_TOP - r._tight_top_reach            # top no longer up at row 100
    assert out[1] + out[3] <= SERVED_BOT + r._tight_bot_reach
    assert out[3] < HCAP


def test_colour_path_is_also_hug_clamped(monkeypatch):
    """h_cap == 0 (colour/track path) is where the live 2026-08-31 flicker lived -- the
    emit stage was detector_fill but the toggle was the mode-2 union running the top up
    the wall seam. The hug clamp now bounds this path too (it was previously uncapped)."""
    r = _reader(monkeypatch)
    r._tight_off = (0, -240, 29, 420)
    out = r._tight_display_box(list(SERVED), top_row=-1, h_cap=0)
    assert out[1] == SERVED_TOP - r._tight_top_reach            # 310, was 88 (240px up)
    assert out[1] + out[3] == SERVED_BOT + r._tight_bot_reach   # 465, was 508
    assert out[3] <= SERVED[3] + r._tight_top_reach + r._tight_bot_reach


def test_short_stub_served_box_still_unions_a_taller_track(monkeypatch):
    """A served box too short to be the meter (a bar-only stub, h below HUG_MIN_FRAC of
    the frame) must be LEFT to mode-2's taller reconstructed track -- the hug clamp trusts
    the served box as the meter only when it is meter-plausibly tall, so the design of
    unioning a taller track when the served box is a stub is preserved unclipped."""
    r = _reader(monkeypatch)
    r._tight_src = ((610, 400, 8, 30), -1, (605, 320, 20, 120), (2, 2))
    out = r._tight_display_box([610, 400, 8, 30], top_row=-1, h_cap=0)
    assert out[3] >= 120                                        # taller track preserved


def test_sane_translated_shape_passes_unchanged(monkeypatch):
    """A healthy remembered shape (~1.1x the served height) already hugs the meter and
    must not be moved by either clamp."""
    r = _reader(monkeypatch)
    r._tight_off = (-2, -7, 29, 133)
    out = r._tight_display_box(list(SERVED), top_row=-1, h_cap=HCAP)
    assert out == [1062, 321, 29, 133]


def test_over_reach_above_the_meter_is_pulled_back(monkeypatch):
    """The dominant live flicker: a fresh mode-2 union whose top ran ~58px above the
    served meter (onto the wall seam). The drawn top must end no more than TOP_REACH
    above the served top, and the drawn box must still contain the served meter span."""
    r = _reader(monkeypatch)
    # fresh union that reaches 58px above the served top and 8px below the base
    r._tight_off = (-2, -58, 29, SERVED[3] + 58 + 8)
    out = r._tight_display_box(list(SERVED), top_row=-1, h_cap=0)
    assert out[1] >= SERVED_TOP - r._tight_top_reach            # top no longer 58px up
    assert out[1] <= SERVED_TOP                                 # still covers the cap
    assert out[1] + out[3] >= SERVED_BOT                        # still covers the base


def test_wrong_fresh_colour_identity_cannot_leave_served_meter(monkeypatch):
    """The 2026-08-31 batch contained 118 full-rate samples whose presentation
    source was more than 18px off the detector-authoritative meter (max 229.5px).
    Reproduce the worst class: a decor column at x=451 while detector_fill is
    correctly serving the meter at x=682.  The outline must snap back around the
    served meter on this same call; no temporal filter or delayed relock is needed."""
    r = _reader(monkeypatch)
    served = [682, 408, 22, 106]
    r._tight_src = ((451, 408, 11, 82), -1, (449, 390, 25, 124), (2, 2))
    out = r._tight_display_box(served, top_row=-1, h_cap=int(1.6 * served[3]))
    assert out[0] <= served[0]
    assert out[0] + out[2] >= served[0] + served[2]
    assert served[0] - out[0] <= r._tight_side_reach
    assert out[0] + out[2] - (served[0] + served[2]) <= r._tight_side_reach
    assert abs((out[0] + out[2] / 2) - (served[0] + served[2] / 2)) \
        <= r._tight_side_reach / 2


def test_horizontal_hug_preserves_local_reference_margin(monkeypatch):
    """A healthy reference-hug already covering the meter keeps its local air;
    the clamp is a containment/identity guard, not a box-width rewrite."""
    r = _reader(monkeypatch)
    r._tight_off = (-8, -7, SERVED[2] + 16, 133)
    out = r._tight_display_box(list(SERVED), top_row=-1, h_cap=HCAP)
    assert out == [SERVED[0] - 8, 321, SERVED[2] + 16, 133]


def test_short_stub_horizontal_track_union_is_not_clipped(monkeypatch):
    """The meter-plausible-height gate applies to both axes: a short bar stub may
    legitimately reconstruct a wider/taller housing and remains untouched."""
    r = _reader(monkeypatch)
    r._tight_src = ((610, 400, 8, 30), -1, (590, 320, 60, 120), (2, 2))
    out = r._tight_display_box([610, 400, 8, 30], top_row=-1, h_cap=0)
    assert out[0] < 610
    assert out[0] + out[2] > 618


def test_hug_clamp_disabled_by_negative_reach(monkeypatch):
    """A negative TOP_REACH disables the whole clamp (the raw mode-2 shape is served)."""
    monkeypatch.setenv("ORION_READER_BOX_TIGHT_TOP_REACH", "-1")
    r = _reader(monkeypatch)
    r._tight_off = (0, -240, 29, 420)
    out = r._tight_display_box(list(SERVED), top_row=-1, h_cap=0)
    assert out == [1064, 88, 29, 420]                           # unclamped legacy translate
