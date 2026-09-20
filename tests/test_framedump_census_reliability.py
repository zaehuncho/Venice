"""A press census distinguishes accepted work from persisted raw frames."""
import queue
import threading
from types import SimpleNamespace

import numpy as np
import pytest

import remote_play_orchestrator as rpo
from shot_records import ShotRecorder


def make_orch(tmp_path, monkeypatch, depth=8):
    monkeypatch.setenv('ORION_FRAMEDUMP_PRESS_WINDOW', '1')
    monkeypatch.setenv('ORION_FRAMEDUMP_PRESS_PRE_MS', '0')
    orch = rpo.RemotePlayOrchestrator.__new__(rpo.RemotePlayOrchestrator)
    orch._framedump_dir = str(tmp_path)
    orch._init_framedump_press_window()
    orch._framedump_dir = str(tmp_path)
    orch._framedump_env_enabled = True
    orch._framedump_enabled = True
    orch._framedump_disabled_reason = ''
    orch._framedump_permanent_stop = ''
    orch._framedump_count = 0
    orch._framedump_max = 1000
    orch._framedump_dropped = 0
    orch._framedump_reported_dropped = 0
    orch._framedump_skipped = 0
    orch._framedump_raw_only = True
    orch._framedump_press_copy = False
    orch._framedump_max_write_s = 5.
    orch._framedump_max_error_streak = 5
    orch._framedump_max_duty = 0.25
    orch._framedump_writer_stop = threading.Event()
    orch._framedump_writer = None
    orch._framedump_q = queue.Queue(maxsize=depth)
    orch._framedump_disk_status = lambda: (True, 10**12, 0, '')
    orch._meter_detector = None
    now = [1000.]
    rec = ShotRecorder(tmp_path / 'shots.jsonl', clock=lambda: now[0],
                       start_thread=False, wait_for_framedump=True)
    orch._shot_records = rec
    return orch, rec, now


def open_graded(orch, rec, epoch, when=1000.):
    rec.note_press(epoch, ts_ms=when, mono_ms=when, shot_type='Right Fade')
    rec.note_release(epoch, when + 500.)
    rec.note_oracle(epoch, {'gap_px': 1., 'verdict_proxy': 'green'})
    rec.note_banner({'release_seq': epoch, 'attributed': 1,
                     'timing': 'EXCELLENT', 'green': True})
    assert orch.framedump_press_open(epoch, mono_now=when / 1000.)


def offer(orch, n=1):
    result = SimpleNamespace(detected=True, fill_pct=42., confidence=.9)
    for _ in range(n):
        frame = np.zeros((8, 8, 3), dtype=np.uint8)
        info = orch._framedump_info(frame, result, 1.)
        assert orch._framedump_offer(frame, info, 1.)


def close_index(orch):
    fh = getattr(orch, '_framedump_index_fh', None)
    if fh is not None:
        fh.close()
        orch._framedump_index_fh = None


def test_graded_record_waits_for_pending_raw_writes(tmp_path, monkeypatch):
    orch, rec, _ = make_orch(tmp_path, monkeypatch)
    open_graded(orch, rec, 1)
    offer(orch, 2)
    orch._framedump_close_press_stats('window_end')
    assert rec._q.empty(), 'accepted frames must not finalize before writer completion'
    assert rec._open[1]['framedump']['pending'] == 2
    try:
        assert orch._framedump_write_item(orch._framedump_q.get_nowait())
        assert rec._q.empty()
        assert orch._framedump_write_item(orch._framedump_q.get_nowait())
        fd = rec._q.get_nowait()['framedump']
        assert (fd['frames'], fd['saved'], fd['indexed'], fd['pending']) == (2, 2, 2, 0)
        assert fd['census_complete'] is True
        assert len(list(tmp_path.glob('ep1_*_raw.jpg'))) == fd['saved']
    finally:
        close_index(orch)


def test_overlap_discards_follow_owning_epoch_not_current_window(tmp_path, monkeypatch):
    orch, rec, _ = make_orch(tmp_path, monkeypatch)
    open_graded(orch, rec, 1)
    offer(orch, 2)
    open_graded(orch, rec, 2, 2000.)
    offer(orch, 3)
    assert orch._framedump_discard_pending() == 5
    orch._framedump_close_press_stats('session_end')
    records = {}
    while not rec._q.empty():
        record = rec._q.get_nowait()
        records[record['epoch']] = record['framedump']
    assert records[1]['dropped'] == 2, 'old-window losses were charged to the new shot'
    assert records[2]['dropped'] == 3
    for epoch, count in [(1, 2), (2, 3)]:
        assert (records[epoch]['saved'], records[epoch]['discarded'], records[epoch]['pending']) == (0, count, 0)
        assert records[epoch]['census_complete'] is True
    assert orch._framedump_dropped == 5
    assert orch._framedump_press_dropped == 5


def test_shutdown_discard_is_in_final_shot_census(tmp_path, monkeypatch):
    orch, rec, _ = make_orch(tmp_path, monkeypatch)
    open_graded(orch, rec, 1)
    offer(orch, 3)
    orch._framedump_close_press_stats('session_end')
    assert orch._stop_framedump_writer(timeout=0.)
    fd = rec._q.get_nowait()['framedump']
    assert fd['dropped'] == 3
    assert (fd['frames'], fd['saved'], fd['discarded'], fd['pending']) == (3, 0, 3, 0)
    assert fd['census_complete'] is True


def test_pending_census_does_not_extend_existing_grace(tmp_path, monkeypatch):
    orch, rec, now = make_orch(tmp_path, monkeypatch)
    open_graded(orch, rec, 1)
    offer(orch, 1)
    orch._framedump_close_press_stats('window_end')
    assert rec._q.empty()
    now[0] = 1500. + rec.grace_ms + 1.
    rec.tick()
    finished = rec._q.get_nowait()
    assert finished['closed_reason'] == 'grace'
    assert finished['framedump']['census_complete'] is False
    assert finished['framedump']['pending'] == 1
    assert finished['framedump']['saved'] == 0
    orch._framedump_discard_pending()
    assert rec._q.empty(), 'late accounting must not duplicate a closed shot'


def test_full_press_queue_rejects_before_frame_copy(tmp_path, monkeypatch):
    orch, rec, _ = make_orch(tmp_path, monkeypatch, depth=1)
    open_graded(orch, rec, 1)
    offer(orch, 1)
    class CopySpy:
        copies = 0
        def copy(self):
            self.copies += 1
            return np.zeros((8, 8, 3), dtype=np.uint8)
    frame = CopySpy()
    orch._framedump_press_copy = True
    assert not orch._framedump_offer(frame, {'det': True}, 1.)
    assert frame.copies == 0, 'full diagnostic queue performed an unnecessary capture-thread copy'
    assert orch._framedump_dropped == 1
    assert orch._framedump_press_stats['dropped'] == 1


def test_failed_raw_write_is_accounted_and_never_claims_saved(tmp_path, monkeypatch):
    orch, rec, _ = make_orch(tmp_path, monkeypatch)
    open_graded(orch, rec, 1)
    offer(orch, 1)
    orch._framedump_close_press_stats('window_end')
    monkeypatch.setattr(rpo.cv2, 'imwrite', lambda *args, **kwargs: False)
    assert not orch._framedump_write_item(orch._framedump_q.get_nowait())
    fd = rec._q.get_nowait()['framedump']
    assert fd['dropped'] == 1
    assert (fd['saved'], fd['discarded'], fd['pending']) == (0, 1, 0)
    assert fd['census_complete'] is True


def test_shot_record_ignores_an_out_of_order_census_snapshot(tmp_path):
    rec = ShotRecorder(tmp_path / 'shots.jsonl', start_thread=False,
                       wait_for_framedump=True, clock=lambda: 1000.)
    rec.note_press(1, ts_ms=1000., mono_ms=1000.)
    assert rec.note_frames(1, frames=2, saved=2, pending=0,
                           census_revision=3, census_complete=True)
    assert not rec.note_frames(1, frames=2, saved=0, pending=2,
                               census_revision=1, census_complete=False)
    assert rec._open[1]['framedump']['saved'] == 2
    assert rec._open[1]['framedump']['census_complete'] is True


def test_stalled_writer_keeps_partial_census_and_capture_handoff_bounded(tmp_path, monkeypatch):
    orch, rec, _ = make_orch(tmp_path, monkeypatch, depth=3)
    open_graded(orch, rec, 1)
    entered, release = threading.Event(), threading.Event()
    real_write = rpo.cv2.imwrite
    def gated_write(*args, **kwargs):
        entered.set()
        assert release.wait(5.), 'test did not release the synthetic disk stall'
        return real_write(*args, **kwargs)
    monkeypatch.setattr(rpo.cv2, 'imwrite', gated_write)
    orch._framedump_writer = threading.Thread(target=orch._framedump_writer_loop, daemon=True)
    orch._framedump_writer.start()
    try:
        offer(orch, 1)
        assert entered.wait(3.)
        # The writer remains blocked inside imwrite throughout these callbacks.
        orch._framedump_close_press_stats('window_end')
        assert rec._q.empty(), 'a blocked image write was described as a completed census'
        open_graded(orch, rec, 2, 2000.)
        offer(orch, 3)
        for _ in range(100):
            assert not orch._framedump_offer(None, {'det': False}, 2.)
        assert orch._framedump_q.qsize() == 3
        assert orch._framedump_count == 4
        assert orch._framedump_dropped == 100
        orch._framedump_close_press_stats('session_end')
        assert not orch._stop_framedump_writer(timeout=0.)
        # Epoch 2 is fully accounted by dropping its queue, without waiting for
        # the old shot's blocked I/O. Epoch 1 stays explicitly unresolved.
        done = rec._q.get_nowait()
        assert done['epoch'] == 2
        assert done['framedump']['discarded'] == 3
        assert done['framedump']['dropped'] == 103
        assert rec._open[1]['framedump']['pending'] == 1
        release.set()
        orch._framedump_writer.join(3.)
        assert not orch._framedump_writer.is_alive()
        done = rec._q.get_nowait()
        assert done['epoch'] == 1
        assert done['framedump']['saved'] == 1
        assert done['framedump']['dropped'] == 0
        assert rec._q.empty()
    finally:
        release.set()
        if orch._framedump_writer is not None:
            orch._framedump_writer.join(3.)
        close_index(orch)


def test_index_failure_keeps_successful_raw_file_distinct(tmp_path, monkeypatch):
    orch, rec, _ = make_orch(tmp_path, monkeypatch)
    open_graded(orch, rec, 1)
    offer(orch, 1)
    orch._framedump_close_press_stats('window_end')
    orch._framedump_write_index = lambda idx, info: False
    assert orch._framedump_write_item(orch._framedump_q.get_nowait())
    fd = rec._q.get_nowait()['framedump']
    assert (fd['saved'], fd['indexed'], fd['discarded'], fd['pending']) == (1, 0, 0, 0)
    assert len(list(tmp_path.glob('ep1_*_raw.jpg'))) == 1


def test_only_inflight_and_bounded_queue_retain_closed_window_stats(tmp_path, monkeypatch):
    orch, rec, _ = make_orch(tmp_path, monkeypatch, depth=3)
    # A stopped consumer models arbitrarily slow storage without retaining any
    # additional frame/window backlog outside the finite diagnostic queue.
    for epoch in range(1, 101):
        orch.framedump_press_open(epoch, mono_now=float(epoch))
        accepted = orch._framedump_offer(None, {'det': False}, float(epoch))
        assert accepted is (epoch <= 3)
        orch._framedump_close_press_stats('superseded')
        assert orch._framedump_q.qsize() <= 3
    assert orch._framedump_press_stats is None
    assert orch._framedump_count == 3
    assert orch._framedump_dropped == 97
    assert orch._framedump_discard_pending() == 3
    assert orch._framedump_dropped == 100
    assert orch._framedump_q.empty()


def test_old_window_expiry_cannot_detach_concurrently_opened_window(tmp_path, monkeypatch):
    orch, rec, _ = make_orch(tmp_path, monkeypatch)
    open_graded(orch, rec, 1)
    offer(orch, 1)
    reached, resume = threading.Event(), threading.Event()
    real_close = orch._framedump_close_press_stats
    def delayed_close(reason, *args, **kwargs):
        if reason == 'window_end':
            reached.set()
            assert resume.wait(3.), 'expiry barrier was not released'
        return real_close(reason, *args, **kwargs)
    monkeypatch.setattr(orch, '_framedump_close_press_stats', delayed_close)
    expirer = threading.Thread(target=lambda: orch._framedump_press_active(10.), daemon=True)
    expirer.start()
    try:
        assert reached.wait(3.)
        # The detector observed window 1 as expired before this control-thread
        # press. Its deferred expiry must not clear this newly initialized shot.
        assert orch.framedump_press_open(2, mono_now=9.)
        until = orch._framedump_press_until
        resume.set()
        expirer.join(3.)
        assert not expirer.is_alive()
        assert orch._framedump_press_epoch == 2
        assert orch._framedump_press_stats['epoch'] == 2
        assert orch._framedump_press_until == until > 10.
    finally:
        resume.set()
        expirer.join(3.)
        orch._framedump_discard_pending()


def test_accounting_exception_does_not_terminate_image_worker(tmp_path, monkeypatch, caplog):
    orch, rec, _ = make_orch(tmp_path, monkeypatch)
    open_graded(orch, rec, 1)
    offer(orch, 1)
    def broken_accounting(item):
        raise RuntimeError('synthetic diagnostic counter failure')
    monkeypatch.setattr(orch, '_framedump_account_item', broken_accounting, raising=False)
    try:
        assert orch._framedump_write_item(orch._framedump_q.get_nowait())
        assert len(list(tmp_path.glob('ep1_*_raw.jpg'))) == 1
        assert any('census accounting failed' in record.getMessage() for record in caplog.records)
    finally:
        close_index(orch)
