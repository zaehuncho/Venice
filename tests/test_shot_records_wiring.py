"""The orchestrator's half of the shot record: the sinks, the press edges, the join.

`tests/test_shot_records.py` proves the assembler; this proves the SIDECAR actually feeds
it -- the shot-gate edges, the tee on the reader's `release_oracle` line, the tee on the
banner reader's `banner_verdict` line, the PICKUP read, and that the press-window frame
files name the same epoch the JSONL is keyed on.
"""

import json
import os
import threading
from types import SimpleNamespace

import pytest

import remote_play_orchestrator as rpo
from shot_records import ShotRecorder


class _Reader:
    """Just enough of AsyncMeterLocator/SimpleMeterReader for the hooks under test."""

    def __init__(self, pickup=None, stats=None):
        self._base = SimpleNamespace(pickup=pickup, stats=stats or {"anchor_patch_hit": 7})
        self.released = []
        self.armed = []

    # the orchestrator's guarded reader hooks
    def notify_physical_shot_start(self, epoch=0):
        self.armed.append(epoch)

    def notify_physical_shot_type(self, epoch, shot_type, rhythm):
        return True

    def notify_physical_shot_release(self, epoch, release_ms=None, reason=""):
        self.released.append((epoch, release_ms, reason))
        return True

    def set_shot_state(self, *a, **kw):
        return None


def _orch(tmp_path, reader=None):
    o = rpo.RemotePlayOrchestrator.__new__(rpo.RemotePlayOrchestrator)
    o._meter_detector = reader
    o._frame_seq = 0
    o._shot_gate_arm_frames = 2400
    o._shot_gate_max_seconds = 20.0
    o._shot_gate_deadline_seq = -1
    o._shot_gate_hw_deadline_seq = -1
    o._shot_gate_deadline_monotonic = -1.0
    o._shot_gate_hw_deadline_monotonic = -1.0
    o._shot_gate_source = ""
    o._shot_gate_epoch = 0
    o._shot_gate_edge_pending = False
    o._banner_verdict = None
    o._framedump_press_window = False
    o._framedump_env_enabled = False
    o._shot_record_onset_epoch = 0
    o._shot_record_onset_done = False
    o._shot_record_icon = False
    o._shot_record_icon_next = 0.0
    o._shot_records = ShotRecorder(str(tmp_path / "s.jsonl"), session="s",
                                   start_thread=False)
    return o


def _rows(o):
    o._shot_records.drain()
    if not os.path.exists(o._shot_records.path):
        return []
    with open(o._shot_records.path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


PICKUP = {"epoch": 4242, "first_sight_fill": 14.0, "first_sight_ms_after_press": 468.0,
          "anchor_used": 1, "anchor_conf": 0.7, "refused_outside_patch": 0,
          "expect_accept": 1, "anchor_ms": 0.3, "logged": 1}


def test_arm_release_and_pickup_produce_one_joined_record(tmp_path, monkeypatch):
    reader = _Reader(pickup=dict(PICKUP))
    o = _orch(tmp_path, reader)
    emitted = []
    monkeypatch.setattr(rpo, "emit_stdout_jsonl", emitted.append)

    o.arm_shot_gate("square", 4242, "Standstill", False, press_ms=rpo.time.time() * 1000.0 - 1.0)
    assert reader.armed == [4242]
    # the reader's release hook is what flushes PICKUP, so the orchestrator reads it after
    o.release_shot_gate(4242, 1234.5)
    assert reader.released and reader.released[0][0] == 4242

    # the reader's release_oracle line, through the orchestrator's tee
    o._shot_record_oracle_sink(json.dumps({
        "event": "release_oracle", "release_seq": 4242, "gap_px": 2.4, "gap_pct": 2.0,
        "settled_fill": 90.2, "green_bottom_pct": 91.0, "verdict_proxy": "green"}) + "\n")
    # the banner reader's verdict line, through the same kind of tee
    o._shot_record_banner_sink(json.dumps({
        "event": "banner_verdict", "timing": "EXCELLENT", "timing_color": "green",
        "green": True, "coverage": "OPEN", "ncc": 0.97, "seq": 3, "attributed": 1,
        "release_seq": 4242, "release_delay_ms": 1180.0, "onset_ms": 9.0,
        "emit_latency_ms": 110.0}) + "\n")

    rows = _rows(o)
    assert len(rows) == 1
    row = rows[0]
    assert row["epoch"] == 4242
    assert row["source"] == "square" and row["shot_type"] == "Standstill"
    assert row["pickup"]["first_sight_fill"] == 14.0
    assert row["pickup"]["patch_hits"] == 7          # read off the locator's stats
    assert row["onset_ms"] is None and row["tempo"] == "unknown"
    assert row["oracle"]["gap_px"] == 2.4
    assert row["banner"]["timing"] == "EXCELLENT"
    assert row["outcome"] == "released" and row["closed_reason"] == "complete"
    # BOTH tees forwarded the identical bytes to the native, unconditionally
    assert len(emitted) == 2
    assert json.loads(emitted[0])["release_seq"] == 4242
    assert json.loads(emitted[1])["timing"] == "EXCELLENT"


def test_a_pickup_for_another_press_is_never_borrowed(tmp_path):
    reader = _Reader(pickup=dict(PICKUP, epoch=9999))
    o = _orch(tmp_path, reader)
    o.arm_shot_gate("square", 4242, "Standstill", False, press_ms=rpo.time.time() * 1000.0 - 1.0)
    o.release_shot_gate(4242, 0.0)
    o._shot_records.close_all("t")
    row = _rows(o)[0]
    assert row["pickup"] is None and row["onset_ms"] is None


def test_disarm_closes_the_record_as_disarmed(tmp_path):
    reader = _Reader(pickup=None)
    o = _orch(tmp_path, reader)
    o.arm_shot_gate("square", 77, "Standstill", False, press_ms=rpo.time.time() * 1000.0 - 1.0)
    o.disarm_shot_gate(77, "manual_cancel")
    o._shot_records.close_all("t")
    row = _rows(o)[0]
    assert row["outcome"] == "disarmed" and row["disarm_reason"] == "manual_cancel"


def test_detect_loop_hook_stamps_the_fallback_onset(tmp_path):
    o = _orch(tmp_path, _Reader(pickup=None))
    o.arm_shot_gate("square", 5, "Standstill", False, press_ms=rpo.time.time() * 1000.0 - 1.0)
    press_mono = o._shot_record_press_mono_ms(5)
    assert press_mono is not None
    # two frames: the first with no meter, the second with one -> that is the onset
    o._shot_record_frame_hook(None, SimpleNamespace(detected=False, fill_pct=0.0),
                              (press_mono + 200.0) / 1000.0)
    o._shot_record_frame_hook(None, SimpleNamespace(detected=True, fill_pct=11.0, gameplay_sample_epoch=o._shot_gate_epoch),
                              (press_mono + 540.0) / 1000.0)
    # a later detection must not move it
    o._shot_record_frame_hook(None, SimpleNamespace(detected=True, fill_pct=60.0, gameplay_sample_epoch=o._shot_gate_epoch),
                              (press_mono + 900.0) / 1000.0)
    o._shot_records.close_all("t")
    row = _rows(o)[0]
    assert row["onset_source"] == "detect_loop"
    assert row["onset_ms"] == pytest.approx(540.0, abs=1.0)
    assert row["onset_fill"] == 11.0
    # 540 (capture clock) + 36 (engine accept lag) = 576, still inside the 500..580 band
    assert row["tempo_estimate"] == "normal" and row["tempo"] == "unknown"


def test_detect_loop_onset_crosses_the_engine_threshold_correctly(tmp_path):
    o = _orch(tmp_path, _Reader(pickup=None))
    o.arm_shot_gate("square", 6, "Standstill", False, press_ms=rpo.time.time() * 1000.0 - 1.0)
    press_mono = o._shot_record_press_mono_ms(6)
    o._shot_record_frame_hook(None, SimpleNamespace(detected=True, fill_pct=11.0, gameplay_sample_epoch=o._shot_gate_epoch),
                              (press_mono + 460.0) / 1000.0)
    o._shot_records.close_all("t")
    row = _rows(o)[0]
    # 460 (capture clock) + 36 (engine accept lag) = 496 -> QUICK, not normal
    assert row["onset_engine_estimate_ms"] == pytest.approx(496.0, abs=1.0)
    assert row["onset_engine_ms"] is None
    assert row["tempo_estimate"] == "quick" and row["tempo"] == "unknown"


def test_press_window_files_join_the_jsonl_by_epoch(tmp_path, monkeypatch):
    """The frame filenames carry the same epoch the record is keyed on."""
    import queue

    import numpy as np

    gib = 1024 ** 3
    monkeypatch.setattr(rpo.shutil, "disk_usage", lambda _path:
                        SimpleNamespace(total=100 * gib, used=gib, free=99 * gib))

    monkeypatch.setenv("ORION_FRAMEDUMP_PRESS_WINDOW", "1")
    monkeypatch.setenv("ORION_FRAMEDUMP_DIR", str(tmp_path))
    o = _orch(tmp_path, _Reader(pickup=dict(PICKUP, epoch=31)))
    o._init_framedump_press_window()
    o._framedump_dir = str(tmp_path)
    o._framedump_env_enabled = True
    o._framedump_enabled = True
    o._framedump_disabled_reason = ""
    o._framedump_permanent_stop = ""
    o._framedump_count = 0
    o._framedump_max = 1000
    o._framedump_interval = 0.2
    o._framedump_last_ts = 0.0
    o._framedump_armed = True
    o._framedump_dropped = 0
    o._framedump_reported_dropped = 0
    o._framedump_skipped = 0
    o._framedump_raw_only = True
    o._framedump_min_free_bytes = 1
    o._framedump_min_free_pct = 0.5
    o._framedump_max_duty = 0.25
    o._framedump_max_write_s = 5.0
    o._framedump_max_error_streak = 5
    o._framedump_backoff_until = 0.0
    o._framedump_backoff_s = 0.0
    o._framedump_slow_streak = 0
    o._framedump_error_streak = 0
    o._framedump_last_write_ms = 0.0
    o._framedump_last_write_ts = 0.0
    o._framedump_heartbeat_ts = 0.0
    o._framedump_heartbeat_s = 0.0
    o._framedump_index_failed = False
    o._framedump_generation = 1
    o._framedump_writer_stop = threading.Event()
    o._framedump_q = queue.Queue(maxsize=o._framedump_queue_depth)

    o.arm_shot_gate("square", 31, "Standstill", False, press_ms=rpo.time.time() * 1000.0 - 1.0)
    assert o._framedump_press_epoch == 31
    for i in range(6):
        o._dump_frame(np.zeros((16, 16, 3), dtype=np.uint8),
                      SimpleNamespace(detected=True, fill_pct=10.0, confidence=0.9,
                                      rejection_reason="", green_window_center_pct=95.0,
                                      green_window_confidence=0.8, bbox=(1, 2, 3, 4),
                                      green_window_start_row=-1, green_window_end_row=-1),
                      now=100.0 + i / 60.0)
    # drain the writer inline (no thread in this test)
    while not o._framedump_q.empty():
        o._framedump_write_item(o._framedump_q.get_nowait())
    fh = getattr(o, "_framedump_index_fh", None)
    if fh is not None:
        fh.close()
        o._framedump_index_fh = None

    o.release_shot_gate(31, 0.0)
    o._framedump_close_press_stats("test")
    o._shot_records.close_all("t")
    row = _rows(o)[0]
    files = [p for p in os.listdir(str(tmp_path)) if p.endswith(".jpg")]
    assert files, "press-window mode wrote no frames"
    assert all(f.startswith("ep31_f") for f in files)
    assert row["epoch"] == 31
    assert row["framedump"]["frames"] == len(files)
    assert row["framedump"]["dropped"] == 0
    assert row["framedump"]["first_idx"] == 0


# --------------------------------------------------------------------------- #
# [ORION_SHOT_RANGE 2026-09-17] The orchestrator's half of the range reader.
# --------------------------------------------------------------------------- #
def _now():
    import time as _t
    return _t.perf_counter()


def _range_orch(tmp_path, monkeypatch):
    import shot_range
    o = _orch(tmp_path, _Reader(pickup=dict(PICKUP)))
    lines = []
    o._shot_range = shot_range.ShotRangeReader(
        classifier=shot_range.ShotRangeClassifier(calibrated=True),
        emit=lines.append, plate_fn=lambda: (300.0, 400.0, 1.0, _now()), 
        on_result=o._shot_range_result, start_thread=False)
    return o, lines


def _icon_frame():
    import numpy as np
    import shot_range
    img = np.full((720, 1280, 3), 200, np.uint8)
    x0, y0, x1, y1 = shot_range.cell_box(300.0, 400.0, 1.0, 1280, 720)
    img[y0:y1, x0:x1] = 10
    img[y0 + (y1 - y0) // 4:y0 + 3 * (y1 - y0) // 4,
        x0 + (x1 - x0) // 4:x0 + 3 * (x1 - x0) // 4] = 250
    return img


def test_the_press_edge_opens_the_range_window_and_the_record_gets_it(tmp_path, monkeypatch):
    o, lines = _range_orch(tmp_path, monkeypatch)
    monkeypatch.setattr(rpo, "emit_stdout_jsonl", lambda line: None)
    o.arm_shot_gate("square", 4242, "Left Fade", False, press_ms=rpo.time.time() * 1000.0 - 1.0)
    assert o._shot_range._epoch == 4242
    press_s = o._shot_range._press_mono_ms / 1000.0
    frame = _icon_frame()
    for dt in (0.050, 0.080, 0.110):
        o._shot_range.note_frame(frame, press_s + dt)
    o._shot_range._worker_drain()
    o.release_shot_gate(4242, 900.0)
    o._shot_records.close_all("t")
    row = _rows(o)[0]
    assert row["range"] == "three"
    assert row["range_source"] == "anchor"
    assert len(row["range_cells"]) == 3
    assert json.loads(lines[0])["release_seq"] == 4242


def test_a_press_that_never_fills_its_window_is_flushed_by_the_close(tmp_path, monkeypatch):
    o, lines = _range_orch(tmp_path, monkeypatch)
    monkeypatch.setattr(rpo, "emit_stdout_jsonl", lambda line: None)
    o.arm_shot_gate("square", 77, "Right Fade", False, press_ms=rpo.time.time() * 1000.0 - 1.0)
    o.disarm_shot_gate(77, "manual_cancel")
    o._shot_range._worker_drain()
    assert json.loads(lines[0])["range"] == "unknown"
    assert json.loads(lines[0])["samples"] == 0


def test_a_missing_range_reader_costs_the_press_nothing(tmp_path, monkeypatch):
    o = _orch(tmp_path, _Reader(pickup=dict(PICKUP)))
    o._shot_range = None
    monkeypatch.setattr(rpo, "emit_stdout_jsonl", lambda line: None)
    o.arm_shot_gate("square", 5, "Standstill", False, press_ms=rpo.time.time() * 1000.0 - 1.0)
    o.release_shot_gate(5, 700.0)
    o._shot_records.close_all("t")
    assert _rows(o)[0]["range"] == "unknown"


class _NestedPickupReader(_Reader):
    """Production shape: SimpleMeterReader -> AsyncMeterLocator -> proposer."""

    def __init__(self, pickup=None, stats=None):
        super().__init__(pickup=pickup, stats=stats)
        self._meter_detector = SimpleNamespace(_base=self._base)
        del self._base


@pytest.mark.parametrize("reader_type", [_Reader, _NestedPickupReader])
def test_pickup_read_supports_explicit_reader_layouts_without_mutation(tmp_path, reader_type):
    pickup = dict(PICKUP)
    reader = reader_type(pickup=pickup)
    o = _orch(tmp_path, reader)
    result = o._read_pickup_record(4242)
    assert result is not None
    assert result["first_sight_fill"] == 14.0
    assert result["patch_hits"] == 7
    result["first_sight_fill"] = 99.0
    assert pickup == PICKUP                 # diagnostics never write to the locator
    assert reader.armed == reader.released == []


@pytest.mark.parametrize("close_kind", ["release", "disarm"])
def test_nested_pickup_survives_terminal_join_without_overriding_accepted_onset(tmp_path, close_kind):
    # Epoch 13's locator saw 61% at +815.8 ms; the first published sample arrived later.
    # The diagnostic must retain both distinctions instead of silently losing PICKUP.
    pickup = dict(PICKUP, epoch=13, first_sight_fill=61.0,
                  first_sight_ms_after_press=815.8)
    reader = _NestedPickupReader(pickup=pickup)
    o = _orch(tmp_path, reader)
    o.arm_shot_gate("square", 13, "Standstill", False, press_ms=rpo.time.time() * 1000.0 - 1.0)
    o._shot_records.note_onset(13, 870.79, fill=78.0, source="detect_loop")
    if close_kind == "release":
        o.release_shot_gate(13, 1500.0)
    else:
        o.disarm_shot_gate(13, "ownership_proof_incomplete")
    o._shot_records.close_all("t")
    row = _rows(o)[0]
    assert row["pickup"] is not None
    assert row["pickup"]["first_sight_fill"] == 61.0
    assert row["onset_ms"] == 870.79
    assert row["onset_source"] == "detect_loop"
    assert row["banner"] is None             # a locator sample is not a game outcome
    assert row["outcome"] == ("released" if close_kind == "release" else "disarmed")


@pytest.mark.parametrize("requested", [0, True, 4242.0, "04242", "", None])
def test_pickup_rejects_invalid_epoch_even_if_proposer_matches(tmp_path, requested):
    # Invalid/missing identities must never compare equal through the zero sentinel.
    reader = _Reader(pickup=dict(PICKUP, epoch=requested))
    o = _orch(tmp_path, reader)
    assert o._read_pickup_record(requested) is None


@pytest.mark.parametrize("reader_type", [_Reader, _NestedPickupReader])
def test_pickup_preserves_full_uint64_identity(tmp_path, reader_type):
    epoch = (1 << 53) + 7
    o = _orch(tmp_path, reader_type(pickup=dict(PICKUP, epoch=str(epoch))))
    assert o._read_pickup_record(str(epoch))["epoch"] == str(epoch)
    assert o._read_pickup_record(str(epoch + 1)) is None


@pytest.mark.parametrize("reader", [
    None,
    SimpleNamespace(),
    SimpleNamespace(_meter_detector=None),
    SimpleNamespace(_meter_detector=SimpleNamespace()),
    SimpleNamespace(_meter_detector=SimpleNamespace(_base=SimpleNamespace(pickup=[]))),
    _NestedPickupReader(pickup=dict(PICKUP, epoch=9999)),
    SimpleNamespace(_meter_detector=SimpleNamespace(
        _meter_detector=SimpleNamespace(_base=SimpleNamespace(pickup=dict(PICKUP))))),
])
def test_pickup_missing_stale_or_unsupported_layout_is_not_borrowed(tmp_path, reader):
    o = _orch(tmp_path, reader)
    assert o._read_pickup_record(4242) is None


def test_nested_pickup_wrong_epoch_does_not_fall_back_to_outer_record(tmp_path):
    reader = _NestedPickupReader(pickup=dict(PICKUP, epoch=9999))
    reader._base = SimpleNamespace(pickup=dict(PICKUP), stats={})
    o = _orch(tmp_path, reader)
    assert o._read_pickup_record(4242) is None
