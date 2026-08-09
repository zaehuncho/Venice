"""Motion-estimator math coverage for the meter detector.

Verifies the weighted-quadratic fit recovers velocity AND acceleration from the
fill trajectory (smoother than differencing consecutive linear slopes), and that
a noisy near-linear ramp does not produce a wild acceleration.
"""
from meter_detector import _MotionEstimator, DetectorConfig


def _feed(est, fn, n=8, dt=0.016):
    for i in range(n):
        t = i * dt
        v = fn(t)
        est.update(v, v, t, True)
    return (n - 1) * dt


def test_motion_estimator_recovers_acceleration_from_quadratic():
    est = _MotionEstimator(DetectorConfig())
    a0, v0 = 200.0, 10.0  # %/s^2, %/s
    t_last = _feed(est, lambda t: 0.5 * a0 * t * t + v0 * t)
    # WLS on an exact quadratic recovers the true coefficients.
    assert abs(est.accel_pct_s2 - a0) < 15.0, f"accel={est.accel_pct_s2}"
    assert abs(est.velocity_pct_s - (v0 + a0 * t_last)) < 8.0, f"vel={est.velocity_pct_s}"


def test_motion_estimator_linear_ramp_has_near_zero_acceleration():
    est = _MotionEstimator(DetectorConfig())
    _feed(est, lambda t: 120.0 * t)  # constant 120 %/s, no curvature
    assert abs(est.velocity_pct_s - 120.0) < 5.0
    assert abs(est.accel_pct_s2) < 30.0, f"accel should be ~0, got {est.accel_pct_s2}"
