"""Causal constant-acceleration Kalman filter over the meter FILL signal — the "bot math precision" lever.

Raw fill% (row-counted, ~1-2% quantized) gives NOISY frame-to-frame velocity, and velocity noise flows
straight into tip prediction -> release jitter. A constant-acceleration Kalman gives a noise-robust estimate
of [fill, velocity, acceleration] from the same measurements, and -- because the meter rise is close to
constant-velocity/gentle-accel -- lets us solve ANALYTICALLY for the sub-frame ms-to-target (when will fill
cross the green window), independent of the CNN forecaster. Runs alongside it as a second estimator so the two
can be graded live (kalmanTipMs vs predTipMs) before either drives the release.

Pure numpy, causal (past-only), no torch/GPU. Flag-gated by the caller; inert unless wired + enabled.
State x = [p, v, a] (p=fill%, v=%/s, a=%/s^2). Never raises: a bad update resets cleanly.
"""
from __future__ import annotations

import math
import os
from typing import Optional

import numpy as np


class FillKalman:
    """update(fill_pct, t_s) each frame; then velocity()/accel() or ms_to_target(target_pct)."""

    def __init__(self, q: float = 1.5e4, r: float = 3.0, max_dt_s: float = 0.15, bias_ms: float = 0.0) -> None:
        # bias_ms: added to ms_to_target. The const-accel model doesn't know the meter DECELERATES near the
        # top, so extrapolating from mid-fill it predicts the tip ~75ms EARLY (offline: median -75ms, IQR
        # 16ms). A calibrated +bias corrects the tight-but-biased estimate. Best accuracy is predicting from
        # HIGH fill (85-90%) where deceleration matters least.
        self._bias_ms = float(bias_ms)
        # q = process spectral density (%^2/s^3): how much the acceleration is allowed to wander. Higher =
        #     tracks a changing rise faster but noisier. r = fill measurement variance (%^2): ~ (1-2%)^2 for
        #     row-counted fill, lower for the sub-pixel reader. max_dt_s: a gap larger than this (dropout /
        #     new shot) re-seeds instead of integrating a huge step.
        self._q = float(q)
        self._r = float(r)
        self._max_dt = float(max_dt_s)
        self._x: Optional[np.ndarray] = None       # [p, v, a]
        self._P: Optional[np.ndarray] = None        # 3x3 covariance
        self._t: Optional[float] = None
        self._n = 0

    def reset(self) -> None:
        self._x = None
        self._P = None
        self._t = None
        self._n = 0

    def _seed(self, fill: float, t: float) -> None:
        self._x = np.array([fill, 0.0, 0.0], dtype=float)
        # generous initial uncertainty on v/a so the first real motion is trusted quickly
        self._P = np.diag([self._r, 1.0e5, 1.0e6])
        self._t = t
        self._n = 1

    def update(self, fill_pct: float, t_s: float, r: Optional[float] = None) -> None:
        """r: optional per-update measurement variance (%^2) -- the quality-adaptive reader
        scales it per frame (sharp frame = small r, smeared = large); None keeps the
        constructor default, so existing callers are unchanged."""
        try:
            fill = float(fill_pct)
            t = float(t_s)
            r_used = float(r) if (r is not None and float(r) > 0.0) else self._r
        except Exception:
            return
        if self._x is None or self._t is None:
            self._seed(fill, t)
            return
        dt = t - self._t
        if dt <= 0.0 or dt > self._max_dt:
            # out-of-order, duplicate, or a gap too large to integrate (dropout / shot boundary) -> re-seed.
            self._seed(fill, t)
            return
        self._t = t
        # --- predict (constant-acceleration transition) ---
        F = np.array([[1.0, dt, 0.5 * dt * dt],
                      [0.0, 1.0, dt],
                      [0.0, 0.0, 1.0]], dtype=float)
        # continuous white-noise-acceleration Q (standard discretization)
        dt2, dt3, dt4, dt5 = dt * dt, dt ** 3, dt ** 4, dt ** 5
        Q = self._q * np.array([[dt5 / 20.0, dt4 / 8.0, dt3 / 6.0],
                                [dt4 / 8.0,  dt3 / 3.0, dt2 / 2.0],
                                [dt3 / 6.0,  dt2 / 2.0, dt]], dtype=float)
        x = F @ self._x
        P = F @ self._P @ F.T + Q
        # --- update (measure position only) ---
        H = np.array([[1.0, 0.0, 0.0]], dtype=float)
        z = fill
        y = z - (H @ x)[0]
        S = (H @ P @ H.T)[0, 0] + r_used
        if S <= 0.0:
            self._x, self._P = x, P
            return
        K = (P @ H.T) / S                          # 3x1
        x = x + (K[:, 0] * y)
        P = (np.eye(3) - K @ H) @ P
        self._x, self._P = x, P
        self._n += 1

    @property
    def ready(self) -> bool:
        return self._x is not None and self._n >= 3

    def fill(self) -> float:
        return float(self._x[0]) if self._x is not None else 0.0

    def velocity(self) -> float:
        """Smoothed fill velocity in %/s (noise-robust vs a raw frame difference)."""
        return float(self._x[1]) if self._x is not None else 0.0

    def accel(self) -> float:
        return float(self._x[2]) if self._x is not None else 0.0

    def predict_to(self, t_s: float):
        """Extrapolate [fill, velocity] to time t_s WITHOUT mutating state -- lets the commit
        decision query 'where is the fill NOW' when the last valid sample is stale-gated /
        skipped frames old (irregular sampling on the compressed path). Returns (fill, vel)
        or None before the filter is ready. Extrapolation is clamped to max_dt like update."""
        if not self.ready or self._t is None:
            return None
        dt = float(t_s) - self._t
        if dt <= 0.0:
            return float(self._x[0]), float(self._x[1])
        dt = min(dt, self._max_dt)
        p, v, a = (float(self._x[0]), float(self._x[1]), float(self._x[2]))
        return p + v * dt + 0.5 * a * dt * dt, v + a * dt

    def ms_to_target(self, target_pct: float) -> Optional[float]:
        """Analytical sub-frame ms until fill reaches target_pct, from the current [p, v, a] state. Solves
        p + v*t + 0.5*a*t^2 = target for the smallest positive real t. Returns None if not rising toward it
        (receding / already past / no real future solution) so the caller can fall back."""
        if not self.ready:
            return None
        p, v, a = float(self._x[0]), float(self._x[1]), float(self._x[2])
        d = target_pct - p
        if d <= 0.0:
            return 0.0 if d > -1.0 else None       # already at/past target (tiny overshoot -> fire now)
        # near-zero accel -> linear
        if abs(a) < 1e-6:
            if v <= 1e-6:
                return None                        # not rising
            t = d / v
            return t * 1000.0 if t >= 0.0 else None
        # quadratic 0.5*a*t^2 + v*t - d = 0
        disc = v * v + 2.0 * a * d
        if disc < 0.0:
            return None                            # decelerating and won't reach target
        sq = math.sqrt(disc)
        t1 = (-v + sq) / a
        t2 = (-v - sq) / a
        cands = [t for t in (t1, t2) if t is not None and t >= 0.0]
        if not cands:
            return None
        return min(cands) * 1000.0 + self._bias_ms


def try_load() -> Optional["FillKalman"]:
    """Return a FillKalman iff ORION_FILL_KALMAN=1, else None (so it stays inert unless enabled)."""
    if os.environ.get("ORION_FILL_KALMAN", "0") != "1":
        return None
    try:
        return FillKalman(
            q=float(os.environ.get("ORION_FILL_KALMAN_Q", "15000")),
            r=float(os.environ.get("ORION_FILL_KALMAN_R", "3.0")),
            bias_ms=float(os.environ.get("ORION_FILL_KALMAN_BIAS_MS", "0")),
        )
    except Exception:
        return None
