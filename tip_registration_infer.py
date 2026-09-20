"""Dependency-free live meter-tip registration.

The predictor registers a persisted, monotone meter-rise template against capture-clock samples:

    fill(t) = fmin + A * g((t - t0) / T)

The template is learned offline. Live fitting changes only phase ``t0``, duration ``T``, and
amplitude ``A``. A bounded coarse-to-fine NumPy search and closed-form robust amplitude solve keep
the far-horizon estimator deterministic and bundle-friendly: SciPy and torch are not runtime
dependencies. ``predict()`` preserves the original ``(milliseconds_to_tip, confidence)`` API;
``predict_details()`` adds the absolute capture-clock tip, uncertainty, model identity, residual,
and support needed by the production fusion path.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import deque
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger("tip_registration")

_ROOT = os.path.dirname(os.path.abspath(__file__))
_MODEL = os.path.join(_ROOT, "models", "tip_registration.json")

U_MAX = 1.25  # must match tools/timing/tip_registration_eval.py
_MODEL_VERSION_FIELDS = (
    "u_grid", "g_vals", "u_tip", "priors", "q33", "q66", "uncertainty",
)


class _MonotoneCubicDerivative:
    def __init__(self, parent: "_MonotoneCubic") -> None:
        self._parent = parent

    def __call__(self, value):
        return self._parent.evaluate_derivative(value)


class _MonotoneCubic:
    """NumPy-only monotone cubic Hermite interpolation (PCHIP shape semantics)."""

    def __init__(self, x, y) -> None:
        self.x = np.asarray(x, dtype=float)
        self.y = np.asarray(y, dtype=float)
        if self.x.ndim != 1 or self.y.ndim != 1 or self.x.size != self.y.size or self.x.size < 2:
            raise ValueError("monotone template requires equal one-dimensional arrays")
        if not np.all(np.isfinite(self.x)) or not np.all(np.isfinite(self.y)):
            raise ValueError("monotone template contains non-finite values")
        h = np.diff(self.x)
        if np.any(h <= 0.0):
            raise ValueError("monotone template grid must be strictly increasing")
        delta = np.diff(self.y) / h
        if np.any(delta < -1e-7):
            raise ValueError("meter-rise template must be monotone")

        slopes = np.empty_like(self.y)
        if self.x.size == 2:
            slopes[:] = delta[0]
        else:
            middle = slopes[1:-1]
            middle[:] = 0.0
            same_sign = (delta[:-1] * delta[1:]) > 0.0
            w1 = 2.0 * h[1:] + h[:-1]
            w2 = h[1:] + 2.0 * h[:-1]
            middle[same_sign] = (
                (w1[same_sign] + w2[same_sign])
                / (w1[same_sign] / delta[:-1][same_sign]
                   + w2[same_sign] / delta[1:][same_sign])
            )
            slopes[0] = self._endpoint_slope(h[0], h[1], delta[0], delta[1])
            slopes[-1] = self._endpoint_slope(h[-1], h[-2], delta[-1], delta[-2])
        self._slopes = slopes

    @staticmethod
    def _endpoint_slope(h0: float, h1: float, d0: float, d1: float) -> float:
        slope = ((2.0 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
        if slope * d0 <= 0.0:
            return 0.0
        if d0 * d1 < 0.0 and abs(slope) > 3.0 * abs(d0):
            return 3.0 * d0
        return float(slope)

    def _parts(self, value):
        original = np.asarray(value, dtype=float)
        clipped = np.clip(original, self.x[0], self.x[-1])
        index = np.searchsorted(self.x, clipped, side="right") - 1
        index = np.clip(index, 0, self.x.size - 2)
        h = self.x[index + 1] - self.x[index]
        phase = (clipped - self.x[index]) / h
        return original, index, h, phase

    @staticmethod
    def _restore_scalar(original, result):
        return float(result) if np.ndim(original) == 0 else result

    def __call__(self, value):
        original, index, h, phase = self._parts(value)
        p2 = phase * phase
        p3 = p2 * phase
        result = (
            (2.0 * p3 - 3.0 * p2 + 1.0) * self.y[index]
            + (p3 - 2.0 * p2 + phase) * h * self._slopes[index]
            + (-2.0 * p3 + 3.0 * p2) * self.y[index + 1]
            + (p3 - p2) * h * self._slopes[index + 1]
        )
        return self._restore_scalar(original, result)

    def evaluate_derivative(self, value):
        original, index, h, phase = self._parts(value)
        p2 = phase * phase
        result = (
            (6.0 * p2 - 6.0 * phase) * self.y[index]
            + (3.0 * p2 - 4.0 * phase + 1.0) * h * self._slopes[index]
            + (-6.0 * p2 + 6.0 * phase) * self.y[index + 1]
            + (3.0 * p2 - 2.0 * phase) * h * self._slopes[index + 1]
        ) / h
        return self._restore_scalar(original, result)

    def derivative(self) -> _MonotoneCubicDerivative:
        return _MonotoneCubicDerivative(self)


class RegistrationPredictor:
    """Register a shot's capture-clock rise stream against the persisted meter template."""

    def __init__(self, model_path: str = _MODEL) -> None:
        self.enabled = False
        self.load_error = "not initialized"
        self._u_grid: Optional[np.ndarray] = None
        self._g_vals: Optional[np.ndarray] = None
        self._u_tip = 1.0
        self._priors: dict[str, tuple[float, float, float]] = {}
        self._q33 = 0.0
        self._q66 = 0.0
        self._sigma_base_ms = 15.0
        self._sigma_horizon_scale = 0.4
        self._sigma_maximum_ms = 250.0
        self._g_spline: Optional[_MonotoneCubic] = None
        self._dg_spline: Optional[_MonotoneCubicDerivative] = None
        self._dense_u: Optional[np.ndarray] = None
        self._dense_g: Optional[np.ndarray] = None
        self._dense_dg: Optional[np.ndarray] = None
        self.model_id = "unavailable"
        self.model_version = "unavailable"
        self._self_test_report: Optional[dict] = None

        self._t: deque = deque(maxlen=256)
        self._f: deque = deque(maxlen=256)
        self._t0_shot: Optional[float] = None
        self._last_shot_t: Optional[np.ndarray] = None
        self._last_shot_f: Optional[np.ndarray] = None
        self.shot_archive_n = 0
        self._last_fit: Optional[dict] = None
        self._last_prediction: Optional[dict] = None
        self._accepted_since_fit = 0
        self._shot_id = 0
        self._pending_reset: Optional[tuple[float, float]] = None
        self._ginv_u: Optional[np.ndarray] = None
        self._ginv_g: Optional[np.ndarray] = None

        try:
            with open(model_path, encoding="utf-8") as handle:
                model = json.load(handle)
            self._u_grid = np.asarray(model["u_grid"], dtype=float)
            self._g_vals = np.asarray(model["g_vals"], dtype=float)
            self._validate_model(model)
            self._g_spline = _MonotoneCubic(self._u_grid, self._g_vals)
            self._dg_spline = self._g_spline.derivative()
            # Candidate evaluation is the hot path. Precompute a dense monotone lookup once so
            # grid fits use NumPy's C-level interpolation instead of repeated knot searches.
            self._dense_u = np.linspace(float(self._u_grid[0]), float(self._u_grid[-1]), 2049)
            self._dense_g = np.asarray(self._g_spline(self._dense_u), dtype=float)
            self._dense_dg = np.asarray(self._dg_spline(self._dense_u), dtype=float)
            self._u_tip = float(model["u_tip"])
            self._priors = {
                str(key): tuple(float(value) for value in values)
                for key, values in model["priors"].items()
            }
            self._q33 = float(model.get("q33", 0.0))
            self._q66 = float(model.get("q66", 0.0))
            uncertainty = model.get("uncertainty", {})
            self._sigma_base_ms = float(uncertainty.get("base_ms", 15.0))
            self._sigma_horizon_scale = float(uncertainty.get("horizon_scale", 0.4))
            self._sigma_maximum_ms = float(uncertainty.get("maximum_ms", 250.0))
            canonical = json.dumps(model, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
            self.model_id = str(model.get("model_id") or f"tip-registration-{fingerprint}")
            self.model_version = str(model.get("model_version") or model.get("built_utc") or fingerprint)
            self.enabled = True
            self.load_error = ""
            self._self_test_report = self.startup_self_test()
            if not self._self_test_report.get("ok", False):
                raise RuntimeError(f"known-answer self-test failed: {self._self_test_report}")
            logger.info(
                "RegistrationPredictor ready: %s (model=%s u_tip=%.3f, %d priors)",
                os.path.basename(model_path), self.model_id, self._u_tip, len(self._priors),
            )
        except Exception as exc:
            self.load_error = str(exc)
            self.enabled = False
            logger.warning("RegistrationPredictor unavailable (%s); disabled", exc)

    def _validate_model(self, model: dict) -> None:
        schema = model.get("schema")
        if schema != "orion.tip_registration.v2":
            raise ValueError(f"unsupported tip-registration model schema {schema!r}")
        if model.get("model_id") != "tip-registration-global":
            raise ValueError("unexpected tip-registration model_id")
        try:
            material = {key: model[key] for key in _MODEL_VERSION_FIELDS}
        except KeyError as exc:
            raise ValueError(f"tip-registration model field missing: {exc.args[0]}") from exc
        canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        expected_version = "sha256-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
        if model.get("model_version") != expected_version:
            raise ValueError("tip-registration content version mismatch")
        if self._u_grid is None or self._g_vals is None:
            raise ValueError("template arrays are missing")
        if self._u_grid.ndim != 1 or self._g_vals.ndim != 1:
            raise ValueError("template arrays must be one-dimensional")
        if self._u_grid.size != self._g_vals.size or self._u_grid.size < 16:
            raise ValueError("template arrays have incompatible lengths")
        if not np.all(np.isfinite(self._u_grid)) or not np.all(np.isfinite(self._g_vals)):
            raise ValueError("template contains non-finite values")
        if np.any(np.diff(self._u_grid) <= 0.0) or np.any(np.diff(self._g_vals) < -1e-7):
            raise ValueError("template grid and values must be monotone")
        u_tip = float(model["u_tip"])
        if not self._u_grid[0] <= u_tip <= min(self._u_grid[-1], U_MAX):
            raise ValueError("u_tip lies outside the learned template support")
        priors = model.get("priors")
        if not isinstance(priors, dict) or "g" not in priors:
            raise ValueError("global duration/amplitude/fill prior is missing")
        for key, values in priors.items():
            if not isinstance(values, (list, tuple)) or len(values) != 3:
                raise ValueError(f"invalid prior {key!r}")
            parsed = np.asarray(values, dtype=float)
            if not np.all(np.isfinite(parsed)) or parsed[0] < 40.0 or parsed[1] < 5.0:
                raise ValueError(f"out-of-range prior {key!r}")
        uncertainty = model.get("uncertainty", {})
        calibration = np.asarray([
            uncertainty.get("base_ms", 15.0),
            uncertainty.get("horizon_scale", 0.4),
            uncertainty.get("maximum_ms", 250.0),
        ], dtype=float)
        if (not np.all(np.isfinite(calibration)) or calibration[0] < 0.0
                or not 0.0 <= calibration[1] <= 2.0 or not 25.0 <= calibration[2] <= 1000.0):
            raise ValueError("invalid uncertainty calibration")

    def _g(self, u):
        if self._dense_u is None or self._dense_g is None:
            raise RuntimeError("registration template is unavailable")
        return np.interp(np.clip(u, 0.0, U_MAX), self._dense_u, self._dense_g)

    def _dg(self, u):
        if self._dense_u is None or self._dense_dg is None:
            raise RuntimeError("registration template derivative is unavailable")
        return np.interp(np.clip(u, 0.0, U_MAX), self._dense_u, self._dense_dg)

    def _prior_for(self, elapsed_ms: float) -> Tuple[float, float, float]:
        # A short partial rise is ambiguous, so begin with the global prior and only move toward
        # slower families once elapsed time has ruled out the faster ones.
        if elapsed_ms > self._q66 and "2" in self._priors:
            return self._priors["2"]
        if elapsed_ms > self._q33 and "1" in self._priors:
            return self._priors["1"]
        return self._priors.get("g", (366.0, 74.0, 20.0))

    def update(self, t_ms: float, fill_pct: float, present: bool, fed: bool = True) -> None:
        """Feed one genuine capture-clock observation. Gated frames skip rather than clear."""
        if not self.enabled:
            return
        try:
            # Absence is subtractive lifecycle information, including health-loss
            # reports without a usable timestamp. It must still clear the old fit.
            if not present:
                if self._t or self._pending_reset is not None:
                    self._archive_shot()
                    self._clear_current_shot()
                return
            if not fed:
                return
            timestamp = float(t_ms)
            fill = float(fill_pct)
            if not np.isfinite(timestamp) or not np.isfinite(fill):
                return
            # Only genuine present observations can grow/confirm a trajectory.
            # Repeated or out-of-order reports neither reweight the fit nor turn
            # one low-fill outlier into two witnesses of a new shot.
            if self._t and timestamp <= self._t[-1]:
                return
            if self._pending_reset is not None and timestamp <= self._pending_reset[0]:
                return

            # Confirm a reset on two genuine reports. A one-frame low outlier is discarded rather
            # than either poisoning the fit or ending the shot. The first frame of a confirmed new
            # rise is retained, so this hysteresis costs no trajectory evidence.
            if self._pending_reset is not None:
                old_max = max(self._f) if self._f else -np.inf
                pending = self._pending_reset
                self._pending_reset = None
                if self._f and fill < old_max - 8.0:
                    self._archive_shot()
                    self._clear_current_shot()
                    self._append_sample(*pending)

            if self._f and fill < max(self._f) - 8.0:
                self._pending_reset = (timestamp, fill)
                return
            self._append_sample(timestamp, fill)
        except Exception:
            logger.debug("tip-registration sample rejected", exc_info=True)

    def _append_sample(self, timestamp: float, fill: float) -> None:
        if self._t0_shot is None:
            self._t0_shot = timestamp
        self._t.append(timestamp)
        self._f.append(fill)
        self._accepted_since_fit += 1

    def _clear_current_shot(self) -> None:
        self._t.clear()
        self._f.clear()
        self._t0_shot = None
        self._accepted_since_fit = 0
        self._last_fit = None
        self._last_prediction = None
        self._pending_reset = None
        self._shot_id += 1

    def reset_tracking(self) -> None:
        """Discard trajectory/ruler history without reloading the meter model.

        A source or fill-ruler change is not a completed shot and must not archive
        a mixed trajectory as a post-hoc timing label. Keep archive IDs monotonic.
        """
        self._clear_current_shot()
        self._last_shot_t = None
        self._last_shot_f = None

    def _archive_shot(self) -> None:
        try:
            if len(self._t) >= 6 and (max(self._f) - min(self._f)) >= 20.0:
                self._last_shot_t = np.asarray(self._t, dtype=float)
                self._last_shot_f = np.asarray(self._f, dtype=float)
                self.shot_archive_n += 1
        except Exception:
            logger.debug("tip-registration shot archive rejected", exc_info=True)

    def _evaluate_candidates(self, tk: np.ndarray, fk: np.ndarray, fmin: float,
                             t0_values: np.ndarray, duration_values: np.ndarray,
                             T0: float, A0: float) -> dict:
        """Evaluate a phase/duration grid and eliminate amplitude with two robust IRLS rounds."""
        t0_mesh, duration_mesh = np.meshgrid(t0_values, duration_values, indexing="ij")
        t0_candidates = t0_mesh.reshape(-1)
        durations = duration_mesh.reshape(-1)
        u = (tk[None, :] - t0_candidates[:, None]) / durations[:, None]
        shape = np.asarray(self._g(u), dtype=float)
        # The floor prevents a saturated/past-tip candidate from hiding arbitrarily large fill
        # errors behind g' ~= 0 while the square-root term still emphasizes the informative rise.
        steepness = np.sqrt(np.abs(np.asarray(self._dg(u), dtype=float))) + 0.20
        y = fk[None, :] - float(fmin)
        base_weight = (steepness / 8.0) ** 2
        amplitude_ridge = (1.0 / max(A0, 5.0)) ** 2
        weights = base_weight
        amplitude = np.full(t0_candidates.shape, A0, dtype=float)
        for _ in range(2):
            denominator = np.sum(weights * shape * shape, axis=1) + amplitude_ridge
            numerator = np.sum(weights * shape * y, axis=1) + amplitude_ridge * A0
            amplitude = np.clip(
                numerator / np.maximum(denominator, 1e-12), 0.5 * A0, 1.6 * A0
            )
            normalized = steepness * (y - amplitude[:, None] * shape) / 8.0
            weights = base_weight / np.sqrt(1.0 + normalized * normalized)

        normalized = steepness * (y - amplitude[:, None] * shape) / 8.0
        prior_T = 1.5 * (durations - T0) / T0
        prior_A = (amplitude - A0) / A0
        point_objective = np.sqrt(1.0 + normalized * normalized) - 1.0
        objective = np.sum(point_objective, axis=1)
        if point_objective.shape[1] >= 8:
            # One detector spike must not select a different phase/duration basin. Keep the
            # residual in confidence and uncertainty, but trim its leverage during registration.
            objective -= np.max(point_objective, axis=1)
        objective += np.sqrt(1.0 + prior_T * prior_T) - 1.0
        objective += np.sqrt(1.0 + prior_A * prior_A) - 1.0
        return {
            "t0": t0_candidates,
            "T": durations,
            "A": amplitude,
            "objective": objective,
            "data_residual": normalized,
            "prior_T": prior_T,
            "prior_A": prior_A,
        }

    @staticmethod
    def _deduplicate_samples(t: np.ndarray, f: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        finite = np.isfinite(t) & np.isfinite(f)
        t, f = t[finite], f[finite]
        order = np.argsort(t, kind="stable")
        t, f = t[order], f[order]
        if t.size and np.any(np.diff(t) <= 0.0):
            reverse_unique = np.unique(t[::-1], return_index=True)[1]
            keep = np.sort(t.size - 1 - reverse_unique)
            t, f = t[keep], f[keep]
        return t, f

    def _fit_core(self, t: np.ndarray, f: np.ndarray, elapsed_ms: float,
                  censor_above: Optional[float] = None) -> Optional[dict]:
        if not self.enabled or self._g_spline is None:
            return None
        t, f = self._deduplicate_samples(
            np.asarray(t, dtype=float), np.asarray(f, dtype=float)
        )
        if t.size < 3:
            return None

        T_prior, A_prior, _fmin_prior = self._prior_for(elapsed_ms)
        # A single dark/partial segmentation frame must not redefine the whole trajectory's
        # baseline. The median of the three lowest genuine samples is robust while remaining
        # within a fraction of one fill point of the observed start on a clean 60 Hz rise.
        baseline_count = min(3, int(f.size))
        fmin = float(np.median(np.partition(f, baseline_count - 1)[:baseline_count]))
        keep = f < 97.0
        if censor_above is not None:
            keep &= f < float(censor_above)
        if int(keep.sum()) < 3:
            keep = f < 97.0
        if int(keep.sum()) < 3:
            keep = np.ones_like(f, dtype=bool)
        tk, fk = t[keep], f[keep]
        if tk.size < 3:
            return None

        A0 = max(float(A_prior), 5.0)
        T0 = max(float(T_prior), 40.0)
        t0_lo, t0_hi = float(tk[0] - 1.2 * T0), float(tk[-1] + 0.2 * T0)
        duration_lo, duration_hi = 0.35 * T0, 2.5 * T0
        t0_values = np.unique(np.concatenate((
            np.linspace(t0_lo, t0_hi, 17),
            np.asarray([tk[0] - 0.05 * T0, tk[0]], dtype=float),
        )))
        duration_values = np.unique(np.append(
            np.linspace(duration_lo, duration_hi, 15), T0
        ))
        evaluation: Optional[dict] = None
        best_index = 0

        for refinement in range(3):
            evaluation = self._evaluate_candidates(
                tk, fk, fmin, t0_values, duration_values, T0, A0
            )
            best_index = int(np.argmin(evaluation["objective"]))
            if refinement == 2:
                break
            best_t0 = float(evaluation["t0"][best_index])
            best_duration = float(evaluation["T"][best_index])
            t0_step = max(float(np.ptp(t0_values)) / max(t0_values.size - 1, 1), 0.25)
            duration_step = max(
                float(np.ptp(duration_values)) / max(duration_values.size - 1, 1), 0.25
            )
            refinement_points = 11 if refinement == 0 else 9
            t0_values = np.linspace(
                max(t0_lo, best_t0 - 2.0 * t0_step),
                min(t0_hi, best_t0 + 2.0 * t0_step),
                refinement_points,
            )
            duration_values = np.linspace(
                max(duration_lo, best_duration - 2.0 * duration_step),
                min(duration_hi, best_duration + 2.0 * duration_step),
                refinement_points,
            )

        assert evaluation is not None
        t0 = float(evaluation["t0"][best_index])
        duration = float(evaluation["T"][best_index])
        amplitude = float(evaluation["A"][best_index])
        data_residual = np.asarray(evaluation["data_residual"][best_index], dtype=float)
        residual_vector = np.concatenate((
            data_residual,
            np.asarray([
                evaluation["prior_T"][best_index], evaluation["prior_A"][best_index]
            ], dtype=float),
        ))
        conf = self._confidence_from_residual(residual_vector, tk, t0, duration)
        rms_pp = float(np.sqrt(np.mean(data_residual * data_residual))) * 8.0
        u_fit = (tk - t0) / max(duration, 30.0)
        model_f = fmin + amplitude * np.asarray(self._g(u_fit), dtype=float)
        rms_unw_pp = float(np.sqrt(np.mean((fk - model_f) ** 2)))
        tip_capture_ms = t0 + duration * self._u_tip

        candidate_tip = evaluation["t0"] + evaluation["T"] * self._u_tip
        delta_cost = np.asarray(evaluation["objective"], dtype=float) - float(
            evaluation["objective"][best_index]
        )
        probability = np.exp(-0.5 * np.clip(delta_cost, 0.0, 50.0))
        probability /= max(float(np.sum(probability)), 1e-12)
        candidate_mean = float(np.sum(probability * candidate_tip))
        candidate_sigma = float(np.sqrt(np.sum(probability * (candidate_tip - candidate_mean) ** 2)))
        dt = float(np.median(np.diff(tk))) if tk.size > 1 else 16.667
        dt = float(np.clip(dt, 1.0, 50.0))
        local_velocity = np.abs(amplitude * np.asarray(self._dg(u_fit), dtype=float) / duration)
        informative_velocity = local_velocity[local_velocity >= 0.01]
        velocity_scale = (
            float(np.percentile(informative_velocity, 65.0)) if informative_velocity.size else 0.01
        )
        residual_time = min(120.0, rms_unw_pp / max(velocity_scale, 0.01))
        horizon = max(0.0, tip_capture_ms - float(tk[-1]))
        structural_sigma = min(
            self._sigma_maximum_ms,
            self._sigma_base_ms + self._sigma_horizon_scale * horizon,
        )
        sigma_ms = float(np.clip(np.sqrt(
            candidate_sigma * candidate_sigma
            + (dt / np.sqrt(12.0)) ** 2
            + (0.35 * residual_time) ** 2
            + ((1.0 - conf) * 0.08 * horizon) ** 2
            + structural_sigma * structural_sigma
        ), 1.0, self._sigma_maximum_ms))

        return {
            "t0": t0,
            "T": duration,
            "A": amplitude,
            "fmin": fmin,
            "conf": float(conf),
            "rms_pp": rms_pp,
            "rms_unw_pp": rms_unw_pp,
            "n_used": int(tk.size),
            "support_n": int(tk.size),
            "T0": T0,
            "tip_capture_ms": float(tip_capture_ms),
            "tip_epoch_ms": float(tip_capture_ms),
            "sample_clock_ms": float(tk[-1]),
            "sigma_ms": sigma_ms,
            "structural_sigma_ms": float(structural_sigma),
            "residual_pp": rms_unw_pp,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "fit_method": "numpy_bounded_robust_v1",
        }

    def _prediction(self) -> Optional[dict]:
        if not self.enabled or len(self._t) < 3:
            self._last_prediction = None
            return None
        t = np.asarray(self._t, dtype=float)
        f = np.asarray(self._f, dtype=float)
        t_now = float(t[-1])
        cached = self._last_fit
        if (cached is not None and cached.get("_shot_id") == self._shot_id
                and self._accepted_since_fit < 2):
            fit = cached
        else:
            elapsed = t_now - float(self._t0_shot if self._t0_shot is not None else t[0])
            fit = self._fit_core(t, f, elapsed)
            if fit is None:
                self._last_prediction = None
                return None
            fit["_shot_id"] = self._shot_id
            fit["_t_last_ms"] = t_now
            self._last_fit = fit
            self._accepted_since_fit = 0

        tip_capture_ms = float(fit["tip_capture_ms"])
        ms_to_tip = tip_capture_ms - t_now
        if ms_to_tip < -40.0 or ms_to_tip > 4.0 * float(fit["T0"]):
            self._last_prediction = None
            return None
        details = {
            "ms_to_tip": float(ms_to_tip),
            "horizon_ms": float(ms_to_tip),
            "confidence": float(fit["conf"]),
            "tip_capture_ms": tip_capture_ms,
            "tip_epoch_ms": tip_capture_ms,
            "sample_clock_ms": t_now,
            "sigma_ms": float(fit["sigma_ms"]),
            "structural_sigma_ms": float(fit["structural_sigma_ms"]),
            "model_id": str(fit["model_id"]),
            "model_version": str(fit["model_version"]),
            "support_n": int(fit["support_n"]),
            "residual_pp": float(fit["residual_pp"]),
            "fit_method": str(fit["fit_method"]),
        }
        self._last_prediction = details
        return details

    def predict(self) -> Optional[Tuple[float, float]]:
        """Return the legacy ``(ms_to_tip, confidence)`` tuple."""
        try:
            details = self._prediction()
            if details is None:
                return None
            return float(details["ms_to_tip"]), float(details["confidence"])
        except Exception:
            logger.debug("tip-registration prediction rejected", exc_info=True)
            return None

    def predict_details(self) -> Optional[dict]:
        """Return a structured prediction on the same clock as the fed capture samples."""
        try:
            details = self._prediction()
            return dict(details) if details is not None else None
        except Exception:
            logger.debug("tip-registration detailed prediction rejected", exc_info=True)
            return None

    def last_prediction(self) -> Optional[dict]:
        return dict(self._last_prediction) if self._last_prediction is not None else None

    def predict_fill(self, t_ms: float) -> Optional[dict]:
        """Model fill and velocity at a capture-clock timestamp from the current live fit."""
        try:
            fit = self._last_fit
            if (not self.enabled or fit is None or fit.get("_shot_id") != self._shot_id
                    or not self._t):
                return None
            query_ms = float(t_ms)
            age = query_ms - float(self._t[-1])
            if age < -150.0 or age > 400.0:
                return None
            duration = max(float(fit["T"]), 30.0)
            u = (query_ms - float(fit["t0"])) / duration
            fill = float(fit["fmin"]) + float(fit["A"]) * float(self._g(u))
            velocity = float(fit["A"]) * float(self._dg(u)) / duration
            return {
                "fill": float(np.clip(fill, 0.0, 110.0)),
                "vel_pp_ms": velocity,
                "sigma_pp": float(fit["rms_unw_pp"]),
                "n": int(fit["n_used"]),
                "conf": float(fit["conf"]),
                "age_ms": float(age),
            }
        except Exception:
            return None

    def last_fit(self) -> Optional[dict]:
        """Return the live fit; private bookkeeping keys are retained for API compatibility."""
        return self._last_fit

    def fit_samples(self, t_arr, f_arr, censor_above: Optional[float] = None) -> Optional[dict]:
        try:
            t = np.asarray(t_arr, dtype=float)
            f = np.asarray(f_arr, dtype=float)
            if t.ndim != 1 or f.ndim != 1 or t.size < 3 or t.size != f.size:
                return None
            finite = np.isfinite(t) & np.isfinite(f)
            if int(finite.sum()) < 3:
                return None
            elapsed = float(np.max(t[finite]) - np.min(t[finite]))
            return self._fit_core(t, f, elapsed, censor_above=censor_above)
        except Exception:
            return None

    def posthoc_tip_ms(self) -> Optional[Tuple[float, float]]:
        try:
            if self._last_shot_t is None or self._last_shot_f is None:
                return None
            fit = self.fit_samples(self._last_shot_t, self._last_shot_f)
            if fit is None or fit["n_used"] < 6 or fit["rms_unw_pp"] > 3.0:
                return None
            return float(fit["tip_capture_ms"]), float(max(fit["conf"], 0.2))
        except Exception:
            return None

    def g_slope(self, u: float) -> Optional[float]:
        if not self.enabled or self._dg_spline is None:
            return None
        try:
            return float(self._dg(float(u)))
        except Exception:
            return None

    def g_inverse(self, gval: float) -> Optional[float]:
        if not self.enabled or self._g_spline is None:
            return None
        try:
            if self._ginv_u is None or self._ginv_g is None:
                u = np.linspace(0.0, float(self._u_tip), 512)
                g = np.maximum.accumulate(np.asarray(self._g_spline(u), dtype=float))
                self._ginv_u, self._ginv_g = u, g
            clipped = float(np.clip(gval, self._ginv_g[0], self._ginv_g[-1]))
            return float(np.interp(clipped, self._ginv_g, self._ginv_u))
        except Exception:
            return None

    def _confidence_from_residual(self, residual: np.ndarray, tk: np.ndarray,
                                  t0: float, duration: float) -> float:
        try:
            u = (tk - t0) / max(duration, 30.0)
            n_steep = int((np.abs(self._dg(u)) > 0.1).sum())
            data = np.asarray(residual[:tk.size], dtype=float)
            rms = float(np.sqrt(np.mean(data * data))) * 8.0
            res_term = float(np.exp(-rms / 6.0))
            n_term = min(1.0, n_steep / 5.0)
            prior_norm = float(np.linalg.norm(residual[-2:]))
            data_norm = float(np.linalg.norm(data)) + 1e-6
            prior_dominance = prior_norm / (prior_norm + data_norm)
            prior_term = float(np.clip(1.0 - prior_dominance, 0.2, 1.0))
            return float(np.clip(res_term * n_term * prior_term, 0.0, 1.0))
        except Exception:
            return 0.0

    def startup_self_test(self) -> dict:
        """Run a deterministic known-answer fit against the bundled template."""
        if not self.enabled or self._g_spline is None:
            return {"ok": False, "reason": "model unavailable"}
        try:
            duration, amplitude, fmin = self._priors.get("g", (366.667, 74.68, 20.805))
            t0 = 1000.0
            stop = t0 + 0.70 * float(duration) * self._u_tip
            t = np.arange(t0, stop + 0.1, 16.0)
            f = float(fmin) + float(amplitude) * np.asarray(
                self._g((t - t0) / float(duration)), dtype=float
            )
            fit = self._fit_core(t, f, float(t[-1] - t[0]))
            if fit is None:
                return {"ok": False, "reason": "fit unavailable"}
            expected_tip = t0 + float(duration) * self._u_tip
            error_ms = abs(float(fit["tip_capture_ms"]) - expected_tip)
            inverse_error = abs(float(self.g_inverse(float(self._g(0.5))) or 0.0) - 0.5)
            ok = error_ms <= 25.0 and fit["rms_unw_pp"] <= 1.5 and inverse_error <= 0.01
            return {
                "ok": bool(ok),
                "tip_error_ms": float(error_ms),
                "residual_pp": float(fit["rms_unw_pp"]),
                "inverse_error": float(inverse_error),
                "model_id": self.model_id,
            }
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}

    def self_test_report(self) -> Optional[dict]:
        return dict(self._self_test_report) if self._self_test_report is not None else None


def try_load() -> Optional[RegistrationPredictor]:
    """Load the default-on predictor; ``ORION_TIP_REG=0`` remains an explicit opt-out."""
    if os.environ.get("ORION_TIP_REG", "1").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    if not os.path.isfile(_MODEL):
        logger.error("required tip-registration model missing: %s", _MODEL)
        return None
    predictor = RegistrationPredictor()
    if not predictor.enabled:
        logger.error("required tip-registration predictor failed startup: %s", predictor.load_error)
        return None
    return predictor
