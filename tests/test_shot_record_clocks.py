"""Diagnostic clock/proposal regressions; no real input, capture device, or console."""
import math
from types import SimpleNamespace

import pytest
import remote_play_orchestrator as rpo
from shot_records import ShotRecorder


def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(rpo.time, "time", lambda: 1.2495)
    monkeypatch.setattr(rpo.time, "perf_counter", lambda: 10.2495)
    reader = SimpleNamespace(
        _base=SimpleNamespace(pickup=None, stats={}),
        notify_physical_shot_start=lambda *a: None,
        notify_physical_shot_type=lambda *a: True,
        notify_physical_shot_release=lambda *a, **kw: True,
        set_shot_state=lambda *a: None,
    )
    o = rpo.RemotePlayOrchestrator.__new__(rpo.RemotePlayOrchestrator)
    o.__dict__.update(
        _meter_detector=reader, _frame_seq=100, _shot_gate_arm_frames=2400,
        _shot_gate_max_seconds=20.0, _shot_gate_deadline_seq=-1,
        _shot_gate_hw_deadline_seq=-1, _shot_gate_deadline_monotonic=-1.0,
        _shot_gate_hw_deadline_monotonic=-1.0, _shot_gate_source="",
        _shot_gate_epoch=0, _shot_gate_edge_pending=False, _banner_verdict=None,
        _framedump_press_window=False, _framedump_env_enabled=False,
        _shot_record_onset_epoch=0, _shot_record_onset_done=False,
        _shot_record_icon=False, _shot_record_icon_next=0.0,
        _shot_records=ShotRecorder(str(tmp_path / "records.jsonl"), start_thread=False,
                                   clock=lambda: 1249.5),
    )
    return o


def sample(epoch=17, fill=14.0):
    return SimpleNamespace(detected=True, fill_pct=fill, gameplay_sample_epoch=epoch,
                           gameplay_structure_verified=True, gameplay_structure_epoch=epoch)


def test_delayed_arm_preserves_physical_hold_and_same_clock_onset(tmp_path, monkeypatch):
    o = harness(tmp_path, monkeypatch)
    assert o.arm_shot_gate("square", 17, "Standstill", False, press_ms=1000.0)
    o._shot_record_frame_hook(None, sample(), 10.4844, frame_seq=100)
    assert o.release_shot_gate(17, 1646.7)
    row = o._shot_records._open[17]
    assert row["press_ts_ms"] == 1000.0
    assert row["press_mono_ms"] == pytest.approx(10000.0)
    assert row["press_receipt_ts_ms"] == 1249.5
    assert row["arm_delivery_ms"] == 249.5
    assert row["press_clock"] == "native_edge"
    assert row["release_after_press_ms"] == 646.7
    assert row["onset_ms"] == 484.4
    assert row["onset_ms"] < row["release_after_press_ms"]
    assert row["onset_frame_seq"] == 100 and row["onset_structure_verified"]
    assert row["onset_engine_ms"] is None and row["tempo"] == "unknown"


@pytest.mark.parametrize("stamp", [0, -1, None, "bad", math.nan, math.inf, -math.inf,
                                     1300.0, -40000.0])
def test_missing_invalid_or_future_stamp_never_becomes_a_physical_press(
        tmp_path, monkeypatch, stamp):
    o = harness(tmp_path, monkeypatch)
    assert o.arm_shot_gate("square", 17, "Standstill", False, press_ms=stamp)
    o._shot_record_frame_hook(None, sample(), 10.4844)
    o.release_shot_gate(17, 1646.7)
    row = o._shot_records._open[17]
    assert row["press_ts_ms"] is None and row["press_mono_ms"] is None
    assert row["press_receipt_ts_ms"] == 1249.5
    assert row["release_after_press_ms"] is None and row["onset_ms"] is None


def test_duplicate_type_upgrade_keeps_first_edge_and_accepted_onset(tmp_path, monkeypatch):
    o = harness(tmp_path, monkeypatch)
    o.arm_shot_gate("square", 17, "Standstill", False, press_ms=1000.0)
    o._shot_record_frame_hook(None, sample(), 10.4844)
    o.arm_shot_gate("type_upgrade", 17, "Left Fade", False, press_ms=1200.0)
    row = o._shot_records._open[17]
    assert row["press_ts_ms"] == 1000.0 and row["onset_ms"] == 484.4
    assert row["arm_delivery_ms"] == 249.5 and row["shot_type_upgraded"] == "Left Fade"
    assert not o.arm_shot_gate("square", 16, "Standstill", False, press_ms=1100.0)
    assert o._shot_gate_epoch == 17


@pytest.mark.parametrize("epoch", [0, 16, 18])
def test_other_epoch_completion_never_claims_this_shots_onset(tmp_path, monkeypatch, epoch):
    o = harness(tmp_path, monkeypatch)
    o.arm_shot_gate("square", 17, "Standstill", False, press_ms=1000.0)
    o._shot_record_frame_hook(None, sample(epoch), 10.1)
    assert o._shot_records._open[17]["onset_ms"] is None
    o._shot_record_frame_hook(None, sample(), 10.4844)
    assert o._shot_records._open[17]["onset_ms"] == 484.4


@pytest.mark.parametrize("fill", [-1.0, 101.0, math.nan, math.inf])
def test_invalid_sample_does_not_poison_first_valid_onset(tmp_path, monkeypatch, fill):
    o = harness(tmp_path, monkeypatch)
    o.arm_shot_gate("square", 17, "Standstill", False, press_ms=1000.0)
    o._shot_record_frame_hook(None, sample(fill=fill), 10.1)
    o._shot_record_frame_hook(None, sample(), 10.4844)
    assert o._shot_records._open[17]["onset_ms"] == 484.4


def test_rejected_old_meter_proposal_is_retained_but_never_overwrites_onset(tmp_path, monkeypatch):
    o = harness(tmp_path, monkeypatch)
    o.arm_shot_gate("square", 17, "Standstill", False, press_ms=1000.0)
    rec = o._shot_records
    raw = {"epoch": 17, "first_sight_fill": 97.0, "first_sight_ms_after_press": 0.0}
    assert rec.note_pickup(17, raw)
    assert rec._open[17]["onset_ms"] is None
    o._shot_record_frame_hook(None, sample(), 10.4844)
    assert rec.note_pickup(17, raw)
    assert rec._open[17]["onset_ms"] == 484.4
    assert rec._open[17]["onset_fill"] == 14.0
    assert rec._open[17]["pickup"]["first_sight_fill"] == 97.0


def test_prepress_sample_and_postrelease_tail_are_not_onset(tmp_path, monkeypatch):
    o = harness(tmp_path, monkeypatch)
    o.arm_shot_gate("square", 17, "Standstill", False, press_ms=1000.0)
    o._shot_record_frame_hook(None, sample(), 9.999)
    assert o._shot_records._open[17]["onset_ms"] is None
    o.release_shot_gate(17, 1646.7)
    o._shot_record_frame_hook(None, sample(), 10.700)
    assert o._shot_records._open[17]["onset_ms"] is None


def test_rapid_new_epoch_survives_late_previous_epoch_result(tmp_path, monkeypatch):
    o = harness(tmp_path, monkeypatch)
    o.arm_shot_gate("square", 17, "Standstill", False, press_ms=1000.0)
    o._shot_record_frame_hook(None, sample(), 10.1)
    o.arm_shot_gate("square", 18, "Standstill", False, press_ms=1200.0)
    o._shot_record_frame_hook(None, sample(17), 10.5)
    assert o._shot_records._open[18]["onset_ms"] is None
    o._shot_record_frame_hook(None, sample(18), 10.7)
    assert o._shot_records._open[18]["onset_ms"] == 500.0


def test_dense_csv_uses_explicit_measurement_destination(tmp_path, monkeypatch):
    paths = []
    class Sink:
        def __init__(self, path, *args, **kwargs):
            paths.append(path)
        def is_running(self):
            return True
        def snapshot(self):
            return {"accepting": True}
    o = harness(tmp_path, monkeypatch)
    o._detcsv_enabled = True
    monkeypatch.setattr(rpo, "AsyncDiagnosticCsv", Sink)
    path = str(tmp_path / "measurements" / "detframes.csv")
    monkeypatch.setenv("ORION_DETCSV_PATH", path)
    o._start_detcsv()
    assert paths == [path]
    o._start_detcsv()
    assert paths == [path]  # duplicate start does not replace a live writer


def test_raw_pickup_alone_does_not_create_an_accepted_onset(tmp_path):
    rec = ShotRecorder(str(tmp_path / "raw.jsonl"), start_thread=False)
    rec.note_press(16, shot_type="Standstill")
    rec.note_pickup(16, {"first_sight_fill": 97.0, "first_sight_ms_after_press": 0.0})
    assert rec._open[16]["onset_ms"] is None


def test_raw_pickup_cannot_replace_an_existing_accepted_onset(tmp_path):
    rec = ShotRecorder(str(tmp_path / "raw.jsonl"), start_thread=False)
    rec.note_press(16, shot_type="Standstill")
    assert rec.note_onset(16, 546.0, fill=33.9)
    rec.note_pickup(16, {"first_sight_fill": 97.0, "first_sight_ms_after_press": 0.0})
    assert rec._open[16]["onset_ms"] == 546.0


def test_capture_onset_and_reader_delivery_are_distinct(tmp_path, monkeypatch):
    o = harness(tmp_path, monkeypatch)
    o.arm_shot_gate("square", 17, "Standstill", False, press_ms=1000.0)
    o._frame_seq = 999  # capture already advanced while the prior frame was processed
    o._shot_record_frame_hook(None, sample(), 10.55, frame_seq=102, sample_wall_ms=1484.4)
    row = o._shot_records._open[17]
    assert row["onset_ms"] == 484.4 and row["onset_read_ms"] == 550.0
    assert row["onset_frame_seq"] == 102 and row["onset_clock"] == "capture_wall"


@pytest.mark.parametrize("stamp", [math.nan, math.inf, 999.0, 1600.0])
def test_invalid_capture_stamp_does_not_claim_onset(tmp_path, monkeypatch, stamp):
    o = harness(tmp_path, monkeypatch)
    o.arm_shot_gate("square", 17, "Standstill", False, press_ms=1000.0)
    o._shot_record_frame_hook(None, sample(), 10.55, sample_wall_ms=stamp)
    assert o._shot_records._open[17]["onset_ms"] is None
    o._shot_record_frame_hook(None, sample(), 10.55, sample_wall_ms=1484.4)
    assert o._shot_records._open[17]["onset_ms"] == 484.4


def test_lossless_bmp_roundtrip_and_captured_frame_identity(tmp_path, monkeypatch):
    import csv
    import cv2
    import numpy as np
    o = harness(tmp_path, monkeypatch)
    o._framedump_format = 'bmp'
    o._framedump_dir = str(tmp_path)
    frame = np.random.default_rng(123).integers(0, 256, (64, 96, 3), dtype=np.uint8)
    ext, params = o._framedump_encoding()
    assert ext == '.bmp'
    image = tmp_path / ('ep17_f000001_1_raw' + ext)
    assert cv2.imwrite(str(image), frame, params)
    assert np.array_equal(cv2.imread(str(image)), frame)
    context = dict(wall_ms=1484.4, processed_seq=102, frame_no=77, source_pts=987.25,
                   source_epoch_ms=1484.4, source_identity=55, frame_integrity_generation=3)
    info = o._framedump_info(frame, sample(), 10.55, context)
    info.update(epoch=17, filename=image.name)
    context['processed_seq'] = 999  # enqueued snapshot must be immutable to the producer
    o._frame_seq = 1000
    assert o._framedump_write_index(1, info)
    o._framedump_index_fh.close()
    with (tmp_path / 'frames.csv').open(newline='') as f:
        row = list(csv.DictReader(f))[0]
    assert float(row['t_wall']) == 1.4844
    assert row['processed_seq'] == '102' and row['sample_shot_epoch'] == '17'
    assert row['frame_no'] == '77' and row['source_identity'] == '55'
    assert row['frame_w'] == '96' and row['frame_h'] == '64'
    assert row['filename'] == image.name


def test_press_window_initialization_honors_lossless_bmp(tmp_path, monkeypatch):
    o = harness(tmp_path, monkeypatch)
    o._framedump_dir = str(tmp_path)
    monkeypatch.setenv('ORION_FRAMEDUMP_PRESS_WINDOW', '1')
    monkeypatch.setenv('ORION_FRAMEDUMP_FORMAT', 'bmp')
    o._init_framedump_press_window()
    assert o._framedump_format == 'bmp'
    assert o._framedump_encoding() == ('.bmp', [])
