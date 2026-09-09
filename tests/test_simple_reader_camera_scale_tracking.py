"""Current-pixel notch localization across detector-supported camera rescaling.

Deterministic image transforms exercise real OpenCV NCC, not mocked scores. A
changed detector box is only a scale proposal: the old-size appearance remains
a competing candidate, and geometry alone must never create a strong match.
"""
import cv2
import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader


W, H = 1280, 720
BOX = (600, 300, 24, 110)
CX, BOTTOM = 612, 410
PATCH = np.random.default_rng(20260906).integers(0, 256, (17, 40), np.uint8)


def _frame(width=24, height=110, cx=CX, bottom=BOTTOM, blank=False):
    frame = np.zeros((H, W, 3), np.uint8)
    if not blank:
        sx, sy = width / BOX[2], height / BOX[3]
        patch = cv2.resize(PATCH, (round(40 * sx), round(17 * sy)),
                           interpolation=cv2.INTER_LINEAR)
        x = round(cx - patch.shape[1] * .5)
        y = round(bottom - 12 * patch.shape[0] / 17)
        frame[y:y + patch.shape[0], x:x + patch.shape[1]] = patch[:, :, None]
    return frame


def _reader(monkeypatch):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    monkeypatch.setenv("ORION_METER_TRACK_CONTINUITY", "1")
    reader = SimpleMeterReader(W, H, require_gameplay_eligibility=False)
    reader._seed_track_template(_frame(), BOX)
    reader._det_tmpl_ts = 1000.
    reader._det_track_box = BOX
    reader._det_new_accept = True
    return reader


def _anchors(box):
    return box[0] + box[2] * .5, box[1] + box[3]


@pytest.mark.parametrize("width,height", [(30, 132), (18, 88), (30, 88), (24, 143)])
@pytest.mark.parametrize("dx,dy", [(6, -4), (-6, 4)])
def test_first_zoomed_frame_matches_before_reseeding_delayed_geometry(
        monkeypatch, width, height, dx, dy):
    reader = _reader(monkeypatch)
    # Geometry arrives from an older source frame; the meter has also moved.
    held = (CX - width // 2, BOTTOM - height, width, height)
    now = 1000.02
    out = reader._det_track_step(
        _frame(width, height, CX + dx, BOTTOM + dy), held, now)
    assert reader._det_track_score >= reader._det_track_min, \
        "camera zoom must localize current pixels before learning a stale-box self-match"
    assert _anchors(out) == pytest.approx((CX + dx, BOTTOM + dy), abs=1.5)
    assert out[2:] == (width, height), "detector still owns box dimensions"
    assert reader._det_tmpl_pos == out[:2], "refresh must use verified localized position"


@pytest.mark.parametrize("width,height", [(30, 132), (18, 88), (30, 88), (24, 143)])
def test_detector_size_breathing_keeps_original_pixel_template_candidate(
        monkeypatch, width, height):
    reader = _reader(monkeypatch)
    held = (CX - width // 2, BOTTOM - height, width, height)
    out = reader._det_track_step(_frame(), held, 1000.02)
    assert reader._det_track_score >= .999
    assert _anchors(out) == (CX, BOTTOM)
    assert (reader._det_track_vx, reader._det_track_vy) == (0., 0.)


def test_rescale_without_visible_notch_does_not_create_pixel_evidence(monkeypatch):
    reader = _reader(monkeypatch)
    held = (597, 278, 30, 132)
    out = reader._det_track_step(_frame(blank=True), held, 1000.02)
    assert out == held
    assert reader._det_track_score < reader._det_track_min
    assert (reader._det_track_vx, reader._det_track_vy) == (0., 0.)
    assert reader._det_state == "idle", "tracking must not confer presence authority"


def test_zoom_duplicate_patch_still_obeys_innovation_guard(monkeypatch):
    reader = _reader(monkeypatch)
    reader._det_track_vy = 10.
    held = (597, 278, 30, 132)
    out = reader._det_track_step(_frame(30, 132, CX, BOTTOM + 34), held, 1000.02)
    assert out == held
    assert reader._det_track_score < reader._det_track_min
    assert reader._det_track_last_match is not None  # ordinary rescale bootstrap, not candidate
    assert (reader._det_track_vx, reader._det_track_vy) == (0., 10.)


@pytest.mark.parametrize("width,height,expected", [
    (24, 110, 1), (26, 120, 2), (30, 132, 2), (18, 88, 2), (48, 220, 1)])
def test_only_detector_supported_bounded_rescale_adds_one_candidate(
        monkeypatch, width, height, expected):
    reader = _reader(monkeypatch)
    candidates = reader._det_track_template_candidates(width, height)
    assert len(candidates) == expected
    assert candidates[0][0] is reader._det_tmpl


def test_disabled_continuity_does_not_introduce_scale_search(monkeypatch):
    reader = _reader(monkeypatch)
    reader._det_track_cont = False
    candidates = reader._det_track_template_candidates(30, 132)
    assert len(candidates) == 1
    assert candidates[0][0] is reader._det_tmpl


@pytest.mark.parametrize("reset", ["_det_reset_lock_state", "reset_tracking"])
def test_scale_reference_dies_with_tracking_reset(monkeypatch, reset):
    reader = _reader(monkeypatch)
    getattr(reader, reset)()
    assert reader._det_tmpl is None
    assert getattr(reader, "_det_tmpl_size", None) is None
