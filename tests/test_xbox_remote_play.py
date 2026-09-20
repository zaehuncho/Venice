"""External-client Xbox attachment and failure isolation (no console required)."""
from types import SimpleNamespace
import pytest

from xbox_remote_play import XboxWindow, is_xbox_window, select_xbox_window
from remote_play_orchestrator import RemotePlayOrchestrator


@pytest.mark.parametrize("exe", ["XboxPcApp.exe", "XboxApp.exe", "msedge.exe", "chrome.exe"])
def test_supported_client_window(exe):
    assert is_xbox_window("Xbox - Remote Play", exe)


@pytest.mark.parametrize("title,exe", [
    ("Xbox - Remote Play", "powershell.exe"), ("Orion Stream", "OrionStream.exe"),
    ("PS Remote Play", "RemotePlay.exe"), ("Personal mailbox", "chrome.exe"),
    ("Xbox notes", "Code.exe"), ("Xbox", "unknown.exe"),
])
def test_unrelated_window_is_not_a_capture_target(title, exe):
    assert not is_xbox_window(title, exe)


def test_selection_must_be_unique_and_exact():
    w = XboxWindow(123, 55, "Xbox - Remote Play", "XboxPcApp.exe")
    assert select_xbox_window(w.title, [w]) == w
    for title, rows in [("", [w]), ("Xbox", [w]), (w.title, []), (w.title, [w, w])]:
        with pytest.raises(ValueError):
            select_xbox_window(title, rows)


def bare_orch():
    o = RemotePlayOrchestrator.__new__(RemotePlayOrchestrator)
    o._xbox_mode = True
    o._frame_backend = None
    o._xbox_window = XboxWindow(123, 55, "Xbox - Remote Play", "XboxPcApp.exe")
    o._frame_backend_mode = "capture"
    return o


def test_xbox_never_attaches_inherited_chiaki_pipe(monkeypatch):
    monkeypatch.setenv("ORION_FRAME_PIPE", "1")
    monkeypatch.setenv("ORION_REQUIRE_FRAME_PIPE", "1")
    o = bare_orch()
    o._preattach_decoder_pipe()
    assert o._frame_backend is None


def test_wgc_failure_never_falls_back_to_ps5_or_desktop(monkeypatch):
    o = bare_orch()
    monkeypatch.setattr("xbox_remote_play.xbox_window_still_matches", lambda _: True)
    calls = []
    def backend(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(start=lambda: False)
    monkeypatch.setattr("wgc_backend.WGCCaptureBackend", backend)
    assert o._start_frame_backend() is False
    assert o._frame_backend is None
    assert calls[0]["window_hwnd"] == 123
    assert "monitor_index" not in calls[0]


def test_window_loss_revokes_input_readiness(monkeypatch):
    o = bare_orch()
    healthy = [True]
    o._frame_backend = SimpleNamespace(is_healthy=lambda: healthy[0])
    window_live = [True]
    monkeypatch.setattr("xbox_remote_play.xbox_window_still_matches", lambda _: window_live[0])
    assert o.input_link_ready()
    window_live[0] = False
    assert not o.input_link_ready()
    window_live[0] = True
    healthy[0] = False
    assert not o.input_link_ready()
    healthy[0] = True
    o._client_stop_requested = True
    assert not o.input_link_ready()


def test_closed_external_app_cannot_be_replaced_implicitly(monkeypatch):
    o = bare_orch()
    monkeypatch.setattr("xbox_remote_play.xbox_window_still_matches", lambda _: False)
    assert o._start_frame_backend() is False
    assert o._frame_backend is None
