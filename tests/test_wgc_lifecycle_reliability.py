"""Deterministic WGC callback/ownership tests; no desktop or console required."""
import sys
import types
import numpy as np
import pytest

from wgc_backend import WGCCaptureBackend
from remote_play_orchestrator import RemotePlayOrchestrator


@pytest.fixture
def capture(monkeypatch):
    instances = []
    class Control:
        def stop(self):
            pass
    class Capture:
        def __init__(self, **kwargs):
            self.callbacks = {}
            instances.append(self)
        def event(self, callback):
            self.callbacks[callback.__name__] = callback
            return callback
        def start_free_threaded(self):
            return Control()
    monkeypatch.setitem(sys.modules, "windows_capture", types.SimpleNamespace(WindowsCapture=Capture))
    return instances, Control()


def publish(capture, pixels, convert=None, index=-1):
    class Frame:
        def convert_to_bgr(self):
            if convert:
                convert()
            return types.SimpleNamespace(frame_buffer=pixels)
    capture[0][index].callbacks["on_frame_arrived"](Frame(), capture[1])


def test_callback_owns_pixels_before_sdk_recycles_buffer(capture):
    b = WGCCaptureBackend(window_hwnd=123)
    assert b.start()
    pixels = np.full((12, 20, 3), 75, np.uint8)
    publish(capture, pixels)
    fd = b.get_frame()
    pixels[:] = 0
    assert np.all(fd.frame == 75)
    assert not fd.frame.flags.writeable
    assert fd.integrity_isolated
    b.stop()


def test_conversion_delay_is_not_restamped_as_fresh(capture, monkeypatch):
    clock = [1_000_000_000]
    monkeypatch.setattr("wgc_backend.time.perf_counter_ns", lambda: clock[0])
    b = WGCCaptureBackend(window_hwnd=123)
    assert b.start()
    publish(capture, np.ones((12, 20, 3), np.uint8), lambda: clock.__setitem__(0, clock[0] + 20_000_000))
    fd = b.get_frame()
    assert fd.capture_timestamp_ns == 1_000_000_000
    assert fd.publication_timestamp_ns - fd.capture_timestamp_ns == 20_000_000
    b.stop()


def test_expensive_conversion_never_becomes_fresh_frame(capture, monkeypatch):
    clock = [1_000_000_000]
    monkeypatch.setattr("wgc_backend.time.perf_counter_ns", lambda: clock[0])
    b = WGCCaptureBackend(window_hwnd=123)
    assert b.start()
    publish(capture, np.ones((12, 20, 3), np.uint8), lambda: clock.__setitem__(0, clock[0] + 80_000_000))
    assert b.get_frame_nonblocking() is None
    b.stop()


def test_stop_and_close_retire_cached_pixels(capture):
    b = WGCCaptureBackend(window_hwnd=123)
    assert b.start()
    publish(capture, np.ones((12, 20, 3), np.uint8))
    capture[0][-1].callbacks["on_closed"]()
    assert b.closed
    assert b.get_frame_nonblocking() is None
    b.stop()
    assert b.get_frame_nonblocking() is None


def test_old_callbacks_cannot_enter_restarted_generation(capture):
    b = WGCCaptureBackend(window_hwnd=123)
    assert b.start()
    old = capture[0][-1]
    b.stop()
    assert b.start()
    old.callbacks["on_closed"]()
    publish(capture, np.ones((12, 20, 3), np.uint8), index=0)
    assert not b.closed
    assert b.get_frame_nonblocking() is None
    publish(capture, np.ones((12, 20, 3), np.uint8))
    assert b.get_frame() is not None
    b.stop()


def test_resize_retires_reader_geometry_generation(capture):
    b = WGCCaptureBackend(window_hwnd=123)
    assert b.start()
    publish(capture, np.ones((12, 20, 3), np.uint8))
    first = b.get_frame()
    publish(capture, np.ones((24, 40, 3), np.uint8))
    second = b.get_frame()
    assert second.source_generation > first.source_generation > 0
    b.stop()


def test_wgc_waits_for_new_publications_not_polling_old_pixels():
    assert RemotePlayOrchestrator._backend_is_event_driven("wgc", types.SimpleNamespace(get_frame=lambda **kw: None))


def test_target_validation_revokes_capture(capture):
    valid = [True]
    b = WGCCaptureBackend(window_hwnd=123, target_validator=lambda: valid[0])
    assert b.start()
    publish(capture, np.ones((12, 20, 3), np.uint8))
    assert b.is_healthy()
    valid[0] = False
    publish(capture, np.ones((12, 20, 3), np.uint8))
    assert not b.is_healthy()
    assert b.get_frame_nonblocking() is None
    b.stop()
