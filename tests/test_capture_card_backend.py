"""HDMI capture-card frame backend.

Contract + graceful-failure tests always run; the live-capture test skips when no
video device is present (CI / headless dev boxes), so the suite is green without a
capture card plugged in.
"""
import numpy as np
import pytest

pytest.importorskip("cv2", reason="opencv not installed")

from capture_card_backend import CaptureCardBackend
from chiaki_backend import FrameData


def test_contract_no_frames_before_start():
    b = CaptureCardBackend(device_index=0)
    assert b.get_frame_nonblocking() is None


def test_ambiguous_static_content_reopen_is_opt_in(monkeypatch):
    """A byte-identical source can be a legitimate menu/loading screen.  Production must
    not repeatedly close an exclusive capture-card handle from pixels alone; the experiment
    remains available explicitly for rigs where that signal has been live-validated."""
    monkeypatch.delenv("ORION_CAPTURE_STALL_REOPEN", raising=False)
    assert not CaptureCardBackend(device_index=0)._stall_reopen

    monkeypatch.setenv("ORION_CAPTURE_STALL_REOPEN", "1")
    assert CaptureCardBackend(device_index=0)._stall_reopen


def test_warm_cache_route_requires_actual_dshow_and_same_resolved_index():
    backend = CaptureCardBackend(device_index=2)

    backend._api_name = "DSHOW"
    assert backend.active_route() == ("DSHOW", 2)
    assert backend.warm_cache_route_matches(2)
    assert not backend.warm_cache_route_matches(1)

    # A successful MSMF fallback at the same number is not DirectShow identity
    # proof; MSMF enumeration order is independent and must revoke warm reuse.
    backend._api_name = "MSMF"
    assert backend.active_route() == ("MSMF", 2)
    assert not backend.warm_cache_route_matches(2)

    backend._api_name = ""
    assert not backend.warm_cache_route_matches(2)
    assert not backend.warm_cache_route_matches("not-an-index")


def test_bogus_device_fails_gracefully(monkeypatch, tmp_path):
    # A configured index that cannot be opened must make start() return False (not raise) so
    # the orchestrator's retry policy owns recovery. HERMETIC: the 2026-07-02 webcam-protection
    # rework recovers via the persisted last-good index / candidate probing, so on a dev box
    # with a real capture card the old form of this test "failed" by SUCCEEDING. Stub out
    # every escape hatch: no openable device, no cached index, no device-name list.
    import capture_card_backend as ccb

    class _NeverOpens:
        def __init__(self, *a, **k):
            pass

        def isOpened(self):
            return False

        def release(self):
            pass

    # cv2 is lazily imported inside the backend's methods — patch the shared module object.
    import cv2

    monkeypatch.setattr(cv2, "VideoCapture", _NeverOpens)
    monkeypatch.setattr(ccb, "_INDEX_CACHE", str(tmp_path / "no_cache.json"))
    monkeypatch.setattr(ccb, "_BRUTE_SCANNED", False)
    monkeypatch.delenv("ORION_VIDEO_DEVICE_NAMES", raising=False)

    b = CaptureCardBackend(device_index=99)
    result = b.start()
    assert result is False
    b.stop()


def test_delivers_bgr_frame_if_device_present():
    b = CaptureCardBackend(device_index=0)
    if not b.start():
        pytest.skip("no capture device at index 0")
    try:
        import time
        fd = None
        deadline = time.time() + 4.0
        while time.time() < deadline:
            fd = b.get_frame_nonblocking()
            if fd is not None:
                break
            time.sleep(0.05)
        assert fd is not None, "device opened but delivered no frame"
        assert isinstance(fd, FrameData)
        frame = fd.frame
        assert isinstance(frame, np.ndarray)
        assert frame.ndim == 3 and frame.shape[2] == 3
        assert frame.dtype == np.uint8
        assert frame.flags["OWNDATA"] and frame.flags["C_CONTIGUOUS"]
        assert not frame.flags["WRITEABLE"], "published capture buffers must be immutable"
    finally:
        b.stop()


def test_reader_survives_frame_post_process_error(monkeypatch):
    """A per-frame processing error (malformed frame / transient alloc failure AFTER a successful
    read) must skip that one frame, never kill the reader thread — a dead reader forces the
    orchestrator to tear down and re-open the whole backend (capture churn). The reader must keep
    pumping and deliver frames once the transient clears."""
    import threading
    import time

    b = CaptureCardBackend(device_index=0)
    # This test exercises post-processing resilience with a tiny synthetic frame;
    # relax only this instance's geometry contract (production remains 1280x720/16:9).
    b._min_width = 4
    b._min_height = 4
    b._aspect_ratio = 1.0

    class _Cap:
        def read(self):
            return True, np.zeros((4, 4, 3), dtype=np.uint8)

        def release(self):
            pass

    b._cap = _Cap()
    b._stop_evt.clear()

    calls = {"n": 0}
    real_put = b._ring.put

    def flaky_put(fd):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise RuntimeError("simulated transient frame-processing failure")
        real_put(fd)

    monkeypatch.setattr(b._ring, "put", flaky_put)

    th = threading.Thread(target=b._run, daemon=True)
    th.start()
    try:
        time.sleep(0.15)
    finally:
        b._stop_evt.set()
        b._cap = None
        th.join(timeout=2.0)

    assert not th.is_alive(), "reader thread must exit cleanly on stop"
    assert calls["n"] > 3, "reader must have kept pumping past the transient failures"
    assert b.get_frame_nonblocking() is not None, "a frame must be delivered once the transient clears"


def test_stall_reopen_tracks_content_change_on_successful_frame(monkeypatch):
    """The opt-in content-stall recovery must execute after publication without
    referencing a stale capture-clock variable or silently skipping its state update."""
    b = CaptureCardBackend(device_index=0)
    b._min_width = 4
    b._min_height = 4
    b._aspect_ratio = 1.0
    b._stall_reopen = True
    b._pts_lock = False
    b._hw_pts_decided = True

    class _Cap:
        def read(self):
            return True, np.zeros((4, 4, 3), dtype=np.uint8)

        def release(self):
            pass

    b._cap = _Cap()
    b._stop_evt.clear()

    def capture_once(_fd):
        b._stop_evt.set()

    monkeypatch.setattr(b._ring, "put", capture_once)
    b._run()

    assert b._sig is not None
    assert b._sig_change_ns > 0


def test_stall_reopen_releases_before_open_and_advances_source_generation(monkeypatch):
    """A byte-frozen feed must cross (not merely reach) the configured threshold,
    release its exclusive handle before opening a replacement, and mark replacement
    frames with a new source generation."""
    import capture_card_backend as ccb

    class _Clock:
        def __init__(self):
            self.ns = 10_000_000_000

        def advance_ms(self, value):
            self.ns += int(float(value) * 1e6)

        def perf_counter_ns(self):
            return self.ns

        def time_ns(self):
            return 1_750_000_000_000_000_000 + self.ns

    clock = _Clock()
    monkeypatch.setattr(ccb.time, "perf_counter_ns", clock.perf_counter_ns)
    monkeypatch.setattr(ccb.time, "time_ns", clock.time_ns)

    backend = CaptureCardBackend(device_index=0)
    backend._min_width = 4
    backend._min_height = 4
    backend._aspect_ratio = 1.0
    backend._pts_lock = False
    backend._hw_pts_decided = True
    backend._stall_reopen = True
    backend._stall_ms = 10.0
    backend._stall_cooldown_ns = 0
    backend._reopen_settle_s = 0.0
    backend._source_generation = 40
    backend._api_name = "MSMF"
    events = []

    class _OldCap:
        def __init__(self):
            self.reads = 0
            self.release_count = 0

        def read(self):
            self.reads += 1
            # Frame 2 reaches exactly 10 ms of unchanged content. The production
            # condition is strictly greater-than, so only frame 3 may trigger reopen.
            clock.advance_ms((1, 10, 1)[self.reads - 1])
            events.append(f"old-read-{self.reads}")
            return True, np.zeros((4, 4, 3), dtype=np.uint8)

        def release(self):
            self.release_count += 1
            events.append("old-release")

    class _NewCap:
        def __init__(self):
            self.release_count = 0

        def read(self):
            clock.advance_ms(1)
            events.append("new-read")
            return True, np.ones((4, 4, 3), dtype=np.uint8)

        def release(self):
            self.release_count += 1
            events.append("new-release")

    old_cap = _OldCap()
    new_cap = _NewCap()
    backend._cap = old_cap
    backend._stop_evt.clear()
    open_observation = {}

    def _open_replacement():
        open_observation["old_reads"] = old_cap.reads
        open_observation["old_release_count"] = old_cap.release_count
        events.append("open")
        return new_cap, "DSHOW"

    published = []

    def _publish(frame_data):
        published.append(frame_data)
        if frame_data.source_generation == 41:
            backend._stop_evt.set()

    monkeypatch.setattr(backend, "_open", _open_replacement)
    monkeypatch.setattr(backend._ring, "put", _publish)

    backend._run()

    assert open_observation == {"old_reads": 3, "old_release_count": 1}
    assert events.index("old-release") < events.index("open") < events.index("new-read")
    assert old_cap.release_count == 1
    assert new_cap.release_count == 1
    assert backend._source_generation == 41
    assert [frame.source_generation for frame in published] == [40, 40, 40, 41]
    assert published[-1].capture_api == "DSHOW"
    assert backend._cap is None


def test_stall_reopen_discards_new_handle_when_stop_arrives_during_open(monkeypatch):
    """A stop racing the blocking reopen must not publish/resurrect its fresh handle.
    Both handles remain reader-thread owned and the failed transition cannot advance
    source identity."""
    import capture_card_backend as ccb

    class _Clock:
        def __init__(self):
            self.ns = 20_000_000_000

        def advance_ms(self, value):
            self.ns += int(float(value) * 1e6)

        def perf_counter_ns(self):
            return self.ns

        def time_ns(self):
            return 1_750_000_000_000_000_000 + self.ns

    clock = _Clock()
    monkeypatch.setattr(ccb.time, "perf_counter_ns", clock.perf_counter_ns)
    monkeypatch.setattr(ccb.time, "time_ns", clock.time_ns)

    backend = CaptureCardBackend(device_index=0)
    backend._min_width = 4
    backend._min_height = 4
    backend._aspect_ratio = 1.0
    backend._pts_lock = False
    backend._hw_pts_decided = True
    backend._stall_reopen = True
    backend._stall_ms = 1.0
    backend._stall_cooldown_ns = 0
    backend._reopen_settle_s = 0.0
    backend._source_generation = 12
    backend._api_name = "MSMF"
    events = []

    class _Cap:
        def __init__(self, name):
            self.name = name
            self.reads = 0
            self.release_count = 0

        def read(self):
            self.reads += 1
            clock.advance_ms(2)
            events.append(f"{self.name}-read")
            return True, np.zeros((4, 4, 3), dtype=np.uint8)

        def release(self):
            self.release_count += 1
            events.append(f"{self.name}-release")

    old_cap = _Cap("old")
    new_cap = _Cap("new")
    backend._cap = old_cap
    backend._stop_evt.clear()

    def _open_then_stop():
        events.append("open")
        backend._stop_evt.set()
        return new_cap, "DSHOW"

    published = []
    monkeypatch.setattr(backend, "_open", _open_then_stop)
    monkeypatch.setattr(backend._ring, "put", published.append)

    backend._run()

    assert events.index("old-release") < events.index("open") < events.index("new-release")
    assert old_cap.release_count == 1
    assert new_cap.release_count == 1
    assert new_cap.reads == 0
    assert backend._source_generation == 12
    assert [frame.source_generation for frame in published] == [12, 12]
    assert all(frame.capture_api == "MSMF" for frame in published)
    assert backend._cap is None
    assert backend._reopen_grace_ns == 0


def test_delivered_frame_carries_actual_capture_route(monkeypatch):
    """The per-frame proof must change with the live API, not the configured preference."""
    b = CaptureCardBackend(device_index=4)
    b._min_width = 4
    b._min_height = 4
    b._aspect_ratio = 1.0
    b._api_name = "MSMF"

    class _Cap:
        def read(self):
            return True, np.zeros((4, 4, 3), dtype=np.uint8)

        def release(self):
            pass

    b._cap = _Cap()
    b._stop_evt.clear()
    captured = []

    def capture_once(fd):
        captured.append(fd)
        b._stop_evt.set()

    monkeypatch.setattr(b._ring, "put", capture_once)
    b._run()

    assert len(captured) == 1
    assert captured[0].capture_api == "MSMF"
    assert captured[0].capture_device_index == 4
    assert not b.warm_cache_route_matches(4)


def test_stop_never_releases_while_reader_blocked_in_read():
    """Bughunt #7: stop() must NOT release the cv2 handle from the caller's thread while the
    reader may be blocked inside cap.read() — an MSMF/DSHOW release-during-read can
    access-violate and take down the sidecar in exactly the wedge-recovery path stop() serves.
    A wedged reader is ABANDONED (handle detached + kept referenced); the release happens on
    the READER thread if/when the read returns, else at process exit."""
    import threading
    import time

    b = CaptureCardBackend(device_index=0)
    b._STOP_JOIN_S = 0.2                    # keep the wedged-join wait short for the test
    release_threads = []
    unwedge = threading.Event()

    class _WedgedCap:
        def read(self):
            unwedge.wait(timeout=10.0)      # simulate read() blocking forever (device wedged)
            return False, None

        def release(self):
            release_threads.append(threading.current_thread().name)

    wedged = _WedgedCap()
    b._cap = wedged
    b._stop_evt.clear()
    th = threading.Thread(target=b._run, name="CaptureCardReader", daemon=True)
    b._thread = th
    th.start()
    time.sleep(0.05)                        # let the reader block inside read()

    b.stop()
    assert release_threads == [], "stop() must never release a handle the reader is blocked in"
    assert b._cap is None, "the wedged handle must be detached so a re-open never touches it"
    assert b._wedged_cap is wedged, "the wedged handle must stay referenced (no GC finalizer)"
    assert th.is_alive(), "the wedged reader is abandoned, not force-killed"

    # when the wedge clears, the READER thread itself performs the (safe) release
    unwedge.set()
    th.join(timeout=2.0)
    assert not th.is_alive()
    assert release_threads == ["CaptureCardReader"], "release must run on the reader thread"


def test_stop_releases_via_reader_thread_when_healthy():
    """Normal teardown: the reader wakes on _stop_evt within one read and releases its own
    handle (on the reader thread); stop() returns with the handle closed exactly once."""
    import threading
    import time

    b = CaptureCardBackend(device_index=0)
    release_threads = []

    class _Cap:
        def read(self):
            time.sleep(0.005)
            return True, np.zeros((4, 4, 3), dtype=np.uint8)

        def release(self):
            release_threads.append(threading.current_thread().name)

    b._cap = _Cap()
    b._stop_evt.clear()
    th = threading.Thread(target=b._run, name="CaptureCardReader", daemon=True)
    b._thread = th
    th.start()
    time.sleep(0.05)

    b.stop()
    assert not th.is_alive(), "a healthy reader must exit promptly on stop()"
    assert release_threads == ["CaptureCardReader"], "exactly one release, on the reader thread"
    assert b._cap is None


def test_bad_read_limit_matches_docs():
    """is_healthy()'s docstring documents the _run() give-up threshold; keep them in sync."""
    assert CaptureCardBackend._BAD_READ_LIMIT == 120
    assert "_BAD_READ_LIMIT" in CaptureCardBackend.is_healthy.__doc__


def test_is_healthy_stall_clock_detects_wedged_reader():
    """2026-07-04 live: a wedged MSMF device BLOCKS read() forever — the reader thread stays
    alive with zero new frames, and the old thread-aliveness-only is_healthy() reported healthy
    for 17.5s until the native watchdog nuked the whole sidecar. The frame-freshness clock must
    report unhealthy after _STALL_S so the orchestrator's in-process re-open fires instead."""
    import threading
    import time as _t

    b = CaptureCardBackend(device_index=0)
    # simulate a running-but-wedged reader: alive thread, started long ago, frames long stale
    evt = threading.Event()
    th = threading.Thread(target=evt.wait, daemon=True)
    th.start()
    try:
        b._thread = th
        now = _t.perf_counter_ns()
        b._started_ns = now - int(60e9)

        # fresh frame just arrived -> healthy
        b._last_put_ns = now
        assert b.is_healthy()

        # frames stale beyond the stall threshold -> unhealthy (thread still alive!)
        b._last_put_ns = now - int((b._STALL_S + 0.5) * 1e9)
        assert not b.is_healthy()

        # never produced a frame at all: healthy inside the startup grace, unhealthy after it
        b._last_put_ns = 0
        b._started_ns = now - int(2e9)
        assert b.is_healthy()
        b._started_ns = now - int((b._STARTUP_GRACE_S + 1.0) * 1e9)
        assert not b.is_healthy()

        # dead thread -> unhealthy regardless of freshness
        b._last_put_ns = now
        evt.set()
        th.join(timeout=2.0)
        assert not b.is_healthy()
    finally:
        evt.set()


def test_is_healthy_detects_sustained_severe_cadence_collapse(monkeypatch):
    """Changing frames at 2fps are not a healthy nominal-60fps feed. They must
    take the same in-process recovery path as a total stall, while a legitimate
    30fps feed and the startup grace remain healthy."""
    import threading
    import time as _t

    monkeypatch.setenv("ORION_CAPTURE_HEALTH_WINDOW_S", "5")
    monkeypatch.setenv("ORION_CAPTURE_HEALTH_GRACE_S", "10")
    monkeypatch.setenv("ORION_CAPTURE_MIN_HEALTH_FPS", "20")
    b = CaptureCardBackend(device_index=0, fps=60)
    evt = threading.Event()
    th = threading.Thread(target=evt.wait, daemon=True)
    th.start()
    try:
        b._thread = th
        now = _t.perf_counter_ns()
        b._started_ns = now - int(60e9)
        b._last_put_ns = now

        with b._arrival_lock:
            b._arrival_ns.clear()
            b._arrival_ns.extend(now - int(i * 0.5e9) for i in reversed(range(11)))
        assert not b.is_healthy()

        with b._arrival_lock:
            b._arrival_ns.clear()
            b._arrival_ns.extend(
                now - int(i * (1e9 / 30.0)) for i in reversed(range(151)))
        assert b.is_healthy()

        b._started_ns = now - int(2e9)
        with b._arrival_lock:
            b._arrival_ns.clear()
            b._arrival_ns.extend((now - int(1e9), now))
        assert b.is_healthy()
    finally:
        evt.set()
        th.join(timeout=2.0)


def test_cadence_stats_attribute_raw_reader_gap_without_hot_path_logging(monkeypatch):
    b = CaptureCardBackend(device_index=0, fps=60)
    now = 10_000_000_000
    monkeypatch.setattr(
        "capture_card_backend.time.perf_counter_ns", lambda: now)
    with b._arrival_lock:
        b._arrival_ns.clear()
        b._arrival_ns.extend((
            now - 100_000_000,
            now - 83_333_333,
            now - 50_000_000,  # one two-period raw reader gap
            now - 33_333_333,
            now - 16_666_667,
            now,
        ))

    stats = b.cadence_stats(window_s=1.0)

    assert stats["samples"] == 6
    assert 49.9 < stats["fps"] < 50.1
    assert 33.3 < stats["max_gap_ms"] < 33.4
    assert stats["late_gaps"] == 1
    assert stats["worst_gap_frame_number"] == 0
    assert stats["worst_gap_event_ns"] == 0
    assert stats["read_block_ms"] == 0.0
    assert stats["isolate_ms"] == 0.0
    assert stats["post_ms"] == 0.0


@pytest.mark.parametrize("delayed_stage", ("read", "isolate", "post"))
def test_reader_worst_gap_attributes_fake_cap_stage_delay(monkeypatch, delayed_stage):
    """The publication-gap proof must identify where time was spent without
    weakening the immutable-frame boundary or logging from the reader thread.

    A deterministic clock makes each case exact: frame 1 is 1+1+1 ms; frame 2
    injects 30 ms into only cap.read, isolation, or ring publication.
    """
    import capture_card_backend as ccb

    class _Clock:
        def __init__(self):
            self.ns = 10_000_000_000
            self.epoch_base_ns = 1_750_000_000_000_000_000

        def advance_ms(self, value):
            self.ns += int(float(value) * 1e6)

        def perf_counter_ns(self):
            return self.ns

        def time_ns(self):
            return self.epoch_base_ns + self.ns

    clock = _Clock()
    monkeypatch.setattr(ccb.time, "perf_counter_ns", clock.perf_counter_ns)
    monkeypatch.setattr(ccb.time, "time_ns", clock.time_ns)

    backend = CaptureCardBackend(device_index=0, fps=60)
    backend._min_width = 4
    backend._min_height = 4
    backend._aspect_ratio = 1.0
    backend._pts_lock = False
    backend._hw_pts_decided = True
    backend._stall_reopen = False
    source_frame = np.zeros((4, 4, 3), dtype=np.uint8)

    class _FakeCap:
        def __init__(self):
            self.reads = 0
            self.released = False

        def read(self):
            self.reads += 1
            clock.advance_ms(30 if self.reads == 2 and delayed_stage == "read" else 1)
            return True, source_frame

        def release(self):
            self.released = True

    cap = _FakeCap()
    backend._cap = cap
    backend._stop_evt.clear()

    isolation_calls = {"count": 0}

    def _fake_isolate(frame, verify_copy=True):
        isolation_calls["count"] += 1
        clock.advance_ms(
            30 if isolation_calls["count"] == 2 and delayed_stage == "isolate" else 1)
        owned = np.array(frame, dtype=np.uint8, order="C", copy=True)
        owned.setflags(write=False)
        return owned, ""

    published = []

    def _fake_put(frame_data):
        published.append(frame_data)
        clock.advance_ms(30 if len(published) == 2 and delayed_stage == "post" else 1)
        if len(published) == 2:
            backend._stop_evt.set()

    warnings = []
    monkeypatch.setattr(ccb, "_isolate_immutable_frame", _fake_isolate)
    monkeypatch.setattr(backend._ring, "put", _fake_put)
    monkeypatch.setattr(ccb.logger, "warning", lambda *args, **kwargs: warnings.append(args))

    backend._run()
    stats = backend.cadence_stats(window_s=1.0)

    assert cap.released
    assert len(published) == 2
    second = published[1]
    # The capture clock is immutable at read return. Copy/validation work and
    # ring/callback delay increase age; neither may replace it with a later stamp.
    assert second.timestamp_ns == second.capture_timestamp_ns
    assert second.epoch_ns == second.capture_epoch_ns
    assert second.publication_timestamp_ns >= second.capture_timestamp_ns
    expected_pre_publish_ms = 30.0 if delayed_stage == "isolate" else 1.0
    assert (second.publication_timestamp_ns - second.capture_timestamp_ns) / 1e6 \
        == pytest.approx(expected_pre_publish_ms)
    expected_age_ms = 31.0 if delayed_stage in ("isolate", "post") else 2.0
    assert (clock.ns - second.capture_timestamp_ns) / 1e6 \
        == pytest.approx(expected_age_ms)
    assert warnings == []
    assert stats["samples"] == 2
    assert stats["worst_gap_frame_number"] == 2
    assert stats["worst_gap_event_ns"] == clock.epoch_base_ns + clock.ns
    assert stats["max_gap_ms"] == pytest.approx(32.0)
    assert stats["late_gaps"] == 1
    assert stats["read_block_ms"] == pytest.approx(
        30.0 if delayed_stage == "read" else 1.0)
    assert stats["isolate_ms"] == pytest.approx(
        30.0 if delayed_stage == "isolate" else 1.0)
    assert stats["post_ms"] == pytest.approx(
        30.0 if delayed_stage == "post" else 1.0)
