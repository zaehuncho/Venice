"""Pointwise HSV work reduction must preserve every existing candidate gate."""
import os

import cv2
import numpy as np
import pytest

import meter_locator_cv as mlc
from test_meter_locator_cv import meter_frame


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("ORION_CV_", "ORION_METER_", "ORION_ANCHOR", "ORION_PLAYER_")):
            monkeypatch.delenv(key, raising=False)


def view(frame):
    # The same behavioral cases run against the frozen eager baseline too.
    factory = getattr(mlc, "_CandidateHsv", None)
    return factory(frame) if factory else cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)


@pytest.mark.parametrize("strided", [False, True])
@pytest.mark.parametrize("bounds", [(0, 1, 0, 1), (1, 15, 2, 13), (0, 200, 0, 160),
                                    (195, 250, 150, 200), (-10, 199, -15, 159), (20, 100, 10, 80)])
def test_roi_hsv_is_byte_identical_without_mutating_bgr(strided, bounds):
    rng = np.random.default_rng(20260921)
    raw = rng.integers(0, 256, (400, 320, 3), dtype=np.uint8)
    frame = raw[::2, ::2] if strided else raw[:200, :160].copy()
    before = raw.copy()
    original = frame.copy()
    y0, y1, x0, x1 = bounds
    eager = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    local = view(frame)
    assert local.shape == eager.shape
    np.testing.assert_array_equal(local[y0:y1, x0:x1], eager[y0:y1, x0:x1])
    np.testing.assert_array_equal(frame, original)
    np.testing.assert_array_equal(raw, before)


@pytest.mark.parametrize("scale", [1., 1.5, 2.])
@pytest.mark.parametrize("mode", ["full", "roi", "parent"])
def test_candidate_converts_only_small_hsv_rectangles(monkeypatch, scale, mode):
    frame, _ = meter_frame()
    if scale != 1.:
        frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    x0, y0 = (int(500*scale), int(280*scale)) if mode == "roi" else (0, 0)
    if mode == "parent":
        y0 = int(330*scale)  # the genuine tip sits above this artificial crop
    x1 = int(750*scale) if mode == "roi" else frame.shape[1]
    sub = frame[y0:, x0:x1]
    pixels = []
    real = cv2.cvtColor
    def counted(image, code, *args, **kwargs):
        if code == cv2.COLOR_BGR2HSV:
            pixels.append(image.shape[0]*image.shape[1])
        return real(image, code, *args, **kwargs)
    monkeypatch.setattr(cv2, "cvtColor", counted)
    loc = mlc.MeterContourLocator()
    got = loc._find(sub, scale, parent=(frame, x0, y0))
    assert got is not None and got[4] == .9
    assert loc.stats.get("shape_error", 0) == 0
    assert pixels and sum(pixels) < sub.shape[0]*sub.shape[1]*.10, pixels


@pytest.mark.parametrize("cx", [0., 2.5, 80.5, 156.5])
@pytest.mark.parametrize("gtop", [-3., 0., 15., 185.])
def test_compact_tip_and_tipless_gates_match_eager_at_boundaries(cx, gtop):
    rng = np.random.default_rng(741)
    parent = rng.integers(0, 256, (260, 220, 3), dtype=np.uint8)
    sub = parent[30:230, 25:185]
    eager = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    local = view(sub)
    a, b = mlc.MeterContourLocator(), mlc.MeterContourLocator()
    context = (parent, 25, 30)
    assert a._compact_tip(eager, cx, gtop, .9, 1., context) == b._compact_tip(local, cx, gtop, .9, 1., context)
    assert a._tipless_track_supported(eager, cx, gtop, 1., context) == b._tipless_track_supported(local, cx, gtop, 1., context)
    assert a.stats == b.stats
