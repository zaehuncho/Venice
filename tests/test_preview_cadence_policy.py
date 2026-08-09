"""Regression coverage for source-paced SHM preview selection."""

from pathlib import Path

import numpy as np

from native_orion.backend.autogreen_sidecar import (
    _PreviewShmFailover,
    _preview_frame_ownership_reason,
    _preview_needs_detector_barrier,
    _preview_uses_source_cadence,
    _retire_preview_shm_after_write_failure,
)


ROOT = Path(__file__).resolve().parents[1]


def test_capture_card_shm_follows_source_events():
    assert _preview_uses_source_cadence("capture_card", {}, shm_active=True)
    assert _preview_uses_source_cadence("auto", {"ORION_CAPTURE_CARD": "1"}, True)


def test_required_remote_play_decoder_pipe_follows_source_events():
    assert _preview_uses_source_cadence("auto", {"ORION_REQUIRE_FRAME_PIPE": "1"}, True)
    assert _preview_uses_source_cadence(
        "auto", {"CHIAKI_ORION_FRAME_PIPE": r"\\.\pipe\orion_frames"}, True
    )


def test_window_or_jpeg_fallback_keeps_explicit_pacing_grid():
    assert not _preview_uses_source_cadence("window", {}, shm_active=True)
    assert not _preview_uses_source_cadence(
        "capture_card", {"ORION_CAPTURE_CARD": "1"}, shm_active=False
    )


def test_false_environment_values_do_not_enable_source_pacing():
    env = {
        "ORION_CAPTURE_CARD": "0",
        "ORION_REQUIRE_FRAME_PIPE": "false",
        "ORION_FRAME_PIPE": "off",
    }
    assert not _preview_uses_source_cadence("auto", env, shm_active=True)


def test_source_paced_shm_does_not_drop_display_behind_detector_work():
    assert not _preview_needs_detector_barrier(source_cadence=True, shm_active=True)


def test_preview_callback_cadence_uses_high_resolution_qpc_clock():
    source = (ROOT / "native_orion" / "backend" / "autogreen_sidecar.py").read_text(
        encoding="utf-8"
    )
    callback = source[
        source.index("    def _video_cb(frame_data):") :
        source.index("    def _preview_stats_emit_loop():")
    ]
    assert "callback_at_ns = time.perf_counter_ns()" in callback
    assert '"callback_last_ns"' in callback
    assert "time.monotonic()" not in callback


def test_shm_preview_preserves_capture_timestamp_through_writer():
    source = (ROOT / "native_orion" / "backend" / "autogreen_sidecar.py").read_text(
        encoding="utf-8"
    )
    callback = source[
        source.index("    def _video_cb(frame_data):") :
        source.index("    def _preview_stats_emit_loop():")
    ]
    preview_loop = source[
        source.index("    def _preview_loop():") :
        source.index("    def _stdin_loop():")
    ]
    assert "frame, frame_number, capture_seq, capture_timestamp_ns = frame_data[:4]" \
        in callback
    assert "timestamp_ns=capture_timestamp_ns" in preview_loop


def test_jpeg_or_non_source_fallback_keeps_detector_first_barrier():
    assert _preview_needs_detector_barrier(source_cadence=True, shm_active=False)
    assert _preview_needs_detector_barrier(source_cadence=False, shm_active=True)


def test_repeated_shm_write_failure_handoffs_once_before_first_jpeg():
    """Failures 1-9 shed display only; failure 10 fences SHM before JPEG."""
    wire = []
    gate = _PreviewShmFailover("OrionPreviewReady_testepoch")

    class Writer:
        stop_calls = 0

        def stop(self):
            self.stop_calls += 1

    writer = Writer()

    def emit_control(payload):
        wire.append(("control", payload.copy()))
        return True

    for frame_number in range(1, 10):
        writer, allow_jpeg, retired = _retire_preview_shm_after_write_failure(
            writer, gate, frame_number, emit_control
        )
        assert writer is not None
        assert allow_jpeg is False and retired is False
        assert writer.stop_calls == 0
        assert wire == []

    writer, allow_jpeg, retired = _retire_preview_shm_after_write_failure(
        writer, gate, 10, emit_control
    )
    assert writer is None
    assert allow_jpeg is True and retired is True
    wire.append(("jpeg", {"frame_number": 10}))

    assert [kind for kind, _ in wire] == ["control", "jpeg"]
    control = wire[0][1]
    assert control == {
        "event": "preview_transport",
        "protocol": 1,
        "mode": "jpeg",
        "reason": "shm_write_failures",
        "shm_ready_event": "OrionPreviewReady_testepoch",
        "first_jpeg_frame_number": 10,
    }
    # A duplicate call cannot write a second transition into the same session.
    assert gate.announce(emit_control) is True
    assert [kind for kind, _ in wire] == ["control", "jpeg"]


def test_failed_handoff_emit_withholds_jpeg_until_control_can_be_written():
    gate = _PreviewShmFailover("OrionPreviewReady_testepoch", failure_limit=1)

    class Writer:
        stopped = False

        def stop(self):
            self.stopped = True

    writer, allow_jpeg, retired = _retire_preview_shm_after_write_failure(
        Writer(), gate, 77, lambda _payload: False
    )
    assert writer is None and allow_jpeg is False and retired is True
    assert gate.pending is True and gate.announced is False

    emitted = []

    def recover(payload):
        emitted.append(payload.copy())
        return True

    assert gate.announce(recover) is True
    assert len(emitted) == 1
    assert emitted[0]["first_jpeg_frame_number"] == 77


def test_source_paced_shm_requires_owned_immutable_frame_or_fails_closed():
    owned = np.zeros((720, 1280, 3), dtype=np.uint8)
    owned.setflags(write=False)
    assert _preview_frame_ownership_reason(owned) == ""
    assert not _preview_needs_detector_barrier(source_cadence=True, shm_active=True)

    mutable = np.zeros((720, 1280, 3), dtype=np.uint8)
    assert _preview_frame_ownership_reason(mutable) == "writeable"

    aliased = owned.view()
    assert not aliased.flags["OWNDATA"]
    assert _preview_frame_ownership_reason(aliased) == "not_owned"

    non_contiguous = owned[:, ::2, :]
    assert not non_contiguous.flags["C_CONTIGUOUS"]
    assert _preview_frame_ownership_reason(non_contiguous) == "not_contiguous"


def test_native_shm_and_jpeg_converge_on_one_bounded_presenter():
    source = (ROOT / "native_orion" / "src" / "RemotePlaySession.cpp").read_text(
        encoding="utf-8"
    )
    # The FrameDecoder callback and the off-GUI SHM pump batch handler must both
    # enter the same presenter. Neither transport may bypass pacing and
    # reintroduce bursty direct QML delivery.
    assert "queuePreviewFrame(std::move(image), frameNumber);" in source
    assert "queuePreviewFrame(batch.image, frameNumber);" in source
    assert "this, &RemotePlaySession::handleShmPumpBatch" in source
    assert "emit frameReady(std::move(next->image), next->frameNumber);" in source
    assert "present_gap_max_ms=" in source
    assert "render_clock=%18 render_tick_gap_max_ms=%19" in source
    assert "newestShmAt" in source
    assert "isPreviewShmLine" in source


def test_native_accepts_only_current_epoch_handoff_and_rejects_late_shm():
    source = (ROOT / "native_orion" / "src" / "RemotePlaySession.cpp").read_text(
        encoding="utf-8"
    )
    handler = source[
        source.index("void RemotePlaySession::handlePreviewTransportHandoff") :
        source.index("void RemotePlaySession::handleShmPumpBatch")
    ]
    assert 'protocol != 1 || mode != QLatin1String("jpeg")' in handler
    assert "readyEvent != shmTransportNames_.readyEventName" in handler
    assert "switchPreviewToJpegFallback(" in handler

    transition = source[
        source.index("bool RemotePlaySession::switchPreviewToJpegFallback") :
        source.index("void RemotePlaySession::handlePreviewTransportHandoff")
    ]
    assert transition.index("shmFallbackRequested_ = true;") < transition.index(
        "retireShmSourceEpoch();"
    )
    assert "previewChunks_.reset();" in transition
    assert "lastPreviewPipelineStatsMs_ = 0;" in transition

    pump = source[
        source.index("void RemotePlaySession::handleShmPumpBatch") :
        source.index("void RemotePlaySession::armMeterGate")
    ]
    assert "|| shmFallbackRequested_)" in pump
    assert "if (batch.notificationFailureRun > 0)" in pump
    assert "switchPreviewToJpegFallback(detail, 0, true);" in pump


def test_presenter_is_bounded_and_cleared_at_sidecar_boundaries():
    header = (ROOT / "native_orion" / "src" / "PreviewPresentationBuffer.h").read_text(
        encoding="utf-8"
    )
    session = (ROOT / "native_orion" / "src" / "RemotePlaySession.cpp").read_text(
        encoding="utf-8"
    )
    assert "kPrimeDepth = 3" in header
    assert "kRecoveryDepth = 2" in header
    assert "kCapacity = 4" in header
    assert "frames_.pop_front();" in header
    assert "scheduleNextPreviewPresentation(nowNs);" in session
    assert "resetPreviewPresentation(true);" in session
    assert session.count("resetPreviewPresentation(false);") >= 3


def test_live_underflow_preserves_absolute_presentation_phase():
    session = (ROOT / "native_orion" / "src" / "RemotePlaySession.cpp").read_text(
        encoding="utf-8"
    )
    empty_at = session.index("if (previewPresentationBuffer_.empty())")
    prime_at = session.index("if (!previewPresentationPrimed_)", empty_at)
    empty_path = session[empty_at:prime_at]
    assert "scheduleNextPreviewPresentation(nowNs);" in empty_path
    assert "previewPresentationTimer_.stop();" not in empty_path
    assert "previewPresentationNextDeadlineNs_ = 0;" not in empty_path
    assert "previewPresentationRecovering_ = true;" in empty_path
    assert "PreviewPresentationBuffer::kRecoveryDepth" in session[prime_at : prime_at + 1600]
