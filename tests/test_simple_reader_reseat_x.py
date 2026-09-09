"""[ORION_READER_RESEAT_X 2026-09-08] A held box that reads fill 0.0 is re-seated on the white
ribbon visible in this frame (+-18 px), instead of holding a zero for 80-180 ms until the tracker
catches up. Audit (329 shots, 09-03): 26 shots held a zero-fill lock for 82 ms median with the
served box 13 px (7-34) beside the meter -- the worker's 30-50 ms-old proposal during a pan.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_simple_reader_ghost_press import (  # noqa: E402
    BH, BW, METER_X, METER_Y, TRUE_BOX, _frame, _latch_press_clock, _reader,
)


def _feed_box(r, ts, box, fill_top):
    r._meter_detector.res = (True, box, 0.9, ts)
    return r.detect(_frame(fill_top), ts=ts)


def _fill(res):
    return float(getattr(res, "fill_pct", 0.0) or 0.0)


def _stage(r, box, t0=1.00, fill_tops=(88, 84, 78, 72, 66, 60)):
    """Press-clock latch, then a rising meter served through `box` every frame."""
    _latch_press_clock(r, t0)
    out = []
    for k, ft in enumerate(fill_tops):
        out.append(_feed_box(r, t0 + 0.50 + 0.03 * k, box, fill_top=ft))
    return out


def test_dx_finds_the_ribbon_beside_a_stale_box_and_fails_closed_otherwise(monkeypatch):
    r = _reader(monkeypatch)
    frame = _frame(fill_top=70)
    # served box 16 px right of the meter -> the ribbon centre is 16 px left of the box centre
    # (the fixture's ribbon centre sits half a pixel off the box centre: +-1 px)
    assert abs(r._reseat_x_dx(frame, (METER_X + 16, METER_Y, BW, BH)) + 16) <= 1
    assert abs(r._reseat_x_dx(frame, (METER_X - 9, METER_Y, BW, BH)) - 9) <= 1
    # on the meter already: the zero read is real, do not move
    assert r._reseat_x_dx(frame, TRUE_BOX) is None
    # beyond the window
    assert r._reseat_x_dx(frame, (METER_X + 30, METER_Y, BW, BH)) is None
    # no ribbon at all
    assert r._reseat_x_dx(_frame(fill_top=None), (METER_X + 16, METER_Y, BW, BH)) is None
    # a wide white object is not the ribbon
    wide = _frame(fill_top=None)
    wide[METER_Y + 60:METER_Y + 100, METER_X - 10:METER_X + 30] = 250
    assert r._reseat_x_dx(wide, (METER_X + 16, METER_Y, BW, BH)) is None


def test_stale_box_reads_fill_on_the_first_frame_and_moves_the_tracker(monkeypatch):
    # this fixture's ribbon is 18 of 24 px wide (the real one ~11 of 23), so it takes 16 px of
    # offset to hard-zero the row walk here; live the audit measured 7-34 px, median 13
    stale = (METER_X + 16, METER_Y, BW, BH)
    good = _stage(_reader(monkeypatch), TRUE_BOX)
    r = _reader(monkeypatch)
    out = _stage(r, stale)
    # every frame of the rise reads a real fill through the stale box, within ~1 pp of the true box
    for a, b in zip(good[2:], out[2:]):
        assert _fill(b) > 0.0, b
        assert abs(_fill(a) - _fill(b)) < 1.2, (_fill(a), _fill(b))
    assert r._det_diag.get("reseat_x", 0) >= 1
    # the served box now sits on the meter
    assert abs(int(r.box[0]) - METER_X) <= 2, r.box


def test_reseat_is_inert_without_the_flag(monkeypatch):
    stale = (METER_X + 16, METER_Y, BW, BH)
    r = _reader(monkeypatch, ORION_READER_RESEAT_X="0")
    out = _stage(r, stale)
    assert all(_fill(o) == 0.0 for o in out[2:]), [_fill(o) for o in out]
    assert r._det_diag.get("reseat_x", 0) == 0
