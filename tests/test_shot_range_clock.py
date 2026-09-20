"""Range diagnostics must pair each frame with its own plate and clock domain."""
import inspect

import numpy as np
import pytest

from shot_range import ShotRangeReader, cell_box


def make_reader():
    plate = [300.0, 400.0, 1.0, 1_789_756_175.0]
    seen = []
    reader = ShotRangeReader(plate_fn=lambda: plate,
                            on_result=lambda ep, p, c: seen.append((ep, p, c)),
                            start_thread=False)
    reader.note_press(7, 100_000.0)
    frame = np.full((720, 1280, 3), 200, np.uint8)
    x0, y0, x1, y1 = cell_box(*plate[:3], 1280, 720)
    frame[y0:y1, x0:x1] = 10
    frame[y0+5:y1-5, x0+5:x1-5] = 250
    return reader, plate, seen, frame


def offer(reader, frame, mono, epoch):
    # Baseline executes the actual old production call, rather than failing only
    # because the new explicit-domain keyword did not yet exist.
    if 'measurement_epoch_s' in inspect.signature(reader.note_frame).parameters:
        return reader.note_frame(frame, mono, measurement_epoch_s=epoch)
    return reader.note_frame(frame, mono)


def test_epoch_plate_uses_measurement_clock_not_monotonic_press_clock():
    r, plate, seen, frame = make_reader()
    for dt in (.05, .08, .11):
        offer(r, frame, 100+dt, plate[3]+dt)
    r._worker_drain()
    assert seen[0][1]['samples'] == 3
    assert seen[0][1]['source'] == 'anchor'
    assert seen[0][1]['evidence'] == 'three'
    assert seen[0][1]['range'] == 'unknown'
    assert seen[0][1]['reason'] == 'uncalibrated'


def test_worker_never_replaces_frame_plate_with_later_mutable_pose():
    r, plate, seen, frame = make_reader()
    # Legacy same-domain callers also require immutable pose/frame association.
    plate[3] = 100.0
    for dt in (.05, .08, .11):
        r.note_frame(frame, 100+dt)
    plate[:] = [700., 400., 1., 101.0]
    r._worker_drain()
    assert seen[0][1]['samples'] == 3
    assert seen[0][1]['evidence'] == 'three'


@pytest.mark.parametrize('offset', [-2.0, .5, float('nan'), float('inf')])
def test_invalid_or_out_of_age_plate_never_supplies_cells(offset):
    r, plate, seen, frame = make_reader()
    epoch = plate[3]
    plate[3] += offset
    for dt in (.05, .08, .11):
        offer(r, frame, 100+dt, epoch+dt)
    r._worker_drain()
    assert seen[0][1]['samples'] == 0
    assert seen[0][1]['range'] == 'unknown'


@pytest.mark.parametrize('second_dt', [.05, .04])
def test_duplicate_or_regressing_frame_time_cannot_supply_majority(second_dt):
    r, plate, seen, frame = make_reader()
    plate[3] = 100.0
    r.note_frame(frame, 100.05)
    r.note_frame(frame, 100+second_dt)
    r.note_close(7)
    r._worker_drain()
    assert seen[0][1]['samples'] == 1


def test_duplicate_measurement_epoch_is_not_two_independent_cells():
    r, plate, seen, frame = make_reader()
    for dt in (.05, .08, .11):
        offer(r, frame, 100+dt, plate[3]+.05)
    r.note_close(7)
    r._worker_drain()
    assert seen[0][1]['samples'] == 1


@pytest.mark.parametrize('stamp', [0., float('nan'), float('inf')])
def test_missing_measurement_epoch_is_not_replaced_by_wall_now(stamp):
    r, plate, seen, frame = make_reader()
    for dt in (.05, .08, .11):
        offer(r, frame, 100+dt, stamp)
    r.note_close(7)
    r._worker_drain()
    assert seen[0][1]['samples'] == 0


def test_production_diagnostic_call_passes_snapshot_epoch():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'remote_play_orchestrator.py').read_text(encoding='utf-8-sig'))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == 'note_frame'
             and isinstance(node.func.value, ast.Name) and node.func.value.id == '_rng']
    assert len(calls) == 1
    value = next((kw.value for kw in calls[0].keywords
                  if kw.arg == 'measurement_epoch_s'), None)
    assert value is not None
    assert '_frame_wall_ms' in ast.unparse(value)
