"""Windows Graphics Capture (WGC) frame backend.

An alternative detector frame source to the decoder pipe + GDI BitBlt. WGC
(``Windows.Graphics.Capture``, Win10 1903+) captures a SPECIFIC window (or a
monitor) off the DWM composition surface at up to the refresh rate, and — unlike
GDI BitBlt — it does NOT black out when the target window is occluded or in the
background (GDI returns an all-zero bitmap on a Vulkan swapchain / when covered;
see [[nexusvision-feed-freeze-gdi-ab]]). Native on Win10 1903+, 60fps+ capture
with overlay isolation. Implemented here on the public ``windows-capture``
package.

Drop-in for the orchestrator's decoder path: same ``start`` / ``stop`` /
``get_frame_nonblocking`` / ``get_frame`` contract and the shared ``FrameData`` /
latest-wins ``FrameRingBuffer`` as ``OrionFramePipeBackend``. ``windows-capture``
is an OPTIONAL dependency — ``start()`` returns ``False`` (never raises) when it
is missing or the target can't be opened. The caller decides recovery; an Xbox
attachment stops rather than falling back to GDI or a different window.

Target precedence: ``window_hwnd`` > ``window_name`` (title substring) >
``monitor_index`` (1-based; 1 = primary).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import numpy as np

from chiaki_backend import FrameData, FrameRingBuffer

logger = logging.getLogger(__name__)


class WGCCaptureBackend:
    """Capture frames from a window/monitor via Windows Graphics Capture."""

    def __init__(
        self,
        window_name: Optional[str] = None,
        window_hwnd: Optional[int] = None,
        monitor_index: Optional[int] = None,
        cursor_capture: bool = False,
        draw_border: bool = False,
        target_validator: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._window_name = window_name
        self._window_hwnd = int(window_hwnd) if window_hwnd else None
        self._monitor_index = monitor_index
        self._cursor_capture = cursor_capture
        self._draw_border = draw_border
        self._target_validator = target_validator

        self._ring = FrameRingBuffer(capacity=2, max_frame_age_ns=50_000_000)
        self._lifecycle_lock = threading.RLock()
        self._generation = 0
        self._source_generation = 0
        self._last_arrival_ns = 0
        self._stop_evt = threading.Event()
        self._capture = None          # windows_capture.WindowsCapture
        self._control = None          # CaptureControl from start_free_threaded()
        self._frame_number = 0
        self._last_geom = (0, 0)
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        if self._control is not None and not self._closed:
            return True
        self.stop()
        try:
            from windows_capture import WindowsCapture
        except Exception as exc:  # package missing / import error
            logger.warning("WGC backend unavailable (windows-capture not importable: %s)", exc)
            return False

        # Build the capture target. Pass ONLY the selected target so the package
        # doesn't ambiguously combine a window + a monitor.
        kwargs = dict(cursor_capture=self._cursor_capture, draw_border=self._draw_border)
        if self._window_hwnd:
            kwargs["window_hwnd"] = self._window_hwnd
            target = f"hwnd=0x{self._window_hwnd:X}"
        elif self._window_name:
            kwargs["window_name"] = self._window_name
            target = f"window='{self._window_name}'"
        else:
            kwargs["monitor_index"] = self._monitor_index if self._monitor_index else 1
            target = f"monitor={kwargs['monitor_index']}"

        try:
            self._capture = WindowsCapture(**kwargs)
        except Exception as exc:
            logger.warning("WGC backend could not open %s: %s", target, exc)
            return False

        with self._lifecycle_lock:
            self._generation += 1
            generation = self._generation
            self._source_generation += 1
            self._frame_number = 0
            self._last_geom = (0, 0)
            self._last_arrival_ns = 0
            self._ring.clear()
            self._stop_evt.clear()
            self._closed = False

        @self._capture.event
        def on_frame_arrived(frame, capture_control):  # runs on the WGC worker thread
            # Callback entry is the earliest clock the SDK exposes. Never hide
            # readback/conversion/copy time by stamping after those operations.
            capture_ns, epoch_ns = time.perf_counter_ns(), time.time_ns()
            if generation != self._generation or self._stop_evt.is_set():
                try:
                    capture_control.stop()
                except Exception:
                    pass
                return
            try:
                if self._target_validator is not None and not self._target_validator():
                    on_closed()
                    capture_control.stop()
                    return
                bgr = frame.convert_to_bgr().frame_buffer
                if (bgr is None or getattr(bgr, "size", 0) == 0
                        or bgr.ndim != 3 or bgr.shape[2] < 3 or bgr.dtype != np.uint8):
                    return
                # ascontiguousarray can alias the SDK's recyclable allocation.
                # A private immutable copy is required before the callback exits.
                bgr = np.array(bgr[:, :, :3], dtype=np.uint8, order="C", copy=True)
                bgr.setflags(write=False)
                geom = (bgr.shape[1], bgr.shape[0])
                with self._lifecycle_lock:
                    if generation != self._generation or self._stop_evt.is_set() or self._closed:
                        return
                    if geom != self._last_geom:
                        self._source_generation += 1
                        self._ring.clear()
                        logger.info("WGC frame geometry %dx%d; new source generation", *geom)
                        self._last_geom = geom
                    self._frame_number += 1
                    if self._ring.put(FrameData(
                        frame=bgr, timestamp_ns=capture_ns, epoch_ns=epoch_ns,
                        frame_number=self._frame_number,
                        source_generation=self._source_generation,
                        integrity_isolated=True,
                    )):
                        self._last_arrival_ns = capture_ns
            except Exception as exc:
                logger.debug("WGC frame convert error: %s", exc)

        @self._capture.event
        def on_closed():
            with self._lifecycle_lock:
                if generation != self._generation:
                    return
                self._closed = True
                self._last_arrival_ns = 0
                self._ring.clear()
            logger.info("WGC capture target closed")

        try:
            self._control = self._capture.start_free_threaded()
        except Exception as exc:
            logger.warning("WGC backend failed to start capture on %s: %s", target, exc)
            self.stop()
            return False

        logger.info("WGC capture started (%s)", target)
        return True

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_evt.set()
            self._generation += 1
            self._closed = True
            self._last_arrival_ns = 0
            self._ring.clear()
            ctrl, self._control = self._control, None
            self._capture = None
        # The SDK may join callbacks here. Never hold their publication lock.
        if ctrl is not None:
            try:
                ctrl.stop()
            except Exception:
                pass
        logger.info("WGC capture stopped")

    def is_healthy(self) -> bool:
        with self._lifecycle_lock:
            return (not self._closed and not self._stop_evt.is_set()
                    and self._last_arrival_ns > 0
                    and 0 <= time.perf_counter_ns() - self._last_arrival_ns < 250_000_000)

    # -- frame access (decoder-backend contract) ---------------------------

    def get_frame_nonblocking(self) -> Optional[FrameData]:
        return self._ring.get_latest_nonblocking()

    def get_frame(self, timeout: float = 0.05) -> Optional[FrameData]:
        return self._ring.get_latest(timeout)

    @property
    def closed(self) -> bool:
        return self._closed
