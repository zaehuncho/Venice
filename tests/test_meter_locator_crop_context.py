"""A roaming crop must not manufacture lone glyphs or a white-floor landmark."""
import os

import numpy as np
import pytest

from meter_locator_cv import MeterContourLocator


def _frame():
    return np.zeros((720, 1280, 3), np.uint8)


def _meter(frame, x, bottom=408, fill=40):
    frame[bottom - fill:bottom, x + 7:x + 18] = 255
    frame[bottom - 100:bottom - 96, x + 10:x + 15] = (0, 200, 0)


@pytest.fixture
def locator(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("ORION_CV_", "ORION_METER_", "ORION_ANCHOR")):
            monkeypatch.delenv(key, raising=False)
    loc = MeterContourLocator()
    loc._last_box = (600, 304, 26, 107)
    loc._last_ts = 1.0
    return loc


@pytest.mark.parametrize("side", ["left", "right"])
def test_roaming_crop_must_not_hide_white_twin(locator, side):
    frame = _frame()
    x, twin = (548, 535) if side == "left" else (650, 679)
    _meter(frame, x)
    frame[368:408, twin:twin + 11] = 255
    assert locator.detect_box(frame, ts=1.1) is None
    assert locator.stats["full"] == 1, "recheck context on this same frame"


@pytest.mark.parametrize("side", ["left", "right"])
def test_crop_stranger_cannot_preempt_real_meter_in_full_band(locator, side):
    frame = _frame()
    x, twin = (548, 535) if side == "left" else (650, 679)
    _meter(frame, x)
    frame[368:408, twin:twin + 11] = 255
    _meter(frame, 800)
    box = locator.detect_box(frame, ts=1.1)
    assert box is not None
    assert box[0] == 800, "full-context meter must win without a frame delay"


def test_roaming_crop_cannot_move_white_floor_to_its_bottom_edge(locator):
    frame = _frame()
    _meter(frame, 600, bottom=480)
    box = locator.detect_box(frame, ts=1.1)
    assert box is not None
    assert box[1] + box[3] == 483, "white floor is 480, not the ROI edge at 475"
    assert locator.stats["full"] == 1


@pytest.mark.parametrize("x", [580, 600, 620])
def test_interior_meter_keeps_fast_roi_path(locator, x):
    frame = _frame()
    _meter(frame, x)
    box = locator.detect_box(frame, ts=1.1)
    assert box is not None and box[0] == x
    assert locator.stats["roi"] == 1
    assert locator.stats["full"] == 0


@pytest.mark.parametrize("x", [548, 650])
def test_genuine_meter_at_crop_edge_recovers_without_waiting(locator, x):
    frame = _frame()
    _meter(frame, x)
    box = locator.detect_box(frame, ts=1.1)
    assert box is not None and box[0] == x
