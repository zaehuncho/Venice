"""Observer timestamps stay attached to their frame, never become capture time."""
from types import SimpleNamespace
from pathlib import Path

import pytest
import remote_play_orchestrator as rpo
from test_frame_integrity_pipeline import _bare_integrity_orchestrator, _sidecar_module


def test_completion_clock_is_atomic_and_does_not_replace_capture_clocks(monkeypatch):
    monkeypatch.setattr(rpo.time, 'time', lambda: 1000.12345)
    o = _bare_integrity_orchestrator()
    o._finish_processed_frame(11, 501, False, True, frame_ts=12.5,
                             epoch_ms=123456., measurement_epoch_ms=123436.,
                             pts=99, frame_wh=(1280, 720))
    snapshot = o._processed_frame_snapshot
    assert snapshot.detector_done_epoch_ms == pytest.approx(1000123.45)
    o._last_processed_seq = 999
    o._last_processed_epoch_ms = 999999.
    out = _sidecar_module()._read_processed_frame_snapshot(o)
    assert out['detector_done_epoch_ms'] == snapshot.detector_done_epoch_ms
    assert out['seq'] == 11 and out['frame_number'] == 501
    assert out['epoch_ms'] == 123456. and out['measurement_epoch_ms'] == 123436.
    monkeypatch.setattr(rpo.time, 'time', lambda: 2000.)
    o._note_frame_reject('dark_frame')
    assert o._processed_frame_snapshot.detector_done_epoch_ms == snapshot.detector_done_epoch_ms
    assert not o._processed_frame_snapshot.integrity_healthy


def test_legacy_snapshot_missing_completion_never_fabricates_a_clock():
    out = _sidecar_module()._read_processed_frame_snapshot(
        SimpleNamespace(_processed_frame_snapshot=SimpleNamespace(seq=4, epoch_ms=10.)))
    assert out['detector_done_epoch_ms'] == 0.
    assert out['epoch_ms'] == 10.


def test_native_trace_keeps_release_frame_identity_and_follows_delivery():
    src = (Path(__file__).parents[1] / 'native_orion/src/RemotePlaySession.cpp').read_text(encoding='utf-8')
    begin = src.index('const qint64 traceReceiptMs')
    delivery = src.index('emit sidecarDetectionReady(sidecarResult);', begin)
    log = src.index('METER PIPE TRACE:', delivery)
    assert begin < src.index('const bool traceSample', begin) < delivery < log
    assert '.arg(traceEpoch).arg(sidecarResult.frameNumber)' in src[log:log+800]
    assert 'meterPipeTraceSamples_ < 480' in src[begin:delivery]
