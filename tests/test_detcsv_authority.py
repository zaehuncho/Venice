"""Actual reader, CSV expression and sidecar serialization share exact authority IDs."""
import ast
import csv
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import remote_play_orchestrator as orchmod
from simple_meter_reader import SimpleMeterReader


def _repo():
    return next(p for p in Path(orchmod.__file__).resolve().parents
                if (p / "native_orion/backend/autogreen_sidecar.py").exists())


@pytest.fixture(scope="module")
def row_expression():
    path = Path(orchmod.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "write"
             and isinstance(n.func.value, ast.Attribute) and n.func.value.attr == "_detcsv"]
    assert len(calls) == 1
    return compile(ast.Expression(calls[0]), str(path), "eval")


def _render(expression, result):
    stream = io.StringIO()
    obj = SimpleNamespace(_detcsv=stream, _detcsv_t0=1.0,
                          _last_frame_is_iframe=0,
                          _should_feed_engine=orchmod.RemotePlayOrchestrator._should_feed_engine,
                          _is_raw_accepted=orchmod.RemotePlayOrchestrator._is_raw_accepted,
                          # Deliberately unrelated mutable next-frame values.
                          _shot_gate_epoch=999, _last_gameplay_structure_verified=False,
                          _last_gameplay_structure_epoch=999,
                          _last_decoded_frame_number=999, _frame_seq=999)
    context = dict(self=obj, result=result, _DETCSV_ROW_FMT=orchmod._DETCSV_ROW_FMT,
                   time=SimpleNamespace(perf_counter=lambda: 2.0),
                   frame=SimpleNamespace(shape=(720, 1280, 3)),
                   _bb2=(200, 300, 25, 120), _dbg={"stage":"detector_fill"},
                   _frame_wall_ms=10000.125, _snap_source_frame_number=5690,
                   _snap_frame_pts=55500, _snap_latency_estimator=None,
                   _snap_integrity_generation=7, _snap_seq=5678,
                   _snap_frame_epoch_ms=10001.25, _snap_source_identity=8,
                   _snap_backend_frozen=False)
    eval(expression, context)
    [row] = list(csv.DictReader(io.StringIO(orchmod._DETCSV_HEADER + "\n" + stream.getvalue())))
    assert None not in row and None not in row.values()
    return row


def _result(epoch=18, proof=True, reason=""):
    return SimpleNamespace(detected=True, fill_pct=18.36, confidence=1.0, bbox=(200,300,25,120),
                           rejection_reason=reason, gameplay_sample_epoch=epoch,
                           gameplay_structure_verified=proof,
                           gameplay_structure_epoch=epoch if proof else 0)


@pytest.mark.parametrize("epoch", [18, 2**53+1, 2**64-1])
def test_csv_exact_frame_and_authority_identity_ignores_mutable_next_frame(row_expression, epoch):
    row = _render(row_expression, _result(epoch))
    assert row["sample_shot_epoch"] == str(epoch)
    assert row["gameplay_structure_verified"] == "1"
    assert row["gameplay_structure_epoch"] == str(epoch)
    assert row["raw_fed"] == "1"
    assert row["reader_stage"] == "detector_fill"
    assert row["processed_seq"] == "5678" and row["frame_no"] == "5690"
    assert row["source_identity"] == "8" and row["frame_integrity_generation"] == "7"
    assert row["backend_frozen"] == "0"
    assert float(row["source_epoch_ms"]) == 10001.25
    assert float(row["wall_ms"]) == 10000.125


def test_csv_distinguishes_unproven_current_shot_from_wrong_epoch(row_expression):
    row = _render(row_expression, _result(proof=False))
    assert row["sample_shot_epoch"] == "18"
    assert row["gameplay_structure_verified"] == "0"
    assert row["gameplay_structure_epoch"] == "0"
    assert row["raw_fed"] == "1"


def test_csv_raw_eligibility_is_not_the_broader_sampler_feed(row_expression):
    row = _render(row_expression, _result(reason="fill_gated"))
    assert row["fed"] == "1" and row["raw_fed"] == "0"
    # This is raw reader evidence, not a claim of final native authority.
    assert row["gameplay_structure_verified"] == "1"


def test_csv_absent_result_has_no_invented_epoch_or_proof(row_expression):
    row = _render(row_expression, None)
    assert [row[k] for k in ("sample_shot_epoch", "gameplay_structure_verified",
                            "gameplay_structure_epoch", "raw_fed")] == ["0"] * 4


@pytest.mark.parametrize("epoch", [18, 2**53+1, 2**64-1])
def test_real_reader_reports_start_epoch_even_when_no_meter_proof(monkeypatch, epoch):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    reader = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    reader.notify_physical_shot_start(epoch)
    reader.set_shot_state(True, 1.0, True)
    result = reader.detect(np.zeros((720, 1280, 3), np.uint8), ts=1000.0)
    assert result.gameplay_sample_epoch == epoch
    assert not result.gameplay_structure_verified and result.gameplay_structure_epoch == 0


def test_real_reader_diagnostic_epoch_stays_with_frame_when_next_press_interleaves(monkeypatch):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    reader = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    reader.notify_physical_shot_start(18)
    reader.set_shot_state(True, 1.0, True)
    original = reader.read
    def interleaved(frame, ts):
        reader.notify_physical_shot_start(19)
        return original(frame, ts)
    reader.read = interleaved
    result = reader.detect(np.zeros((720, 1280, 3), np.uint8), ts=1000.0)
    assert reader._physical_shot_epoch == 19
    assert result.gameplay_sample_epoch == 18
    assert not result.gameplay_structure_verified and result.gameplay_structure_epoch == 0


def test_existing_sidecar_serialization_preserves_proof_and_exact_uint64():
    path = _repo() / "native_orion/backend/autogreen_sidecar.py"
    spec = importlib.util.spec_from_file_location("_authority_sidecar_test", path)
    sidecar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sidecar)
    epoch = 2**53+1
    snapshot = orchmod._ProcessedFrameSnapshot(seq=5678, frame_number=5690,
        epoch_ms=10001.25, measurement_epoch_ms=10000.125, integrity_healthy=True,
        meter_present=True, raw_fed=True, gameplay_structure_verified=True,
        gameplay_structure_epoch=epoch)
    # Current production snapshot wins over unrelated in-progress mutable state.
    processed = sidecar._read_processed_frame_snapshot(SimpleNamespace(
        _processed_frame_snapshot=snapshot, _last_gameplay_structure_epoch=999))
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    keys = {"gameplay_structure_verified", "gameplay_structure_epoch", "raw_fed", "frame_number"}
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Dict)
             and keys <= {k.value for k in n.keys if isinstance(k, ast.Constant)}
             and any(isinstance(v, ast.Subscript) and isinstance(v.value, ast.Name)
                     and v.value.id == "_processed" for v in ast.walk(n))]
    assert len(nodes) == 1
    pairs = [(k,v) for k,v in zip(nodes[0].keys,nodes[0].values)
             if isinstance(k, ast.Constant) and k.value in keys]
    expr = ast.Expression(ast.Dict(keys=[k for k,v in pairs], values=[v for k,v in pairs]))
    wire = eval(compile(ast.fix_missing_locations(expr),str(path),"eval"), {"_processed":processed,"_safe_uint64":sidecar._safe_uint64})
    assert wire == {"gameplay_structure_verified":True, "gameplay_structure_epoch":str(epoch),
                    "raw_fed":True, "frame_number":5690}
