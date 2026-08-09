"""Frozen-meter oracle + boot-probe latency estimator: the release-path latency it emits must
recover an injected latency from a synthetic rise->freeze and expose sane phase/bootstrap state."""
import json
import logging
import os
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path

import numpy as np
import pytest

from latency_estimator import (
    LatencyEstimator, LatencyTelemetrySnapshot, PHASE_RISE, PHASE_FROZEN, PHASE_NONE,
    TICK_PERIOD_MS, TICK_WAIT_EXPECT_MS, _load_factory_prior,
    fit_tick_phase, try_load,
)


def _factory_prior_payload():
    """The shipped route-prior artifact, so tests assert its contract instead of its snapshot."""
    path = Path(__file__).resolve().parents[1] / "models" / "latency_factory_prior.json"
    return json.loads(path.read_text(encoding="utf-8"))


class _ObservedRLock:
    """RLock that deterministically reports when one named thread tries to enter."""

    def __init__(self, watched_thread: str, attempted: threading.Event):
        self._inner = threading.RLock()
        self._watched_thread = watched_thread
        self._attempted = attempted

    def __enter__(self):
        if threading.current_thread().name == self._watched_thread:
            self._attempted.set()
        self._inner.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._inner.release()
        return False


def _synthetic_shot(est, t_rel_ms, latency_ms, f_stop=88.0, dt=16.0,
                    base=1_000_000.0, calibration=False,
                    validation_target_pct=None,
                    validation_tolerance_pct=None, rise_duration_ms=180.0,
                    rtt_ms=None, seq=None):
    """Feed a rise that visibly reaches F_stop `latency_ms` after a release at t_rel, then freezes.

    The observed fill crosses F_stop at t_rel + latency_ms (that IS the definition the oracle inverts),
    so the estimator should recover ~latency_ms.
    """
    # rise from ~20% up to F_stop, crossing F_stop exactly at t_rel + latency_ms
    t_cross = t_rel_ms + latency_ms
    slope = (f_stop - 20.0) / float(rise_duration_ms)
    t = base
    fired = False
    while t <= t_cross + 300.0:
        if t >= t_rel_ms and not fired:
            est.mark_release(
                t, seq=seq, rtt_ms=rtt_ms, calibration=calibration,
                validation_target_pct=validation_target_pct,
                validation_tolerance_pct=validation_tolerance_pct)
            fired = True
        f = 20.0 + slope * (t - (t_cross - (f_stop - 20.0) / slope))
        f = min(f, f_stop)              # freeze at F_stop
        est.update(t, f, present=True, fed=True)
        t += dt


def test_recovers_injected_latency():
    est = LatencyEstimator(freeze_win=4, lo_ms=10.0, hi_ms=200.0)
    base = 1_000_000.0
    for k in range(6):
        _synthetic_shot(est, t_rel_ms=base + k * 5000, latency_ms=70.0, base=base + k * 5000)
        # meter leaves between shots (shot boundary)
        est.update(base + k * 5000 + 4000, 0.0, present=False, fed=False)
    assert est.n_labels >= 3
    assert not est.bootstrapped
    # median recovered latency within a couple frames of the 70ms injected
    assert 55.0 <= est.value_ms() <= 90.0, est.value_ms()
    assert 0.0 <= est.confidence() <= 1.0


def test_bootstrap_prior_until_first_label():
    est = LatencyEstimator(boot_prior_ms=45.0)
    assert est.bootstrapped
    assert est.value_ms() == 45.0
    assert est.confidence() < 0.3          # a prior is a weak guess
    # a zero prior emits nothing until measured
    est0 = LatencyEstimator(boot_prior_ms=0.0)
    assert est0.value_ms() == 0.0
    assert est0.confidence() == 0.0


def test_mark_release_cannot_interleave_frame_driven_close(monkeypatch):
    """A close already in progress must finish before the next marker becomes pending.

    This models the real CV-thread versus sidecar-stdin-thread race.  The observed
    lock removes scheduler timing from the test: the close is held at a barrier
    until the marker has definitely attempted to acquire the estimator lock.
    """
    est = LatencyEstimator(restore_cache=False)
    assert est.mark_release(1_000.0, seq=1) == 1

    close_entered = threading.Event()
    allow_close = threading.Event()
    marker_attempted = threading.Event()
    marker_done = threading.Event()
    failures = []
    est._lock = _ObservedRLock("native-marker", marker_attempted)

    def _blocking_close(_wall_ms, _fill):
        close_entered.set()
        if not allow_close.wait(2.0):
            raise AssertionError("test did not release oracle close barrier")
        # This is the final pending-state mutation performed by a close.  If a
        # new marker were allowed to interleave, it would be erased here.
        est._pending_release_ms = None
        est._frozen_captured = True
        est._last_status = "closed_seq_1"

    monkeypatch.setattr(est, "_classify", _blocking_close)

    def _run_update():
        try:
            est.update(1_100.0, 50.0, present=True, fed=True)
        except BaseException as exc:  # surfaced in the asserting thread
            failures.append(exc)

    marker_result = []

    def _run_marker():
        try:
            marker_result.append(est.mark_release(1_200.0, seq=2))
        except BaseException as exc:  # surfaced in the asserting thread
            failures.append(exc)
        finally:
            marker_done.set()

    close_thread = threading.Thread(target=_run_update, name="oracle-close")
    marker_thread = threading.Thread(target=_run_marker, name="native-marker")
    close_thread.start()
    assert close_entered.wait(2.0)
    marker_thread.start()
    assert marker_attempted.wait(2.0)
    assert not marker_done.is_set()  # deterministically blocked behind close
    allow_close.set()
    close_thread.join(2.0)
    marker_thread.join(2.0)

    assert not close_thread.is_alive()
    assert not marker_thread.is_alive()
    assert failures == []
    assert marker_result == [2]
    snapshot = est.telemetry_snapshot()
    assert isinstance(snapshot, LatencyTelemetrySnapshot)
    assert snapshot.pending_release is True
    assert snapshot.pending_release_seq == 2
    assert snapshot.release_seq == 2
    assert snapshot.last_status == "release_pending"
    with pytest.raises(FrozenInstanceError):
        snapshot.n_labels = 99


def test_factory_prior_matches_only_a_known_exact_video_and_controller_scope():
    """Every scope must select its own route profile, read straight from the shipped artifact.

    The numbers live in models/latency_factory_prior.json and are rebuilt by
    tools/timing/build_latency_factory_prior.py, so this asserts the ROUTING contract rather
    than a snapshot of the values -- re-pinning literals here is what let a single hand-copied
    measurement masquerade as four independent ones.
    """
    payload = _factory_prior_payload()
    profiles = {profile["name"]: profile for profile in payload["profiles"]}

    for scope, name in (
        ("console=a|controller=pipe|capture_card|device=0|mode=1080p60", "capture-card-pipe"),
        ("console=a|controller=pipe|decoder|wire=orf2:1280x720:i420", "decoder-pipe"),
        ("console=a|controller=vigem_ds4|capture_card|device=0", "capture-card-vigem"),
        ("console=a|controller=vigem_xusb|decoder|wire=orf2:1280x720:i420", "decoder-vigem"),
    ):
        assert _load_factory_prior(scope) == {
            "mean_ms": profiles[name]["mean_ms"],
            "sd_ms": profiles[name]["sd_ms"],
            "source": f"{payload['model_id']}:{name}",
            "version": payload["model_version"],
        }, scope

    # The one route that has actually been measured must be the most confident one; every
    # never-measured route has to admit strictly more uncertainty than it.
    measured_sd = profiles["capture-card-pipe"]["sd_ms"]
    assert profiles["capture-card-pipe"]["evidence_n"] > 0
    for name in ("decoder-pipe", "capture-card-vigem", "decoder-vigem"):
        assert profiles[name]["evidence_n"] == 0
        assert profiles[name]["sd_ms"] > measured_sd, name

    assert _load_factory_prior("") is None
    assert _load_factory_prior("console=a|controller=unknown|frame_source=auto") is None


def test_try_load_uses_versioned_factory_prior_but_dev_override_has_no_authority(monkeypatch):
    scope = "console=a|controller=pipe|capture_card|device=0|mode=1080p60"
    monkeypatch.setenv("ORION_MEASURE_LATENCY", "1")
    monkeypatch.delenv("ORION_LATENCY_BOOT_MS", raising=False)
    payload = _factory_prior_payload()
    profile = next(p for p in payload["profiles"] if p["name"] == "capture-card-pipe")
    factory = try_load(scope, restore_cache=False)
    assert factory is not None
    assert factory.value_ms() == pytest.approx(profile["mean_ms"])
    assert factory.factory_prior_active
    assert factory.factory_prior_source.endswith(":capture-card-pipe")
    assert factory.factory_prior_version == payload["model_version"]
    assert factory.factory_prior_sd_ms == profile["sd_ms"]

    monkeypatch.setenv("ORION_LATENCY_BOOT_MS", "77.0")
    override = try_load(scope, restore_cache=False)
    assert override is not None
    assert override.value_ms() == pytest.approx(77.0)
    assert not override.factory_prior_active
    assert override.factory_prior_source == ""
    assert override.factory_prior_version == ""
    assert override.factory_prior_sd_ms == 0.0


def test_factory_prior_blends_toward_posterior_without_a_fixed_warmup_batch(monkeypatch):
    """A factory profile is a weak route seed, not a measurement of this machine tonight.

    Ordinary marker-backed labels must be able to pull the PUBLISHED ACTUATION lead toward the
    observed posterior. Freezing it at the seed until a six-label validated batch exists is what
    stranded the 2026-08-03 live session: the estimator had already computed 196.0ms while the
    engine kept actuating on the 241.4ms seed, so every command deadline landed in the past, every
    shot aborted, and an aborted shot emits no release marker -- so label #2 could never arrive.

    The move is bounded and widens with corroboration, so one observation influences the lead but
    never owns it. Authority KIND stays "factory" throughout; only a validated posterior may claim
    that stronger contract.
    """
    scope = "console=a|controller=pipe|decoder|wire=orf2:1280x720:i420"
    monkeypatch.delenv("ORION_LATENCY_BOOT_MS", raising=False)
    est = try_load(scope, restore_cache=False)
    assert est is not None and est.factory_prior_active

    seed_ms = float(est._boot_prior_ms)
    seed_sd = float(est._factory_prior_sd_ms)

    # n == 0: the seed is the whole story, and the published lead is bit-identical to it.
    assert est.authority_value_ms == pytest.approx(seed_ms)
    assert est.authority_sd_ms == pytest.approx(seed_sd)

    previous_allowance = 0.0
    for index, total_ms in enumerate((238.0, 239.0, 240.0, 241.0, 242.0), 1):
        assert est._ingest_label(total_ms, sigma_meas=4.0, rtt_known=False)
        assert est.n_labels == index
        assert est.factory_prior_active
        assert est.authority_kind == "factory"

        authority = est.authority_value_ms
        posterior = est.value_ms()
        allowance = est._authority_move_allowance_ms()

        # Bounded by k(n)*sd, and never overshooting the posterior it is moving toward.
        assert abs(authority - seed_ms) <= allowance + 1e-9
        assert min(seed_ms, posterior) - 1e-9 <= authority <= max(seed_ms, posterior) + 1e-9
        # Corroboration only ever buys more permission to move. (The realised gap is free to
        # shrink whenever the posterior itself drifts back toward the seed, as it does here.)
        assert allowance >= previous_allowance - 1e-9
        previous_allowance = allowance
        # Once the posterior sits inside the allowance the lead follows it exactly -- the clamp
        # is a brake on unproven movement, not a permanent offset.
        if abs(posterior - seed_ms) <= allowance:
            assert authority == pytest.approx(posterior)

        snap = est.telemetry_snapshot()
        assert snap.value_ms == pytest.approx(posterior)
        assert snap.authority_value_ms == pytest.approx(authority)
        assert snap.authority_sd_ms == pytest.approx(est.authority_sd_ms)
        # The published SD is spent directly by the scheduler's combinedSigma gate, so it may
        # tighten with evidence but must never claim to be wider than the seed's own admission.
        assert 0.0 < snap.authority_sd_ms <= seed_sd + 1e-9

    assert est._ingest_label(243.0, sigma_meas=4.0, rtt_known=False)
    assert est.n_labels == 6
    assert not est.factory_prior_active
    assert est.value_ms() != pytest.approx(seed_ms)


def test_a_single_label_moves_the_lead_but_never_owns_it(monkeypatch):
    """The plug-and-play authority rule, stated as an explicit invariant.

    This is the guard against the opposite failure of the frozen-seed bug: handing one noisy
    observation total control of the actuation lead would convert a stream of aborts into a
    stream of badly-timed releases, which is worse because it looks like it is working.
    """
    scope = "console=a|controller=pipe|decoder|wire=orf2:1280x720:i420"
    monkeypatch.delenv("ORION_LATENCY_BOOT_MS", raising=False)
    est = try_load(scope, restore_cache=False)
    assert est is not None and est.factory_prior_active

    seed_ms = float(est._boot_prior_ms)
    # A single label far below the seed -- the exact shape of the live 241.4-vs-193.7
    # disagreement. The distance is DERIVED from the loaded profile's sd (2*sd always exceeds
    # the n=1 move allowance k(1)*sd, k(1)=1.18) so the invariant binds no matter what
    # models/latency_factory_prior.json ships. A hardcoded 48.0 encoded the pre-2026-08-06
    # prior's sd: when the regenerated prior honestly widened (34.5 -> 41.5 on this profile),
    # the allowance grew past 48 and the clamp assertion degenerated into an equality.
    gap_ms = 2.0 * float(est._factory_prior_sd_ms)
    assert est._ingest_label(seed_ms - gap_ms, sigma_meas=4.0, rtt_known=False)
    assert est.n_labels == 1

    authority = est.authority_value_ms
    posterior = est.value_ms()

    assert posterior < authority < seed_ms
    assert authority == pytest.approx(seed_ms - est._authority_move_allowance_ms())
    # Still only factory-kind authority: one observation cannot manufacture the validated contract.
    assert est.authority_kind == "factory"
    assert not est.ready_for_native
def test_controlled_calibration_converges_at_high_measured_latency_without_prior_bias():
    """Six clean controlled early releases at ~275ms must establish measured authority.

    The ordinary passive ceiling is intentionally still 180ms. Calibration uses the same
    rising/non-cap gates but its first accepted sample initializes the posterior from evidence,
    so the static 60ms construction prior cannot reject or pull a high-latency capture path.
    """
    est = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=180.0,
                           post_release_window_ms=900.0, mu_prior_ms=60.0)
    base = 1_500_000.0
    for shot in range(6):
        t0 = base + shot * 5_000.0
        _synthetic_shot(est, t_rel_ms=t0, latency_ms=275.0, f_stop=88.0,
                        base=t0, calibration=True,
                        validation_target_pct=88.0 if shot > 0 else None,
                        validation_tolerance_pct=4.0 if shot > 0 else None)
        est.update(t0 + 4_000.0, 0.0, present=False, fed=False)

    assert est.n_labels == 6
    assert est.ready_for_native
    assert 0.0 < est.l_fixed_sd_ms <= 3.3
    assert est.label_method == "calibration"
    assert est.last_status == "calibration_ready"
    assert est.value_ms() == pytest.approx(275.0, abs=12.0)
    assert est.l_fixed_ms > 230.0  # demonstrably not pulled back toward the 60ms prior


def test_one_clean_controlled_label_is_telemetry_only():
    est = LatencyEstimator()
    est._pending_rtt_known = True
    est._pending_rtt_ms = 12.0

    assert est._ingest_label(72.0, sigma_meas=4.0, calibration=True)
    assert est.n_labels == 1
    assert est.controlled_anchor_available
    assert not est.provisional_ready
    assert not est.ready_for_native
    assert est.value_ms(rtt_ms=12.0) == pytest.approx(72.0)


def test_validated_warm_start_keeps_refining_from_ordinary_live_labels():
    """A cold route needs no fixed setup batch after its causal two-stage validation.

    Later trustworthy gameplay observations update the same posterior even though their marker is
    not tagged as calibration. Authority remains live; there is no six-shot enable gate.
    """
    est = LatencyEstimator()
    assert est._ingest_label(72.0, sigma_meas=4.0,
                             calibration=True, rtt_known=True, rtt_ms=12.0)
    first_fixed = est.l_fixed_ms
    assert est._ingest_label(74.0, sigma_meas=4.0, calibration=True,
                             controlled_validation=True,
                             rtt_known=True, rtt_ms=12.0)
    assert est.ready_for_native

    for total_ms in (76.0, 78.0, 80.0):
        assert est._ingest_label(total_ms, sigma_meas=4.0,
                                 calibration=False, rtt_known=True, rtt_ms=12.0)

    assert est.n_labels == 5
    assert est.ready_for_native
    assert est.l_fixed_ms > first_fixed
    assert est.value_ms(rtt_ms=12.0) > 72.0


def test_controlled_slow_route_allows_high_ordinary_corroboration_and_refinement():
    """A controlled slow-route anchor must not become a permanent one-shot estimate.

    Cold ordinary observations retain the conservative 180ms ceiling. Once native has supplied a
    controlled non-cap marker on this route, later ordinary non-cap consequences use an
    anchor-centered uncertainty envelope (with a hard 500ms outer ceiling).
    """
    cold = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=180.0,
                            post_release_window_ms=900.0)
    base = 1_650_000.0
    _synthetic_shot(cold, t_rel_ms=base, latency_ms=225.0, f_stop=88.0,
                    base=base, calibration=False)
    assert cold.n_labels == 0
    assert cold.last_rejection == "out_of_range"

    proven = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=180.0,
                              post_release_window_ms=900.0)
    _synthetic_shot(proven, t_rel_ms=base, latency_ms=235.0, f_stop=88.0,
                    base=base, calibration=True)
    assert proven.n_labels == 1
    first = proven.value_ms()
    proven.update(base + 4_000.0, 0.0, present=False, fed=False)

    second_base = base + 5_000.0
    _synthetic_shot(proven, t_rel_ms=second_base, latency_ms=220.0, f_stop=88.0,
                    base=second_base, calibration=False)
    assert proven.n_labels == 2
    assert proven.value_ms() < first
    assert 215.0 <= proven.value_ms() <= 235.0

    before_far = proven.n_labels
    far_base = second_base + 5_000.0
    _synthetic_shot(proven, t_rel_ms=far_base, latency_ms=420.0, f_stop=88.0,
                    base=far_base, calibration=False)
    assert proven.n_labels == before_far
    assert proven.last_rejection == "out_of_range"


def test_distinct_planned_stop_validation_is_required_for_provisional_authority():
    est = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=180.0,
                           post_release_window_ms=900.0)
    base = 1_700_000.0
    _synthetic_shot(est, t_rel_ms=base, latency_ms=235.0, f_stop=72.0,
                    base=base, calibration=True)
    assert est.n_labels == 1
    assert est.controlled_anchor_available
    assert not est.provisional_ready

    second = base + 5_000.0
    _synthetic_shot(est, t_rel_ms=second, latency_ms=220.0, f_stop=88.0,
                    base=second, calibration=True,
                    validation_target_pct=88.0,
                    validation_tolerance_pct=4.0)
    assert est.n_labels == 2
    assert est.provisional_ready
    assert est.ready_for_native


def test_validation_stop_residual_and_latency_agreement_fail_closed():
    residual = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=180.0,
                                post_release_window_ms=900.0)
    base = 1_750_000.0
    _synthetic_shot(residual, t_rel_ms=base, latency_ms=235.0, f_stop=72.0,
                    base=base, calibration=True)
    _synthetic_shot(residual, t_rel_ms=base + 5_000.0, latency_ms=220.0,
                    f_stop=80.0, base=base + 5_000.0, calibration=True,
                    validation_target_pct=88.0,
                    validation_tolerance_pct=4.0)
    assert residual.n_labels == 1
    assert not residual.provisional_ready
    assert residual.last_rejection == "validation_stop_residual"

    disagreement = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=180.0,
                                    post_release_window_ms=900.0)
    _synthetic_shot(disagreement, t_rel_ms=base, latency_ms=235.0, f_stop=72.0,
                    base=base, calibration=True)
    _synthetic_shot(disagreement, t_rel_ms=base + 5_000.0, latency_ms=350.0,
                    f_stop=88.0, base=base + 5_000.0, calibration=True,
                    validation_target_pct=88.0,
                    validation_tolerance_pct=4.0)
    assert disagreement.n_labels == 1
    assert not disagreement.provisional_ready
    assert disagreement.last_rejection == "out_of_range"


def test_clean_controlled_noncap_miss_refines_only_anchor_then_requires_new_validation():
    """A causal rising/non-cap L2 miss may fix the next plan, but cannot validate itself."""
    est = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=180.0,
                           post_release_window_ms=900.0)
    base = 1_780_000.0
    _synthetic_shot(est, t_rel_ms=base, latency_ms=235.0, f_stop=72.0,
                    base=base, calibration=True)
    anchor_ms = est.value_ms()
    anchor_sd_ms = est.l_fixed_sd_ms
    assert est.n_labels == 1
    est.update(base + 4_000.0, 0.0, present=False, fed=False)

    miss_base = base + 5_000.0
    _synthetic_shot(est, t_rel_ms=miss_base, latency_ms=220.0, f_stop=80.0,
                    base=miss_base, calibration=True,
                    validation_target_pct=88.0,
                    validation_tolerance_pct=4.0)

    assert est.n_labels == 1
    assert est.value_ms() < anchor_ms
    assert est.l_fixed_sd_ms >= anchor_sd_ms
    assert est.controlled_anchor_available
    assert not est.provisional_ready
    assert not est.ready_for_native
    assert est.last_status == "calibration_anchor_refined_retry_required"
    assert est.last_rejection == "validation_stop_residual"

    # Native consumes the revised telemetry only to plan a distinct retry. It becomes provisional
    # authority solely after that new marker visibly lands on its own planned, non-cap stop.
    est.update(miss_base + 4_000.0, 0.0, present=False, fed=False)
    retry_base = base + 10_000.0
    revised_plan_ms = est.value_ms()
    _synthetic_shot(est, t_rel_ms=retry_base, latency_ms=revised_plan_ms,
                    f_stop=88.0, base=retry_base, calibration=True,
                    validation_target_pct=88.0,
                    validation_tolerance_pct=4.0)

    assert est.n_labels == 2
    assert est.provisional_ready
    assert est.ready_for_native
    assert est.last_status == "calibration_ready"
    assert est.last_rejection == ""


def test_repeated_clean_residual_misses_cannot_accumulate_labels_or_readiness():
    est = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=180.0,
                           post_release_window_ms=900.0)
    base = 1_800_000.0
    _synthetic_shot(est, t_rel_ms=base, latency_ms=235.0, f_stop=72.0,
                    base=base, calibration=True)
    est.update(base + 4_000.0, 0.0, present=False, fed=False)

    first_miss = base + 5_000.0
    _synthetic_shot(est, t_rel_ms=first_miss, latency_ms=220.0, f_stop=80.0,
                    base=first_miss, calibration=True,
                    validation_target_pct=88.0,
                    validation_tolerance_pct=4.0)
    refined_ms = est.value_ms()
    refined_sd_ms = est.l_fixed_sd_ms
    assert est.n_labels == 1

    # A second equally clean miss cannot repeatedly walk/shrink the posterior. It remains one
    # unvalidated label no matter how many failed controlled markers follow.
    for attempt in range(3):
        est.update(first_miss + attempt * 5_000.0 + 4_000.0, 0.0,
                   present=False, fed=False)
        retry_base = first_miss + (attempt + 1) * 5_000.0
        _synthetic_shot(est, t_rel_ms=retry_base,
                        latency_ms=refined_ms + 15.0, f_stop=94.0,
                        base=retry_base, calibration=True,
                        validation_target_pct=88.0,
                        validation_tolerance_pct=4.0)
        assert est.n_labels == 1
        assert est.value_ms() == pytest.approx(refined_ms)
        assert est.l_fixed_sd_ms == pytest.approx(refined_sd_ms)
        assert not est.provisional_ready
        assert not est.ready_for_native
        assert est.last_status == "rejected_validation_recovery_exhausted"


def test_controlled_miss_recovery_rejects_gross_out_of_range_and_noisy_evidence():
    base = 1_820_000.0

    gross = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=180.0,
                             post_release_window_ms=900.0)
    _synthetic_shot(gross, t_rel_ms=base, latency_ms=235.0, f_stop=72.0,
                    base=base, calibration=True)
    gross_before = gross.value_ms()
    gross.update(base + 4_000.0, 0.0, present=False, fed=False)
    _synthetic_shot(gross, t_rel_ms=base + 5_000.0, latency_ms=255.0,
                    f_stop=94.0, base=base + 5_000.0, calibration=True,
                    validation_target_pct=70.0,
                    validation_tolerance_pct=4.0)
    assert gross.n_labels == 1
    assert gross.value_ms() == pytest.approx(gross_before)
    assert gross.last_status == "rejected_validation_recovery_gross_residual"
    assert not gross.ready_for_native

    out_of_range = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=180.0,
                                    post_release_window_ms=900.0)
    assert out_of_range._ingest_label(490.0, sigma_meas=5.0, calibration=True)
    out_of_range_before = out_of_range.value_ms()
    _synthetic_shot(out_of_range, t_rel_ms=base + 15_000.0, latency_ms=520.0,
                    f_stop=90.0, base=base + 15_000.0, calibration=True,
                    validation_target_pct=80.0,
                    validation_tolerance_pct=4.0)
    assert out_of_range.n_labels == 1
    assert out_of_range.value_ms() == pytest.approx(out_of_range_before)
    assert out_of_range.last_status == "rejected_validation_recovery_out_of_range"
    assert not out_of_range.ready_for_native

    noisy = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=180.0,
                             post_release_window_ms=900.0)
    _synthetic_shot(noisy, t_rel_ms=base + 25_000.0, latency_ms=235.0,
                    f_stop=72.0, base=base + 25_000.0, calibration=True)
    noisy_before = noisy.value_ms()
    noisy.update(base + 29_000.0, 0.0, present=False, fed=False)
    _synthetic_shot(noisy, t_rel_ms=base + 30_000.0, latency_ms=255.0,
                    f_stop=94.0, base=base + 30_000.0, calibration=True,
                    validation_target_pct=88.0,
                    validation_tolerance_pct=4.0,
                    rise_duration_ms=600.0)
    assert noisy.n_labels == 1
    assert noisy.value_ms() == pytest.approx(noisy_before)
    assert noisy.last_status == "rejected_validation_recovery_noisy"
    assert not noisy.ready_for_native


def test_controlled_miss_recovery_cannot_mix_unknown_rtt_into_decomposed_anchor():
    est = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=180.0,
                           post_release_window_ms=900.0)
    base = 1_880_000.0
    _synthetic_shot(est, t_rel_ms=base, latency_ms=235.0, f_stop=72.0,
                    base=base, calibration=True, rtt_ms=12.0)
    before = est.value_ms()
    est.update(base + 4_000.0, 0.0, present=False, fed=False)

    _synthetic_shot(est, t_rel_ms=base + 5_000.0, latency_ms=255.0,
                    f_stop=94.0, base=base + 5_000.0, calibration=True,
                    validation_target_pct=88.0,
                    validation_tolerance_pct=4.0, rtt_ms=None)

    assert est.n_labels == 1
    assert est.value_ms() == pytest.approx(before)
    assert est.last_status == "rejected_validation_recovery_rtt_unavailable"
    assert est.last_rejection == "rtt_unavailable"
    assert not est.ready_for_native


def _assert_unvalidated_controlled_epoch_was_fully_revoked(est):
    assert est.n_labels == 0
    assert est.bootstrapped
    assert est.value_ms() == 0.0
    assert est.l_fixed_ms == pytest.approx(60.0)
    assert est.l_fixed_sd_ms == pytest.approx(12.0)
    assert list(est._labels) == []
    assert list(est._fixed_accepted) == []
    assert list(est._rtt_used) == []
    assert est.rtt_regime == "unlocked"
    assert est._pending_rtt_ms == 0.0
    assert not est._pending_rtt_known
    assert not est.controlled_anchor_available
    assert not est.provisional_ready
    assert not est.ready_for_native


def test_repeated_controlled_near_cap_failures_reset_cold_without_accumulating(tmp_path):
    cache = tmp_path / "unvalidated-latency.bin"
    est = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=180.0,
                           post_release_window_ms=900.0,
                           route_scope="test-capture-route", cache_path=str(cache))
    base = 1_920_000.0

    for cycle in range(2):
        l1_base = base + cycle * 10_000.0
        _synthetic_shot(est, t_rel_ms=l1_base, latency_ms=235.0, f_stop=72.0,
                        base=l1_base, calibration=True, rtt_ms=12.0,
                        seq=cycle * 2 + 1)
        assert est.n_labels == 1
        assert est.controlled_anchor_available
        assert not est.ready_for_native
        assert not cache.exists()  # N=1 never becomes a persisted warm-start authority.
        cache.write_bytes(b"stale-route-cache")
        est.update(l1_base + 4_000.0, 0.0, present=False, fed=False)

        l2_base = l1_base + 5_000.0
        _synthetic_shot(est, t_rel_ms=l2_base, latency_ms=255.0, f_stop=98.0,
                        base=l2_base, calibration=True, rtt_ms=12.0,
                        validation_target_pct=90.0,
                        validation_tolerance_pct=4.0,
                        seq=cycle * 2 + 2)
        assert est.last_status == "rejected_near_cap"
        assert est.last_rejection == "near_cap"
        _assert_unvalidated_controlled_epoch_was_fully_revoked(est)
        assert not cache.exists()
        est.update(l2_base + 4_000.0, 0.0, present=False, fed=False)

    # Marker identity/anti-replay state is deliberately outside the revoked timing posterior.
    assert est.release_seq == 4
    assert est._last_native_release_seq == 4


@pytest.mark.parametrize("terminal", ["deflate", "no_freeze"])
def test_unsafe_controlled_l2_terminal_resets_entire_epoch_and_preserves_reason(terminal):
    est = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=180.0,
                           post_release_window_ms=500.0)
    base = 1_980_000.0
    _synthetic_shot(est, t_rel_ms=base, latency_ms=235.0, f_stop=72.0,
                    base=base, calibration=True, rtt_ms=12.0, seq=1)
    assert est.n_labels == 1
    est.update(base + 4_000.0, 0.0, present=False, fed=False)

    l2_base = base + 5_000.0
    assert est.mark_release(l2_base + 100.0, seq=2, rtt_ms=12.0,
                            calibration=True, validation_target_pct=88.0,
                            validation_tolerance_pct=4.0) == 2
    if terminal == "no_freeze":
        est.update(l2_base + 601.0, 0.0, present=False, fed=False)
    else:
        t = 0.0
        for f in range(20, 101, 6):
            est.update(l2_base + t, float(f), present=True, fed=True)
            t += 16.0
        for _ in range(3):
            est.update(l2_base + t, 100.0, present=True, fed=True)
            t += 16.0
        for f in range(95, 59, -6):
            est.update(l2_base + t, float(f), present=True, fed=True)
            t += 16.0
        for _ in range(6):
            est.update(l2_base + t, 60.0, present=True, fed=True)
            t += 16.0

    assert est.last_status == f"rejected_{terminal}"
    assert est.last_rejection == terminal
    _assert_unvalidated_controlled_epoch_was_fully_revoked(est)
    assert est.release_seq == 2
    assert est._last_native_release_seq == 2

    # With the stale epoch gone, a new calibration marker without L2 metadata is a genuine L1.
    next_base = base + 10_000.0
    est.update(next_base - 1_000.0, 0.0, present=False, fed=False)
    _synthetic_shot(est, t_rel_ms=next_base, latency_ms=240.0, f_stop=72.0,
                    base=next_base, calibration=True, rtt_ms=12.0, seq=3)
    assert est.n_labels == 1
    assert est.controlled_anchor_available
    assert not est.provisional_ready
    assert not est.ready_for_native
    assert est.release_seq == 3
    assert est._last_native_release_seq == 3


def _replay_cap_deflate(est, *, t_rel_ms, fill_at_release, vel_pct_per_ms, peak, settle,
                        base, dt=1000.0 / 60.0, seq=None):
    """Replay a live cap+recede trace: rise -> peak near the ceiling -> recede -> flat settle."""
    t = base
    fill = fill_at_release - vel_pct_per_ms * (t_rel_ms - base)
    fired = False
    while fill < peak - 1e-9:
        if t >= t_rel_ms and not fired:
            est.mark_release(t, seq=seq)
            fired = True
        est.update(t, min(fill, peak), present=True, fed=True)
        fill += vel_pct_per_ms * dt
        t += dt
    est.update(t, peak, present=True, fed=True)
    t += dt
    # the recede, then the settle plateau the freeze detector actually latches onto
    for step in (0.75, 0.5, 0.25):
        est.update(t, settle + (peak - settle) * step, present=True, fed=True)
        t += dt
    for _ in range(8):
        est.update(t, settle, present=True, fed=True)
        t += dt


def test_live_cap_deflate_trace_is_rejected_not_inverted_from_its_settle_value():
    """The 2026-08-03 cap+recede traces must stay rejected: their settle is not our freeze.

    Live epoch 5 (release 02:48:57.618Z, seq=2) read f_stop=90.6 peak=98.1 at vel=0.1879 pct/ms.
    It is tempting to read that settle as an EARLY release (100 - 47*0.1879 = 91.2 ~ 90.6) and
    invert it into a label instead of discarding it. The session evidence says otherwise:

      * that shot did not fire on the 241.4ms factory lead at all -- its own release marker logs
        measured_latency_ms=196.0, i.e. the engine had already adopted the 193.7ms posterior from
        seq=1, so the release landed within ~2ms of its target and the target was fill=100.0%;
      * across the session f_stop on these traces does not respond to the actuation lead. Holding
        the shot mix fixed and moving the lead 241.4 -> 190.9ms, a release-controlled freeze must
        move by -vel per ms of lead (-0.198 pct/ms). Measured: deflate +0.072, near_cap -0.023,
        ambiguous +0.269 pct/ms -- all the wrong sign or ~zero;
      * with the lead pinned at 241.4ms the deflate settle scatters 5.50pp (=28.4ms of implied
        latency) against 10.7ms for the clean rise-freezes of the same session.

    So the settle is not release-controlled and inverting it would fabricate a label that moves
    the actuation lead. Rejecting it is correct; only the sub-ceiling rise-freeze is invertible.
    """
    # The prior is seeded near the route's measured value so that what this test exercises is the
    # freeze classifier, not the posterior outlier gate.
    est = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=280.0,
                           post_release_window_ms=900.0, mu_prior_ms=190.0)
    base = 3_100_000.0
    _replay_cap_deflate(est, t_rel_ms=base + 200.0, fill_at_release=48.6,
                        vel_pct_per_ms=0.1879, peak=98.1, settle=90.6, base=base, seq=1)

    assert est.last_status == "rejected_deflate"
    assert est.last_rejection == "deflate"
    assert est.freeze_kind == "deflate"
    assert est.n_labels == 0

    # ...and the discrimination is real: the same estimator still takes the genuine sub-ceiling
    # rise-freeze that seq=1 of that very block produced (f_stop 89.1 ~ peak 90.0 -> 193.7ms).
    est.update(base + 4_000.0, 0.0, present=False, fed=False)
    clean_base = base + 5_000.0
    _synthetic_shot(est, t_rel_ms=clean_base + 100.0, latency_ms=193.7, f_stop=89.1,
                    base=clean_base, seq=2)
    assert est.n_labels == 1
    assert est.freeze_kind == "rise"


def test_no_frame_controlled_l2_is_cold_reset_before_next_marker_can_supersede_it(tmp_path):
    cache = tmp_path / "superseded-unvalidated.bin"
    est = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=180.0,
                           post_release_window_ms=900.0,
                           route_scope="test-supersede-route", cache_path=str(cache))
    base = 2_040_000.0

    for cycle in range(2):
        l1_seq = cycle * 3 + 1
        l2_seq = l1_seq + 1
        superseding_l2_seq = l1_seq + 2
        l1_base = base + cycle * 12_000.0
        _synthetic_shot(est, t_rel_ms=l1_base, latency_ms=235.0, f_stop=72.0,
                        base=l1_base, calibration=True, rtt_ms=12.0, seq=l1_seq)
        assert est.n_labels == 1
        assert est.controlled_anchor_available
        cache.write_bytes(b"stale-route-cache")
        est.update(l1_base + 4_000.0, 0.0, present=False, fed=False)

        # Open a real L2 opportunity, then deliver no frames at all before the next stale L2
        # marker. Supersede handling must perform the expiry/reset that update() never could.
        assert est.mark_release(l1_base + 5_000.0, seq=l2_seq, rtt_ms=12.0,
                                calibration=True, validation_target_pct=88.0,
                                validation_tolerance_pct=4.0) == l2_seq
        assert est.mark_release(l1_base + 6_000.0, seq=superseding_l2_seq,
                                rtt_ms=12.0, calibration=True,
                                validation_target_pct=88.0,
                                validation_tolerance_pct=4.0) == 0
        assert est.last_status == "rejected_superseded"
        assert est.last_rejection == "no_freeze"
        _assert_unvalidated_controlled_epoch_was_fully_revoked(est)
        assert est._last_native_release_seq == superseding_l2_seq
        assert not cache.exists()

        # The superseding validation marker was consumed, not reinterpreted as a new L1. Only a
        # later no-target marker on the next physical epoch may establish the replacement anchor.
        if cycle == 0:
            est.update(l1_base + 9_000.0, 0.0, present=False, fed=False)

    assert est.release_seq == 4  # two accepted L1s + two accepted L2 opportunities
    assert est._last_native_release_seq == 6
    assert not est.ready_for_native


def test_release_marker_replay_stale_and_epoch_reset_fail_closed(monkeypatch):
    # Native marker timestamps and sidecar arrival time share the host wall clock. Keep the
    # synthetic epoch deterministic so the future-marker guard tests that clock, rather than
    # incorrectly treating the previous shot's final frame as "now".
    monkeypatch.setattr("latency_estimator.time.time", lambda: 10.0)
    est = LatencyEstimator(post_release_window_ms=500.0)
    est.update(10_000.0, 20.0, present=True, fed=True)
    assert est.mark_release(9_990.0, seq=7) == 7
    pending_wall = est._pending_release_ms

    assert est.mark_release(9_995.0, seq=7) == 0
    assert est.last_rejection == "replayed_marker"
    assert est._pending_release_seq == 7
    assert est._pending_release_ms == pending_wall

    assert est.mark_release(9_499.0, seq=8) == 0
    assert est.last_rejection == "stale_marker"
    assert est._pending_release_seq == 7

    assert est.mark_release(10_501.0, seq=8) == 0
    assert est.last_rejection == "future_marker"
    assert est._pending_release_seq == 7

    fresh_epoch = LatencyEstimator(post_release_window_ms=500.0)
    assert fresh_epoch.mark_release(10_100.0, seq=1) == 1


def test_validation_metadata_without_anchor_or_complete_tolerance_is_rejected():
    est = LatencyEstimator()
    assert est.mark_release(10_000.0, seq=1, calibration=True,
                            validation_target_pct=88.0,
                            validation_tolerance_pct=4.0) == 0
    assert est.last_rejection == "invalid_validation_marker"
    assert est._pending_release_ms is None

    est._ingest_label(72.0, sigma_meas=4.0, calibration=True)
    assert est.mark_release(11_000.0, seq=2, calibration=True,
                            validation_target_pct=88.0) == 0
    assert est.last_rejection == "invalid_validation_marker"


def test_pending_release_without_freeze_expires_with_diagnostic_status():
    est = LatencyEstimator(post_release_window_ms=500.0)
    est.mark_release(1_000.0, seq=7, calibration=False)
    est.update(1_501.0, 0.0, present=False, fed=False)

    assert est.n_labels == 0
    assert est.last_status == "rejected_no_freeze"
    assert est.last_rejection == "no_freeze"


def test_release_observation_forensic_line_emits_with_controlled_provenance(caplog):
    est = LatencyEstimator()
    est._pending_release_seq = 17
    est._pending_release_calibration = True
    est._has_controlled_anchor = True
    est._last_status = "calibration_ready"
    est._last_freeze_kind = "rise"
    est._last_freeze_f_stop = 88.0
    est._last_freeze_peak = 88.0

    with caplog.at_level(logging.INFO, logger="latency_estimator"):
        est._log_release_observation(True, 72.0, 5.0)

    line = next(record.message for record in caplog.records
                if record.name == "latency_estimator"
                and record.message.startswith("latency observation:"))
    assert "seq=17" in line
    assert "freeze=rise" in line
    assert "controlled=1" in line


def test_ordinary_or_noisy_single_label_cannot_grant_warm_authority():
    ordinary = LatencyEstimator()
    ordinary._pending_rtt_known = True
    ordinary._pending_rtt_ms = 12.0
    assert ordinary._ingest_label(72.0, sigma_meas=4.0, calibration=False)
    assert not ordinary.provisional_ready
    assert not ordinary.ready_for_native

    noisy = LatencyEstimator()
    noisy._pending_rtt_known = True
    noisy._pending_rtt_ms = 12.0
    assert noisy._ingest_label(72.0, sigma_meas=6.1, calibration=True)
    assert not noisy.provisional_ready
    assert not noisy.ready_for_native


@pytest.mark.skipif(os.name != "nt", reason="production cache is Windows DPAPI-only")
def test_controlled_total_route_cache_restores_provisional_only_after_video_attestation(tmp_path):
    cache = tmp_path / "latency.bin"
    scope = "console=a|controller=pipe|capture_card|device=0|mode=1080p60"
    est = LatencyEstimator(route_scope=scope, cache_path=str(cache))
    assert est._ingest_label(70.0, sigma_meas=4.0,
                             calibration=True, rtt_known=False)
    assert est._ingest_label(72.0, sigma_meas=4.0, calibration=True,
                             controlled_validation=True, rtt_known=False)
    assert est.ready_for_native
    assert cache.is_file()

    restored = LatencyEstimator(route_scope=scope, cache_path=str(cache))
    assert restored.n_labels == 2
    assert restored.label_method == "persisted_total"
    assert restored.restored_from_cache
    assert not restored.restored_route_attested
    assert restored.value_ms() == 0.0
    assert restored.value_ms(rtt_ms=18.0) == 0.0
    assert not restored.ready_for_native
    assert restored.set_restored_route_attested(True)
    assert restored.value_ms() == pytest.approx(
        restored.l_fixed_ms + TICK_WAIT_EXPECT_MS)
    assert restored.ready_for_native
    assert restored.provisional_ready

    wrong_route = LatencyEstimator(route_scope=scope + "|different",
                                   cache_path=str(cache))
    assert wrong_route.n_labels == 0
    assert wrong_route.value_ms() == 0.0


@pytest.mark.skipif(os.name != "nt", reason="production cache is Windows DPAPI-only")
def test_uncontrolled_or_decomposed_posterior_is_never_persisted(tmp_path):
    cache = tmp_path / "latency.bin"
    est = LatencyEstimator(route_scope="decoder|uncontrolled", cache_path=str(cache))
    for _ in range(6):
        assert est._ingest_label(70.0, sigma_meas=4.0,
                                 rtt_known=False, rtt_ms=None)
    assert est.ready_for_native
    assert est.rtt_regime == "total"
    assert not cache.exists()

    decomposed_cache = tmp_path / "decomposed.bin"
    decomposed = LatencyEstimator(
        route_scope="decoder|decomposed", cache_path=str(decomposed_cache))
    assert decomposed._ingest_label(
        70.0, sigma_meas=4.0, calibration=True, rtt_known=True, rtt_ms=10.0)
    assert decomposed._ingest_label(
        70.0, sigma_meas=4.0, calibration=True, controlled_validation=True,
        rtt_known=True, rtt_ms=10.0)
    assert not decomposed_cache.exists()


@pytest.mark.skipif(os.name != "nt", reason="production cache is Windows DPAPI-only")
def test_total_cache_tamper_age_transition_and_contradiction_fail_closed(tmp_path, monkeypatch):
    scope = "console=a|controller=pipe|decoder|hash=a"

    def _write(path):
        est = LatencyEstimator(route_scope=scope, cache_path=str(path))
        assert est._ingest_label(
            70.0, sigma_meas=4.0, calibration=True, rtt_known=False)
        assert est._ingest_label(
            72.0, sigma_meas=4.0, calibration=True, controlled_validation=True,
            rtt_known=False)
        assert path.is_file()
        return est

    corrupt_path = tmp_path / "corrupt.bin"
    _write(corrupt_path)
    blob = bytearray(corrupt_path.read_bytes())
    blob[-1] ^= 0x5A
    corrupt_path.write_bytes(bytes(blob))
    corrupt = LatencyEstimator(route_scope=scope, cache_path=str(corrupt_path))
    assert corrupt.n_labels == 0
    assert corrupt.value_ms() == 0.0

    stale_path = tmp_path / "stale.bin"
    now = 50_000.0
    monkeypatch.setattr("latency_estimator.time.time", lambda: now)
    _write(stale_path)
    monkeypatch.setattr("latency_estimator.time.time", lambda: now + 61.0)
    stale = LatencyEstimator(
        route_scope=scope, cache_path=str(stale_path), cache_max_age_s=60.0)
    assert stale.n_labels == 0

    route_path = tmp_path / "route.bin"
    monkeypatch.setattr("latency_estimator.time.time", lambda: now)
    _write(route_path)
    transitioned = LatencyEstimator(route_scope=scope, cache_path=str(route_path))
    assert transitioned.set_restored_route_attested(True)
    assert transitioned.ready_for_native
    assert transitioned.revoke_restored_route("backend_transition")
    assert transitioned.n_labels == 0
    assert transitioned.value_ms() == 0.0
    assert route_path.exists()

    contradictory = LatencyEstimator(route_scope=scope, cache_path=str(route_path))
    assert contradictory.set_restored_route_attested(True)
    assert not contradictory._ingest_label(130.0, sigma_meas=4.0, rtt_known=False)
    assert not contradictory.restored_from_cache
    assert contradictory.n_labels == 0
    assert not contradictory.ready_for_native
    assert not route_path.exists()


@pytest.mark.skipif(os.name != "nt", reason="production cache is Windows DPAPI-only")
def test_cache_has_no_plaintext_fallback_when_dpapi_is_unavailable(tmp_path, monkeypatch):
    cache = tmp_path / "latency.bin"
    monkeypatch.setattr("latency_estimator._dpapi_transform", lambda *_a, **_k: None)
    est = LatencyEstimator(route_scope="exact-route", cache_path=str(cache))
    assert est._ingest_label(
        70.0, sigma_meas=4.0, calibration=True, rtt_known=False)
    assert est._ingest_label(
        72.0, sigma_meas=4.0, calibration=True, controlled_validation=True,
        rtt_known=False)
    assert not cache.exists()


def test_online_posterior_tracks_sustained_fixed_path_shift_without_single_outlier_jump():
    est = LatencyEstimator()
    for _ in range(6):
        assert est._ingest_label(70.0, sigma_meas=4.0,
                                 rtt_known=True, rtt_ms=10.0)
    baseline = est.l_fixed_ms

    # One isolated jump remains rejected. Two same-side measurements open the existing regime
    # escape, after which bounded process noise keeps later clean evidence influential.
    assert not est._ingest_label(95.0, sigma_meas=4.0,
                                 rtt_known=True, rtt_ms=10.0)
    assert est.l_fixed_ms == pytest.approx(baseline)
    assert est._ingest_label(95.0, sigma_meas=4.0,
                             rtt_known=True, rtt_ms=10.0)
    for _ in range(4):
        assert est._ingest_label(95.0, sigma_meas=4.0,
                                 rtt_known=True, rtt_ms=10.0)
    assert est.l_fixed_ms > baseline + 15.0
    assert est.ready_for_native


# ---- [ORION_REOPEN_SOFT] regime-reopen soft landing (flag-gated, default OFF) -------------------
#
# The replayed numbers are the measured 2026-08-06T14:27Z episode that benched the bot for the
# last two shots (physical epochs 50/51) of the 46/50 counted batch: a converged posterior at
# fixed ~221.6 sd 2.3 took seq=47 total=329.1 (rejected_outlier) then seq=48 total=272.9 (second
# same-side reject -> regime escape force-accept), the posterior reopened to fixed 250.8 sd 5.8,
# ready_for_native dropped, and native logged IDLE-GATE reason=waiting_for_latency_calibration on
# both presses. Convergence at 8x total=229.9 sigma=5.5 reproduces the pre-episode posterior
# (fixed ~219.6, sd ~2.35) and the forced accept lands at fixed ~250 sd ~5.85, matching the log.

def _reopen_estimator(**kwargs):
    # The live fresh-estimator prior (every 2026-08-05/06 session logs "fixed_ms=210.2
    # sd_ms=28.7 n=0"); without it the first ~221.6 fixed label is 161ms from the synthetic
    # 60ms default prior and the cold outlier gate rejects the whole warmup.
    return LatencyEstimator(mu_prior_ms=210.2, sd_prior_ms=28.7, **kwargs)


def _converge_total_posterior(est, total_ms=229.9, sigma=5.5, n=8):
    for _ in range(n):
        assert est._ingest_label(total_ms, sigma_meas=sigma)
    assert est.ready_for_native
    assert est.last_status == "ready"


def test_regime_reopen_soft_keeps_authority_through_the_batch_outlier_pair():
    est = _reopen_estimator(regime_reopen_soft=True)
    _converge_total_posterior(est)
    fixed_before = est.l_fixed_ms
    sd_before = est.l_fixed_sd_ms
    assert sd_before <= 3.3

    # seq=47: a single outlier is rejected and neither demotes nor arms anything.
    assert not est._ingest_label(329.1, sigma_meas=7.1)
    assert est.ready_for_native
    assert est._reopen_guard is None

    # seq=48: second same-side reject -> regime escape force-accept. THE FIX: the published
    # authority stays the retained converged tuple; only the learning posterior reopens.
    assert est._ingest_label(272.9, sigma_meas=7.1)
    assert est.ready_for_native
    assert est.last_status == "ready_reopen_retained"
    assert est.reopen_guard_active
    assert est.l_fixed_ms == pytest.approx(fixed_before, abs=1e-9)
    assert est.l_fixed_sd_ms == pytest.approx(sd_before, abs=1e-9)
    # ...while the live learning posterior really did reopen underneath:
    assert float(np.sqrt(est._var)) > 3.3
    assert est._mu > fixed_before + 15.0

    # The published tuple stays coherent for native's validatedAuthorityContract checks
    # (authority == value within 0.05, authority sd == published sd within 0.05).
    snap = est.telemetry_snapshot()
    assert snap.ready_for_native is True
    assert snap.authority_kind == "validated"
    assert abs(snap.authority_value_ms - snap.value_ms) <= 0.05
    assert abs(snap.authority_sd_ms - snap.l_fixed_sd_ms) <= 0.05
    assert snap.l_fixed_sd_ms <= 3.3
    assert snap.reopen_guard_active is True

    # The world snaps back to the old regime (the transient-outlier shape of the live episode):
    # the guard rides through the snap-back -- including the second escape it produces, whose
    # forced label AGREES with the retained posterior -- and the bot is never benched.
    for _ in range(4):
        est._ingest_label(229.9, sigma_meas=5.5)
        assert est.ready_for_native          # never benched, not even for one press
    assert est._reopen_guard is None          # resolved: live posterior re-converged
    assert est.last_status == "ready"
    assert 215.0 <= est.l_fixed_ms <= 230.0   # published authority is live again, near the truth
    assert est.l_fixed_sd_ms <= 3.3


def test_regime_reopen_default_off_pins_todays_benching_behaviour(monkeypatch):
    # A silent default flip (constructor default OR env default) must fail this test: with the
    # flag unset the 329.1/272.9 pair still demotes ready_for_native exactly as measured live.
    monkeypatch.delenv("ORION_LATENCY_REGIME_REOPEN_SOFT", raising=False)
    est = _reopen_estimator()
    _converge_total_posterior(est)
    assert not est._ingest_label(329.1, sigma_meas=7.1)
    assert est._ingest_label(272.9, sigma_meas=7.1)
    assert est._reopen_guard is None
    assert not est.ready_for_native
    assert est.last_status == "warming"
    assert est.l_fixed_sd_ms > 3.3


def test_regime_reopen_soft_never_arms_without_converged_authority():
    # Mirrors the live 2026-08-05T23:19Z / 2026-08-06T00:05Z episodes: an estimator that was
    # still WARMING (n=4 < CONVERGED_MIN_LABELS) takes the same two-same-side escape. Genuinely
    # absent authority must stay absent -- flag ON changes nothing here.
    est = _reopen_estimator(regime_reopen_soft=True)
    for _ in range(4):
        assert est._ingest_label(229.9, sigma_meas=5.5)
    assert not est.ready_for_native
    assert not est._ingest_label(170.0, sigma_meas=5.5)   # low-side reject #1
    assert est._ingest_label(170.0, sigma_meas=5.5)       # escape force-accept
    assert est._reopen_guard is None                      # nothing converged -> nothing retained
    assert not est.ready_for_native
    assert est.last_status == "warming"


def test_regime_reopen_soft_drops_retained_authority_on_genuine_regime_shift():
    # Mirrors the live 2026-08-06T10:38Z episode shape: the route genuinely moved (~+77ms fixed).
    # The retained authority may cover the first escape, but evidence that contradicts the
    # RETAINED posterior too (second escape outside the 4-sigma/25ms radius) must drop it --
    # fail closed to honest warming, then honest re-convergence at the new regime.
    est = _reopen_estimator(regime_reopen_soft=True)
    _converge_total_posterior(est)
    fixed_before = est.l_fixed_ms

    assert not est._ingest_label(305.0, sigma_meas=7.1)   # reject #1
    assert est._ingest_label(305.0, sigma_meas=7.1)       # escape #1: guard arms
    assert est.ready_for_native
    assert est.reopen_guard_active

    assert not est._ingest_label(305.0, sigma_meas=7.1)   # reject (new regime persists)
    assert est._ingest_label(305.0, sigma_meas=7.1)       # escape #2: contradicts retained -> drop
    assert est._reopen_guard is None
    assert not est.ready_for_native                       # fail closed: no trustworthy authority
    assert est.last_status == "warming"

    # Honest recovery: clean labels at the new regime re-converge and readiness returns on the
    # live posterior, near the new truth (fixed ~296.7), not the stale retained value.
    for _ in range(8):
        est._ingest_label(305.0, sigma_meas=7.1)
    assert est.ready_for_native
    assert est.last_status == "ready"
    assert est.l_fixed_ms > fixed_before + 50.0


def test_regime_reopen_guard_cannot_outlive_its_bounds():
    # Wall-clock bound: a retained authority that never sees re-convergence evidence expires on
    # the frame clock (update() keeps flowing even when labels do not).
    est = _reopen_estimator(regime_reopen_soft=True)
    base = 1_000_000.0
    est.update(base, 0.0, present=False, fed=False)       # establish the frame wall clock
    _converge_total_posterior(est)
    assert not est._ingest_label(329.1, sigma_meas=7.1)
    assert est._ingest_label(272.9, sigma_meas=7.1)
    assert est.ready_for_native
    est.update(base + 120_001.0, 0.0, present=False, fed=False)
    assert est._reopen_guard is None
    assert not est.ready_for_native

    # Relearn-budget bound: once _REOPEN_GUARD_MAX_LABELS accepted labels fail to re-converge
    # the live posterior, the retained authority stops being publishable immediately.
    est2 = _reopen_estimator(regime_reopen_soft=True)
    _converge_total_posterior(est2)
    assert not est2._ingest_label(329.1, sigma_meas=7.1)
    assert est2._ingest_label(272.9, sigma_meas=7.1)
    assert est2.ready_for_native
    est2._reopen_guard["labels_since"] = 8                # _REOPEN_GUARD_MAX_LABELS
    assert not est2.ready_for_native


def test_six_labels_are_not_ready_until_posterior_sd_converges():
    est = LatencyEstimator(lo_ms=15.0, hi_ms=180.0)
    for _ in range(6):
        # Deliberately broad measurement uncertainty: enough labels by count, not by precision.
        assert est._ingest_label(70.0, sigma_meas=20.0)
    assert est.n_labels == 6
    assert est.l_fixed_sd_ms > 3.3
    assert not est.ready_for_native
    assert est.last_status == "warming"


def test_high_latency_normal_release_remains_out_of_range():
    est = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=180.0,
                           post_release_window_ms=900.0)
    base = 1_800_000.0
    _synthetic_shot(est, t_rel_ms=base, latency_ms=275.0, f_stop=88.0,
                    base=base, calibration=False)
    assert est.n_labels == 0
    assert est.last_status == "rejected_out_of_range"
    assert est.last_rejection == "out_of_range"


def test_controlled_calibration_remains_bounded_at_500ms_and_non_cap():
    est = LatencyEstimator(freeze_win=4, lo_ms=15.0, hi_ms=180.0,
                           post_release_window_ms=900.0)
    base = 1_900_000.0
    _synthetic_shot(est, t_rel_ms=base, latency_ms=525.0, f_stop=88.0,
                    base=base, calibration=True)
    assert est.n_labels == 0
    assert est.label_method == "calibration"
    assert est.last_status == "rejected_out_of_range"

    # The calibration bit never weakens the hard non-cap eligibility gate.
    t0 = base + 5_000.0
    _synthetic_shot(est, t_rel_ms=t0, latency_ms=275.0, f_stop=97.0,
                    base=t0, calibration=True)
    assert est.n_labels == 0
    assert est.last_rejection == "near_cap"


def test_phase_tags_rise_then_frozen():
    est = LatencyEstimator(freeze_win=4)
    base = 2_000_000.0
    est.mark_release(base + 50)
    # rising
    for i, f in enumerate([20, 35, 50, 65, 80]):
        est.update(base + i * 16, float(f), present=True, fed=True)
    assert est.phase == PHASE_RISE
    # freeze (flat high fill, just after the release)
    for i in range(6):
        est.update(base + 100 + i * 16, 88.0, present=True, fed=True)
    assert est.phase == PHASE_FROZEN
    # meter gone -> none
    est.update(base + 400, 0.0, present=False, fed=False)
    assert est.phase == PHASE_NONE


def test_never_raises_on_garbage():
    est = LatencyEstimator()
    est.update(float("nan"), float("inf"), present=True, fed=True)
    est.update(0.0, 0.0, present=True, fed=True)
    est.mark_release(float("nan"))
    assert est.value_ms() >= 0.0


def _feed(est, samples, base):
    for dt, f in samples:
        est.update(base + dt, float(f), present=True, fed=True)


def test_v2_deflate_freeze_rejected():
    """Rise -> cap -> deflate -> freeze is a LATE release: the settle value encodes the rest
    point, not view latency (Part-0 M4: snap-back constant). Must classify 'deflate', no label."""
    est = LatencyEstimator(freeze_win=4, lo_ms=10.0, hi_ms=400.0)
    base = 5_000_000.0
    est.mark_release(base + 100)
    t = 0.0
    # rise 20 -> 100 (~0.38 pct/ms)
    for f in range(20, 101, 6):
        est.update(base + t, float(f), present=True, fed=True)
        t += 16.0
    # cap dwell
    for _ in range(3):
        est.update(base + t, 100.0, present=True, fed=True)
        t += 16.0
    # deflate 100 -> 60
    for f in range(95, 59, -6):
        est.update(base + t, float(f), present=True, fed=True)
        t += 16.0
    # settle (freeze) at 60, well inside the 900ms post-release window
    for _ in range(6):
        est.update(base + t, 60.0, present=True, fed=True)
        t += 16.0
    assert est.freeze_kind == "deflate"
    assert est.n_labels == 0


def test_v2_near_cap_freeze_ineligible():
    """A freeze above F_stop=95 sits in the flat cap: no timing info, no label (hard gate)."""
    est = LatencyEstimator(freeze_win=4, lo_ms=10.0, hi_ms=400.0)
    base = 6_000_000.0
    est.mark_release(base + 100)
    t = 0.0
    for f in range(20, 98, 6):
        est.update(base + t, float(f), present=True, fed=True)
        t += 16.0
    for _ in range(6):
        est.update(base + t, 97.0, present=True, fed=True)
        t += 16.0
    assert est.n_labels == 0
    assert est.freeze_kind in ("ambiguous", "")


def test_label_starved_after_eight_ineligible_near_cap_freezes():
    """A run of tip-cap freezes remains fail-closed but becomes diagnosable.

    F_stop>95 is intentionally not relaxed: the flat cap cannot produce a
    trustworthy inverse. After eight such releases the telemetry must be able
    to tell native/support that the estimator is starved rather than absent.
    """
    est = LatencyEstimator(freeze_win=4, lo_ms=10.0, hi_ms=400.0)
    base = 6_500_000.0
    for shot in range(8):
        t0 = base + shot * 3_000.0
        est.mark_release(t0 + 100.0, seq=shot + 1)
        t = 0.0
        for f in range(20, 98, 6):
            est.update(t0 + t, float(f), present=True, fed=True)
            t += 16.0
        for _ in range(6):
            est.update(t0 + t, 97.0, present=True, fed=True)
            t += 16.0
        est.update(t0 + 2_000.0, 0.0, present=False, fed=False)

    assert est.n_labels == 0
    assert est.value_ms() == 0.0
    assert est.label_starved
    assert not est.ready_for_native
    assert est.last_status == "rejected_near_cap"
    assert est.last_rejection == "near_cap"


def test_v2_posterior_converges_and_decomposes_rtt():
    """5 clean shots at 70ms total with rtt=10 -> posterior sd <= 3.3, l_fixed ~ 70-10-8.3,
    and value_ms() reconstitutes ~70."""
    est = LatencyEstimator(freeze_win=4, lo_ms=10.0, hi_ms=200.0)
    base = 7_000_000.0
    for k in range(5):
        t0 = base + k * 5000
        t_rel = t0
        t_cross = t_rel + 70.0
        slope = (88.0 - 20.0) / 180.0
        t = t0
        fired = False
        while t <= t_cross + 300.0:
            if t >= t_rel and not fired:
                est.mark_release(t, rtt_ms=10.0)
                fired = True
            f = min(20.0 + slope * (t - (t_cross - (88.0 - 20.0) / slope)), 88.0)
            est.update(t, f, present=True, fed=True)
            t += 16.0
        est.update(t0 + 4000, 0.0, present=False, fed=False)
    assert est.n_labels >= 4
    assert est.converged, (est.l_fixed_sd_ms, est.n_labels)
    assert 44.0 <= est.l_fixed_ms <= 60.0, est.l_fixed_ms      # ~51.7 pulled slightly to prior 60
    assert 58.0 <= est.value_ms() <= 82.0, est.value_ms()      # reconstituted total ~70


def test_converged_value_reconstitutes_with_current_verified_rtt():
    """RTT drift adjusts the total lead live without changing the fixed posterior."""
    est = LatencyEstimator(freeze_win=4, lo_ms=10.0, hi_ms=200.0)
    for shot in range(6):
        est._pending_rtt_ms = 10.0
        est._pending_rtt_known = True
        assert est._ingest_label(70.0, sigma_meas=5.0)

    fixed_before = est.l_fixed_ms
    historical = est.value_ms()
    live = est.value_ms(rtt_ms=30.0)

    assert est.ready_for_native
    assert live == pytest.approx(est.l_fixed_ms + 30.0 + TICK_WAIT_EXPECT_MS)
    assert live - historical == pytest.approx(20.0)
    assert est.l_fixed_ms == pytest.approx(fixed_before)


def test_later_live_rtt_is_not_added_when_calibration_rtt_was_unknown():
    """First verified RTT latches a zero-jump baseline; only later delta moves the total."""
    est = LatencyEstimator()
    for _ in range(6):
        est._pending_rtt_ms = 0.0
        est._pending_rtt_known = False
        assert est._ingest_label(70.0, sigma_meas=5.0)
    assert est.ready_for_native
    assert est.rtt_regime == "total"
    historical = est.value_ms()
    assert est.value_ms(rtt_ms=30.0) == pytest.approx(historical)
    assert est.value_ms(rtt_ms=42.0) == pytest.approx(historical + 12.0)


def test_unknown_to_known_rtt_labels_never_mix_posterior_domains():
    est = LatencyEstimator()
    est._pending_rtt_known = False
    assert est._ingest_label(70.0, sigma_meas=5.0)
    assert est.rtt_regime == "total"

    # RTT becomes verified mid-run. Total mode intentionally does not subtract it, so all labels
    # remain in the same command-to-visible-effect domain and can converge without hybrid bias.
    for _ in range(5):
        est._pending_rtt_ms = 30.0
        est._pending_rtt_known = True
        assert est._ingest_label(70.0, sigma_meas=5.0)
    assert est.n_labels == 6
    assert est.ready_for_native
    assert est.rtt_regime == "total"
    assert est.l_fixed_ms > 55.0  # total minus tick, never total minus RTT minus tick


def test_decomposed_rtt_regime_rejects_unknown_labels():
    est = LatencyEstimator()
    est._pending_rtt_ms = 20.0
    est._pending_rtt_known = True
    assert est._ingest_label(70.0, sigma_meas=5.0)
    assert est.rtt_regime == "decomposed"
    before_n = est.n_labels
    before_mu = est.l_fixed_ms

    est._pending_rtt_ms = 0.0
    est._pending_rtt_known = False
    assert not est._ingest_label(70.0, sigma_meas=5.0)
    assert est.n_labels == before_n
    assert est.l_fixed_ms == pytest.approx(before_mu)
    assert est.last_rejection == "rtt_unavailable"


def test_rejected_first_label_cannot_lock_rtt_regime():
    est = LatencyEstimator()
    est._pending_rtt_ms = 20.0
    est._pending_rtt_known = True
    assert not est._ingest_label(200.0, sigma_meas=4.0)
    assert est.rtt_regime == "unlocked"
    assert est.n_labels == 0

    est._pending_rtt_ms = 0.0
    est._pending_rtt_known = False
    assert est._ingest_label(70.0, sigma_meas=5.0)
    assert est.rtt_regime == "total"


def test_release_and_probe_keep_independent_rtt_known_state():
    est = LatencyEstimator()
    est.mark_release(1_000.0, rtt_ms=20.0)
    est.mark_probe(1_010.0, rtt_ms=None)
    assert est._pending_rtt_known is True
    assert est._pending_probe_rtt_known is False

    est.mark_probe(2_000.0, rtt_ms=25.0)
    est.mark_release(2_010.0, rtt_ms=None)
    assert est._pending_probe_rtt_known is True
    assert est._pending_rtt_known is False


def test_closing_overlapping_probe_cannot_overwrite_pending_release_rtt():
    class _LinearProbeTemplate:
        @staticmethod
        def _prior_for(_elapsed):
            return 100.0, 100.0, 0.0

        @staticmethod
        def g_inverse(value):
            return float(value)

    est = LatencyEstimator(lo_ms=10.0, hi_ms=200.0)
    est._reg = _LinearProbeTemplate()
    est.mark_release(1_050.0, rtt_ms=30.0)
    assert est.mark_probe(1_000.0, rtt_ms=10.0, spawn_offset_ms=20.0)
    # Each point back-extrapolates to animation start 1090ms: raw=90, spawn-stripped=70.
    est._probe_rise = [(1_100.0, 10.0), (1_110.0, 20.0), (1_120.0, 30.0)]
    est._close_probe()

    assert est.n_labels == 1
    assert est._rtt_used[-1] == pytest.approx(10.0)
    # The simultaneously pending real release must retain its own route snapshot.
    assert est._pending_rtt_known is True
    assert est._pending_rtt_ms == pytest.approx(30.0)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1.0, "bad"])
def test_invalid_live_rtt_cannot_move_measured_value(invalid):
    est = LatencyEstimator()
    est._measured_ms = 70.0
    assert est.value_ms(rtt_ms=invalid) == 70.0


def test_v2_outlier_label_rejected():
    """After convergence a single wild label (inside the outer wall) must not move the posterior."""
    est = LatencyEstimator(freeze_win=4, lo_ms=10.0, hi_ms=200.0)
    base = 8_000_000.0
    for k in range(4):
        _synthetic_shot(est, t_rel_ms=base + k * 5000, latency_ms=70.0, base=base + k * 5000)
        est.update(base + k * 5000 + 4000, 0.0, present=False, fed=False)
    n_before = est.n_labels
    mu_before = est.l_fixed_ms
    _synthetic_shot(est, t_rel_ms=base + 4 * 5000, latency_ms=160.0, base=base + 4 * 5000)
    est.update(base + 4 * 5000 + 4000, 0.0, present=False, fed=False)
    assert est.n_labels == n_before            # rejected
    assert abs(est.l_fixed_ms - mu_before) < 1e-9


def _drive_probe(est, press_ms, appear_ms, spawn_offset_ms):
    """Feed a template-shaped rise appearing at appear_ms after a probe press at press_ms."""
    T, A, fmin = est._reg._prior_for(0.0)
    assert est.mark_probe(press_ms, seq=1, rtt_ms=None, spawn_offset_ms=spawn_offset_ms)
    t = appear_ms
    while t < appear_ms + 400.0:
        u = (t - appear_ms) / max(T, 40.0)
        f = float(fmin) + max(float(A), 5.0) * float(est._reg._g(u))
        est.update(t, f, present=True, fed=True)
        t += 16.0


def test_probe_label_feeds_posterior():
    """[ORION_PROBE] pump-fake probe: press, template rise appearing 100ms later, spawn
    offset 30 -> label ~70ms total; ingested into the L_fixed posterior at sigma 8."""
    est = LatencyEstimator(lo_ms=10.0, hi_ms=200.0)
    if est._reg is None:
        pytest.skip("registration template / scipy unavailable")
    base = 9_000_000.0
    _drive_probe(est, press_ms=base, appear_ms=base + 100.0, spawn_offset_ms=30.0)
    assert est.n_labels == 1
    assert abs(est.last_probe_raw_ms - 100.0) < 10.0, est.last_probe_raw_ms
    assert est.label_method == "probe"
    assert 55.0 <= est.value_ms() <= 85.0, est.value_ms()   # reconstituted ~70


def test_probe_uncalibrated_logs_raw_only():
    """spawn_offset 0 (uncalibrated) -> no label, but the raw press->appear is exposed for
    the offline D_spawn calibration."""
    est = LatencyEstimator(lo_ms=10.0, hi_ms=200.0)
    if est._reg is None:
        pytest.skip("registration template / scipy unavailable")
    base = 10_000_000.0
    _drive_probe(est, press_ms=base, appear_ms=base + 100.0, spawn_offset_ms=0.0)
    assert est.n_labels == 0
    assert est.last_probe_raw_ms > 0.0


def test_probe_ignores_sub_fmin_onset_samples(caplog):
    """[ORION_PROBE] A real meter fades in near 0% and climbs THROUGH the template's fmin
    (20.8 on the shipped prior). Sub-fmin samples carry no timing information: g_inverse's
    argument clamps to 0, so each one back-extrapolates to its OWN timestamp. Three of them sit
    one frame apart -> t0 spread ~13.6ms -> over the 6.0ms consistency cap -> probe dropped.

    This is the live failure of 2026-08-05: 16 markers, 0 closes, 0 expiries, in total silence.
    Every probe test passed throughout, because _drive_probe() begins its synthetic rise exactly
    AT fmin and so never produced the samples the live reader always produces first.
    """
    est = LatencyEstimator(lo_ms=10.0, hi_ms=200.0)
    if est._reg is None:
        pytest.skip("registration template / scipy unavailable")
    T, A, fmin = est._reg._prior_for(0.0)
    base = 12_000_000.0
    appear = base + 100.0
    assert est.mark_probe(base, seq=1, rtt_ms=None, spawn_offset_ms=0.0)

    with caplog.at_level(logging.WARNING, logger="latency_estimator"):
        # the onset the live reader actually emits: below the template floor, monotone rising
        t = appear - 64.0
        for f in (2.0, 6.0, 11.0, 16.0):
            est.update(t, f, present=True, fed=True)
            t += 16.0
        t = appear
        while t < appear + 400.0:
            u = (t - appear) / max(T, 40.0)
            f = float(fmin) + max(float(A), 5.0) * float(est._reg._g(u))
            est.update(t, f, present=True, fed=True)
            t += 16.0

    assert est.last_probe_raw_ms > 0.0, "probe dropped on sub-fmin onset samples"
    assert abs(est.last_probe_raw_ms - 100.0) < 15.0, est.last_probe_raw_ms
    dropped = [r.getMessage() for r in caplog.records if "probe DROPPED" in str(r.msg)]
    assert not dropped, dropped


def _drive_probe_run(est, base_ms, offsets_ms, spawn_offset_ms=0.0):
    """Drive a full N-probe warmup run, 2.5s apart (the shipped probeGapMs) so the >60s
    stale-run reset never trips mid-run."""
    for i, off in enumerate(offsets_ms):
        press = base_ms + i * 2500.0
        _drive_probe(est, press_ms=press, appear_ms=press + off, spawn_offset_ms=spawn_offset_ms)


def test_probe_raw_spread_is_logged_while_uncalibrated(caplog):
    """[ORION_PROBE] The jitter readout must survive the uncalibrated drop.

    raw = L_route + D_spawn with D_spawn a game-side CONSTANT, so sd(raw) == sd(L_route): the
    spread is a valid actuation-jitter measurement even though the LABEL is dropped. Before this
    line existed a cold warmup run -- the exact case probes were built for -- logged nothing at
    all, because the D_spawn readout returns early until >=2 passive-oracle labels exist.
    """
    est = LatencyEstimator(lo_ms=10.0, hi_ms=200.0)
    if est._reg is None:
        pytest.skip("registration template / scipy unavailable")
    with caplog.at_level(logging.INFO, logger="latency_estimator"):
        _drive_probe_run(est, 11_000_000.0,
                         [100.0, 104.0, 96.0, 102.0, 98.0, 103.0, 97.0, 101.0],
                         spawn_offset_ms=0.0)

    assert est.n_labels == 0, "uncalibrated run must still ingest no labels"
    lines = [r.getMessage() for r in caplog.records if "probe raw spread" in str(r.msg)]
    assert lines, "no spread line logged — the jitter measurement went on the floor"
    assert "n=8" in lines[-1], lines[-1]


def test_probe_raw_spread_tracks_injected_jitter(caplog):
    """A wide run must report a larger robust_sd than a tight one.

    Asserting a specific value would be asserting the registration template's reconstruction
    error, not the statistic. The property that makes the number USABLE is that it responds to
    real jitter -- if it did not, a machine with a 30ms actuation path would look like a clean one.
    """
    def _sd_for(offsets, base):
        est = LatencyEstimator(lo_ms=10.0, hi_ms=200.0)
        if est._reg is None:
            pytest.skip("registration template / scipy unavailable")
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="latency_estimator"):
            _drive_probe_run(est, base, offsets, spawn_offset_ms=0.0)
        msgs = [r.getMessage() for r in caplog.records if "probe raw spread" in str(r.msg)]
        assert msgs, "expected a spread line"
        return float(msgs[-1].split("robust_sd=")[1].split("ms")[0])

    tight = _sd_for([100.0, 100.5, 99.5, 100.2, 99.8, 100.3, 99.7, 100.1], 12_000_000.0)
    wide = _sd_for([100.0, 130.0, 70.0, 120.0, 80.0, 125.0, 75.0, 105.0], 13_000_000.0)
    assert wide > tight * 3.0, f"tight={tight:.2f} wide={wide:.2f} — statistic is not responsive"


def test_probe_expires_when_meter_never_appears():
    est = LatencyEstimator()
    if est._reg is None:
        pytest.skip("registration template / scipy unavailable")
    base = 11_000_000.0
    assert est.mark_probe(base, spawn_offset_ms=30.0)
    # no meter for >2s -> the probe silently expires; a later unrelated rise adds no label
    est.update(base + 2500.0, 20.0, present=True, fed=True)
    est.update(base + 2516.0, 24.0, present=True, fed=True)
    est.update(base + 2532.0, 28.0, present=True, fed=True)
    assert est.n_labels == 0
    assert est.last_probe_raw_ms < 0.0


def test_probe_expiry_fires_and_logs_on_not_present_frames(caplog):
    """[ORION_PROBE] The meterless probe -- the case that actually happens -- must expire LOUDLY.

    A press that spawns no meter (no ball in hand, press too short, input swallowed) generates
    ONLY not-present frames. update() returns early on those, so while the timeout lived inside
    the present-frame branch it could never fire for the one failure it was written to catch: the
    pending probe just sat there until the next press overwrote it. A live run on 2026-08-05 lost
    all 8 probes this way and was indistinguishable in the log from a clean run.

    The sibling test above drives present=True frames and therefore passed throughout.
    """
    est = LatencyEstimator()
    if est._reg is None:
        pytest.skip("registration template / scipy unavailable")
    base = 14_000_000.0
    assert est.mark_probe(base, spawn_offset_ms=30.0)

    with caplog.at_level(logging.WARNING, logger="latency_estimator"):
        # Exactly what the console sends when the press produced nothing at all.
        t = base
        while t < base + 2600.0:
            est.update(t, 0.0, present=False, fed=True)
            t += 16.0

    assert est.n_labels == 0
    assert est.last_probe_raw_ms < 0.0
    expired = [r.getMessage() for r in caplog.records if "probe EXPIRED" in str(r.msg)]
    assert expired, "a meterless probe expired silently — the failure is invisible again"
    assert "rise_samples=0" in expired[0], expired[0]


def _sawtooth_pairs(base, phi_e, n=8, c=60.0):
    """Synthetic probe run: presses 2.5s apart, tick-staggered k*2.083ms; raws on the sawtooth
    L_k = c + ((phi_e - press_k mod P) mod P) — a press waits for the next input-tick edge."""
    P = TICK_PERIOD_MS
    pairs = []
    for k in range(n):
        press = base + k * 2500.0 + k * 2.083
        raw = c + ((phi_e - (press % P)) % P)
        pairs.append((press, raw))
    return pairs


def _phase_err(phi, phi_e):
    P = TICK_PERIOD_MS
    d = abs(float(phi) - float(phi_e)) % P
    return min(d, P - d)


def test_tick_phase_fit_recovers_injected_phase():
    """[Phase-2 A2(c)] the 64-point sawtooth scan recovers a known injected edge phase from
    synthetic probe raws L_k = 60 + ((phi_e - k*2.083) mod 16.667) to within 1ms."""
    base = 12_000_000.0            # exactly 720000 ticks -> press phase = k*2.083
    for phi_e in (0.0, 5.0, 12.9):
        phi, rms, c = fit_tick_phase(_sawtooth_pairs(base, phi_e))
        assert phi is not None
        assert _phase_err(phi, phi_e) <= 1.0, (phi_e, phi, rms)
        assert rms < 1.0, rms
        assert abs(c - 60.0) < 2.0, c
    # unfittable inputs never raise, just decline
    assert fit_tick_phase([])[0] is None
    assert fit_tick_phase([(0.0, 60.0)] * 3)[0] is None


def test_tick_phase_properties_and_conf_decay():
    """>= 6 closed probes -> tick_phase_ms/conf/sd exposed; the conf then DECAYS with wall time
    since the probe run (halved per 10 minutes) so a stale phase self-retires."""
    est = LatencyEstimator()
    assert est.tick_phase_conf == 0.0
    assert est.tick_phase_ms < 0.0
    base = 12_000_000.0
    phi_e = 5.0
    pairs = _sawtooth_pairs(base, phi_e, n=6)
    for press, raw in pairs[:5]:
        est._record_probe_pair(press, raw)
    assert est.tick_phase_conf == 0.0          # < 6 pairs -> no fit yet
    est._record_probe_pair(*pairs[5])
    assert _phase_err(est.tick_phase_ms, phi_e) <= 1.0, est.tick_phase_ms
    assert est.tick_phase_conf > 0.5
    assert 0.0 < est.tick_phase_sd_ms <= 1.2
    conf0 = est.tick_phase_conf
    # ~20 minutes of session later: conf must have halved twice.
    est.update(pairs[5][0] + 20.0 * 60_000.0, 0.0, present=False, fed=False)
    assert est.tick_phase_conf == pytest.approx(conf0 * 0.25, rel=0.05)


def test_probe_run_fits_tick_phase_end_to_end():
    """[Phase-2 A2(c)] full probe path: tick-staggered presses closed via the template
    back-extrapolation accumulate (press_phase, raw) pairs and fit the injected edge phase.
    spawn_offset 0 (uncalibrated) on purpose: the PHASE needs no D_spawn (a constant lands in
    the sawtooth's C term), so it converges even on an uncalibrated rig."""
    est = LatencyEstimator(lo_ms=10.0, hi_ms=200.0)
    if est._reg is None:
        pytest.skip("registration template / scipy unavailable")
    base = 13_000_000.0
    phi_e = 9.0
    for press, raw in _sawtooth_pairs(base, phi_e, n=8):
        _drive_probe(est, press_ms=press, appear_ms=press + raw, spawn_offset_ms=0.0)
    assert est.tick_phase_conf > 0.4, (est.tick_phase_ms, est.tick_phase_sd_ms)
    assert _phase_err(est.tick_phase_ms, phi_e) <= 1.0, est.tick_phase_ms
    assert est.n_labels == 0                    # uncalibrated: phase yes, labels no


def test_robust_to_transient_drops():
    est = LatencyEstimator(freeze_win=4, lo_ms=10.0, hi_ms=200.0)
    base = 3_000_000.0
    est.mark_release(base + 50)
    
    # Rising with a transient dropout
    est.update(base + 0, 20.0, present=True, fed=True)
    est.update(base + 16, 35.0, present=True, fed=True)
    
    # Transient drop for 2 frames (~33ms)
    est.update(base + 32, 0.0, present=False, fed=False)
    est.update(base + 48, 0.0, present=False, fed=False)
    
    # Meter recovers and keeps rising, then freezes
    est.update(base + 64, 50.0, present=True, fed=True)
    est.update(base + 80, 65.0, present=True, fed=True)
    est.update(base + 96, 80.0, present=True, fed=True)
    
    for i in range(6):
        est.update(base + 112 + i * 16, 88.0, present=True, fed=True)
        
    # The oracle should have successfully closed and registered a label despite the transient drop
    assert est.n_labels == 1
    assert 15.0 <= est.value_ms() <= 100.0


# ---------------------------------------------------------------------------
# 2026-08-04 lock-domain labelling. The oracle used to timestamp the meter's
# extrapolated UP-CROSSING of F_stop, but the real meter eases out, overshoots,
# and recedes before the shot visibly locks -- so the label read short by that
# whole animation and (because a shorter lead makes the meter freeze higher) the
# posterior walked further down every session. These cover the new path; every
# pre-existing test uses a frozen-in-place synthetic that clamps f to F_stop and
# therefore still exercises the unchanged crossing branch.
# ---------------------------------------------------------------------------

def _overshoot_shot(est, t_rel_ms, cross_lag_ms, lock_lag_ms, f_stop=88.0,
                    overshoot_pp=5.0, dt=16.0, base=1_000_000.0, seq=None):
    """Rise crosses F_stop at t_rel+cross_lag, peaks above it, then settles at F_stop.

    The trailing settled run begins at t_rel + cross_lag + lock_lag, so a lock-domain
    oracle must report ~cross_lag+lock_lag, while the old crossing oracle reported ~cross_lag.
    """
    t_cross = t_rel_ms + cross_lag_ms
    t_settle = t_cross + lock_lag_ms
    peak = f_stop + overshoot_pp
    slope = (f_stop - 20.0) / 180.0
    t, fired = base, False
    while t <= t_settle + 400.0:
        if t >= t_rel_ms and not fired:
            est.mark_release(t, seq=seq)
            fired = True
        if t <= t_cross:                      # fast rising flank
            f = max(0.0, f_stop - slope * (t_cross - t))
        elif t < t_settle:                    # ease-out to peak, then recede
            frac = (t - t_cross) / max(1e-6, (t_settle - t_cross))
            f = f_stop + overshoot_pp * (1.0 - abs(2.0 * frac - 1.0))
            f = min(f, peak)
        else:                                 # settled / locked
            f = f_stop
        est.update(t, f, present=True, fed=True)
        t += dt


def test_overshoot_trace_labels_the_lock_not_the_crossing():
    """A sub-cap overshoot must be labelled at the freeze onset, not the up-crossing.

    Sized to the default prior (mu=60, sd=12) so the posterior outlier gate admits it: the
    crossing lands at 60ms (exactly the prior) and the lock 30ms later. The OLD crossing oracle
    would report ~60; a lock-domain oracle must report ~90.
    """
    est = LatencyEstimator(freeze_win=4, lo_ms=10.0, hi_ms=500.0)
    base = 1_000_000.0
    _overshoot_shot(est, t_rel_ms=base, cross_lag_ms=60.0, lock_lag_ms=30.0, base=base)
    assert est.n_labels >= 1, (
        f"sub-cap overshoot produced no label ({est.last_rejection})")
    assert est.value_ms() > 75.0, (
        f"label still in the crossing domain: {est.value_ms():.1f} (crossing=60, lock=90)")
    assert est.lock_lag_ms > 10.0, f"lock lag not measured: {est.lock_lag_ms}"


def test_frozen_in_place_is_unchanged_by_lock_domain_change():
    """No overshoot -> crossing IS the onset; the old numbers must be preserved exactly."""
    est = LatencyEstimator(freeze_win=4, lo_ms=10.0, hi_ms=200.0)
    base = 1_000_000.0
    for k in range(6):
        _synthetic_shot(est, t_rel_ms=base + k * 5000, latency_ms=70.0, base=base + k * 5000)
        est.update(base + k * 5000 + 4000, 0.0, present=False, fed=False)
    assert est.n_labels >= 3
    assert abs(est.value_ms() - 70.0) < 25.0, f"frozen-in-place regressed: {est.value_ms():.1f}"
    assert est.lock_lag_ms <= 1.0, f"frozen-in-place should measure ~0 lag: {est.lock_lag_ms}"


def test_cap_deflate_still_rejected_after_gate_relaxation():
    """Relaxing rise_ok must NOT start accepting cap+recede traces (those encode overtime)."""
    est = LatencyEstimator(freeze_win=4, lo_ms=10.0, hi_ms=500.0)
    base = 1_000_000.0
    _overshoot_shot(est, t_rel_ms=base, cross_lag_ms=60.0, lock_lag_ms=30.0,
                    f_stop=92.0, overshoot_pp=8.0, base=base)   # peak 100 >= 97 -> deflate
    assert est.n_labels == 0
    assert est.last_rejection in ("deflate", "ambiguous", "near_cap"), est.last_rejection


# ---- [ORION_PROBE_PERSIST]/[ORION_AUTO_PROBE_ONBOARD] probe-validated lead persistence ---------
#
# Precedence contract pinned by these tests ("first 10 minutes on a new rig"):
#   1. USER-SET LEAD ALWAYS WINS - enforced natively: AutomationEngine::measuredLeadForActuationMs
#      (AutomationEngine.cpp:9393-9398) returns config_.userActuationLeadMs whenever
#      actuationLeadUserSet latched it. Nothing in this module writes settings.json, so the
#      probe cache can never overwrite an owner-tuned lead; not testable from Python.
#   2. PERSISTED PROBE LEAD as a replacement factory SEED (probe-measured mean/sd/source) is
#      gated behind ORION_PROBE_PRIOR_AUTHORITY (paired with the native settings flag
#      `probe_cache_prior_authority`). WITHOUT that flag the restore is a Bayesian warm start
#      ONLY and the packaged tuple stands untouched - measured 2026-08-06: the unconditional
#      replacement published a source native's venice-e2e-route-prior: prefix check refuses,
#      which destroyed cold-start factory authority and benched every session-start press.
#   3. FACTORY PRIOR is the floor for a genuinely cold install.
# A probe cache never mints validated/provisional authority and never bypasses L1/L2.


def _factory_style_kwargs(scope, cache, enabled):
    """Ctor kwargs emulating a packaged-factory install (218.5/28.7) with a probe-persist flag."""
    return dict(route_scope=scope, cache_path=str(cache),
                boot_prior_ms=218.5, hi_ms=500.0,
                mu_prior_ms=218.5 - TICK_WAIT_EXPECT_MS, sd_prior_ms=28.7,
                factory_prior_source="model:test", factory_prior_version="v1",
                factory_prior_sd_ms=28.7, probe_persist_enabled=enabled)


def test_probe_persist_and_onboard_flags_default_off(monkeypatch):
    """Env unset -> both features dark; explicit '0' stays dark; '1' arms them."""
    monkeypatch.delenv("ORION_PROBE_LEAD_PERSIST", raising=False)
    monkeypatch.delenv("ORION_AUTO_PROBE_ONBOARD", raising=False)
    est = LatencyEstimator()
    assert est._probe_persist_enabled is False
    assert est._probe_onboard_enabled is False
    assert est.probe_recommended is False           # dark even on a label-less cold install
    monkeypatch.setenv("ORION_PROBE_LEAD_PERSIST", "0")
    monkeypatch.setenv("ORION_AUTO_PROBE_ONBOARD", "0")
    est0 = LatencyEstimator()
    assert est0._probe_persist_enabled is False
    assert est0._probe_onboard_enabled is False
    monkeypatch.setenv("ORION_PROBE_LEAD_PERSIST", "1")
    monkeypatch.setenv("ORION_AUTO_PROBE_ONBOARD", "1")
    est1 = LatencyEstimator()
    assert est1._probe_persist_enabled is True
    assert est1._probe_onboard_enabled is True


@pytest.mark.skipif(os.name != "nt", reason="production cache is Windows DPAPI-only")
def test_probe_run_persists_lead_only_behind_flag_and_after_three_probes(tmp_path):
    """The persisted file appears exactly at run completion (3rd probe label), flag ON only."""
    scope = "console=a|controller=pipe|capture_card|device=0|mode=1080p60"

    dark_cache = tmp_path / "dark.bin"
    dark = LatencyEstimator(**_factory_style_kwargs(scope, dark_cache, enabled=False))
    if dark._reg is None:
        pytest.skip("registration template / scipy unavailable")
    _drive_probe_run(dark, 20_000_000.0, [330.0, 332.0, 328.0], spawn_offset_ms=30.0)
    assert dark.n_labels == 3 and dark.probe_labels == 3
    assert not dark_cache.exists(), "flag OFF must never write the cache"

    cache = tmp_path / "probe_lead.bin"
    est = LatencyEstimator(**_factory_style_kwargs(scope, cache, enabled=True))
    _drive_probe_run(est, 30_000_000.0, [330.0, 332.0], spawn_offset_ms=30.0)
    assert est.n_labels == 2 and est.probe_labels == 2
    assert not cache.exists(), "an incomplete run (2 probes) must not persist"
    _drive_probe(est, press_ms=30_000_000.0 + 2 * 2500.0,
                 appear_ms=30_000_000.0 + 2 * 2500.0 + 329.0, spawn_offset_ms=30.0)
    assert est.n_labels == 3 and est.probe_labels == 3
    assert cache.is_file(), "run complete -> the persisted file appears"
    # the live posterior converged on the injected ~300ms truth from the 218.5 factory seed
    assert 285.0 <= est.value_ms() <= 315.0, est.value_ms()
    assert not est.ready_for_native            # probes never mint validated authority live
    assert not est.provisional_ready


@pytest.mark.skipif(os.name != "nt", reason="production cache is Windows DPAPI-only")
def test_probe_cache_restore_default_is_warmstart_and_keeps_packaged_authority(tmp_path):
    """DEFAULT-OFF leg: without ORION_PROBE_PRIOR_AUTHORITY the restore must NOT touch the
    packaged factory tuple. This pins the 2026-08-06 poisoning fix - the old unconditional
    replacement published source="probe_cache:self_measured", which native's
    factoryLatencyPriorMatchesRoute() refuses, so cold-start authority vanished and every
    session-start press was refused waiting_for_latency_calibration. Fails if the
    replacement default ever silently flips back ON."""
    scope = "console=a|controller=pipe|capture_card|device=0|mode=1080p60"
    cache = tmp_path / "probe_lead.bin"
    writer = LatencyEstimator(**_factory_style_kwargs(scope, cache, enabled=True))
    if writer._reg is None:
        pytest.skip("registration template / scipy unavailable")
    _drive_probe_run(writer, 40_000_000.0, [330.0, 332.0, 328.0], spawn_offset_ms=30.0)
    assert cache.is_file()

    restored = LatencyEstimator(**_factory_style_kwargs(scope, cache, enabled=True),
                                probe_prior_authority=False)
    assert restored.probe_prior_restored                 # warm start ran...
    assert restored.authority_kind == "factory"
    assert restored.factory_prior_source == "model:test"  # ...but the packaged tuple STANDS
    assert restored.authority_value_ms == pytest.approx(218.5)
    assert restored.last_status == "probe_prior_warmstart"
    # the probe measurement entered as the Bayesian prior (faster convergence, no authority)
    assert 275.0 <= restored._prior_mu <= 315.0, restored._prior_mu
    assert restored._prior_var >= 36.0                   # floored at the 6.0 provisional sd
    assert restored.n_labels == 0
    assert not restored.ready_for_native
    assert not restored.provisional_ready
    assert not restored.restored_from_cache

    # flag OFF ignores the probe cache entirely -> the packaged factory seed stands
    cold = LatencyEstimator(**_factory_style_kwargs(scope, cache, enabled=False))
    assert not cold.probe_prior_restored
    assert cold.factory_prior_source == "model:test"
    assert cold.authority_value_ms == pytest.approx(218.5)

    # wrong route scope -> different digest -> no restore
    wrong = LatencyEstimator(**_factory_style_kwargs(scope + "|other", cache, enabled=True))
    assert not wrong.probe_prior_restored
    assert wrong.authority_value_ms == pytest.approx(218.5)


@pytest.mark.skipif(os.name != "nt", reason="production cache is Windows DPAPI-only")
def test_probe_cache_restores_as_measured_prior_only_behind_authority_flag(tmp_path):
    """ARMED leg (ORION_PROBE_PRIOR_AUTHORITY + native probe_cache_prior_authority): install #2
    session #2 cold-starts at ITS OWN probe-measured lead instead of the packaged 218.5."""
    scope = "console=a|controller=pipe|capture_card|device=0|mode=1080p60"
    cache = tmp_path / "probe_lead.bin"
    writer = LatencyEstimator(**_factory_style_kwargs(scope, cache, enabled=True))
    if writer._reg is None:
        pytest.skip("registration template / scipy unavailable")
    _drive_probe_run(writer, 40_000_000.0, [330.0, 332.0, 328.0], spawn_offset_ms=30.0)
    assert cache.is_file()

    restored = LatencyEstimator(**_factory_style_kwargs(scope, cache, enabled=True),
                                probe_prior_authority=True)
    assert restored.probe_prior_restored
    assert restored.authority_kind == "factory"          # factory KIND, probe-measured SEED
    assert restored.factory_prior_source == "probe_cache:self_measured"
    assert 285.0 <= restored.authority_value_ms <= 315.0, restored.authority_value_ms
    assert restored.authority_sd_ms >= 6.0               # never narrower than provisional floor
    assert restored.last_status == "probe_prior_restored"
    # weaker than any validated/controlled restore, by construction:
    assert restored.n_labels == 0
    assert not restored.ready_for_native
    assert not restored.provisional_ready
    assert not restored.restored_from_cache              # prior seed, not restored authority
    snap = restored.telemetry_snapshot()
    assert snap.probe_prior_restored is True
    assert snap.authority_kind == "factory"


@pytest.mark.skipif(os.name != "nt", reason="production cache is Windows DPAPI-only")
def test_controlled_cache_still_outranks_probe_path_and_probe_never_downgrades_it(tmp_path):
    """A controlled+validated posterior keeps the strict restore path even with the flag ON."""
    scope = "console=a|controller=pipe|decoder|hash=b"
    cache = tmp_path / "latency.bin"
    est = LatencyEstimator(route_scope=scope, cache_path=str(cache),
                           probe_persist_enabled=True)
    assert est._ingest_label(70.0, sigma_meas=4.0, calibration=True, rtt_known=False)
    assert est._ingest_label(72.0, sigma_meas=4.0, calibration=True,
                             controlled_validation=True, rtt_known=False)
    assert cache.is_file()
    restored = LatencyEstimator(route_scope=scope, cache_path=str(cache),
                                probe_persist_enabled=True)
    assert restored.restored_from_cache          # strict controlled restore, not the probe seed
    assert not restored.probe_prior_restored
    assert restored.value_ms() == 0.0            # still gated on route attestation
    assert restored.set_restored_route_attested(True)
    assert restored.ready_for_native


def test_probe_recommended_is_flag_gated_and_clears_on_any_evidence():
    est = LatencyEstimator(probe_onboard_enabled=True)
    assert est.probe_recommended
    snap = est.telemetry_snapshot()
    assert snap.probe_recommended is True
    assert est._ingest_label(70.0, sigma_meas=4.0, rtt_known=False)
    assert not est.probe_recommended, "any accepted label retires the onboarding hint"

    dark = LatencyEstimator()                    # flag defaulted OFF (env unset in CI)
    dark._probe_onboard_enabled = False          # explicit: independent of ambient env
    assert not dark.probe_recommended


def test_latency_oracle_wire_carries_probe_onboarding_fields():
    import importlib.util
    sc = os.path.join(os.path.dirname(__file__), "..",
                      "native_orion", "backend", "autogreen_sidecar.py")
    spec = importlib.util.spec_from_file_location("_autogreen_sidecar_probe_ut", sc)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    est = LatencyEstimator(probe_onboard_enabled=True)
    wire = mod._latency_oracle_wire(est.telemetry_snapshot())
    assert wire["measured_latency_probe_recommended"] is True
    assert wire["measured_latency_probe_prior_restored"] is False
    # absent fields on an older snapshot fail closed to False
    legacy = mod._latency_oracle_wire(LatencyEstimator().telemetry_snapshot())
    assert legacy["measured_latency_probe_recommended"] is False
