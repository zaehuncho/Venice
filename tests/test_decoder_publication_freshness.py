"""No-device decoder freshness regressions with a deterministic monotonic clock."""
import numpy as np
import pytest

import chiaki_backend as decoder


class Clock:
    ns = 100_000_000_000

    def perf_counter_ns(self):
        return self.ns

    def time_ns(self):
        return 1_750_000_000_000_000_000 + self.ns


def _feed(monkeypatch, delays_ms, *, stage="conversion"):
    clock = Clock()
    monkeypatch.setattr(decoder.time, "perf_counter_ns", clock.perf_counter_ns)
    monkeypatch.setattr(decoder.time, "time_ns", clock.time_ns)
    backend = decoder.OrionFramePipeBackend()
    backend._connected = True
    backend._connection_generation = backend._source_generation = 1
    payload = bytes(64 * 32 * 3 // 2)
    sequence = 0
    expect_payload = False
    published = []

    def read(size):
        nonlocal sequence, expect_payload
        if expect_payload:
            expect_payload = False
            assert size == len(payload)
            clock.ns += 3_000_000
            return payload
        if sequence == len(delays_ms):
            return None
        sequence += 1
        expect_payload = True
        return backend._HEADER.pack(
            backend._MAGIC, backend._VERSION, backend._HEADER.size,
            0, 8, sequence, clock.ns, 64, 32, backend._FMT_NV12,
            len(payload), sequence * 16_667, 0,
        )

    def convert(_payload, w, h, _fmt):
        if stage == "conversion":
            clock.ns += int(delays_ms[sequence - 1] * 1_000_000)
        return np.full((h, w, 3), sequence, dtype=np.uint8)

    class DelayedPublication:
        def __enter__(self):
            clock.ns += int(delays_ms[sequence - 1] * 1_000_000)

        def __exit__(self, *_args):
            return False

    if stage == "publication_lock":
        backend._publication_lock = DelayedPublication()
    put = backend._ring.put

    def publish(frame):
        published.append(frame)
        put(frame)

    monkeypatch.setattr(backend, "_read_exact", read)
    monkeypatch.setattr(backend, "_to_bgr", convert)
    monkeypatch.setattr(backend._ring, "put", publish)
    backend._reader_loop()
    return backend, published


@pytest.mark.parametrize("stage", ["conversion", "publication_lock"])
def test_frame_that_ages_out_after_payload_read_is_not_published(monkeypatch, stage):
    backend, published = _feed(monkeypatch, [60], stage=stage)
    assert published == [], "decoder published pixels older than its existing freshness budget"
    assert backend.get_frame_nonblocking() is None
    assert not backend.is_ready()
    assert backend._wire_seq_last == 1
    assert backend._wire_reject_counts["publication_timestamp_stale"] == 1


@pytest.mark.parametrize("stage", ["conversion", "publication_lock"])
def test_next_fresh_frame_recovers_without_extra_buffering(monkeypatch, stage):
    backend, published = _feed(monkeypatch, [60, 1], stage=stage)
    assert [frame.frame_number for frame in published] == [2]
    fresh = published[0]
    assert fresh.publication_timestamp_ns - fresh.capture_timestamp_ns == 4_000_000
    assert backend.get_frame_nonblocking() is fresh
    assert backend.is_ready()
    assert backend._wire_seq_last == 2
    assert backend._wire_seq_gaps == 0
    assert backend._wire_consecutive_rejects == 0


def test_repeated_conversion_stalls_do_not_reset_rejection_streak(monkeypatch):
    backend, published = _feed(monkeypatch, [60] * 30)
    assert published == []
    assert backend._wire_reject_counts["publication_timestamp_stale"] == 30
    assert backend._wire_reject_latch_logged
    assert not backend.is_ready()


def test_within_budget_conversion_preserves_original_capture_clock(monkeypatch):
    backend, published = _feed(monkeypatch, [20, 30])
    assert [frame.frame_number for frame in published] == [1, 2]
    assert [frame.publication_timestamp_ns - frame.capture_timestamp_ns
            for frame in published] == [23_000_000, 33_000_000]
    assert backend._wire_reject_counts == {}
