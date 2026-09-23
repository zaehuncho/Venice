"""Warm-preview -> stream promotion (the `start_stream` stdin command) coverage.

Proves the missing Python half of the warm-sidecar Connect fix:
  1. orchestrator.promote_to_stream() brings up the SAME Chiaki/input path the cold connect uses
     (via _launch_remote_play_client), with the launcher mocked, WITHOUT touching capture/detector.
  2. promotion is idempotent — a second call never double-launches Chiaki.
  3. the sidecar `start_stream` handler routes console_ip into promote_to_stream and emits
     {"event":"started"} so the native transitions cleanly before its 2500ms fallback.
"""
import importlib.util
import os

import pytest


@pytest.fixture(autouse=True)
def _no_console_egress(monkeypatch):
    import remote_play_client
    # The warm-preview tests exercise scheduling, not live console discovery.
    monkeypatch.setattr(remote_play_client, "_console_host_lookup", lambda host: host)


def _load_sidecar():
    sc = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "native_orion", "backend", "autogreen_sidecar.py")
    spec = importlib.util.spec_from_file_location("_autogreen_sidecar_startstream_ut", sc)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_orch(monkeypatch):
    # Warm-preview sidecar config: capture-card source, no auto-launch (preview didn't start Chiaki).
    monkeypatch.setenv("ORION_CAPTURE_CARD", "1")
    from remote_play_orchestrator import RemotePlayOrchestrator, OrchestratorConfig
    cfg = OrchestratorConfig(console_ip="", virtual_controller=False, hidhide=False,
                             auto_launch_client=False, frame_source="capture_card")
    return RemotePlayOrchestrator(cfg)


class _FakeStatus:
    def __init__(self, ok=True):
        self.ok = ok
        self.window_title = "Chiaki"
        self.hwnd = 1234
        self.message = "" if ok else "launch failed"
        self.session_ready = ok


class _FakeManager:
    """Stands in for RemotePlayClientManager — records that ensure_running (the Chiaki/input
    bring-up) was invoked, so we can assert the promotion reached the real launch path."""
    instances = []

    def __init__(self, config):
        self.config = config
        self.ensure_calls = 0
        self.stopped = False
        self.running = True
        self.ready = True
        _FakeManager.instances.append(self)

    def ensure_running(self):
        self.ensure_calls += 1
        return _FakeStatus(ok=True)

    def stop(self):
        self.stopped = True
        self.running = False
        self.ready = False

    def is_running(self):
        return self.running

    def is_session_ready(self):
        return self.ready


@pytest.fixture(autouse=True)
def _reset_fake():
    _FakeManager.instances = []
    yield


def _patch_launcher(monkeypatch):
    import remote_play_orchestrator as rpo
    monkeypatch.setattr(rpo, "CLIENT_AVAILABLE", True, raising=False)
    monkeypatch.setattr(rpo, "RemotePlayClientManager", _FakeManager, raising=False)
    # RemotePlayClientConfig just needs to accept the kwargs the orchestrator passes.
    monkeypatch.setattr(rpo, "RemotePlayClientConfig",
                        lambda **kw: kw, raising=False)


def test_promote_to_stream_launches_chiaki_input(monkeypatch):
    orch = _build_orch(monkeypatch)
    _patch_launcher(monkeypatch)
    assert orch._client_manager is None                     # preview: no Chiaki yet

    ok = orch.promote_to_stream("1.2.3.4")

    assert ok is True
    assert orch.config.console_ip == "1.2.3.4"              # console_ip from the command applied
    assert len(_FakeManager.instances) == 1                 # exactly one Chiaki/input launch
    mgr = _FakeManager.instances[0]
    assert mgr.ensure_calls == 1                            # ensure_running() (the bring-up) invoked
    assert mgr.config["console_ip"] == "1.2.3.4"
    assert mgr.config["disable_video"] is True              # card is the sole video source
    assert orch._client_manager is mgr


def test_promote_to_stream_is_idempotent(monkeypatch):
    orch = _build_orch(monkeypatch)
    _patch_launcher(monkeypatch)

    assert orch.promote_to_stream("1.2.3.4") is True
    # Second promote (e.g. a duplicate start_stream) must NOT spin up a second Chiaki.
    assert orch.promote_to_stream("1.2.3.4") is True
    assert len(_FakeManager.instances) == 1                 # still one launch only
    assert _FakeManager.instances[0].ensure_calls == 1


def test_promote_replaces_stale_manager_whose_child_exited(monkeypatch):
    orch = _build_orch(monkeypatch)
    _patch_launcher(monkeypatch)

    class _DeadManager:
        stopped = False

        @staticmethod
        def is_running():
            return False

        def stop(self):
            self.stopped = True

    dead = _DeadManager()
    orch._client_manager = dead

    assert orch.promote_to_stream("1.2.3.4") is True
    assert dead.stopped is True
    assert len(_FakeManager.instances) == 1
    assert orch._client_manager is _FakeManager.instances[0]


def test_promote_rejects_legacy_alive_manager_without_readiness_contract(monkeypatch):
    orch = _build_orch(monkeypatch)
    _patch_launcher(monkeypatch)

    class _LegacyAliveManager:
        stopped = False

        @staticmethod
        def is_running():
            return True

        def stop(self):
            self.stopped = True

    legacy = _LegacyAliveManager()
    orch._client_manager = legacy

    assert orch.promote_to_stream("1.2.3.4") is True
    assert legacy.stopped is True
    assert len(_FakeManager.instances) == 1
    assert orch._client_manager is _FakeManager.instances[0]


def test_input_only_recovery_preserves_capture_and_detector(monkeypatch):
    orch = _build_orch(monkeypatch)
    _patch_launcher(monkeypatch)
    old = _FakeManager({})
    orch._client_manager = old
    detector_before = orch._meter_detector
    capture_before = object()
    processing_before = object()
    orch._capture_thread = capture_before
    orch._thread = processing_before
    orch._running = True

    assert orch.recover_input_link() is True
    assert old.stopped is True
    assert orch._client_manager is not old
    assert orch._meter_detector is detector_before
    assert orch._capture_thread is capture_before
    assert orch._thread is processing_before
    assert orch._running is True


def test_input_only_recovery_retries_transient_session_handoff_failures(monkeypatch):
    orch = _build_orch(monkeypatch)
    detector_before = orch._meter_detector
    capture_before = object()
    processing_before = object()
    orch._capture_thread = capture_before
    orch._thread = processing_before
    orch._running = True
    sleeps = []

    class _RetryManager:
        instances = []

        def __init__(self, config):
            self.config = config
            self.ready = len(self.instances) >= 2
            self.stopped = False
            self._failed_start_reaped = False
            self.instances.append(self)

        def ensure_running(self):
            return _FakeStatus(ok=self.ready)

        def is_session_ready(self):
            return self.ready

        @staticmethod
        def is_running():
            return True

        def stop(self):
            self.stopped = True

    import remote_play_orchestrator as rpo
    monkeypatch.setattr(rpo, "CLIENT_AVAILABLE", True, raising=False)
    monkeypatch.setattr(rpo, "RemotePlayClientManager", _RetryManager, raising=False)
    monkeypatch.setattr(rpo, "RemotePlayClientConfig", lambda **kw: kw, raising=False)
    monkeypatch.setattr(rpo.time, "sleep", lambda delay: sleeps.append(delay))

    assert orch.recover_input_link() is True
    assert len(_RetryManager.instances) == 3
    assert sleeps == [0.75, 1.25]
    assert [m.config["wait_timeout_s"] for m in _RetryManager.instances] == [3.0, 9.0, 3.0]
    assert all(m.stopped for m in _RetryManager.instances[:2])
    assert orch._client_manager is _RetryManager.instances[-1]
    assert orch.input_link_ready() is True
    assert orch._meter_detector is detector_before
    assert orch._capture_thread is capture_before
    assert orch._thread is processing_before
    assert orch._running is True


def test_input_only_recovery_exhaustion_stays_fail_closed_with_live_capture(monkeypatch):
    orch = _build_orch(monkeypatch)
    detector_before = orch._meter_detector
    capture_before = object()
    processing_before = object()
    orch._capture_thread = capture_before
    orch._thread = processing_before
    orch._running = True

    class _NeverReadyManager:
        instances = []

        def __init__(self, config):
            self.config = config
            self._failed_start_reaped = False
            self.stopped = False
            self.instances.append(self)

        @staticmethod
        def ensure_running():
            return _FakeStatus(ok=False)

        @staticmethod
        def is_session_ready():
            return False

        def stop(self):
            self.stopped = True

    import remote_play_orchestrator as rpo
    monkeypatch.setattr(rpo, "CLIENT_AVAILABLE", True, raising=False)
    monkeypatch.setattr(rpo, "RemotePlayClientManager", _NeverReadyManager, raising=False)
    monkeypatch.setattr(rpo, "RemotePlayClientConfig", lambda **kw: kw, raising=False)
    monkeypatch.setattr(rpo.time, "sleep", lambda _delay: None)

    assert orch.recover_input_link() is False
    assert len(_NeverReadyManager.instances) == 3
    assert all(m.stopped for m in _NeverReadyManager.instances)
    assert orch._client_manager is None
    assert orch.input_link_ready() is False
    assert orch._meter_detector is detector_before
    assert orch._capture_thread is capture_before
    assert orch._thread is processing_before
    assert orch._running is True


def test_promote_does_not_touch_capture_or_detector(monkeypatch):
    orch = _build_orch(monkeypatch)
    _patch_launcher(monkeypatch)
    detector_before = orch._meter_detector
    # Stand up sentinels for the capture/detector threads; the promotion must leave them intact.
    orch._running = True
    sentinel_capture = object()
    sentinel_proc = object()
    orch._capture_thread = sentinel_capture
    orch._thread = sentinel_proc

    orch.promote_to_stream("1.2.3.4")

    assert orch._meter_detector is detector_before          # detector untouched
    assert orch._capture_thread is sentinel_capture         # capture thread not restarted
    assert orch._thread is sentinel_proc                    # processing loop not restarted
    assert orch._running is True                             # never stopped


def test_failed_promotion_reaps_input_manager_but_preserves_capture(monkeypatch):
    orch = _build_orch(monkeypatch)
    detector_before = orch._meter_detector
    capture_before = object()
    processing_before = object()
    backend_before = object()
    orch._capture_thread = capture_before
    orch._thread = processing_before
    orch._frame_backend = backend_before
    orch._running = True

    class _FailedManager:
        instances = []

        def __init__(self, _config):
            self.stopped = False
            self._failed_start_reaped = False
            _FailedManager.instances.append(self)

        def ensure_running(self):
            return _FakeStatus(ok=False)

        @staticmethod
        def is_session_ready():
            return False

        def stop(self):
            self.stopped = True

    import remote_play_orchestrator as rpo
    monkeypatch.setattr(rpo, "CLIENT_AVAILABLE", True, raising=False)
    monkeypatch.setattr(rpo, "RemotePlayClientManager", _FailedManager, raising=False)
    monkeypatch.setattr(rpo, "RemotePlayClientConfig", lambda **kw: kw, raising=False)

    assert orch.promote_to_stream("1.2.3.4") is False
    assert len(_FailedManager.instances) == 1
    assert _FailedManager.instances[0].stopped is True
    assert orch._client_manager is None
    assert orch._input_link_ready is False
    assert orch._meter_detector is detector_before
    assert orch._capture_thread is capture_before
    assert orch._thread is processing_before
    assert orch._frame_backend is backend_before
    assert orch._running is True


def test_failed_promotion_does_not_run_slow_cleanup_twice_after_child_reaped(monkeypatch):
    orch = _build_orch(monkeypatch)

    class _AlreadyReapedManager:
        instances = []

        def __init__(self, _config):
            self._failed_start_reaped = True
            self.stop_calls = 0
            _AlreadyReapedManager.instances.append(self)

        def ensure_running(self):
            return _FakeStatus(ok=False)

        @staticmethod
        def is_session_ready():
            return False

        def stop(self):
            self.stop_calls += 1

    import remote_play_orchestrator as rpo
    monkeypatch.setattr(rpo, "CLIENT_AVAILABLE", True, raising=False)
    monkeypatch.setattr(rpo, "RemotePlayClientManager", _AlreadyReapedManager, raising=False)
    monkeypatch.setattr(rpo, "RemotePlayClientConfig", lambda **kw: kw, raising=False)

    assert orch.promote_to_stream("1.2.3.4") is False
    assert _AlreadyReapedManager.instances[0].stop_calls == 0
    assert orch._client_manager is None


def test_cold_start_fails_when_console_input_session_does_not_become_ready(monkeypatch):
    orch = _build_orch(monkeypatch)
    orch.config.auto_launch_client = True
    monkeypatch.setattr(orch, "_launch_remote_play_client", lambda: False)

    class _Backend:
        stopped = False

        def stop(self):
            self.stopped = True

    backend = _Backend()
    orch._frame_backend = backend

    assert orch.start() is False
    assert backend.stopped is True
    assert orch._frame_backend is None
    assert orch._running is False


def test_sidecar_start_stream_cmd_promotes_and_emits_started(monkeypatch):
    """Feeding {"cmd":"start_stream","console_ip":...} through the sidecar handler triggers the
    Chiaki/input bring-up (mocked) and emits {"event":"started"}, capture/detector left alone."""
    mod = _load_sidecar()

    emitted = []
    monkeypatch.setattr(mod, "_emit", lambda payload: emitted.append(payload))

    calls = {"ip": None, "n": 0}

    class _FakeOrch:
        def promote_to_stream(self, console_ip=None):
            calls["ip"] = console_ip
            calls["n"] += 1
            return True

        @staticmethod
        def input_link_ready():
            return True

    ok = mod._handle_start_stream(_FakeOrch(), {"cmd": "start_stream", "console_ip": "1.2.3.4"})

    assert ok is True
    assert calls["n"] == 1 and calls["ip"] == "1.2.3.4"     # promotion invoked with the IP
    started = [e for e in emitted if e.get("event") == "started"]
    assert len(started) == 1                                # native gets its transition signal
    assert started[0]["input_ready"] is True


def test_sidecar_rejects_legacy_success_without_explicit_session_readiness(monkeypatch):
    mod = _load_sidecar()
    emitted = []
    monkeypatch.setattr(mod, "_emit", lambda payload: emitted.append(payload))

    class _LegacyOrch:
        @staticmethod
        def promote_to_stream(_console_ip=None):
            return True

    assert mod._handle_start_stream(_LegacyOrch(), {"console_ip": "1.2.3.4"}) is False
    assert not [e for e in emitted if e.get("event") == "started"]
    assert [e for e in emitted if e.get("event") == "error"]


def test_capture_card_live_process_without_console_session_never_emits_started(monkeypatch):
    mod = _load_sidecar()
    emitted = []
    monkeypatch.setattr(mod, "_emit", lambda payload: emitted.append(payload))

    class _Status:
        pid = 44
        message = "no fresh console-session readiness marker was observed"

    class _Process:
        @staticmethod
        def poll():
            return None

    class _Manager:
        status = _Status()
        _process = _Process()

    class _Orch:
        _cc_mode = True
        _client_manager = _Manager()

        @staticmethod
        def promote_to_stream(_console_ip=None):
            return False

    assert mod._handle_start_stream(_Orch(), {"console_ip": "1.2.3.4"}) is False
    assert not [e for e in emitted if e.get("event") == "started"]
    errors = [e for e in emitted if e.get("event") == "error"]
    assert errors
    assert errors[-1]["input_ready"] is False


def test_sidecar_start_stream_old_orch_no_started(monkeypatch):
    """An orchestrator build lacking promote_to_stream must not emit a bogus started event."""
    mod = _load_sidecar()
    emitted = []
    monkeypatch.setattr(mod, "_emit", lambda payload: emitted.append(payload))

    class _OldOrch:
        pass

    ok = mod._handle_start_stream(_OldOrch(), {"console_ip": "1.2.3.4"})
    assert ok is False
    assert not [e for e in emitted if e.get("event") == "started"]
    errors = [e for e in emitted if e.get("event") == "error"]
    assert errors and errors[-1]["input_ready"] is False


def test_sidecar_input_recovery_reports_ready_without_restarting_orchestrator(monkeypatch):
    mod = _load_sidecar()
    emitted = []
    monkeypatch.setattr(mod, "_emit", lambda payload: emitted.append(payload))

    class _FakeOrch:
        calls = 0

        def recover_input_link(self):
            self.calls += 1
            return True

        @staticmethod
        def input_link_ready():
            return True

    orch = _FakeOrch()
    assert mod._handle_recover_input(orch) is True
    assert orch.calls == 1
    assert [e.get("state") for e in emitted if e.get("event") == "input_recovery"] == [
        "begin", "ready"
    ]


def test_sidecar_input_recovery_cannot_rearm_without_new_ready_marker(monkeypatch):
    mod = _load_sidecar()
    emitted = []
    monkeypatch.setattr(mod, "_emit", lambda payload: emitted.append(payload))

    class _FakeOrch:
        @staticmethod
        def recover_input_link():
            return True

        @staticmethod
        def input_link_ready():
            return False

    assert mod._handle_recover_input(_FakeOrch()) is False
    states = [e.get("state") for e in emitted if e.get("event") == "input_recovery"]
    assert states == ["begin", "error"]
    assert "ready" not in states
    assert emitted[-1]["input_ready"] is False


def test_start_stream_ack_does_not_delay_spawn_and_precedes_verdict(monkeypatch):
    """2026-08-29: the `begin` ack is emitted OFF-THREAD so a contended _emit_lock (the
    preview writer can hold it >1s) no longer delays the Chiaki spawn; the ack threads are
    joined before the verdict so begin/waking still precede started on the wire, and a
    rest-mode wake reported by the client surfaces as a `waking` event with its budget."""
    import threading, time
    sc = _load_sidecar()
    events = []
    def slow_emit(payload):
        if payload.get("event") == "stream_promote" and payload.get("state") == "begin":
            time.sleep(0.3)                     # simulate the lock held by the preview writer
        events.append(dict(payload))
    monkeypatch.setattr(sc, "_emit", slow_emit)
    monkeypatch.setattr(sc, "_log", lambda *a, **k: None)

    class _Orch:
        def __init__(self):
            self.promote_called_at = None
            self.console_waking_callback = None
        def promote_to_stream(self, ip=None, identity=None):
            self.promote_called_at = time.perf_counter()
            if callable(self.console_waking_callback):
                self.console_waking_callback(25.0)   # client found the console resting
            return True
        def input_link_ready(self):
            return True

    orch = _Orch()
    t0 = time.perf_counter()
    assert sc._handle_start_stream(orch, {"console_ip": "1.2.3.4"}) is True
    assert orch.promote_called_at is not None
    assert orch.promote_called_at - t0 < 0.15          # spawn NOT blocked behind the 0.3s ack
    names = [f"{e.get('event')}:{e.get('state', '')}" for e in events]
    assert names.index("stream_promote:begin") < names.index("started:")
    assert names.index("stream_promote:waking") < names.index("started:")
    waking = [e for e in events if e.get("state") == "waking"][0]
    assert waking["budget_ms"] == 25000 and waking["console_ip"] == "1.2.3.4"


def test_prewarm_standby_spawns_during_preview_and_stops_with_orchestrator(monkeypatch):
    """[ORION_STANDBY] The warm-preview sidecar asks the orchestrator to pre-boot the
    Remote Play client; the prewarm loop must pass the connect-identical parameters
    (console ip, capture-card input-only route, executable identity) to the pool and
    exit when the orchestrator stops."""
    import time
    import remote_play_orchestrator as rpo

    orch = _build_orch(monkeypatch)
    calls = []

    class _FakePool:
        def ensure_spawned(self, **kwargs):
            calls.append(kwargs)
            return "spawned"

    monkeypatch.setattr(rpo, "get_standby_pool", lambda: _FakePool())
    monkeypatch.setattr(rpo, "standby_client_enabled", lambda: True)
    orch._running = True
    orch._client_manager = None
    orch.config.console_ip = "5.6.7.8"

    orch.prewarm_standby_client()
    deadline = time.time() + 3.0
    while not calls and time.time() < deadline:
        time.sleep(0.02)
    orch._running = False
    thread = orch._standby_prewarm_thread
    thread.join(timeout=3.0)

    assert calls, "prewarm never attempted a standby spawn"
    kwargs = calls[0]
    assert kwargs["console_ip"] == "5.6.7.8"
    # Capture-card preview => the promoted chiaki is INPUT-ONLY, so the standby
    # must be spawned with the identical video-disable route.
    assert kwargs["disable_video"] is True
    assert not thread.is_alive()


def test_prewarm_standby_idles_while_a_client_manager_exists(monkeypatch):
    import time
    import remote_play_orchestrator as rpo

    orch = _build_orch(monkeypatch)
    calls = []

    class _FakePool:
        def ensure_spawned(self, **kwargs):
            calls.append(kwargs)
            return "spawned"

    monkeypatch.setattr(rpo, "get_standby_pool", lambda: _FakePool())
    monkeypatch.setattr(rpo, "standby_client_enabled", lambda: True)
    orch._running = True
    orch._client_manager = object()  # live/attempted session owns the client

    orch.prewarm_standby_client()
    time.sleep(0.3)
    orch._running = False
    orch._standby_prewarm_thread.join(timeout=3.0)

    assert calls == []
