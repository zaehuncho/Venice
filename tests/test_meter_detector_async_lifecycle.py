"""Offline, event-gated regressions for detector generation/lifecycle boundaries."""

import importlib.util
import os
import threading

import numpy as np
import pytest


if os.environ.get("METER_DETECTOR_MODULE"):
    _spec = importlib.util.spec_from_file_location(
        "meter_detector_lifecycle_probe", os.environ["METER_DETECTOR_MODULE"])
    _module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)
    AsyncMeterLocator = _module.AsyncMeterLocator
else:
    from meter_detector_yolo import AsyncMeterLocator


class _GatedBase:
    ok = True
    provider = "offline-fake"

    def __init__(self, negative_first=False):
        self.started = [threading.Event() for _ in range(4)]
        self.release = [threading.Event() for _ in range(4)]
        self.calls = []
        self.negative_first = negative_first

    def detect_box(self, frame):
        i = len(self.calls)
        self.calls.append((tuple(frame.shape), int(frame[0, 0, 0])))
        self.started[i].set()
        if not self.release[i].wait(2.0):
            raise RuntimeError("test did not release mock inference")
        if self.negative_first and i == 0:
            return None
        return (1, 2, 3, 4, 0.9)

    def detect_box_tile(self, frame, _region, _fraction):
        return self.detect_box(frame)


def _frame(shape=(8, 8, 3), marker=1):
    return np.full(shape, marker, np.uint8)


def _close(locator, base):
    locator.stop()
    for event in base.release:
        event.set()
    if locator._thread is not None:
        locator._thread.join(2.0)
        assert not locator._thread.is_alive()


@pytest.fixture(autouse=True)
def _cadence(monkeypatch):
    monkeypatch.setenv("ORION_METER_DETECTOR_MIN_INTERVAL_MS", "0")


def test_new_geometry_immediately_invalidates_published_old_geometry():
    base = _GatedBase()
    locator = AsyncMeterLocator(base=base, sync=True)
    try:
        base.release[0].set()
        locator.submit(_frame(), 1.0)
        assert locator.latest()[0] is True
        locator._sync = False  # inspect pending/result without creating a worker
        locator.submit(_frame((16, 16, 3)), 1.01)
        assert locator.latest() == (False, None, 0.0, -1.0)
    finally:
        _close(locator, base)


@pytest.mark.parametrize("negative_first", [False, True])
def test_inflight_old_geometry_cannot_publish_positive_or_no_meter(negative_first):
    base = _GatedBase(negative_first=negative_first)
    locator = AsyncMeterLocator(base=base, sync=False)
    try:
        locator.submit(_frame(), 1.0)
        assert base.started[0].wait(1.0)
        locator.submit(_frame((16, 16, 3)), 1.01)
        base.release[0].set()
        assert base.started[1].wait(1.0)
        assert locator.latest_details() == (False, None, 0.0, -1.0, "full")
    finally:
        _close(locator, base)


def test_reset_discards_inflight_and_pending_without_joining_worker():
    base = _GatedBase()
    locator = AsyncMeterLocator(base=base, sync=False)
    try:
        locator.submit(_frame(marker=1), 1.0)
        assert base.started[0].wait(1.0)
        locator.submit(_frame(marker=2), 1.01)
        locator.reset()
        assert locator._pending is None
        assert locator.latest() == (False, None, 0.0, -1.0)
        # A new session may restart its source clock and retain the same dimensions.
        locator.submit(_frame(marker=3), 0.1)
        base.release[0].set()
        assert base.started[1].wait(1.0)
        assert base.calls[1][1] == 3
        assert locator.latest() == (False, None, 0.0, -1.0)
    finally:
        _close(locator, base)


def test_stop_clears_pending_and_suppresses_inflight_publication():
    base = _GatedBase()
    locator = AsyncMeterLocator(base=base, sync=False)
    try:
        locator.submit(_frame(marker=1), 1.0)
        assert base.started[0].wait(1.0)
        locator.submit(_frame(marker=2), 1.01)
        locator.stop()
        base.release[0].set()
        locator._thread.join(1.0)
        assert not locator._thread.is_alive()
        assert locator._pending is None
        assert locator.latest() == (False, None, 0.0, -1.0)
        locator.submit(_frame(marker=3), 1.02)
        assert locator._pending is None
        assert len(base.calls) == 1
    finally:
        _close(locator, base)


def test_stop_blocks_explicit_sync_inference_and_clears_published_result():
    base = _GatedBase()
    locator = AsyncMeterLocator(base=base, sync=True)
    try:
        for event in base.release:
            event.set()
        locator.submit(_frame(), 1.0)
        assert locator.latest()[0]
        locator.stop()
        locator.submit(_frame(), 1.01)
        assert locator.detect_now(_frame(), 1.02) == (False, None, 0.0, -1.0)
        assert locator.latest() == (False, None, 0.0, -1.0)
        assert len(base.calls) == 1
    finally:
        _close(locator, base)


def test_same_geometry_preserves_latest_until_new_result_and_copies_frame():
    base = _GatedBase()
    locator = AsyncMeterLocator(base=base, sync=True)
    try:
        base.release[0].set()
        locator.submit(_frame(), 1.0)
        previous = locator.latest()
        locator._sync = False
        frame = _frame(marker=2)
        locator.submit(frame, 1.01)
        frame[:] = 99
        assert locator.latest() == previous
        assert locator._pending[0][0, 0, 0] == 2
    finally:
        _close(locator, base)


def test_new_geometry_result_recovers_and_normal_pending_queue_stays_latest_only():
    base = _GatedBase()
    locator = AsyncMeterLocator(base=base, sync=False)
    try:
        locator.submit(_frame(marker=1), 1.0)
        assert base.started[0].wait(1.0)
        # Source switches resolution while inference is busy. A long stream still
        # retains only one pending snapshot; no delayed FIFO develops.
        for marker in range(2, 102):
            locator.submit(_frame((16, 16, 3), marker), 1.0 + marker / 1000)
        base.release[0].set()
        assert base.started[1].wait(1.0)
        assert base.calls[1] == ((16, 16, 3), 101)
        assert locator.latest()[3] == -1.0
        locator.submit(_frame((16, 16, 3), 102), 1.102)
        base.release[1].set()
        assert base.started[2].wait(1.0)
        assert locator.latest() == (True, (1, 2, 3, 4), 0.9, 1.101)
    finally:
        _close(locator, base)


def test_tile_miss_does_not_survive_reset_as_full_frame_negative():
    base = _GatedBase(negative_first=True)
    locator = AsyncMeterLocator(base=base, sync=True)
    try:
        base.release[0].set()
        locator.submit_priority_region(_frame(), 1.0, "left")
        assert locator.latest_details() == (False, None, 0.0, 1.0, "left")
        locator.reset()
        assert locator.latest_details() == (False, None, 0.0, -1.0, "full")
        base.release[1].set()
        locator.submit_priority_region(_frame(), 0.1, "right")
        assert locator.latest_details() == (True, (1, 2, 3, 4), 0.9, 0.1, "right")
    finally:
        _close(locator, base)


def test_reset_during_snapshot_discards_frame_copied_before_reset(monkeypatch):
    base = _GatedBase()
    base.ok = False  # a manual queue so no inference can consume the probe
    locator = AsyncMeterLocator(base=base, sync=False)
    locator.ok = True
    real_ascontiguousarray = np.ascontiguousarray

    def _reset_then_copy(frame):
        locator.reset()
        return real_ascontiguousarray(frame)

    monkeypatch.setattr(np, "ascontiguousarray", _reset_then_copy)
    try:
        locator.submit(_frame(), 1.0)
        assert locator._pending is None
        assert locator.latest()[3] == -1.0
    finally:
        _close(locator, base)


def test_reset_during_sync_inference_returns_no_evidence():
    base = _GatedBase()
    locator = AsyncMeterLocator(base=base, sync=True)
    results = []
    caller = threading.Thread(target=lambda: results.append(
        locator.detect_now(_frame(), 1.0)))
    try:
        caller.start()
        assert base.started[0].wait(1.0)
        locator.reset()
        base.release[0].set()
        caller.join(1.0)
        assert not caller.is_alive()
        assert results == [(False, None, 0.0, -1.0)]
        assert locator.latest()[3] == -1.0
    finally:
        _close(locator, base)
        caller.join(1.0)


def test_reset_does_not_reenable_stopped_worker():
    base = _GatedBase()
    locator = AsyncMeterLocator(base=base, sync=False)
    try:
        locator.stop()
        locator.reset()
        locator.submit(_frame(), 1.0)
        assert locator._pending is None
        assert locator.detect_now(_frame(), 1.0) == (False, None, 0.0, -1.0)
        assert base.calls == []
    finally:
        _close(locator, base)
