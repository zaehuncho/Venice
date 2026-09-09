"""A retained notch template also retains its bounded reacquisition search origin.

Real OpenCV matching, scripted async results, deterministic pixels. These cases
pin current-frame position recovery, not recorded-game hit rates or pixel truth.
"""
import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader

W, H = 1280, 720
BOX = (600, 300, 24, 110)
STEP = 1 / 60


class _Cfg:
    meter_style = "Arrow2"
    meter_color = "White"
    confidence_threshold = .32


class _Locator:
    provider = "search-origin-regression-fixture"
    infer_ms = 1.
    result = (True, BOX, .9, 1000.)

    def submit(self, frame, ts): pass
    def submit_priority(self, frame, ts): return True
    def latest(self): return self.result


def _frame(x=600, bottom=410):
    pixels = np.zeros((H, W, 3), np.uint8)
    pixels[bottom-55:bottom, x:x+24] = 255
    patch = np.random.default_rng(42).integers(0, 256, (17, 8), np.uint8)
    pixels[bottom-12:bottom+5, x-8:x] = patch[:, :, None]
    pixels[bottom-12:bottom+5, x+24:x+32] = patch[:, :, None]
    return pixels


def _reader(monkeypatch, vx=13, vy=0):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    monkeypatch.setenv("ORION_METER_LIFECYCLE", "1")
    monkeypatch.setenv("ORION_METER_TRACK_CONTINUITY", "1")
    r = SimpleMeterReader(W, H, cfg=_Cfg(), require_gameplay_eligibility=False)
    r._meter_detector = _Locator()
    r.set_shot_state(True, 1., True)
    result = r.detect(_frame(), ts=1000.)
    assert result.detected and r._det_state == "locked"
    # The detector's six accepted prior source boxes represent slower motion
    # before camera acceleration. Its existing extrapolation remains enabled;
    # no gate, velocity bound, reconcile gain, or search radius is overridden.
    r._det_box_hist.clear()
    r._det_box_hist.extend((999.75+i*.05,
                           612+vx*20*(-.25+i*.05),
                           410+vy*20*(-.25+i*.05), 24, 110)
                          for i in range(6))
    assert r._det_hist_vel()[3]
    for i in (1, 2):
        result = r.detect(_frame(600+vx*i, 410+vy*i), ts=1000.+i*STEP)
        assert r._det_track_score >= r._det_track_min
        assert result.fill_pct > 0.
    return r


@pytest.mark.parametrize("vx,vy", [(13, 0), (-13, 0), (0, 13), (9, -9)])
def test_first_revealed_moving_frame_relocks_without_search_origin_snap(monkeypatch, vx, vy):
    r = _reader(monkeypatch, vx, vy)
    template = r._det_tmpl.copy()
    template_ts = r._det_tmpl_ts
    velocity = (r._det_track_vx, r._det_track_vy)
    old_source = r._meter_detector.result
    old_epoch = r._physical_shot_epoch
    hidden = r.detect(np.zeros((H, W, 3), np.uint8), ts=1000.+3*STEP)
    assert hidden.fill_pct == 0., "search state is not visible fill evidence"
    assert r._det_track_score < r._det_track_min
    assert r._det_track_last_match is None
    assert np.array_equal(r._det_tmpl, template)
    assert r._det_tmpl_ts == template_ts
    assert not r._det_new_accept, "repeated source cannot refresh appearance/authority"

    result = r.detect(_frame(600+4*vx, 410+4*vy), ts=1000.+4*STEP)
    assert r._det_track_score >= r._det_track_min, "first visible exact notch was lost"
    assert result.detected and result.fill_pct > 0.
    x, y, w, h = result.bbox
    assert abs(x+w*.5-(612+4*vx)) <= 4.
    assert abs(y+h-(410+4*vy)) <= 4.
    # Reacquisition starts a new derivative pair; the displacement across the
    # hidden frame must not be interpreted as a one-frame velocity impulse.
    assert (r._det_track_vx, r._det_track_vy) == velocity
    assert r._meter_detector.result == old_source
    assert r._physical_shot_epoch == old_epoch
    assert not r._det_new_accept and r._det_tmpl_ts == template_ts


def test_weak_frame_returns_held_box_not_retained_search_position(monkeypatch):
    r = _reader(monkeypatch)
    last_localized = r._det_track_box
    r._det_new_accept = False
    held = (613, 300, 24, 110)
    assert held != last_localized
    out = r._det_track_step(np.zeros((H, W, 3), np.uint8), held, 1000.+3*STEP)
    assert out == held
    assert r._det_track_box == last_localized
    assert r._det_track_score == 0. and r._det_track_last_match is None


def test_repeated_weak_frames_do_not_move_origin_or_extend_expiry(monkeypatch):
    r = _reader(monkeypatch)
    last_localized = r._det_track_box
    seed_ts = r._det_tmpl_ts
    r._det_new_accept = False
    blank = np.zeros((H, W, 3), np.uint8)
    for elapsed in (.1, .2, .3, .49):
        assert r._det_track_step(blank, BOX, 1000.+elapsed) == BOX
        assert r._det_track_box == last_localized
        assert r._det_tmpl_ts == seed_ts
    r._det_track_step(blank, BOX, 1000.+r._det_tmpl_max_s+.01)
    assert r._det_track_box == BOX
    assert r._det_tmpl_ts == 1000.+r._det_tmpl_max_s+.01
    assert r._det_tmpl_std < r._det_tmpl_std_min


def test_detector_dimensions_remain_authoritative_for_retained_origin(monkeypatch):
    r = _reader(monkeypatch)
    old = r._det_track_box
    held = (612, 297, 26, 113)  # same-scale detector size breathing
    r._det_new_accept = True
    out = r._det_track_step(np.zeros((H, W, 3), np.uint8), held, 1000.+3*STEP)
    assert out == held
    tx, ty, tw, th = r._det_track_box
    assert (tw, th) == (26, 113)
    assert tx+tw*.5 == old[0]+old[2]*.5
    assert ty+th == old[1]+old[3]


def test_genuine_rescale_reboots_search_origin_immediately(monkeypatch):
    r = _reader(monkeypatch)
    held = (600, 260, 24, 150)
    r._det_new_accept = True
    now = 1000.+3*STEP
    r._det_track_step(np.zeros((H, W, 3), np.uint8), held, now)
    assert r._det_track_box == held and r._det_tmpl_ts == now


def test_teleport_discards_retained_origin_before_matching(monkeypatch):
    r = _reader(monkeypatch)
    held = (900, 300, 24, 110)
    r._det_new_accept = True
    assert r._det_track_step(_frame(900), held, 1000.+3*STEP) == held
    assert r._det_track_box == held
    assert (r._det_track_vx, r._det_track_vy) == (0., 0.)


def test_low_texture_has_no_localized_origin_to_preserve(monkeypatch):
    r = _reader(monkeypatch)
    blank = np.zeros((H, W, 3), np.uint8)
    r._seed_track_template(blank, BOX)
    r._det_new_accept = False
    assert r._det_track_step(blank, BOX, 1000.+3*STEP) == BOX
    assert r._det_track_box == BOX


@pytest.mark.parametrize("reset", ["_det_reset_lock_state", "reset_tracking"])
def test_reset_clears_search_origin_and_preserved_appearance(monkeypatch, reset):
    r = _reader(monkeypatch)
    r.detect(np.zeros((H, W, 3), np.uint8), ts=1000.+3*STEP)
    getattr(r, reset)()
    assert r._det_track_box is None and r._det_tmpl is None
    assert r._det_track_last_match is None


def test_current_full_negative_retirement_still_clears_origin(monkeypatch):
    r = _reader(monkeypatch)
    blank = np.zeros((H, W, 3), np.uint8)
    r.detect(blank, ts=1000.+3*STEP)
    for i in range(4, 7):
        ts = 1000.+i*STEP
        r._meter_detector.result = (False, None, 0., ts)
        result = r.detect(blank, ts=ts)
        assert result.fill_pct == 0.
    assert r._det_state != "locked"
    assert r._det_track_box is None and r._det_tmpl is None


def test_continuity_disabled_keeps_existing_primitive_path(monkeypatch):
    r = _reader(monkeypatch)
    r._det_track_cont = False
    calls = []
    r._track_box_ncc = lambda frame, box: calls.append(box) or box
    assert r._det_track_step(_frame(), BOX, 1000.+3*STEP) == BOX
    assert calls == [BOX]
