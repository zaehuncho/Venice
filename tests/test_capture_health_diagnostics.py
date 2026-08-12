import threading

import remote_play_orchestrator as rpo


def _snapshot(backend, tier, suspect=False):
    return rpo._CaptureHealthSnapshot(
        backend=backend,
        tier=tier,
        tier_counts=((tier, 3),),
        unique_frame_fps=60,
        duplicate_frame_pct=2.0,
        cv_fps=58.0,
        cv_detect_ms=1.5,
        source_sequence_skips=7,
        preview_duplicate_refreshes=8,
        core_black_run=9,
        core_static_run=10,
        suspect=suspect,
    )


def test_capture_health_worker_is_off_loop_bounded_latest_wins_and_drains(monkeypatch):
    main_thread = threading.get_ident()
    first_query_entered = threading.Event()
    release_first_query = threading.Event()
    two_logs_emitted = threading.Event()
    query_threads = []
    rendered = []

    class Backend:
        def __init__(self):
            self.export_calls = 0

        def export_stats(self):
            query_threads.append(threading.get_ident())
            self.export_calls += 1
            if self.export_calls == 1:
                first_query_entered.set()
                assert release_first_query.wait(timeout=2.0)
            return 59.0, 1.0

        def cadence_stats(self, window_s):
            query_threads.append(threading.get_ident())
            assert window_s == 5.0
            return {
                'fps': 57.5,
                'max_gap_ms': 24.5,
                'late_gaps': 2,
                'worst_gap_frame_number': 321,
                'worst_gap_event_ns': 1_750_000_000_125_000_000,
                'read_block_ms': 16.25,
                'isolate_ms': 1.75,
                'post_ms': 6.5,
            }

    def record_warning(fmt, *args):
        rendered.append((threading.get_ident(), fmt % args))
        if len(rendered) >= 2:
            two_logs_emitted.set()

    monkeypatch.setattr(rpo.logger, 'warning', record_warning)
    backend = Backend()
    sink = rpo._LatestCaptureHealthSink(
        rpo.RemotePlayOrchestrator._emit_capture_health_diagnostic)
    sink.start()
    try:
        assert sink.submit(_snapshot(backend, 'first'))
        assert first_query_entered.wait(timeout=2.0)

        # The worker is blocked on the first backend query.  Only the newest of
        # these two pending reports may survive the one-slot handoff.
        assert sink.submit(_snapshot(backend, 'superseded'))
        assert sink.submit(_snapshot(backend, 'latest', suspect=True))
        assert sink.replaced == 1
        release_first_query.set()
        assert two_logs_emitted.wait(timeout=2.0)
    finally:
        release_first_query.set()
        assert sink.stop(timeout=2.0)

    assert not sink.is_running()
    assert len(rendered) == 2
    assert all(thread_id != main_thread for thread_id, _ in rendered)
    assert query_threads and all(thread_id != main_thread for thread_id in query_threads)
    assert 'tier=first' in rendered[0][1]
    assert all('tier=superseded' not in line for _, line in rendered)
    assert rendered[1][1] == (
        "Capture health: SUSPECT cap_mode=? tier=latest tiers={'latest': 3} "
        "uniqfps=60 dup%=2 "
        "export_fps=59 gap_fps=1 cv_fps=58 detect_ms=1.5 "
        "raw_fps=57.5 raw_gap_max_ms=24.5 raw_late=2 "
        "source_skip=7 raw_gap_frame=321 raw_gap_event_ms=1750000000125 "
        "raw_read_block_ms=16.25 raw_isolate_ms=1.75 raw_post_ms=6.50 "
        "preview_dup_refresh=8 "
        "core_black_run=9 core_static_run=10"
    )
    # The two fields worth paging on must survive RemotePlaySession's .left(300)
    # forward, which leaves 244 characters for the message body.  Pin their offsets
    # so a future field insertion cannot quietly push them back off the end again.
    assert rendered[1][1].index('SUSPECT') < 244
    assert rendered[1][1].index('cap_mode=') < 244


def test_capture_health_reports_the_driver_negotiated_mode(monkeypatch):
    """cap_mode must restate what the DRIVER gave us, not what we requested.

    capture_card_backend logs the negotiated mode once at open time via logger.info,
    but sidecar INFO only reaches orion_native.log in the shutdown tail -- so a live
    session could never confirm that ORION_CAPTURE_MJPG=0 / ORION_DETECTOR_1080P=1
    actually took.  Every capture-format A/B was faith-based until this field existed.
    """
    class Backend:
        def export_stats(self):
            return 59.0, 1.0

        def cadence_stats(self, window_s):
            return {'fps': 57.5}

        def negotiated_mode(self):
            return (1920, 1080, 60.0, 'YUY2', 1.0)

    rendered = []
    monkeypatch.setattr(rpo.logger, 'warning', lambda fmt, *a: rendered.append(fmt % a))
    rpo.RemotePlayOrchestrator._emit_capture_health_diagnostic(
        _snapshot(Backend(), 'latest'))

    assert len(rendered) == 1
    assert 'cap_mode=1920x1080@60/YUY2/buf1' in rendered[0]


def test_capture_health_never_lets_a_mode_probe_break_the_health_line(monkeypatch):
    """A backend without the accessor, or one that throws, must degrade to '?'.

    Diagnostics are observational: losing cap_mode is acceptable, losing the whole
    capture-health line (the tier/dup/fps evidence used to diagnose a degraded feed)
    is not.
    """
    class NoAccessor:
        def export_stats(self):
            return 0.0, 0.0

    class Throws(NoAccessor):
        def negotiated_mode(self):
            raise RuntimeError('driver went away mid-teardown')

    for backend in (NoAccessor(), Throws(), None):
        rendered = []
        monkeypatch.setattr(rpo.logger, 'warning', lambda fmt, *a: rendered.append(fmt % a))
        rpo.RemotePlayOrchestrator._emit_capture_health_diagnostic(
            _snapshot(backend, 'latest'))
        assert len(rendered) == 1
        assert 'cap_mode=? ' in rendered[0]
        assert 'core_black_run=9' in rendered[0]


def test_capture_loop_handoff_only_snapshots_primitives_and_never_queries_backend():
    class Backend:
        def export_stats(self):
            raise AssertionError('export_stats ran on the capture handoff')

        def cadence_stats(self, window_s):
            raise AssertionError('cadence_stats ran on the capture handoff')

    class RecordingSink:
        def __init__(self):
            self.snapshot = None

        @staticmethod
        def is_running():
            return True

        def submit(self, snapshot):
            self.snapshot = snapshot
            return True

    orch = rpo.RemotePlayOrchestrator.__new__(rpo.RemotePlayOrchestrator)
    backend = Backend()
    sink = RecordingSink()
    orch._capture_health_diagnostics = sink
    orch._frame_backend = backend
    orch._last_capture_tier = 'capture_card'
    orch._tier_counts = {'capture_card': 300}
    orch._unique_frame_fps = 60
    orch._duplicate_frame_pct = 0.0
    orch._cv_fps = 60.0
    orch._cv_detect_ms_ema = 1.25
    orch._source_sequence_skips = 0
    orch._preview_duplicate_refreshes = 4
    orch._video_core_black_run = 0
    orch._video_core_static_run = 0

    assert orch._queue_capture_health_diagnostic(False)
    sample = sink.snapshot
    assert sample.backend is backend
    assert sample.tier_counts == (('capture_card', 300),)
    assert sample.unique_frame_fps == 60
    assert sample.preview_duplicate_refreshes == 4


def test_capture_health_stop_drains_the_latest_pending_sample():
    emitted = []
    sink = rpo._LatestCaptureHealthSink(emitted.append)
    sink.start()
    assert sink.submit('final')
    assert sink.stop(timeout=2.0)
    assert emitted == ['final']


def test_capture_health_handoff_fails_isolated_from_frame_delivery():
    class FailingSink:
        @staticmethod
        def is_running():
            return True

        @staticmethod
        def submit(_snapshot):
            raise RuntimeError('diagnostics teardown race')

    orch = rpo.RemotePlayOrchestrator.__new__(rpo.RemotePlayOrchestrator)
    orch._capture_health_diagnostics = FailingSink()
    orch._frame_backend = None
    orch._last_capture_tier = 'capture_card'
    orch._tier_counts = {'capture_card': 1}
    orch._unique_frame_fps = 60
    orch._duplicate_frame_pct = 0.0
    orch._cv_fps = 60.0
    orch._cv_detect_ms_ema = 1.0
    orch._source_sequence_skips = 0
    orch._preview_duplicate_refreshes = 0
    orch._video_core_black_run = 0
    orch._video_core_static_run = 0

    assert orch._queue_capture_health_diagnostic(False) is False
