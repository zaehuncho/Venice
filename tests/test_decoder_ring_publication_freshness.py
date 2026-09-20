"""No-device regression for decoder descheduling at the final publication lock."""
import numpy as np
import pytest

import chiaki_backend as decoder


class Clock:
    ns = 100_000_000_000

    def perf_counter_ns(self):
        return self.ns

    def time_ns(self):
        return 1_750_000_000_000_000_000 + self.ns


def _feed(monkeypatch, ring_delays_ms):
    clock = Clock()
    monkeypatch.setattr(decoder.time, "perf_counter_ns", clock.perf_counter_ns)
    monkeypatch.setattr(decoder.time, "time_ns", clock.time_ns)
    backend = decoder.OrionFramePipeBackend()
    backend._connected = True
    backend._connection_generation = backend._source_generation = 1
    payload = bytes(64 * 32 * 3 // 2)
    sequence = 0
    expect_payload = False
    delays = iter(ring_delays_ms)

    class DelayedRingLock:
        def __enter__(self):
            clock.ns += int(next(delays, 0) * 1_000_000)

        def __exit__(self, *_args):
            return False

    backend._ring._lock = DelayedRingLock()

    def read(size):
        nonlocal sequence, expect_payload
        if expect_payload:
            expect_payload = False
            assert size == len(payload)
            clock.ns += 3_000_000
            return payload
        if sequence == len(ring_delays_ms):
            return None
        sequence += 1
        expect_payload = True
        return backend._HEADER.pack(
            backend._MAGIC, backend._VERSION, backend._HEADER.size,
            0, 8, sequence, clock.ns, 64, 32, backend._FMT_NV12,
            len(payload), sequence * 16_667, 0,
        )

    def convert(_payload, w, h, _fmt):
        clock.ns += 1_000_000
        return np.full((h, w, 3), sequence, dtype=np.uint8)

    monkeypatch.setattr(backend, "_read_exact", read)
    monkeypatch.setattr(backend, "_to_bgr", convert)
    backend._reader_loop()
    return backend


def test_stall_inside_ring_publication_drops_frame_and_does_not_signal_ready(monkeypatch):
    backend = _feed(monkeypatch, [60])
    assert backend.get_frame_nonblocking() is None, "stale pixels crossed the final ring lock"
    assert backend._ring.total_frames == 0
    assert not backend.is_ready()
    assert not backend._cadence_samples
    assert backend._wire_reject_counts["publication_timestamp_stale"] == 1


def test_fresh_frame_immediately_recovers_after_ring_lock_stall(monkeypatch):
    backend = _feed(monkeypatch, [60, 1])
    assert backend._ring.total_frames == 1, "the stalled frame was published before recovery"
    latest = backend.get_frame_nonblocking()
    assert latest.frame_number == 2
    assert latest.publication_timestamp_ns - latest.capture_timestamp_ns == 5_000_000
    assert len(backend._cadence_samples) == 1
    assert backend.is_ready()
    assert backend._wire_consecutive_rejects == 0
    assert backend._wire_reject_counts["publication_timestamp_stale"] == 1


def test_repeated_ring_stalls_do_not_reset_rejection_streak(monkeypatch):
    backend = _feed(monkeypatch, [60] * 30)
    assert backend.get_frame_nonblocking() is None
    assert backend._wire_reject_counts["publication_timestamp_stale"] == 30
    assert backend._wire_reject_latch_logged
    assert not backend.is_ready()


@pytest.mark.parametrize("delay_ms", [0, 1, 46])
def test_within_existing_age_budget_preserves_source_identity_and_pixels(monkeypatch, delay_ms):
    backend = _feed(monkeypatch, [delay_ms])
    latest = backend.get_frame_nonblocking()
    assert latest.frame_number == latest.producer_sequence == 1
    assert latest.capture_timestamp_ns == latest.producer_timestamp_ns
    assert latest.publication_timestamp_ns - latest.capture_timestamp_ns == int((4 + delay_ms) * 1e6)
    assert np.all(latest.frame == 1)
    assert not latest.frame.flags.writeable
    assert backend._wire_reject_counts == {}
    assert backend.is_ready()


def test_default_ring_retains_legacy_non_decoder_behavior(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(decoder.time, "perf_counter_ns", clock.perf_counter_ns)
    ring = decoder.FrameRingBuffer(capacity=2)
    frame = decoder.FrameData(np.zeros((4, 4, 3), dtype=np.uint8), timestamp_ns=1)
    ring.put(frame)
    assert ring.get_latest(0.0) is frame
    assert ring.get_latest_nonblocking() is frame
