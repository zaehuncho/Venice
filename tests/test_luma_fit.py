"""luma_meter kernels: K2 logistic fill-boundary fit, tip-sliver finder, ROI SAD.

These are the compressed-path (Chiaki H.264 4:2:0) primitives -- chroma is destroyed by the
encode, so the fill boundary is read from the interior Y-column profile. Tests exercise the
artifact classes the fit must survive: deblocking blur, ringing overshoot, sensor/mosquito
noise, and the degenerate near-full window (plateau missing -> frozen to the EMA).
"""
import numpy as np
import pytest

from luma_meter import PlateauEMA, FillFit, fit_fill_boundary, find_tip_sliver, roi_sad

RNG = np.random.default_rng(20260706)


def _profile(n=120, y0=70.3, s=1.5, l_dark=30.0, l_fill=90.0, noise=2.0, rng=RNG):
    y = np.arange(n, dtype=np.float64)
    p = l_dark + (l_fill - l_dark) / (1.0 + np.exp(-(y - y0) / s))
    return p + rng.normal(0.0, noise, n)


def test_clean_step_recovers_subpixel_boundary():
    fit = fit_fill_boundary(_profile())
    assert fit is not None
    assert abs(fit.y0 - 70.3) < 0.35
    assert fit.step == pytest.approx(60.0, abs=8.0)


def test_heavy_blur_recovers_center_and_width():
    """Deblocking smear: s~3.5px. The center must stay unbiased and the fitted width must
    report the blur (it is the per-frame quality signal)."""
    fit = fit_fill_boundary(_profile(s=3.5, noise=3.0))
    assert fit is not None
    assert abs(fit.y0 - 70.3) < 0.8
    assert 2.0 <= fit.s <= 6.0


def test_ringing_overshoot_is_absorbed_by_huber():
    p = _profile(noise=2.0)
    p[66] -= 8.0   # dark-side undershoot ring
    p[74] += 8.0   # fill-side overshoot ring
    fit = fit_fill_boundary(p)
    assert fit is not None
    assert abs(fit.y0 - 70.3) < 1.0


def test_flat_profile_rejected():
    assert fit_fill_boundary(np.full(120, 40.0) + RNG.normal(0, 2, 120)) is None


def test_low_contrast_step_rejected():
    """A <15-grey-level step is not a credible fill boundary."""
    assert fit_fill_boundary(_profile(l_dark=40, l_fill=50, noise=2.0)) is None


def test_degenerate_near_full_localizes_or_rejects():
    """Near-full: the dark plateau is almost gone. The fit must either localize the boundary
    accurately (frozen to the EMA or not) or reject -- it must NEVER fabricate a mid-profile
    boundary from a plateau-less window."""
    p = _profile(y0=3.0, s=1.5)
    ema = PlateauEMA()
    ema.update(30.0, 90.0)
    for plateaus in (None, ema):
        fit = fit_fill_boundary(p, plateaus=plateaus)
        if fit is not None:
            assert abs(fit.y0 - 3.0) < 2.0, (plateaus, fit)
    assert fit_fill_boundary(p, plateaus=ema) is not None   # with an EMA it must not give up


def test_warm_start_tracks_moving_boundary():
    prev = None
    errs = []
    for true_y0 in np.linspace(90, 40, 12):     # rising fill = boundary moving UP
        p = _profile(y0=true_y0, s=2.0, noise=3.0)
        fit = fit_fill_boundary(p, prev_y0=prev)
        assert fit is not None
        errs.append(abs(fit.y0 - true_y0))
        prev = fit.y0
    assert float(np.median(errs)) < 0.6


def test_quality_signal_orders_sharp_above_blurred():
    q_sharp = fit_fill_boundary(_profile(s=1.0, noise=2.0)).q
    q_blur = fit_fill_boundary(_profile(s=4.5, noise=5.0)).q
    assert q_sharp > q_blur


# --------------------------------------------------------------------------- #
#  tip sliver
# --------------------------------------------------------------------------- #
def _sliver_profile(n=140, rows=(5, 10), tip=160.0, fill=90.0, dark=27.0):
    p = np.full(n, dark)
    p[rows[0]:rows[1]] = tip
    p[n // 2:] = fill
    return p


def test_tip_sliver_found_near_top():
    ema = PlateauEMA()
    ema.update(27.0, 90.0)
    idx = find_tip_sliver(_sliver_profile(), ema)
    assert idx.size >= 4
    assert idx[0] == 5 and idx[-1] == 9


def test_tip_sliver_cold_start_uses_default_levels():
    idx = find_tip_sliver(_sliver_profile(), PlateauEMA())
    assert idx.size >= 4


def test_bright_region_far_from_top_is_not_the_sliver():
    p = np.full(140, 27.0)
    p[100:110] = 160.0          # bright, but nowhere near the track top
    ema = PlateauEMA()
    ema.update(27.0, 90.0)
    assert find_tip_sliver(p, ema).size == 0


def test_capped_meter_full_bright_interior_keeps_top_run_only():
    """When the fill turns green (cap) the whole interior is bright: the finder must return
    only the top-contiguous run, not the entire profile (the cap is an EVENT, not an anchor)."""
    p = np.full(140, 150.0)
    p[:3] = 27.0
    ema = PlateauEMA()
    ema.update(27.0, 90.0)
    idx = find_tip_sliver(p, ema)
    assert idx.size == 0 or (idx[0] >= 3 and idx.size < 140)


# --------------------------------------------------------------------------- #
#  ROI SAD (stale detector)
# --------------------------------------------------------------------------- #
def test_roi_sad_identical_is_zero():
    a = RNG.integers(0, 255, (64, 32)).astype(np.uint8)
    assert roi_sad(a, a.copy()) == 0.0


def test_roi_sad_motion_is_positive():
    a = RNG.integers(0, 255, (64, 32)).astype(np.uint8)
    b = np.roll(a, 3, axis=0)
    assert roi_sad(a, b) > 1.0


def test_roi_sad_shape_mismatch_sentinel():
    a = np.zeros((64, 32), np.uint8)
    assert roi_sad(a, np.zeros((60, 32), np.uint8)) == -1.0
    assert roi_sad(a, None) == -1.0
