"""Detector-stage fault injection at the production reader boundary.

The async locator owns presence and the box measurement owns fill. A failure in
either component must not silently publish the colour-reader fallback or reuse
the previous frame's detector authority as a new observation.
"""

import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader


BOX = (600, 300, 24, 107)


class _Cfg:
    meter_style = "Arrow2"
    meter_color = "White"
    confidence_threshold = 0.32


class _Locator:
    provider = "fault-injection"
    infer_ms = 0.0

    def __init__(self):
        self.result = (True, BOX, 0.9, 1.0)
        self.fail_submit = False
        self.fail_latest = False

    def submit(self, _frame, ts):
        if self.fail_submit:
            raise RuntimeError("injected locator submission failure")
        self.result = (True, BOX, 0.9, ts)

    def submit_priority(self, frame, ts):
        self.submit(frame, ts)

    def latest(self):
        if self.fail_latest:
            raise RuntimeError("injected locator result failure")
        return self.result


def _reader(monkeypatch):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    monkeypatch.setenv("ORION_METER_DETECTOR_TRACK", "0")
    # Isolate detector authority from unrelated press/ghost policies.
    monkeypatch.setenv("ORION_READER_GHOST_PRESS_BREAK", "0")
    monkeypatch.setenv("ORION_READER_COLD_FIRST_READ_VETO", "0")
    monkeypatch.setenv("ORION_READER_PRESS_ONSET_PLAUSIBILITY", "0")
    monkeypatch.setenv("ORION_READER_IDLE_PUBLISH_GATE", "0")
    reader = SimpleMeterReader(1280, 720, cfg=_Cfg(), require_gameplay_eligibility=False)
    reader._meter_detector = _Locator()
    reader.set_shot_state(True, 1.0, True)
    reader.notify_physical_shot_start(1)
    return reader


def _frame():
    frame = np.zeros((720, 1280, 3), np.uint8)
    frame[350:405, 600:624] = 255
    return frame


def test_measurement_exception_never_publishes_colour_fallback(monkeypatch):
    reader = _reader(monkeypatch)

    def fail_measurement(*_args, **_kwargs):
        raise RuntimeError("injected white-ribbon measurement failure")

    monkeypatch.setattr(reader, "_measure_fill_in_box", fail_measurement)
    results = [reader.detect(_frame(), ts=ts) for ts in (1.0, 1.02, 1.04, 1.06)]
    assert not any(result.detected for result in results)
    assert all(result.rejection_reason == "detector_fill_exception" for result in results)
    assert not reader._det_fill_hist
    assert not reader._det_coarse_fill_hist
    assert reader.detector_health_snapshot()["fill_exception"] == 4


def test_locator_exception_rejects_stale_box_then_recovers(monkeypatch):
    reader = _reader(monkeypatch)
    assert reader.detect(_frame(), ts=1.0).detected
    assert reader._det_active_box is not None

    reader._meter_detector.fail_latest = True
    result = reader.detect(_frame(), ts=1.02)
    assert not result.detected
    assert result.rejection_reason == "detector_locator_exception"
    assert reader._det_active_box is None
    assert not reader._det_fill_hist
    assert not reader._det_coarse_fill_hist
    assert reader.detector_health_snapshot()["locator_exception"] == 1

    reader._meter_detector.fail_latest = False
    assert reader.detect(_frame(), ts=1.04).detected


@pytest.mark.parametrize("fault_site", ["submit", "latest"])
def test_repeated_locator_fault_never_renews_old_box(monkeypatch, fault_site):
    reader = _reader(monkeypatch)
    assert reader.detect(_frame(), ts=1.0).detected
    setattr(reader._meter_detector, "fail_" + fault_site, True)

    # Both normal and extended-coast windows have expired by the last frame.
    for ts in (1.02, 1.20, 1.70, 2.30):
        result = reader.detect(_frame(), ts=ts)
        assert not result.detected
        assert result.rejection_reason == "detector_locator_exception"
        assert reader._det_active_box is None


def test_locator_fault_cannot_latch_colour_structure(monkeypatch):
    reader = _reader(monkeypatch)
    reader._require_gameplay_eligibility = True
    reader._meter_detector.fail_latest = True

    def forbidden_qualification(*_args, **_kwargs):
        raise AssertionError("colour qualification ran after locator failure")

    monkeypatch.setattr(reader, "_qualify_gameplay_sample", forbidden_qualification)
    result = reader.detect(_frame(), ts=1.0)
    assert not result.detected
    assert result.rejection_reason == "detector_locator_exception"


def test_partial_locator_fault_retries_same_uncommitted_source(monkeypatch):
    reader = _reader(monkeypatch)
    locator = reader._meter_detector
    locator.result = (True, BOX, 0.9, 1.0)
    monkeypatch.setattr(locator, "submit", lambda _frame, _ts: None)
    monkeypatch.setattr(locator, "submit_priority", lambda _frame, _ts: None)
    original = reader._note_static_zone_absence
    calls = [0]

    def fail_once(**kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("injected post-high-water locator fault")
        return original(**kwargs)

    monkeypatch.setattr(reader, "_note_static_zone_absence", fail_once)
    first = reader.detect(_frame(), ts=1.0)
    assert not first.detected
    assert first.rejection_reason == "detector_locator_exception"
    assert reader._det_state == "idle"
    assert reader._det_seen_dts < 1.0
    second = reader.detect(_frame(), ts=1.02)
    assert second.detected
    assert reader._det_seen_dts == 1.0


def test_fill_fault_rolls_back_colour_qualification_proof(monkeypatch):
    reader = _reader(monkeypatch)
    reader._require_gameplay_eligibility = True

    def colour_qualification(sample, epoch):
        reader._latch_gameplay_structure_proof(epoch)
        return sample

    def fail_measurement(*_args, **_kwargs):
        raise RuntimeError("injected fill failure after colour proof")

    monkeypatch.setattr(reader, "_qualify_gameplay_sample", colour_qualification)
    monkeypatch.setattr(reader, "_measure_fill_in_box", fail_measurement)
    result = reader.detect(_frame(), ts=1.0)
    assert not result.detected
    assert result.rejection_reason == "detector_fill_exception"
    assert not reader._gameplay_structure_verified
    assert reader._gameplay_structure_proof_epoch == 0
    assert not reader._ep_census.get("latched", False)


def test_fill_fault_rolls_back_partial_latch_commit(monkeypatch):
    reader = _reader(monkeypatch)
    reader._require_gameplay_eligibility = True
    monkeypatch.setattr(reader, "_qualify_gameplay_sample", lambda sample, _epoch: sample)

    def low_green(_frame, _box, ts=None):
        reader._last_fill_coarse = 5.0
        return 5.0, (2.0, 8.0, 5.0, 6.0, 0.9, 3), 60

    monkeypatch.setattr(reader, "_measure_fill_in_box", low_green)
    original_latch = reader._latch_gameplay_structure_proof

    def commit_then_fail(epoch):
        original_latch(epoch)
        raise RuntimeError("injected post-latch failure")

    monkeypatch.setattr(reader, "_latch_gameplay_structure_proof", commit_then_fail)
    result = reader.detect(_frame(), ts=1.0)
    assert not result.detected
    assert result.rejection_reason == "detector_fill_exception"
    assert not reader._gameplay_structure_verified
    assert reader._gameplay_structure_proof_epoch == 0
    assert not reader._ep_census.get("latched", False)


def test_fill_fault_never_resurrects_identity_cleared_proof(monkeypatch):
    reader = _reader(monkeypatch)
    reader._require_gameplay_eligibility = True
    reader._latch_gameplay_structure_proof(1)
    reader._gameplay_lock_authorized = True

    def identity_steal(_frame, _ts):
        reader.last_debug = {"hw_reseat": True}
        return {"detected": True, "meter_present": True, "fill": 5.0,
                "fill_coarse": 5.0, "bbox": BOX, "stage": "steal_reseat",
                "confidence": 0.9, "velocity_pct_s": 0.0, "top_row": 350,
                "green": None, "rejection_reason": "", "rise_state": ""}

    monkeypatch.setattr(reader, "read", identity_steal)
    monkeypatch.setattr(reader, "_measure_fill_in_box",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fill")))
    result = reader.detect(_frame(), ts=1.0)
    assert not result.detected
    assert result.rejection_reason == "detector_fill_exception"
    assert not reader._gameplay_structure_verified
    assert reader._gameplay_structure_proof_epoch == 0
    assert not reader._gameplay_lock_authorized


def test_late_fill_exception_retires_partial_lock_bookkeeping(monkeypatch):
    reader = _reader(monkeypatch)
    reader._require_gameplay_eligibility = True
    monkeypatch.setattr(reader, "_qualify_gameplay_sample", lambda sample, _epoch: sample)
    values = iter((5.0, 15.0))

    def measured(_frame, _box, ts=None):
        value = next(values)
        reader._last_fill_coarse = value
        return value, None, 350

    monkeypatch.setattr(reader, "_measure_fill_in_box", measured)
    monkeypatch.setattr(reader, "_advance_detfill_nogreen_rise",
                        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("late fill")))
    for ts in (1.0, 1.02):
        result = reader.detect(_frame(), ts=ts)
        assert not result.detected
        assert result.rejection_reason == "detector_fill_exception"
        assert reader._det_lock_fill0 is None
        assert reader._det_lock_read_n == 0
        assert reader._det_active_box is None
        assert not reader._det_fill_hist


def test_old_frame_fill_fault_does_not_reset_new_arm(monkeypatch):
    reader = _reader(monkeypatch)

    def new_arm_then_fail(*_args, **_kwargs):
        reader.notify_physical_shot_start(2)
        reader._det_state = "locked"
        reader._det_lock_fill0 = 42.0
        reader._det_lock_fill_max = 45.0
        raise RuntimeError("old frame failed after new arm")

    monkeypatch.setattr(reader, "_measure_fill_in_box", new_arm_then_fail)
    result = reader.detect(_frame(), ts=1.0)
    assert not result.detected
    assert result.rejection_reason == "detector_fill_exception"
    assert result.gameplay_sample_epoch == 1
    assert reader._physical_shot_epoch == 2
    assert reader._det_state == "locked"
    assert reader._det_lock_fill0 == 42.0


def test_health_counter_exception_cannot_veto_valid_locator(monkeypatch):
    reader = _reader(monkeypatch)
    reader._det_diag_last = -1.0e9
    monkeypatch.setattr(reader, "_proposer_stats",
                        lambda: (_ for _ in ()).throw(RuntimeError("telemetry")))
    result = reader.detect(_frame(), ts=1.0)
    assert result.detected
    assert result.rejection_reason == ""
    assert reader.detector_health_snapshot()["health_exception"] == 1


def test_valid_locator_and_measurement_still_publish(monkeypatch):
    reader = _reader(monkeypatch)
    result = reader.detect(_frame(), ts=1.0)
    assert result.detected
    assert result.rejection_reason == ""
