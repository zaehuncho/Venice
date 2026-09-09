"""Capture-to-detector wakeups use the immutable frame identity, not poll timers."""
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

import remote_play_orchestrator as module
from chiaki_backend import FrameData
from remote_play_orchestrator import OrchestratorConfig, RemotePlayOrchestrator


def make_orchestrator():
    orch = RemotePlayOrchestrator(OrchestratorConfig(
        frame_source='capture_card', auto_launch_client=False,
        virtual_controller=False, target_fps=60))
    orch._pose_timing = None
    orch._green_analyzer = object()
    orch._fill_forecaster = None
    orch._anim_anchor = None
    orch._latency_estimator_for_frame = lambda _generation: None
    orch._emit_calibration_status = lambda: None
    orch._update_green_overlay = lambda _value: None
    orch._running = True
    return orch


def publish(orch, seq, *, generation=3, identity=7):
    pixels = np.full((8, 8, 3), seq % 255, dtype=np.uint8)
    pixels.flags.writeable = False
    stamp = time.perf_counter()
    epoch = time.time() * 1000
    bundle = (pixels, stamp, None, seq * 1000, epoch, epoch - 2.5,
              seq, seq + 100, False, generation, identity)
    orch._last_frame = pixels
    orch._last_frame_ts = stamp
    orch._frame_bundle = bundle
    orch._frame_seq = seq
    orch._detector_frame_ready_evt.set()
    return bundle


class ObservedEvent:
    def __init__(self):
        self.event = threading.Event()
        self.entered = threading.Event()
        self.timeouts = []
    def set(self): self.event.set()
    def clear(self): self.event.clear()
    def is_set(self): return self.event.is_set()
    def wait(self, timeout):
        self.timeouts.append(timeout)
        self.entered.set()
        return self.event.wait(timeout)


def test_real_processing_loop_does_not_park_on_timer_after_capture_commit(monkeypatch):
    orch = make_orchestrator()
    event = ObservedEvent()
    orch._detector_frame_ready_evt = event
    # A deliberately delayed polling sleep models timer/descheduling delay. The
    # old real loop parks here even after publication; event-driven pickup does
    # not call it. This is a synchronization regression, not a fps benchmark.
    poll_entered, release_poll, completed = (threading.Event() for _ in range(3))
    real_time = time
    class Clock:
        def __getattr__(self, name): return getattr(real_time, name)
        def sleep(self, seconds):
            if threading.current_thread().name == 'DetectorWakeTest' and seconds == .0005:
                poll_entered.set()
                release_poll.wait(1.0)
            else:
                real_time.sleep(seconds)
    monkeypatch.setattr(module, 'time', Clock())
    seen=[]
    class Detector:
        last_debug={}
        def detect(self, frame, ts=None):
            seen.append((frame, ts))
            return None
    orch._meter_detector = Detector()
    orch._finish_processed_frame = lambda *args, **kw: completed.set()
    worker = threading.Thread(target=orch._processing_loop, name='DetectorWakeTest')
    worker.start()
    try:
        end=time.monotonic()+1.0
        while not event.entered.is_set() and not poll_entered.is_set() and time.monotonic()<end:
            time.sleep(.001)
        expected=publish(orch, 1)
        assert completed.wait(.2), 'captured frame remained parked behind polling timer'
        assert not poll_entered.is_set(), 'real detector loop still uses capture polling sleeps'
        assert seen[0][0] is expected[0] and seen[0][1] == expected[5] / 1000.0
    finally:
        orch._running=False
        event.set()
        release_poll.set()
        worker.join(1)
    assert not worker.is_alive()


def test_capture_loop_signals_only_committed_authoritative_bundles():
    orch=make_orchestrator()
    base=np.random.default_rng(21).integers(20,235,(720,1280,3),dtype=np.uint8)
    changed=base.copy(); changed[333,777,2] ^= np.uint8(1)
    frames=iter([base,base.copy(),np.zeros_like(base),changed])
    committed=[]
    class Event(ObservedEvent):
        def set(self):
            bundle=orch._frame_bundle
            committed.append(bundle)
            assert bundle[6] == orch._frame_seq
            assert bundle[0] is not None and not bundle[0].flags.writeable
            super().set()
    orch._detector_frame_ready_evt=Event()
    class Backend:
        n=0
        def get_frame(self, timeout=.02):
            try: frame=next(frames)
            except StopIteration:
                orch._running=False
                return None
            self.n+=1
            return FrameData(frame=frame,timestamp_ns=time.perf_counter_ns(),
                             epoch_ns=time.time_ns(),frame_number=self.n)
        def is_healthy(self): return True
    orch._frame_backend=Backend()
    orch._frame_backend_mode='capture_card'
    orch._capture_loop()
    assert [b[6] for b in committed] == [1,2]
    assert [b[7] for b in committed] == [1,4]
    assert committed[1][9] > committed[0][9]  # dark reject remains an integrity fence
    assert orch._frame_integrity_counts['dark_frame'] == 1
    assert orch._frame_integrity_counts['pixel_duplicate'] == 1


@pytest.mark.parametrize('boundary',['before_clear','after_clear','before_wait'])
def test_publish_at_each_clear_check_wait_boundary_is_not_lost(boundary):
    orch=make_orchestrator()
    expected=[]
    class Event(ObservedEvent):
        fired=False
        def fire(self):
            if not self.fired:
                self.fired=True
                expected.append(publish(orch,5))
        def clear(self):
            if boundary=='before_clear': self.fire()
            super().clear()
            if boundary=='after_clear': self.fire()
        def wait(self, timeout):
            if boundary=='before_wait': self.fire()
            return super().wait(timeout)
    orch._detector_frame_ready_evt=Event()
    result=orch._wait_for_detector_frame(4)
    assert result is expected[0]
    assert result[6:] == (5,105,False,3,7)


def test_latest_wins_burst_preserves_captured_identity_without_queue():
    orch=make_orchestrator()
    first=publish(orch,1,generation=4,identity=9)
    assert orch._wait_for_detector_frame(-1) is first
    publish(orch,2,generation=4,identity=9)
    latest=publish(orch,3,generation=5,identity=10)
    # Mutable legacy diagnostics can be ahead; only bundled identity is used.
    orch._source_identity=999
    orch._last_frame_measurement_epoch_ms=1
    assert orch._wait_for_detector_frame(1) is latest
    assert latest[9:] == (5,10) and latest[5] != 1


def test_separate_event_does_not_consume_telemetry_wakeup():
    orch=make_orchestrator()
    assert orch._detector_frame_ready_evt is not orch._frame_ready_evt
    orch._frame_ready_evt.set()
    expected=publish(orch,1)
    assert orch._wait_for_detector_frame(-1) is expected
    assert orch._frame_ready_evt.is_set()


def test_watchdog_remaining_deadline_bounds_idle_wait(monkeypatch):
    orch=make_orchestrator()
    expected=publish(orch,1)
    orch._last_meter_present=True
    orch._stall_watchdog_ms=250
    waits=[]
    class Event(ObservedEvent):
        def wait(self,timeout): waits.append(timeout); return False
    orch._detector_frame_ready_evt=Event()
    monkeypatch.setattr(module,'time',SimpleNamespace(perf_counter=lambda:expected[1]+.240))
    assert orch._wait_for_detector_frame(1) is None
    assert waits == [pytest.approx(.010,abs=1e-8)]


def test_expired_watchdog_is_not_delayed_by_idle_event_wait(monkeypatch):
    orch=make_orchestrator()
    expected=publish(orch,1)
    orch._last_meter_present=True
    orch._stall_watchdog_ms=250
    class Event(ObservedEvent):
        def wait(self,timeout):
            assert timeout == 0
            return False
    orch._detector_frame_ready_evt=Event()
    monkeypatch.setattr(module,'time',SimpleNamespace(perf_counter=lambda:expected[1]+.251))
    assert orch._wait_for_detector_frame(1) is None


def test_stop_interrupts_detector_wait_before_client_teardown(monkeypatch):
    orch=make_orchestrator()
    event=ObservedEvent()
    orch._detector_frame_ready_evt=event
    result=[]
    waiter=orch._wait_for_detector_frame
    worker=threading.Thread(target=lambda:result.append(waiter(-1)))
    orch._thread=worker
    teardown_alive=[]
    class Client:
        def stop(self):
            worker.join(.1)
            teardown_alive.append(worker.is_alive())
    orch._client_manager=Client()
    monkeypatch.setattr(module,'get_standby_pool',None)
    worker.start()
    assert event.entered.wait(1)
    orch.stop()
    assert result == [None]
    assert teardown_alive == [False], 'detector was left waiting during slow client teardown'


def test_no_duplicate_detection_from_stale_set_event():
    orch=make_orchestrator()
    publish(orch,4)
    waits=[]
    class Event(ObservedEvent):
        def wait(self,timeout): waits.append(timeout); return False
    event=Event(); event.set(); orch._detector_frame_ready_evt=event
    assert orch._wait_for_detector_frame(4) is None
    assert waits == [.25]


def test_busy_real_detector_coalesces_burst_without_relabelling_inflight_frame():
    orch=make_orchestrator()
    # Also make this control executable against the polling baseline.
    if not hasattr(orch,'_detector_frame_ready_evt'):
        orch._detector_frame_ready_evt=threading.Event()
    entered, release, done=(threading.Event() for _ in range(3))
    seen=[]
    resets=[]
    finished=[]
    class Detector:
        last_debug={}
        def reset_tracking(self): resets.append(len(seen))
        def detect(self,frame,ts=None):
            seen.append(int(frame[0,0,0]))
            if len(seen)==1:
                entered.set()
                assert release.wait(1)
            return None
    def finish(seq,source_no,frozen,success,*args,**kwargs):
        finished.append((seq,source_no,kwargs['integrity_generation']))
        if seq==3:
            orch._running=False
            done.set()
    orch._meter_detector=Detector()
    orch._finish_processed_frame=finish
    publish(orch,1,generation=3,identity=7)
    worker=threading.Thread(target=orch._processing_loop)
    worker.start()
    try:
        assert entered.wait(1)
        publish(orch,2,generation=4,identity=8)
        publish(orch,3,generation=5,identity=9)
        release.set()
        assert done.wait(1)
    finally:
        orch._running=False
        release.set()
        orch._detector_frame_ready_evt.set()
        worker.join(1)
    assert not worker.is_alive()
    assert seen==[1,3] and orch._cv_frames_skipped==1
    assert finished==[(1,101,3),(3,103,5)]
    assert resets==[1]
