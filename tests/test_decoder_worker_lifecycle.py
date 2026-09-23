"""Frame-pipe lifecycle must not advertise a retired or failed reader as running."""
import sys
from types import SimpleNamespace
import pytest
import chiaki_backend as cb


class Worker:
    def __init__(self, *, alive=False, **kwargs):
        self.alive = alive
        self.started = False
    def is_alive(self):
        return self.alive
    def start(self):
        self.started = self.alive = True


def test_restart_during_retirement_reports_not_started(monkeypatch):
    monkeypatch.setitem(sys.modules, 'win32file', SimpleNamespace())
    backend = cb.OrionFramePipeBackend()
    old = Worker(alive=True)
    backend._thread = old
    backend._stop_evt.set()
    assert backend.start() is False
    assert backend._thread is old
    assert backend._stop_evt.is_set()
    assert not backend.is_ready()


def test_start_is_idempotent_for_running_worker(monkeypatch):
    monkeypatch.setitem(sys.modules, 'win32file', SimpleNamespace())
    backend = cb.OrionFramePipeBackend()
    old = Worker(alive=True)
    backend._thread = old
    assert backend.start() is True
    assert backend._thread is old
    assert not backend._stop_evt.is_set()


def test_restart_after_retirement_creates_new_worker(monkeypatch):
    monkeypatch.setitem(sys.modules, 'win32file', SimpleNamespace())
    monkeypatch.setattr(cb.threading, 'Thread', Worker)
    backend = cb.OrionFramePipeBackend()
    old = Worker(alive=False)
    backend._thread = old
    backend._stop_evt.set()
    backend._ready_evt.set()
    assert backend.start() is True
    assert backend._thread is not old and backend._thread.started
    assert not backend._stop_evt.is_set()
    assert not backend._ready_evt.is_set()


@pytest.mark.parametrize('exception', [RuntimeError, MemoryError])
def test_reader_exception_retires_connection_and_readiness(monkeypatch, exception):
    backend = cb.OrionFramePipeBackend()
    backend._connected = True
    backend._ready_evt.set()
    backend._producer_identity_state = 'verified'
    handle = object()
    backend._handle = handle
    closed = []
    cleared = []
    monkeypatch.setattr(backend, '_connect_once', lambda: True)
    monkeypatch.setattr(backend._ring, 'clear', lambda: cleared.append(True))
    def failed_reader():
        raise exception('synthetic decoder failure')
    def close():
        closed.append(backend._handle)
        backend._handle = None
    monkeypatch.setattr(backend, '_reader_loop', failed_reader)
    monkeypatch.setattr(backend, '_close', close)
    with pytest.raises(exception, match='synthetic decoder failure'):
        backend._run()
    assert not backend._connected and not backend._ready_evt.is_set()
    assert closed == [handle] and len(cleared) == 1
    assert backend._producer_identity_state == 'disconnected'
    assert backend._handle is None


def test_normal_reader_exit_also_retires_connection(monkeypatch):
    backend = cb.OrionFramePipeBackend()
    backend._connected = True
    backend._ready_evt.set()
    closed = []
    monkeypatch.setattr(backend, '_connect_once', lambda: True)
    monkeypatch.setattr(backend, '_reader_loop', backend._stop_evt.set)
    monkeypatch.setattr(backend, '_close', lambda: closed.append(True))
    backend._run()
    assert closed == [True]
    assert not backend._connected and not backend._ready_evt.is_set()
