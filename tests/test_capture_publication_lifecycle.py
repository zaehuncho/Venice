"""Deterministic frame-lifecycle regressions; never open a device or pipe."""
import threading
import time

import numpy as np
import pytest

import capture_card_backend as capture
from chiaki_backend import FrameData, OrionFramePipeBackend


def _capture_backend():
    backend = capture.CaptureCardBackend()
    backend._min_width = backend._min_height = 4
    backend._aspect_ratio = 1.0
    backend._hw_pts_decided = True
    backend._pts_lock = False
    backend._source_generation = 8
    return backend


@pytest.mark.parametrize("blocking", [False, True])
def test_capture_stop_discards_last_generation(blocking):
    backend = _capture_backend()
    backend._ring.put(FrameData(np.zeros((4, 4, 3), dtype=np.uint8), source_generation=8))
    backend.stop()
    frame = backend.get_frame(0.0) if blocking else backend.get_frame_nonblocking()
    assert frame is None, "a stopped capture source must not retain usable pixels"


@pytest.mark.parametrize("transition", ["stop", "replace"])
@pytest.mark.parametrize("stage", ["read", "isolation"])
def test_capture_inflight_old_generation_never_publishes(monkeypatch, transition, stage):
    backend = _capture_backend()
    publications = []

    class Cap:
        def read(self):
            if stage == "read":
                change_generation()
            return True, np.ones((4, 4, 3), dtype=np.uint8)

        def release(self):
            pass

    backend._cap = Cap()

    def change_generation():
        backend.stop()
        if transition == "replace":
            # A replacement owns the same backend; clearing the shared event
            # alone must not resurrect a previous reader's accepted frame.
            backend._cap = Cap()
            backend._source_generation += 1
            backend._stop_evt.clear()

    isolate = capture._isolate_immutable_frame

    def isolate_then_transition(frame, verify):
        result = isolate(frame, verify)
        if stage == "isolation":
            change_generation()
        return result

    monkeypatch.setattr(capture, "_isolate_immutable_frame", isolate_then_transition)
    monkeypatch.setattr(backend._ring, "put", publications.append)
    backend._run()
    assert publications == [], "in-flight old pixels crossed the stop/source-generation boundary"
    assert backend._last_put_ns == 0
    assert not backend._cadence_samples


def test_abandoned_capture_reader_cannot_repopulate_after_stop():
    backend = _capture_backend()
    entered, release_read = threading.Event(), threading.Event()
    released = []

    class Cap:
        def read(self):
            entered.set()
            assert release_read.wait(3.0)
            return True, np.ones((4, 4, 3), dtype=np.uint8)

        def release(self):
            released.append(threading.current_thread().name)

    backend._cap = Cap()
    backend._STOP_JOIN_S = 0.0
    reader = threading.Thread(target=backend._run, name="LifecycleCaptureReader")
    backend._thread = reader
    reader.start()
    try:
        assert entered.wait(3.0)
        backend.stop()
        assert released == []
    finally:
        release_read.set()
        reader.join(3.0)
    assert not reader.is_alive()
    assert released == ["LifecycleCaptureReader"]
    assert backend.get_frame_nonblocking() is None
    assert backend._last_put_ns == 0


@pytest.mark.parametrize("transition", ["stop", "replace"])
@pytest.mark.parametrize("stage", ["payload", "conversion"])
def test_pipe_inflight_old_generation_never_publishes(monkeypatch, transition, stage):
    backend = OrionFramePipeBackend()
    backend._connected = True
    backend._connection_generation = 8
    backend._source_generation = 8
    width, height = 64, 32
    payload = bytes(width * height * 3 // 2)
    producer_ns = time.perf_counter_ns()
    header = backend._HEADER.pack(
        backend._MAGIC, backend._VERSION, backend._HEADER.size,
        0, 17, 1, producer_ns, width, height, backend._FMT_NV12,
        len(payload), 1_000_000, 0,
    )
    reads = iter((header, payload, None))

    def change_generation():
        backend.stop()
        if transition == "replace":
            backend._connection_generation += 1
            backend._source_generation += 1
            backend._connected = True
            backend._stop_evt.clear()

    def read(_n):
        value = next(reads)
        if value is payload and stage == "payload":
            change_generation()
        return value

    def convert(_payload, w, h, _fmt):
        if stage == "conversion":
            change_generation()
        return np.ones((h, w, 3), dtype=np.uint8)

    monkeypatch.setattr(backend, "_read_exact", read)
    monkeypatch.setattr(backend, "_to_bgr", convert)
    backend._reader_loop()
    assert backend.get_frame_nonblocking() is None
    assert not backend._ready_evt.is_set(), "stopped conversion must not rearm readiness"
    assert not backend._cadence_samples


def test_capture_current_generation_keeps_latest_frame_and_capture_clock(monkeypatch):
    backend = _capture_backend()

    class Cap:
        def read(self):
            return True, np.full((4, 4, 3), 39, dtype=np.uint8)

        def release(self):
            pass

    backend._cap = Cap()
    put = backend._ring.put

    def publish(frame):
        put(frame)
        backend._stop_evt.set()

    monkeypatch.setattr(backend._ring, "put", publish)
    backend._run()
    fd = backend.get_frame_nonblocking()
    assert fd is not None and fd.source_generation == 8
    assert fd.capture_timestamp_ns <= fd.publication_timestamp_ns
    assert np.all(fd.frame == 39)
    assert not fd.frame.flags.writeable
