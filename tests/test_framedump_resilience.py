"""The frame dump must not die silently.

Regression suite for the 2026-09-14 loss: session_20260914_204600 wrote 2,096 frames in its first
six minutes and then NOTHING for the remaining hour of a 67-minute batch, while capture, detector
and preview stayed live -- and no `FRAMEDUMP disabled (...)` line existed in any log.  Two distinct
mechanisms produce that shape:

  1. ONE slow PNG write past `_framedump_max_write_s` called `_framedump_disable('writer_stall')`,
     which is permanent for the process generation.
  2. `stop()` tore the writer down and `start()` re-armed only DETCSV, so any in-process capture
     restart ended the dump for good -- with no log line at all.

and the notice for (1) was lost to the native relay, where every sidecar WARNING competes for one
shared 1 Hz slot against DETDIAG's 25 lines/second (RemotePlaySession.cpp:2883-2889).
"""

import logging
import queue
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

import remote_play_orchestrator as rpo


_GIB = 1024 ** 3


def _bare_orchestrator(tmp_path, **overrides):
    orch = rpo.RemotePlayOrchestrator.__new__(rpo.RemotePlayOrchestrator)
    orch._framedump_env_enabled = True
    orch._framedump_enabled = True
    orch._framedump_permanent_stop = ""
    orch._framedump_disabled_reason = ""
    orch._framedump_dir = str(tmp_path)
    orch._framedump_count = 0
    orch._framedump_max = 100
    orch._framedump_interval = 0.0
    orch._framedump_last_ts = 0.0
    orch._framedump_armed = True
    orch._framedump_dropped = 0
    orch._framedump_reported_dropped = 0
    orch._framedump_skipped = 0
    orch._framedump_queue_depth = 1
    orch._framedump_q = queue.Queue(maxsize=1)
    orch._framedump_writer = None
    orch._framedump_writer_stop = threading.Event()
    orch._framedump_raw_only = True
    orch._framedump_min_free_bytes = 10 * _GIB
    orch._framedump_min_free_pct = 2.0
    orch._framedump_max_duty = 0.25
    orch._framedump_max_write_s = 0.250
    orch._framedump_backoff_max_s = 15.0
    orch._framedump_backoff_until = 0.0
    orch._framedump_backoff_s = 0.0
    orch._framedump_slow_streak = 0
    orch._framedump_error_streak = 0
    orch._framedump_max_error_streak = 5
    orch._framedump_last_write_ms = 0.0
    orch._framedump_last_write_ts = 0.0
    orch._framedump_heartbeat_s = 60.0
    orch._framedump_heartbeat_ts = 0.0
    orch._framedump_generation = 1
    orch._framedump_index_failed = False
    orch._framedump_index_fh = None
    orch._meter_detector = None
    for key, value in overrides.items():
        setattr(orch, "_framedump_" + key, value)
    return orch


def _info():
    return {"t": 10.0, "wall": 1_000.0, "det": True, "fill": 42.0, "conf": 0.9, "rej": "",
            "gc": 95.0, "gconf": 0.8, "bbox": (1, 2, 3, 4), "gs": -1, "ge": -1, "park": False}


def _result():
    return SimpleNamespace(gameplay_eligible=True, detected=True, fill_pct=42.0, confidence=0.9,
                           rejection_reason="", green_window_center_pct=95.0,
                           green_window_confidence=0.8, bbox=(1, 2, 3, 4),
                           green_window_start_row=-1, green_window_end_row=-1)


def _frame():
    return np.zeros((8, 8, 3), dtype=np.uint8)


def _slow_write_item(orch, monkeypatch, write_s):
    """Run one writer item whose PNG write takes `write_s` seconds of PERF-CLOCK time."""
    orch._framedump_disk_status = lambda: (True, 50 * _GIB, 10 * _GIB, "")
    orch._framedump_write_index = lambda idx, info: None
    clock = {"t": 0.0}
    real_perf = rpo.time.perf_counter

    def fake_perf():
        return clock["t"]

    def fake_imwrite(*_a, **_kw):
        clock["t"] += write_s
        return True

    monkeypatch.setattr(rpo.time, "perf_counter", fake_perf)
    monkeypatch.setattr(rpo.cv2, "imwrite", fake_imwrite)
    try:
        return orch._framedump_write_item((orch._framedump_count, _frame(), _info()))
    finally:
        monkeypatch.setattr(rpo.time, "perf_counter", real_perf)


# ---------------------------------------------------------------- slow writes


def test_one_slow_write_backs_off_instead_of_killing_the_dump(tmp_path, monkeypatch, caplog):
    """THE 2026-09-14 BUG. A 350 ms write must cost a cooldown, not the rest of the session."""
    orch = _bare_orchestrator(tmp_path)
    with caplog.at_level(logging.WARNING, logger=rpo.logger.name):
        keep_running = _slow_write_item(orch, monkeypatch, 0.350)

    assert keep_running is True
    assert orch._framedump_enabled is True
    assert orch._framedump_disabled_reason == ""
    assert orch._framedump_permanent_stop == ""
    assert orch._framedump_backoff_s == pytest.approx(1.0)
    assert orch._framedump_backoff_until > 0.0
    assert any("backing off" in r.getMessage() for r in caplog.records)


def test_back_off_and_recovery_log_at_error_so_the_native_relay_cannot_drop_them(tmp_path,
                                                                                monkeypatch,
                                                                                caplog):
    """Sidecar WARNINGs share ONE 1 Hz relay slot with 25 DETDIAG lines/s; ERROR bypasses it."""
    orch = _bare_orchestrator(tmp_path)
    with caplog.at_level(logging.WARNING, logger=rpo.logger.name):
        _slow_write_item(orch, monkeypatch, 0.350)
        orch._framedump_note_healthy_write(0.030)

    framedump = [r for r in caplog.records if "FRAMEDUMP" in r.getMessage()]
    assert framedump, "the dump said nothing about stalling"
    assert all(r.levelno >= logging.ERROR for r in framedump)
    assert any("resuming" in r.getMessage() for r in framedump)


def test_producer_skips_frames_during_the_cooldown_and_resumes_after_it(tmp_path):
    orch = _bare_orchestrator(tmp_path)
    orch._framedump_backoff_until = time.perf_counter() + 30.0

    orch._dump_frame(_frame(), _result())
    assert (orch._framedump_count, orch._framedump_skipped) == (0, 1)
    assert orch._framedump_q.empty()

    # Cooldown expired: the very next frame is dumped again, no restart needed.
    orch._framedump_backoff_until = time.perf_counter() - 0.001
    orch._dump_frame(_frame(), _result())
    assert (orch._framedump_count, orch._framedump_skipped) == (1, 1)
    assert orch._framedump_backoff_until == 0.0
    assert orch._framedump_q.qsize() == 1


def test_a_fast_write_after_a_stall_clears_the_streak_and_the_cooldown(tmp_path, monkeypatch):
    orch = _bare_orchestrator(tmp_path)
    _slow_write_item(orch, monkeypatch, 0.350)
    assert orch._framedump_slow_streak == 1

    keep_running = _slow_write_item(orch, monkeypatch, 0.010)

    assert keep_running is True
    assert orch._framedump_slow_streak == 0
    assert orch._framedump_backoff_until == 0.0
    assert orch._framedump_last_write_ms == pytest.approx(10.0, abs=1.0)


def test_repeated_stalls_widen_the_cooldown_but_never_disable(tmp_path, monkeypatch):
    orch = _bare_orchestrator(tmp_path, backoff_max_s=4.0)
    seen = []
    for _ in range(5):
        _slow_write_item(orch, monkeypatch, 0.400)
        seen.append(orch._framedump_backoff_s)

    assert seen == [1.0, 2.0, 4.0, 4.0, 4.0]      # doubling, capped, never fatal
    assert orch._framedump_enabled is True
    assert orch._framedump_permanent_stop == ""


def test_disk_and_directory_faults_still_disable_permanently(tmp_path, monkeypatch):
    """Back-off is for SLOW writes only; a full disk must still stop the dump for good."""
    orch = _bare_orchestrator(tmp_path)
    orch._framedump_disk_status = lambda: (False, 5 * _GIB, 10 * _GIB, "")
    monkeypatch.setattr(rpo.cv2, "imwrite",
                        lambda *_a, **_kw: pytest.fail("no PNG below the disk floor"))

    keep_running = orch._framedump_write_item((0, _frame(), _info()))

    assert keep_running is False
    assert orch._framedump_enabled is False
    assert orch._framedump_permanent_stop == "low_disk"


def test_a_burst_of_writer_exceptions_backs_off_then_gives_up_with_a_reason(tmp_path, monkeypatch):
    orch = _bare_orchestrator(tmp_path, max_error_streak=3)
    orch._framedump_disk_status = lambda: (True, 50 * _GIB, 10 * _GIB, "")
    monkeypatch.setattr(rpo.cv2, "imwrite",
                        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("boom")))

    assert orch._framedump_write_item((0, _frame(), _info())) is True
    assert orch._framedump_write_item((1, _frame(), _info())) is True
    assert orch._framedump_write_item((2, _frame(), _info())) is False
    assert orch._framedump_permanent_stop == "writer_error"


# ------------------------------------------------------------------ heartbeat


def test_heartbeat_reports_the_dump_once_a_minute_with_the_stall_age(tmp_path, caplog):
    orch = _bare_orchestrator(tmp_path, heartbeat_s=60.0)
    orch._framedump_count = 2096
    orch._framedump_last_write_ts = 1_000.0

    with caplog.at_level(logging.WARNING, logger=rpo.logger.name):
        orch._framedump_heartbeat(1_000.0)      # first call only seeds the timer
        assert not [r for r in caplog.records if r.getMessage().startswith("FRAMEDUMP: frames=")]
        orch._framedump_heartbeat(1_059.0)      # inside the interval: still silent
        assert not [r for r in caplog.records if r.getMessage().startswith("FRAMEDUMP: frames=")]
        orch._framedump_heartbeat(1_061.0)

    [line] = [r for r in caplog.records if r.getMessage().startswith("FRAMEDUMP: frames=")]
    assert line.levelno >= logging.ERROR
    message = line.getMessage()
    assert "frames=2096" in message
    assert "last_write_age_s=61.0" in message
    assert "skipped=0" in message
    assert str(tmp_path) in message


def test_a_stopped_dump_keeps_reporting_itself(tmp_path, caplog):
    """The silent hour is the failure. A disabled dump must still say so every minute."""
    orch = _bare_orchestrator(tmp_path)
    orch._framedump_disk_status = lambda: (False, 1 * _GIB, 10 * _GIB, "")
    orch._framedump_write_item((0, _frame(), _info()))
    orch._framedump_heartbeat_ts = 1_000.0

    with caplog.at_level(logging.WARNING, logger=rpo.logger.name):
        orch._dump_frame(_frame(), _result())          # capture is still live
        orch._framedump_heartbeat_ts = 1_000.0
        rpo.time.perf_counter()
        orch._framedump_heartbeat(1_100.0)

    states = [r.getMessage() for r in caplog.records if "state=" in r.getMessage()]
    assert any("state=disabled:low_disk" in m for m in states)


# ------------------------------------------------------- stop/start life cycle


def test_stop_then_start_re_arms_the_writer_instead_of_dying_silently(tmp_path, caplog):
    """A capture restart used to end the dump permanently, with zero log lines."""
    orch = _bare_orchestrator(tmp_path)
    # This test exercises writer lifecycle, not the host's remaining disk space.
    orch._framedump_disk_status = lambda: (True, 50 * _GIB, 10 * _GIB, "")
    orch._framedump_writer = threading.Thread(target=lambda: None)
    orch._framedump_writer.start()
    orch._framedump_writer.join()

    with caplog.at_level(logging.WARNING, logger=rpo.logger.name):
        assert orch._stop_framedump_writer(timeout=0.1) is True
        assert orch._framedump_enabled is False
        assert orch._framedump_q is None
        assert orch._start_framedump() is True

    assert orch._framedump_enabled is True
    assert orch._framedump_q is not None
    assert orch._framedump_writer.is_alive()
    assert not orch._framedump_writer_stop.is_set()
    assert orch._framedump_generation == 2
    messages = [r.getMessage() for r in caplog.records]
    assert any("writer stopped" in m for m in messages)
    assert any("re-armed" in m for m in messages)
    orch._stop_framedump_writer(timeout=1.0)


def test_re_arm_is_refused_after_a_permanent_fault(tmp_path):
    orch = _bare_orchestrator(tmp_path)
    orch._framedump_disable("output_directory_unavailable", detail="no such directory")

    assert orch._start_framedump() is False
    assert orch._framedump_enabled is False


def test_live_writer_thread_survives_a_slow_write_and_keeps_dumping(tmp_path, monkeypatch):
    """End-to-end on the REAL writer thread: stall, cool down, resume, more PNGs on disk."""
    orch = _bare_orchestrator(tmp_path, backoff_max_s=0.05, heartbeat_s=0.0)
    orch._framedump_writer = None
    orch._framedump_q = None
    orch._framedump_disk_status = lambda: (True, 50 * _GIB, 10 * _GIB, "")
    real_imwrite = rpo.cv2.imwrite
    stalls = {"left": 1}

    def imwrite(path, image, *a, **kw):
        if stalls["left"]:
            stalls["left"] -= 1
            time.sleep(orch._framedump_max_write_s + 0.05)
        return real_imwrite(path, image, *a, **kw)

    monkeypatch.setattr(rpo.cv2, "imwrite", imwrite)
    orch._framedump_interval = 0.01
    assert orch._start_framedump() is True
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and orch._framedump_count < 12:
            orch._dump_frame(_frame(), _result())
            time.sleep(0.005)
        live = (orch._framedump_enabled, orch._framedump_permanent_stop,
                orch._framedump_slow_streak, orch._framedump_skipped)
        written = sorted(p.name for p in tmp_path.glob("f*_raw.png"))
    finally:
        orch._stop_framedump_writer(timeout=2.0)

    enabled, permanent_stop, slow_streak, skipped = live
    assert slow_streak == 0                  # the stall was transient and cleared itself
    assert enabled is True                   # ...and never became permanent
    assert permanent_stop == ""
    assert skipped > 0                       # frames WERE skipped during the cooldown
    assert len(written) >= 5, written        # and the dump kept producing pixels afterwards
    assert (tmp_path / "frames.csv").exists()


def test_the_frame_dump_is_never_opted_in_without_the_env_gate(tmp_path):
    orch = _bare_orchestrator(tmp_path)
    orch._framedump_env_enabled = False
    orch._framedump_enabled = False

    assert orch._start_framedump() is False
    assert orch._framedump_writer is None
