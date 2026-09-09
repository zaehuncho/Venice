"""Current-pixel camera tracking must search all admissible appearance evidence.

Deterministic, real OpenCV matching: no model, timing prediction or fabricated
meter presence. Geometry is a proposal; only current pixels earn a relocation.
"""
import cv2
import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader


W, H = 1280, 720
BOX = (600, 300, 24, 110)
CX, BOTTOM = 612, 410
PATCH = np.random.default_rng(20260906).integers(0, 256, (17, 40), np.uint8)


def _place(frame, patch, cx=CX, bottom=BOTTOM):
    x = round(cx - patch.shape[1] * .5)
    y = round(bottom - 12 * patch.shape[0] / 17)
    frame[y:y + patch.shape[0], x:x + patch.shape[1]] = patch[:, :, None]


def _frame(width=24, height=110, dx=0, dy=0, blank=False):
    frame = np.zeros((H, W, 3), np.uint8)
    if not blank:
        patch = cv2.resize(PATCH, (round(40 * width / 24), round(17 * height / 110)),
                           interpolation=cv2.INTER_LINEAR)
        _place(frame, patch, CX + dx, BOTTOM + dy)
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


@pytest.mark.parametrize("width,height", [(26, 120), (22, 100), (26, 110), (24, 100)])
@pytest.mark.parametrize("dx,dy", [(6, -4), (-6, 4)])
def test_gradual_camera_scale_localizes_before_denominator_relatch(
        monkeypatch, width, height, dx, dy):
    reader = _reader(monkeypatch)
    held = (CX - width // 2, BOTTOM - height, width, height)
    assert max(abs(width / 24 - 1), abs(height / 110 - 1)) < reader._fill_denom_relatch
    out = reader._det_track_step(_frame(width, height, dx, dy), held, 1000.02)
    assert reader._det_track_score >= .99, "small camera zoom lost current notch pixels"
    # Odd resized patches live on a half-pixel centre. Judge against the
    # actual rasterized anchors, not the pre-rounding rendering request.
    pw, ph = round(40 * width / 24), round(17 * height / 110)
    pixel_cx = round(CX + dx - pw * .5) + pw * .5
    pixel_bottom = round(BOTTOM + dy - 12 * ph / 17) + 12 * ph / 17
    assert _anchors(out) == pytest.approx((pixel_cx, pixel_bottom), abs=1.5)
    assert out[2:] == (width, height)
    assert reader._det_state == "idle", "position matching cannot acquire presence"


@pytest.mark.parametrize("width,height", [(26, 120), (22, 100), (26, 110), (24, 100)])
def test_small_detector_breathing_keeps_exact_original_candidate(monkeypatch, width, height):
    reader = _reader(monkeypatch)
    held = (CX - width // 2, BOTTOM - height, width, height)
    out = reader._det_track_step(_frame(), held, 1000.02)
    assert reader._det_track_score >= .999
    assert _anchors(out) == (CX, BOTTOM)
    assert (reader._det_track_vx, reader._det_track_vy) == (0., 0.)


@pytest.mark.parametrize("axis,direction", [(0, 1), (0, -1), (1, 1), (1, -1)])
@pytest.mark.parametrize("occluded", [False, True])
def test_inadmissible_duplicate_cannot_hide_visible_in_gate_notch(
        monkeypatch, axis, direction, occluded):
    reader = _reader(monkeypatch)
    reader._det_new_accept = False
    # A plausible slight illumination change, or a narrow foreground occluder,
    # makes the genuine notch score just below an exact off-path duplicate.
    visible = np.clip(PATCH.astype(float) + np.random.default_rng(39).normal(
        0, 4, PATCH.shape), 0, 255).astype(np.uint8)
    if occluded:
        visible[:, 17:23] = 0
    actual = [CX, BOTTOM]
    actual[1 - axis] += 3
    far = [CX, BOTTOM]
    far[axis] += 34 * direction
    frame = np.zeros((H, W, 3), np.uint8)
    _place(frame, visible, *actual)
    _place(frame, PATCH, *far)
    reader._det_track_vx = 10. * direction if axis == 0 else 0.
    reader._det_track_vy = 10. * direction if axis == 1 else 0.
    out = reader._det_track_step(frame, BOX, 1000.02)
    assert reader._det_track_score >= reader._det_track_min
    assert _anchors(out) == pytest.approx(actual, abs=.5), \
        "out-of-gate global argmax hid admissible current-frame evidence"
    assert reader._det_state == "idle"


@pytest.mark.parametrize("axis,direction", [(0, 1), (0, -1), (1, 1), (1, -1)])
def test_inadmissible_duplicate_alone_remains_rejected(monkeypatch, axis, direction):
    reader = _reader(monkeypatch)
    reader._det_new_accept = False
    far = [CX, BOTTOM]
    far[axis] += 34 * direction
    frame = np.zeros((H, W, 3), np.uint8)
    _place(frame, PATCH, *far)
    reader._det_track_vx = 10. * direction if axis == 0 else 0.
    reader._det_track_vy = 10. * direction if axis == 1 else 0.
    velocity = (reader._det_track_vx, reader._det_track_vy)
    assert reader._det_track_step(frame, BOX, 1000.02) == BOX
    assert reader._det_track_score < reader._det_track_min
    assert (reader._det_track_vx, reader._det_track_vy) == velocity
    assert reader._det_track_last_match is None


def test_small_scale_blank_frame_never_becomes_match_evidence(monkeypatch):
    reader = _reader(monkeypatch)
    template = reader._det_tmpl.copy()
    held = (599, 290, 26, 120)
    assert reader._det_track_step(_frame(blank=True), held, 1000.02) == held
    assert reader._det_track_score == 0.
    assert np.array_equal(reader._det_tmpl, template)
    assert reader._det_tmpl_ts == 1000.


def test_subpixel_no_op_scale_does_not_add_redundant_match(monkeypatch):
    reader = _reader(monkeypatch)
    assert len(reader._det_track_template_candidates(24, 111)) == 1
