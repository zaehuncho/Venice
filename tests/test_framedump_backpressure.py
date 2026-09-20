"""Framedump diagnostics must yield to the live capture path."""

import queue
import threading
from types import SimpleNamespace

import numpy as np

import remote_play_orchestrator as rpo


_GIB = 1024 ** 3


def _bare_orchestrator(tmp_path):
    orch = rpo.RemotePlayOrchestrator.__new__(rpo.RemotePlayOrchestrator)
    orch._framedump_enabled = True
    orch._framedump_disabled_reason = ""
    orch._framedump_dir = str(tmp_path)
    orch._framedump_count = 0
    orch._framedump_max = 100
    orch._framedump_interval = 0.0
    orch._framedump_last_ts = 0.0
    orch._framedump_armed = True
    orch._framedump_dropped = 0
    orch._framedump_reported_dropped = 0
    orch._framedump_queue_depth = 1
    orch._framedump_q = queue.Queue(maxsize=1)
    orch._framedump_writer = None
    orch._framedump_writer_stop = threading.Event()
    orch._framedump_raw_only = True
    orch._framedump_min_free_bytes = 10 * _GIB
    orch._framedump_min_free_pct = 2.0
    orch._framedump_max_duty = 0.25
    orch._framedump_max_write_s = 0.250
    orch._meter_detector = None
    return orch


class _CopySpy:
    def __init__(self):
        self.copies = 0

    def copy(self):
        self.copies += 1
        return np.zeros((8, 8, 3), dtype=np.uint8)


def _result():
    return SimpleNamespace(
        gameplay_eligible=True,
        detected=True,
        fill_pct=42.0,
        confidence=0.9,
        rejection_reason="",
        green_window_center_pct=95.0,
        green_window_confidence=0.8,
        bbox=(1, 2, 3, 4),
        green_window_start_row=-1,
        green_window_end_row=-1,
    )


def _info():
    return {
        "t": 10.0,
        "wall": 1_000.0,
        "det": True,
        "fill": 42.0,
        "conf": 0.9,
        "rej": "",
        "gc": 95.0,
        "gconf": 0.8,
        "bbox": (1, 2, 3, 4),
        "gs": -1,
        "ge": -1,
        "park": False,
    }


def test_full_queue_drops_before_copying_full_resolution_frame(tmp_path):
    orch = _bare_orchestrator(tmp_path)
    orch._framedump_q.put_nowait(object())
    frame = _CopySpy()

    orch._dump_frame(frame, _result())

    assert frame.copies == 0
    assert orch._framedump_dropped == 1
    assert orch._framedump_count == 0
    assert orch._framedump_last_ts == 0.0


def test_disk_floor_uses_larger_of_absolute_and_volume_percentage(tmp_path, monkeypatch):
    orch = _bare_orchestrator(tmp_path)
    # Two percent of 1 TiB is 20.48 GiB, larger than the absolute 10 GiB floor.
    monkeypatch.setattr(
        rpo.shutil, "disk_usage",
        lambda _path: SimpleNamespace(total=1024 * _GIB, used=1009 * _GIB, free=15 * _GIB),
    )

    ok, free_bytes, floor_bytes, error = orch._framedump_disk_status()

    assert not ok
    assert free_bytes == 15 * _GIB
    assert floor_bytes > 20 * _GIB
    assert error == ""


def test_low_disk_disables_before_any_png_write(tmp_path, monkeypatch):
    orch = _bare_orchestrator(tmp_path)
    orch._framedump_disk_status = lambda: (False, 5 * _GIB, 10 * _GIB, "")

    def unexpected_write(*_args, **_kwargs):
        raise AssertionError("PNG writer must not run below the disk floor")

    monkeypatch.setattr(rpo.cv2, "imwrite", unexpected_write)

    keep_running = orch._framedump_write_item(
        (0, np.zeros((8, 8, 3), dtype=np.uint8), _info()))

    assert not keep_running
    assert not orch._framedump_enabled
    assert orch._framedump_disabled_reason == "low_disk"


def test_failed_raw_write_does_not_create_csv_index_row(tmp_path, monkeypatch):
    orch = _bare_orchestrator(tmp_path)
    orch._framedump_disk_status = lambda: (True, 50 * _GIB, 10 * _GIB, "")
    indexed = []
    orch._framedump_write_index = lambda idx, info: indexed.append((idx, info))
    monkeypatch.setattr(rpo.cv2, "imwrite", lambda *_args, **_kwargs: False)

    keep_running = orch._framedump_write_item(
        (7, np.zeros((8, 8, 3), dtype=np.uint8), _info()))

    assert not keep_running
    assert indexed == []
    assert orch._framedump_disabled_reason == "raw_png_write_failed"


def test_writer_cooldown_caps_average_duty_cycle():
    # 50 ms of PNG work at a 25% duty limit must yield for 150 ms off-thread.
    assert rpo.RemotePlayOrchestrator._framedump_cooldown_s(0.050, 0.25) == 0.15000000000000002

