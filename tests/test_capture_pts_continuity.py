"""Exercise real capture stamp/publication code with a once-proven device clock."""
import math

import numpy as np
import pytest

import capture_card_backend as capture


DT_NS = 16_666_667
BASE_NS = 100_000_000_000
EPOCH_OFFSET = 1_750_000_000_000_000_000


def _backend(pts_lock):
    backend = capture.CaptureCardBackend()
    backend._min_width = backend._min_height = 4
    backend._aspect_ratio = 1.0
    backend._stall_reopen = False
    backend._pts_lock = pts_lock
    backend._latency_ns = 35_000_000
    backend._source_generation = 8
    backend._hw_pts_enabled = True
    backend._hw_pts_decided = backend._hw_pts_ok = True
    backend._hw_pts_anchor = (1000.0, BASE_NS)
    backend._hw_pts_prev_ms = 1000.0
    backend._cadence.reset(BASE_NS)
    return backend


@pytest.mark.parametrize("pts_lock", [False, True])
@pytest.mark.parametrize("fault", ["zero", "nan", "inf", "repeat", "regress", "future"])
def test_clock_fault_is_a_fenced_one_frame_transition_not_a_timeline_splice(monkeypatch, pts_lock, fault):
    backend = _backend(pts_lock)
    now = BASE_NS
    publications = []
    clears = []

    class Cap:
        reads = 0
        queries = 0

        def read(self):
            nonlocal now
            self.reads += 1
            now += DT_NS
            if self.reads > 5:
                backend._stop_evt.set()
            return True, np.full((4, 4, 3), self.reads, dtype=np.uint8)

        def get(self, _prop):
            self.queries += 1
            if self.reads == 3:
                return {"zero": 0.0, "nan": float("nan"), "inf": float("inf"),
                        "repeat": 1000.0 + 2 * DT_NS / 1e6,
                        "regress": 500.0, "future": 50_000.0}[fault]
            return 1000.0 + self.reads * DT_NS / 1e6

        def release(self):
            pass

    cap = Cap()
    backend._cap = cap
    real_clear = backend._ring.clear
    real_put = backend._ring.put

    def clear():
        clears.append((cap.reads, backend._source_generation))
        real_clear()

    def put(frame):
        publications.append((frame, now))
        real_put(frame)

    monkeypatch.setattr(capture.time, "perf_counter_ns", lambda: now)
    monkeypatch.setattr(capture.time, "time_ns", lambda: now + EPOCH_OFFSET)
    monkeypatch.setattr(backend._ring, "clear", clear)
    monkeypatch.setattr(backend._ring, "put", put)
    backend._run()

    assert [int(fd.frame[0, 0, 0]) for fd, _ in publications] == [1, 2, 4, 5]
    assert [fd.source_generation for fd, _ in publications] == [8, 8, 9, 9]
    assert clears == [(3, 9)]
    assert cap.queries == 3, "a revoked clock stays off for this handle"
    assert not backend._hw_pts_ok and backend._hw_pts_decided
    stamps = [fd.capture_timestamp_ns for fd, _ in publications]
    assert all(b > a for a, b in zip(stamps, stamps[1:]))
    for frame, read_ns in publications:
        assert abs(frame.capture_timestamp_ns - (read_ns - 35_000_000)) <= 2
        assert abs(frame.capture_epoch_ns - (read_ns + EPOCH_OFFSET - 35_000_000)) <= 128


def test_old_but_monotonic_hardware_stamp_keeps_real_queue_age():
    backend = _backend(False)

    class Cap:
        def get(self, _prop):
            return 1000.0 + DT_NS / 1e6

    backend._cap = Cap()
    # A delayed consumer must not restamp a real older frame fresh.
    read_ns = BASE_NS + 500_000_000
    result, _ = backend._stamp_frame(read_ns, read_ns + EPOCH_OFFSET)
    assert abs(result - (BASE_NS + DT_NS - 35_000_000)) <= 2
    assert backend._hw_pts_ok
    assert not getattr(backend, "_hw_pts_fault_reason", "")


def test_valid_dropped_frame_interval_preserves_device_timeline():
    backend = _backend(True)

    class Cap:
        def get(self, _prop):
            return 1000.0 + 3 * DT_NS / 1e6

    backend._cap = Cap()
    read_ns = BASE_NS + 3 * DT_NS
    result, _ = backend._stamp_frame(read_ns, read_ns + EPOCH_OFFSET)
    assert abs(result - (read_ns - 35_000_000)) <= 2
    assert backend._hw_pts_ok
    assert not getattr(backend, "_hw_pts_fault_reason", "")


def test_late_old_clock_query_cannot_revoke_or_advance_replacement_timing():
    backend = _backend(True)
    new_cap = object()
    new_cadence = capture.CadenceLock(60.0)
    new_cadence.reset(BASE_NS + 10_000_000_000)
    new_anchor = (7.0, BASE_NS + 10_000_000_000)

    class OldCap:
        def get(self, _prop):
            backend._cap = new_cap
            backend._source_generation = 9
            backend._reset_source_timing()
            backend._cadence = new_cadence
            backend._hw_pts_decided = backend._hw_pts_ok = True
            backend._hw_pts_anchor = new_anchor
            backend._hw_pts_prev_ms = 7.0
            return 0.0

    backend._cap = OldCap()
    read_ns = BASE_NS + DT_NS
    backend._stamp_frame(read_ns, read_ns + EPOCH_OFFSET)
    assert backend._cap is new_cap and backend._source_generation == 9
    assert backend._cadence is new_cadence
    assert backend._cadence.t_locked == BASE_NS + 10_000_000_000
    assert backend._hw_pts_ok and backend._hw_pts_anchor == new_anchor
    assert backend._hw_pts_prev_ms == 7.0
    assert not getattr(backend, "_hw_pts_fault_reason", "")


def test_real_source_reset_restores_bounded_probe_after_latched_fallback():
    backend = _backend(True)
    backend._hw_pts_ok = False
    backend._hw_pts_fault_reason = "nonadvancing"
    backend._reset_source_timing()
    assert not backend._hw_pts_decided and not backend._hw_pts_ok
    assert backend._hw_pts_anchor is None
    assert backend._hw_pts_prev_ms is None
    assert not backend._hw_pts_fault_reason
    assert backend._cadence.t_locked is None
