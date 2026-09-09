"""Preserve usable NCC appearance through an unverified occlusion update.

Real cv2 matching, scripted async locator cadence, deterministic pixel fixtures.
The patches prove the branch behavior, not real-game outcome rates or pixel truth.
"""
import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader

W, H = 1280, 720
BOX = (600, 300, 24, 110)
STEP = 1 / 60


class _Cfg:
    meter_style = 'Arrow2'
    meter_color = 'White'
    confidence_threshold = .32


class _Locator:
    provider = 'occlusion-regression-fixture'
    infer_ms = 1.0
    result = (False, None, 0., -1.)

    def submit(self, frame, ts): pass
    def submit_priority(self, frame, ts): return True
    def latest(self): return self.result


def _frame(x=600, bottom=410, seed=42):
    frame = np.zeros((H, W, 3), np.uint8)
    frame[bottom-55:bottom, x:x+24] = 255
    patch = np.random.default_rng(seed).integers(0, 256, (17, 8), np.uint8)
    frame[bottom-12:bottom+5, x-8:x] = patch[:, :, None]
    frame[bottom-12:bottom+5, x+24:x+32] = patch[:, :, None]
    return frame


def _reader(monkeypatch):
    monkeypatch.setenv('ORION_METER_DETECTOR', '0')
    monkeypatch.setenv('ORION_METER_LIFECYCLE', '1')
    monkeypatch.setenv('ORION_METER_TRACK_CONTINUITY', '1')
    r = SimpleMeterReader(W, H, cfg=_Cfg(), require_gameplay_eligibility=False)
    r._meter_detector = _Locator()
    r.set_shot_state(True, 1., True)
    r._meter_detector.result = (True, BOX, .9, 1000.)
    result = r.detect(_frame(), ts=1000.)
    assert result.detected and r._det_state == 'locked'
    assert r._det_tmpl_std > r._det_tmpl_std_min
    return r


@pytest.mark.parametrize('background', [0, 16, 32])
def test_new_detector_result_during_full_occlusion_preserves_good_appearance(monkeypatch, background):
    r = _reader(monkeypatch)
    template = r._det_tmpl.copy()
    seed_ts = r._det_tmpl_ts
    blank = np.full((H, W, 3), background, np.uint8)
    ts = 1000. + STEP
    r._meter_detector.result = (True, BOX, .9, ts)
    result = r.detect(blank, ts=ts)
    assert result.fill_pct == 0., 'retained geometry is not observed fill'
    assert r._det_track_score < r._det_track_min
    assert r._det_track_last_match is None
    assert np.array_equal(r._det_tmpl, template), 'async presence must not teach occluder pixels'
    assert r._det_tmpl_ts == seed_ts, 'weak results must not recursively extend template age'


@pytest.mark.parametrize('duration',[3,5])
def test_repeated_weak_geometry_updates_do_not_extend_or_erase_last_good_template(monkeypatch,duration):
    r=_reader(monkeypatch)
    template=r._det_tmpl.copy()
    seed_ts=r._det_tmpl_ts
    # Isolate tracker appearance from the independent zero-fill lock-retirement
    # policy. Public no-meter/reset behavior is tested separately below.
    r._det_new_accept=True
    for i in range(1,duration+1):
        out=r._det_track_step(np.zeros((H,W,3),np.uint8),BOX,1000.+i*STEP)
        assert out == BOX and r._det_track_score < r._det_track_min
        assert r._det_track_last_match is None
        assert np.array_equal(r._det_tmpl,template)
        assert r._det_tmpl_ts == seed_ts


@pytest.mark.parametrize('shift', [(6, 4), (-6, -4), (0, 6)])
def test_first_visible_frame_relocks_after_new_result_on_occlusion(monkeypatch, shift):
    r = _reader(monkeypatch)
    r._meter_detector.result = (True, BOX, .9, 1000.+STEP)
    result = r.detect(np.zeros((H, W, 3), np.uint8), ts=1000.+STEP)
    assert result.fill_pct == 0.
    result = r.detect(_frame(600+shift[0], 410+shift[1]), ts=1000.+2*STEP)
    assert result.detected and result.fill_pct > 0.
    assert r._det_track_score >= r._det_track_min
    x,y,w,h = result.bbox
    assert abs((x+w/2)-(612+shift[0])) <= 1.5
    assert abs((y+h)-(410+shift[1])) <= 1.5


def test_notch_occlusion_clipped_match_does_not_poison_appearance_or_motion(monkeypatch):
    r = _reader(monkeypatch)
    template = r._det_tmpl.copy()
    offset = (r._det_tmpl_cx_offset, r._det_tmpl_bottom_offset)
    seed_ts = r._det_tmpl_ts
    occluded = _frame()
    occluded[398:415,580:650] = 0
    r._meter_detector.result = (True, BOX, .9, 1000.+STEP)
    r.detect(occluded, ts=1000.+STEP)
    # This fixture's remaining white edge gives a strong but out-of-envelope
    # NCC proposal. It is clipped, not a verified position for appearance learning.
    assert r._det_track_score >= r._det_track_min
    assert r._det_track_last_match is None
    assert np.array_equal(r._det_tmpl, template)
    assert r._det_tmpl_ts == seed_ts
    assert (r._det_tmpl_cx_offset, r._det_tmpl_bottom_offset) == offset
    assert (r._det_track_vx, r._det_track_vy) == (0., 0.)
    result = r.detect(_frame(606,414), ts=1000.+2*STEP)
    assert r._det_track_score >= r._det_track_min
    assert result.bbox == (605,303,24,110)


def test_persistent_appearance_change_rebootstraps_at_existing_expiry(monkeypatch):
    r = _reader(monkeypatch)
    # New all-textured notch replaces the previous white-border appearance.
    # Direct step isolates appearance expiry from the independent detector lease.
    replacement = np.zeros((H,W,3),np.uint8)
    replacement[360:450,550:680] = np.random.default_rng(123).integers(
        0,256,(90,130,3),np.uint8)
    old = r._det_tmpl.copy()
    r._det_new_accept = True
    r._det_track_step(replacement, BOX, 1000.+.1)
    assert r._det_track_score < r._det_track_min
    assert np.array_equal(r._det_tmpl, old)
    r._det_track_step(replacement, BOX, 1000.+r._det_tmpl_max_s+.01)
    assert not np.array_equal(r._det_tmpl, old)
    assert r._det_tmpl_std >= r._det_tmpl_std_min
    r._det_new_accept = False
    r._det_track_step(replacement, BOX, 1000.+r._det_tmpl_max_s+.03)
    assert r._det_track_score >= r._det_track_min


def test_low_texture_bootstrap_can_still_refresh_on_new_detector_result(monkeypatch):
    r = _reader(monkeypatch)
    blank = np.zeros((H,W,3), np.uint8)
    r._seed_track_template(blank, BOX)
    r._det_tmpl_ts = 1000.
    assert r._det_tmpl_std < r._det_tmpl_std_min
    r._det_new_accept = True
    r._det_track_step(_frame(), BOX, 1000.+STEP)
    assert r._det_tmpl_std > r._det_tmpl_std_min
    assert r._det_tmpl_ts == 1000.+STEP


def test_fresh_detector_rescale_rebootstraps_without_waiting_for_expiry(monkeypatch):
    r = _reader(monkeypatch)
    old = r._det_tmpl.copy()
    replacement = np.zeros((H,W,3),np.uint8)
    replacement[360:450,550:680] = np.random.default_rng(123).integers(
        0,256,(90,130,3),np.uint8)
    held = (600,260,24,150)
    r._det_new_accept = True
    r._det_track_step(replacement,held,1000.+STEP)
    assert r._det_track_score < r._det_track_min
    assert r._det_tmpl_ts == 1000.+STEP
    assert not np.array_equal(r._det_tmpl,old)
    r._det_new_accept = False
    r._det_track_step(replacement,held,1000.+2*STEP)
    assert r._det_track_score >= r._det_track_min


@pytest.mark.parametrize('held',[(600,290,24,120),(599,300,27,110)])
def test_small_detector_size_breathing_keeps_occlusion_template(monkeypatch,held):
    r = _reader(monkeypatch)
    old = r._det_tmpl.copy()
    seed_ts = r._det_tmpl_ts
    r._det_new_accept = True
    r._det_track_step(np.zeros((H,W,3),np.uint8),held,1000.+STEP)
    assert r._det_track_score < r._det_track_min
    assert r._det_tmpl_ts == seed_ts
    assert np.array_equal(r._det_tmpl,old)


def test_persistently_clipped_match_still_obeys_template_expiry(monkeypatch):
    r = _reader(monkeypatch)
    before = r._det_tmpl.copy()
    r._det_tmpl_ts = 1000. - r._det_tmpl_max_s - .01
    occluded = _frame()
    occluded[398:415,580:650] = 0
    r._det_new_accept = True
    r._det_track_step(occluded, BOX, 1000. + STEP)
    assert r._det_track_score >= r._det_track_min
    assert r._det_tmpl_ts == 1000. + STEP
    assert not np.array_equal(r._det_tmpl, before)


def test_verified_strong_new_result_still_refreshes_at_detector_cadence(monkeypatch):
    r = _reader(monkeypatch)
    before = r._det_tmpl.copy()
    # A small lighting change preserves a real match but requires new pixel bytes.
    frame = np.maximum(_frame().astype(np.int16)-3,0).astype(np.uint8)
    ts=1000.+STEP
    r._meter_detector.result = (True,BOX,.9,ts)
    r.detect(frame,ts=ts)
    assert r._det_track_score >= r._det_track_min
    assert r._det_tmpl_ts == ts
    assert not np.array_equal(r._det_tmpl,before)


@pytest.mark.parametrize('reset', ['_det_reset_lock_state','reset_tracking'])
def test_preserved_template_dies_with_lock_reset(monkeypatch,reset):
    r = _reader(monkeypatch)
    r._meter_detector.result = (True,BOX,.9,1000.+STEP)
    r.detect(np.zeros((H,W,3),np.uint8),ts=1000.+STEP)
    getattr(r,reset)()
    assert r._det_tmpl is None
    assert r._det_track_last_match is None
    assert (r._det_track_vx,r._det_track_vy)==(0.,0.)


def test_negative_source_drops_lock_and_new_epoch_does_not_inherit_proof(monkeypatch):
    r = _reader(monkeypatch)
    # Start from a proven prior epoch; motion/template behavior must not bypass
    # the existing proof reset when a distinct physical shot subsequently arms.
    r.notify_physical_shot_start(7)
    r._latch_gameplay_structure_proof(7)
    r._meter_detector.result=(True,BOX,.9,1000.+STEP)
    r.detect(np.zeros((H,W,3),np.uint8),ts=1000.+STEP)
    for i in range(2,5):
        ts=1000.+i*STEP
        r._meter_detector.result=(False,None,0.,ts)
        r.detect(np.zeros((H,W,3),np.uint8),ts=ts)
    assert r._det_state != 'locked' and r._det_tmpl is None
    r.notify_physical_shot_start(8)
    assert not r.gameplay_structure_verified
    r._meter_detector.result=(True,BOX,.9,1000.+.1)
    result=r.detect(_frame(),ts=1000.+.1)
    assert r._det_tmpl is not None
    assert result.gameplay_sample_epoch == 8
    assert not result.gameplay_structure_verified or result.gameplay_structure_epoch == 8
