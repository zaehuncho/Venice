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


@pytest.mark.parametrize('future_ns', [2_000_001, 500_000_000, 10_000_000_000])
def test_bug_sweep_future_frame_cannot_poison_publication_order(monkeypatch, future_ns):
    import numpy as np
    from chiaki_backend import FrameData, FrameRingBuffer
    now=1_000_000_000
    monkeypatch.setattr('chiaki_backend.time.perf_counter_ns', lambda: now)
    ring=FrameRingBuffer(max_frame_age_ns=50_000_000)
    def frame(t):
        return FrameData(np.zeros((2, 2, 3), dtype=np.uint8), capture_timestamp_ns=t,
                         timestamp_ns=t, source_generation=1)
    first=frame(now-1_000_000)
    assert ring.put(first)
    assert not ring.put(frame(now+future_ns))
    assert ring.last_put_rejection == 'publication_timestamp_future'
    assert ring.get_latest_nonblocking() is first
    assert ring.put(frame(now))  # rejected future must not poison high-water state


def test_bug_sweep_future_rounding_tolerance_matches_decoder(monkeypatch):
    import numpy as np
    from chiaki_backend import FrameData, FrameRingBuffer
    monkeypatch.setattr('chiaki_backend.time.perf_counter_ns', lambda: 1_000_000_000)
    ring=FrameRingBuffer(max_frame_age_ns=50_000_000)
    frame=FrameData(np.zeros((2, 2, 3), dtype=np.uint8), capture_timestamp_ns=1_002_000_000)
    assert ring.put(frame)
    assert ring.get_latest_nonblocking() is frame


def test_bug_sweep_old_generation_completion_does_not_replace_new_pixels(monkeypatch):
    import numpy as np
    from chiaki_backend import FrameData, FrameRingBuffer
    now=1_000_000_000
    monkeypatch.setattr('chiaki_backend.time.perf_counter_ns', lambda: now)
    ring=FrameRingBuffer(max_frame_age_ns=50_000_000)
    new=FrameData(np.zeros((2, 2, 3), dtype=np.uint8), capture_timestamp_ns=now-10,
                  source_generation=2)
    old=FrameData(np.ones((2, 2, 3), dtype=np.uint8), capture_timestamp_ns=now-1,
                  source_generation=1)
    assert ring.put(new)
    assert not ring.put(old)
    assert ring.last_put_rejection == 'publication_source_generation_old'
    assert ring.get_latest_nonblocking() is new
    ring.clear()
    assert ring.put(old)  # explicit lifecycle reset permits a new numbering domain



def test_bug_sweep_future_frame_is_also_rejected_at_consumption(monkeypatch):
    import numpy as np
    from chiaki_backend import FrameData,FrameRingBuffer
    now=1_000_000_000
    monkeypatch.setattr('chiaki_backend.time.perf_counter_ns',lambda:now)
    ring=FrameRingBuffer(max_frame_age_ns=50_000_000)
    f=FrameData(np.zeros((2,2,3),dtype=np.uint8),capture_timestamp_ns=now)
    assert ring.put(f)
    now -= 3_000_000  # inconsistent clock domain must not look permanently fresh
    assert ring.get_latest_nonblocking() is None
    assert not ring._event.is_set()


def test_bug_sweep_legacy_ring_has_no_new_clock_or_generation_requirement(monkeypatch):
    import numpy as np
    from chiaki_backend import FrameData,FrameRingBuffer
    ring=FrameRingBuffer()
    first=FrameData(np.zeros((2,2,3),dtype=np.uint8),source_generation=9)
    last=FrameData(np.ones((2,2,3),dtype=np.uint8),source_generation=0)
    assert ring.put(first) and ring.put(last)
    assert ring.get_latest_nonblocking() is last
