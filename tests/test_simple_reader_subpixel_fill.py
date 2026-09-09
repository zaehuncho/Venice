"""Sub-pixel fill edge (_subpixel_fill_edge / _measure_fill_in_box integration).

The coarse walk quantizes the fill edge to whole rows (~0.93% of scale on the ~107px
2K27 meter); the sub-pixel path measures the 50% luma crossing between the two
plateau levels on the bar-core column profile, anchored to the meter's OWN base edge
so box-position jitter cancels. These tests render the measured 2K27 white-meter
structure (fill core V~253/S~3, empty track V~140/S~200, base shelf V~170, green cap)
with a supersampled sub-row edge and lock in the properties the timing stack needs:
sub-row resolution, scale parity with the coarse walk, green-cap immunity, fail-open
quality gates, and box-jitter cancellation via the base anchor.
"""
import numpy as np
import pytest

import cv2

from simple_meter_reader import SimpleMeterReader

BH, BW = 107, 24
SS = 8          # vertical supersample for sub-pixel edge placement
BASE_ROW = 100.0   # meter base (top of the rounded-base falloff), rows from box top


def _render_box(fill_edge_row, base_row=BASE_ROW, green=True, contrast=1.0,
                blur=0.65, noise=0.0, seed=0):
    """A BGR white-meter patch with the fill edge at sub-pixel `fill_edge_row`."""
    rng = np.random.default_rng(seed)
    hr = np.zeros((BH * SS, BW, 3), np.float32)
    # column archetypes (measured: only ~half the box columns are the bar core)
    fill_c = np.zeros((BW, 3), np.float32)
    empty_c = np.zeros((BW, 3), np.float32)
    fill_c[:] = (250, 252, 253)      # white core: V~253, S~3
    empty_c[:] = (30, 70, 140)       # empty track: V=140, S~200 (saturated backing)
    for c in list(range(0, 4)) + list(range(BW - 4, BW)):
        fill_c[c] = (60, 60, 65)     # outline/shadow columns never pass the white mask
        empty_c[c] = (40, 40, 45)
    e = int(round(fill_edge_row * SS))
    b = int(round(base_row * SS))
    hr[:e] = empty_c[None, :]
    hr[e:b] = fill_c[None, :]
    # base furniture: 3-row shelf (V~170) then court
    sh = min(BH * SS, b + 3 * SS)
    hr[b:sh] = (168, 170, 171)
    hr[sh:] = (100, 105, 110)
    if green:
        hr[2 * SS:4 * SS, 4:BW - 4] = (60, 200, 60)   # make-window cap near the top
    lr = hr.reshape(BH, SS, BW, 3).mean(axis=1)
    lr = cv2.GaussianBlur(lr, (0, 0), sigmaX=0.4, sigmaY=blur)
    if contrast != 1.0:
        lr = 140.0 + (lr - 140.0) * contrast
    if noise:
        lr = lr + rng.normal(0.0, noise, lr.shape).astype(np.float32)
    return np.clip(lr, 0, 255).astype(np.uint8)


def _frame_with_box(patch, x=600, y=300, pad=40):
    H, W = patch.shape[0] + 2 * pad, patch.shape[1] + 2 * pad
    f = np.full((H + y, W + x, 3), 35, np.uint8)
    f[y:y + patch.shape[0], x:x + patch.shape[1]] = patch
    return f, (x, y, patch.shape[1], patch.shape[0])


def _reader():
    r = SimpleMeterReader(1280, 720)
    return r


def _measure(r, edge_row, box_dy=0, ts=None, **kw):
    patch = _render_box(edge_row, **kw)
    frame, box = _frame_with_box(patch)
    box = (box[0], box[1] + box_dy, box[2], box[3] - box_dy)
    return r._measure_fill_in_box(frame, box, ts=ts)


def test_subpixel_resolves_subrow_motion():
    """A sub-row edge sweep must produce strictly decreasing fill with more unique
    values than the row-quantized coarse walk."""
    r = _reader()
    fills, coarse = [], []
    for k in range(12):
        fill, _, _ = _measure(r, 60.0 + 0.25 * k, noise=1.5, seed=k)
        assert r._dbg_subpx and r._dbg_subpx["ok"] == 1, r._dbg_subpx
        fills.append(fill)
        coarse.append(r._last_fill_coarse)
    assert len(set(fills)) > len(set(coarse))
    diffs = np.diff(fills)
    assert np.all(diffs < 0.05), fills          # edge moves DOWN -> fill falls
    assert np.all(np.abs(diffs + 0.25 / (BH - 1) * 100.0) < 0.15), diffs


def test_scale_parity_with_coarse():
    """The sub-pixel value must live on the SAME scale as the coarse walk: the engine's
    tuned constants (rate 0.196 %/ms, green tip ~96%) must not shift."""
    r = _reader()
    deltas = []
    for k in range(20):
        fill, _, _ = _measure(r, 55.0 + 0.05 * k * 20, noise=1.5, seed=100 + k)
        deltas.append(fill - r._last_fill_coarse)
    med = float(np.median(deltas))
    assert abs(med) <= 0.5, (med, deltas)       # < ~0.5pp median shift


def test_subpixel_beats_coarse_rms():
    """Ground-truth sweep: sub-pixel RMS about the true edge must be well under the
    coarse quantization RMS (synthetic bench measured ~0.05px vs 0.29px)."""
    r = _reader()
    sub_err, coarse_err = [], []
    for k in range(30):
        true_edge = 58.0 + k / 30.0 * 3.0
        fill, _, _ = _measure(r, true_edge, noise=1.5, seed=200 + k)
        true_fill_rows = (BH - 1.0) - true_edge     # up to a constant offset
        sub_err.append(fill / 100.0 * (BH - 1) - true_fill_rows)
        coarse_err.append(r._last_fill_coarse / 100.0 * (BH - 1) - true_fill_rows)
    sub_err = np.array(sub_err) - np.median(sub_err)       # constant offset is parity's
    coarse_err = np.array(coarse_err) - np.median(coarse_err)   # job, not precision's
    rms_s = float(np.sqrt(np.mean(sub_err ** 2)))
    rms_c = float(np.sqrt(np.mean(coarse_err ** 2)))
    assert rms_s < 0.5 * rms_c, (rms_s, rms_c)
    assert rms_s < 0.15, rms_s                   # sub-pixel in absolute terms


def test_base_anchor_cancels_box_jitter():
    """With the per-shot anchor latched, a +/-1px box-position jitter (the measured
    dominant sigma_y term) must NOT move the sub-pixel fill, while it moves the
    box-relative coarse fill by ~1 row."""
    r = _reader()
    for k in range(3):                            # seed the latch on clean frames
        _measure(r, 62.0, noise=0.0, seed=k)
    assert r._subpx_D is not None and r._subpx_off is not None
    subs, coars = [], []
    for dy in (0, 1, -1, 1, 0, -1):
        fill, _, _ = _measure(r, 62.0, box_dy=dy, noise=0.0)
        assert r._dbg_subpx["anchor"] == "base"
        subs.append(fill)
        coars.append(r._last_fill_coarse)
    assert (max(subs) - min(subs)) < 0.25, subs           # anchored: immune
    assert (max(coars) - min(coars)) > 0.6, coars         # coarse: most of a row of error


def test_green_cap_near_edge_fails_open_or_measures():
    """With the make-window cap directly above the edge the empty-plateau window is
    green-contaminated: the estimator must fail open (coarse) or stay within a row."""
    r = _reader()
    patch = _render_box(8.0)          # fill almost to the cap (rows 2-4 are green)
    frame, box = _frame_with_box(patch)
    fill, _, _ = r._measure_fill_in_box(frame, box)
    assert abs(fill - r._last_fill_coarse) <= 1.2, (fill, r._last_fill_coarse)


def test_low_contrast_fails_open_to_coarse():
    r = _reader()
    fill, _, _ = _measure(r, 62.0, contrast=0.22, noise=1.0)   # washed-out capture
    q = r._dbg_subpx
    assert q is None or q["ok"] == 0
    assert fill == pytest.approx(r._last_fill_coarse, abs=1e-6)


def test_emission_flag_off_keeps_coarse(monkeypatch):
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL", "0")
    r = _reader()
    fill, _, _ = _measure(r, 61.6, noise=0.0)
    # computed (stash populated) but NOT emitted
    assert r._dbg_subpx and r._dbg_subpx["ok"] == 1
    assert fill == pytest.approx(r._last_fill_coarse, abs=1e-6)


def test_validated_latch_carries_across_same_scale_new_shot():
    r = _reader()
    for k in range(3):
        _measure(r, 62.0, seed=k, ts=1.0 + k / 60.0)
    D, off = r._subpx_D, r._subpx_off
    generation = r._last_fill_estimator_generation
    assert D is not None and off is not None
    r._clear_per_shot_rise_evidence()             # the new-press reset
    assert r._subpx_D == D and r._subpx_off == off
    assert r._subpx_base_hist == [] and r._subpx_carry_pending

    _measure(r, 61.0, ts=2.0)
    assert r._subpx_D == D and r._subpx_off == off
    assert not r._subpx_carry_pending
    assert r._last_fill_estimator_mode == "subpixel"
    assert r._last_fill_estimator_generation == generation


def test_carried_latch_is_dropped_before_changed_scale_emits():
    r = _reader()
    for k in range(3):
        _measure(r, 62.0, seed=k, ts=1.0 + k / 60.0)
    old_D = r._subpx_D
    old_generation = r._last_fill_estimator_generation
    assert old_D is not None

    r._clear_per_shot_rise_evidence()
    _measure(r, 62.0, box_dy=3, ts=2.0)

    assert not r._subpx_carry_pending
    assert r._subpx_D is None
    assert r._subpx_off is None
    assert r._last_fill_estimator_generation > old_generation


def test_fill_estimator_generation_bridges_transient_quality_failure():
    """A native anchor may join only samples emitted on one numerical ruler.

    The wire generation stays stable during a consistent sub-pixel run, but
    changes when the per-shot latch engages. Once that base-anchored ruler exists,
    a short quality failure is bridged on the same ruler rather than toggling coarse.
    """
    r = _reader()

    # The first two clean frames use the sub-pixel seed/box ruler.
    _measure(r, 64.0, noise=0.0, ts=1.000)
    seed_generation = r._last_fill_estimator_generation
    assert r._last_fill_estimator_mode == "subpixel"
    _measure(r, 63.0, noise=0.0, ts=1.017)
    assert r._last_fill_estimator_generation == seed_generation

    # Frame three commits D/off and therefore starts a new ruler generation.
    _measure(r, 62.0, noise=0.0, ts=1.034)
    latched_generation = r._last_fill_estimator_generation
    assert r._subpx_D is not None
    assert latched_generation > seed_generation
    _measure(r, 61.0, noise=0.0, ts=1.051)
    assert r._last_fill_estimator_mode == "subpixel"
    assert r._last_fill_estimator_generation == latched_generation

    # A broad edge keeps the coarse row walk but fails sub-pixel width quality.
    _measure(r, 60.0, blur=4.0, noise=0.0, ts=1.068)
    assert r._dbg_subpx["bridge"] == 1
    assert r._dbg_subpx["anchor"] == "base_bridge"
    assert r._last_fill_estimator_mode == "subpixel"
    assert r._last_fill_estimator_generation == latched_generation

    _measure(r, 59.0, noise=0.0, ts=1.085)
    assert r._last_fill_estimator_mode == "subpixel"
    assert r._last_fill_estimator_generation == latched_generation


def test_expired_subpixel_gap_falls_back_and_fences_generation():
    r = _reader()
    for k, edge in enumerate((64.0, 63.0, 62.0, 61.0)):
        _measure(r, edge, noise=0.0, ts=1.0 + k * 0.017)
    latched_generation = r._last_fill_estimator_generation

    _measure(r, 60.0, blur=4.0, noise=0.0, ts=1.5)
    assert r._last_fill_estimator_mode == "coarse"
    assert r._last_fill_estimator_generation > latched_generation


def test_coarse_generation_ignores_measured_box_height_quantization(monkeypatch):
    """The latest live batch's adjacent detector heights move by at most two pixels.

    Those +/-1/2 px box quantization steps must not churn the provenance generation,
    otherwise a valid 60 Hz anchor straddle is routinely split even though the meter
    has not physically rescaled.
    """
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL", "0")
    r = _reader()
    _measure(r, 62.0, box_dy=0, noise=0.0)
    generation = r._last_fill_estimator_generation
    assert r._last_fill_estimator_mode == "coarse"
    assert generation > 0

    for box_dy in (1, -1, 2, -2):
        _measure(r, 62.0, box_dy=box_dy, noise=0.0)
        assert r._last_fill_estimator_mode == "coarse"
        assert r._last_fill_estimator_generation == generation


def test_coarse_generation_fences_a_real_box_height_rescale(monkeypatch):
    """A >2 px denominator change is outside measured tracking jitter and must
    invalidate the native straddling pair before this changed-scale frame ships."""
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL", "0")
    r = _reader()
    _measure(r, 62.0, box_dy=0, noise=0.0)
    initial = r._last_fill_estimator_generation

    _measure(r, 62.0, box_dy=3, noise=0.0)  # denominator 106 -> 103
    rescaled = r._last_fill_estimator_generation
    assert r._last_fill_estimator_mode == "coarse"
    assert rescaled > initial

    # Jitter around the new latched scale stays in its new generation.
    _measure(r, 62.0, box_dy=2, noise=0.0)
    assert r._last_fill_estimator_generation == rescaled


def test_coarse_generation_changes_after_shot_scope_reset(monkeypatch):
    """Numerically identical box heights on two shot identities are still distinct
    local rulers; a bridged lock cannot carry provenance across the press boundary."""
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL", "0")
    r = _reader()
    _measure(r, 62.0, noise=0.0)
    first = r._last_fill_estimator_generation

    r._clear_per_shot_rise_evidence()
    _measure(r, 62.0, noise=0.0)
    assert r._last_fill_estimator_generation > first
