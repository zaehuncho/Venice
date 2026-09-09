"""Scheduling contracts for the live AsyncMeterLocator.

Acquisition and read rescue may ask for immediate/latest pixels, but they must never
move ONNX inference back onto the capture callback.  These tests need no model/CUDA.
"""

import threading
import time

import numpy as np

from meter_detector_yolo import AsyncMeterLocator, MeterYoloLocator


class _Base:
    ok = True
    provider = "fake"

    def __init__(self):
        self.cv = threading.Condition()
        self.calls = []

    def detect_box(self, frame):
        with self.cv:
            self.calls.append((int(frame[0, 0, 0]), time.perf_counter(), "full"))
            self.cv.notify_all()
        return None

    def detect_box_tile(self, frame, region, _fraction):
        with self.cv:
            self.calls.append((int(frame[0, 0, 0]), time.perf_counter(), region))
            self.cv.notify_all()
        return None

    def wait_calls(self, count, timeout=1.0):
        deadline = time.perf_counter() + timeout
        with self.cv:
            while len(self.calls) < count:
                left = deadline - time.perf_counter()
                if left <= 0.0:
                    return False
                self.cv.wait(left)
        return True


def _frame(marker):
    return np.full((8, 8, 3), int(marker), np.uint8)


def _stop(locator):
    locator.stop()
    if locator._thread is not None:
        locator._thread.join(timeout=1.0)


def test_ordinary_newer_submit_keeps_pending_priority_wake(monkeypatch):
    """Newest pixels may replace queued pixels; ordinary work cannot erase urgency."""
    monkeypatch.setenv("ORION_METER_DETECTOR_MIN_INTERVAL_MS", "1000")
    base = _Base()
    # Build all normal state without starting a worker, then inspect the atomic queue.
    base.ok = False
    locator = AsyncMeterLocator(base=base, sync=False)
    locator.ok = True

    locator.submit_priority_region(_frame(1), 1.0, "left")
    locator.submit(_frame(2), 2.0)

    pending = locator._pending
    assert pending is not None
    frame, ts, scope, priority = pending
    assert int(frame[0, 0, 0]) == 2       # latest frame wins
    assert ts == 2.0 and scope == "full"
    assert priority is True               # priority wake survives replacement


def test_priority_interrupts_worker_breathing_gap(monkeypatch):
    """Priority bypasses only the cadence wait; inference remains on meter-yolo."""
    monkeypatch.setenv("ORION_METER_DETECTOR_MIN_INTERVAL_MS", "1000")
    base = _Base()
    locator = AsyncMeterLocator(base=base, sync=False)
    try:
        locator.submit(_frame(1), 1.0)
        assert base.wait_calls(1)

        queued_at = time.perf_counter()
        locator.submit_priority(_frame(2), 2.0)
        assert base.wait_calls(2, timeout=0.40)
        marker, started_at, scope = base.calls[1]
        assert marker == 2 and scope == "full"
        assert started_at - queued_at < 0.40  # did not wait the configured 1s gap
        assert locator._thread.ident != threading.get_ident()
    finally:
        _stop(locator)


def test_worker_and_explicit_detect_now_share_one_inference_lock(monkeypatch, tmp_path):
    """Even the offline synchronous API cannot overlap the worker's ORT session."""
    monkeypatch.setenv("ORION_METER_DETECTOR_MIN_INTERVAL_MS", "0")
    base = MeterYoloLocator(model_path=str(tmp_path / "missing.onnx"))
    base.ok = True
    base.provider = "fake"
    active = 0
    max_active = 0
    entered = threading.Event()
    state_lock = threading.Lock()

    def _slow(_frame_arg, output_offset=(0, 0), plausibility_size=None):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        entered.set()
        time.sleep(0.04)
        with state_lock:
            active -= 1
        return None

    base._detect_box_locked = _slow
    locator = AsyncMeterLocator(base=base, sync=False)
    caller = None
    try:
        locator.submit_priority(_frame(1), 1.0)
        assert entered.wait(1.0)
        caller = threading.Thread(
            target=lambda: locator.detect_now(_frame(2), 2.0), daemon=True)
        caller.start()
        caller.join(timeout=1.0)
        assert not caller.is_alive()
        assert max_active == 1
    finally:
        _stop(locator)
        if caller is not None:
            caller.join(timeout=1.0)

