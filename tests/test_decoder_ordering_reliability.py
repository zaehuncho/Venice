"""Rejected samples must not become the baseline that admits older pixels."""
from chiaki_backend import OrionFramePipeBackend
from remote_play_orchestrator import RemotePlayOrchestrator
import pytest


def test_wire_pts_reject_does_not_lower_high_water_mark():
    b = OrionFramePipeBackend()
    assert b._wire_pts_reason(10_000) == ""
    for pts in (1_000, 2_000, 3_000, 9_999, 10_000):
        assert b._wire_pts_reason(pts) == "pts_regression"
        assert b._wire_pts_last == 10_000
    assert b._wire_pts_reason(10_001) == ""


def test_detector_pts_reject_does_not_lower_high_water_mark():
    o = RemotePlayOrchestrator.__new__(RemotePlayOrchestrator)
    assert o._source_pts_reason(10_000, 1_000_000, 1) == ""
    for pts in (1_000, 2_000, 3_000, 9_999, 10_000):
        assert o._source_pts_reason(pts, 1_000_000, 1) == "decoder_pts_regression"
        assert o._pts_source_last == 10_000
    assert o._source_pts_reason(10_001, 1_000_000, 1) == ""
    assert o._source_pts_reason(1_000, 1_000_000, 2) == ""


def test_missing_clock_never_poison_pts_baseline():
    o = RemotePlayOrchestrator.__new__(RemotePlayOrchestrator)
    assert o._source_pts_reason(10_000, 1_000_000, 1) == ""
    assert o._source_pts_reason(1_000_000, 0, 1) == "decoder_pts_clock_missing"
    assert o._pts_source_last == 10_000
    assert o._source_pts_reason(10_001, 1_000_000, 1) == ""


def test_reject_streak_clears_readiness_not_ordering_proof():
    b = OrionFramePipeBackend()
    b._wire_pts_last = 10_000
    b._wire_producer_ts_last = 2_000_000
    b._ready_evt.set()
    for _ in range(b._WIRE_REJECT_REBASELINE_FRAMES - 1):
        assert not b._wire_reject_latched("pts_regression")
    assert b._wire_reject_latched("pts_regression")
    assert not b._ready_evt.is_set()
    assert b._wire_pts_last == 10_000
    assert b._wire_producer_ts_last == 2_000_000


@pytest.mark.parametrize("reason", ["producer_timestamp_stale", "pts_missing"])
def test_reader_retires_connection_at_rejection_limit(monkeypatch, reason):
    b = OrionFramePipeBackend()
    b._handle = object()
    b._wire_pts_last = 10_000
    b._ready_evt.set()
    monkeypatch.setattr(b, "_producer_lease_reason_now", lambda: "")
    monkeypatch.setattr("chiaki_backend.time.perf_counter_ns", lambda: 1_000_000_000)
    chunks = []
    for seq in range(1, b._WIRE_REJECT_REBASELINE_FRAMES + 2):
        producer = (1_000 if reason == "producer_timestamp_stale" else 999_000_000) + seq
        pts = 10_000 + seq if reason == "producer_timestamp_stale" else 0
        chunks.extend((b._HEADER.pack(b._MAGIC, b._VERSION, b._HEADER.size, 0,
            1, seq, producer, 2, 2, b._FMT_NV12, 6, pts, 0), b"\0" * 6))
    reads = []
    def read_exact(size):
        reads.append(size)
        return chunks.pop(0) if chunks else None
    monkeypatch.setattr(b, "_read_exact", read_exact)
    b._reader_loop()
    assert len(reads) == 2 * b._WIRE_REJECT_REBASELINE_FRAMES
    assert len(chunks) == 2  # The old connection is not resumed after the latch.
    assert not b._ready_evt.is_set()
    assert b._wire_pts_last == 10_000
