"""Strict partial-occlusion recovery for detector-authoritative white-meter fill.

The recovery is intentionally post-acquisition: it can bridge a short in-shot limb
crossing after direct rising evidence, but cannot acquire from partial pixels, carry
evidence across a shot/lock identity, or recursively extend itself.
"""
from __future__ import annotations

import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader


W, H = 1280, 720
BOX = (600, 300, 24, 107)


class _Cfg:
    meter_style = "Arrow2"
    meter_color = "White"
    confidence_threshold = 0.32


def _frame(fill_pct: float, visible_cols=None):
    """Render a detector-box-aligned white ribbon with optional side occlusion."""
    frame = np.zeros((H, W, 3), np.uint8)
    x, y, w, h = BOX
    top = y + int(round((h - 1) * (1.0 - fill_pct / 100.0)))
    if visible_cols is None:
        visible_cols = range(3, w - 3)
    for c in visible_cols:
        frame[top:y + h, x + int(c)] = 255
    return frame


def _top_censored_frame(actual_fill: float, visible_fill: float):
    """Render an ambiguous top loss with no directly visible masking object."""
    frame = _frame(actual_fill)
    x, y, w, h = BOX
    actual_top = y + int(round((h - 1) * (1.0 - actual_fill / 100.0)))
    visible_top = y + int(round((h - 1) * (1.0 - visible_fill / 100.0)))
    frame[actual_top:visible_top, x:x + w] = 0
    return frame


def _top_censored_evidence_frame(actual_fill: float, visible_fill: float):
    """Render a top loss with a visible coloured foreground strip.

    The strip is the independent masking evidence a plateau/fall does not have.
    Its saturation keeps it outside the white-fill mask while preserving a broad
    appearance edge in the would-be hidden rows.
    """
    frame = _frame(actual_fill)
    x, y, w, h = BOX
    actual_top = y + int(round((h - 1) * (1.0 - actual_fill / 100.0)))
    visible_top = y + int(round((h - 1) * (1.0 - visible_fill / 100.0)))
    frame[actual_top:visible_top, x:x + w] = (32, 96, 180)
    return frame


def _reader(monkeypatch):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    monkeypatch.setenv("ORION_METER_RUNG_FILL", "0")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL", "0")
    monkeypatch.setenv("ORION_METER_DETECTOR_TRACK", "0")
    monkeypatch.setenv("ORION_METER_PARTIAL_OCCLUSION", "1")
    reader = SimpleMeterReader(W, H, cfg=_Cfg(),
                               require_gameplay_eligibility=False)
    reader.set_shot_state(True, 1.0, True)
    reader.notify_physical_shot_start(7)
    reader._det_state = "locked"
    reader._start_det_lock_generation()
    reader._det_occ_locator_source_positive = True
    # These unit tests seed direct reads without going through detect(); mark
    # the already-consumed arm edge so detect() does not correctly clear them.
    reader._prev_hw_armed = True
    reader._hw_arm_ts = 9.0
    return reader


def _seed_direct_rise(reader, t0=10.0):
    reader._det_occ_locator_source_positive = True
    direct = ((t0, 10.0), (t0 + 0.02, 16.0), (t0 + 0.04, 22.0))
    for ts, fill in direct:
        reader._det_last_found_ts = ts
        measured, _, top = reader._measure_fill_in_box(_frame(fill), BOX, ts=ts)
        assert top >= 0 and measured == pytest.approx(fill, abs=2.0)
        assert not reader._det_occ_recovered
    reader._latch_gameplay_structure_proof(7)
    return direct[-1][0]


def test_two_surviving_columns_bridge_short_occlusion(monkeypatch):
    """A side-on occluder may hide 16/18 ribbon columns; two adjacent columns
    still carry a bottom-connected edge and can bridge one short timing frame."""
    reader = _reader(monkeypatch)
    last = _seed_direct_rise(reader)
    reader._det_last_found_ts = last

    measured, _, top = reader._measure_fill_in_box(
        _frame(28.0, visible_cols=(10, 11)), BOX, ts=last + 0.02)

    assert top >= 0
    assert reader._det_occ_recovered
    assert reader._det_occ_kind == "partial_columns"
    assert reader._det_occ_support > 0.0
    assert measured == pytest.approx(28.0, abs=2.0)
    assert reader._det_diag["occlusion_fill"] == 1


def test_floating_lower_fragment_is_not_base_connected(monkeypatch):
    """Entering the lower fifth is not enough: a bright player fragment may
    float well above the meter floor and must remain a miss."""
    reader = _reader(monkeypatch)
    last = _seed_direct_rise(reader)
    reader._det_last_found_ts = last
    frame = np.zeros((H, W, 3), np.uint8)
    x, y, w, h = BOX
    predicted_top = y + int(round((h - 1) * (1.0 - 28.0 / 100.0)))
    # Ends 16 rows above the box floor: inside the old lower-20% allowance,
    # but not physically connected to the meter base.
    frame[predicted_top:y + h - 16, x + 10:x + 12] = 255

    measured, _, top = reader._measure_fill_in_box(
        frame, BOX, ts=last + 0.02)

    assert (measured, top) == (0.0, -1)
    assert not reader._det_occ_recovered


def test_detect_publishes_recovered_fill_with_reduced_confidence(monkeypatch):
    """The engine-facing result remains usable for timing, while its confidence
    honestly records that only partial image support survived."""
    reader = _reader(monkeypatch)
    last = _seed_direct_rise(reader)
    reader._det_last_found_ts = last
    reader._det_active_box = BOX
    reader._det_last_box = BOX
    reader._det_fill_hist.extend(((last - 0.04, 10.0),
                                  (last - 0.02, 16.0),
                                  (last, 22.0)))
    reader._det_coarse_fill_hist.extend(reader._det_fill_hist)

    result = reader.detect(
        _frame(28.0, visible_cols=(10, 11)), ts=last + 0.02)

    assert result.detected
    assert result.fill_pct == pytest.approx(28.0, abs=2.0)
    assert result.fill_velocity_pct_s > 20.0
    assert 0.65 <= result.confidence < 1.0
    assert result.rejection_reason == ""


def test_partial_pixels_cannot_cold_acquire_or_stamp(monkeypatch):
    reader = _reader(monkeypatch)
    reader._det_last_found_ts = 10.0

    measured, _, top = reader._measure_fill_in_box(
        _frame(28.0, visible_cols=(10, 11)), BOX, ts=10.0)

    assert (measured, top) == (0.0, -1)
    assert not reader._det_occ_recovered
    assert not reader.gameplay_structure_verified


def test_partial_recovery_requires_current_epoch_structure(monkeypatch):
    reader = _reader(monkeypatch)
    last = _seed_direct_rise(reader)
    reader._clear_gameplay_structure_proof()
    reader._det_last_found_ts = last

    measured, _, top = reader._measure_fill_in_box(
        _frame(28.0, visible_cols=(10, 11)), BOX, ts=last + 0.02)

    assert (measured, top) == (0.0, -1)
    assert not reader._det_occ_recovered


def test_top_strip_censor_recovers_proven_rising_edge(monkeypatch):
    """A broad top mask must not turn a real 28% frame into a stale 22%
    measurement; the bounded direct-rise model restores the censored edge."""
    reader = _reader(monkeypatch)
    last = _seed_direct_rise(reader)
    reader._det_last_found_ts = last

    measured, _, top = reader._measure_fill_in_box(
        _top_censored_evidence_frame(28.0, 22.0), BOX, ts=last + 0.02)

    assert top >= 0
    assert measured == pytest.approx(28.0, abs=1.25)
    assert reader._det_occ_recovered
    assert reader._det_occ_kind == "top_censored"
    assert reader._det_diag["occlusion_top"] == 1
    # The invisible/predicted row is never promoted into direct evidence.
    assert len(reader._det_occ_direct) == 3


@pytest.mark.parametrize("visible_fill", [22.0, 20.0])
def test_fully_visible_plateau_or_fall_is_never_extrapolated(
        monkeypatch, visible_fill):
    """Trajectory lag alone is not masking evidence.

    The old top-censor path turned the first plateau/fall after 10->16->22 into
    a synthetic ~28% rise.  The fully visible edge must remain a direct sample.
    """
    reader = _reader(monkeypatch)
    last = _seed_direct_rise(reader)
    reader._det_last_found_ts = last

    measured, _, top = reader._measure_fill_in_box(
        _frame(visible_fill), BOX, ts=last + 0.02)

    assert top >= 0
    assert measured == pytest.approx(visible_fill, abs=1.25)
    assert not reader._det_occ_recovered
    assert reader._det_occ_kind == ""


def test_ambiguous_black_top_loss_is_not_inferred(monkeypatch):
    """If the alleged mask is indistinguishable from the empty track, the
    pixels are observationally identical to a real 22% plateau and stay direct."""
    reader = _reader(monkeypatch)
    last = _seed_direct_rise(reader)
    reader._det_last_found_ts = last

    measured, _, top = reader._measure_fill_in_box(
        _top_censored_frame(28.0, 22.0), BOX, ts=last + 0.02)

    assert top >= 0
    assert measured == pytest.approx(22.0, abs=1.25)
    assert not reader._det_occ_recovered


def test_top_strip_censor_detect_result_stays_timing_usable(monkeypatch):
    reader = _reader(monkeypatch)
    last = _seed_direct_rise(reader)
    reader._det_last_found_ts = last
    reader._det_active_box = BOX
    reader._det_last_box = BOX
    reader._det_fill_hist.extend(((last - 0.04, 10.0),
                                  (last - 0.02, 16.0),
                                  (last, 22.0)))
    reader._det_coarse_fill_hist.extend(reader._det_fill_hist)

    result = reader.detect(
        _top_censored_evidence_frame(28.0, 22.0), ts=last + 0.02)

    assert result.detected
    assert result.fill_pct == pytest.approx(28.0, abs=1.25)
    assert result.fill_velocity_pct_s > 20.0
    assert 0.65 <= result.confidence < 1.0
    assert reader._det_occ_kind == "top_censored"


def test_unique_negative_locator_result_revokes_recovery(monkeypatch):
    """A full-frame no-find is authoritative even while the lifecycle still
    holds the old box for ordinary tracking hysteresis."""
    reader = _reader(monkeypatch)
    last = _seed_direct_rise(reader)
    reader._det_last_found_ts = last
    reader._det_active_box = BOX
    reader._det_last_box = BOX

    class _NegativeLocator:
        provider = "fixture"
        infer_ms = 0.0

        @staticmethod
        def submit(frame, ts):
            return None

        @staticmethod
        def latest():
            return False, None, 0.0, last + 0.01

    reader._meter_detector = _NegativeLocator()

    result = reader.detect(
        _frame(28.0, visible_cols=(10, 11)), ts=last + 0.02)

    # Lifecycle hysteresis may still report the held box itself, but it must not
    # convert the partial pixels into a positive fill/timing sample.
    assert result.fill_pct == 0.0
    assert not reader._det_occ_locator_source_positive
    assert not reader._det_occ_recovered


def test_one_negative_top_censor_bridges_then_rejects_and_drops(monkeypatch):
    """One <=45 ms full-negative frame may use a broad surviving meter body;
    later negatives cannot self-extend it and retire the held lock normally."""
    reader = _reader(monkeypatch)
    last = _seed_direct_rise(reader)
    reader._det_last_found_ts = last
    reader._det_active_box = BOX
    reader._det_last_box = BOX

    class _NegativeSequence:
        provider = "fixture"
        infer_ms = 0.0

        def __init__(self):
            self.ts = last

        def submit(self, frame, ts):
            self.ts = float(ts)

        def latest(self):
            return False, None, 0.0, self.ts - 0.005

    reader._meter_detector = _NegativeSequence()
    results = [reader.detect(_top_censored_evidence_frame(
                                 28.0 + 6.0 * i, 22.0),
                             ts=last + 0.02 * (i + 1))
               for i in range(3)]

    assert results[0].detected
    assert results[0].fill_pct == pytest.approx(28.0, abs=1.25)
    assert results[0].rejection_reason == ""
    assert 0.65 <= results[0].confidence <= 0.75
    assert results[1].fill_pct == 0.0
    assert results[1].rejection_reason == "detector_fill_top_occluded"
    assert results[2].fill_pct == 0.0
    assert not reader._det_occ_recovered
    assert not reader._det_occ_locator_source_positive
    assert reader._det_state == "idle"
    assert reader._det_active_box is None


def test_negative_bridge_requires_half_width_and_45ms(monkeypatch):
    def _run(frame, dt):
        reader = _reader(monkeypatch)
        last = _seed_direct_rise(reader)
        reader._det_last_found_ts = last
        reader._det_active_box = BOX
        reader._det_last_box = BOX

        class _Negative:
            provider = "fixture"
            infer_ms = 0.0

            @staticmethod
            def submit(source, ts):
                return None

            @staticmethod
            def latest():
                return False, None, 0.0, last + dt - 0.005

        reader._meter_detector = _Negative()
        return reader, reader.detect(frame, ts=last + dt)

    # Eight of twenty inner columns still pass the ordinary 28%-of-box row
    # walk, but are below the stricter 50% negative-bridge evidence gate.
    narrow = _top_censored_evidence_frame(28.0, 22.0)
    x, y, w, h = BOX
    keep = set(range(8, 16))
    visible_top = y + int(round((h - 1) * (1.0 - 22.0 / 100.0)))
    for c in range(w):
        if c not in keep:
            # Retain the broad coloured occluder evidence; narrow only the
            # surviving lower white body used by the half-width gate.
            narrow[visible_top:y + h, x + c] = 0
    narrow_reader, narrow_result = _run(narrow, 0.02)
    assert narrow_result.fill_pct == 0.0
    assert narrow_result.rejection_reason == "detector_fill_top_occluded"
    assert not narrow_reader._det_occ_recovered

    late_reader, late_result = _run(
        _top_censored_evidence_frame(40.0, 22.0), 0.046)
    assert late_result.fill_pct == 0.0
    assert late_result.rejection_reason == "detector_fill_top_occluded"
    assert not late_reader._det_occ_recovered


def test_fully_hidden_negative_consumes_one_frame_budget(monkeypatch):
    reader = _reader(monkeypatch)
    last = _seed_direct_rise(reader)
    reader._det_last_found_ts = last
    reader._det_active_box = BOX
    reader._det_last_box = BOX

    class _NegativeSequence:
        provider = "fixture"
        infer_ms = 0.0

        def __init__(self):
            self.ts = last

        def submit(self, frame, ts):
            self.ts = float(ts)

        def latest(self):
            return False, None, 0.0, self.ts - 0.005

    reader._meter_detector = _NegativeSequence()
    hidden = np.zeros((H, W, 3), np.uint8)
    first = reader.detect(hidden, ts=last + 0.01)
    second = reader.detect(
        _top_censored_evidence_frame(31.0, 22.0), ts=last + 0.03)

    assert first.fill_pct == 0.0
    assert reader._det_occ_negative_bridge_used
    assert second.fill_pct == 0.0
    assert second.rejection_reason == "detector_fill_top_occluded"
    assert reader._det_diag["occlusion_negative"] == 0
    assert not reader._det_occ_recovered


def test_aged_positive_slot_cannot_authorize_top_prediction(monkeypatch):
    """A once-positive async slot loses prediction authority at its source TTL;
    the held box may coast, but its truncated lower edge is rejected."""
    reader = _reader(monkeypatch)
    last = _seed_direct_rise(reader)
    reader._det_last_found_ts = last
    reader._det_active_box = BOX
    reader._det_last_box = BOX

    class _StalePositive:
        provider = "fixture"
        infer_ms = 0.0

        @staticmethod
        def submit(frame, ts):
            return None

        @staticmethod
        def latest():
            return True, BOX, 0.95, last - reader._det_ttl_s - 0.01

    reader._meter_detector = _StalePositive()
    result = reader.detect(
        _top_censored_evidence_frame(28.0, 22.0), ts=last + 0.02)

    assert result.fill_pct == 0.0
    assert result.rejection_reason == "detector_fill_top_occluded"
    assert not reader._det_occ_locator_source_positive
    assert not reader._det_occ_recovered


def test_repeated_positive_slot_does_not_renew_hold_authority(monkeypatch):
    """Serving a repeated async positive is allowed only within the original
    source frame's hold window; callback consumption cannot refresh its clock."""
    reader = _reader(monkeypatch)
    reader._det_hold_s = 0.05
    reader._det_ttl_s = 0.20
    reader._det_last_box = BOX
    reader._det_active_box = BOX
    reader._det_last_found_ts = 19.9

    class _RepeatedPositive:
        provider = "fixture"
        infer_ms = 0.0

        @staticmethod
        def submit(frame, ts):
            return None

        @staticmethod
        def latest():
            return True, BOX, 0.95, 20.0

    reader._meter_detector = _RepeatedPositive()
    blank = np.zeros((H, W, 3), np.uint8)
    reader.detect(blank, ts=20.01)  # unique accepted source
    assert reader._det_last_found_ts == pytest.approx(20.0)
    reader.detect(blank, ts=20.04)  # same slot, still within hold
    assert reader._det_last_found_ts == pytest.approx(20.0)
    assert reader._det_active_box is not None
    reader.detect(blank, ts=20.06)  # same slot cannot renew; hold expires
    assert reader._det_state == "idle"
    assert reader._det_active_box is None


def test_future_result_stamp_is_rejected_without_poisoning_recovery(monkeypatch):
    """A producer clock ahead of the callback is neither fresh nor unique.
    The next correctly ordered result must still be able to acquire immediately."""
    reader = _reader(monkeypatch)
    reader._det_reset_lock_state()
    reader._det_active_box = None
    seen_before = reader._det_seen_dts

    class _MutableLocator:
        provider = "fixture"
        infer_ms = 0.0

        def __init__(self):
            self.result = (True, BOX, 0.95, 1000.0)

        @staticmethod
        def submit(frame, ts):
            return None

        def latest(self):
            return self.result

    locator = _MutableLocator()
    reader._meter_detector = locator

    rejected = reader.detect(_frame(10.0), ts=20.0)
    assert not rejected.detected
    assert reader._det_seen_dts == seen_before
    assert reader._det_last_found_ts < -1.0e8
    assert reader._det_state == "idle"

    locator.result = (True, BOX, 0.95, 20.01)
    recovered = reader.detect(_frame(10.0), ts=20.02)
    assert recovered.detected
    assert reader._det_seen_dts == pytest.approx(20.01)
    assert reader._det_last_found_ts == pytest.approx(20.01)
    assert reader._det_state == "locked"
    assert reader._det_active_box is not None


def test_recovered_frames_do_not_extend_their_own_deadline(monkeypatch):
    reader = _reader(monkeypatch)
    last = _seed_direct_rise(reader)
    reader._det_last_found_ts = last

    first, _, top1 = reader._measure_fill_in_box(
        _frame(40.0, visible_cols=(10, 11)), BOX, ts=last + 0.06)
    second, _, top2 = reader._measure_fill_in_box(
        _frame(58.0, visible_cols=(10, 11)), BOX,
        ts=last + reader._det_occ_max_gap_s + 0.01)

    assert top1 >= 0 and first > 0.0
    assert (second, top2) == (0.0, -1)
    assert not reader._det_occ_recovered
    assert len(reader._det_occ_direct) == 3


def test_full_occlusion_and_implausible_edge_remain_misses(monkeypatch):
    reader = _reader(monkeypatch)
    last = _seed_direct_rise(reader)
    reader._det_last_found_ts = last

    hidden = np.zeros((H, W, 3), np.uint8)
    assert reader._measure_fill_in_box(hidden, BOX, ts=last + 0.02)[2] == -1

    # A bottom-connected white fragment whose edge jumps far ahead of the
    # established physical trajectory is an occluder/object switch, not fill.
    measured, _, top = reader._measure_fill_in_box(
        _frame(85.0, visible_cols=(10, 11)), BOX, ts=last + 0.02)
    assert (measured, top) == (0.0, -1)
    assert not reader._det_occ_recovered


def test_lock_and_shot_boundaries_clear_recovery_evidence(monkeypatch):
    reader = _reader(monkeypatch)
    _seed_direct_rise(reader)
    assert len(reader._det_occ_direct) == 3

    reader._det_reset_lock_state()
    assert list(reader._det_occ_direct) == []
    assert reader._det_occ_key is None

    reader._det_state = "locked"
    reader._start_det_lock_generation()
    _seed_direct_rise(reader, t0=20.0)
    reader.notify_physical_shot_start(8)
    assert list(reader._det_occ_direct) == []
    assert reader._det_occ_key is None


def test_flag_off_preserves_honest_miss(monkeypatch):
    reader = _reader(monkeypatch)
    last = _seed_direct_rise(reader)
    reader._det_occ_fill = False
    reader._det_last_found_ts = last

    measured, _, top = reader._measure_fill_in_box(
        _frame(28.0, visible_cols=(10, 11)), BOX, ts=last + 0.02)
    assert (measured, top) == (0.0, -1)
