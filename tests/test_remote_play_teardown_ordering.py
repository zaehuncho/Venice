"""Disconnect/teardown ordering contracts (2026-09-19 audit, defect "no disconnect bugs").

Two PROVEN bugs on the Python side of the native<->sidecar teardown boundary, both
ending in the same place: the native writes {"cmd":"shutdown"}, waits
kSidecarGracefulShutdownMs (5 s, SidecarWatchdog.h) and then `taskkill /F /T`.

  F1  `recover_input` / `start_stream` ran ON the stdin reader thread, so while
      either was in flight the reader could not READ `shutdown` at all.
      recover_input_link() paces three attempts against a 17 s deadline, so a
      Disconnect pressed during input recovery -- exactly when the owner would press
      it, because input is dead while the video is still live -- was force-killed at
      t=5 s. chiaki_session_stop() never ran (console: "LAN cable disconnected") and
      orch.stop() never ran (Elgato handle still held).

  F2  The graceful Chiaki close lived inside orch.stop(), which autogreen_sidecar.py
      only called AFTER joining the preview worker (2.0 s) and the preview-stats
      worker (1.0 s). On a slow join the WM_CLOSE went out at t~3 s and its own 3 s
      wait ran past the force-kill.

These tests pin the fixes at the seams a unit test can actually reach: the new
RemotePlayOrchestrator.close_remote_play_client() and recover_input_link()'s
stopping-intent check.
"""
import threading
import time

import pytest

import remote_play_orchestrator as rpo


class _FakeClientManager:
    def __init__(self, events, name="client", close_s=0.0):
        self.events = events
        self.name = name
        self.close_s = close_s
        self.stopped = False

    def stop(self):
        if self.close_s:
            time.sleep(self.close_s)
        self.stopped = True
        self.events.append(f"{self.name}.stop")


def _bare_orchestrator():
    """An orchestrator instance without running __init__'s full machinery."""
    orch = rpo.RemotePlayOrchestrator.__new__(rpo.RemotePlayOrchestrator)
    orch._running = True
    orch._client_manager = None
    orch._input_link_ready = True
    orch._input_link_checked_at = 0.0
    return orch


# ---------------------------------------------------------------------------
# F2 — the PS5 disconnect handshake must be reachable on its own, first
# ---------------------------------------------------------------------------

def test_close_remote_play_client_is_callable_alone_and_publishes_stop_intent():
    events = []
    orch = _bare_orchestrator()
    orch._client_manager = _FakeClientManager(events)

    orch.close_remote_play_client()

    assert events == ["client.stop"], "the graceful close must actually run"
    assert orch._client_manager is None
    # The stopping intent has to be visible to anything still running on another
    # thread (the session-command worker) BEFORE the close returns.
    assert orch._running is False
    assert orch._input_link_ready is False


def test_close_remote_play_client_is_idempotent_so_stop_stays_a_no_op():
    events = []
    orch = _bare_orchestrator()
    orch._client_manager = _FakeClientManager(events)

    orch.close_remote_play_client()
    orch.close_remote_play_client()

    assert events == ["client.stop"], "stop() must not close a second time"


def test_close_remote_play_client_survives_a_throwing_manager():
    class _Angry:
        def stop(self):
            raise RuntimeError("WM_CLOSE refused")

    orch = _bare_orchestrator()
    orch._client_manager = _Angry()
    orch.close_remote_play_client()          # must not raise
    assert orch._client_manager is None, "a failed close must still release the slot"


def test_the_sidecar_teardown_calls_the_client_close_before_the_preview_joins():
    """Source-level contract for autogreen_sidecar.py's ``finally:`` block.

    This is an ordering bug that only shows up under a slow preview join, which a
    unit test cannot reproduce without the capture card. Pin the ORDER instead: the
    client close must appear before the first preview join in the teardown block.
    """
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "native_orion", "backend", "autogreen_sidecar.py")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()

    close_at = source.rindex("close_remote_play_client")
    join_at = source.rindex("_preview_thread.join")
    orch_stop_at = source.rindex("orch.stop()")
    assert close_at < join_at, (
        "the PS5 disconnect handshake must be requested before the preview joins "
        "can spend the native's 5 s grace")
    assert join_at < orch_stop_at, "the rest of the teardown still follows"


# ---------------------------------------------------------------------------
# F1 — a teardown must abort an in-flight input recovery
# ---------------------------------------------------------------------------

def test_input_recovery_abandons_itself_once_the_orchestrator_is_stopping():
    launches = []
    orch = _bare_orchestrator()
    orch._running = False                       # a teardown already began
    orch._launch_remote_play_client = lambda **kw: launches.append(kw) or True

    assert orch.recover_input_link() is False
    assert launches == [], (
        "a recovery that keeps launching clients under a teardown leaves the console "
        "with a session the teardown never closes")
    assert orch._input_link_ready is False


def test_input_recovery_still_runs_normally_while_the_session_is_live():
    orch = _bare_orchestrator()
    calls = []

    def _launch(**kw):
        calls.append(kw)
        return True

    orch._launch_remote_play_client = _launch
    orch.input_link_ready = lambda: True

    assert orch.recover_input_link() is True
    assert len(calls) == 1, "the stopping-intent check must not cost a live attempt"


def test_a_stop_landing_mid_recovery_stops_it_at_the_next_attempt_boundary():
    orch = _bare_orchestrator()
    calls = []

    def _launch(**kw):
        calls.append(kw)
        # First attempt "succeeds" as a process but never proves readiness, so the
        # loop would normally continue; meanwhile a teardown lands.
        orch._running = False
        return False

    orch._launch_remote_play_client = _launch
    orch.input_link_ready = lambda: False

    assert orch.recover_input_link() is False
    assert len(calls) == 1, "the second attempt must not start after the stop"


# ---------------------------------------------------------------------------
# F1 — the stdin reader must not be the thread that blocks
# ---------------------------------------------------------------------------

def test_the_stdin_loop_defers_the_two_long_blocking_commands():
    """Source-level contract for autogreen_sidecar.py's command dispatch.

    The full loop needs a live orchestrator, a capture card and a real stdin, so the
    guarantee is pinned where it is decided: neither long-blocking handler may be
    invoked directly from the stdin reader's dispatch chain.
    """
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "native_orion", "backend", "autogreen_sidecar.py")
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    start = next(i for i, l in enumerate(lines) if l.strip() == "def _stdin_loop():")
    # The dispatch chain ends at the thread that runs it.
    end = next(i for i in range(start, len(lines)) if "_stdin_loop_wrapped" in lines[i])
    body = "\n".join(lines[start:end])

    assert "_defer_command(\"recover_input\"" in body
    assert "_defer_command(\"start_stream\"" in body
    assert "_handle_recover_input(orch)" not in body, (
        "recover_input blocks the reader for up to 17 s; shutdown then cannot be read")
    assert "_handle_start_stream(orch, msg)" not in body, (
        "start_stream blocks the reader for the whole promotion")
    # shutdown stays on the reader itself — that is the whole point.
    assert "stop_evt.set()" in body


def test_deferred_queue_is_bounded_so_a_wedged_handler_cannot_grow_it():
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "native_orion", "backend", "autogreen_sidecar.py")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    assert "_deferred_cmd_queue: \"queue.Queue\" = queue.Queue(maxsize=" in source
    assert "except queue.Full:" in source
