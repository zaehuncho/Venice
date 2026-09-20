"""Compressed-reader source/profile resets; pure local pixels, no capture/model."""
import importlib.util
import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest


@pytest.fixture
def reader(monkeypatch):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    monkeypatch.setenv("ORION_METER_READER_Y", "0")
    candidate = os.environ.get("COMPRESSED_READER_TEST_MODULE")
    if candidate:
        spec = importlib.util.spec_from_file_location("compressed_source_candidate", candidate)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    else:
        import compressed_meter_reader as module
    cfg = SimpleNamespace(meter_style="Arrow2", meter_color="White", confidence_threshold=.32)
    return module.CompressedMeterReader(1280, 720, cfg=cfg, luma_tracking=False)


def _seed_source_pixels(reader):
    reader._acq_hint = ((40, 40, 20, 120), .9, 10.0)
    reader._decor_registry = [{"x": 50.0, "spacing": 20, "ts": 10.0, "hard": True}]
    reader._registry_frame = 29


def test_source_reset_discards_old_hint_and_hard_rejection_region(reader):
    _seed_source_pixels(reader)
    reader.reset_tracking()
    assert reader._acq_hint is None
    assert reader._decor_registry == []
    assert reader._registry_match(50.0, 20, 10.01) == ""
    assert reader._registry_frame == 0


def test_normal_lock_reset_preserves_same_source_registry_and_hint(reader):
    _seed_source_pixels(reader)
    hint = reader._acq_hint
    registry = list(reader._decor_registry)
    reader._reset_state()
    assert reader._acq_hint == hint
    assert reader._decor_registry == registry
    assert reader._registry_match(50.0, 20, 10.01) == "hard"


def test_unchanged_style_does_not_discard_source_registry(reader):
    _seed_source_pixels(reader)
    reader.set_active_style("Arrow2")
    assert reader._acq_hint is not None
    assert reader._registry_match(50.0, 20, 10.01) == "hard"


@pytest.mark.parametrize("change", ["style", "colour"])
def test_profile_change_uses_compressed_source_reset(reader, change):
    _seed_source_pixels(reader)
    if change == "style":
        reader.set_active_style("Pill")
    else:
        reader._cfg.meter_color = "Red"
        reader.reload_config(reader._cfg)
    assert reader._acq_hint is None
    assert reader._decor_registry == []


def _seed_moving_stale_roi(reader):
    frame = np.zeros((720, 1280, 3), np.uint8)
    reader.box = (40, 40, 20, 120)
    reader.last_tbox = list(reader.box)
    reader.conf = 1.0
    reader.last_fill = reader.last_coarse = 40.0
    reader._velocity = 100.0
    assert not reader._check_stale(frame, 9.0)  # records the actual ROI
    reader._last_read_ts = 9.0


@pytest.mark.parametrize("shape", [(360, 640), (1080, 1920)])
def test_direct_read_resizes_before_stale_early_return(reader, shape):
    _seed_source_pixels(reader)
    _seed_moving_stale_roi(reader)
    # Old coordinates lie inside both resolutions; identical ROI pixels would
    # otherwise return a held old-source box before base.read checks geometry.
    result = reader.read(np.zeros((*shape, 3), np.uint8), ts=10.0)
    assert (reader.H, reader.W) == shape
    assert result.get("rejection_reason") != "stale_frame"
    assert reader._acq_hint is None and reader._decor_registry == []
    assert reader._cur_ts == 10.0  # reset must precede current-frame de-lag stamp


def test_same_geometry_still_treats_frozen_moving_roi_as_missing_data(reader):
    _seed_moving_stale_roi(reader)
    result = reader.read(np.zeros((720, 1280, 3), np.uint8), ts=10.0)
    assert result["rejection_reason"] == "stale_frame"
    assert result["fill"] == 40.0
    assert result["bbox"] == [40, 40, 20, 120]


def test_first_direct_frame_initializes_geometry_without_partial_constructor_reset(monkeypatch):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    monkeypatch.setenv("ORION_METER_READER_Y", "0")
    candidate = os.environ.get("COMPRESSED_READER_TEST_MODULE")
    if candidate:
        spec = importlib.util.spec_from_file_location("compressed_first_frame_candidate", candidate)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    else:
        import compressed_meter_reader as module
    instance = module.CompressedMeterReader(luma_tracking=False)
    instance.read(np.zeros((360, 640, 3), np.uint8), ts=1.0)
    assert (instance.H, instance.W) == (360, 640)
    assert instance._cur_ts == 1.0
