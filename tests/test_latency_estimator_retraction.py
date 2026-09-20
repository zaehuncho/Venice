"""2K27 post-top-out RETRACTION must never become a latency label.

Forensics 2026-09-15 (111 banner-graded releases on this rig): `_try_close_oracle` dated the
freeze `t_star` by two different definitions depending on the trace shape --

  * CROSSING: the extrapolated up-crossing of F_stop, used whenever the meter stopped in place
    (n=91, p50 total 217.0ms -- the rig's real release-path latency);
  * ONSET: the first sample of the trailing settled run, used whenever `peak > f_stop + 1.0`
    (n=20, p50 total 269.7ms).

The +46ms split is not latency. On a MISSED shot 2K27 snaps the bar back 4-14px after it tops
out, so the settled run those 20 rows were dated at is the retraction's rest point and the label
carries the retraction animation. It is also reverse-causal: the miss produces the snap-back, the
latency did not change. The deflate gate that exists to reject cap-and-recede traces was bound to
`peak >= 97.0`, unreachable on 2K27's reader scale (live peaks read 88-92), so every one of those
traces fell through `rise_ok` (`peak - f_stop <= 8.0`) as `freeze=rise`, was ACCEPTED, and moved
the posterior `fixed_ms` by 30-47ms -- which is what feeds `measured_latency_ms` ->
`measuredLatencyMs_` -> `lead_auto_seed` and the PRESS-TIP `l_fixed` priors.

These tests pin the scale-relative gate (RETRACTION_TOL_PCT) and the per-shot rise buffer.
"""
import logging

import pytest

from latency_estimator import (
    LatencyEstimator, RETRACTION_TOL_PCT, TICK_WAIT_EXPECT_MS, _retraction_tol_pct,
)

RIG_LATENCY_MS = 217.0          # this rig's crossing-dated population median
DT_MS = 16.0


def _fresh_est(**kwargs):
    """An estimator primed near this rig's measured route so the posterior gate is not the test."""
    params = dict(freeze_win=4, lo_ms=50.0, hi_ms=500.0,
                  post_release_window_ms=900.0,
                  mu_prior_ms=RIG_LATENCY_MS - TICK_WAIT_EXPECT_MS, sd_prior_ms=12.0)
    params.update(kwargs)
    return LatencyEstimator(**params)


def _observations(caplog):
    """Parse every `latency observation` forensic line emitted so far into a list of dicts."""
    rows = []
    for record in caplog.records:
        message = record.getMessage()
        if not message.startswith("latency observation:"):
            continue
        fields = {}
        for token in message.split("latency observation:", 1)[1].split():
            if "=" in token:
                key, value = token.split("=", 1)
                fields[key] = value
        rows.append(fields)
    return rows


def _clean_freeze_shot(est, *, t_rel_ms, latency_ms, f_stop, base, seq=None,
                       rise_duration_ms=180.0, dt=DT_MS, mark=True):
    """Meter rises and STOPS IN PLACE at F_stop `latency_ms` after the release: peak == f_stop.

    This is the crossing-dated population: nothing to retract, so both t* definitions coincide.
    """
    t_cross = t_rel_ms + latency_ms
    slope = (f_stop - 20.0) / float(rise_duration_ms)
    t, fired = base, False
    while t <= t_cross + 320.0:
        if mark and t >= t_rel_ms and not fired:
            est.mark_release(t, seq=seq)
            fired = True
        f = min(f_stop, f_stop - slope * (t_cross - t))
        est.update(t, max(0.0, f), present=True, fed=True)
        t += dt


def _retraction_shot(est, *, t_rel_ms, latency_ms, f_stop, peak, base, seq=None,
                     retract_ms=48.0, rise_duration_ms=180.0, dt=DT_MS):
    """Meter tops out at `peak` then SNAPS BACK to `f_stop` -- the 2K27 missed-shot retraction.

    The trailing settled run therefore begins `retract_ms` after the up-crossing, which is exactly
    the bracket the onset branch used to date `t_star` at.
    """
    t_cross = t_rel_ms + latency_ms
    t_settle = t_cross + retract_ms
    slope = (f_stop - 20.0) / float(rise_duration_ms)
    t, fired = base, False
    while t <= t_settle + 320.0:
        if t >= t_rel_ms and not fired:
            est.mark_release(t, seq=seq)
            fired = True
        if t <= t_cross:                       # fast rising flank
            f = max(0.0, f_stop - slope * (t_cross - t))
        elif t < t_settle:                     # top out, then snap back
            frac = (t - t_cross) / max(1e-6, (t_settle - t_cross))
            f = min(peak, f_stop + (peak - f_stop) * (1.0 - abs(2.0 * frac - 1.0)))
        else:                                  # settled at the retraction's rest point
            f = f_stop
        est.update(t, f, present=True, fed=True)
        t += dt


def _end_shot(est, wall_ms):
    """Meter leaves the screen long enough for the ordinary absence reset."""
    est.update(wall_ms, 0.0, present=False, fed=False)
    est.update(wall_ms + 400.0, 0.0, present=False, fed=False)


# ---------------------------------------------------------------------------------------------
# 1. The clean crossing population is untouched.
# ---------------------------------------------------------------------------------------------

def test_clean_crossing_still_labels_and_its_total_is_identical_to_the_old_gate(monkeypatch,
                                                                               caplog):
    """peak - f_stop <= 1pp: accepted, and byte-for-byte the same total as the pre-fix gate.

    The env knob set to a value no drop can exceed reproduces the OLD behaviour exactly (the gate
    then reduces to `peak >= 97.0`), so this compares the new code against its own legacy branch
    on the identical trace rather than against a remembered number.
    """
    totals = {}
    for arm, override in (("legacy", "99.0"), ("new", None)):
        if override is None:
            monkeypatch.delenv("ORION_LATENCY_RETRACTION_TOL_PCT", raising=False)
        else:
            monkeypatch.setenv("ORION_LATENCY_RETRACTION_TOL_PCT", override)
        est = _fresh_est()
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="latency_estimator"):
            _clean_freeze_shot(est, t_rel_ms=1_000_000.0, latency_ms=RIG_LATENCY_MS,
                               f_stop=89.2, base=1_000_000.0, seq=1)
        rows = _observations(caplog)
        assert len(rows) == 1, rows
        assert rows[0]["accepted"] == "1", rows[0]
        assert rows[0]["reject"] == "-", rows[0]
        assert rows[0]["freeze"] == "rise", rows[0]
        assert est.n_labels == 1
        totals[arm] = float(rows[0]["total_ms"])

    assert totals["new"] == totals["legacy"], totals
    # ...and it is still the injected latency, within the frame quantisation of the trace.
    assert abs(totals["new"] - RIG_LATENCY_MS) <= DT_MS, totals


# ---------------------------------------------------------------------------------------------
# 2. A retraction is rejected and leaves the posterior exactly where it was.
# ---------------------------------------------------------------------------------------------

def test_retraction_after_top_out_is_rejected_and_does_not_move_fixed_ms(caplog):
    """A 6pp snap-back is `reject=deflate_retraction`: no label, `fixed_ms` unchanged."""
    est = _fresh_est()
    base = 2_000_000.0
    for shot in range(3):                       # establish a real posterior first
        t0 = base + shot * 4_000.0
        _clean_freeze_shot(est, t_rel_ms=t0, latency_ms=RIG_LATENCY_MS, f_stop=89.0,
                           base=t0, seq=shot + 1)
        _end_shot(est, t0 + 1_200.0)
    assert est.n_labels == 3, est.last_rejection

    labels_before = est.n_labels
    fixed_before = est.l_fixed_ms
    sd_before = est.l_fixed_sd_ms

    t_ret = base + 20_000.0
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="latency_estimator"):
        _retraction_shot(est, t_rel_ms=t_ret, latency_ms=RIG_LATENCY_MS, f_stop=88.0,
                         peak=94.0, base=t_ret, seq=4)       # 6.0pp / 6.4% retraction

    rows = _observations(caplog)
    assert len(rows) == 1, rows
    assert rows[0]["accepted"] == "0", rows[0]
    assert rows[0]["status"] == "rejected_deflate", rows[0]
    assert rows[0]["reject"] == "deflate_retraction", rows[0]
    assert rows[0]["freeze"] == "deflate", rows[0]
    assert float(rows[0]["peak"]) - float(rows[0]["f_stop"]) > RETRACTION_TOL_PCT

    assert est.n_labels == labels_before
    assert est.l_fixed_ms == fixed_before
    assert est.l_fixed_sd_ms == sd_before
    assert est.freeze_kind == "deflate"
    assert est.last_rejection == "deflate_retraction"


def test_retraction_with_the_gate_disabled_is_still_crossing_dated_not_onset_dated(monkeypatch,
                                                                                   caplog):
    """The knob disables the REJECTION, but it can no longer resurrect the artefact.

    With the tolerance raised past any possible drop the deflate gate collapses to the shipped
    `peak >= 97.0`, so this retraction trace is accepted again as `freeze=rise`. Since 2026-09-16
    the onset branch follows the same tolerance, so the label is dated by the CROSSING: the
    retraction animation is never folded into `total_ms` whatever the knob says. Pre-fix the same
    trace minted a label ~47 ms above the true latency (the forensics' "+45..73 ms spikes").
    """
    monkeypatch.setenv("ORION_LATENCY_RETRACTION_TOL_PCT", "99.0")
    est = _fresh_est()
    base = 2_500_000.0
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="latency_estimator"):
        _retraction_shot(est, t_rel_ms=base, latency_ms=RIG_LATENCY_MS, f_stop=88.0,
                         peak=94.0, base=base, seq=1)
    rows = _observations(caplog)
    assert len(rows) == 1, rows
    assert rows[0]["accepted"] == "1", rows[0]
    assert rows[0]["freeze"] == "rise", rows[0]
    # crossing-dated: within one capture frame of the true latency, no retraction folded in.
    assert abs(float(rows[0]["total_ms"]) - RIG_LATENCY_MS) <= 20.0, rows[0]


# ---------------------------------------------------------------------------------------------
# 3. Each shot measures from its own rise buffer.
# ---------------------------------------------------------------------------------------------

def test_rapid_fire_shot_does_not_inherit_the_previous_shots_peak(caplog):
    """Back-to-back releases with the meter never absent: shot 2 must read its OWN peak.

    Before the fix `_rise` / `_rise_peak` were only cleared after >250ms with no meter, so shot 2
    inherited shot 1's 91% peak. Against shot 2's own 88% freeze that is a 3pp phantom retraction:
    under the new gate it would be thrown away as `deflate_retraction`, and under the old one it
    dated shot 2 at an onset that belongs to a different shot. Arming the marker starts a fresh
    buffer, so neither happens.
    """
    est = _fresh_est()
    base = 3_000_000.0

    # --- shot 1: freezes high, at 91% ---------------------------------------------------------
    _clean_freeze_shot(est, t_rel_ms=base, latency_ms=RIG_LATENCY_MS, f_stop=91.0,
                       base=base, seq=1)
    assert est._rise_peak >= 90.9, "precondition: shot 1 left a high peak in the buffer"

    # --- the meter never leaves for the 250ms the absence reset needs -------------------------
    t = base + RIG_LATENCY_MS + 340.0
    for _ in range(4):
        est.update(t, 91.0, present=True, fed=True)
        t += DT_MS

    # --- shot 2: its own meter, rising from scratch to an 88% freeze --------------------------
    t_rel2 = t + 32.0
    est.mark_release(t_rel2, seq=2)
    assert len(est._rise) == 0, "arming a marker must start this shot's own rise buffer"
    assert est._rise_peak == 0.0

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="latency_estimator"):
        _clean_freeze_shot(est, t_rel_ms=t_rel2, latency_ms=RIG_LATENCY_MS, f_stop=88.0,
                           base=t_rel2, seq=2, mark=False)

    rows = _observations(caplog)
    assert len(rows) == 1, rows
    assert rows[0]["seq"] == "2", rows[0]
    assert rows[0]["accepted"] == "1", rows[0]
    assert rows[0]["freeze"] == "rise", rows[0]
    peak = float(rows[0]["peak"])
    assert abs(peak - 88.0) <= 0.5, f"shot 2 inherited a foreign peak: {peak}"
    assert abs(float(rows[0]["total_ms"]) - RIG_LATENCY_MS) <= DT_MS, rows[0]


# ---------------------------------------------------------------------------------------------
# 4. The legacy ceiling branch is untouched.
# ---------------------------------------------------------------------------------------------

def test_cap_and_recede_at_the_reader_ceiling_still_reports_plain_deflate(caplog):
    """peak >= 97 keeps the original `reject=deflate` string and the original outcome."""
    est = _fresh_est()
    base = 4_000_000.0
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="latency_estimator"):
        _retraction_shot(est, t_rel_ms=base, latency_ms=RIG_LATENCY_MS, f_stop=90.0,
                         peak=99.0, base=base, seq=1)
    rows = _observations(caplog)
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "rejected_deflate", rows[0]
    assert rows[0]["reject"] == "deflate", rows[0]
    assert est.n_labels == 0
    assert est.freeze_kind == "deflate"
    assert est.last_rejection == "deflate"


# ---------------------------------------------------------------------------------------------
# 5. The knob itself.
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("", RETRACTION_TOL_PCT),
    ("   ", RETRACTION_TOL_PCT),
    ("nonsense", RETRACTION_TOL_PCT),
    ("0", RETRACTION_TOL_PCT),
    ("-2", RETRACTION_TOL_PCT),
    ("3.5", 3.5),
])
def test_retraction_tolerance_env_override(monkeypatch, raw, expected):
    """Default 1.5pp; only a finite positive override is honoured, junk falls back."""
    monkeypatch.setenv("ORION_LATENCY_RETRACTION_TOL_PCT", raw)
    assert _retraction_tol_pct() == pytest.approx(expected)
    monkeypatch.delenv("ORION_LATENCY_RETRACTION_TOL_PCT", raising=False)
    assert _retraction_tol_pct() == pytest.approx(RETRACTION_TOL_PCT)
