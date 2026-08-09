"""Production contracts for the no-capture-card decoded-frame path."""

from pathlib import Path
import os
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

import remote_play_orchestrator as rpo
from chiaki_backend import FrameData, FrameRingBuffer, OrionFramePipeBackend
from decoder_pipe_identity import stable_executable_snapshot


def _owned_frame(value=64, width=1280, height=720):
    frame = np.full((height, width, 3), value, dtype=np.uint8)
    frame.setflags(write=False)
    return frame


def _v2_header(backend, *, seq=1, generation=0xA11CE, producer_ns=None,
               width=64, height=32, fmt=None, payload_len=None, pts=1_000_000,
               magic=None, version=None, header_size=None, flags=0, reserved=0):
    if producer_ns is None:
        producer_ns = time.perf_counter_ns()
    if fmt is None:
        fmt = backend._FMT_NV12
    if payload_len is None:
        payload_len = width * height * 3 // 2
    return backend._HEADER.pack(
        backend._MAGIC if magic is None else magic,
        backend._VERSION if version is None else version,
        backend._HEADER.size if header_size is None else header_size,
        flags, generation, seq, producer_ns, width, height, fmt,
        payload_len, pts, reserved,
    )


def test_ring_blocking_read_is_latest_wins_and_consumes_wakeup():
    ring = FrameRingBuffer(capacity=2)
    ring.put(FrameData(frame=_owned_frame(10), frame_number=1))
    ring.put(FrameData(frame=_owned_frame(20), frame_number=2))

    assert ring.get_latest(timeout=0.001).frame_number == 2
    assert ring.get_latest(timeout=0.001) is None


def test_ring_publication_delay_never_rewrites_capture_clock(monkeypatch):
    import chiaki_backend as cb

    capture_ns = 10_000_000
    capture_epoch_ns = 1_700_000_000_010_000_000
    monkeypatch.setattr(cb.time, "perf_counter_ns", lambda: capture_ns + 7_000_000)
    monkeypatch.setattr(cb.time, "time_ns", lambda: capture_epoch_ns + 7_000_000)
    frame = FrameData(
        frame=_owned_frame(), timestamp_ns=capture_ns, epoch_ns=capture_epoch_ns)
    ring = FrameRingBuffer(capacity=2)

    ring.put(frame)
    delivered = ring.get_latest(timeout=0.001)

    assert delivered is not None
    assert delivered.timestamp_ns == delivered.capture_timestamp_ns == capture_ns
    assert delivered.epoch_ns == delivered.capture_epoch_ns == capture_epoch_ns
    assert delivered.publication_timestamp_ns - delivered.capture_timestamp_ns \
        == 7_000_000


def test_decoder_mode_is_event_driven_but_window_mode_is_not():
    class Backend:
        def get_frame(self, timeout=0.02):
            return None

    backend = Backend()
    assert rpo.RemotePlayOrchestrator._backend_is_event_driven("decoder", backend)
    assert rpo.RemotePlayOrchestrator._backend_is_event_driven("capture_card", backend)
    assert not rpo.RemotePlayOrchestrator._backend_is_event_driven("wgc", backend)
    assert not rpo.RemotePlayOrchestrator._backend_is_event_driven("decoder", object())


def test_frame_data_capture_route_defaults_are_fail_closed():
    fd = FrameData(frame=_owned_frame())
    assert fd.capture_api == ""
    assert fd.capture_device_index == -1


def test_orchestrator_prefers_explicit_capture_clock_and_exports_publication_age():
    capture_ns = time.perf_counter_ns() - 20_000_000
    capture_epoch_ns = time.time_ns() - 20_000_000
    frame_data = SimpleNamespace(
        frame=_owned_frame(),
        capture_timestamp_ns=capture_ns,
        # Deliberately conflicting legacy values prove they are fallback-only.
        timestamp_ns=capture_ns + 9_000_000,
        capture_epoch_ns=capture_epoch_ns,
        epoch_ns=capture_epoch_ns + 9_000_000,
        publication_timestamp_ns=capture_ns + 12_500_000,
        frame_number=4,
        source_generation=2,
        pts=777,
        feed_frozen=False,
        integrity_isolated=True,
        y_plane=None,
    )

    class Backend:
        def get_frame_nonblocking(self):
            return frame_data

    orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        auto_launch_client=False, virtual_controller=False))
    orch._frame_backend = Backend()
    # Avoid route-attestation concerns: this test isolates only FrameData clocks.
    orch._frame_backend_mode = "capture"

    frame, pts, epoch_ns, mono_ns = orch._capture_decoded_frame(block=False)

    assert frame is frame_data.frame and pts == 777
    assert mono_ns == capture_ns
    assert epoch_ns == capture_epoch_ns
    assert orch.capture_publication_age_ms() == pytest.approx(12.5)


def test_orchestrator_publication_age_rejects_missing_reversed_and_unbounded_clocks():
    assert rpo.RemotePlayOrchestrator._frame_publication_age_ms(
        SimpleNamespace(timestamp_ns=0, publication_timestamp_ns=1)) == 0.0
    assert rpo.RemotePlayOrchestrator._frame_publication_age_ms(
        SimpleNamespace(timestamp_ns=10, publication_timestamp_ns=9)) == 0.0
    assert rpo.RemotePlayOrchestrator._frame_publication_age_ms(SimpleNamespace(
        capture_timestamp_ns=1,
        publication_timestamp_ns=60_000_000_002,
    )) == 0.0


def _capture_route_orchestrator(monkeypatch, frame_data, backend_route_matches=True):
    monkeypatch.setenv("ORION_MEASURE_LATENCY", "1")
    frame_height, frame_width = frame_data.frame.shape[:2]
    mode_width = int(getattr(frame_data, "capture_width", 0) or frame_width)
    mode_height = int(getattr(frame_data, "capture_height", 0) or frame_height)
    mode_fps = float(getattr(frame_data, "capture_fps", 0.0) or 60.0)
    mode_fourcc = str(getattr(frame_data, "capture_fourcc", "") or "MJPG")
    mode_buffer = float(getattr(frame_data, "capture_buffer_size", 0.0) or 1.0)
    capture_mode = rpo._capture_mode_descriptor(
        (mode_width, mode_height, mode_fps, mode_fourcc, mode_buffer))
    orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        frame_source="capture_card", auto_launch_client=False,
        virtual_controller=False,
        capture_mode=capture_mode,
    ))

    if not getattr(frame_data, "capture_width", 0):
        frame_data.capture_width = mode_width
        frame_data.capture_height = mode_height
        frame_data.capture_fps = mode_fps
        frame_data.capture_fourcc = mode_fourcc
        frame_data.capture_buffer_size = mode_buffer

    class Backend:
        route = ("DSHOW", 1) if backend_route_matches else ("MSMF", 1)

        def get_frame(self, timeout=0.02):
            return frame_data

        def get_frame_nonblocking(self):
            return frame_data

        def warm_cache_route_matches(self, expected):
            assert expected == 1
            return backend_route_matches

        def active_route(self):
            return self.route

        def negotiated_mode(self):
            return (
                frame_data.capture_width, frame_data.capture_height,
                frame_data.capture_fps, frame_data.capture_fourcc,
                frame_data.capture_buffer_size,
            )

    orch._frame_backend = Backend()
    orch._frame_backend_mode = "capture_card"
    orch._capture_warm_cache_expected_index = 1
    return orch


def test_capture_frame_msmf_fallback_revokes_warm_estimator_before_return(monkeypatch):
    fd = FrameData(
        frame=_owned_frame(), frame_number=1, timestamp_ns=time.perf_counter_ns(),
        capture_api="MSMF", capture_device_index=1,
    )
    orch = _capture_route_orchestrator(monkeypatch, fd)
    warm = object()
    orch._latency_estimator = warm
    initial_generation = orch._frame_integrity_generation

    frame, *_ = orch._capture_decoded_frame(block=True)

    assert frame is fd.frame
    assert orch._capture_warm_cache_revoked
    assert not orch._capture_warm_cache_verified
    assert orch._latency_estimator is not warm
    assert getattr(orch._latency_estimator, "_route_scope", None) == ""
    assert orch._frame_integrity_generation > initial_generation

    # Revocation is one-way, but the same cold estimator survives subsequent
    # frames on the same actual route so online calibration can still converge.
    cold = orch._latency_estimator
    cold_generation = orch._frame_integrity_generation
    frame, *_ = orch._capture_decoded_frame(block=True)
    assert frame is fd.frame
    assert orch._latency_estimator is cold
    assert orch._frame_integrity_generation == cold_generation

    # A later route transition stays cold and clears MSMF-specific evidence
    # before the first DirectShow frame can feed the estimator.
    fd.capture_api = "DSHOW"
    frame, *_ = orch._capture_decoded_frame(block=True)
    assert frame is fd.frame
    assert orch._latency_estimator is not cold
    assert getattr(orch._latency_estimator, "_route_scope", None) == ""
    assert orch._frame_integrity_generation > cold_generation


def test_capture_frame_wrong_index_or_missing_stamp_revokes_warm_estimator(monkeypatch):
    for api, index in (("DSHOW", 2), ("", -1)):
        fd = FrameData(
            frame=_owned_frame(), frame_number=1,
            timestamp_ns=time.perf_counter_ns(), capture_api=api,
            capture_device_index=index,
        )
        orch = _capture_route_orchestrator(monkeypatch, fd)
        warm = object()
        orch._latency_estimator = warm

        orch._capture_decoded_frame(block=True)

        assert orch._capture_warm_cache_revoked
        assert orch._latency_estimator is not warm


def test_capture_route_rechecked_before_telemetry_even_without_new_frame(monkeypatch):
    fd = FrameData(
        frame=_owned_frame(), capture_api="DSHOW", capture_device_index=1,
    )
    orch = _capture_route_orchestrator(
        monkeypatch, fd, backend_route_matches=False)
    warm = object()
    orch._latency_estimator = warm

    snapshot = orch.latency_estimator_snapshot()

    assert orch._capture_warm_cache_revoked
    assert snapshot is orch._latency_estimator
    assert snapshot is not warm

    # The persisted cache remains revoked, and a later API transition clears
    # route-specific online evidence before telemetry can emit it on DSHOW.
    cold_msmf = snapshot
    orch._frame_backend.route = ("DSHOW", 1)
    snapshot = orch.latency_estimator_snapshot()
    assert snapshot is orch._latency_estimator
    assert snapshot is not cold_msmf
    assert getattr(snapshot, "_route_scope", None) == ""


def test_capture_frame_matching_dshow_index_preserves_warm_estimator(monkeypatch):
    fd = FrameData(
        frame=_owned_frame(), frame_number=1, timestamp_ns=time.perf_counter_ns(),
        capture_api="DSHOW", capture_device_index=1,
    )
    orch = _capture_route_orchestrator(monkeypatch, fd)
    warm = object()
    orch._latency_estimator = warm
    initial_generation = orch._frame_integrity_generation

    frame, *_ = orch._capture_decoded_frame(block=True)

    assert frame is fd.frame
    assert orch._capture_warm_cache_verified
    assert not orch._capture_warm_cache_revoked
    assert orch._latency_estimator is warm
    assert orch._frame_integrity_generation == initial_generation


def test_capture_mode_canonicalizes_unsupported_directshow_buffer_reporting():
    # The production HD60 X + OpenCV DirectShow route reports -1 because
    # CAP_PROP_BUFFERSIZE is unsupported.  Geometry/fps/fourcc remain exact and
    # the unsupported field must not erase the entire controller/video scope.
    expected = "1920x1080@60.000|fourcc=yuy2|buffer=unreported"
    assert rpo._capture_mode_descriptor(
        (1920, 1080, 60.00024, "YUY2", -1.0)) == expected
    assert rpo._capture_mode_descriptor(
        (1920, 1080, 60.00024, "YUY2", 0.0)) == expected

    # Unknown negative sentinels and incomplete modes remain fail closed.
    assert rpo._capture_mode_descriptor(
        (1920, 1080, 60.0, "YUY2", -2.0)) == ""
    assert rpo._capture_mode_descriptor((0, 0, 0.0, "", -1.0)) == ""


def test_capture_unreported_buffer_allows_exact_pipe_attestation(monkeypatch):
    """Reproduce the live preview -> DirectShow -> neutral Pipe proof order."""
    monkeypatch.setenv("ORION_MEASURE_LATENCY", "1")
    monkeypatch.setenv("ORION_CAPTURE_CARD_INDEX", "0")
    monkeypatch.setenv(
        "ORION_VIDEO_DEVICE_NAMES",
        "Elgato HD60 X|Logitech Webcam C925e|OBS Virtual Camera")
    monkeypatch.setenv(
        "ORION_VIDEO_DEVICE_IDS",
        "|".join((
            "dshow-moniker-sha256-v1:" + "1" * 64,
            "dshow-moniker-sha256-v1:" + "2" * 64,
            "dshow-moniker-sha256-v1:" + "3" * 64,
        )))
    orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        frame_source="capture_card", auto_launch_client=False,
        virtual_controller=False,
        console_identity="registered-host-sha256-v1:" + "a" * 64,
        # Production starts preview before native proves the active output route.
        controller_route="", capture_mode="",
    ))
    orch._capture_warm_cache_expected_index = 0
    frame_data = FrameData(
        frame=_owned_frame(width=1920, height=1080), frame_number=1,
        timestamp_ns=time.perf_counter_ns(), capture_api="DSHOW",
        capture_device_index=0, capture_width=1920, capture_height=1080,
        capture_fps=60.00024, capture_fourcc="YUY2",
        capture_buffer_size=-1.0,
    )

    assert orch._guard_capture_latency_route(frame_data=frame_data)
    assert not orch._capture_warm_cache_revoked
    assert orch.config.capture_mode == (
        "1920x1080@60.000|fourcc=yuy2|buffer=unreported")

    # A fresh neutral local delivery supplies the final missing route component.
    # The exact scope is then committed and can be echoed to native immediately.
    assert orch.attest_controller_latency_route("pipe", 1)
    receipt = orch.controller_latency_route_attestation_receipt("pipe", 1)
    assert receipt
    assert receipt == rpo._latency_route_scope(orch.config)

    class _LiveBackend:
        @staticmethod
        def active_route():
            return "DSHOW", 0

        @staticmethod
        def negotiated_mode():
            return 1920, 1080, 60.00024, "YUY2", -1.0

    orch._frame_backend = _LiveBackend()
    orch._frame_backend_mode = "capture_card"
    estimator, generation, route = orch.latency_estimator_attestation_snapshot()
    telemetry = estimator.telemetry_snapshot()
    assert (generation, route) == (1, "pipe")
    assert telemetry.factory_prior_active
    assert telemetry.value_ms > 0.0
    assert 6.0 <= telemetry.factory_prior_sd_ms <= 100.0


def test_capture_backend_metadata_waits_for_first_exact_frame(monkeypatch):
    monkeypatch.setenv("ORION_MEASURE_LATENCY", "1")
    monkeypatch.setenv("ORION_CAPTURE_CARD_INDEX", "0")
    monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", "Elgato HD60 X")
    monkeypatch.setenv(
        "ORION_VIDEO_DEVICE_IDS", "dshow-moniker-sha256-v1:" + "1" * 64)
    orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        frame_source="capture_card", auto_launch_client=False,
        virtual_controller=False,
        console_identity="registered-host-sha256-v1:" + "a" * 64,
        controller_route="pipe", capture_mode="",
    ))
    orch._capture_warm_cache_expected_index = 0
    backend = SimpleNamespace(
        active_route=lambda: ("DSHOW", 0),
        negotiated_mode=lambda: (1920, 1080, 60.00024, "YUY2", -1.0),
    )

    # OpenCV properties may pre-load the scoped posterior, but they cannot prove
    # the pixels have the claimed geometry or authorize a controller ACK.
    assert not orch._guard_capture_latency_route(backend=backend)
    assert not orch._capture_warm_cache_verified
    assert not orch.attest_controller_latency_route("pipe", 9)

    frame_data = FrameData(
        frame=_owned_frame(width=1920, height=1080), frame_number=1,
        timestamp_ns=time.perf_counter_ns(), capture_api="DSHOW",
        capture_device_index=0, capture_width=1920, capture_height=1080,
        capture_fps=60.00024, capture_fourcc="YUY2",
        capture_buffer_size=-1.0,
    )
    assert orch._guard_capture_latency_route(frame_data=frame_data)
    assert orch._capture_warm_cache_verified
    assert orch.attest_controller_latency_route("pipe", 9)


def test_capture_frame_geometry_must_match_negotiated_stamp(monkeypatch):
    fd = FrameData(
        # A valid 720p ndarray falsely stamped as the configured 1080p mode.
        frame=_owned_frame(width=1280, height=720), frame_number=1,
        timestamp_ns=time.perf_counter_ns(), capture_api="DSHOW",
        capture_device_index=1, capture_width=1920, capture_height=1080,
        capture_fps=60.0, capture_fourcc="MJPG", capture_buffer_size=1.0,
    )
    orch = _capture_route_orchestrator(monkeypatch, fd)
    warm = object()
    orch._latency_estimator = warm
    integrity_generation = orch._frame_integrity_generation
    scope_epoch = orch.latency_estimator_scope_epoch()
    replacement_observations = []
    replace = orch._replace_latency_authority

    def observe_replace(route_scope, reason, **kwargs):
        replacement_observations.append((
            orch._capture_warm_cache_revoked,
            orch._capture_warm_cache_verified,
            route_scope,
        ))
        return replace(route_scope, reason, **kwargs)

    monkeypatch.setattr(orch, "_replace_latency_authority", observe_replace)

    assert not orch._guard_capture_latency_route(frame_data=fd)
    assert replacement_observations == [(True, False, "")]
    assert orch._capture_warm_cache_revoked
    assert not orch._capture_warm_cache_verified
    assert orch._latency_route_scope_value == ""
    assert orch._latency_estimator is not warm
    assert orch._frame_integrity_generation > integrity_generation
    assert orch.latency_estimator_scope_epoch() == scope_epoch + 1


def test_capture_revocation_cannot_be_rekeyed_or_acknowledged(monkeypatch):
    monkeypatch.setenv("ORION_MEASURE_LATENCY", "1")
    monkeypatch.setenv("ORION_CAPTURE_CARD_INDEX", "0")
    monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", "Elgato HD60 X")
    monkeypatch.setenv(
        "ORION_VIDEO_DEVICE_IDS", "dshow-moniker-sha256-v1:" + "1" * 64)
    orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        frame_source="capture_card", auto_launch_client=False,
        virtual_controller=False,
        console_identity="registered-host-sha256-v1:" + "a" * 64,
        controller_route="pipe",
        capture_mode="1920x1080@60.000|fourcc=yuy2|buffer=unreported",
    ))
    orch._capture_warm_cache_expected_index = 0
    fd = FrameData(
        frame=_owned_frame(width=1920, height=1080), frame_number=1,
        timestamp_ns=time.perf_counter_ns(), capture_api="DSHOW",
        capture_device_index=0, capture_width=1920, capture_height=1080,
        capture_fps=60.00024, capture_fourcc="YUY2",
        capture_buffer_size=-1.0,
    )

    assert orch._guard_capture_latency_route(frame_data=fd)
    assert orch.attest_controller_latency_route("pipe", 7)
    assert orch.controller_latency_route_attestation_receipt("pipe", 7)

    # The same sample now claims the wrong hardware index. Revocation is
    # published before authority replacement and is one-way for this process.
    fd.capture_device_index = 1
    assert not orch._guard_capture_latency_route(frame_data=fd)
    assert orch._capture_warm_cache_revoked
    assert orch._latency_route_scope_value == ""
    assert not orch.attest_controller_latency_route("vigem_ds4", 8)
    assert orch.config.controller_route == "pipe"
    assert orch.controller_latency_route_attestation_receipt("pipe", 7) == ""
    assert orch.controller_latency_route_attestation_receipt_snapshot(
        "pipe", 7) == ("", 0)

    # Direct re-key attempts and telemetry snapshots must also stay unscoped.
    orch._rekey_latency_route("revoked_retry")
    assert orch._latency_route_scope_value == ""
    orch._replace_latency_authority(
        rpo._latency_route_scope(orch.config), "revoked_direct_replace",
        fence_frames=False, restore_cache=True)
    assert orch._latency_route_scope_value == ""
    assert getattr(orch._latency_estimator, "_route_scope", None) == ""
    orch._frame_backend = SimpleNamespace(
        active_route=lambda: ("DSHOW", 0),
        negotiated_mode=lambda: (1920, 1080, 60.00024, "YUY2", -1.0),
    )
    orch._frame_backend_mode = "capture_card"
    estimator, generation, route = orch.latency_estimator_attestation_snapshot()
    assert estimator is orch._latency_estimator
    assert (generation, route) == (0, "")


def test_capture_frame_missing_negotiated_mode_revokes_warm_estimator(monkeypatch):
    fd = FrameData(
        frame=_owned_frame(), frame_number=1, timestamp_ns=time.perf_counter_ns(),
        capture_api="DSHOW", capture_device_index=1,
    )
    orch = _capture_route_orchestrator(monkeypatch, fd)
    fd.capture_width = 0
    fd.capture_height = 0
    fd.capture_fps = 0.0
    fd.capture_fourcc = ""
    fd.capture_buffer_size = 0.0
    warm = object()
    orch._latency_estimator = warm

    orch._capture_decoded_frame(block=True)

    assert orch._capture_warm_cache_revoked
    assert orch._latency_estimator is not warm


def test_capture_mode_transition_fences_old_completion_and_estimator_feed(monkeypatch):
    import latency_estimator as latency_module

    fd = FrameData(
        frame=_owned_frame(width=1920, height=1080), frame_number=1,
        timestamp_ns=time.perf_counter_ns(), capture_api="DSHOW",
        capture_device_index=1, capture_width=1920, capture_height=1080,
        capture_fps=60.0, capture_fourcc="MJPG", capture_buffer_size=1.0,
    )
    orch = _capture_route_orchestrator(monkeypatch, fd)

    class Estimator:
        def __init__(self):
            self.updates = []

        def update(self, *args):
            self.updates.append(args)

    old_estimator = Estimator()
    replacement = Estimator()
    orch._latency_estimator = old_estimator
    orch._latency_controller_attestation_generation = 73
    orch._latency_controller_attestation_scope = "old-mode"
    orch._latency_controller_attestation_route = "pipe"

    # Establish the configured route. An unchanged, valid route must neither
    # replace timing nor advance the detector generation.
    assert orch._guard_capture_latency_route(frame_data=fd)
    old_generation = orch._frame_integrity_generation
    assert orch._latency_estimator is old_estimator

    loads = []

    def load_replacement(route_scope="", cache_path=None, restore_cache=True):
        loads.append((route_scope, restore_cache))
        return replacement

    monkeypatch.setattr(latency_module, "try_load", load_replacement)

    # HDMI renegotiates in place while an old-mode detector job is outstanding.
    fd.frame = _owned_frame(width=1280, height=720)
    fd.capture_width = 1280
    fd.capture_height = 720
    assert orch._guard_capture_latency_route(frame_data=fd)

    assert loads == [(rpo._latency_route_scope(orch.config), False)]
    assert orch._latency_estimator is replacement
    assert orch._frame_integrity_generation > old_generation
    assert orch._latency_controller_attestation_generation == 0
    assert orch._latency_controller_attestation_scope == ""
    assert orch._latency_controller_attestation_route == ""
    assert orch._latency_estimator_for_frame(old_generation) is None
    assert orch._latency_estimator_for_frame(
        orch._frame_integrity_generation) is replacement

    # A successful completion from the old mode cannot re-attest health or
    # acquire the replacement estimator for a timing update.
    orch._finish_processed_frame(
        seq=91, source_frame_number=91, backend_frozen=False, success=True,
        frame_ts=time.perf_counter(), epoch_ms=time.time() * 1000.0,
        pts=0, frame_wh=(1280, 720),
        integrity_generation=old_generation,
    )
    assert not replacement.updates
    assert orch._capture_integrity_healthy is False
    assert orch._processed_frame_snapshot.integrity_healthy is False

    # The next observation of the same negotiated mode preserves the new
    # authority and generation; only a real transition is fenced.
    current_generation = orch._frame_integrity_generation
    assert orch._guard_capture_latency_route(frame_data=fd)
    assert orch._latency_estimator is replacement
    assert orch._frame_integrity_generation == current_generation
    assert len(loads) == 1


def test_required_pipe_policy_is_no_card_production_only(monkeypatch):
    monkeypatch.setenv("ORION_REQUIRE_FRAME_PIPE", "1")
    monkeypatch.delenv("ORION_CAPTURE_CARD", raising=False)

    remote_play = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        frame_source="auto", auto_launch_client=False, virtual_controller=False,
    ))
    capture_card = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        frame_source="capture_card", auto_launch_client=False,
        virtual_controller=False,
    ))

    assert remote_play._frame_pipe_required
    assert not capture_card._frame_pipe_required


def test_required_pipe_preattach_failure_is_explicit_and_has_no_backend(monkeypatch):
    monkeypatch.setenv("ORION_REQUIRE_FRAME_PIPE", "1")
    monkeypatch.setenv("ORION_FRAME_PIPE", "0")  # A stale dev override cannot weaken it.
    monkeypatch.delenv("CHIAKI_ORION_FRAME_PIPE", raising=False)
    monkeypatch.delenv("ORION_CAPTURE_CARD", raising=False)

    class FailingPipeBackend:
        def __init__(self, pipe_name):
            assert pipe_name == r"\\.\pipe\orion_frames"

        def start(self):
            return False

    import chiaki_backend
    monkeypatch.setattr(chiaki_backend, "OrionFramePipeBackend", FailingPipeBackend)
    orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        frame_source="wgc", auto_launch_client=False, virtual_controller=False,
    ))

    orch._preattach_decoder_pipe()

    assert orch._frame_pipe_required
    assert orch._frame_backend is None
    assert "window fallback is disabled" in orch._last_error_msg.lower()


def test_stop_reaps_partial_start_client_and_frame_backend():
    """A failed required-pipe verdict must not leave OrionStream/pipe ownership alive."""

    class Resource:
        def __init__(self):
            self.stops = 0

        def stop(self):
            self.stops += 1

    orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        frame_source="decoder", auto_launch_client=False,
        virtual_controller=False,
    ))
    client = Resource()
    backend = Resource()
    rtt = Resource()
    orch._running = False  # start() never reached its Running transition
    orch._client_manager = client
    orch._frame_backend = backend
    orch._frame_backend_mode = "decoder"
    orch._rtt_engine = rtt

    orch.stop()

    assert client.stops == 1
    assert backend.stops == 1
    assert rtt.stops == 1
    assert orch._client_manager is None
    assert orch._frame_backend is None
    assert orch._frame_backend_mode == "capture"
    assert orch._rtt_engine is None


def test_repeated_stop_never_tears_backend_down_under_stuck_capture_thread():
    """A second cleanup attempt must retain the worker/backend ownership join."""

    class ThreadResource:
        def __init__(self):
            self.alive = True
            self.joins = 0

        def join(self, timeout=None):
            self.joins += 1

        def is_alive(self):
            return self.alive

    class Resource:
        def __init__(self):
            self.stops = 0

        def stop(self):
            self.stops += 1

    orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        frame_source="decoder", auto_launch_client=False,
        virtual_controller=False,
    ))
    worker = ThreadResource()
    backend = Resource()
    orch._running = False
    orch._capture_thread = worker
    orch._frame_backend = backend

    orch.stop()

    assert orch._capture_thread is worker
    assert orch._frame_backend is backend
    assert backend.stops == 0

    worker.alive = False
    orch.stop()

    assert worker.joins == 2
    assert orch._capture_thread is None
    assert orch._frame_backend is None
    assert backend.stops == 1


def test_required_pipe_capture_loop_never_calls_window_capture(monkeypatch):
    monkeypatch.setenv("ORION_REQUIRE_FRAME_PIPE", "1")
    monkeypatch.delenv("ORION_CAPTURE_CARD", raising=False)
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace())
    orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        frame_source="auto", auto_launch_client=False, virtual_controller=False,
    ))
    rejects = []

    def stop_after_reject(reason, fail_closed=True):
        rejects.append((reason, fail_closed))
        orch._last_frame_reject_reason = reason
        orch._running = False

    monkeypatch.setattr(orch, "_note_frame_reject", stop_after_reject)
    monkeypatch.setattr(
        orch, "_capture_window_tier",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("production required-pipe path reached window capture")),
    )
    monkeypatch.setattr(rpo.time, "sleep", lambda _seconds: None)
    orch._running = True

    orch._capture_loop()

    assert rejects == [("decoder_frame_pipe_unavailable", True)]
    assert orch._frame_seq == 0


def test_decoder_capture_loop_waits_once_and_has_no_poll_pacing(monkeypatch):
    orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        frame_source="decoder", auto_launch_client=False,
        virtual_controller=False, target_fps=60,
    ))

    class Backend:
        def __init__(self):
            self.blocking_calls = 0
            self.nonblocking_calls = 0

        def get_frame(self, timeout=0.02):
            self.blocking_calls += 1
            # End after this producer frame. The current iteration must still publish it.
            orch._running = False
            return FrameData(
                frame=_owned_frame(),
                timestamp_ns=time.perf_counter_ns(),
                epoch_ns=time.time_ns(),
                frame_number=1,
                source_generation=1,
                integrity_isolated=True,
            )

        def get_frame_nonblocking(self):
            self.nonblocking_calls += 1
            raise AssertionError("decoder path polled instead of waiting for producer")

    sleeps = []
    monkeypatch.setattr(rpo.time, "sleep", lambda seconds: sleeps.append(seconds))
    backend = Backend()
    orch._frame_backend = backend
    orch._frame_backend_mode = "decoder"
    orch._running = True
    orch._capture_loop()

    assert backend.blocking_calls == 1
    assert backend.nonblocking_calls == 0
    assert sleeps == []
    assert orch._frame_seq == 1
    assert orch._frame_bundle[0].flags["OWNDATA"]
    assert not orch._frame_bundle[0].flags["WRITEABLE"]


def test_capture_card_and_decoder_publish_the_same_detector_frame_contract(monkeypatch):
    """Both production eyes must feed identical owned 720p pixels to timing."""
    published = []
    for mode in ("capture_card", "decoder"):
        orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
            frame_source=mode, auto_launch_client=False,
            virtual_controller=False, target_fps=60,
        ))

        class Backend:
            def get_frame(self, timeout=0.02):
                orch._running = False
                return FrameData(
                    frame=_owned_frame(value=91, width=1920, height=1080),
                    timestamp_ns=time.perf_counter_ns(),
                    epoch_ns=time.time_ns(),
                    frame_number=1,
                    source_generation=1,
                    integrity_isolated=True,
                    capture_api="DSHOW" if mode == "capture_card" else "",
                    capture_device_index=0 if mode == "capture_card" else -1,
                )

            def get_frame_nonblocking(self):
                raise AssertionError("production source was polled instead of event-driven")

        backend = Backend()
        orch._frame_backend = backend
        orch._frame_backend_mode = mode
        # Route-identity behavior has dedicated tests; this assertion isolates
        # the downstream pixel/timestamp contract shared by both valid routes.
        monkeypatch.setattr(orch, "_guard_capture_latency_route", lambda **_kwargs: True)
        orch._running = True
        orch._capture_loop()

        frame = orch._frame_bundle[0]
        assert orch._frame_seq == 1
        assert frame.shape == (720, 1280, 3)
        assert frame.flags["OWNDATA"] and not frame.flags["WRITEABLE"]
        published.append(frame)

    assert np.array_equal(published[0], published[1])


def test_same_backend_new_connection_generation_resets_source_identity():
    orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        auto_launch_client=False, virtual_controller=False,
    ))
    backend = object()
    first = orch._backend_source_identity(backend, 1)
    assert orch._backend_source_identity(backend, 1) == first
    second = orch._backend_source_identity(backend, 2)
    assert second == first + 1


def test_source_generation_transition_replaces_fresh_timing_and_fences_old_completion():
    orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        frame_source="decoder", auto_launch_client=False, virtual_controller=False,
    ))

    class FreshAuthoritative:
        restored_from_cache = False
        n_labels = 6
        ready_for_native = True
        _pending_release_ms = 123.0

    fresh = FreshAuthoritative()
    orch._latency_estimator = fresh
    orch._latency_controller_attestation_generation = 77
    orch._latency_controller_attestation_scope = "old"
    orch._latency_controller_attestation_route = "pipe"
    orch._source_identity = 10
    old_integrity_generation = orch._frame_integrity_generation
    frame = _owned_frame(value=70)
    assert orch._source_frame_reason(
        frame, 11, 1, time.perf_counter_ns(), time.perf_counter()) == ""
    assert orch._latency_estimator is not fresh
    assert orch._latency_controller_attestation_generation == 0
    assert orch._latency_controller_attestation_scope == ""
    assert orch._latency_controller_attestation_route == ""
    assert orch._frame_integrity_generation > old_integrity_generation
    assert orch._capture_integrity_healthy is False
    orch._finish_processed_frame(
        seq=99,
        source_frame_number=99,
        backend_frozen=False,
        success=True,
        frame_ts=time.perf_counter(),
        epoch_ms=time.time() * 1000.0,
        pts=99,
        frame_wh=(1280, 720),
        integrity_generation=old_integrity_generation,
    )
    assert orch._capture_integrity_healthy is False
    assert orch._processed_frame_snapshot.integrity_healthy is False


def test_neutral_controller_route_rekeys_without_creating_release_state(monkeypatch):
    orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        frame_source="decoder", auto_launch_client=False, virtual_controller=False,
    ))
    rekeys = []
    monkeypatch.setattr(orch, "_rekey_latency_route", lambda reason: rekeys.append(reason))
    pending_before = getattr(orch._latency_estimator, "_pending_release_ms", None)
    labels_before = getattr(orch._latency_estimator, "n_labels", 0)

    # The route name is accepted/re-keyed, but this deliberately incomplete decoder config has no
    # console/binary identity and therefore cannot become a cache scope.
    assert not orch.attest_controller_latency_route("pipe", 7)
    assert orch.config.controller_route == "pipe"
    assert rekeys == ["controller_delivery_route_transition"]
    assert getattr(orch._latency_estimator, "_pending_release_ms", None) == pending_before
    assert getattr(orch._latency_estimator, "n_labels", 0) == labels_before
    assert not orch.attest_controller_latency_route("unknown", 8)
    assert not orch.attest_controller_latency_route("pipe", 0)


def test_pipe_header_validation_is_exact_and_bounded():
    b = OrionFramePipeBackend
    assert b._HEADER.size == 64
    assert b._wire_contract_reason(
        b._MAGIC, b._VERSION, b._HEADER.size, 0, 1, 1, 1, 0) == ""
    assert b._wire_contract_reason(
        0, b._VERSION, b._HEADER.size, 0, 1, 1, 1, 0) == "magic"
    assert b._wire_contract_reason(
        b._MAGIC, 1, b._HEADER.size, 0, 1, 1, 1, 0) == "version"
    assert b._wire_contract_reason(
        b._MAGIC, b._VERSION, 28, 0, 1, 1, 1, 0) == "header_size"
    assert b._wire_contract_reason(
        b._MAGIC, b._VERSION, b._HEADER.size, 1, 1, 1, 1, 0) == "flags"
    assert b._wire_contract_reason(
        b._MAGIC, b._VERSION, b._HEADER.size, 0, 0, 1, 1, 0) == "generation"
    assert b._wire_contract_reason(
        b._MAGIC, b._VERSION, b._HEADER.size, 0, 1, 0, 1, 0) == "sequence"
    assert b._wire_contract_reason(
        b._MAGIC, b._VERSION, b._HEADER.size, 0, 1, 1, 0, 0) == "producer_timestamp"
    assert b._wire_contract_reason(
        b._MAGIC, b._VERSION, b._HEADER.size, 0, 1, 1, 1, 4) == "reserved"
    assert b._wire_header_reason(1280, 720, b._FMT_NV12, 1280 * 720 * 3 // 2) == ""
    assert b._wire_header_reason(1281, 720, b._FMT_NV12, 1281 * 720 * 3 // 2) == "chroma_geometry"
    assert b._wire_header_reason(1280, 720, 99, 1280 * 720 * 3 // 2) == "format"
    assert b._wire_header_reason(1280, 720, b._FMT_NV12, 7) == "payload_length"
    assert b._wire_header_reason(8192, 720, b._FMT_NV12, 8192 * 720 * 3 // 2) == "geometry"


def test_pipe_frame_is_stamped_before_conversion_and_isolated(monkeypatch):
    backend = OrionFramePipeBackend()
    width, height = 64, 32
    payload = bytes(width * height * 3 // 2)
    producer1 = time.perf_counter_ns()
    producer2 = producer1 + 1_000
    header1 = _v2_header(
        backend, seq=1, generation=99, producer_ns=producer1,
        width=width, height=height, payload_len=len(payload), pts=1234)
    header2 = _v2_header(
        backend, seq=2, generation=99, producer_ns=producer2,
        width=width, height=height, payload_len=len(payload), pts=17_901)
    reads = iter((header1, payload, header2, payload, None))
    monkeypatch.setattr(backend, "_read_exact", lambda _n: next(reads))

    def convert(_payload, w, h, _fmt):
        time.sleep(0.012)
        return np.zeros((h, w, 3), dtype=np.uint8)

    monkeypatch.setattr(backend, "_to_bgr", convert)
    backend._connection_generation = 7
    backend._connected = True
    backend._reader_loop()
    fd = backend.get_frame_nonblocking()

    assert fd is not None
    assert (time.perf_counter_ns() - fd.timestamp_ns) / 1e6 >= 10.0
    assert fd.source_generation == 7
    assert fd.producer_generation == 99
    assert fd.producer_sequence == 2 and fd.frame_number == 2
    assert fd.producer_timestamp_ns == producer2
    assert fd.capture_timestamp_ns == producer2 == fd.timestamp_ns
    assert fd.arrival_timestamp_ns >= producer2
    assert fd.publication_timestamp_ns >= fd.arrival_timestamp_ns
    assert (fd.publication_timestamp_ns - fd.arrival_timestamp_ns) / 1e6 >= 10.0
    assert fd.publication_epoch_ns >= fd.capture_epoch_ns == fd.epoch_ns
    assert fd.integrity_isolated
    assert fd.frame.flags["OWNDATA"] and not fd.frame.flags["WRITEABLE"]
    assert fd.y_plane is not None and not fd.y_plane.flags["WRITEABLE"]
    assert backend.is_ready()


def test_pipe_readiness_requires_accepted_frame_and_clears_on_stop(monkeypatch):
    backend = OrionFramePipeBackend()
    assert not backend.is_ready()

    width, height = 64, 32
    payload = bytes(width * height * 3 // 2)
    now_ns = time.perf_counter_ns()
    header1 = _v2_header(
        backend, seq=1, producer_ns=now_ns, width=width, height=height,
        payload_len=len(payload), pts=1_000_000)
    header2 = _v2_header(
        backend, seq=2, producer_ns=now_ns + 1_000,
        width=width, height=height, payload_len=len(payload), pts=1_016_667)
    reads = iter((header1, payload, header2, payload, None))
    monkeypatch.setattr(backend, "_read_exact", lambda _n: next(reads))
    monkeypatch.setattr(
        backend, "_to_bgr",
        lambda _payload, w, h, _fmt: np.zeros((h, w, 3), dtype=np.uint8),
    )
    backend._connected = True

    backend._reader_loop()

    assert backend.wait_until_ready(0.0)
    backend.stop()
    assert not backend.is_ready()


def test_required_orchestrator_waits_for_real_v2_frame(monkeypatch):
    monkeypatch.setenv("ORION_REQUIRE_FRAME_PIPE", "1")
    monkeypatch.setenv("ORION_FRAME_PIPE_READY_TIMEOUT_S", "0.1")
    orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        frame_source="auto", auto_launch_client=False, virtual_controller=False,
    ))

    class Backend:
        def __init__(self, ready):
            self.ready = ready
            self.timeouts = []

        def wait_until_ready(self, timeout_s):
            self.timeouts.append(timeout_s)
            return self.ready

    pending = Backend(False)
    orch._frame_backend = pending
    orch._frame_backend_mode = "decoder"
    assert not orch._wait_for_required_frame_pipe()
    assert pending.timeouts == [0.1]
    assert "no fresh valid v2 frame" in orch._last_error_msg

    ready = Backend(True)
    orch._frame_backend = ready
    orch._last_error_msg = ""
    assert orch._wait_for_required_frame_pipe()
    assert ready.timeouts == [0.1]


def test_pipe_sequence_regression_stops_without_reading_second_payload(monkeypatch):
    backend = OrionFramePipeBackend()
    width, height = 64, 32
    payload = bytes(width * height * 3 // 2)
    header = _v2_header(
        backend, seq=4, width=width, height=height,
        payload_len=len(payload), pts=1)
    reads = iter((header, payload, header))
    calls = []

    def read_exact(n):
        calls.append(n)
        return next(reads)

    monkeypatch.setattr(backend, "_read_exact", read_exact)
    monkeypatch.setattr(
        backend, "_to_bgr",
        lambda _payload, w, h, _fmt: np.zeros((h, w, 3), dtype=np.uint8),
    )
    backend._reader_loop()

    # The first valid frame is delivered (no warm-up withholding); the REPEATED seq
    # is then rejected on its header, so the second payload is never read.
    assert backend._ring.total_frames == 1
    assert calls == [backend._HEADER.size, len(payload), backend._HEADER.size]


def test_pipe_callback_gaps_survive_latest_wins_and_reach_frame_contract(monkeypatch):
    backend = OrionFramePipeBackend()
    width, height = 64, 32
    payload = bytes(width * height * 3 // 2)
    now_ns = time.perf_counter_ns()
    header1 = _v2_header(
        backend, seq=10, generation=7, producer_ns=now_ns,
        width=width, height=height, payload_len=len(payload), pts=1_000_000)
    header2 = _v2_header(
        backend, seq=14, generation=7, producer_ns=now_ns + 1_000_000,
        width=width, height=height, payload_len=len(payload), pts=1_016_667)
    reads = iter((header1, payload, header2, payload, None))
    monkeypatch.setattr(backend, "_read_exact", lambda _n: next(reads))
    monkeypatch.setattr(
        backend, "_to_bgr",
        lambda _payload, w, h, _fmt: np.zeros((h, w, 3), dtype=np.uint8),
    )

    backend._reader_loop()
    fd = backend.get_frame_nonblocking()

    assert fd is not None and fd.frame_number == 14
    assert fd.producer_sequence == 14 and fd.producer_generation == 7
    assert backend._wire_seq_gaps == 3


def test_pipe_rejects_old_producer_frame_without_laundering_arrival_time(monkeypatch):
    backend = OrionFramePipeBackend()
    width, height = 64, 32
    payload = bytes(width * height * 3 // 2)
    stale_ns = time.perf_counter_ns() - backend._MAX_PRODUCER_AGE_NS - 1_000_000
    header = _v2_header(
        backend, seq=1, producer_ns=stale_ns, width=width, height=height,
        payload_len=len(payload), pts=1_000_000)
    reads = iter((header, payload, None))
    monkeypatch.setattr(backend, "_read_exact", lambda _n: next(reads))
    monkeypatch.setattr(
        backend, "_to_bgr",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("stale source reached conversion")),
    )

    backend._reader_loop()

    assert backend.get_frame_nonblocking() is None
    assert backend._wire_seq_last == 1


def test_pipe_reject_logging_is_counted_but_throttled(monkeypatch):
    backend = OrionFramePipeBackend()
    warnings = []
    monkeypatch.setattr(
        "chiaki_backend.logger.warning", lambda *args: warnings.append(args))
    monkeypatch.setattr("chiaki_backend.time.perf_counter", lambda: 10.0)

    for seq in range(1, 122):
        backend._note_wire_reject("producer_timestamp_stale", seq, 7)

    assert backend._wire_reject_counts == {"producer_timestamp_stale": 121}
    assert len(warnings) == 2  # first occurrence plus bounded 120-frame reminder
    assert warnings[0][-1] == 1 and warnings[1][-1] == 120


def test_pipe_generation_change_mid_connection_fails_before_payload(monkeypatch):
    backend = OrionFramePipeBackend()
    width, height = 64, 32
    payload = bytes(width * height * 3 // 2)
    now_ns = time.perf_counter_ns()
    header1 = _v2_header(
        backend, seq=1, generation=7, producer_ns=now_ns,
        width=width, height=height, payload_len=len(payload), pts=1_000_000)
    header2 = _v2_header(
        backend, seq=1, generation=8, producer_ns=now_ns + 1_000_000,
        width=width, height=height, payload_len=len(payload), pts=1_016_667)
    reads = iter((header1, payload, header2))
    calls = []

    def read_exact(n):
        calls.append(n)
        return next(reads)

    monkeypatch.setattr(backend, "_read_exact", read_exact)
    backend._reader_loop()

    # The first valid sample is delivered immediately (no PTS warm-up withholding).
    assert backend.get_frame_nonblocking() is not None
    # The generation change is rejected on its HEADER, before its payload is read.
    assert calls == [backend._HEADER.size, len(payload), backend._HEADER.size]


def test_pipe_pts_checks_ordering_only_and_never_gates_on_clock_drift():
    """PTS is a nominal-rate counter, not a clock: it must not reject fresh frames.

    Regression guard for the live 59.94-vs-60.000 drift latch. chiaki advances the
    exported PTS by a fixed 1e6/max_fps step per received frame, so on a real PS5
    ``arrival - pts`` grows ~0.91 ms/s forever. The old floor-based backlog gate
    ratcheted its floor DOWN only, so ~40-55 s in it rejected 100% of frames until
    the pipe reconnected (live: 483 frames, 480 rejects, uniqfps 60 -> 0).
    """
    backend = OrionFramePipeBackend()
    # No warm-up withholding: the first progressive frame is usable immediately.
    assert backend._wire_pts_reason(1_000_000) == ""
    assert backend._wire_pts_reason(1_016_667) == ""

    # Simulate a full session of accumulated drift. Every frame progresses PTS by a
    # nominal 16.667 ms while wall time advances 16.684 ms (59.94 fps). Arrival is
    # irrelevant to this gate now, so none of it may be rejected.
    pts = 1_016_667
    for _ in range(3600):            # ~60 s at 60 fps
        pts += 16_667
        assert backend._wire_pts_reason(pts) == ""

    # Ordering faults are still caught.
    assert backend._wire_pts_reason(pts - 1) == "pts_regression"
    assert backend._wire_pts_reason(0) == "pts_missing"


def test_orchestrator_pts_checks_ordering_only_and_generation_reset_is_fail_closed():
    orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        auto_launch_client=False, virtual_controller=False,
    ))
    epoch0 = 1_700_000_000_000_000_000
    pts0 = 1_000_000
    assert orch._source_pts_reason(pts0, epoch0, 1) == ""
    # Accumulated arrival-vs-PTS drift is NOT a rejection: staleness is enforced by
    # _source_frame_reason against a real clock, before this call.
    assert orch._source_pts_reason(
        pts0 + 16_667, epoch0 + 16_667_000 + 80_000_000, 1
    ) == ""
    # Ordering faults still fail closed.
    assert orch._source_pts_reason(pts0, epoch0 + 200_000_000, 1) == "decoder_pts_regression"
    # Same PTS range is legitimate only after explicit connection generation change.
    assert orch._source_pts_reason(1_000, epoch0 + 100_000_000, 2) == ""
    assert orch._pts_to_epoch_ms == 0.0 and orch._pts_to_wall_offset == 0.0


def test_pipe_consecutive_reject_latch_is_broken_and_logged_once(monkeypatch):
    """No gate may reject 100% of frames forever; the escape must not widen freshness."""
    backend = OrionFramePipeBackend()
    errors = []
    monkeypatch.setattr("chiaki_backend.logger.error", lambda *a: errors.append(a))
    backend._ready_evt.set()

    n = backend._WIRE_REJECT_REBASELINE_FRAMES
    for _ in range(n - 1):
        assert backend._wire_reject_latched("producer_timestamp_stale") is False
    assert backend._ready_evt.is_set()          # not yet latched
    assert backend._wire_reject_latched("producer_timestamp_stale") is True

    # Monotonic baselines re-seeded, readiness dropped so the stall fails closed loudly.
    assert backend._wire_pts_last == 0 and backend._wire_producer_ts_last == 0
    assert not backend._ready_evt.is_set()
    assert len(errors) == 1

    # Sustained rejects keep escaping but must not spam the log.
    for _ in range(n * 3):
        backend._wire_reject_latched("producer_timestamp_stale")
    assert len(errors) == 1

    # The freshness budget itself is untouched: a stale producer is still rejected.
    stale_ns = time.perf_counter_ns() - backend._MAX_PRODUCER_AGE_NS - 1_000_000
    assert backend._producer_timestamp_reason(
        stale_ns, time.perf_counter_ns()) == "producer_timestamp_stale"

    # A single good frame clears the latch state.
    backend._note_wire_accept()
    assert backend._wire_consecutive_rejects == 0
    assert backend._wire_reject_latch_logged is False


def test_detector_env_cannot_weaken_shipping_contract(monkeypatch):
    monkeypatch.setenv("ORION_DETECTOR_MIN_WIDTH", "1")
    monkeypatch.setenv("ORION_DETECTOR_MIN_HEIGHT", "1")
    monkeypatch.setenv("ORION_DETECTOR_ASPECT_TOLERANCE", "0.25")
    monkeypatch.setenv("ORION_MAX_DETECTOR_FRAME_AGE_MS", "999999")
    orch = rpo.RemotePlayOrchestrator(rpo.OrchestratorConfig(
        auto_launch_client=False, virtual_controller=False,
    ))

    assert orch._detector_min_width == 1280
    assert orch._detector_min_height == 720
    assert orch._detector_aspect_tolerance == 0.035
    assert orch._max_detector_frame_age_ms == 125.0
    _, reason = orch._prepare_detector_frame(
        np.zeros((540, 960, 3), dtype=np.uint8))
    assert reason == "undersized"


def test_low_resolution_presets_are_labeled_manual_only():
    qml = (Path(__file__).parents[1] / "native_orion" / "qml" / "components"
           / "StreamSetupForm.qml").read_text(encoding="utf-8")
    assert 'key: "LowBandwidth", label: "Low · 540p60 · Manual preview"' in qml
    assert 'key: "UltraLow",     label: "Ultra-Low · 360p30 · Manual preview"' in qml
    assert "Competitive, Balanced, and Quality keep the 720p minimum required for shot automation." in qml


def test_stream_setup_hides_unsupported_split_hardware_decode_toggle():
    root = Path(__file__).parents[1]
    qml = (root / "native_orion" / "qml" / "components"
           / "StreamSetupForm.qml").read_text(encoding="utf-8")
    session = (root / "native_orion" / "src"
               / "RemotePlaySession.cpp").read_text(encoding="utf-8")

    assert "Hardware decode" not in qml
    assert "orion.hardwareDecode" not in qml
    assert "remote_play_decode::resolve(" in session
    assert 'cs.setValue(QStringLiteral("use_zero_copy"),\n                decodePolicy.zeroCopy);' in session
    assert "config_.hardwareDecode" not in session


def test_launcher_copy_matches_the_enable_bot_controller_cta():
    root = Path(__file__).parents[1] / "native_orion" / "qml"
    tour = (root / "components" / "FirstRunTour.qml").read_text(encoding="utf-8")
    setup = (root / "components" / "StreamSetupForm.qml").read_text(encoding="utf-8")
    notes = (root / "pages" / "PatchNotesPage.qml").read_text(encoding="utf-8")
    session = (root.parent / "src" / "RemotePlaySession.cpp").read_text(encoding="utf-8")

    assert "Start Stream" not in tour
    assert "Start Stream" not in setup
    assert "Start Stream" not in session
    assert "Enable Bot + Controller" in tour
    assert "Enable Bot + Controller" in setup
    assert "press Enable Bot + Controller" in session
    assert "Meter tab" not in notes
    assert "beside Live Capture" in notes


def test_latency_cache_scope_never_crosses_capture_routes(monkeypatch, tmp_path):
    decoder_binary = tmp_path / "chiaki-orion.exe"
    decoder_binary.write_bytes(b"decoder-build")
    monkeypatch.setenv("ORION_CAPTURE_CARD_INDEX", "1")
    monkeypatch.setenv("ORION_CAPTURE_MJPG", "0")
    monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", "Integrated Camera|Elgato HD60 X")
    webcam_id = "dshow-moniker-sha256-v1:" + "1" * 64
    card_id = "dshow-moniker-sha256-v1:" + "2" * 64
    monkeypatch.setenv("ORION_VIDEO_DEVICE_IDS", f"{webcam_id}|{card_id}")
    console = "registered-host-sha256-v1:" + "a" * 64
    common = dict(console_identity=console, controller_route="pipe")
    capture = rpo._latency_route_scope(rpo.OrchestratorConfig(
        frame_source="capture_card", resolution="1920x1080", target_fps=60,
        capture_mode="1920x1080@60.000|fourcc=yuy2|buffer=1.000", **common))
    decoder_identity = stable_executable_snapshot(str(decoder_binary))
    decoder = rpo._latency_route_scope(rpo.OrchestratorConfig(
        frame_source="decoder", resolution="1920x1080", target_fps=60,
        chiaki_path=str(decoder_binary),
        chiaki_identity_size=decoder_identity.size,
        chiaki_identity_sha256=decoder_identity.sha256,
        decoder_mode="orf2:1920x1080:nv12", **common))

    assert capture
    assert decoder
    assert capture != decoder
    assert "capture_card" in capture
    assert "device=elgato hd60 x" in capture
    assert f"device-id={card_id}" in capture
    assert "decoder" in decoder
    assert "controller=pipe" in decoder
    assert f"console={console}" in decoder
    assert rpo._latency_route_scope(rpo.OrchestratorConfig(frame_source="auto")) == ""

    monkeypatch.delenv("ORION_VIDEO_DEVICE_NAMES")
    assert rpo._latency_route_scope(rpo.OrchestratorConfig(
        frame_source="capture_card", **common)) == ""

    monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", "Integrated Camera|Elgato HD60 X")
    monkeypatch.delenv("ORION_VIDEO_DEVICE_IDS")
    assert rpo._latency_route_scope(rpo.OrchestratorConfig(
        frame_source="capture_card", **common)) == ""

    monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", "Elgato HD60 X|Elgato 4K60 Pro")
    monkeypatch.setenv("ORION_VIDEO_DEVICE_IDS", f"{webcam_id}|{card_id}")
    assert rpo._latency_route_scope(rpo.OrchestratorConfig(
        frame_source="capture_card", **common)) == ""


def test_latency_cache_capture_scope_rejects_identically_named_card_swap(monkeypatch):
    monkeypatch.setenv("ORION_CAPTURE_CARD_INDEX", "1")
    monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", "Integrated Camera|Elgato HD60 X")
    webcam_id = "dshow-moniker-sha256-v1:" + "a" * 64
    first_card_id = "dshow-moniker-sha256-v1:" + "b" * 64
    swapped_card_id = "dshow-moniker-sha256-v1:" + "c" * 64
    config = rpo.OrchestratorConfig(
        frame_source="capture_card", resolution="1920x1080", target_fps=60,
        console_identity="registered-host-sha256-v1:" + "d" * 64,
        controller_route="pipe",
        capture_mode="1920x1080@60.000|fourcc=mjpg|buffer=1.000")

    monkeypatch.setenv("ORION_VIDEO_DEVICE_IDS", f"{webcam_id}|{first_card_id}")
    first = rpo._latency_route_scope(config)
    monkeypatch.setenv("ORION_VIDEO_DEVICE_IDS", f"{webcam_id}|{swapped_card_id}")
    swapped = rpo._latency_route_scope(config)

    assert first and swapped and first != swapped
    assert "device=elgato hd60 x" in first and "device=elgato hd60 x" in swapped


def test_latency_cache_decoder_scope_hashes_binary_not_mutable_metadata(tmp_path):
    decoder_binary = tmp_path / "chiaki-orion.exe"
    decoder_binary.write_bytes(b"A" * 4096)
    original = decoder_binary.stat()
    config = rpo.OrchestratorConfig(
        frame_source="decoder", resolution="1920x1080", target_fps=60,
        chiaki_path=str(decoder_binary),
        chiaki_identity_size=4096,
        chiaki_identity_sha256=stable_executable_snapshot(str(decoder_binary)).sha256,
        decoder_mode="orf2:1920x1080:nv12",
        console_identity="registered-host-sha256-v1:" + "d" * 64,
        controller_route="pipe")
    first = rpo._latency_route_scope(config)

    # Preserve the exact size and timestamps that the old scope trusted while replacing every
    # byte. Streaming SHA-256 must produce a different route and reject the old posterior.
    decoder_binary.write_bytes(b"B" * 4096)
    os.utime(decoder_binary, ns=(original.st_atime_ns, original.st_mtime_ns))
    second = rpo._latency_route_scope(config)

    assert first and "sha256=" in first
    assert second == ""  # native-issued hash no longer matches the configured image


def test_latency_scope_separates_console_controller_and_negotiated_capture_mode(monkeypatch):
    monkeypatch.setenv("ORION_CAPTURE_CARD_INDEX", "0")
    monkeypatch.setenv("ORION_VIDEO_DEVICE_NAMES", "Elgato HD60 X")
    monkeypatch.setenv(
        "ORION_VIDEO_DEVICE_IDS", "dshow-moniker-sha256-v1:" + "e" * 64)
    base = dict(
        frame_source="capture_card", resolution="1920x1080", target_fps=60,
        console_identity="registered-host-sha256-v1:" + "1" * 64,
        controller_route="pipe",
        capture_mode="1920x1080@60.000|fourcc=mjpg|buffer=1.000")
    first = rpo._latency_route_scope(rpo.OrchestratorConfig(**base))
    assert first

    changed_console = dict(base)
    changed_console["console_identity"] = "registered-host-sha256-v1:" + "2" * 64
    changed_controller = dict(base)
    changed_controller["controller_route"] = "vigem_ds4"
    changed_mode = dict(base)
    changed_mode["capture_mode"] = "1280x720@60.000|fourcc=mjpg|buffer=1.000"
    assert rpo._latency_route_scope(rpo.OrchestratorConfig(**changed_console)) != first
    assert rpo._latency_route_scope(rpo.OrchestratorConfig(**changed_controller)) != first
    assert rpo._latency_route_scope(rpo.OrchestratorConfig(**changed_mode)) != first

    unknown_controller = dict(base)
    unknown_controller["controller_route"] = ""
    assert rpo._latency_route_scope(rpo.OrchestratorConfig(**unknown_controller)) == ""


def test_latency_attestation_generation_is_bound_to_exact_rekeyed_scope(
        monkeypatch, tmp_path):
    import latency_estimator as latency_module

    decoder_binary = tmp_path / "orion-stream.exe"
    decoder_binary.write_bytes(b"decoder-build")
    config = rpo.OrchestratorConfig(
        frame_source="decoder", resolution="1920x1080", target_fps=60,
        chiaki_path=str(decoder_binary),
        chiaki_identity_size=stable_executable_snapshot(str(decoder_binary)).size,
        chiaki_identity_sha256=stable_executable_snapshot(str(decoder_binary)).sha256,
        decoder_mode="orf2:1920x1080:nv12",
        console_identity="registered-host-sha256-v1:" + "9" * 64,
        controller_route="pipe")
    first_scope = rpo._latency_route_scope(config)
    assert first_scope

    orch = object.__new__(rpo.RemotePlayOrchestrator)
    orch.config = config
    orch._cc_mode = False
    orch._decoder_warm_cache_revoked = False
    orch._frame_backend_mode = "decoder"
    orch._frame_backend = SimpleNamespace(producer_identity_status=lambda: {
        "state": "verified", "verified": True, "pid": 1,
        "launch_generation": 1, "reason": ""})
    orch._latency_route_lock = rpo.threading.Lock()
    orch._latency_route_scope_value = first_scope
    first_estimator = object()
    orch._latency_estimator = first_estimator
    orch._latency_controller_attestation_generation = 0
    orch._latency_controller_attestation_scope = ""
    orch._latency_controller_attestation_route = ""

    startup_epoch = orch.latency_estimator_scope_epoch()
    assert startup_epoch == 1
    assert orch.attest_controller_latency_route("pipe", 41)
    assert orch.attest_controller_latency_route("pipe", 41)  # exact idempotent retry
    assert not orch.attest_controller_latency_route("pipe", 40)
    assert not orch.attest_controller_latency_route("vigem_ds4", 41)
    assert config.controller_route == "pipe"
    assert orch.controller_latency_route_attestation_receipt_snapshot(
        "pipe", 41) == (first_scope, startup_epoch)
    assert orch.latency_estimator_attestation_snapshot() == (
        first_estimator, 41, "pipe")

    # A no-op re-key does not create a fresh estimator epoch or invalidate the
    # exact retry binding.
    orch._rekey_latency_route("same_scope_noop")
    assert orch.latency_estimator_scope_epoch() == startup_epoch
    assert orch.attest_controller_latency_route("pipe", 41)

    replacement = object()
    monkeypatch.setattr(
        latency_module, "try_load", lambda route_scope="", **_kwargs: replacement)
    config.controller_route = "vigem_ds4"
    orch._rekey_latency_route("test_route_transition")
    second_scope = rpo._latency_route_scope(config)
    assert second_scope and second_scope != first_scope
    second_epoch = orch.latency_estimator_scope_epoch()
    assert second_epoch == startup_epoch + 1
    assert orch.latency_estimator_attestation_snapshot() == (replacement, 0, "")

    # Generation 41 is immutable: it cannot be rebound to the new route/scope.
    assert not orch.attest_controller_latency_route("vigem_ds4", 41)
    assert orch.attest_controller_latency_route("vigem_ds4", 42)
    assert orch.controller_latency_route_attestation_receipt_snapshot(
        "vigem_ds4", 42) == (second_scope, second_epoch)
    assert orch.latency_estimator_attestation_snapshot() == (
        replacement, 42, "vigem_ds4")
    assert not orch.attest_controller_latency_route("pipe", 42)
    assert not orch.attest_controller_latency_route("pipe", 41)
    assert config.controller_route == "vigem_ds4"

    # Even a same-scope authority replacement is a fresh estimator epoch. The
    # old generation cannot reacquire it; a strictly newer proof recovers.
    orch._replace_latency_authority(
        second_scope, "same_scope_source_reset", fence_frames=False)
    third_epoch = orch.latency_estimator_scope_epoch()
    assert third_epoch == second_epoch + 1
    assert not orch.attest_controller_latency_route("vigem_ds4", 42)
    assert orch.attest_controller_latency_route("vigem_ds4", 43)
    assert orch.controller_latency_route_attestation_receipt_snapshot(
        "vigem_ds4", 43) == (second_scope, third_epoch)


def test_native_sidecar_config_carries_selected_latency_route():
    """Native launches must not leave the estimator on its ambiguous auto scope."""
    root = Path(__file__).parents[1]
    source = (root / "native_orion" / "src" / "RemotePlaySession.cpp").read_text(
        encoding="utf-8"
    )
    config_builder = source[
        source.index("QByteArray RemotePlaySession::buildSidecarConfig() const") :
        source.index("void RemotePlaySession::promoteWarmPreviewToStream()")
    ]
    assert 'obj.insert(QStringLiteral("frame_source"), config_.videoSource);' in config_builder
    assert "insertDecoderPipeProducerExpectation(obj, chiakiPath)" in config_builder
    policy = (root / "native_orion" / "src" / "RemotePlayExecutablePolicy.h").read_text(
        encoding="utf-8"
    )
    assert 'QStringLiteral("chiaki_identity_size")' in policy
    assert 'QStringLiteral("chiaki_identity_sha256")' in policy
    sidecar = (root / "native_orion" / "backend" / "autogreen_sidecar.py").read_text(
        encoding="utf-8"
    )
    assert 'console_identity=str(cfg.get("console_identity", ""))' in sidecar
    assert 'chiaki_identity_size=(_safe_uint64(cfg.get("chiaki_identity_size")) or -1)' in sidecar
    assert 'chiaki_identity_sha256=str(cfg.get("chiaki_identity_sha256", ""))' in sidecar
