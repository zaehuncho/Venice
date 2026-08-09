"""Release-marker relay + PTS-domain measurement timestamps (RC-3 follow-through).

The native C++ side already emits release_marker on every release (AutomationEngine::
triggerRelease -> RemotePlaySession::sendReleaseMarker, covered by AutomationEngineTests).
These tests cover the previously-untested Python half of the chain:

  sidecar stdin dispatch (_handle_release_marker)
      -> RemotePlayOrchestrator.mark_release
          -> LatencyEstimator.mark_release        (epoch-ms clock domain end to end)

plus the PTS-domain feed: _frame_measurement_epoch_ms() rides the decoder PTS clock
mapped onto the epoch axis when pts is available, and falls back to the raw capture
epoch stamp on pts=0 sources (capture card / WGC / window).
"""
import hashlib
import importlib.util
import os
import threading
import time

import pytest

from latency_estimator import LatencyEstimator


class _ObservedRLock:
    """RLock that exposes a named thread's acquisition attempt to a test barrier."""

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


def _load_sidecar():
    sc = os.path.join(os.path.dirname(__file__), "..",
                      "native_orion", "backend", "autogreen_sidecar.py")
    spec = importlib.util.spec_from_file_location("_autogreen_sidecar_rm_ut", sc)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _RecordingOrch:
    def __init__(self):
        self.calls = []

    def mark_release(self, wall_ms=None, seq=None, calibration=False,
                      validation_target_pct=None,
                      validation_tolerance_pct=None,
                      physical_epoch=0, shot_attempt=0):
        self.calls.append((wall_ms, seq, calibration,
                           validation_target_pct, validation_tolerance_pct,
                           physical_epoch, shot_attempt))
        return seq or 0


class _FakeEstimator:
    """Duck-type of LatencyEstimator's marker API (kwargs-tolerant: the orchestrator's
    relay grows keyword args like rtt_ms as the oracle evolves -- the relay contract this
    file pins is (wall_ms, seq) reaching the estimator, not the exact kwarg set)."""
    def __init__(self):
        self.calls = []
        self.validation = []
        self.rtt = []

    def mark_release(self, ts, seq=None, calibration=False, **kw):
        self.calls.append((ts, seq, calibration))
        self.validation.append((kw.get("validation_target_pct"),
                                kw.get("validation_tolerance_pct")))
        self.rtt.append(kw.get("rtt_ms"))
        return seq if seq is not None else 42

    def value_ms(self):
        return 70.0


class _ColdObservableEstimator:
    """Zero-value oracle with enough state to explain why it has no authority."""
    n_labels = 0
    bootstrapped = True
    l_fixed_ms = 60.0
    l_fixed_sd_ms = 12.0
    ready_for_native = False
    label_starved = True
    last_status = "rejected_near_cap"
    last_rejection = "near_cap"

    @staticmethod
    def value_ms():
        return 0.0

    @staticmethod
    def confidence():
        return 0.0


def _build_orch(monkeypatch, frame_source="auto"):
    monkeypatch.setenv("ORION_SIMPLE_READER", "1")
    from remote_play_orchestrator import RemotePlayOrchestrator, OrchestratorConfig
    cfg = OrchestratorConfig(console_ip="1.2.3.4", virtual_controller=False,
                             hidhide=False, auto_launch_client=False,
                             frame_source=frame_source)
    return RemotePlayOrchestrator(cfg)


# --------------------------------------------------------------------------- #
#  sidecar stdin dispatch -> orch.mark_release
# --------------------------------------------------------------------------- #
def test_sidecar_release_marker_dispatch_relays_ts_and_seq():
    mod = _load_sidecar()
    orch = _RecordingOrch()
    mod._handle_release_marker(orch, {"cmd": "release_marker",
                                      "seq": 7, "wall_ms": 1234567.5})
    assert orch.calls == [(1234567.5, 7, False, None, None, 0, 0)]


def test_sidecar_release_marker_dispatch_relays_calibration_flag():
    mod = _load_sidecar()
    orch = _RecordingOrch()
    mod._handle_release_marker(orch, {"cmd": "release_marker",
                                      "seq": 8, "wall_ms": 1234568.5,
                                      "calibration": True})
    assert orch.calls == [(1234568.5, 8, True, None, None, 0, 0)]


def test_sidecar_release_marker_dispatch_relays_validation_metadata():
    mod = _load_sidecar()
    orch = _RecordingOrch()
    mod._handle_release_marker(orch, {"cmd": "release_marker",
                                      "seq": 9, "wall_ms": 1234569.5,
                                      "calibration": True,
                                      "validation_target_pct": 88.0,
                                      "validation_tolerance_pct": 3.5})
    assert orch.calls == [(1234569.5, 9, True, 88.0, 3.5, 0, 0)]


def test_sidecar_release_marker_dispatch_tolerates_missing_fields():
    mod = _load_sidecar()
    orch = _RecordingOrch()
    mod._handle_release_marker(orch, {"cmd": "release_marker"})
    assert orch.calls == [(None, None, False, None, None, 0, 0)]  # orchestrator stamps 'now' itself


def test_sidecar_release_marker_dispatch_preserves_exact_ownership_identity():
    mod = _load_sidecar()
    orch = _RecordingOrch()
    mod._handle_release_marker(
        orch, {"cmd": "release_marker", "seq": 11, "wall_ms": 1234570.5,
               "physical_epoch": "18446744073709551615",
               "shot_attempt": "9007199254740993"})
    assert orch.calls == [
        (1234570.5, 11, False, None, None,
         0xFFFFFFFFFFFFFFFF, 9007199254740993)]


def test_sidecar_release_marker_dispatch_never_raises():
    mod = _load_sidecar()

    class _Boom:
        def mark_release(self, *_a, **_k):
            raise RuntimeError("estimator down")

    mod._handle_release_marker(_Boom(), {"cmd": "release_marker", "seq": 1,
                                         "wall_ms": 1.0})   # must not propagate
    mod._handle_release_marker(object(), {"cmd": "release_marker"})  # no mark_release attr


def test_neutral_latency_route_attestation_cannot_create_release_or_label_state():
    mod = _load_sidecar()

    class _Orch:
        def __init__(self):
            self.route_calls = []
            self.release_calls = 0

        def attest_controller_latency_route(self, route, generation):
            self.route_calls.append((route, generation))
            return True

        def mark_release(self, *_a, **_k):
            self.release_calls += 1
            raise AssertionError("neutral route proof entered release path")

    orch = _Orch()
    assert mod._handle_latency_route_attestation(
        orch, {"cmd": "latency_route_attestation", "delivery_route": "pipe",
               "attestation_generation": "18446744073709551615"})
    assert orch.route_calls == [("pipe", 0xFFFFFFFFFFFFFFFF)]
    assert orch.release_calls == 0
    assert not mod._handle_latency_route_attestation(
        orch, {"cmd": "latency_route_attestation", "delivery_route": "unknown",
               "attestation_generation": "2"})
    for malformed in (None, 1, "", "0", "01", "1.0", "18446744073709551616"):
        assert not mod._handle_latency_route_attestation(
            orch, {"cmd": "latency_route_attestation", "delivery_route": "pipe",
                   "attestation_generation": malformed})
    assert orch.route_calls == [("pipe", 0xFFFFFFFFFFFFFFFF)]


def test_latency_route_attestation_direct_ack_is_exact_and_scope_hashed():
    mod = _load_sidecar()

    class _Orch:
        def __init__(self):
            self.committed = None

        def attest_controller_latency_route(self, route, generation):
            self.committed = (route, generation)
            return True

        def controller_latency_route_attestation_receipt_snapshot(
                self, route, generation):
            assert (route, generation) == self.committed
            return "console=owned|capture=card0|controller=pipe", 17

    msg = {"cmd": "latency_route_attestation", "delivery_route": "pipe",
           "attestation_generation": "9007199254740993"}
    ack = mod._latency_route_attestation_ack_payload(_Orch(), msg)
    assert ack == {
        "event": "latency_route_attestation_ack",
        "accepted": True,
        "delivery_route": "pipe",
        "attestation_generation": "9007199254740993",
        "scope_epoch": "17",
        "scope_digest": hashlib.sha256(
            b"console=owned|capture=card0|controller=pipe").hexdigest(),
        "reason": "",
    }
    assert "console=owned" not in repr(ack)


def test_latency_route_attestation_ack_fails_closed_without_scope_receipt():
    mod = _load_sidecar()

    class _OldOrch:
        def attest_controller_latency_route(self, _route, _generation):
            return True

    ack = mod._latency_route_attestation_ack_payload(
        _OldOrch(), {"delivery_route": "vigem_ds4",
                     "attestation_generation": "8"})
    assert ack["accepted"] is False
    assert ack["attestation_generation"] == "8"
    assert ack["scope_epoch"] == ""
    assert ack["scope_digest"] == ""
    assert ack["reason"] == "scope_commit_unverified"


def test_cold_latency_oracle_wire_exposes_status_without_granting_authority():
    mod = _load_sidecar()
    wire = mod._latency_oracle_wire(_ColdObservableEstimator())

    assert wire["measured_latency_ms"] == 0.0
    assert wire["measured_latency_n"] == 0
    assert wire["measured_latency_ready"] is False
    assert wire["measured_latency_controlled_anchor"] is False
    assert wire["measured_latency_provisional"] is False
    assert wire["measured_latency_restored"] is False
    assert wire["measured_latency_video_route_attested"] is False
    assert wire["measured_latency_attestation_generation"] == ""
    assert wire["measured_latency_delivery_route"] == ""
    assert wire["measured_latency_scope_epoch"] == ""
    assert wire["measured_latency_starved"] is True
    assert wire["measured_latency_status"] == "rejected_near_cap"
    assert wire["measured_latency_last_rejection"] == "near_cap"
    assert wire["measured_latency_method"] == ""
    assert wire["measured_latency_rtt_regime"] == "unknown"
    assert wire["measured_latency_boot"] is True
    assert wire["measured_l_fixed_ms"] == 60.0
    assert wire["measured_latency_sd_ms"] == 12.0
    # [ORION_REOPEN_SOFT] additive diagnostics bit, default OFF -> False on a cold estimator.
    assert wire["measured_latency_reopen_retained"] is False


def test_latency_oracle_wire_encodes_attestation_generation_as_exact_decimal_string():
    mod = _load_sidecar()
    wire = mod._latency_oracle_wire(
        _ColdObservableEstimator(),
        controller_attestation_generation=0xFFFFFFFFFFFFFFFF,
        controller_delivery_route="vigem_ds4", scope_epoch=19)
    assert wire["measured_latency_attestation_generation"] == "18446744073709551615"
    assert wire["measured_latency_delivery_route"] == "vigem_ds4"
    assert wire["measured_latency_scope_epoch"] == "19"

    mismatched = mod._latency_oracle_wire(
        _ColdObservableEstimator(), controller_attestation_generation=7,
        controller_delivery_route="unknown", scope_epoch=19)
    assert mismatched["measured_latency_attestation_generation"] == ""
    assert mismatched["measured_latency_delivery_route"] == ""
    assert mismatched["measured_latency_scope_epoch"] == "19"

    unscoped = mod._latency_oracle_wire(
        _ColdObservableEstimator(), controller_attestation_generation=7,
        controller_delivery_route="pipe")
    assert unscoped["measured_latency_attestation_generation"] == ""
    assert unscoped["measured_latency_delivery_route"] == ""
    assert unscoped["measured_latency_scope_epoch"] == ""


def test_latency_oracle_wire_defaults_new_fields_for_legacy_estimator():
    mod = _load_sidecar()

    class _LegacyEstimator:
        n_labels = 7
        bootstrapped = False

        @staticmethod
        def value_ms():
            return 70.0

        @staticmethod
        def confidence():
            return 0.8

    wire = mod._latency_oracle_wire(_LegacyEstimator())
    assert wire["measured_latency_ms"] == 70.0
    assert wire["measured_latency_n"] == 7
    # A legacy estimator without posterior SD cannot claim native-ready authority.
    assert wire["measured_latency_ready"] is False
    assert wire["measured_latency_provisional"] is False
    assert wire["measured_latency_authority_kind"] == "none"
    assert wire["measured_latency_authority_ms"] == 0.0
    assert wire["measured_latency_authority_sd_ms"] == 0.0
    assert wire["measured_latency_starved"] is False
    assert wire["measured_latency_status"] == "unknown"
    assert wire["measured_latency_last_rejection"] == ""
    assert wire["measured_latency_method"] == ""
    assert wire["measured_latency_attestation_generation"] == ""
    assert wire["measured_latency_delivery_route"] == ""
    assert wire["measured_latency_scope_epoch"] == ""


def test_latency_oracle_wire_publishes_versioned_factory_prior_uncertainty():
    mod = _load_sidecar()

    class _FactoryEstimator:
        n_labels = 0
        bootstrapped = True
        l_fixed_ms = 233.1
        l_fixed_sd_ms = 32.0
        ready_for_native = False
        factory_prior_active = True
        factory_prior_source = "venice-e2e-route-prior:decoder-pipe"
        factory_prior_version = "2026.08.02.1"
        factory_prior_sd_ms = 32.0
        authority_kind = "factory"
        authority_value_ms = 241.4
        authority_sd_ms = 32.0

        @staticmethod
        def value_ms():
            return 241.4

        @staticmethod
        def confidence():
            return 0.15

    wire = mod._latency_oracle_wire(
        _FactoryEstimator(), controller_attestation_generation=91,
        controller_delivery_route="pipe", scope_epoch=23)
    assert wire["measured_latency_factory_prior"] is True
    assert wire["measured_latency_authority_kind"] == "factory"
    assert wire["measured_latency_authority_ms"] == pytest.approx(241.4)
    assert wire["measured_latency_authority_sd_ms"] == pytest.approx(32.0)
    assert wire["measured_latency_prior_source"] == (
        "venice-e2e-route-prior:decoder-pipe")
    assert wire["measured_latency_model_version"] == "2026.08.02.1"
    assert wire["measured_latency_sd_ms"] == 32.0
    assert wire["measured_latency_attestation_generation"] == "91"
    assert wire["measured_latency_delivery_route"] == "pipe"
    assert wire["measured_latency_scope_epoch"] == "23"


def test_latency_oracle_wire_missing_explicit_authority_tuple_fails_closed():
    mod = _load_sidecar()

    class _MixedBuildFactoryEstimator:
        n_labels = 1
        l_fixed_sd_ms = 4.0
        ready_for_native = False
        factory_prior_active = True
        factory_prior_source = "venice-e2e-route-prior:decoder-pipe"
        factory_prior_version = "2026.08.02.1"
        factory_prior_sd_ms = 32.0

        @staticmethod
        def value_ms():
            # Mutable ordinary observation; it must never be reconstructed as
            # the immutable packaged authority by a newer sidecar.
            return 190.9

    wire = mod._latency_oracle_wire(
        _MixedBuildFactoryEstimator(), controller_attestation_generation=91,
        controller_delivery_route="pipe", scope_epoch=23)
    assert wire["measured_latency_ms"] == pytest.approx(190.9)
    assert wire["measured_latency_authority_kind"] == "none"
    assert wire["measured_latency_authority_ms"] == 0.0
    assert wire["measured_latency_authority_sd_ms"] == 0.0
    assert wire["measured_latency_factory_prior"] is False
    assert wire["measured_latency_prior_source"] == ""
    assert wire["measured_latency_model_version"] == ""


def test_latency_oracle_wire_keeps_public_court_rtt_out_of_release_authority():
    mod = _load_sidecar()

    class _CurrentRttEstimator(_ColdObservableEstimator):
        bootstrapped = False
        n_labels = 6
        l_fixed_sd_ms = 3.0
        ready_for_native = True
        provisional_ready = True

        @staticmethod
        def value_ms(rtt_ms=None):
            return 50.0 + float(rtt_ms or 0.0) + 8.3

    wire = mod._latency_oracle_wire(_CurrentRttEstimator(), live_rtt_ms=24.0)
    assert wire["measured_latency_ms"] == pytest.approx(58.3)
    assert wire["measured_latency_provisional"] is True


def test_update_and_wire_snapshot_cannot_publish_a_hybrid_posterior(monkeypatch):
    """Wire serialization observes either side of an update, never its midpoint."""
    mod = _load_sidecar()
    est = LatencyEstimator(restore_cache=False)
    update_midpoint = threading.Event()
    allow_update = threading.Event()
    wire_attempted = threading.Event()
    wire_done = threading.Event()
    failures = []
    wire_payload = []
    est._lock = _ObservedRLock("latency-wire", wire_attempted)

    def _blocking_update(_wall_ms, _fill):
        # Deliberately create a state that would be unsafe if independently
        # sampled: new N/value paired with the old prior SD/readiness.
        est._measured_ms = 72.0
        est._n_labels = 2
        est._mu = 63.7
        update_midpoint.set()
        if not allow_update.wait(2.0):
            raise AssertionError("test did not release update barrier")
        est._var = 16.0
        est._has_controlled_anchor = True
        est._controlled_validation_ready = True
        est._last_status = "calibration_ready"
        est._last_rejection = ""

    monkeypatch.setattr(est, "_classify", _blocking_update)

    def _run_update():
        try:
            est.update(2_000.0, 50.0, present=True, fed=True)
        except BaseException as exc:  # surfaced in the asserting thread
            failures.append(exc)

    def _run_wire():
        try:
            # This is the production contract: capture once, then serialize only
            # the frozen object. _latency_oracle_wire never revisits live state.
            wire_payload.append(mod._latency_oracle_wire(
                est.telemetry_snapshot()))
        except BaseException as exc:  # surfaced in the asserting thread
            failures.append(exc)
        finally:
            wire_done.set()

    update_thread = threading.Thread(target=_run_update, name="latency-update")
    wire_thread = threading.Thread(target=_run_wire, name="latency-wire")
    update_thread.start()
    assert update_midpoint.wait(2.0)
    wire_thread.start()
    assert wire_attempted.wait(2.0)
    assert not wire_done.is_set()  # snapshot waits for the whole update transaction
    allow_update.set()
    update_thread.join(2.0)
    wire_thread.join(2.0)

    assert not update_thread.is_alive()
    assert not wire_thread.is_alive()
    assert failures == []
    assert len(wire_payload) == 1
    wire = wire_payload[0]
    assert wire["measured_latency_ms"] == pytest.approx(72.0)
    assert wire["measured_latency_n"] == 2
    assert wire["measured_l_fixed_ms"] == pytest.approx(63.7)
    assert wire["measured_latency_sd_ms"] == pytest.approx(4.0)
    assert wire["measured_latency_controlled_anchor"] is True
    assert wire["measured_latency_provisional"] is True
    assert wire["measured_latency_ready"] is True
    assert wire["measured_latency_status"] == "calibration_ready"


def test_trusted_live_rtt_requires_ready_verified_finite_snapshot():
    mod = _load_sidecar()

    class _Snap:
        ready = True
        target_verified = True
        rtt_filtered_ms = 24.5

    class _Engine:
        @staticmethod
        def get_snapshot():
            return _Snap()

    class _Orch:
        _rtt_engine = _Engine()

    assert mod._trusted_live_rtt_ms(_Orch()) == pytest.approx(24.5)
    _Snap.ready = False
    assert mod._trusted_live_rtt_ms(_Orch()) is None
    _Snap.ready = True
    _Snap.target_verified = False
    assert mod._trusted_live_rtt_ms(_Orch()) is None
    _Snap.target_verified = True
    for invalid in (0.0, -1.0, float("nan"), float("inf"), 500.1):
        _Snap.rtt_filtered_ms = invalid
        assert mod._trusted_live_rtt_ms(_Orch()) is None


# --------------------------------------------------------------------------- #
#  orch.mark_release -> LatencyEstimator.mark_release (epoch ms end to end)
# --------------------------------------------------------------------------- #
def test_orchestrator_mark_release_feeds_latency_estimator(monkeypatch):
    orch = _build_orch(monkeypatch)
    est = _FakeEstimator()
    orch._latency_estimator = est
    rs = orch.mark_release(1234567.5, 9)
    assert est.calls == [(1234567.5, 9, False)]
    assert rs == 9


def test_orchestrator_mark_release_feeds_calibration_to_estimator(monkeypatch):
    orch = _build_orch(monkeypatch)
    est = _FakeEstimator()
    orch._latency_estimator = est
    rs = orch.mark_release(1234567.5, 9, calibration=True)
    assert est.calls == [(1234567.5, 9, True)]
    assert rs == 9


def test_orchestrator_mark_release_feeds_validation_metadata_to_estimator(monkeypatch):
    orch = _build_orch(monkeypatch)
    est = _FakeEstimator()
    orch._latency_estimator = est
    rs = orch.mark_release(1234567.5, 10, calibration=True,
                           validation_target_pct=88.0,
                           validation_tolerance_pct=3.5)
    assert est.calls == [(1234567.5, 10, True)]
    assert est.validation == [(88.0, 3.5)]
    assert rs == 10


@pytest.mark.parametrize("frame_source", ["capture_card", "decoder"])
def test_public_court_rtt_is_telemetry_only_for_every_video_route(
        monkeypatch, frame_source):
    """A public-peer ping cannot model either HDMI or private Remote Play video."""
    orch = _build_orch(monkeypatch, frame_source=frame_source)
    est = _FakeEstimator()
    orch._latency_estimator = est
    # Capture-card cache-route validation has separate coverage; this test
    # isolates the RTT domain at the marker boundary.
    monkeypatch.setattr(orch, "latency_estimator_snapshot", lambda: est)

    class _VerifiedCourtSnapshot:
        ready = True
        target_verified = True
        rtt_filtered_ms = 80.0

    class _CourtRttEngine:
        @staticmethod
        def get_snapshot():
            return _VerifiedCourtSnapshot()

    orch._rtt_engine = _CourtRttEngine()
    assert orch.mark_release(1234567.5, 12, calibration=True) == 12
    assert est.rtt == [None]


def test_orchestrator_mark_release_stamps_now_when_ts_missing(monkeypatch):
    orch = _build_orch(monkeypatch)
    est = _FakeEstimator()
    orch._latency_estimator = est
    before = time.time() * 1000.0
    orch.mark_release()
    after = time.time() * 1000.0
    (ts, seq, calibration), = est.calls
    assert before <= ts <= after            # epoch ms, stamped at call time
    assert seq is None
    assert calibration is False


def test_orchestrator_mark_release_noop_without_estimator(monkeypatch):
    orch = _build_orch(monkeypatch)
    orch._latency_estimator = None
    assert orch.mark_release(1.0, 1) == 0


def test_release_window_diagnostic_preserves_reader_identity_across_newer_marker(monkeypatch):
    """A delayed shot-1 record cannot be relabeled with the newer shot-2 marker."""
    orch = _build_orch(monkeypatch)
    orch._latency_estimator = _FakeEstimator()
    orch._shot_gate_epoch = 701
    orch._active_pose_arm_token = 801
    assert orch.mark_release(1234567.5, 17, physical_epoch=701, shot_attempt=801) == 17
    orch._shot_gate_epoch = 702
    orch._active_pose_arm_token = 802
    assert orch.mark_release(1234568.5, 18, physical_epoch=702, shot_attempt=802) == 18

    # This callback is deliberately delayed until after marker 18. Identity must come from the
    # reader's immutable shot record, never an orchestrator-global "latest release" variable.
    assert orch._on_green_grade({"label": "EARLY", "release_proxy": False,
                                 "release_seq": 17, "physical_epoch": 701,
                                 "shot_attempt": 801}) is True
    grade = orch.pop_green_grade()
    assert grade["seq"] == 1
    assert grade["release_seq"] == 17
    assert grade["physical_epoch"] == 701
    assert grade["shot_attempt"] == 801

    # Marker identity remains transportable when the optional latency oracle is down.
    orch._latency_estimator = None
    orch._shot_gate_epoch = 703
    orch._active_pose_arm_token = 803
    assert orch.mark_release(1234569.5, 19, physical_epoch=703, shot_attempt=803) == 0
    assert orch._on_green_grade({"label": "GREEN", "release_proxy": False,
                                 "release_seq": 19, "physical_epoch": 703,
                                 "shot_attempt": 803}) is True
    grade = orch.pop_green_grade()
    assert grade["seq"] == 2
    assert grade["release_seq"] == 19


def test_release_window_diagnostic_rejects_proxy_or_missing_native_identity(monkeypatch):
    orch = _build_orch(monkeypatch)
    assert orch._on_green_grade({"label": "GREEN", "release_proxy": True,
                                 "release_seq": 20, "physical_epoch": 1,
                                 "shot_attempt": 2}) is False
    assert orch._on_green_grade({"label": "GREEN", "release_proxy": False,
                                 "release_seq": 0}) is False
    assert orch.pop_green_grade() is None


def test_orchestrator_drops_mismatched_release_identity_before_oracle_or_reader(monkeypatch):
    orch = _build_orch(monkeypatch)
    est = _FakeEstimator()
    orch._latency_estimator = est
    orch._shot_gate_epoch = 55
    orch._active_pose_arm_token = 66
    assert orch.mark_release(1234567.5, 17,
                             physical_epoch=54, shot_attempt=66) == 0
    assert est.calls == []


def test_sidecar_release_window_wire_preserves_identity_and_rejects_proxy():
    mod = _load_sidecar()
    record = {"label": "EARLY", "seq": 3, "release_seq": 17,
              "physical_epoch": 701, "shot_attempt": 801,
              "g_lo": 91.0, "fill_at_release": 86.5, "window_conf": 1.0,
              "release_proxy": False}
    wire = mod._release_window_diagnostic_wire(record)
    assert wire["release_seq"] == 17
    assert wire["physical_epoch"] == "701"
    assert wire["shot_attempt"] == "801"

    assert mod._release_window_diagnostic_wire({**record, "release_proxy": True}) is None
    assert mod._release_window_diagnostic_wire({**record, "release_seq": 0}) is None
    assert mod._release_window_diagnostic_wire({**record, "physical_epoch": 0}) is None
    assert mod._release_window_diagnostic_wire({**record, "window_conf": float("nan")}) is None


# --------------------------------------------------------------------------- #
#  PTS-domain measurement timestamps
# --------------------------------------------------------------------------- #
def test_frame_measurement_uses_pts_mapped_epoch_when_calibrated(monkeypatch):
    orch = _build_orch(monkeypatch)
    orch._last_frame_pts = 5_000_000          # 5.0 s of decoder PTS (µs)
    orch._pts_to_epoch_ms = 1_700_000_000_000.0
    orch._last_frame_epoch_ms = 1_700_000_000_123.0   # raw stamp differs (arrival jitter)
    assert orch._frame_measurement_epoch_ms() == pytest.approx(1_700_000_005_000.0)


def test_frame_measurement_falls_back_to_capture_epoch_without_pts(monkeypatch):
    orch = _build_orch(monkeypatch)
    orch._last_frame_pts = 0                  # capture card / WGC / window: no PTS
    orch._pts_to_epoch_ms = 0.0
    orch._last_frame_epoch_ms = 1_700_000_000_123.0
    assert orch._frame_measurement_epoch_ms() == pytest.approx(1_700_000_000_123.0)


def test_frame_measurement_fails_closed_when_source_clocks_are_missing(monkeypatch):
    orch = _build_orch(monkeypatch)
    orch._last_frame_pts = 0
    orch._pts_to_epoch_ms = 0.0
    orch._last_frame_epoch_ms = 0.0
    assert orch._frame_measurement_epoch_ms() == 0.0


def test_frame_measurement_rejects_pts_mapping_without_source_epoch(monkeypatch):
    orch = _build_orch(monkeypatch)
    orch._last_frame_pts = 5_000_000
    orch._pts_to_epoch_ms = 1_700_000_000_000.0
    orch._last_frame_epoch_ms = 0.0
    assert orch._frame_measurement_epoch_ms() == 0.0
