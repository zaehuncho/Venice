"""Unit tests for the capture-card cadence-lock (PTS-alignment jitter reducer).

Covers: locks to 60fps (jitter collapses), rejects a single outlier without yanking the grid,
re-locks after an fps change, and the device-latency / stamp plumbing in _stamp_frame.
"""
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from capture_card_backend import CadenceLock, CaptureCardBackend  # noqa: E402


def _synth(fps=60.0, n=800, jitter_ms=3.0, seed=7):
    import random
    rng = random.Random(seed)
    dt = 1e9 / fps
    t = 0.0
    out = []
    for _ in range(n):
        t += dt
        out.append(int(t + rng.gauss(0.0, jitter_ms * 1e6)))
    return out


def _dt_std_ms(stamps):
    dts = [(stamps[i] - stamps[i - 1]) / 1e6 for i in range(1, len(stamps))]
    return statistics.pstdev(dts)


def test_hardware_pts_probe_is_enabled_but_remains_proof_gated(monkeypatch):
    monkeypatch.delenv("ORION_CAPTURE_USE_HW_PTS", raising=False)
    backend = CaptureCardBackend()
    assert backend._hw_pts_enabled is True
    assert backend._hw_pts_ok is False


def test_hardware_pts_can_be_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("ORION_CAPTURE_USE_HW_PTS", "0")
    assert CaptureCardBackend()._hw_pts_enabled is False


def test_locks_to_60fps_and_collapses_jitter():
    raw = _synth(60.0, n=800, jitter_ms=3.0)
    lock = CadenceLock(60.0)
    locked = [lock.update(t) for t in raw]
    raw_std = _dt_std_ms(raw)
    lock_std = _dt_std_ms(locked[50:])          # skip lock-in transient
    assert raw_std > 2.0                         # the input really is jittery (~3ms)
    assert lock_std < 0.5                        # sub-ms after the lock
    assert lock_std < raw_std / 5.0              # at least 5x tighter
    # mean cadence preserved (no drift)
    mean_dt = statistics.mean([(locked[i] - locked[i - 1]) / 1e6 for i in range(51, len(locked))])
    assert abs(mean_dt - 1000.0 / 60.0) < 0.2


def test_rejects_single_outlier():
    dt = 1e9 / 60.0
    lock = CadenceLock(60.0)
    # settle on a clean grid
    t = 0.0
    for _ in range(60):
        t += dt
        lock.update(int(t))
    grid_before = lock.t_locked
    # inject one badly-late read (+40ms — OFF-grid: 3.4 periods, not near an integer
    # multiple, so the drop hypothesis must reject it) — must NOT yank the grid forward
    t_outlier = t + dt + 40e6
    out = lock.update(int(t_outlier))
    # the emitted stamp advanced ~one nominal period, not ~40ms
    assert abs((out - grid_before) - dt) < 0.25 * dt
    # and the next on-time frame is still on-grid
    t += 2 * dt
    out2 = lock.update(int(t))
    assert abs((out2 - out) - dt) < 0.3 * dt


def test_multi_period_drop_quantizes_grid_advance():
    """Bughunt #5: a dropped-frames gap (e.g. 2 frames never delivered => gap ~= 3*dt) must
    advance the grid by the INTEGER number of periods actually elapsed, not exactly one.
    The old behavior stamped every subsequent frame (k-1) periods too OLD for up to
    relock_after frames (inflated frame_age -> over-lead -> early fire)."""
    dt = 1e9 / 60.0
    lock = CadenceLock(60.0)
    t = 0.0
    for _ in range(120):
        t += dt
        lock.update(int(t))
    relocks_before = lock.relocks
    # 2 frames dropped by the driver: next read arrives 3 periods after the previous one
    t += 3 * dt
    out = lock.update(int(t))
    assert abs(out - t) < 0.15 * dt, "the post-drop frame must be stamped ON the true grid (k=3)"
    # every subsequent on-time frame is stamped at its true time — no residual aging
    for _ in range(10):
        t += dt
        out = lock.update(int(t))
        assert abs(out - t) < 0.15 * dt, "post-drop frames must not be stamped a period too old"
    # an isolated drop is NOT a cadence change: the grid must absorb it without a re-lock
    assert lock.relocks == relocks_before


def test_burst_early_read_still_coasts_one_period():
    """A burst/duplicate read (gap far under one period) clamps to k=1 and lands in the
    outlier branch: the grid coasts one period instead of being yanked backward."""
    dt = 1e9 / 60.0
    lock = CadenceLock(60.0)
    t = 0.0
    for _ in range(60):
        t += dt
        lock.update(int(t))
    grid_before = lock.t_locked
    out = lock.update(int(t + 0.2 * dt))         # early read, +0.2*dt after the previous one
    assert abs((out - grid_before) - dt) < 1e5   # coasted exactly one period (no alpha pull)


def test_relocks_after_fps_change():
    lock = CadenceLock(60.0)
    dt60 = 1e9 / 60.0
    t = 0.0
    for _ in range(120):
        t += dt60
        lock.update(int(t))
    relocks_before = lock.relocks
    # source drops to 30fps: dt doubles. The big gap must trigger a re-lock, then track 30fps.
    dt30 = 1e9 / 30.0
    locked30 = []
    for _ in range(120):
        t += dt30
        locked30.append(lock.update(int(t)))
    assert lock.relocks > relocks_before        # a re-lock fired on the cadence break
    mean_dt = statistics.mean([(locked30[i] - locked30[i - 1]) / 1e6 for i in range(40, len(locked30))])
    assert abs(mean_dt - 1000.0 / 30.0) < 1.5   # now tracking ~33.3ms


def test_pts_lock_disabled_passthrough():
    lock = CadenceLock(60.0)
    # with lock disabled the backend passes raw through; emulate that contract
    raw = _synth(60.0, n=100, jitter_ms=3.0)
    # CadenceLock itself always locks; the backend gates it via self._pts_lock — verify the
    # backend honors ORION_CAPTURE_PTS_LOCK=0.
    os.environ["ORION_CAPTURE_PTS_LOCK"] = "0"
    try:
        b = CaptureCardBackend()
        assert b._pts_lock is False
        p, e = b._stamp_frame(1_000_000_000, 2_000_000_000)
        assert p == 1_000_000_000                # raw perf passthrough, no latency
        assert e == 2_000_000_000                # epoch = perf + measured offset
    finally:
        del os.environ["ORION_CAPTURE_PTS_LOCK"]


def test_device_latency_shifts_mean_earlier():
    os.environ["ORION_CAPTURE_PTS_LOCK"] = "0"
    os.environ["ORION_CAPTURE_DEVICE_LATENCY_MS"] = "35"
    try:
        b = CaptureCardBackend()
        assert b._latency_ns == 35_000_000
        p, e = b._stamp_frame(1_000_000_000, 2_000_000_000)
        # frame dated 35ms EARLIER than the read => frame_age reflects true glass->detector time
        assert p == 1_000_000_000 - 35_000_000
        assert e == 2_000_000_000 - 35_000_000
    finally:
        del os.environ["ORION_CAPTURE_PTS_LOCK"]
        del os.environ["ORION_CAPTURE_DEVICE_LATENCY_MS"]


def test_stamp_defaults_preserve_mean():
    # defaults: lock ON, latency 0 -> first stamp locks to the read (no mean shift)
    b = CaptureCardBackend()
    assert b._pts_lock is True
    assert b._latency_ns == 0
    p, e = b._stamp_frame(5_000_000_000, 6_000_000_000)
    assert p == 5_000_000_000                    # first frame anchors exactly on the read
    assert e == 6_000_000_000
