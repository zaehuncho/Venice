"""Deterministic shutdown interleavings; all process and transport operations mocked."""
from types import SimpleNamespace
import threading
import pytest
import remote_play_client as rpc
import remote_play_orchestrator as rpo


def bare():
    orch = rpo.RemotePlayOrchestrator.__new__(rpo.RemotePlayOrchestrator)
    orch._running = True
    orch._client_manager = None
    orch._input_link_ready = False
    orch._input_link_checked_at = 0.0
    orch._cc_mode = True
    orch._last_error_msg = ''
    orch.config = SimpleNamespace(platform='ps5', client_mode='chiaki', console_ip='',
        window_title='', chiaki_path='', chiaki_identity_size=-1, chiaki_identity_sha256='',
        close_client_on_disconnect=True, wait_timeout_s=1.0)
    return orch


class FakeManager:
    def __init__(self, config=None):
        self.stops = 0
    def stop(self):
        self.stops += 1
    def is_running(self):
        return not self.stops
    def is_session_ready(self):
        return not self.stops
    def ensure_running(self):
        return rpc.RemotePlayClientStatus(ok=True, session_ready=True)


def test_recovery_stop_during_backoff_does_not_relaunch(monkeypatch):
    orch = bare()
    calls = []
    orch._launch_remote_play_client = lambda **kw: calls.append(kw) or False
    monkeypatch.setattr(rpo.time, 'sleep', lambda _: orch.close_remote_play_client())
    assert not orch.recover_input_link()
    assert len(calls) == 1


def test_launch_begun_after_close_does_not_construct_client(monkeypatch):
    orch = bare()
    constructed = []
    monkeypatch.setattr(rpo, 'CLIENT_AVAILABLE', True)
    monkeypatch.setattr(rpo, 'RemotePlayClientManager', lambda cfg: constructed.append(cfg) or FakeManager())
    orch.close_remote_play_client()
    assert not orch._launch_remote_play_client()
    assert not constructed
    assert orch._client_manager is None


def test_completion_cannot_borrow_new_manager_readiness(monkeypatch):
    orch = bare()
    newer = FakeManager()
    class Replaced(FakeManager):
        def ensure_running(self):
            orch.close_remote_play_client()
            # Simulate a newer intent occupying the slot before this old call returns.
            orch._client_manager = newer
            return super().ensure_running()
    monkeypatch.setattr(rpo, 'CLIENT_AVAILABLE', True)
    monkeypatch.setattr(rpo, 'RemotePlayClientManager', Replaced)
    assert not orch._launch_remote_play_client()
    assert orch._client_manager is newer
    assert newer.stops == 0
    assert not orch._input_link_ready


def test_client_stop_during_preflight_never_spawns(monkeypatch):
    manager = rpc.RemotePlayClientManager(rpc.RemotePlayClientConfig(close_on_stop=True))
    spawned = []
    monkeypatch.setattr(rpc, 'terminate_chiaki_processes', lambda: None)
    monkeypatch.setattr(rpc, 'canonical_executable_path', lambda p: p)
    monkeypatch.setattr(rpc, 'windows_process_creation_time_100ns', lambda _: 123)
    monkeypatch.setattr(rpc, 'optimize_chiaki_process', lambda _: None)
    def env(**kw):
        manager.stop()
        return {}
    monkeypatch.setattr(rpc, 'chiaki_controller_env', env)
    monkeypatch.setattr(rpc.subprocess, 'Popen', lambda *a, **kw: spawned.append(a) or SimpleNamespace(pid=123))
    status = manager._launch_process(['fixture'], 'chiaki', 'fixture.exe')
    assert not spawned
    assert not status.ok
    assert manager._process is None


def test_stop_revokes_even_a_nonclosing_manager(monkeypatch):
    manager = rpc.RemotePlayClientManager(rpc.RemotePlayClientConfig(close_on_stop=False))
    manager._status = rpc.RemotePlayClientStatus(ok=True, session_ready=True, hwnd=1)
    launches = []
    monkeypatch.setattr(rpc, 'standby_client_enabled', lambda: False)
    monkeypatch.setattr(rpc, 'find_remote_play_window', lambda *a: (0, ''))
    monkeypatch.setattr(manager, '_launch', lambda *a: launches.append(a) or rpc.RemotePlayClientStatus(ok=False))
    manager.stop()
    assert not manager.is_session_ready()
    assert not manager.ensure_running().ok
    assert not launches


def test_close_is_reentrant_without_double_stop():
    orch = bare()
    class Reentrant(FakeManager):
        def stop(self):
            self.stops += 1
            if self.stops == 1:
                orch.close_remote_play_client()
    manager = Reentrant()
    orch._client_manager = manager
    orch.close_remote_play_client()
    assert manager.stops == 1


def test_tracker_ready_returning_after_stop_cannot_restore_authority(monkeypatch):
    manager = rpc.RemotePlayClientManager(rpc.RemotePlayClientConfig(
        close_on_stop=False, require_session_ready=True))
    manager._status = rpc.RemotePlayClientStatus(ok=True, session_ready=True)
    monkeypatch.setattr(manager, 'is_running', lambda: True)
    def poll():
        manager.stop()
        return 'ready', 'fixture.log'
    monkeypatch.setattr(manager._session_tracker, 'poll', poll)
    assert not manager.is_session_ready()
    assert not manager.status.session_ready


def test_standby_resolve_returning_after_stop_never_sends_open(monkeypatch):
    manager = rpc.RemotePlayClientManager(rpc.RemotePlayClientConfig(
        close_on_stop=True, require_session_ready=True, console_ip='fixture-host'))
    child = SimpleNamespace(process=SimpleNamespace(pid=123, _handle=None), pipe_path='fixture-pipe')
    pool = SimpleNamespace(state=lambda: 'ready', claim=lambda **kw: child)
    opened, discarded = [], []
    monkeypatch.setattr(rpc, 'terminate_chiaki_processes', lambda: None)
    monkeypatch.setattr(rpc, 'get_standby_pool', lambda: pool)
    monkeypatch.setattr(rpc, 'find_chiaki_binary', lambda *a: 'fixture.exe')
    monkeypatch.setattr(rpc, '_probe_chiaki_client_cached', lambda *a: SimpleNamespace(client_ran=True, nickname='fixture'))
    monkeypatch.setattr(rpc, '_running_image_pids', lambda *a: [(123, 'fixture.exe')])
    monkeypatch.setattr(rpc, '_snapshot_session_logs', lambda *a: {})
    monkeypatch.setattr(rpc, 'canonical_executable_path', lambda p: p)
    monkeypatch.setattr(rpc, 'windows_process_creation_time_100ns', lambda *a: 123)
    monkeypatch.setattr(rpc, '_standby_pipe_transact', lambda *a, **kw: opened.append(a) or rpc._STANDBY_OPEN_OK_REPLY)
    monkeypatch.setattr(manager, '_ensure_console_awake', lambda *a: None)
    monkeypatch.setattr(manager, '_discard_claimed_standby', lambda *a: discarded.append(a))
    def resolve(host):
        manager.stop()
        return host
    monkeypatch.setattr(manager, '_resolve_console_host', resolve)
    status = manager._promote_standby({})
    assert status is not None and not status.ok
    assert not opened
    assert len(discarded) == 1
    assert manager._process is None


def test_orchestrator_ready_poll_returning_after_close_is_rejected():
    orch = bare()
    class LateReady(FakeManager):
        def is_session_ready(self):
            orch.close_remote_play_client()
            return True
    orch._client_manager = LateReady()
    assert not orch.input_link_ready()
    assert not orch._input_link_ready


def test_window_adoption_returning_after_stop_does_not_publish_ready(monkeypatch):
    manager = rpc.RemotePlayClientManager(rpc.RemotePlayClientConfig(
        close_on_stop=False, require_session_ready=False))
    monkeypatch.setattr(rpc, '_snapshot_session_logs', lambda *a: {})
    def scan(*args):
        manager.stop()
        return 123, 'fixture'
    monkeypatch.setattr(rpc, 'find_remote_play_window', scan)
    assert not manager.ensure_running().ok
    assert not manager.status.session_ready


def test_timed_out_standby_worker_does_not_write_when_open_finishes_late(monkeypatch):
    open_entered, allow_open, closed = threading.Event(), threading.Event(), threading.Event()
    writes = []
    class Pipe:
        def write(self, data):
            writes.append(data)
        def read(self, count):
            return b'\n'
        def close(self):
            closed.set()
    def delayed_open(*args, **kwargs):
        open_entered.set()
        assert allow_open.wait(5)
        return Pipe()
    monkeypatch.setattr(rpc, 'open', delayed_open, raising=False)
    try:
        assert rpc._standby_pipe_transact('fixture', 'open fixture', timeout_s=0.1) == ''
        assert open_entered.is_set()
    finally:
        allow_open.set()
    assert closed.wait(5)
    assert writes == []


def test_standby_reply_wait_does_not_block_stop_or_republish_child(monkeypatch):
    manager = rpc.RemotePlayClientManager(rpc.RemotePlayClientConfig(
        close_on_stop=True, require_session_ready=True, console_ip='fixture-host'))
    process = SimpleNamespace(pid=123, _handle=None, poll=lambda: 0)
    child = SimpleNamespace(process=process, pipe_path='fixture-pipe')
    pool = SimpleNamespace(state=lambda: 'ready', claim=lambda **kw: child)
    monkeypatch.setattr(rpc, 'terminate_chiaki_processes', lambda: None)
    monkeypatch.setattr(rpc, 'get_standby_pool', lambda: pool)
    monkeypatch.setattr(rpc, 'find_chiaki_binary', lambda *a: 'fixture.exe')
    monkeypatch.setattr(rpc, '_probe_chiaki_client_cached', lambda *a: SimpleNamespace(client_ran=True, nickname='fixture'))
    monkeypatch.setattr(rpc, '_running_image_pids', lambda *a: [(123, 'fixture.exe')])
    monkeypatch.setattr(rpc, '_snapshot_session_logs', lambda *a: {})
    monkeypatch.setattr(rpc, 'canonical_executable_path', lambda p: p)
    monkeypatch.setattr(rpc, 'windows_process_creation_time_100ns', lambda *a: 123)
    monkeypatch.setattr(manager, '_ensure_console_awake', lambda *a: None)
    monkeypatch.setattr(manager, '_resolve_console_host', lambda p: p)
    entered, finish_reply, stopped = threading.Event(), threading.Event(), threading.Event()
    def transact(*args, **kwargs):
        entered.set()
        assert finish_reply.wait(5)
        return rpc._STANDBY_OPEN_OK_REPLY
    monkeypatch.setattr(rpc, '_standby_pipe_transact', transact)
    result = []
    worker = threading.Thread(target=lambda: result.append(manager._promote_standby({})))
    stopper = threading.Thread(target=lambda: (manager.stop(), stopped.set()))
    worker.start()
    try:
        assert entered.wait(5)
        stopper.start()
        assert stopped.wait(2), 'stop must not wait for the standby reply timeout'
    finally:
        finish_reply.set()
        worker.join(5)
        if stopper.ident is not None:
            stopper.join(5)
    assert not worker.is_alive() and not stopper.is_alive()
    assert result and result[0] is not None and not result[0].ok
    assert manager._process is None
