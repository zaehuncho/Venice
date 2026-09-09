"""Source reset must run on the detector thread using the immutable frame bundle."""
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from chiaki_backend import FrameData
from remote_play_orchestrator import OrchestratorConfig, RemotePlayOrchestrator


def _orchestrator():
    orch = RemotePlayOrchestrator(OrchestratorConfig(
        frame_source="capture_card", auto_launch_client=False,
        virtual_controller=False, target_fps=60,
    ))
    orch._pose_timing = None
    orch._green_analyzer = object()
    orch._fill_forecaster = None
    orch._anim_anchor = None
    orch._latency_estimator_for_frame = lambda _generation: None
    orch._emit_calibration_status = lambda: None
    orch._update_green_overlay = lambda _value: None
    return orch


def _process_sources(identities, *, live_identity=None, generations=None,
                     fail_first=False, fail_reset=False):
    orch = _orchestrator()
    extended_bundle = len(orch._frame_bundle) > 10
    frames = [np.full((720, 1280, 3), n + 1, dtype=np.uint8)
              for n in range(len(identities))]
    events, completions = [], []
    index = 0

    def publish(n):
        now, epoch_ms = time.perf_counter(), time.time() * 1000.0
        generation = generations[n] if generations else 0
        orch._last_frame = frames[n]
        orch._last_frame_ts = now
        orch._frame_seq = n + 1
        bundle = (frames[n], now, np.full((720, 1280), n + 1, dtype=np.uint8),
                  0, epoch_ms, epoch_ms, n + 1, n + 100, False, generation)
        orch._frame_bundle = bundle + (identities[n],) if extended_bundle else bundle
        # This deliberately races ahead of the pending frame's captured identity.
        # A correct detector reset must not consult these capture-thread globals.
        orch._source_identity = live_identity if live_identity is not None else identities[n]
        orch._pending_source_identity = orch._source_identity

    class Detector:
        last_debug = {}

        def reset_tracking(self):
            events.append(("reset", index, threading.current_thread().name))
            if fail_reset:
                raise RuntimeError("synthetic reset failure")

        def set_shot_state(self, *_args):
            events.append(("shot", index, threading.current_thread().name))

        def set_native_y(self, _plane):
            events.append(("y", index, threading.current_thread().name))

        def detect(self, frame, ts=None):
            events.append(("detect", index, threading.current_thread().name))
            assert frame is frames[index]
            if fail_first and index == 0:
                raise RuntimeError("synthetic initial detector failure")
            return None

    def finish(_seq, _frame_no, _frozen, success, *args, **kwargs):
        nonlocal index
        completions.append(bool(success))
        index += 1
        if index == len(identities):
            orch._running = False
        else:
            publish(index)

    orch._meter_detector = Detector()
    orch._finish_processed_frame = finish
    publish(0)
    orch._running = True
    worker = threading.Thread(target=orch._processing_loop, name="SourceResetDetector")
    worker.start()
    worker.join(4.0)
    if worker.is_alive():
        orch._running = False
        worker.join(1.0)
    assert not worker.is_alive(), "detector fixture failed to make progress"
    assert len(completions) == len(identities)
    return events, completions


def test_same_size_source_change_resets_once_before_shot_and_native_y():
    events, completions = _process_sources([7, 7, 8, 8])
    assert completions == [True] * 4
    assert [event[:2] for event in events if event[0] == "reset"] == [("reset", 2)]
    assert [event[0] for event in events if event[1] == 2] == ["reset", "shot", "y", "detect"]
    assert {event[2] for event in events} == {"SourceResetDetector"}


@pytest.mark.parametrize("identities,live,expected", [
    ([7, 7, 8], 999, [2]),
    ([7, 7, 7], 8, []),
    ([7, 3, 7], 1000, [1, 2]),
])
def test_reset_uses_bundled_identity_not_capture_globals(identities, live, expected):
    events, completions = _process_sources(identities, live_identity=live)
    assert all(completions)
    assert [event[1] for event in events if event[0] == "reset"] == expected


def test_generic_integrity_reject_generation_does_not_reset_reader():
    events, completions = _process_sources([7, 7, 7], generations=[0, 4, 8])
    assert all(completions)
    assert not [event for event in events if event[0] == "reset"]


def test_initial_failed_detection_does_not_invent_processed_source():
    events, completions = _process_sources([7, 8, 8], fail_first=True)
    assert completions == [False, True, True]
    assert not [event for event in events if event[0] == "reset"]


def test_reset_exception_fails_closed_before_reading_new_source():
    events, completions = _process_sources([7, 8, 8], fail_reset=True)
    assert completions == [True, False, False]
    assert [event[1] for event in events if event[0] == "detect"] == [0]


def test_capture_bundle_carries_identity_of_exact_published_frame():
    orch = _orchestrator()
    frame = np.full((720, 1280, 3), 100, dtype=np.uint8)

    class Backend:
        count = 0

        def get_frame(self, timeout=0.02):
            self.count += 1
            if self.count > 1:
                # A mutable capture identity changed after publication; the bundle
                # must still describe the earlier frame when the detector wakes.
                orch._pending_source_identity = 999
                orch._source_identity = 999
                orch._running = False
                return None
            return FrameData(frame, timestamp_ns=time.perf_counter_ns(),
                             epoch_ns=time.time_ns(), frame_number=1,
                             source_generation=11)

        def get_frame_nonblocking(self):
            return self.get_frame()

        def is_healthy(self):
            return True

    backend = Backend()
    orch._frame_backend = backend
    orch._frame_backend_mode = "capture_card"
    orch._running = True
    orch._capture_loop()
    assert orch._frame_seq == 1
    assert len(orch._frame_bundle) == 11
    assert orch._frame_bundle[10] == 1
    assert orch._source_identity == 999
