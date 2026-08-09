"""FillKalman irregular-sampling extensions for the compressed path:
per-update measurement variance r (quality-adaptive R) and predict_to (query the state
extrapolated to NOW when the last valid sample is stale-gated/skipped frames old)."""
import numpy as np
import pytest

from fill_kalman import FillKalman


def _feed_rise(kf, n=20, vel=22.6, dt=1 / 60.0, noise=0.0, r=None, seed=7):
    rng = np.random.default_rng(seed)
    t = 0.0
    for k in range(n):
        fill = 10.0 + vel * t + (rng.normal(0, noise) if noise else 0.0)
        kf.update(fill, t, r=r) if r is not None else kf.update(fill, t)
        t += dt
    return t - dt


def test_default_r_unchanged_without_param():
    """Existing two-arg callers must see identical behavior."""
    a, b = FillKalman(), FillKalman()
    t = 0.0
    for k in range(15):
        fill = 10.0 + 22.6 * t
        a.update(fill, t)
        b.update(fill, t, r=None)
        t += 1 / 60.0
    assert a.fill() == b.fill() and a.velocity() == b.velocity()


def test_small_r_trusts_measurement_more():
    """A jump in the measurement moves the low-R filter further than the high-R filter."""
    lo, hi = FillKalman(), FillKalman()
    _feed_rise(lo, r=0.16)
    t = _feed_rise(hi, r=25.0)
    jump_t = t + 1 / 60.0
    target = 10.0 + 22.6 * jump_t + 8.0            # +8pp measurement shock
    lo.update(target, jump_t, r=0.16)
    hi.update(target, jump_t, r=25.0)
    base = 10.0 + 22.6 * jump_t
    assert lo.fill() - base > hi.fill() - base     # low-R chased the shock harder


def test_predict_to_extrapolates_without_mutation():
    kf = FillKalman()
    t_last = _feed_rise(kf, n=30)
    fill_now, vel_now = kf.fill(), kf.velocity()
    pred = kf.predict_to(t_last + 0.05)            # 50ms of stale-gated frames later
    assert pred is not None
    p_fill, p_vel = pred
    assert p_fill == pytest.approx(fill_now + vel_now * 0.05, abs=1.0)
    assert kf.fill() == fill_now                   # state untouched
    assert kf.predict_to(t_last) == (fill_now, vel_now)   # non-future query = current state


def test_predict_to_clamps_to_max_dt():
    kf = FillKalman(max_dt_s=0.15)
    t_last = _feed_rise(kf, n=30)
    near = kf.predict_to(t_last + 0.15)[0]
    far = kf.predict_to(t_last + 5.0)[0]           # absurd gap: clamped, not runaway
    assert far == pytest.approx(near, abs=1e-9)


def test_predict_to_none_before_ready():
    kf = FillKalman()
    assert kf.predict_to(1.0) is None
    kf.update(10.0, 0.0)
    assert kf.predict_to(1.0) is None              # n<3: not ready
