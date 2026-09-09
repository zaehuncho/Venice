"""A physical camera zoom must not masquerade as progress along a filled meter.

The full local scene is resized, not just the NCC patch or the detector box.
These tests use real OpenCV matching, display-box smoothing and fill measurement
in one unchanged 1280x720 source. No locator/model or input device is opened.
"""
import cv2
import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader


W, H = 1280, 720
CX, BOTTOM = 612, 420
NOTCH = np.random.default_rng(7493).integers(35, 170, (17, 40), np.uint8)


class Config:
    meter_style = "Arrow2"
    meter_color = "White"
    confidence_threshold = .32


def _scene(rows=80, *, sx=1., sy=1., dx=0, dy=0, notch=True, cap=True):
    # The stable textured notch sits below the bar's white base. Its luminance
    # stays below the white-reader threshold so it cannot fabricate fill rows.
    tile = np.full((160, 80, 3), 40, np.uint8)
    if notch:
        tile[133:150, 20:60] = NOTCH[:, :, None]
    tile[133 - rows:133, 34:46] = 255
    if cap:
        tile[33:37, 36:44] = (0, 200, 0)
    tile = cv2.resize(tile, (round(80 * sx), round(160 * sy)),
                      interpolation=cv2.INTER_LINEAR)
    ox = round(CX + dx - 40 * sx)
    oy = round(BOTTOM + dy - 145 * sy)
    frame = np.full((H, W, 3), 40, np.uint8)
    frame[oy:oy + tile.shape[0], ox:ox + tile.shape[1]] = tile
    width, height = round(24 * sx), round(120 * sy)
    box = (round(CX + dx - width / 2), BOTTOM + dy - height, width, height)
    return frame, box


def _measure(reader, frame, box, index, *, refresh=False):
    ts = 1000. + index / 60.
    reader._det_new_accept = refresh
    tracked = reader._det_track_step(frame, box, ts)
    emitted = reader._det_smooth_emit(tracked)
    fill, green, top = reader._measure_fill_in_box(frame, emitted, ts=ts)
    assert top >= 0, "fixture lost the actual white meter edge"
    return float(fill), green


def _reader(monkeypatch):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    monkeypatch.setenv("ORION_METER_TRACK_CONTINUITY", "1")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL", "1")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_SESSION_RULER", "0")
    reader = SimpleMeterReader(W, H, cfg=Config())
    # Isolate camera-scale evidence from the optional detector reconciliation
    # bias. The ordinary innovation/deviation bounds remain active.
    reader._det_track_gain = 0.
    frame, box = _scene()
    reader._seed_track_template(frame, box)
    reader._det_track_box = box
    reader._det_tmpl_ts = 1000.
    last = None
    for index, rows in enumerate((10, 30, 50, 70, 80)):
        last = _measure(reader, *_scene(rows), index)
    assert reader._subpx_D is not None
    return reader, last


@pytest.mark.parametrize("sx,sy", [(1.1, 1.1), (.9, .9), (1., 1.1), (1.1, 1.)])
@pytest.mark.parametrize("refresh", [False, True])
def test_visible_physical_zoom_preserves_fill_without_compounding(
        monkeypatch, sx, sy, refresh):
    reader, (expected_fill, expected_green) = _reader(monkeypatch)
    generations = []
    for k in range(6):
        frame, box = _scene(sx=sx, sy=sy, dx=3, dy=-2)
        fill, green = _measure(reader, frame, box, 5 + k,
                              refresh=refresh and k % 3 == 0)
        assert reader._det_track_score >= reader._det_track_min
        assert fill == pytest.approx(expected_fill, abs=1.5), \
            "camera scale changed measured fill despite unchanged physical fill fraction"
        assert green is not None and expected_green is not None
        assert green[:2] == pytest.approx(expected_green[:2], abs=1.5), \
            "green target and fill used different camera-scale coordinates"
        generations.append(reader._last_fill_estimator_generation)
    # A one-time coordinate rebase may intentionally fence an estimator, but
    # unchanged scale / detector-cadence template refresh cannot keep fencing it.
    assert len(set(generations)) == 1, "held camera scale churned the ruler generation"


@pytest.mark.parametrize("width,height", [(26, 132), (22, 108), (26, 120), (24, 132)])
@pytest.mark.parametrize("refresh", [False, True])
def test_detector_box_breathing_alone_never_rescales_a_latched_ruler(
        monkeypatch, width, height, refresh):
    reader, (expected_fill, _green) = _reader(monkeypatch)
    original_generation = reader._last_fill_estimator_generation
    original_D = reader._subpx_D
    for k in range(6):
        frame, _ = _scene(dx=3, dy=-2)
        proposal = (round(CX + 3 - width / 2), BOTTOM - 2 - height, width, height)
        fill, _green = _measure(reader, frame, proposal, 5 + k,
                               refresh=refresh and k % 3 == 0)
        assert reader._det_track_score >= reader._det_track_min
        assert fill == pytest.approx(expected_fill, abs=.2)
        assert reader._subpx_D == original_D
        assert reader._last_fill_estimator_generation == original_generation


def test_unobserved_scale_proposal_does_not_gain_ruler_authority(monkeypatch):
    reader, (expected_fill, _green) = _reader(monkeypatch)
    original_generation = reader._last_fill_estimator_generation
    original_D = reader._subpx_D
    frame, _ = _scene(notch=False)
    proposal = (599, 288, 26, 132)
    fill, _green = _measure(reader, frame, proposal, 5)
    assert reader._det_track_score < reader._det_track_min
    assert fill == pytest.approx(expected_fill, abs=.2)
    assert reader._subpx_D == original_D
    assert reader._last_fill_estimator_generation == original_generation


def test_zoomed_fill_rise_keeps_the_same_normalized_trajectory(monkeypatch):
    control, _ = _reader(monkeypatch)
    zoomed, _ = _reader(monkeypatch)
    for k, rows in enumerate((80, 82, 84, 86, 88)):
        normal_fill, _ = _measure(control, *_scene(rows, dx=3, dy=-2), 5 + k)
        zoom_fill, _ = _measure(zoomed, *_scene(rows, sx=1.1, sy=1.1, dx=3, dy=-2),
                                5 + k, refresh=k % 3 == 0)
        assert zoomed._det_track_score >= zoomed._det_track_min
        assert zoom_fill == pytest.approx(normal_fill, abs=1.5), \
            "physical zoom changed the normalized fill trajectory used for timing"


@pytest.mark.parametrize("refresh", [False, True])
def test_camera_return_to_seed_scale_retires_prior_scale_correction(monkeypatch, refresh):
    reader, (expected_fill, _green) = _reader(monkeypatch)
    for k, scale in enumerate((1.1, 1.1, 1.1, 1., 1., 1.)):
        frame, box = _scene(sx=scale, sy=scale, dx=3, dy=-2)
        fill, _green = _measure(reader, frame, box, 5 + k,
                               refresh=refresh and k % 3 == 0)
        assert reader._det_track_score >= reader._det_track_min
        assert fill == pytest.approx(expected_fill, abs=1.5), \
            "an original-template match retained the previous camera scale"


def test_resized_notch_alone_cannot_rescale_without_independent_meter_span(monkeypatch):
    reader, (_fill, _green) = _reader(monkeypatch)
    original_effective_D = reader._last_subpx_transform[1]
    original_generation = reader._last_fill_estimator_generation
    frame, box = _scene(sx=1.1, sy=1.1, dx=3, dy=-2, cap=False)
    _measure(reader, frame, box, 5)
    assert reader._det_track_score >= reader._det_track_min
    assert reader._last_subpx_transform[1] == original_effective_D, \
        "a notch match acquired scale authority without an independent cap/base span"
    assert reader._last_fill_estimator_generation == original_generation


def test_accepted_camera_scale_survives_a_subpixel_edge_bridge(monkeypatch):
    reader, (expected_fill, _green) = _reader(monkeypatch)
    frame, box = _scene(sx=1.1, sy=1.1, dx=3, dy=-2)
    zoom_fill, _green = _measure(reader, frame, box, 5)
    assert zoom_fill == pytest.approx(expected_fill, abs=1.5)
    # A scale transition correctly discards pre-zoom derivative pairs. Supply
    # the second directly measured post-zoom base required by the existing
    # short-lived predictor; no one-anchor hold behavior is assumed here.
    zoom_fill, _green = _measure(reader, frame, box, 6)
    assert zoom_fill == pytest.approx(expected_fill, abs=1.5)
    effective_D = reader._last_subpx_transform[1]
    generation = reader._last_fill_estimator_generation
    # Model an edge-fit quality rejection only. The frame still carries its
    # actual coarse white edge, current NCC notch, and short-lived measured base.
    monkeypatch.setattr(reader, "_subpixel_fill_edge", lambda *_args, **_kwargs: None)
    bridged_fill, _green = _measure(reader, frame, box, 7)
    assert reader._dbg_subpx.get("bridge") == 1
    assert bridged_fill == pytest.approx(expected_fill, abs=1.5)
    assert reader._last_subpx_transform[1] == effective_D
    assert reader._last_fill_estimator_generation == generation


@pytest.mark.parametrize("physical_zoom", [False, True])
def test_enabled_yolo_adapter_public_detect_path_preserves_ruler_and_retires_absence(
        monkeypatch, physical_zoom):
    # Exercise the production YOLO adapter and public reader.detect() wiring.
    # Only model inference is scripted: no reader, tracker, lifecycle, scale,
    # fill, or AsyncMeterLocator method is replaced. This is a wiring regression,
    # not a claim about trained ONNX model accuracy on synthetic images.
    import meter_detector_yolo as yolo

    class ScriptedInference:
        ok = True
        provider = "regression-inference"
        box = None
        calls = 0

        def detect_box(self, frame):
            assert frame.shape == (H, W, 3)
            self.calls += 1
            return (*self.box, .99) if self.box is not None else None

    inference = ScriptedInference()
    locator = yolo.AsyncMeterLocator(base=inference, sync=True)
    monkeypatch.setenv("ORION_METER_DETECTOR", "1")
    monkeypatch.setenv("ORION_METER_TRACK_CONTINUITY", "1")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL", "1")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_SESSION_RULER", "0")
    monkeypatch.setattr(yolo, "get_async_locator", lambda: locator)
    try:
        reader = SimpleMeterReader(W, H, cfg=Config(), require_gameplay_eligibility=True)
        assert reader._meter_detector is locator
        reader.set_shot_state(True, 1., True)
        reader.notify_physical_shot_start(7)
        for index, rows in enumerate((10, 14, 18, 24, 32, 40, 50, 60, 70, 80)):
            frame, inference.box = _scene(rows)
            result = reader.detect(frame, ts=1000. + index / 60.)
        assert result.detected and result.gameplay_structure_verified
        assert result.gameplay_structure_epoch == 7
        expected_fill = result.fill_pct
        canonical_D = reader._subpx_D
        generation = reader._last_fill_estimator_generation
        for k in range(4):
            scale = 1.1 if physical_zoom else 1.
            frame, _ = _scene(sx=scale, sy=scale)
            inference.box = (599, 288, 26, 132)
            ts = 1000. + (10 + k) / 60.
            result = reader.detect(frame, ts=ts)
            assert locator.latest()[3] == ts
            assert result.detected and result.gameplay_structure_verified
            assert result.fill_pct == pytest.approx(expected_fill, abs=1.5)
            assert reader._subpx_D == canonical_D
            assert reader._last_fill_estimator_generation == generation
            assert reader._subpx_camera_scale == (1.1 if physical_zoom else 1.)
        assert inference.calls >= 14
        # Complete the disappearance through public detector-negative results,
        # not by directly invoking an internal reset helper.
        inference.box = None
        absent = np.full((H, W, 3), 40, np.uint8)
        for k in range(60):
            result = reader.detect(absent, ts=1000. + (14 + k) / 60.)
        assert not result.detected and reader._det_state == "idle"
        assert reader._subpx_camera_ref is None
        assert reader._subpx_camera_scale == 1.
        assert reader._det_track_scale_match is None
    finally:
        locator.stop()
