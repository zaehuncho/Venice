"""WGC (Windows Graphics Capture) frame backend.

Validates the occlusion-proof capture path is a drop-in for the orchestrator's
decoder frame source. Skips cleanly when the optional ``windows-capture`` package
isn't installed (it's import-guarded so the bot never hard-depends on it).
"""
import time

import numpy as np
import pytest

pytest.importorskip("windows_capture", reason="windows-capture not installed")

from wgc_backend import WGCCaptureBackend
from chiaki_backend import FrameData


def test_backend_contract_no_frames_before_start():
    b = WGCCaptureBackend(monitor_index=1)
    # Decoder-backend contract: non-blocking getter returns None when empty.
    assert b.get_frame_nonblocking() is None


def test_bogus_window_fails_gracefully():
    # A window title that won't match must NOT raise — start() returns False so the
    # orchestrator falls back to window capture.
    b = WGCCaptureBackend(window_name="__orion_no_such_window__zzz")
    result = b.start()
    assert isinstance(result, bool)
    b.stop()


def test_monitor_capture_delivers_bgr_frame():
    b = WGCCaptureBackend(monitor_index=1)
    assert b.start() is True
    try:
        fd = None
        deadline = time.time() + 6.0
        while time.time() < deadline:
            fd = b.get_frame_nonblocking()
            if fd is not None:
                break
            time.sleep(0.05)
        assert fd is not None, "WGC delivered no frame within 6s"
        assert isinstance(fd, FrameData)
        frame = fd.frame
        assert isinstance(frame, np.ndarray)
        assert frame.ndim == 3 and frame.shape[2] == 3, f"expected HxWx3 BGR, got {frame.shape}"
        assert frame.dtype == np.uint8
        assert frame.shape[0] > 0 and frame.shape[1] > 0
    finally:
        b.stop()
