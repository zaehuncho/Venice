"""Controlled early moving-meter pickup in a cluttered anchor patch."""
import os

import cv2
import numpy as np
import pytest

from meter_locator_cv import MeterContourLocator
from player_anchor import Anchor


def scene(x=800, y=300, fill=15, scale=1., clutter=True):
    frame = np.zeros((720, 1280, 3), np.uint8)
    frame[y+108-fill:y+108, x+7:x+18] = 255
    frame[y+8:y+12, x+10:x+15] = (0, 200, 0)
    if clutter:
        # Twelve larger bright glyphs in the scan patch, but well ABOVE the
        # anchor's bottom band. They are not eligible owner-meter candidates.
        for xx in (715, 752, 789, 826, 863, 900):
            for yy in (218, 264):
                frame[yy:yy+30, xx:xx+11] = 255
    if scale != 1.:
        frame = cv2.resize(frame, None, fx=scale, fy=scale,
                           interpolation=cv2.INTER_NEAREST)
    return frame


@pytest.fixture
def make(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("ORION_CV_", "ORION_METER_", "ORION_ANCHOR", "ORION_PLAYER_")):
            monkeypatch.delenv(key, raising=False)
    def create(scale=1., confidence=.9):
        loc = MeterContourLocator()
        anchor = Anchor(783*scale, 561*scale, 1., confidence, .9, .9, 4, 10,
                        -30*scale, 150*scale, 85*scale, 70*scale,
                        130*scale, round(1280*scale), round(720*scale))
        monkeypatch.setattr(loc, "_update_anchor", lambda *_: anchor)
        monkeypatch.setattr(loc, "_press_state", lambda _: (True, 800., "Left Fade"))
        return loc, anchor
    return create


@pytest.mark.parametrize("scale", [1., 1.5, 2.])
@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("confidence", [.35, .9])
def test_clutter_does_not_delay_small_moving_fade_pickup(make, scale, direction, confidence):
    loc, anchor = make(scale, confidence)
    clean, _ = make(scale, confidence)
    for i in range(4):
        x, y, fill = 800+direction*9*i, 300-3*i, 15+3*i
        expected = clean.detect_box(scene(x, y, fill, scale, clutter=False), ts=10+i/60)
        got = loc.detect_box(scene(x, y, fill, scale), ts=10+i/60)
        assert expected is not None
        assert got == expected, "out-of-band glyphs consumed the 12-candidate budget"
        assert anchor.contains_box(*got[:4])
    assert loc.stats['col_cands'] <= 12*4, "keep the expensive candidate budget bounded"


@pytest.mark.parametrize("side", [-1, 1])
def test_excluded_neighbour_still_rejects_a_white_twin(make, side):
    loc, anchor = make()
    x = 880 if side == 1 else 720
    frame = scene(x=x, fill=40, clutter=False)
    twin = x+31 if side == 1 else x-18
    frame[368:408, twin:twin+11] = 255
    assert anchor.contains_box(x, 304, 26, 107)
    assert not anchor.contains_box(twin-7, 304, 26, 107)
    assert loc.detect_box(frame, ts=10) is None
    assert loc.stats['not_lone'] > 0


@pytest.mark.parametrize("scale", [1., 1.25, 1.5, 1.75, 2.])
def test_filter_preserves_exact_boundary_and_rejects_just_outside(make, scale):
    loc, anchor = make(scale)
    frame = scene(scale=scale, clutter=False)
    first = loc.detect_box(frame, ts=10)
    assert first is not None
    cx, bottom = first[0]+first[2]*.5, first[1]+first[3]
    # Call the real candidate finder to isolate raster rounding at the exact
    # authoritative boundary. One pixel of prefilter slack may not admit a box.
    for dx, dy, accepted in ((0, 0, True), (.01, 0, False), (0, .01, False)):
        anchor.cx_lo, anchor.cx_hi = cx+dx, cx+dx
        anchor.bot_lo, anchor.bot_hi = bottom+dy, bottom+dy
        result = loc._find(frame, scale, anchor_bounds=anchor)
        assert (result is not None) is accepted
        if accepted:
            assert result == first


def test_all_out_of_bounds_clutter_never_mints_a_meter(make):
    loc, anchor = make()
    frame = scene(fill=0)
    assert loc._find(frame, 1., anchor_bounds=anchor) is None
    assert loc._find_col is None
    assert loc.detect_box(frame, ts=10) is None


def test_in_bounds_candidates_still_share_the_same_twelve_slot_budget(make):
    loc, anchor = make()
    frame = np.zeros((720, 1280, 3), np.uint8)
    for i in range(13):
        x = 100+80*i
        frame[368:408, x:x+11] = 255
        frame[308:312, x+3:x+8] = (0, 200, 0)
    anchor.cx_lo, anchor.cx_hi = 0, 1280
    anchor.bot_lo, anchor.bot_hi = 0, 720
    assert loc._find(frame, 1., anchor_bounds=anchor) is not None
    assert loc.stats['col_cands'] == 12


@pytest.mark.parametrize("fill", [15, 35, 70, 96])
def test_no_anchor_keeps_the_existing_general_scan(make, fill):
    loc, _ = make()
    frame = scene(fill=fill)
    # The new budget filter is confined to an existing landmark hypothesis.
    loc._find(frame, 1.)
    assert loc.stats.get('anchor_budget_skip', 0) == 0
