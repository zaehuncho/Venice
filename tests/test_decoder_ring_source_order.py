"""Latest-source-frame wins even when publication completion order differs."""
import numpy as np
import pytest
import chiaki_backend as decoder


NOW = 100_000_000_000


def frame(stamp, generation=7):
    return decoder.FrameData(np.zeros((2, 2, 3), dtype=np.uint8),
                             capture_timestamp_ns=stamp, source_generation=generation)


@pytest.mark.parametrize("blocking", [False, True])
@pytest.mark.parametrize("delta", [0, -10_000_000])
def test_delayed_or_repeated_publication_cannot_replace_newer_source(monkeypatch, blocking, delta):
    monkeypatch.setattr(decoder.time, "perf_counter_ns", lambda: NOW)
    ring = decoder.FrameRingBuffer(max_frame_age_ns=50_000_000)
    current = frame(NOW - 1_000_000)
    assert ring.put(current)
    old = frame(current.capture_timestamp_ns + delta)
    assert not ring.put(old), "last completed publication replaced fresher source pixels"
    assert ring.total_frames == 1
    assert old.publication_timestamp_ns == 0
    assert (ring.get_latest(0) if blocking else ring.get_latest_nonblocking()) is current


def test_rejected_frames_do_not_erase_order_baseline_after_stale_read(monkeypatch):
    now = [NOW]
    monkeypatch.setattr(decoder.time, "perf_counter_ns", lambda: now[0])
    ring = decoder.FrameRingBuffer(max_frame_age_ns=50_000_000)
    assert ring.put(frame(NOW - 1_000_000))
    now[0] += 51_000_000
    assert ring.get_latest_nonblocking() is None
    # A new publication is allowed; rejecting duplicates does not wedge recovery.
    fresh = frame(now[0] - 1_000_000)
    assert ring.put(fresh)
    for _ in range(40):
        assert not ring.put(frame(fresh.capture_timestamp_ns - 1_000_000))
    assert ring.get_latest_nonblocking() is fresh


def test_new_source_generation_and_explicit_clear_reset_source_order(monkeypatch):
    monkeypatch.setattr(decoder.time, "perf_counter_ns", lambda: NOW)
    ring = decoder.FrameRingBuffer(max_frame_age_ns=50_000_000)
    assert ring.put(frame(NOW - 1_000_000))
    replacement = frame(NOW - 2_000_000, generation=8)
    assert ring.put(replacement)
    ring.clear()
    assert ring.put(frame(NOW - 3_000_000, generation=8))


def test_non_clock_attested_ring_preserves_legacy_arrival_order(monkeypatch):
    monkeypatch.setattr(decoder.time, "perf_counter_ns", lambda: NOW)
    ring = decoder.FrameRingBuffer()
    assert ring.put(frame(NOW - 1_000_000))
    older = frame(NOW - 2_000_000)
    assert ring.put(older)
    assert ring.get_latest_nonblocking() is older
