#!/usr/bin/env python3
"""Learn and atomically publish the production tip-registration model.

The research harness remains SciPy-based, but this builder emits the validated v2 data contract
consumed by the dependency-free ``tip_registration_infer.RegistrationPredictor`` runtime.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "models" / "tip_registration.json"

SCHEMA = "orion.tip_registration.v2"
MODEL_ID = "tip-registration-global"
MODEL_VERSION_PREFIX = "sha256-"
UNCERTAINTY = {
    "base_ms": 15.0,
    "horizon_scale": 0.4,
    "maximum_ms": 250.0,
    "basis": "conservative structural floor from leave-one-session-out horizon error",
}
NOTE = "learn_template on full corpus; see tools/timing/tip_registration_eval.py for LOSO accuracy"
_VERSION_FIELDS = ("u_grid", "g_vals", "u_tip", "priors", "q33", "q66", "uncertainty")


def _load_training_module():
    """Import heavyweight research dependencies only when an actual rebuild is requested."""

    root_text = str(ROOT)
    timing = str(ROOT / "tools" / "timing")
    diagnostics = str(ROOT / "tools" / "diagnostics")
    for path in (root_text, timing, diagnostics):
        if path not in sys.path:
            sys.path.insert(0, path)
    import tip_registration_eval as training
    return training


def _priors(pool: Sequence[dict], training) -> tuple[float, float, float]:
    return (
        float(np.median([training.shot_rise_dur(shot) for shot in pool])),
        float(np.median([
            float(shot["fill"][shot["tip"]] - np.min(shot["fill"][: shot["tip"] + 1]))
            for shot in pool
        ])),
        float(np.median([
            float(np.min(shot["fill"][: shot["tip"] + 1])) for shot in pool
        ])),
    )


def model_content_version(payload: dict[str, Any]) -> str:
    """Return a deterministic version for the numerical model and calibration contract."""

    material = {key: payload[key] for key in _VERSION_FIELDS}
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return MODEL_VERSION_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def assemble_payload(
    *,
    u_grid,
    g_vals,
    u_tip: float,
    priors: dict[str, Sequence[float]],
    q33: float,
    q66: float,
    n_shots: int,
    n_sessions: int,
    built_utc: str,
) -> dict[str, Any]:
    """Create, version, and validate one serializable v2 model document."""

    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "model_id": MODEL_ID,
        "u_grid": [round(float(value), 6) for value in u_grid],
        "g_vals": [round(float(value), 6) for value in g_vals],
        "u_tip": round(float(u_tip), 6),
        "priors": {
            str(key): [round(float(value), 3) for value in values]
            for key, values in priors.items()
        },
        "q33": round(float(q33), 2),
        "q66": round(float(q66), 2),
        "n_shots": int(n_shots),
        "n_sessions": int(n_sessions),
        "built_utc": str(built_utc),
        "uncertainty": dict(UNCERTAINTY),
        "note": NOTE,
    }
    payload["model_version"] = model_content_version(payload)
    validate_payload(payload)
    return payload


def validate_payload(payload: dict[str, Any]) -> None:
    """Raise ``ValueError`` unless payload is safe for the bundled runtime."""

    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("tip-registration model has an unsupported schema")
    if payload.get("model_id") != MODEL_ID:
        raise ValueError("tip-registration model_id mismatch")
    try:
        expected_version = model_content_version(payload)
    except KeyError as exc:
        raise ValueError(f"tip-registration model field missing: {exc.args[0]}") from exc
    if payload.get("model_version") != expected_version:
        raise ValueError("tip-registration content version mismatch")

    grid = np.asarray(payload.get("u_grid"), dtype=float)
    values = np.asarray(payload.get("g_vals"), dtype=float)
    if (grid.ndim != 1 or values.ndim != 1 or grid.size != values.size
            or grid.size < 16 or not np.all(np.isfinite(grid))
            or not np.all(np.isfinite(values)) or np.any(np.diff(grid) <= 0.0)
            or np.any(np.diff(values) < -1e-7)):
        raise ValueError("tip-registration template must be finite and monotone")
    tip = float(payload.get("u_tip", float("nan")))
    if not math.isfinite(tip) or not grid[0] <= tip <= min(grid[-1], 1.25):
        raise ValueError("tip-registration u_tip is outside template support")

    priors = payload.get("priors")
    if not isinstance(priors, dict) or set(priors) != {"g", "0", "1", "2"}:
        raise ValueError("tip-registration priors must contain g/0/1/2")
    for key, prior in priors.items():
        parsed = np.asarray(prior, dtype=float)
        if (parsed.shape != (3,) or not np.all(np.isfinite(parsed))
                or parsed[0] < 40.0 or parsed[1] < 5.0):
            raise ValueError(f"invalid tip-registration prior {key!r}")

    q33 = float(payload.get("q33", float("nan")))
    q66 = float(payload.get("q66", float("nan")))
    if not math.isfinite(q33) or not math.isfinite(q66) or not 40.0 <= q33 <= q66 <= 1000.0:
        raise ValueError("invalid tip-registration duration terciles")
    if int(payload.get("n_shots", 0)) < 8 or int(payload.get("n_sessions", 0)) < 1:
        raise ValueError("tip-registration training corpus is too small")
    built_utc = payload.get("built_utc")
    if not isinstance(built_utc, str) or not built_utc.endswith("Z") or len(built_utc) != 20:
        raise ValueError("invalid tip-registration build timestamp")

    uncertainty = payload.get("uncertainty")
    if not isinstance(uncertainty, dict) or uncertainty.get("basis") != UNCERTAINTY["basis"]:
        raise ValueError("tip-registration uncertainty calibration is missing")
    calibration = np.asarray([
        uncertainty.get("base_ms"),
        uncertainty.get("horizon_scale"),
        uncertainty.get("maximum_ms"),
    ], dtype=float)
    if (not np.all(np.isfinite(calibration)) or calibration[0] < 0.0
            or not 0.0 <= calibration[1] <= 2.0 or not 25.0 <= calibration[2] <= 1000.0):
        raise ValueError("invalid tip-registration uncertainty calibration")


def write_validated_payload(payload: dict[str, Any], destination: Path = OUT) -> None:
    """Known-answer test a temporary file, then atomically replace the production model."""

    validate_payload(payload)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        from tip_registration_infer import RegistrationPredictor
        predictor = RegistrationPredictor(str(temporary))
        if not predictor.enabled:
            raise ValueError(f"generated model failed runtime self-test: {predictor.load_error}")
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    training = _load_training_module()
    sessions, dropped = training.load_shots_by_session()
    all_shots = [shot for session in sessions.values() for shot in session]
    if len(all_shots) < 8:
        print(f"ERROR: only {len(all_shots)} usable shots in the corpus -- need >= 8 to learn a template.")
        return 1
    durations = np.asarray([training.shot_rise_dur(shot) for shot in all_shots], dtype=float)
    q33, q66 = (float(value) for value in np.percentile(durations, [33, 66]))

    def duration_type(shot) -> int:
        duration = training.shot_rise_dur(shot)
        return 0 if duration <= q33 else (1 if duration <= q66 else 2)

    print(
        f"corpus: {len(all_shots)} shots / {len(sessions)} sessions "
        f"({dropped} artifacts dropped) | rise-dur median={np.median(durations):.0f}ms "
        f"terciles: fast<= {q33:.0f}ms < med <= {q66:.0f}ms < slow"
    )
    g_vals, u_tip = training.learn_template(all_shots)
    priors = {"g": _priors(all_shots, training)}
    for kind in (0, 1, 2):
        pool = [shot for shot in all_shots if duration_type(shot) == kind]
        priors[str(kind)] = _priors(pool, training) if len(pool) >= 4 else priors["g"]

    payload = assemble_payload(
        u_grid=training.U_GRID,
        g_vals=g_vals,
        u_tip=u_tip,
        priors=priors,
        q33=q33,
        q66=q66,
        n_shots=len(all_shots),
        n_sessions=len(sessions),
        built_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    write_validated_payload(payload)
    print(f"wrote {OUT} ({payload['model_version']})")
    print(
        f"  u_tip={u_tip:.3f}  priors(T,A,fmin): "
        + "  ".join(f"{key}={tuple(round(x, 1) for x in value)}" for key, value in priors.items())
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
