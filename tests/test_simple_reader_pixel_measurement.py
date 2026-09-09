"""Opt-in P2 pixel/display, guarded ruler and prediction invariants."""
import numpy as np
import pytest
from simple_meter_reader import SimpleMeterReader


def scene(dx=0, dy=0, rows=50, cap=True, jersey=False, height=100):
    frame = np.full((300, 400, 3), 40, np.uint8)
    base, tip = 250 + dy, 250 + dy - height
    frame[base - rows:base, 106 + dx:118 + dx] = 255
    if cap:
        frame[tip:tip + 4, 108 + dx:116 + dx] = (0, 200, 0)
    if jersey:
        frame[tip + 20:base + 10, 140 + dx:180 + dx] = 255
    return frame, (100 + dx, tip - 8, 24, height + 20)


def reader(monkeypatch, measured=True, ruler=False, kalman=False):
    for key, flag in [('ORION_METER_DETECTOR', False),
        ('ORION_METER_MEASUREMENT_BOX', measured), ('ORION_METER_PIXEL_RULER', ruler),
        ('ORION_METER_KALMAN_SEARCH', kalman), ('ORION_METER_SUBPIXEL_FILL', True)]:
        monkeypatch.setenv(key, str(int(flag)))
    return SimpleMeterReader(400, 300, require_gameplay_eligibility=False)


def test_default_off(monkeypatch):
    for key in ('ORION_METER_MEASUREMENT_BOX','ORION_METER_PIXEL_RULER','ORION_METER_KALMAN_SEARCH'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('ORION_METER_DETECTOR','0')
    r = SimpleMeterReader(400, 300)
    assert not (r._det_measure_raw or r._det_pixel_ruler or r._det_kalman_search)


def test_disabled_retains_legacy_identity(monkeypatch):
    r = reader(monkeypatch, measured=False)
    f,b = scene()
    assert r._det_measurement_boxes(f,b,1000.) == (b,b)
    f,b = scene(dx=20,dy=14)
    m,d = r._det_measurement_boxes(f,b,1000.02)
    assert m == d and d != b


def test_measurement_follows_cap_not_jersey_or_display(monkeypatch):
    r = reader(monkeypatch)
    f,b = scene()
    r._det_measurement_boxes(f,b,1000.)
    f,_ = scene(dx=20,dy=14,jersey=True)
    m,d = r._det_measurement_boxes(f,b,1000.02)
    g = r._det_pixel_geometry
    assert g is not None
    assert g['cx'] == pytest.approx(131.5,abs=.1)
    assert g['base'] == pytest.approx(263.5,abs=.1)
    assert m != d and d == b


def test_no_cap_no_brightness_snap(monkeypatch):
    r = reader(monkeypatch)
    f,b = scene(cap=False,jersey=True)
    m,_ = r._det_measurement_boxes(f,b,1000.)
    assert m == b and r._det_pixel_geometry is None


@pytest.mark.parametrize('dx,dy,dh',[(0,0,0),(8,6,5),(-7,-5,-4)])
def test_ruler_ignores_pan_and_detector_jitter(monkeypatch,dx,dy,dh):
    r = reader(monkeypatch,ruler=True)
    f,b = scene(dx=dx,dy=dy)
    b=(b[0]-3,b[1]-dh,b[2]+4,b[3]+dh)
    fill,g,t = r._measure_fill_in_box(f,b,ts=1000.)
    assert t >= 0 and fill == pytest.approx(50.,abs=.05)
    assert g[:2] == (96.,100.)
    assert r._dbg_subpx['ruler'] == 'pixel_bar'


def test_session_guard_rejects_span_outlier_without_learning_it(monkeypatch):
    r = reader(monkeypatch,ruler=True)
    f,b=scene()
    for i in range(3):
        assert r._measure_fill_in_box(f,b,1000.+i/60.)[2] >= 0
    old=list(r._det_pixel_span_hist)
    f,b=scene(height=115)
    assert r._measure_fill_in_box(f,b,1000.1) == (0.,None,-1)
    assert r._det_pixel_ruler_reject
    assert r._dbg_subpx['gate'] == 'pixel_ruler_outlier'
    assert list(r._det_pixel_span_hist) == old


def test_hidden_cap_never_reuses_geometry(monkeypatch):
    r=reader(monkeypatch,ruler=True)
    f,b=scene()
    assert r._measure_fill_in_box(f,b,1000.)[2] >= 0
    f,b=scene(cap=False)
    assert r._measure_fill_in_box(f,b,1000.02) == (0.,None,-1)
    assert r._det_pixel_ruler_reject and r._last_fill_estimator_generation == 0


def test_kalman_elapsed_prediction_and_expiry(monkeypatch):
    r=reader(monkeypatch,kalman=True)
    for i in range(10):
        r._det_kalman_observe((100+i*6,142+i*3,24,120),1000.+i/60.)
    origin=1000.+9/60.
    r._det_track_box_ts=origin
    r._det_track_sample_ts=origin+3/60.
    assert r._det_track_predicted_step(0.,0.) == pytest.approx((18.,9.),abs=.8)
    r._det_track_sample_ts=origin+1.
    assert r._det_track_predicted_step(0.,0.) == (0.,0.)


def test_kalman_ignores_height_breathing_and_repeated_timestamp(monkeypatch):
    r=reader(monkeypatch,kalman=True)
    for i,h in enumerate((120,115,122,118,120)):
        r._det_kalman_observe((100,262-h,24,h),1000.+i/60.)
    assert r._det_kalman_state['x'][3] == pytest.approx(0.,abs=1e-9)
    n=r._det_kalman_state['n']
    r._det_kalman_observe((100,142,24,120),1000.+4/60.)
    assert r._det_kalman_state['n'] == n
    r._det_reset_lock_state()
    assert r._det_kalman_state is None


def test_source_reset_clears_ruler_guard(monkeypatch):
    r=reader(monkeypatch,ruler=True)
    f,b=scene()
    r._measure_fill_in_box(f,b,1000.)
    assert r._det_pixel_span_hist
    r.reset_tracking()
    assert not r._det_pixel_span_hist


def test_pixel_ruler_never_promotes_existing_censor_or_recovery(monkeypatch):
    r=reader(monkeypatch,ruler=True)
    f,b=scene()
    for attr in ('_det_occ_censored_reject','_det_occ_recovered'):
        def legacy(frame,box,ts):
            r._det_occ_censored_reject=False
            r._det_occ_recovered=False
            setattr(r,attr,True)
            return 50.,None,58
        r._measure_fill_in_box_legacy=legacy
        assert r._measure_fill_in_box(f,b,1000.) == (0.,None,-1)
        assert r._det_pixel_ruler_reject
