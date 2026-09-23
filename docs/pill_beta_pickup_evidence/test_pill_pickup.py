"""Synthetic Pill pickup wiring; no ONNX model, source capture, or app launch."""

import logging
from types import SimpleNamespace

import pytest

import player_anchor as pa
from remote_play_orchestrator import RemotePlayOrchestrator
from simple_meter_reader import SimpleMeterReader


@pytest.fixture(autouse=True)
def _no_anchor(monkeypatch):
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "0")
    monkeypatch.setenv("ORION_CV_TIPLESS_ARMED", "0")
    pa.ARM.reset()
    yield
    pa.ARM.reset()


def _reader():
    reader = SimpleMeterReader.__new__(SimpleMeterReader)
    reader._meter_detector = SimpleNamespace(_base=SimpleNamespace())
    reader._tracking_meter_style = "pill"
    reader._physical_shot_epoch = 0
    reader._shot_armed_hw = False
    reader._pa_epoch = 0
    reader._pa_released_epoch = 0
    reader._pa_last_ts = -1.0
    reader._pa_rhythm = False
    reader._pa_shot_type_for = lambda _epoch: "Standstill"
    return reader


def _lines(caplog):
    return [record.getMessage() for record in caplog.records
            if "PICKUP:" in record.getMessage()]


def test_pill_first_valid_fill_and_miss_are_joined_by_press_epoch(caplog):
    reader = _reader()
    base = reader._meter_detector._base
    reader._physical_shot_epoch = 41
    reader._shot_armed_hw = True
    reader._publish_press_window(100.0)
    assert base.pickup["epoch"] == 41
    assert base.pickup["first_sight_fill"] == -1.0

    reader._note_pill_pickup(99.9, 9.0)  # stale frame predates press
    assert base.pickup["first_sight_fill"] == -1.0
    reader._note_pill_pickup(100.2, 8.3)
    reader._note_pill_pickup(100.3, 22.0)  # later reads cannot replace first sight
    assert base.pickup["first_sight_fill"] == 8.3
    assert base.pickup["first_sight_ms_after_press"] == pytest.approx(200.0)
    assert base.pickup["anchor_used"] == 0

    orchestrator = SimpleNamespace(
        _meter_detector=SimpleNamespace(_meter_detector=reader._meter_detector))
    joined = RemotePlayOrchestrator._read_pickup_record(orchestrator, 41)
    assert joined["first_sight_fill"] == 8.3
    assert RemotePlayOrchestrator._read_pickup_record(orchestrator, 40) is None

    reader._shot_armed_hw = False
    reader._physical_shot_epoch = 0
    with caplog.at_level(logging.ERROR, logger="simple_reader"):
        reader._publish_press_window(100.4)
    assert len(_lines(caplog)) == 1
    assert "epoch=41 first_sight_fill=8.3" in _lines(caplog)[0]

    reader._physical_shot_epoch = 42
    reader._shot_armed_hw = True
    reader._publish_press_window(101.0)
    reader._shot_armed_hw = False
    reader._physical_shot_epoch = 0
    with caplog.at_level(logging.ERROR, logger="simple_reader"):
        reader._publish_press_window(101.4)
    assert len(_lines(caplog)) == 2
    assert "epoch=42 first_sight_fill=-1.0" in _lines(caplog)[1]


def test_pill_release_before_first_frame_still_flushes_miss(caplog):
    reader = _reader()
    reader._physical_shot_epoch = 45
    reader._roll_pickup_record(45)  # authoritative control-thread press
    with caplog.at_level(logging.ERROR, logger="simple_reader"):
        assert reader.notify_physical_shot_release(45)
    assert len(_lines(caplog)) == 1
    assert "epoch=45 first_sight_fill=-1.0" in _lines(caplog)[0]


def test_pill_does_not_attribute_post_release_or_other_epoch_fill():
    reader = _reader()
    reader._physical_shot_epoch = 51
    reader._shot_armed_hw = True
    reader._publish_press_window(200.0)
    base = reader._meter_detector._base
    pa.ARM.note_release(51, 200.1)
    reader._note_pill_pickup(200.2, 12.0)
    assert base.pickup["first_sight_fill"] == -1.0
    pa.ARM.note_press(52, 200.3)
    reader._note_pill_pickup(200.4, 15.0)
    assert base.pickup["first_sight_fill"] == -1.0
