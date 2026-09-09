"""Geometry regressions for the learned full-frame meter locator."""

import numpy as np

from meter_detector_yolo import MeterYoloLocator


_BAND_ENV = (
    "ORION_METER_BAND_TOP",
    "ORION_METER_BAND_BOTTOM",
    "ORION_METER_W_MIN_FRAC",
    "ORION_METER_W_MAX_FRAC",
    "ORION_METER_H_MIN_FRAC",
    "ORION_METER_H_MAX_FRAC",
)


def _locator(monkeypatch, tmp_path):
    for name in _BAND_ENV:
        monkeypatch.delenv(name, raising=False)
    # A missing model intentionally leaves inference disabled while still constructing
    # the exact production geometry gate.  These tests therefore need neither CUDA nor
    # ONNX Runtime and cannot accidentally load a developer's local weight file.
    return MeterYoloLocator(model_path=str(tmp_path / "missing.onnx"))


def test_default_band_keeps_archived_deep_meter_geometry(monkeypatch, tmp_path):
    locator = _locator(monkeypatch, tmp_path)

    assert locator._band_bot == 0.95
    # Representative 1280x720 n3 boxes from the archived deep-shot frames.  Their
    # centre-y values are 0.881-0.921, below the player but still genuine meters.
    for box in (
        (500, 582, 24, 104),
        (510, 590, 23, 108),
        (520, 600, 24, 104),
        (530, 608, 23, 110),
    ):
        assert locator._plausible(*box, 1280, 720), box


def test_expanded_bottom_band_remains_bounded(monkeypatch, tmp_path):
    locator = _locator(monkeypatch, tmp_path)

    # Centre below the 0.95 court band, scoreboard/top-band décor, and implausible
    # dimensions remain rejected.  The deep-shot fix widens only the bottom location.
    assert not locator._plausible(500, 640, 24, 104, 1280, 720)
    assert not locator._plausible(500, 20, 24, 104, 1280, 720)
    assert not locator._plausible(500, 590, 80, 108, 1280, 720)
    assert not locator._plausible(500, 500, 24, 200, 1280, 720)


def test_environment_can_restore_legacy_bottom_band(monkeypatch, tmp_path):
    for name in _BAND_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ORION_METER_BAND_BOTTOM", "0.85")
    locator = MeterYoloLocator(model_path=str(tmp_path / "missing.onnx"))

    assert locator._band_bot == 0.85
    assert not locator._plausible(500, 582, 24, 104, 1280, 720)


def test_invalid_top_score_cannot_hide_lower_score_real_meter(monkeypatch, tmp_path):
    locator = _locator(monkeypatch, tmp_path)

    class _Session:
        @staticmethod
        def run(_outputs, _feeds):
            # Model-space coordinates for a 1280-square letterbox of a 1280x720 frame
            # (280px vertical pad). Proposal 0 is a sharper but impossible scoreboard
            # column; proposal 1 is a normal 24x106 court meter.
            return [np.array([[[510.0, 712.0],
                               [320.0, 630.0],
                               [24.0, 24.0],
                               [180.0, 106.0],
                               [0.93, 0.81]]], dtype=np.float32)]

    locator._sess = _Session()
    locator._inp = "images"
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)

    found = locator._detect_box_locked(frame)

    assert found is not None
    x, y, w, h, conf = found
    assert (x, y, w, h) == (700, 297, 24, 106)
    assert abs(conf - 0.81) < 1e-5


class _OneProposalSession:
    def __init__(self, proposal):
        self.proposal = proposal

    def run(self, _outputs, _feeds):
        # Runtime export shape is (1, 5, N).
        return [np.asarray(self.proposal, dtype=np.float32).reshape(1, 5, 1)]


def _model_proposal(box, src_w, src_h, imgsz=960, conf=0.80):
    """Map one source-pixel xywh box into the export's letterboxed cxcywh."""
    x, y, w, h = box
    r = min(imgsz / float(src_h), imgsz / float(src_w))
    nw, nh = int(round(src_w * r)), int(round(src_h * r))
    left, top = (imgsz - nw) // 2, (imgsz - nh) // 2
    return ((x + w * 0.5) * r + left,
            (y + h * 0.5) * r + top,
            w * r, h * r, conf)


def test_edge_tile_maps_box_back_and_uses_full_frame_geometry(monkeypatch, tmp_path):
    locator = _locator(monkeypatch, tmp_path)
    locator.ok = True
    locator.imgsz = 960
    locator._inp = "images"
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    crop_w = int(round(1280 * 0.55))

    # Left tile: local x=100 is also full-frame x=100.
    locator._sess = _OneProposalSession(
        _model_proposal((100, 300, 24, 106), crop_w, 720))
    assert locator.detect_box_tile(frame, "left")[:4] == (100, 300, 24, 106)

    # Right tile starts at x=576; local x=424 maps back to full-frame x=1000.
    right_x0 = 1280 - crop_w
    locator._sess = _OneProposalSession(
        _model_proposal((1000 - right_x0, 300, 24, 106), crop_w, 720))
    assert locator.detect_box_tile(frame, "right")[:4] == (1000, 300, 24, 106)

    # 12px would pass the crop-relative minimum (12/704 > .013) but fails the
    # unchanged full-frame gate (12/1280 < .013). The tile must not loosen it.
    locator._sess = _OneProposalSession(
        _model_proposal((100, 300, 12, 106), crop_w, 720))
    assert locator.detect_box_tile(frame, "left") is None

    assert locator.detect_box_tile(frame, "centre") is None


def test_letterbox_repaints_padding_when_scan_geometry_changes(monkeypatch, tmp_path):
    locator = _locator(monkeypatch, tmp_path)
    locator.imgsz = 960
    full = np.full((720, 1280, 3), 31, dtype=np.uint8)
    tile = np.full((720, int(round(1280 * 0.55)), 3), 207, dtype=np.uint8)

    locator._letterbox(full)
    locator._letterbox(tile)
    canvas, _r, left, top = locator._letterbox(full)
    nh = int(round(720 * min(960 / 720.0, 960 / 1280.0)))
    nw = int(round(1280 * min(960 / 720.0, 960 / 1280.0)))

    # Without the geometry-change fill these bands contain the prior tile's
    # court pixels, a synthetic image edge visible to YOLO.
    assert top > 0 and left == 0 and nw == 960
    assert np.all(canvas[:top] == 114)
    assert np.all(canvas[top + nh:] == 114)
