"""Fail-closed detector-frame ownership, geometry, freshness, and preview-priority tests."""

import importlib.util
import json
import os
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np

import capture_card_backend as ccb
from chiaki_backend import FrameData
from remote_play_orchestrator import (
    OrchestratorConfig,
    RemotePlayOrchestrator,
    _detector_frame_contract_reason,
    _frames_identical,
    _isolate_detector_frame,
    _normalize_detector_frame,
    _normalize_detector_y_plane,
)


def _sidecar_module():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "native_orion", "backend", "autogreen_sidecar.py")
    name = "_autogreen_frame_integrity_test"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.modules.pop(name, None)


def _valid_frame(seed=1):
    # Non-dark, 16:9, detector-minimum frame.  A mostly constant image makes exact
    # duplicate behavior deterministic while remaining cheap to construct.
    frame = np.full((720, 1280, 3), 70 + seed, dtype=np.uint8)
    frame[100:110, 100:110] = (10, 180, 240)
    return frame


def test_capture_contract_rejects_undersized_stretched_and_wrong_format():
    assert ccb._frame_contract_reason(_valid_frame()) == ""
    assert ccb._frame_contract_reason(np.zeros((480, 640, 3), np.uint8)) == "undersized"
    assert ccb._frame_contract_reason(np.zeros((720, 1280, 4), np.uint8)) == "channels"
    assert ccb._frame_contract_reason(np.zeros((720, 1400, 3), np.uint8)) == "aspect"
    assert ccb._frame_contract_reason(np.zeros((720, 1280, 3), np.float32)) == "dtype"


def test_capture_isolation_owns_and_freezes_pixels():
    source = _valid_frame()
    owned, reason = ccb._isolate_immutable_frame(source, verify_copy=True)
    assert reason == ""
    assert owned.flags["OWNDATA"] and owned.flags["C_CONTIGUOUS"]
    assert not owned.flags["WRITEABLE"]
    assert not np.shares_memory(source, owned)
    before = int(owned[0, 0, 0])
    source[0, 0, 0] = 255
    assert int(owned[0, 0, 0]) == before


def test_capture_isolation_rejects_source_that_changes_during_copy(monkeypatch):
    source = _valid_frame()
    real_array = ccb.np.array

    def _copy_then_mutate(value, *args, **kwargs):
        out = real_array(value, *args, **kwargs)
        value[0, 0, 0] ^= np.uint8(1)
        return out

    monkeypatch.setattr(ccb.np, "array", _copy_then_mutate)
    owned, reason = ccb._isolate_immutable_frame(source, verify_copy=True)
    assert owned is None and reason == "torn_copy"


def test_orchestrator_boundary_copies_untrusted_frame_and_never_stretches():
    source = _valid_frame()
    assert _detector_frame_contract_reason(source) == ""
    owned, reason = _isolate_detector_frame(source, trusted_isolated=False)
    assert reason == ""
    assert not np.shares_memory(source, owned)
    assert not owned.flags["WRITEABLE"]
    assert _detector_frame_contract_reason(np.zeros((553, 706, 3), np.uint8)) == "undersized"


def test_healthy_1080p_is_uniformly_normalized_to_exact_detector_geometry():
    source = np.full((1080, 1920, 3), 83, dtype=np.uint8)
    source[400:500, 1400:1500] = (12, 210, 245)
    assert _detector_frame_contract_reason(source) == ""
    isolated, reason = _isolate_detector_frame(source, trusted_isolated=False)
    assert reason == ""
    normalized, reason = _normalize_detector_frame(isolated)
    assert reason == ""
    assert normalized.shape == (720, 1280, 3)
    assert normalized.flags["OWNDATA"] and normalized.flags["C_CONTIGUOUS"]
    assert not normalized.flags["WRITEABLE"]
    # A 100x100 source square under the uniform 2/3 scale remains square.  This
    # catches independent x/y stretching at the detector boundary.
    mask = normalized[:, :, 1] > 150
    ys, xs = np.where(mask)
    assert abs((xs.max() - xs.min()) - (ys.max() - ys.min())) <= 1


def test_prepare_detector_frame_accepts_1080p_and_publishes_exact_720p():
    orch = _bare_integrity_orchestrator()
    source = np.full((1080, 1920, 3), 90, dtype=np.uint8)
    normalized, reason = orch._prepare_detector_frame(source, trusted_isolated=False)
    assert reason == ""
    assert normalized.shape == (720, 1280, 3)
    assert not normalized.flags["WRITEABLE"]
    assert not np.shares_memory(normalized, source)


def test_native_y_plane_uses_same_normalization_or_falls_back_safely():
    y_plane = np.arange(1080 * 1920, dtype=np.uint8).reshape(1080, 1920)
    normalized = _normalize_detector_y_plane(y_plane, 1920, 1080)
    assert normalized.shape == (720, 1280)
    assert normalized.flags["OWNDATA"] and not normalized.flags["WRITEABLE"]
    assert _normalize_detector_y_plane(y_plane[:, :-1], 1920, 1080) is None


def test_exact_duplicate_check_cannot_step_over_thin_meter_change():
    a = _valid_frame()
    b = a.copy()
    assert _frames_identical(a, b)
    # One pixel is enough; the retired coarse grid hash could miss the whole thin meter.
    b[357, 1003, 1] ^= np.uint8(1)
    assert not _frames_identical(a, b)


def _bare_integrity_orchestrator():
    orch = RemotePlayOrchestrator.__new__(RemotePlayOrchestrator)
    orch._detector_min_width = 1280
    orch._detector_min_height = 720
    orch._detector_aspect_tolerance = 0.035
    orch._max_detector_frame_age_ms = 125.0
    orch._source_identity = 0
    orch._source_frame_number = 0
    orch._source_timestamp_ns = 0
    orch._source_sequence_skips = 0
    orch._preview_duplicate_refreshes = 0
    orch._backend_identity_ref = None
    orch._backend_identity_generation = 0
    orch._last_frame = None
    orch._last_frame_hash = None
    orch._frame_integrity_counts = {}
    orch._last_frame_reject_reason = ""
    orch._frame_integrity_lock = threading.Lock()
    orch._frame_integrity_generation = 0
    orch._capture_integrity_healthy = True
    orch._last_meter_present = True
    orch._last_raw_fed = True
    orch._last_gameplay_structure_verified = True
    orch._last_gameplay_structure_epoch = 77
    orch._last_meter_track = SimpleNamespace(fill_pct=80.0)
    orch._last_meter_stage = "track"
    orch._last_meter_bbox = (100, 200, 30, 140)
    orch._last_meter_bbox_wh = (1280, 720)
    orch._last_green_window = {
        "start": 92.0, "end": 98.0, "center": 95.0, "width": 6.0,
    }
    orch._last_release_fusion = SimpleNamespace(fill_pct=80.0, confidence=0.8)
    orch._last_tip_reg = None
    orch._green_show_streak = 0
    orch._green_hide_streak = 0
    orch._remap_engine = SimpleNamespace(
        _lock=threading.RLock(),
        _shot=SimpleNamespace(fill_pct=80.0, confidence=0.8, rtt_offset_ms=12.5),
    )
    orch._frame_ready_evt = threading.Event()
    orch._telemetry_priority_evt = threading.Event()
    orch._telemetry_revision = 0
    orch._last_processed_seq = 0
    orch._last_processed_frame_number = 0
    orch._last_processed_frame_ts = 0.0
    orch._last_processed_epoch_ms = 0.0
    orch._last_processed_measurement_epoch_ms = 0.0
    orch._last_processed_pts = 0
    orch._last_processed_frame_wh = (0, 0)
    orch._processed_frame_snapshot = None
    return orch


def test_backend_reopen_uses_monotonic_generation_not_reusable_object_address():
    orch = _bare_integrity_orchestrator()
    first = SimpleNamespace(name="first")
    replacement = SimpleNamespace(name="replacement")
    first_id = orch._backend_source_identity(first)
    assert first_id == 1
    assert orch._backend_source_identity(first) == first_id
    replacement_id = orch._backend_source_identity(replacement)
    assert replacement_id == first_id + 1
    # The old backend remains strongly referenced until replacement was observed,
    # so a reopened object's Python address cannot masquerade as the old source.
    assert orch._backend_identity_ref is replacement


def test_source_sequence_timestamp_and_pixel_duplicate_fail_closed():
    orch = _bare_integrity_orchestrator()
    now = time.perf_counter()
    first = _valid_frame()
    assert orch._source_frame_reason(first, 10, 1, int(now * 1e9), now) == ""
    orch._last_frame = first
    assert orch._source_frame_reason(first, 10, 1, int(now * 1e9), now) == "source_poll_repeat"

    now2 = time.perf_counter()
    assert orch._source_frame_reason(first.copy(), 10, 2, int(now2 * 1e9), now2) == "pixel_duplicate"
    changed = first.copy()
    changed[200, 400, 0] ^= np.uint8(1)
    now3 = time.perf_counter()
    assert orch._source_frame_reason(changed, 10, 3, int(now3 * 1e9), now3) == ""
    changed_again = changed.copy()
    changed_again[201, 401, 0] ^= np.uint8(1)
    now4 = time.perf_counter()
    assert orch._source_frame_reason(
        changed_again, 10, 5, int(now4 * 1e9), now4) == ""
    assert orch._source_sequence_skips == 1

    stale_now = time.perf_counter()
    assert orch._source_frame_reason(
        changed, 11, 1, int((stale_now - 0.5) * 1e9), stale_now
    ) == "source_timestamp_stale"

    orch._note_frame_reject("pixel_duplicate")
    assert not orch._capture_integrity_healthy
    assert not orch._last_meter_present and not orch._last_raw_fed
    assert orch._frame_ready_evt.is_set() and orch._telemetry_priority_evt.is_set()


def test_single_pixel_repeat_skips_without_blink_and_cannot_refresh_lease():
    orch = _bare_integrity_orchestrator()
    prior_ts = time.perf_counter() - 0.060
    orch._last_processed_frame_ts = prior_ts
    orch._last_processed_seq = 9
    orch._note_frame_reject(
        "pixel_duplicate",
        fail_closed=orch._frame_reject_fails_closed("pixel_duplicate"),
    )

    assert orch._capture_integrity_healthy
    assert orch._last_meter_present and orch._last_raw_fed
    assert orch._last_processed_seq == 9
    assert orch._last_processed_frame_ts == prior_ts
    # Repeats do not renew authority: a sustained run naturally exceeds the
    # strict 50 ms lease even though one render hold caused no instant blink.
    assert (time.perf_counter() - orch._last_processed_frame_ts) * 1000.0 > 50.0
    assert not orch._frame_reject_fails_closed("source_poll_repeat")
    assert orch._frame_reject_fails_closed("dark_frame")


def test_processed_frame_metadata_is_paired_and_only_then_healthy():
    orch = _bare_integrity_orchestrator()
    orch._capture_integrity_healthy = False
    orch._finish_processed_frame(
        7, 91, False, True,
        frame_ts=12.5, epoch_ms=123456.0,
        measurement_epoch_ms=123436.0, pts=999,
        frame_wh=(1280, 720),
    )
    assert orch._capture_integrity_healthy
    assert orch._last_processed_seq == 7
    assert orch._last_processed_frame_number == 91
    assert orch._last_processed_frame_ts == 12.5
    assert orch._last_processed_epoch_ms == 123456.0
    assert orch._last_processed_measurement_epoch_ms == 123436.0
    assert orch._last_processed_frame_wh == (1280, 720)
    snapshot = orch._processed_frame_snapshot
    assert snapshot.seq == 7 and snapshot.frame_number == 91
    assert snapshot.frame_ts == 12.5 and snapshot.epoch_ms == 123456.0
    assert snapshot.measurement_epoch_ms == 123436.0
    assert snapshot.frame_wh == (1280, 720) and snapshot.integrity_healthy
    assert snapshot.revision == orch._telemetry_revision
    assert snapshot.gameplay_structure_verified is True
    assert snapshot.gameplay_structure_epoch == 77


def test_missing_measurement_epoch_is_not_replaced_by_raw_identity_clock():
    orch = _bare_integrity_orchestrator()
    orch._finish_processed_frame(
        8, 92, False, True,
        frame_ts=12.6, epoch_ms=123472.0, pts=1000,
        frame_wh=(1280, 720),
    )

    assert orch._last_processed_epoch_ms == 123472.0
    assert orch._last_processed_measurement_epoch_ms == 0.0
    sidecar = _sidecar_module()
    snapshot = sidecar._read_processed_frame_snapshot(orch)
    assert snapshot["epoch_ms"] == 123472.0
    assert snapshot["measurement_epoch_ms"] == 0.0


def test_older_detector_completion_cannot_clear_newer_capture_reject():
    orch = _bare_integrity_orchestrator()
    captured_generation = orch._frame_integrity_generation

    # Frame 7 began detection, then capture observed a newer corrupt frame.
    orch._note_frame_reject("dark_frame")
    assert orch._frame_integrity_generation == captured_generation + 1
    # Emulate the older detector continuing after the capture thread rejected a
    # newer frame.  These partial legacy writes must never become authoritative.
    orch._last_meter_present = True
    orch._last_raw_fed = True
    orch._last_gameplay_structure_verified = True
    orch._last_gameplay_structure_epoch = 88
    orch._last_meter_stage = "track_green"
    orch._last_meter_bbox = (500, 300, 32, 150)
    orch._last_meter_bbox_wh = (1280, 720)
    orch._last_green_window = {
        "start": 93.0, "end": 99.0, "center": 96.0, "width": 6.0,
    }
    orch._last_meter_track = SimpleNamespace(fill_pct=96.0, confidence=0.99)
    orch._remap_engine._shot.fill_pct = 96.0
    orch._remap_engine._shot.confidence = 0.99
    orch._finish_processed_frame(
        7, 91, False, True,
        frame_ts=12.5, epoch_ms=123456.0, pts=999,
        frame_wh=(1280, 720), integrity_generation=captured_generation,
    )

    snapshot = orch._processed_frame_snapshot
    assert not orch._capture_integrity_healthy
    assert not snapshot.integrity_healthy
    assert snapshot.reject_reason == "dark_frame"
    assert snapshot.seq == 7 and snapshot.frame_number == 91
    assert not snapshot.meter_present and not snapshot.raw_fed
    assert not snapshot.gameplay_structure_verified
    assert snapshot.gameplay_structure_epoch == 0
    assert snapshot.stage == ""
    assert snapshot.bbox == () and snapshot.bbox_wh == ()
    assert snapshot.green == () and snapshot.tracking == () and snapshot.shot == ()
    assert not orch._last_meter_present and not orch._last_raw_fed
    assert not orch._last_gameplay_structure_verified
    assert orch._last_gameplay_structure_epoch == 0
    assert orch._last_meter_bbox is None and orch._last_green_window is None
    assert orch._remap_engine._shot.fill_pct == 0.0
    assert orch._remap_engine._shot.confidence == 0.0

    # Only a frame published after that reject generation may restore authority.
    orch._finish_processed_frame(
        8, 92, False, True,
        frame_ts=12.6, epoch_ms=123472.0, pts=1000,
        frame_wh=(1280, 720),
        integrity_generation=orch._frame_integrity_generation,
    )
    snapshot = orch._processed_frame_snapshot
    assert orch._capture_integrity_healthy and snapshot.integrity_healthy
    assert snapshot.reject_reason == ""
    assert snapshot.seq == 8 and snapshot.frame_number == 92


def test_sidecar_prefers_one_atomic_processed_snapshot_over_torn_legacy_fields():
    orch = _bare_integrity_orchestrator()
    orch._finish_processed_frame(
        11, 501, False, True,
        frame_ts=42.25, epoch_ms=987654.0,
        measurement_epoch_ms=987674.0, pts=1234,
        frame_wh=(1280, 720), integrity_generation=0,
    )
    # Simulate individually-read legacy fields changing underneath telemetry.
    orch._last_processed_seq = 999
    orch._last_processed_frame_number = 998
    orch._last_processed_frame_ts = 1.0
    orch._last_processed_epoch_ms = 2.0
    orch._last_processed_measurement_epoch_ms = 3.0
    orch._last_processed_frame_wh = (3, 4)
    orch._capture_integrity_healthy = False
    orch._last_frame_reject_reason = "torn_legacy"
    orch._last_gameplay_structure_verified = False
    orch._last_gameplay_structure_epoch = 999

    sidecar = _sidecar_module()
    snapshot = sidecar._read_processed_frame_snapshot(orch)
    assert snapshot["seq"] == 11
    assert snapshot["frame_number"] == 501
    assert snapshot["frame_ts"] == 42.25
    assert snapshot["epoch_ms"] == 987654.0
    assert snapshot["measurement_epoch_ms"] == 987674.0
    assert snapshot["frame_wh"] == (1280, 720)
    assert snapshot["integrity_healthy"] is True
    assert snapshot["reject_reason"] == ""
    assert snapshot["revision"] == orch._processed_frame_snapshot.revision
    assert snapshot["meter_present"] is True and snapshot["raw_fed"] is True
    assert snapshot["gameplay_structure_verified"] is True
    assert snapshot["gameplay_structure_epoch"] == 77
    assert snapshot["stage"] == "track"
    assert snapshot["bbox"] == (100, 200, 30, 140)
    assert snapshot["bbox_wh"] == (1280, 720)
    assert snapshot["green"]["center"] == 95.0
    assert snapshot["tracking"]["fill_pct"] == 80.0
    assert snapshot["fusion"]["confidence"] == 0.8
    assert snapshot["shot"] == {
        "fill_pct": 80.0, "confidence": 0.8, "rtt_offset_ms": 12.5,
    }


def test_tip_registration_cannot_cross_pair_with_next_detector_frame():
    orch = _bare_integrity_orchestrator()
    orch._last_tip_reg = {
        "seq": 31,
        "reg_tip_ms": 187.5,
        "reg_conf": 0.91,
        "reg_sample_capture_ms": 700000.0,
        "reg_tip_capture_ms": 700187.5,
        "reg_sigma_ms": 18.0,
        "reg_model_id": "numpy-robust-v2",
    }
    orch._finish_processed_frame(
        31, 701, False, True,
        frame_ts=70.0, epoch_ms=700000.0,
        measurement_epoch_ms=700000.0, pts=3001,
        frame_wh=(1280, 720), integrity_generation=0,
    )

    # Detection advances its legacy working state to N+1 before telemetry emits N.
    # The immutable publication must continue to carry only frame N's registration.
    orch._last_tip_reg = {
        "seq": 32,
        "reg_tip_ms": 99.0,
        "reg_conf": 0.99,
        "reg_sample_capture_ms": 700016.0,
        "reg_tip_capture_ms": 700115.0,
        "reg_sigma_ms": 9.0,
        "reg_model_id": "wrong-next-frame",
    }

    sidecar = _sidecar_module()
    snapshot = sidecar._read_processed_frame_snapshot(orch)
    assert snapshot["seq"] == 31
    assert snapshot["tip_registration"]["seq"] == 31
    assert snapshot["tip_registration"]["reg_tip_ms"] == 187.5
    assert snapshot["tip_registration"]["reg_model_id"] == "numpy-robust-v2"
    wire = sidecar._tip_registration_wire(snapshot["tip_registration"])
    assert wire["reg_seq"] == 31
    assert wire["reg_tip_ms"] == 187.5
    assert wire["reg_model_id"] == "numpy-robust-v2"


def test_partial_next_detection_cannot_tear_actionable_snapshot():
    orch = _bare_integrity_orchestrator()
    sidecar = _sidecar_module()
    orch._finish_processed_frame(
        21, 601, False, True,
        frame_ts=50.0, epoch_ms=500000.0, pts=2001,
        frame_wh=(1280, 720), integrity_generation=0,
    )

    # Detector frame 22 has started mutating legacy fields, but has not crossed the
    # _finish_processed_frame publication boundary.  Periodic telemetry must still
    # see frame 21's complete state, never frame-21 identity plus frame-22 values.
    orch._last_meter_stage = "acquire"
    orch._last_gameplay_structure_verified = False
    orch._last_gameplay_structure_epoch = 999
    orch._last_meter_bbox = (700, 310, 36, 148)
    orch._last_green_window["center"] = 97.0
    orch._last_meter_track.fill_pct = 42.0
    orch._remap_engine._shot.fill_pct = 42.0
    orch._remap_engine._shot.confidence = 0.55

    pending = sidecar._read_processed_frame_snapshot(orch)
    assert pending["seq"] == 21 and pending["frame_number"] == 601
    assert pending["stage"] == "track"
    assert pending["gameplay_structure_verified"] is True
    assert pending["gameplay_structure_epoch"] == 77
    assert pending["bbox"] == (100, 200, 30, 140)
    assert pending["green"]["center"] == 95.0
    assert pending["tracking"]["fill_pct"] == 80.0
    assert pending["shot"]["fill_pct"] == 80.0

    orch._finish_processed_frame(
        22, 602, False, True,
        frame_ts=50.1, epoch_ms=500016.0, pts=2002,
        frame_wh=(1280, 720), integrity_generation=0,
    )
    complete = sidecar._read_processed_frame_snapshot(orch)
    assert complete["seq"] == 22 and complete["frame_number"] == 602
    assert complete["stage"] == "acquire"
    assert complete["gameplay_structure_verified"] is False
    assert complete["gameplay_structure_epoch"] == 0
    assert complete["bbox"] == (700, 310, 36, 148)
    assert complete["green"]["center"] == 97.0
    assert complete["tracking"]["fill_pct"] == 42.0
    assert complete["shot"]["fill_pct"] == 42.0


def test_unhealthy_feed_override_removes_every_actionable_held_value():
    sidecar = _sidecar_module()
    payload = {
        "feed_healthy": False,
        "meter_present": True,
        "raw_fed": True,
        "gameplay_structure_verified": True,
        "gameplay_structure_epoch": "77",
        "shot": {"fill_pct": 88.0, "confidence": 0.9},
        "green": {"center": 95.0},
        "bbox": [1, 2, 3, 4],
        "bbox_wh": [1280, 720],
        "tracking": {"fill_pct": 88.0},
        "fusion": {"fill_pct": 88.0},
    }
    assert sidecar._apply_feed_health_override(payload)
    assert payload["meter_present"] is False and payload["raw_fed"] is False
    assert payload["gameplay_structure_verified"] is False
    assert payload["gameplay_structure_epoch"] == "0"
    assert payload["shot"]["fill_pct"] == 0.0
    assert payload["shot"]["confidence"] == 0.0
    for key in ("green", "bbox", "bbox_wh", "tracking", "fusion"):
        assert key not in payload


def test_pixel_stall_is_surfaced_as_unhealthy_before_payload_cleanup():
    sidecar = _sidecar_module()
    payload = {
        "feed_healthy": True,
        "meter_present": True,
        "raw_fed": True,
        "gameplay_structure_verified": True,
        "gameplay_structure_epoch": "77",
        "frame_reject_reason": "",
        "shot": {"fill_pct": 72.0, "confidence": 0.8},
        "bbox": [10, 20, 30, 40],
    }
    assert sidecar._apply_stall_override(payload, 251.0, 250.0)
    assert payload["feed_healthy"] is False
    assert payload["gameplay_structure_verified"] is False
    assert payload["gameplay_structure_epoch"] == "0"
    assert payload["frame_reject_reason"] == "pixel_stall"
    assert sidecar._apply_feed_health_override(payload)
    assert payload["frame_integrity_ok"] is False
    assert "bbox" not in payload


def test_preview_barrier_drops_when_detector_backlogged_and_passes_after_completion():
    sidecar = _sidecar_module()
    orch = SimpleNamespace(_meter_detector=object(), _last_processed_seq=4)
    assert not sidecar._preview_wait_for_detector(orch, capture_seq=5, timeout_s=0.002)

    def _complete():
        time.sleep(0.003)
        orch._last_processed_seq = 5

    th = threading.Thread(target=_complete)
    th.start()
    try:
        assert sidecar._preview_wait_for_detector(orch, capture_seq=5, timeout_s=0.05)
    finally:
        th.join(timeout=1.0)


class _ChunkStdout:
    def __init__(self, set_event=None):
        self.buffer = self
        self.data = bytearray()
        self._set_event = set_event
        self._writes = 0

    def write(self, value):
        raw = value.encode("ascii") if isinstance(value, str) else bytes(value)
        self.data.extend(raw)
        self._writes += 1
        if self._set_event is not None and self._writes == 1:
            self._set_event.set()
        return len(raw)

    def flush(self):
        return None


def test_preview_chunks_bound_each_stdout_lock_hold_and_round_trip(monkeypatch):
    sidecar = _sidecar_module()
    sink = _ChunkStdout()
    monkeypatch.setattr(sidecar.sys, "stdout", sink)
    sidecar._emit_lock = threading.Lock()
    raw = b"A" * 20_000

    assert sidecar._emit_frame_chunks(raw, 77)
    lines = bytes(sink.data).splitlines()
    assert len(lines) == 3
    records = [json.loads(line) for line in lines]
    assert all(r["event"] == "frame_chunk" for r in records)
    assert [r["chunk_index"] for r in records] == [0, 1, 2]
    assert all(r["chunk_count"] == 3 and r["frame_number"] == 77 for r in records)
    assert all(r["chunk_frame_id"] == 77 for r in records)
    assert all(len(r["jpeg_b64"]) <= 8 * 1024 for r in records)
    assert "".join(r["jpeg_b64"] for r in records).encode("ascii") == raw


def test_preview_chunk_transport_yields_to_new_telemetry_between_chunks(monkeypatch):
    sidecar = _sidecar_module()
    priority = threading.Event()
    sink = _ChunkStdout(set_event=priority)
    monkeypatch.setattr(sidecar.sys, "stdout", sink)
    sidecar._emit_lock = threading.Lock()

    assert not sidecar._emit_frame_chunks(b"B" * 20_000, 91, priority_event=priority)
    assert len(bytes(sink.data).splitlines()) == 1
    assert sidecar._emit_lock.acquire(blocking=False), "chunk abort must always release stdout lock"
    sidecar._emit_lock.release()


class _FrameSeriesBackend:
    def __init__(self, orch, frames):
        self._orch = orch
        self._frames = list(frames)
        self._index = 0

    def get_frame(self, timeout=0.02):
        if self._index >= len(self._frames):
            self._orch._running = False
            return None
        frame = self._frames[self._index]
        self._index += 1
        return FrameData(
            frame=frame,
            timestamp_ns=time.perf_counter_ns(),
            epoch_ns=time.time_ns(),
            frame_number=self._index,
        )

    def get_frame_nonblocking(self):
        return self.get_frame()

    def is_healthy(self):
        return True


class _MissingEpochBackend(_FrameSeriesBackend):
    def get_frame(self, timeout=0.02):
        if self._index >= len(self._frames):
            self._orch._running = False
            return None
        frame = self._frames[self._index]
        self._index += 1
        return FrameData(
            frame=frame,
            timestamp_ns=time.perf_counter_ns(),
            epoch_ns=0,
            frame_number=self._index,
        )


def test_capture_loop_publishes_only_valid_immutable_unique_frames(monkeypatch):
    monkeypatch.setenv("ORION_SIMPLE_READER", "1")
    orch = RemotePlayOrchestrator(OrchestratorConfig(
        frame_source="capture_card", auto_launch_client=False,
        virtual_controller=False, target_fps=60,
    ))
    first = _valid_frame(1)
    duplicate = first.copy()
    dark = np.zeros_like(first)
    changed = first.copy()
    changed[333, 777, 2] ^= np.uint8(1)
    backend = _FrameSeriesBackend(orch, [first, duplicate, dark, changed])
    orch._frame_backend = backend
    orch._frame_backend_mode = "capture_card"
    orch._running = True
    forwarded = []
    orch._video_callback = forwarded.append

    orch._capture_loop()

    assert orch._frame_seq == 2
    assert orch._frame_integrity_counts["pixel_duplicate"] == 1
    assert orch._frame_integrity_counts["dark_frame"] == 1
    # Dark/loading content is displayable but never detector-authoritative: the launcher receives
    # an immutable black preview instead of freezing on the previous bright frame, while frame_seq
    # still advances only for the two valid detector frames.
    # A fresh source-number/timestamp exact-pixel repeat is displayable at source
    # cadence but never detector-authoritative.  It must not manufacture the
    # 32 ms preview hole that the live card showed when one repeated render was
    # skipped.  Dark/loading content remains display-only for the same reason.
    assert len(forwarded) == 4
    assert np.array_equal(forwarded[1][0], forwarded[0][0])
    assert forwarded[1][1] == 2
    assert forwarded[1][2] == 1
    assert forwarded[1][3] > 0
    assert not np.any(forwarded[2][0])
    assert forwarded[2][2] == 1
    assert forwarded[2][3] > 0
    assert orch._preview_duplicate_refreshes == 1
    published = orch._frame_bundle[0]
    assert published.flags["OWNDATA"] and not published.flags["WRITEABLE"]
    assert not np.shares_memory(published, changed)
    # Capture-card PTS is absent, so its canonical measurement clock is the
    # backend-provided epoch committed beside these exact pixels.
    assert orch._frame_bundle[5] == orch._frame_bundle[4]
    assert orch._frame_bundle[5] > 0.0
    assert forwarded[-1][2] == orch._frame_seq


def test_backend_frame_without_epoch_publishes_zero_timing_authority(monkeypatch):
    monkeypatch.setenv("ORION_SIMPLE_READER", "1")
    orch = RemotePlayOrchestrator(OrchestratorConfig(
        frame_source="capture_card", auto_launch_client=False,
        virtual_controller=False, target_fps=60,
    ))
    orch._frame_backend = _MissingEpochBackend(orch, [_valid_frame(8)])
    orch._frame_backend_mode = "capture_card"
    orch._running = True

    orch._capture_loop()

    assert orch._frame_seq == 1
    assert orch._frame_bundle[4] == 0.0
    assert orch._frame_bundle[5] == 0.0


def test_processing_uses_bundled_measurement_epoch_across_pts_interleaving():
    """Frame N cannot acquire frame N+1's mutable PTS-to-epoch calibration."""
    orch = _bare_integrity_orchestrator()
    frame = _valid_frame(9)
    captured_measurement_ms = 1_700_000_005_000.0
    orch._last_frame = frame
    orch._last_frame_ts = 25.0
    orch._frame_seq = 1
    orch._frame_bundle = (
        frame, 25.0, None, 5_000_000, 1_700_000_005_017.0,
        captured_measurement_ms, 1, 91, False, 0,
    )
    orch._stall_active = False
    orch._cv_frames_skipped = 0
    orch._latency_estimator_for_frame = lambda _generation: None

    finished = {}

    def _pose_update(_frame, _seq):
        # End after one iteration; the finish callback still runs for this frame.
        orch._running = False

    orch._pose_timing = SimpleNamespace(update=_pose_update)
    orch._emit_pose_overlay = lambda: None

    def _finish(_seq, _frame_number, _frozen, _success, _reason, **kwargs):
        finished.update(kwargs)

    orch._finish_processed_frame = _finish

    # Deterministic interleaving: capture has already published frame N, then the
    # next capture changes the live calibration before processing reads frame N.
    orch._pts_to_epoch_ms = 1_800_000_000_000.0

    def _forbidden_recompute(*_args, **_kwargs):
        orch._running = False
        raise AssertionError("processing consulted the live PTS mapping")

    orch._frame_measurement_epoch_ms = _forbidden_recompute
    orch._running = True
    orch._processing_loop()

    assert finished["measurement_epoch_ms"] == captured_measurement_ms
