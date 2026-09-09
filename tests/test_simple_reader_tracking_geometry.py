"""NCC position continuity is centre/bottom-anchored when detector size changes.

The random, deterministic notch makes localization unambiguous and exercises real
cv2.matchTemplate. Detector width/height are intentionally independent of the
unchanged notch, as learned box boundaries can breathe without physical motion.
"""
import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader


W, H = 1280, 720
BOX = (600, 300, 24, 110)


def _anchors(box):
    x, y, w, h = box
    return x + w * 0.5, y + h


def _frame(cx=612, bottom=410):
    frame = np.zeros((H, W, 3), dtype=np.uint8)
    patch = np.random.default_rng(20260905).integers(
        0, 256, (17, 40), dtype=np.uint8)
    frame[bottom - 12:bottom + 5, cx - 20:cx + 20] = patch[:, :, None]
    return frame


def _reader(monkeypatch):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    monkeypatch.setenv("ORION_METER_TRACK_CONTINUITY", "1")
    r = SimpleMeterReader(W, H, require_gameplay_eligibility=False)
    r._seed_track_template(_frame(), BOX)
    assert r._det_tmpl_std > r._det_tmpl_std_min
    r._det_track_box = BOX
    r._det_tmpl_ts = 1000.0
    r._det_fresh_accept = False
    r._det_new_accept = False
    return r


@pytest.mark.parametrize("width,height", [(24, 112), (24, 108), (28, 110),
                                           (20, 110), (28, 134), (20, 86)])
def test_size_change_does_not_create_notch_velocity(monkeypatch, width, height):
    r = _reader(monkeypatch)
    held = (612 - width // 2, 410 - height, width, height)
    out = r._det_track_step(_frame(), held, 1000.02)
    assert r._det_track_score >= r._det_track_min, "size change lost a stationary notch"
    assert _anchors(out) == (612, 410)
    assert r._det_track_vx == pytest.approx(0.0), "width change created x velocity"
    assert r._det_track_vy == pytest.approx(0.0), "height change created y velocity"


def test_diagonal_motion_velocity_excludes_size_change(monkeypatch):
    r = _reader(monkeypatch)
    # Same physical +6 X/-4 Y move as the old geometry, plus width and height
    # breathing. The NCC velocity is 0.4 times actual motion from a cold speed.
    # The delayed detector is dimensionally fresh but positionally one frame old.
    held = (598, 298, 28, 112)
    out = r._det_track_step(_frame(618, 406), held, 1000.02)
    assert r._det_track_score >= r._det_track_min
    assert _anchors(out) == pytest.approx((617, 407), abs=0.5)
    assert r._det_track_vx == pytest.approx(2.4)
    assert r._det_track_vy == pytest.approx(-1.6)


def test_constant_size_stationary_match_remains_exact(monkeypatch):
    r = _reader(monkeypatch)
    out = r._det_track_step(_frame(), BOX, 1000.02)
    assert out == BOX
    assert r._det_track_score >= r._det_track_min
    assert (r._det_track_vx, r._det_track_vy) == (0.0, 0.0)


def test_missing_notch_keeps_detector_geometry_without_inventing_motion(monkeypatch):
    r = _reader(monkeypatch)
    held = (598, 298, 28, 112)
    blank = np.zeros((H, W, 3), dtype=np.uint8)
    out = r._det_track_step(blank, held, 1000.02)
    assert out == held
    assert r._det_track_score < r._det_track_min
    assert (r._det_track_vx, r._det_track_vy) == (0.0, 0.0)


def test_teleport_still_reseats_to_detector(monkeypatch):
    r = _reader(monkeypatch)
    held = (900, 300, 24, 110)
    out = r._det_track_step(_frame(912, 410), held, 1000.02)
    assert out == held
    assert _anchors(out) == (912, 410)


def test_continuity_disabled_keeps_primitive_route(monkeypatch):
    r = _reader(monkeypatch)
    r._det_track_cont = False
    seen = []
    held = (598, 298, 28, 112)
    r._track_box_ncc = lambda frame, box: seen.append(box) or box
    assert r._det_track_step(_frame(), held, 1000.02) == held
    assert seen == [held]


@pytest.mark.parametrize("width", [21, 23, 25, 27])
@pytest.mark.parametrize("refresh", [False, True])
def test_odd_width_stationary_notch_never_invents_persistent_velocity(
        monkeypatch, width, refresh):
    r = _reader(monkeypatch)
    held = (612 - width // 2, 300, width, 110)
    for i in range(20):
        r._det_fresh_accept = refresh and i % 3 == 0
        r._det_new_accept = r._det_fresh_accept
        out = r._det_track_step(_frame(), held, 1000.02 + i * 0.02)
        assert r._det_track_score >= r._det_track_min
        assert _anchors(out) == pytest.approx((612, 410), abs=0.5)
        assert r._det_track_vx == pytest.approx(0.0, abs=1e-9)
        assert r._det_track_vy == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("width", [21, 23, 25, 27])
@pytest.mark.parametrize("velocity", [-1, 1])
def test_odd_width_one_pixel_motion_is_not_zero_two_pixel_quantized(
        monkeypatch, width, velocity):
    r = _reader(monkeypatch)
    r._det_track_vx = float(velocity)
    for i in range(1, 21):
        cx = 612 + i * velocity
        held = (cx - width // 2, 300, width, 110)
        r._det_fresh_accept = i % 3 == 0
        r._det_new_accept = r._det_fresh_accept
        out = r._det_track_step(_frame(cx), held, 1000.0 + i * 0.02)
        assert r._det_track_score >= r._det_track_min
        assert _anchors(out) == pytest.approx((cx, 410), abs=0.5)
        assert r._det_track_vx == pytest.approx(float(velocity), abs=1e-9)
        assert r._det_track_vy == pytest.approx(0.0, abs=1e-9)


def test_weak_then_reacquired_odd_width_does_not_join_across_gap(monkeypatch):
    r = _reader(monkeypatch)
    held = (602, 300, 21, 110)
    r._det_track_step(np.zeros((H, W, 3), np.uint8), held, 1000.02)
    assert r._det_track_last_match is None
    # The first accepted match re-establishes a derivative origin, not a
    # guessed one-frame displacement spanning the unobserved frame.
    for i in range(4):
        r._det_track_step(_frame(), held, 1000.04 + i * 0.02)
        assert r._det_track_score >= r._det_track_min
        assert r._det_track_vx == 0.0
        assert r._det_track_vy == 0.0


def test_low_texture_rejection_discards_candidate_motion_and_anchor(monkeypatch):
    r = _reader(monkeypatch)
    r._det_tmpl_std = 0.0  # independently reject even an exact raw NCC match
    out = r._det_track_step(_frame(618, 412), BOX, 1000.02)
    assert out == BOX
    assert r._det_track_score == 0.0
    assert (r._det_track_vx, r._det_track_vy) == (0.0, 0.0)
    assert r._det_track_last_match is None


def test_innovation_rejection_does_not_retain_duplicate_patch_anchor(monkeypatch):
    r = _reader(monkeypatch)
    r._det_track_vy = 10.0
    out = r._det_track_step(_frame(612, 444), BOX, 1000.02)
    assert out == BOX
    assert r._det_track_score == 0.0
    assert (r._det_track_vx, r._det_track_vy) == (0.0, 10.0)
    assert r._det_track_last_match is None


def test_deviation_clamp_drops_out_of_envelope_derivative_origin(monkeypatch):
    r = _reader(monkeypatch)
    out = r._det_track_step(_frame(622, 410), BOX, 1000.02)
    assert r._det_track_score >= r._det_track_min
    assert _anchors(out)[0] < 622
    assert r._det_track_last_match is None


@pytest.mark.parametrize("reset", ["_det_reset_lock_state", "reset_tracking"])
def test_lifecycle_reset_drops_template_derivative_origin(monkeypatch, reset):
    r = _reader(monkeypatch)
    r._det_track_step(_frame(616, 412), BOX, 1000.02)
    assert r._det_track_last_match is not None
    getattr(r, reset)()
    assert r._det_track_last_match is None
    assert (r._det_track_vx, r._det_track_vy) == (0.0, 0.0)


def test_detector_hard_snap_rebases_new_template_origin(monkeypatch):
    r = _reader(monkeypatch)
    r._det_track_vx = 9.0
    held = (900, 300, 24, 110)
    out = r._det_track_step(_frame(912, 410), held, 1000.02)
    assert out == held
    assert r._det_track_last_match == (912.0, 410.0)
    assert (r._det_track_vx, r._det_track_vy) == (0.0, 0.0)
