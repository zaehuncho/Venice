"""No-device timing epoch regressions for restarting/reopening capture handles."""
import numpy as np
import pytest
import threading

import capture_card_backend as capture


class Clock:
    ns = 100_000_000_000
    epoch_offset = 1_750_000_000_000_000_000

    def perf_counter_ns(self):
        return self.ns

    def time_ns(self):
        return self.ns + self.epoch_offset


@pytest.mark.parametrize("pts_lock", [False, True])
def test_reopened_zero_based_device_clock_does_not_inherit_old_anchor(monkeypatch, pts_lock):
    clock = Clock()
    monkeypatch.setattr(capture.time, 'perf_counter_ns', clock.perf_counter_ns)
    monkeypatch.setattr(capture.time, 'time_ns', clock.time_ns)
    backend = capture.CaptureCardBackend()
    backend._min_width = backend._min_height = 4
    backend._aspect_ratio = 1.0
    backend._pts_lock = pts_lock
    backend._hw_pts_enabled = True
    backend._frame_number = 10_000
    backend._source_generation = 40
    backend._hw_pts_decided = backend._hw_pts_ok = True
    backend._hw_pts_anchor = (10_000.0, clock.ns)
    backend._hw_pts_prev_ms = 10_000.0
    backend._hw_pts_good = 20
    backend._cadence.reset(clock.ns)
    backend._latency_ns = 35_000_000
    backend._stall_reopen = True
    backend._stall_ms = 20.0
    backend._stall_cooldown_ns = 0
    backend._reopen_settle_s = 0.0

    class Cap:
        def __init__(self, restart=False):
            self.reads = 0
            self.restart = restart

        def read(self):
            self.reads += 1
            clock.ns += 16_666_667
            # Fresh device content changes; only the old handle triggers reopen.
            value = self.reads if self.restart else 1
            return True, np.full((4, 4, 3), value, dtype=np.uint8)

        def get(self, _prop):
            if self.restart:
                return (self.reads - 1) * (1000.0 / 60.0)
            return 10_000.0 + self.reads * (1000.0 / 60.0)

        def release(self):
            pass

    backend._cap = Cap()
    replacement = Cap(restart=True)

    def reopen():
        clock.ns += 3_000_000_000
        # A wall-clock correction during downtime must not survive as a slow EMA.
        clock.epoch_offset += 2_000_000_000
        return replacement, 'DSHOW'

    accepted = []

    def publish(fd):
        if fd.source_generation == 41:
            accepted.append((fd, clock.ns, clock.time_ns()))
            if len(accepted) == 25:
                backend._stop_evt.set()

    monkeypatch.setattr(backend, '_open', reopen)
    monkeypatch.setattr(backend._ring, 'put', publish)
    backend._run()

    assert len(accepted) == 25
    ages = [(now - fd.capture_timestamp_ns) / 1e6 for fd, now, _epoch in accepted]
    assert max(abs(age - 35.0) for age in ages) < 0.001, ages
    epoch_ages = [(epoch - fd.capture_epoch_ns) / 1e6 for fd, _now, epoch in accepted]
    assert max(abs(age - 35.0) for age in epoch_ages) < 0.001, epoch_ages
    assert backend._hw_pts_decided and backend._hw_pts_ok
    assert backend._hw_pts_anchor[0] < 1_000.0
    assert all(fd.source_generation == 41 for fd, _now, _epoch in accepted)


@pytest.mark.parametrize("previously_usable", [False, True])
@pytest.mark.parametrize("probe_valid", [False, True])
def test_start_on_reused_backend_reprobes_hardware_pts_for_full_source_window(monkeypatch, previously_usable, probe_valid):
    backend = capture.CaptureCardBackend()
    backend._frame_number = 10_000
    backend._source_generation = 40
    backend._hw_pts_decided = True
    backend._hw_pts_ok = previously_usable
    backend._hw_pts_good = 20 if previously_usable else 0
    backend._hw_pts_anchor = (10_000.0, 100_000_000_000) if previously_usable else None
    backend._hw_pts_prev_ms = 10_000.0
    backend._perf_to_epoch_off_ns = 12345
    backend._cadence.reset(100_000_000_000)

    class Thread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(capture.threading, 'Thread', Thread)
    monkeypatch.setattr(capture, '_device_names', lambda: ['Capture Card'])
    monkeypatch.setattr(capture, '_save_cached_index', lambda *_args: None)
    monkeypatch.setattr(backend, '_candidate_indices', lambda: [0])
    monkeypatch.setattr(backend, '_open', lambda _index: (object(), 'DSHOW'))
    assert backend.start()
    assert not backend._hw_pts_decided and not backend._hw_pts_ok
    assert backend._hw_pts_anchor is None and backend._hw_pts_prev_ms is None
    assert backend._cadence.t_locked is None
    assert backend._perf_to_epoch_off_ns == 0
    assert backend._frame_number == 10_000
    for n in range(21 if probe_valid else 45):
        backend._frame_number += 1
        pts_ms = (n + 1) * (1000.0 / 60.0) if probe_valid else 0.0
        backend._probe_hw_pts(pts_ms, 200_000_000_000 + n * 16_666_667)
        if n < (20 if probe_valid else 44):
            assert not backend._hw_pts_decided
    assert backend._hw_pts_decided and backend._hw_pts_ok is probe_valid
    assert backend._source_generation == 41


@pytest.mark.parametrize("transition_stage", ["before_recovery", "during_open"])
def test_abandoned_reopen_cannot_replace_newer_active_source(monkeypatch, transition_stage):
    clock = Clock()
    monkeypatch.setattr(capture.time, 'perf_counter_ns', clock.perf_counter_ns)
    monkeypatch.setattr(capture.time, 'time_ns', clock.time_ns)
    backend = capture.CaptureCardBackend()
    backend._min_width = backend._min_height = 4
    backend._aspect_ratio = 1.0
    backend._pts_lock = False
    backend._hw_pts_decided = True
    backend._stall_reopen = True
    backend._stall_ms = 1.0
    backend._stall_cooldown_ns = 0
    backend._reopen_settle_s = 0.0
    backend._source_generation = 40
    events = []

    class Cap:
        def __init__(self, name):
            self.name = name
            self.reads = 0

        def read(self):
            self.reads += 1
            clock.ns += 2_000_000
            if self.name != 'old':
                # Bound the regression even if the old code installs this handle.
                backend._stop_evt.set()
            return True, np.ones((4, 4, 3), dtype=np.uint8)

        def release(self):
            events.append(('release', self.name, threading.current_thread().name))

    old = Cap('old')
    obsolete_candidate = Cap('obsolete')
    active = Cap('active')
    backend._cap = old
    active_cadence = capture.CadenceLock(60.0)
    active_cadence.reset(clock.ns + 1_000_000_000)
    active_frame = capture.FrameData(np.full((4, 4, 3), 88, dtype=np.uint8), source_generation=41)
    real_put = backend._ring.put

    def install_active_source():
        # Deterministic stop/restart interleaving: the event has been cleared by
        # the new owner before the abandoned open returns.
        backend._stop_evt.set()
        backend._cap = active
        backend._source_generation = 41
        backend._cadence = active_cadence
        backend._reopen_grace_ns = 987654321
        backend._stop_evt.clear()
        real_put(active_frame)

    def publish(fd):
        real_put(fd)

    def hash_then_transition(data):
        # Hashing occurs after the publication critical section and immediately
        # before recovery eligibility/handle invalidation.
        if old.reads == 2 and transition_stage == 'before_recovery':
            install_active_source()
        return hash(data)

    def reopen():
        events.append(('open', 'obsolete', threading.current_thread().name))
        if transition_stage == 'during_open':
            install_active_source()
        return obsolete_candidate, 'DSHOW'

    monkeypatch.setattr(backend._ring, 'put', publish)
    monkeypatch.setattr(capture, 'hash', hash_then_transition, raising=False)
    monkeypatch.setattr(backend, '_open', reopen)
    reader = threading.Thread(target=backend._run, name='AbandonedReopenReader')
    reader.start()
    reader.join(3.0)
    assert not reader.is_alive()
    assert backend._cap is active
    assert backend._source_generation == 41
    assert backend._cadence is active_cadence
    assert backend._reopen_grace_ns == 987654321
    assert backend._ring.get_latest_nonblocking() is active_frame
    assert obsolete_candidate.reads == active.reads == 0
    assert ('release', 'active', 'AbandonedReopenReader') not in events
    if transition_stage == 'during_open':
        assert events == [('release', 'old', 'AbandonedReopenReader'),
                          ('open', 'obsolete', 'AbandonedReopenReader'),
                          ('release', 'obsolete', 'AbandonedReopenReader')]
    else:
        assert events == [('release', 'old', 'AbandonedReopenReader')]
