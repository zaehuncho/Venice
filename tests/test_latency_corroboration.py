"""Post-L1 controlled traces that carry no validation target must REFINE the posterior without
ever manufacturing VALIDATED actuation authority.

Live evidence this pins (logs/orion_native.log, 2026-08-02 block 0): twelve consecutive clean
rise-freezes at 224-271ms (sd ~3.9) arrived as `calibration=1` markers with `target_pct=-1.0` and
were all discarded as `rejected_validation_target_missing`. The estimator finished the block at
n=1 and the engine actuated on the 241.4ms factory seed for the whole session.

The fix demotes those traces to ordinary corroboration labels. The safety property that makes the
demotion legal -- and that these tests exist to hold -- is that corroboration labels are subtracted
out of every count that can promote the authority KIND, so the L1/L2 validation protocol stays the
only route to `validated`.
"""
import numpy as np
import pytest

from latency_estimator import (
    CONVERGED_MAX_SD_MS, CONVERGED_MIN_LABELS, LatencyEstimator,
)

from test_latency_estimator import _drive_probe, _synthetic_shot


def _cold_estimator(**kwargs):
    params = dict(freeze_win=4, lo_ms=15.0, hi_ms=300.0,
                  post_release_window_ms=900.0, restore_cache=False)
    params.update(kwargs)
    return LatencyEstimator(**params)


def _anchor(est, base, latency_ms=245.0):
    """L1: the first controlled marker. Broad, telemetry-only, sets the controlled anchor."""
    _synthetic_shot(est, t_rel_ms=base, latency_ms=latency_ms, f_stop=72.0,
                    base=base, calibration=True, seq=1)
    est.update(base + 4_000.0, 0.0, present=False, fed=False)


def _untargeted_controlled_shot(est, base, latency_ms, seq, f_stop=88.0):
    """A post-L1 `calibration=1` marker that native never named a validation target for."""
    _synthetic_shot(est, t_rel_ms=base, latency_ms=latency_ms, f_stop=f_stop,
                    base=base, calibration=True, seq=seq)
    est.update(base + 4_000.0, 0.0, present=False, fed=False)


# --------------------------------------------------------------------------- #
#  the label is no longer thrown away
# --------------------------------------------------------------------------- #
def test_untargeted_post_l1_controlled_trace_is_ingested_as_corroboration():
    est = _cold_estimator()
    base = 2_000_000.0
    _anchor(est, base)
    assert est.n_labels == 1
    assert est.corroboration_labels == 0
    anchor_mu = est.l_fixed_ms

    _untargeted_controlled_shot(est, base + 5_000.0, latency_ms=225.0, seq=2)

    assert est.n_labels == 2, est.last_status
    assert est.corroboration_labels == 1
    assert est.last_rejection == ""
    assert est.last_status == "corroboration_accepted"
    assert est.label_method == "corroboration"
    # It really moved the posterior -- that is the whole point of not discarding it.
    assert est.l_fixed_ms < anchor_mu - 1.0, (anchor_mu, est.l_fixed_ms)


def test_live_block_shape_produces_a_posterior_instead_of_staying_at_n_equals_one():
    """Replay of the discarded block: L1 + 12 untargeted controlled rise-freezes."""
    est = _cold_estimator()
    base = 3_000_000.0
    _anchor(est, base, latency_ms=247.0)
    observed = [247.3, 247.4, 250.5, 258.4, 252.8, 245.5,
                253.4, 251.2, 251.1, 244.0, 255.0, 248.2]
    for index, latency in enumerate(observed):
        _untargeted_controlled_shot(
            est, base + 5_000.0 * (index + 1), latency_ms=latency, seq=index + 2)

    assert est.n_labels == 1 + len(observed)
    assert est.corroboration_labels == len(observed)
    # The posterior tracks the measured route, not the 60ms static prior.
    assert 235.0 <= est.value_ms() <= 265.0, est.value_ms()


# --------------------------------------------------------------------------- #
#  ...but it can never mint validated authority
# --------------------------------------------------------------------------- #
def test_corroboration_labels_cannot_reach_validated_authority():
    """The safety property. Enough corroboration to satisfy N and SD numerically, and still
    `factory` authority, because none of it is protocol-validated."""
    est = _cold_estimator(boot_prior_ms=241.4,
                          factory_prior_source="capture-card-pipe",
                          factory_prior_version="test",
                          factory_prior_sd_ms=28.7)
    base = 4_000_000.0
    _anchor(est, base, latency_ms=247.0)
    for index in range(3 * CONVERGED_MIN_LABELS):
        _untargeted_controlled_shot(
            est, base + 5_000.0 * (index + 1),
            latency_ms=247.0 + (0.4 if index % 2 else -0.4), seq=index + 2)

    # Preconditions that make this test meaningful: the raw counts and the posterior SD both
    # clear the purely-passive convergence gate. Only the corroboration subtraction stops it.
    assert est.n_labels >= CONVERGED_MIN_LABELS
    assert 0.0 < est.l_fixed_sd_ms <= CONVERGED_MAX_SD_MS, est.l_fixed_sd_ms
    assert est.corroboration_labels == est.n_labels - 1

    assert not est.converged
    assert not est.provisional_ready
    assert not est.ready_for_native
    assert est.authority_kind == "factory"


def test_corroboration_does_not_set_the_validation_latch_or_persist():
    est = _cold_estimator()
    base = 5_000_000.0
    _anchor(est, base, latency_ms=247.0)
    for index in range(4):
        _untargeted_controlled_shot(
            est, base + 5_000.0 * (index + 1), latency_ms=246.0, seq=index + 2)

    assert est.corroboration_labels == 4
    # The L1/L2 latch is untouched: L1 exists, L2 never happened.
    assert est.controlled_anchor_available
    assert est._controlled_validation_ready is False
    # Only validation-capable labels may satisfy the persistence minimum.
    assert est._validation_capable_labels() == 1


def test_corroboration_cannot_claim_calibration_provenance():
    """Defense in depth: a corroboration label that also claimed the controlled/validated
    provenance would be the exact back door this class exists to close."""
    est = _cold_estimator()
    base = 6_000_000.0
    _anchor(est, base, latency_ms=247.0)
    before = est.n_labels

    assert est._ingest_label(246.0, 5.0, calibration=True,
                             corroboration_only=True) is False
    assert est.last_rejection == "corroboration_provenance"
    assert est.n_labels == before
    assert est.corroboration_labels == 0


# --------------------------------------------------------------------------- #
#  the real protocol still works, and still outranks corroboration
# --------------------------------------------------------------------------- #
def test_genuine_l2_validation_still_promotes_after_corroboration():
    est = _cold_estimator()
    base = 7_000_000.0
    _anchor(est, base, latency_ms=235.0)
    _untargeted_controlled_shot(est, base + 5_000.0, latency_ms=232.0, seq=2)
    assert est.corroboration_labels == 1
    assert not est.ready_for_native

    third = base + 10_000.0
    _synthetic_shot(est, t_rel_ms=third, latency_ms=233.0, f_stop=88.0,
                    base=third, calibration=True, seq=3,
                    validation_target_pct=88.0, validation_tolerance_pct=4.0)

    assert est.provisional_ready, est.last_status
    assert est.ready_for_native
    assert est.authority_kind == "validated"
    # The corroboration count is unchanged by the promotion -- L2 is its own evidence.
    assert est.corroboration_labels == 1


def test_corroboration_moves_bounded_factory_authority_off_the_seed():
    """Why the demotion matters for actuation: with `factory` authority the published lead is a
    bounded blend toward the posterior, so corroboration alone gets the engine off the seed."""
    est = _cold_estimator(boot_prior_ms=241.4,
                          factory_prior_source="capture-card-pipe",
                          factory_prior_version="test",
                          factory_prior_sd_ms=28.7)
    assert est.authority_kind == "factory"
    assert est.authority_value_ms == pytest.approx(241.4)

    base = 8_000_000.0
    _anchor(est, base, latency_ms=200.0)
    for index in range(5):
        _untargeted_controlled_shot(
            est, base + 5_000.0 * (index + 1), latency_ms=200.0, seq=index + 2)

    assert est.authority_kind == "factory"
    assert est.corroboration_labels == 5
    moved = abs(float(est.authority_value_ms) - 241.4)
    assert moved > 5.0, (est.authority_value_ms, est.value_ms())
    # ...but never past the posterior it is blending toward.
    assert min(241.4, est.value_ms()) - 1e-6 <= est.authority_value_ms \
        <= max(241.4, est.value_ms()) + 1e-6


# --------------------------------------------------------------------------- #
#  the frozen convergence gate stays where it is
# --------------------------------------------------------------------------- #
def test_probe_spawn_estimate_is_derivable_online_and_never_feeds_the_posterior():
    """[ORION_PROBE] D_spawn is the constant that currently makes every warmup probe a no-op
    (`probe_spawn_offset_ms` ships as 0). Deriving it online turns the offline log-grinding pass
    into a session readout -- but it must stay telemetry."""
    est = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=300.0,
                           post_release_window_ms=900.0, restore_cache=False)
    if est._reg is None:
        pytest.skip("registration template / scipy unavailable")

    base = 9_500_000.0
    for index in range(3):
        shot_at = base + 5_000.0 * index
        _synthetic_shot(est, t_rel_ms=shot_at, latency_ms=100.0, f_stop=88.0,
                        base=shot_at, seq=index + 1)
        est.update(shot_at + 4_000.0, 0.0, present=False, fed=False)
    assert est.n_labels >= 2
    assert est.probe_spawn_estimate_ms < 0.0   # no probes closed yet

    measured_total = float(est.value_ms())
    mu_before, var_before, n_before = est.l_fixed_ms, est._var, est.n_labels

    # Three UNCALIBRATED probes (spawn 0) whose meter appears measured_total + 40ms after press.
    probe_base = base + 40_000.0
    for index in range(3):
        press = probe_base + 3_000.0 * index
        _drive_probe(est, press_ms=press, appear_ms=press + measured_total + 40.0,
                     spawn_offset_ms=0.0)
        est.update(press + 2_000.0, 0.0, present=False, fed=False)

    assert est.probe_spawn_estimate_ms == pytest.approx(40.0, abs=12.0), \
        est.probe_spawn_estimate_ms
    # Telemetry only: uncalibrated probes added no label and moved no posterior state.
    assert est.n_labels == n_before
    assert est.l_fixed_ms == pytest.approx(mu_before)
    assert est._var == pytest.approx(var_before)


def test_probe_spawn_estimate_fails_closed_once_probe_labels_exist():
    """Circularity guard: a posterior that already contains probe labels is no longer an
    independent reference for the constant those labels were computed with."""
    est = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=300.0,
                           post_release_window_ms=900.0, restore_cache=False)
    if est._reg is None:
        pytest.skip("registration template / scipy unavailable")

    base = 9_700_000.0
    for index in range(3):
        press = base + 3_000.0 * index
        _drive_probe(est, press_ms=press, appear_ms=press + 140.0, spawn_offset_ms=40.0)
        est.update(press + 2_000.0, 0.0, present=False, fed=False)

    assert est.n_labels == 3
    assert est._probe_labels == 3
    assert est.probe_spawn_estimate_ms < 0.0


def test_passive_convergence_gate_constants_are_unchanged():
    """CONVERGED_MIN_LABELS/CONVERGED_MAX_SD_MS govern the ONLY promotion path with no causal
    validation behind it. Lowering them would weaken the weakest path; the binding warm-start
    route is provisional_ready (n>=2 plus L1/L2)."""
    assert CONVERGED_MIN_LABELS == 6
    assert CONVERGED_MAX_SD_MS == pytest.approx(3.3)


# --------------------------------------------------------------------------- #
#  the wire N cliff: corroboration must not revoke the engine's lead authority
# --------------------------------------------------------------------------- #
def test_corroboration_labels_do_not_march_the_wire_n_past_the_native_factory_gate():
    """Corroboration must never cost the engine its lead authority mid-session.

    Native spends N at BOTH ends: `factoryPriorStructured` requires measuredLatencyN_ < 6
    (AutomationEngine.cpp) and the passive convergence path requires >= 6. Corroboration labels are
    deliberately excluded from ever reaching `validated`, so if the raw total were published the
    session walks off a cliff: L1 plus five untargeted post-L1 traces pushes the total to 6, the
    factory contract fails because N is no longer < 6, validated is unreachable by construction,
    and the engine is left with NO lead at all -- it stops shooting for the rest of the session.

    That regime is not hypothetical: it is exactly the live block this whole demotion was built
    for (twelve consecutive target-less post-L1 markers).
    """
    from native_orion.backend.autogreen_sidecar import _latency_oracle_wire
    from latency_estimator import try_load

    # A REAL profile-loaded estimator: the cliff only exists when there is factory authority to
    # lose, so a bare LatencyEstimator (authority_kind == "none") would not exercise it.
    est = try_load("console=a|controller=pipe|decoder|wire=orf2:1280x720:i420",
                   restore_cache=False)
    assert est is not None and est.factory_prior_active
    base = 2_000_000.0
    _anchor(est, base)
    for i in range(5):
        _untargeted_controlled_shot(est, base + 5_000.0 * (i + 1),
                                    latency_ms=225.0 + i, seq=2 + i)

    assert est.n_labels == 6, est.last_status
    assert est.corroboration_labels == 5
    assert est._validation_capable_labels() == 1
    assert est.authority_kind == "factory"          # validated is unreachable on this path

    wire = _latency_oracle_wire(est.telemetry_snapshot(), live_rtt_ms=0.0)

    # THE INVARIANT: what native gates on is validation-capable evidence, not the raw total.
    assert wire["measured_latency_n"] == 1
    assert wire["measured_latency_n"] < 6, (
        "wire N reached the native factory gate; the engine would lose lead authority")
    # The real total stays visible, just not as the thing native gates on.
    assert wire["measured_latency_corroboration_n"] == 5
    assert wire["measured_latency_total_n"] == 6
    # And the factory authority the engine actually actuates on survives intact.
    assert wire["measured_latency_authority_kind"] == "factory"
    assert wire["measured_latency_authority_ms"] > 0.0
