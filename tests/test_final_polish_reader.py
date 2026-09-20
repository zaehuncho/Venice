"""Exact-semantic reuse of the tiny Theil-Sen history on repeated source slots."""
from collections import deque

import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader


def _reader(monkeypatch):
    monkeypatch.setenv('ORION_METER_DETECTOR', '0')
    r = SimpleMeterReader(1280, 720)
    r._det_box_hist.extend((1000+i*.05, 600-i*39, 410+i*3, 24, 110) for i in range(6))
    return r


def _uncached_reference(r):
    """The original arithmetic, including cadence/early-speed trust policy."""
    try:
        if len(r._det_box_hist) < 3:
            return 0., 0., 0., False
        h = list(r._det_box_hist)
        span = float(h[-1][0])-float(h[0][0])
        if span < .05:
            return 0., 0., 0., False
        trusted = len(h) >= 4 and span >= float(r._det_vel_min_span_s)
        px, py = [], []
        for i, a in enumerate(h[:-1]):
            for b in h[i+1:]:
                dt = float(b[0])-float(a[0])
                if dt >= .01:
                    px.append((b[1]-a[1])/dt)
                    py.append((b[2]-a[2])/dt)
        if not px:
            return 0., 0., 0., False
        vx, vy = float(np.median(px)), float(np.median(py))
        speed = float(np.hypot(vx, vy))
        if not trusted and speed > float(r._det_early_v_max):
            sc = float(r._det_early_v_max)/max(1e-6, speed)
            vx *= sc; vy *= sc; speed = float(r._det_early_v_max)
        return float(vx), float(vy), float(speed), trusted
    except Exception:
        return 0., 0., 0., False


def test_repeated_history_reuses_medians_without_redoing_numpy_work(monkeypatch):
    r = _reader(monkeypatch)
    median = np.median
    calls = []
    monkeypatch.setattr(np, 'median', lambda a: calls.append(tuple(a)) or median(a))
    first = r._det_hist_vel()
    assert r._det_hist_speed() == first[2]
    assert r._det_hist_vel() == first
    assert len(calls) == 2, 'one x/y fit serves all three same-history consumers'


@pytest.mark.parametrize('mutation', ['append', 'middle', 'clear', 'trust', 'speed'])
def test_every_history_or_policy_change_recomputes(monkeypatch, mutation):
    r = _reader(monkeypatch)
    r._det_hist_vel()
    if mutation == 'append':
        r._det_box_hist.append((1000.3, 360., 442., 26., 112.))
    elif mutation == 'middle':
        r._det_box_hist[2] = (1000.1, 120., 700., 24., 110.)
    elif mutation == 'clear':
        r._det_box_hist.clear()
    elif mutation == 'trust':
        r._det_vel_min_span_s = .8
    else:
        r._det_vel_min_span_s = .8
        r._det_hist_vel()
        r._det_early_v_max = 37.
    assert r._det_hist_vel() == _uncached_reference(r)


def test_lock_reset_retires_cached_history(monkeypatch):
    r = _reader(monkeypatch)
    r._det_hist_vel()
    assert getattr(r, '_det_hist_velocity_cache', None) is not None
    r._det_reset_lock_state()
    assert r._det_hist_velocity_cache is None
    assert r._det_hist_vel() == (0., 0., 0., False)


def test_mutable_diagnostic_rows_cannot_modify_cached_key_in_place(monkeypatch):
    r = _reader(monkeypatch)
    r._det_box_hist = deque(list(row) for row in r._det_box_hist)
    first = r._det_hist_vel()
    for row in r._det_box_hist:
        row[1] *= 2
    expected = _uncached_reference(r)
    assert expected != first
    assert r._det_hist_vel() == expected


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf'), 1e308])
def test_nonfinite_and_overflow_history_keeps_original_numeric_semantics(monkeypatch, value):
    r = _reader(monkeypatch)
    r._det_box_hist[2] = (1000.1, value, value, 24., 110.)
    with np.errstate(all='ignore'):
        expected = _uncached_reference(r)
        for _ in range(2):
            got = r._det_hist_vel()
            np.testing.assert_array_equal(got[:3], expected[:3])
            assert got[3] == expected[3]


@pytest.mark.parametrize('seed', range(12))
def test_irregular_duplicate_backward_and_outlier_histories_are_exact(monkeypatch, seed):
    r = _reader(monkeypatch)
    rng = np.random.default_rng(seed)
    for _ in range(50):
        times = 1000 + np.cumsum(rng.choice([-.02, 0., .001, .009, .02, .05], size=6))
        r._det_box_hist = deque((float(t), float(x), float(y), 24., 110.)
            for t, x, y in zip(times, rng.normal(600,80,6), rng.normal(410,50,6)))
        expected = _uncached_reference(r)
        assert r._det_hist_vel() == expected
        assert r._det_hist_vel() == expected
