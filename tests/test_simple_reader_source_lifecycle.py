"""Source/profile boundaries invalidate rulers; ordinary shots preserve them."""
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest


@pytest.fixture
def reader(monkeypatch):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    candidate = os.environ.get("ORION_READER_TEST_MODULE")
    if candidate:
        spec = importlib.util.spec_from_file_location("reader_source_lifecycle_candidate", candidate)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    else:
        import simple_meter_reader as module
    cfg = SimpleNamespace(meter_style="Arrow2", meter_color="White", confidence_threshold=.32)
    return module.SimpleMeterReader(frame_w=1280, frame_h=720, cfg=cfg)


def seed(reader):
    reader._subpx_ruler_hist = [(100.0, -12.0)] * 3
    reader._sess_track_h = [107.0] * 3
    reader._subpx_D = 100.0
    reader._subpx_off = -12.0
    reader._subpx_provisional = True
    reader._det_state = "locked"
    reader._det_last_box = (600, 300, 24, 107)
    reader._det_active_box = reader._det_last_box
    reader._det_last_found_ts = 90.0
    reader._det_seen_dts = 90.0
    reader._det_warm_pos = reader._det_last_box
    reader._det_warm_ts = 90.0
    reader._det_fill_hist.append((90.0, 80.0))
    reader._det_box_hist.append((90.0, 612, 407, 24, 107))
    reader._det_track_vy = 5.0
    reader._det_tmpl = np.ones((107, 24), np.uint8)
    reader._arm_edge_mask = np.ones((720, 1280), np.uint8)
    reader._arm_edge_mask_epoch = 4
    reader._fill_estimator_identity = ("subpixel", (100, -12))
    reader._fill_estimator_generation = 12
    reader._last_fill_estimator_generation = 12


class Locator:
    provider = "fake"
    infer_ms = 0.0

    def __init__(self, reader):
        self.reader = reader
        self.reset_calls = 0
        self.snapshots = []
        self.result = (True, (600, 300, 24, 107), .9, 90.0)

    def reset(self):
        self.reset_calls += 1
        self.result = (False, None, 0.0, -1.0)

    def submit(self, frame, ts):
        self.snapshots.append((self.reader.W, self.reader.H,
                               list(self.reader._subpx_ruler_hist), self.reader._det_state))

    def latest(self):
        return self.result


def test_reset_tracking_clears_detector_geometry_and_slow_ruler(reader):
    seed(reader)
    reader.reset_tracking()
    assert reader._subpx_ruler_hist == []
    assert reader._sess_track_h == []
    assert reader._subpx_D is None and reader._subpx_off is None
    assert not reader._subpx_provisional
    assert reader._det_state == "idle"
    assert reader._det_active_box is None and reader._det_last_box is None
    assert reader._det_warm_pos is None
    assert not reader._det_fill_hist and not reader._det_box_hist
    assert reader._det_track_vy == 0 and reader._det_tmpl is None
    assert reader._arm_edge_mask is None
    assert reader._det_seen_dts < 0


def test_profile_reset_invalidates_same_size_async_result(reader):
    reader._meter_detector = worker = Locator(reader)
    reader.reset_tracking()
    assert worker.reset_calls == 1
    assert worker.latest() == (False, None, 0.0, -1.0)


@pytest.mark.parametrize("shape", [(360, 640), (1080, 1920)])
def test_detect_resizes_before_locator_and_clears_old_ruler(reader, shape):
    seed(reader)
    reader._meter_detector = worker = Locator(reader)
    reader.detect(np.zeros((*shape, 3), np.uint8), ts=100.0)
    assert worker.snapshots == [(shape[1], shape[0], [], "idle")]
    assert worker.reset_calls == 1
    reference = type(reader)(frame_w=shape[1], frame_h=shape[0], cfg=reader._cfg)
    assert reader._band == reference._band
    assert reader._h_acq == reference._h_acq


@pytest.mark.parametrize("shape", [(360, 640), (1080, 1920)])
def test_read_resizes_and_ends_old_session(reader, shape):
    seed(reader)
    reader.read(np.zeros((*shape, 3), np.uint8), ts=100.0)
    assert (reader.H, reader.W) == shape
    assert reader._subpx_ruler_hist == []
    assert reader._det_state == "idle"


def test_same_shape_does_not_cold_reset_ruler(reader):
    seed(reader)
    reader._meter_detector = worker = Locator(reader)
    reader.detect(np.zeros((720, 1280, 3), np.uint8), ts=100.0)
    assert worker.snapshots[0][2] == [(100.0, -12.0)] * 3
    assert worker.reset_calls == 0


def test_style_change_resets_even_when_shared_cfg_was_already_updated(reader):
    seed(reader)
    reader._cfg.meter_style = "Pill"  # orchestrator writes shared config first
    reader.set_active_style("Pill")
    assert reader._subpx_ruler_hist == []
    assert reader._det_state == "idle"


def test_repeated_same_style_keeps_session_ruler(reader):
    seed(reader)
    reader.set_active_style("Arrow2")
    assert reader._subpx_ruler_hist == [(100.0, -12.0)] * 3
    assert reader._det_state == "locked"


def test_colour_reload_discards_detector_ruler(reader):
    seed(reader)
    reader._cfg.meter_color = "Red"
    reader.reload_config(reader._cfg)
    assert reader._subpx_ruler_hist == []
    assert reader._det_state == "idle"


@pytest.mark.parametrize("old_colour,new_colour", [("White", "Red"), ("Red", "White")])
def test_colour_reload_refreshes_geometry_like_fresh_reader(reader, old_colour, new_colour):
    reader._cfg.meter_color = old_colour
    reader.reload_config(reader._cfg)
    reader._cfg.meter_color = new_colour
    reader.reload_config(reader._cfg)
    fresh = type(reader)(frame_w=reader.W, frame_h=reader.H, cfg=reader._cfg)
    for name in ("_w_min", "_gw_min", "_gh_min", "_g_area_min"):
        assert getattr(reader, name) == getattr(fresh, name), name


def test_old_capless_history_and_cold_scan_cooldown_end_at_source_reset(reader):
    reader._capless_since = 0.0
    reader._capless_hist.extend((2.4 + i * .01, False) for i in range(10))
    reader._prob_refuse_n = 10
    reader.reset_tracking()
    assert reader._capless_since is None and not reader._capless_hist
    assert reader._prob_refuse_n == 0


def test_normal_lock_and_shot_scale_resets_preserve_session_history(reader):
    seed(reader)
    reader._det_reset_lock_state()
    reader.reset_session_scale()
    assert reader._subpx_ruler_hist == [(100.0, -12.0)] * 3
    assert reader._sess_track_h == [107.0] * 3


def test_reset_reissues_ruler_identity_without_rewinding_generation(reader):
    seed(reader)
    reader.reset_tracking()
    assert reader._fill_estimator_generation == 12
    assert reader._last_fill_estimator_generation == 0
    reader._stamp_fill_estimator("subpixel", (100, -12))
    assert reader._last_fill_estimator_generation == 13


def test_reset_does_not_invent_physical_shot_authority(reader):
    reader._physical_shot_epoch = 4
    reader._shot_armed_hw = True
    reader._gameplay_structure_verified = True
    reader._gameplay_structure_proof_epoch = 4
    reader.reset_tracking()
    assert reader._physical_shot_epoch == 4 and reader._shot_armed_hw
    assert not reader._gameplay_structure_verified
    assert reader._gameplay_structure_proof_epoch == 0
