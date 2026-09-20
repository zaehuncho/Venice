"""Detached white decor is not a timing edge, even with a current green cap.

These are pixel/reader regressions, not a claim about unseen live footage or
model accuracy. Scripted locator results exercise the reader's independent
measurement and current-epoch structure proof on exact synthetic pixels.
"""
import os

import cv2
import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader


class Config:
    meter_style = "Arrow2"
    meter_color = "Red"  # detector-authoritative White path must also be correct
    confidence_threshold = .32


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("ORION_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    monkeypatch.setenv("ORION_METER_DETECTOR_TRACK", "0")


def scene(top=76, bottom=82, scale=1., *, cap=True):
    patch = np.full((107, 24, 3), 40, np.uint8)
    if top is not None:
        patch[top:bottom, 5:19] = 250
    if cap:
        patch[3:8, 6:18] = (60, 200, 60)
    patch = cv2.resize(patch, (round(24*scale), round(107*scale)),
                       interpolation=cv2.INTER_NEAREST)
    frame = np.full((1080, 1920, 3), 35, np.uint8)
    h, w = patch.shape[:2]
    frame[300:300+h, 600:600+w] = patch
    return frame, (600, 300, w, h)


@pytest.mark.parametrize("top,bottom", [(5, 11), (40, 68), (65, 82)])
@pytest.mark.parametrize("subpixel", [False, True])
@pytest.mark.parametrize("scale", [.75, 1., 1.5])
def test_detached_run_never_mints_a_timing_edge(monkeypatch, top, bottom, subpixel, scale):
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL", str(int(subpixel)))
    reader = SimpleMeterReader(1920, 1080, cfg=Config())
    actual = reader._measure_fill_in_box(*scene(top, bottom, scale), ts=1.)
    assert actual == (0., None, -1)
    assert reader._last_fill_coarse == 0.
    assert reader._last_fill_estimator_mode == ""
    assert reader._last_fill_estimator_generation == 0


@pytest.mark.parametrize("top", [90, 95])
@pytest.mark.parametrize("subpixel", [False, True])
@pytest.mark.parametrize("scale", [.75, 1., 1.5])
def test_genuine_low_base_ribbon_stays_measurable(monkeypatch, top, subpixel, scale):
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL", str(int(subpixel)))
    reader = SimpleMeterReader(1920, 1080, cfg=Config())
    fill, green, edge = reader._measure_fill_in_box(*scene(top, 100, scale), ts=1.)
    assert edge >= 0 and 0. < fill < 25.
    assert green is not None


class Locator:
    ok = True
    provider = "regression-inference"
    infer_ms = 0.
    box = (600, 300, 24, 107)

    def submit(self, frame, ts):
        self.result = (True, self.box, .99, ts)

    def submit_priority(self, frame, ts):
        self.submit(frame, ts)

    def latest(self):
        return self.result

    def detect_now(self, frame, ts):
        self.submit(frame, ts)
        return self.result


@pytest.mark.parametrize("subpixel", [False, True])
def test_floating_rise_cannot_stamp_current_epoch_but_real_onset_can(monkeypatch, subpixel):
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL", str(int(subpixel)))
    reader = SimpleMeterReader(1920, 1080, cfg=Config(), require_gameplay_eligibility=True)
    reader._meter_detector = Locator()
    reader.set_shot_state(True, 1., True)
    reader.notify_physical_shot_start(7)
    for k, top in enumerate((76, 73, 70, 67)):
        frame, _ = scene(top, 82)
        result = reader.detect(frame, ts=1.+k/60.)
        assert not result.gameplay_structure_verified
        assert result.fill_pct == 0. and result.raw_fill_pct == 0.
    frame, _ = scene(90, 100)
    result = reader.detect(frame, ts=1.+4/60.)
    assert result.detected and 0. < result.fill_pct < 25.
    assert result.gameplay_structure_verified and result.gameplay_structure_epoch == 7


@pytest.mark.parametrize("subpixel", [False, True])
def test_repeated_floating_rejections_preserve_trusted_ruler_and_width_history(monkeypatch, subpixel):
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL", str(int(subpixel)))
    monkeypatch.setenv("ORION_METER_SUBPIXEL_SESSION_RULER", "0")
    reader = SimpleMeterReader(1920, 1080, cfg=Config())
    for k, top in enumerate((90, 85, 80, 75, 70)):
        result = reader._measure_fill_in_box(*scene(top, 100), ts=1.+k/60.)
        assert result[2] >= 0
    assert reader._subpx_D is not None
    reader._box_w_hist.extend([24/107]*12)
    ruler = (reader._subpx_D, reader._subpx_off)
    widths = tuple(reader._box_w_hist)
    for k in range(40):
        assert reader._measure_fill_in_box(*scene(40, 68), ts=2.+k/60.) == (0., None, -1)
        assert (reader._subpx_D, reader._subpx_off) == ruler
        assert tuple(reader._box_w_hist) == widths
    result = reader._measure_fill_in_box(*scene(50, 100), ts=3.)
    assert result[2] == 50 and result[0] > 40.
    assert (reader._subpx_D, reader._subpx_off) == ruler


def test_reader_latch_log_does_not_claim_native_shot_ownership(caplog):
    reader = SimpleMeterReader(1920, 1080, cfg=Config())
    reader.set_shot_state(True, 1., True)
    reader.notify_physical_shot_start(7)
    reader._det_lock_generation = 12
    box = (600, 300, 24, 107)
    reader._arm_box_latch(box, 1.)
    messages = [r.getMessage() for r in caplog.records
                if r.getMessage().startswith("BOX LATCHED:")]
    assert len(messages) == 1
    assert messages[0].startswith("BOX LATCHED: epoch=7 generation=12 box=[600,300,24,107] ")
    assert "engine owns" not in messages[0]
    assert messages[0].endswith("reader_geometry_latched=1 native_ownership=unconfirmed")
    reader._arm_box_latch(box, 1.02)
    assert len([r for r in caplog.records if r.getMessage().startswith("BOX LATCHED:")]) == 1


@pytest.mark.parametrize("ticks", [False, True])
@pytest.mark.parametrize("subpixel", [False, True])
def test_unselected_single_base_rung_does_not_validate_a_floating_stripe(
        monkeypatch, ticks, subpixel):
    from test_simple_reader_rung_fill import _render_pill, _measure

    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL", str(int(subpixel)))
    reader = SimpleMeterReader(1280, 720)
    patch = _render_pill(100, ticks=ticks, occluder=(30, 45))
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    white = (hsv[:, :, 2] >= 200) & (hsv[:, :, 1] <= 65)
    white[30:45] = False
    # A genuine low-fill base rung exists, but the unchanged selection policy
    # does not prefer its single segment to the disconnected broad stripe.
    assert reader._rung_fill_block(white, *patch.shape[:2]) == (100, 8, 1)
    assert _measure(reader, patch) == (0., None, -1)
    assert reader._dbg_rung is None
    assert reader._last_fill_coarse == 0.
    assert reader._last_fill_estimator_mode == ""
    assert reader._last_fill_estimator_generation == 0
    assert reader._dbg_subpx["gate"] == "floating_without_base_ribbon"


@pytest.mark.parametrize("subpixel", [False, True])
def test_top_sharing_pill_tick_keeps_its_base_supported_edge(monkeypatch, subpixel):
    from test_simple_reader_rung_fill import _render_pill, _measure, BH

    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL", str(int(subpixel)))
    reader = SimpleMeterReader(1280, 720)
    patch = _render_pill(12, ticks=True)
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    white = (hsv[:, :, 2] >= 200) & (hsv[:, :, 1] <= 65)
    assert reader._rung_fill_block(white, *patch.shape[:2]) == (8, 100, 11)
    fill, green, edge = _measure(reader, patch)
    # The walk's one-row tick and valid base-reaching ladder share row 8.
    # Preserve the established coarse coordinate instead of rejecting the tick.
    assert edge == 8 and reader._dbg_rung is None
    assert reader._last_fill_coarse == round((BH - 1 - 8) / (BH - 1) * 100, 2)
    assert abs(fill - reader._last_fill_coarse) < 1.0
    assert green is not None
