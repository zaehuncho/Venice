"""Sub-pixel fill-top: the served _measure_track fill-top is sharpened by a LINEAR half-max crossing
of the red-column row profile, which resolves sub-row edge motion the integer box edge cannot -- the
precision fix that de-quantizes track.fill_pct feeding the native velocity/tip estimator.

Uses a plain linear crossing, NOT the classical subpixel_fill_metrics' _gaussian_threshold_crossing:
that adds a parabolic peak-refinement tuned for a bump/flank and snaps a MONOTONE fill edge to
half-integers (it measurably reduces fill resolution here). This test locks in the linear behavior.

(The trained CNN reader, ORION_METER_READER, is deliberately NOT used: it was measured WORSE than
the classical row-counter on the rise, 5.70% vs 3.12% residual, and stays gated off.)"""
import numpy as np


def _linear_crossing(profile, thr):
    """The exact interpolator used in meter_detector._measure_track: first rising linear crossing."""
    for i in range(1, len(profile)):
        a, b = float(profile[i - 1]), float(profile[i])
        if a < thr <= b:
            return (i - 1) + (thr - a) / max(1e-9, b - a)
    return -1.0


def _edge_profile(top_row, n=40, width=18.0, blur=1.3):
    """A realistic red-column row profile: a smooth (blurred, ~2-3 row) rise from ~0 above the edge
    to `width` below it, centered at the sub-pixel `top_row` -- a 720p meter fill edge after blur."""
    rows = np.arange(n, dtype=np.float64)
    return width / (1.0 + np.exp(-(rows - top_row) / blur))


def test_crossing_is_subpixel_monotone():
    thr = 9.0   # half of width -> crosses at the logistic center == the true sub-pixel edge
    crossings, truths = [], []
    for k in range(12):
        top = 12.0 + 0.25 * k
        c = _linear_crossing(_edge_profile(top), thr)
        assert c >= 0.0
        crossings.append(c); truths.append(top)
    crossings, truths = np.array(crossings), np.array(truths)
    assert np.max(np.abs(crossings - truths)) <= 0.3, np.abs(crossings - truths)   # sub-pixel
    assert np.all(np.diff(crossings) > 0.05)                                       # strictly rising
    int_edges = [int(np.argmax(_edge_profile(12.0 + 0.25 * k) >= thr)) for k in range(12)]
    assert len(set(int_edges)) < len(set(np.round(crossings, 3).tolist()))          # de-quantized


def test_crossing_beats_integer_edge_rms():
    thr = 9.0
    truths = np.array([12.0 + 0.2 * k for k in range(15)])
    sub = np.array([_linear_crossing(_edge_profile(t), thr) for t in truths])
    integ = np.array([float(np.argmax(_edge_profile(t) >= thr)) for t in truths])
    rms_sub = float(np.sqrt(np.mean((sub - truths) ** 2)))
    rms_int = float(np.sqrt(np.mean((integ - truths) ** 2)))
    assert rms_sub < rms_int, (rms_sub, rms_int)
    assert rms_sub < 0.25    # sub-pixel


def test_measure_track_uses_linear_crossing():
    """Guard: the production code path uses the plain linear crossing (no parabolic snap)."""
    import inspect
    import meter_detector
    src = inspect.getsource(meter_detector.MeterDetector._measure_track)
    assert "ORION_METER_SUBPIXEL_FILL" in src
    assert "_gaussian_threshold_crossing(" not in src   # no parabolic-snap call in this path
