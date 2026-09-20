"""Execute the real CSV write expression with capture advancing during detection.

AST extraction avoids hardware/GUI initialization but executes every production
format argument and the actual write call; this is not a source-text assertion.
"""

import ast
import csv
import io
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def csv_writer_expression():
    source = Path(__file__).resolve().parents[1] / "remote_play_orchestrator.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
    namespace = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and target.id in ("_DETCSV_HEADER", "_DETCSV_ROW_FMT")
            for target in node.targets
        ):
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)
             and node.func.attr == "write"
             and isinstance(node.func.value, ast.Attribute)
             and node.func.value.attr == "_detcsv"]
    assert len(calls) == 1, "Keep this test attached to the sole production row writer"
    expression = compile(ast.Expression(body=calls[0]), str(source), "eval")
    return namespace, expression


def render_row(writer, captured_number, captured_pts, live_number, live_pts):
    namespace, expression = writer
    stream = io.StringIO()
    frame_time = 1_234_567.875
    instance = SimpleNamespace(
        _detcsv=stream, _detcsv_t0=1.0,
        _last_decoded_frame_number=live_number, _last_pts=live_pts,
        _last_frame_is_iframe=0,
        _should_feed_engine=lambda result: True,
        _is_raw_accepted=lambda result: True,
    )
    values = dict(namespace, self=instance,
                  time=SimpleNamespace(perf_counter=lambda: 3.0),
                  result=SimpleNamespace(detected=True, fill_pct=42.5, confidence=.9,
                                         fill_estimator_mode="subpixel",
                                         fill_estimator_generation=7),
                  frame=SimpleNamespace(shape=(720, 1280, 3)),
                  _bb2=(10, 20, 30, 40), _dbg={},
                  _frame_wall_ms=frame_time,
                  _snap_source_frame_number=captured_number,
                  _snap_frame_pts=captured_pts,
                  _snap_latency_estimator=None,
                  _snap_integrity_generation=1180,
                  _snap_seq=44, _snap_frame_epoch_ms=frame_time,
                  _snap_source_identity=51, _snap_backend_frozen=False)
    eval(expression, values)
    [row] = list(csv.DictReader(io.StringIO(namespace["_DETCSV_HEADER"] + "\n" + stream.getvalue())))
    assert None not in row and None not in row.values(), "CSV width remains schema-compatible"
    assert float(row["wall_ms"]) == frame_time
    assert float(row["t_ms"]) == 2000.0
    assert int(row["frame_integrity_generation"]) == 1180
    return row


@pytest.mark.parametrize("live_number,live_pts", [(18, 18_000), (0, 0), (99, 99_000)])
def test_csv_uses_captured_frame_identity_not_newer_or_reset_live_state(
    csv_writer_expression, live_number, live_pts
):
    row = render_row(csv_writer_expression, 17, 17_000, live_number, live_pts)
    assert int(row["frame_no"]) == 17
    assert int(row["pts"]) == 17_000


def test_csv_retains_unavailable_captured_identity_instead_of_borrowing_live_state(csv_writer_expression):
    row = render_row(csv_writer_expression, 0, 0, 20, 20_000)
    assert int(row["frame_no"]) == 0
    assert int(row["pts"]) == 0


def test_csv_two_distinct_snapshots_do_not_repeat_a_live_frame_number(csv_writer_expression):
    rows = [render_row(csv_writer_expression, number, number * 1000, 18, 18_000)
            for number in (17, 18)]
    assert [int(row["frame_no"]) for row in rows] == [17, 18]
    assert [int(row["pts"]) for row in rows] == [17_000, 18_000]


def test_csv_identity_does_not_consult_unrelated_mutable_fields(csv_writer_expression):
    row = render_row(csv_writer_expression, 17, 17_000, "not-a-number", "not-a-pts")
    assert int(row["frame_no"]) == 17
    assert int(row["pts"]) == 17_000
