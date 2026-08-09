"""RegistrationPredictor API, fit quality, archive, and coast semantics."""
import numpy as np
import pytest

from tip_registration_infer import RegistrationPredictor


@pytest.fixture(scope="module")
def rp():
    p = RegistrationPredictor()
    assert p.enabled, p.load_error
    return p


def _synth(rp, t0=1000.0, T=400.0, A=75.0, fmin=20.0, dt=16.0):
    """Fill samples generated FROM the template itself with known (t0, T, A)."""
    t = np.arange(t0, t0 + T + dt, dt)
    u = (t - t0) / T
    f = fmin + A * np.asarray(rp._g(u), float)
    return t, f


def test_fit_samples_recovers_known_params(rp):
    t, f = _synth(rp)
    fit = rp.fit_samples(t, f)
    assert fit is not None
    assert abs(fit["T"] - 400.0) / 400.0 < 0.15, fit
    assert abs(fit["t0"] - 1000.0) < 40.0, fit
    assert fit["rms_unw_pp"] < 1.0
    assert fit["n_used"] >= 10
    assert 0.0 <= fit["conf"] <= 1.0


def test_g_inverse_roundtrip(rp):
    for u in (0.2, 0.5, 0.8):
        g = float(rp._g(u))
        u_back = rp.g_inverse(g)
        assert u_back is not None
        assert abs(u_back - u) < 0.03, (u, u_back)


def test_update_skips_not_clears_on_gated_frame(rp):
    p = RegistrationPredictor()
    assert p.enabled, p.load_error
    t, f = _synth(p)
    for tt, ff in zip(t[:10], f[:10]):
        p.update(float(tt), float(ff), present=True, fed=True)
    n_before = len(p._t)
    # a gated/held frame mid-shot must NOT wipe the accumulated fit samples
    p.update(float(t[10]), float(f[10]), present=True, fed=False)
    assert len(p._t) == n_before
    # ...while meter-gone DOES end the shot (and archives it)
    p.update(float(t[10]) + 16.0, 0.0, present=False, fed=False)
    assert len(p._t) == 0


def test_archive_and_posthoc_tip(rp):
    p = RegistrationPredictor()
    assert p.enabled, p.load_error
    t, f = _synth(p, t0=2000.0, T=380.0, A=74.0, fmin=21.0)
    for tt, ff in zip(t, f):
        p.update(float(tt), float(ff), present=True, fed=True)
    assert p.shot_archive_n == 0
    p.update(float(t[-1]) + 16.0, 0.0, present=False, fed=False)
    assert p.shot_archive_n == 1
    ph = p.posthoc_tip_ms()
    assert ph is not None
    tip, conf = ph
    true_tip = 2000.0 + 380.0 * p._u_tip
    assert abs(tip - true_tip) < 40.0, (tip, true_tip)
    assert conf >= 0.2


def test_predict_exposes_last_fit(rp):
    p = RegistrationPredictor()
    assert p.enabled, p.load_error
    t, f = _synth(p)
    keep = f < 90.0          # mid-rise: predict() has a real forward horizon
    for tt, ff in zip(t[keep], f[keep]):
        p.update(float(tt), float(ff), present=True, fed=True)
    out = p.predict()
    assert out is not None
    ms_to_tip, conf = out
    fit = p.last_fit()
    assert fit is not None
    for key in ("t0", "T", "A", "fmin", "conf", "rms_pp", "rms_unw_pp", "n_used"):
        assert key in fit
    # predicted tip consistent with the known synthesis
    tip = float(t[keep][-1]) + ms_to_tip
    true_tip = 1000.0 + 400.0 * p._u_tip
    assert abs(tip - true_tip) < 60.0, (tip, true_tip)


# ---------------------------------------------------------------------------- #
#  Phase 3 (plan B2): refit cadence cap + predict_fill (the reader's fit hook)
# ---------------------------------------------------------------------------- #
def test_refit_cadence_at_most_every_second_accepted_sample(rp):
    """The fit is load-bearing now [fix]: refit only on accepted samples, at most every 2nd —
    between refits predict() re-reads the cached (t0,T,A) at the newest sample time."""
    p = RegistrationPredictor()
    assert p.enabled, p.load_error
    t, f = _synth(p)
    keep = f < 85.0
    tk, fk = t[keep], f[keep]
    for tt, ff in zip(tk[:-2], fk[:-2]):
        p.update(float(tt), float(ff), present=True, fed=True)
    assert p.predict() is not None
    fit1 = p.last_fit()
    # +1 accepted sample -> cached fit reused (identity, not equality)
    p.update(float(tk[-2]), float(fk[-2]), present=True, fed=True)
    out = p.predict()
    assert out is not None
    assert p.last_fit() is fit1
    # +2nd accepted sample -> refit
    p.update(float(tk[-1]), float(fk[-1]), present=True, fed=True)
    assert p.predict() is not None
    assert p.last_fit() is not fit1
    # gated frames don't advance the cadence counter
    n = p._accepted_since_fit
    p.update(float(tk[-1]) + 16.0, float(fk[-1]), present=True, fed=False)
    assert p._accepted_since_fit == n


def test_predict_fill_tracks_the_template(rp):
    p = RegistrationPredictor()
    assert p.enabled, p.load_error
    t0, T, A, fmin = 1000.0, 400.0, 75.0, 20.0
    t, f = _synth(p, t0=t0, T=T, A=A, fmin=fmin)
    keep = f < 85.0
    for tt, ff in zip(t[keep], f[keep]):
        p.update(float(tt), float(ff), present=True, fed=True)
    assert p.predict() is not None
    t_query = float(t[keep][-1]) + 33.0            # ~2 frames ahead
    fs = p.predict_fill(t_query)
    assert fs is not None
    for key in ("fill", "vel_pp_ms", "sigma_pp", "n", "conf", "age_ms"):
        assert key in fs
    u = (t_query - t0) / T
    true_fill = fmin + A * float(p._g(u))
    assert abs(fs["fill"] - true_fill) < 6.0, (fs["fill"], true_fill)
    assert fs["vel_pp_ms"] > 0.0                   # rising phase
    assert fs["n"] >= 6
    # stale fit (>400ms past the last sample) -> None (the reader must not coast on it)
    assert p.predict_fill(float(t[keep][-1]) + 500.0) is None
    # shot boundary kills the hook
    p.update(float(t[keep][-1]) + 16.0, 0.0, present=False, fed=False)
    assert p.predict_fill(t_query) is None
