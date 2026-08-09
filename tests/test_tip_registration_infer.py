"""Production tests for the dependency-free live tip-registration predictor."""
import builtins
import json

import numpy as np
import pytest

from tip_registration_infer import RegistrationPredictor, _MODEL, try_load


def _feed_rise(rp, T=360.0, u_tip=None, dt=16.0, up_to_frac=0.55):
    """Feed the first `up_to_frac` of a template-shaped rise; return (last_t, true_tip_ms)."""
    u_tip = rp._u_tip if u_tip is None else u_tip
    fmin, A = 20.0, 75.0
    t = 0.0
    tip_ms = T * u_tip
    last = 0.0
    while t <= up_to_frac * tip_ms:
        u = t / T
        f = fmin + A * float(rp._g(u))
        rp.update(t, min(f, 100.0), present=True, fed=True)
        last = t
        t += dt
    return last, tip_ms


def test_loads_and_predicts():
    rp = RegistrationPredictor()
    assert rp.enabled, rp.load_error
    last_t, tip_ms = _feed_rise(rp)
    out = rp.predict()
    assert out is not None
    ms_to_tip, conf = out
    pred_tip = last_t + ms_to_tip
    # predicted tip within a generous band of the true template tip (far-horizon estimate)
    assert abs(pred_tip - tip_ms) <= 160.0, (pred_tip, tip_ms)
    assert 0.0 <= conf <= 1.0
    assert ms_to_tip > 0.0        # still ahead of the tip on a partial rise


def test_reset_between_shots():
    rp = RegistrationPredictor()
    assert rp.enabled, rp.load_error
    _feed_rise(rp)
    assert len(rp._t) > 3
    # meter leaves -> history clears
    rp.update(9_999.0, 0.0, present=False, fed=False)
    assert len(rp._t) == 0
    assert rp.predict() is None


def test_fill_reset_starts_new_shot_even_when_meter_never_disappears():
    rp = RegistrationPredictor()
    assert rp.enabled, rp.load_error
    for timestamp, fill in enumerate((20.0, 32.0, 48.0, 66.0, 82.0)):
        rp.update(timestamp * 16.0, fill, present=True, fed=True)
    previous_shot_id = rp._shot_id

    rp.update(96.0, 24.0, present=True, fed=True)
    assert rp._shot_id == previous_shot_id  # one low frame is held as a reset candidate
    rp.update(112.0, 28.0, present=True, fed=True)
    assert rp._shot_id == previous_shot_id + 1
    assert list(rp._f) == [24.0, 28.0]
    assert list(rp._t) == [96.0, 112.0]
    assert rp.predict() is None


def test_one_low_outlier_does_not_end_or_poison_current_shot():
    rp = RegistrationPredictor()
    assert rp.enabled, rp.load_error
    for timestamp, fill in enumerate((20.0, 32.0, 48.0, 66.0, 82.0)):
        rp.update(timestamp * 16.0, fill, present=True, fed=True)
    previous_shot_id = rp._shot_id
    previous_count = len(rp._f)

    rp.update(96.0, 30.0, present=True, fed=True)
    rp.update(112.0, 85.0, present=True, fed=True)
    assert rp._shot_id == previous_shot_id
    assert len(rp._f) == previous_count + 1
    assert 30.0 not in rp._f


def test_back_to_back_retained_meter_predicts_second_shot_independently():
    rp = RegistrationPredictor()
    assert rp.enabled, rp.load_error
    _feed_rise(rp, T=360.0, up_to_frac=0.70)
    first_shot_id = rp._shot_id

    second_t0, duration = 1000.0, 400.0
    second_t = np.arange(second_t0, second_t0 + 0.60 * duration * rp._u_tip, 16.0)
    second_fill = 20.0 + 75.0 * np.asarray(
        rp._g((second_t - second_t0) / duration), dtype=float
    )
    for timestamp, fill in zip(second_t, second_fill):
        rp.update(float(timestamp), float(fill), present=True, fed=True)

    details = rp.predict_details()
    assert rp._shot_id == first_shot_id + 1
    assert details is not None
    assert details["support_n"] == len(second_t)
    assert abs(details["tip_capture_ms"] - (second_t0 + duration * rp._u_tip)) <= 80.0


def test_try_load_default_on_with_explicit_opt_out(monkeypatch):
    """Phase 3 (plan B2 [fix]): ORION_TIP_REG is DEFAULT-ON — the fit is load-bearing for the
    reader's trajectory gate + dead-reckoned coast. ORION_TIP_REG=0 opts out."""
    monkeypatch.delenv("ORION_TIP_REG", raising=False)
    rp = try_load()
    assert rp is not None and rp.enabled
    monkeypatch.setenv("ORION_TIP_REG", "0")
    assert try_load() is None
    monkeypatch.setenv("ORION_TIP_REG", "1")
    rp = try_load()
    assert rp is not None and rp.enabled


def test_confidence_bounded_on_short_history():
    rp = RegistrationPredictor()
    assert rp.enabled, rp.load_error
    rp.update(0.0, 22.0, present=True, fed=True)
    rp.update(16.0, 24.0, present=True, fed=True)
    # <3 samples -> no prediction yet
    assert rp.predict() is None


def test_structured_prediction_uses_capture_clock_and_reports_quality():
    rp = RegistrationPredictor()
    assert rp.enabled, rp.load_error
    last_t, _ = _feed_rise(rp, T=400.0, up_to_frac=0.65)
    details = rp.predict_details()
    assert details is not None
    assert details["tip_capture_ms"] == pytest.approx(
        details["sample_clock_ms"] + details["ms_to_tip"]
    )
    assert details["sample_clock_ms"] == pytest.approx(last_t)
    assert details["tip_epoch_ms"] == details["tip_capture_ms"]
    assert 1.0 <= details["sigma_ms"] <= 250.0
    assert 15.0 <= details["structural_sigma_ms"] <= details["sigma_ms"]
    assert details["support_n"] >= 3
    assert details["residual_pp"] >= 0.0
    assert details["model_id"].startswith("tip-registration-")
    assert details["fit_method"] == "numpy_bounded_robust_v1"
    legacy = rp.predict()
    assert legacy == pytest.approx((details["ms_to_tip"], details["confidence"]))


def test_startup_known_answer_is_mandatory():
    rp = RegistrationPredictor()
    assert rp.enabled, rp.load_error
    report = rp.self_test_report()
    assert report is not None and report["ok"] is True
    assert report["tip_error_ms"] <= 25.0
    assert report["residual_pp"] <= 1.5


def test_constructor_never_imports_scipy(monkeypatch):
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "scipy" or name.startswith("scipy."):
            raise AssertionError("SciPy must not be imported by the bundled predictor")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    rp = RegistrationPredictor()
    assert rp.enabled, rp.load_error


def test_invalid_model_fails_closed_instead_of_skipping(tmp_path):
    with open(_MODEL, encoding="utf-8") as handle:
        model = json.load(handle)
    model["g_vals"][12] = -50.0
    path = tmp_path / "invalid-tip-registration.json"
    path.write_text(json.dumps(model), encoding="utf-8")

    rp = RegistrationPredictor(str(path))
    assert not rp.enabled
    assert "content version" in rp.load_error


def test_robust_fit_rejects_one_bad_frame_without_past_tip_collapse():
    rp = RegistrationPredictor()
    assert rp.enabled, rp.load_error
    t0, duration = 1000.0, 400.0
    t = np.arange(t0, t0 + 0.70 * duration * rp._u_tip, 1000.0 / 60.0)
    fill = 20.0 + 75.0 * np.asarray(rp._g((t - t0) / duration), dtype=float)
    clean = rp.fit_samples(t, fill)
    assert clean is not None

    contaminated = fill.copy()
    contaminated[7] += 20.0
    robust = rp.fit_samples(t, contaminated)
    assert robust is not None
    assert abs(robust["tip_capture_ms"] - clean["tip_capture_ms"]) <= 20.0
    assert robust["tip_capture_ms"] > t[-1]
    assert robust["residual_pp"] > clean["residual_pp"]
    assert robust["sigma_ms"] >= robust["structural_sigma_ms"]


def test_duplicate_capture_timestamp_is_not_counted_as_support():
    rp = RegistrationPredictor()
    assert rp.enabled, rp.load_error
    t0, duration = 1000.0, 400.0
    t = np.arange(t0, t0 + 0.65 * duration * rp._u_tip, 1000.0 / 60.0)
    fill = 20.0 + 75.0 * np.asarray(rp._g((t - t0) / duration), dtype=float)
    duplicated_t = np.insert(t, [5, 5], t[5])
    duplicated_fill = np.insert(fill, [5, 5], [fill[5] + 10.0, fill[5]])

    fit = rp.fit_samples(duplicated_t, duplicated_fill)
    assert fit is not None
    assert fit["support_n"] == len(t)
