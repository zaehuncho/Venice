"""Lossless archive, byte-bounded handoff, and honest per-window accounting."""
import csv
import threading
import time

import cv2
import numpy as np
import pytest

import lossless_frame_archive as archive_module
from lossless_frame_archive import LosslessFrameArchive, read_frame
from test_framedump_census_reliability import make_orch, close_index


def pixels(i=0):
    return np.random.default_rng(i).integers(0, 256, (48, 64, 3), dtype=np.uint8)


def test_chunks_are_pixel_exact_and_receipts_do_not_retain_raw_arrays(tmp_path):
    saved, discarded = [], []
    archive = LosslessFrameArchive(tmp_path, lambda *a: saved.append(a), discarded.append, 4)
    for i in range(11):
        archive.append((i, pixels(i), {'epoch': i // 3}))
        assert len(archive.pending) < 4
        assert all(item[1] is None for item, _ in archive.pending)
    assert len(saved) == 8
    archive.flush()
    assert len(saved) == 11 and not discarded
    for item, filename, ordinal in saved:
        assert np.array_equal(read_frame(tmp_path / filename, ordinal), pixels(item[0]))


def test_closed_chunk_failure_never_claims_saved_pixels(tmp_path, monkeypatch):
    saved, discarded = [], []
    archive = LosslessFrameArchive(tmp_path, lambda *a: saved.append(a), discarded.append, 2)
    actual = cv2.VideoCapture
    class BrokenCapture:
        def __init__(self, *a): self.cap = actual(*a)
        def isOpened(self): return self.cap.isOpened()
        def read(self):
            ok, frame = self.cap.read()
            if ok: frame[0, 0, 0] ^= 1
            return ok, frame
        def release(self): self.cap.release()
    monkeypatch.setattr(archive_module.cv2, 'VideoCapture', BrokenCapture)
    archive.append((0, pixels(), {}))
    with pytest.raises(IOError, match='pixel verification'):
        archive.append((1, pixels(1), {}))
    assert not saved and len(discarded) == 2
    assert not list(tmp_path.glob('raw_*[0-9].avi'))
    assert archive.writer is None and not archive.pending


def test_independent_writers_do_not_overwrite_a_prior_generation(tmp_path):
    saved = []
    for _ in range(2):
        a = LosslessFrameArchive(tmp_path, lambda *args: saved.append(args), lambda _: None, 1)
        a.append((0, pixels(), {}))
    assert saved[0][1] != saved[1][1]
    assert len(list(tmp_path.glob('*.avi'))) == 2


def test_archive_missing_ordinal_is_not_silently_another_frame(tmp_path):
    saved = []
    a = LosslessFrameArchive(tmp_path, lambda *args: saved.append(args), lambda _: None, 1)
    a.append((0, pixels(), {}))
    with pytest.raises(IOError, match='missing'):
        read_frame(tmp_path / saved[0][1], 1)
    with pytest.raises(ValueError):
        read_frame(tmp_path / saved[0][1], -1)


def test_commit_failure_does_not_strand_later_receipts(tmp_path):
    saved, discarded = [], []
    def commit(item, name, ordinal):
        if ordinal == 1:
            raise IOError('metadata fault')
        saved.append(item[0])
    a = LosslessFrameArchive(tmp_path, commit, lambda item: discarded.append(item[0]), 3)
    a.append((0, pixels(), {}))
    a.append((1, pixels(1), {}))
    with pytest.raises(IOError, match='metadata fault'):
        a.append((2, pixels(2), {}))
    assert saved == [0, 2] and discarded == [1]
    assert not a.pending and a.writer is None


def archive_orch(tmp_path, monkeypatch, depth=12):
    monkeypatch.setenv('ORION_FRAMEDUMP_FORMAT', 'huffyuv')
    o, rec, clock = make_orch(tmp_path, monkeypatch, depth)
    assert o._framedump_format == 'huffyuv'
    o.framedump_press_open(1, mono_now=0.0)  # expired guard for direct writer tests
    return o, rec


def offer(o, i=0):
    frame = pixels(i)
    info = dict(t=float(i), wall=1000.0+i, det=True, fill=42., conf=.9, gc=96., rej='',
                frame_context=dict(processed_seq=20+i, frame_no=100+i))
    assert o._framedump_offer(frame, info, 0.0)
    return frame


def test_archive_commits_only_after_verified_close_and_joins_ordinals(tmp_path, monkeypatch):
    o, rec = archive_orch(tmp_path, monkeypatch)
    stats = o._framedump_press_stats
    for i in range(5): offer(o, i)
    for _ in range(5): assert o._framedump_write_item(o._framedump_q.get_nowait())
    assert stats['pending'] == 5 and stats['saved'] == 0
    assert o._framedump_buffer_bytes == 0
    o._framedump_flush_archive()
    assert stats['pending'] == 0 and stats['saved'] == stats['indexed'] == 5
    close_index(o)
    rows = list(csv.DictReader((tmp_path / 'frames.csv').open()))
    assert len(rows) == 5
    for i, row in enumerate(rows):
        assert int(row['processed_seq']) == 20+i
        assert int(row['frame_no']) == 100+i
        assert int(row['container_frame']) == i
        assert np.array_equal(read_frame(tmp_path / row['filename'], i), pixels(i))
    rec.stop()


def test_byte_cap_rejects_before_copy_and_releases_after_discard(tmp_path, monkeypatch):
    o, rec = archive_orch(tmp_path, monkeypatch)
    o._framedump_buffer_limit = pixels().nbytes
    offer(o)
    class NoCopy:
        nbytes = 1
        def copy(self): raise AssertionError('must not copy rejected frame')
    o._framedump_press_copy = True
    assert not o._framedump_offer(NoCopy(), {}, 0.0)
    item = o._framedump_q.get_nowait()
    o._framedump_finish_item(item)
    o._framedump_finish_item(item)
    assert o._framedump_buffer_bytes == 0
    assert o._framedump_press_stats['discarded'] == 1
    rec.stop()


def test_physical_press_defers_encoding_and_stop_interrupts_guard(tmp_path, monkeypatch):
    o, rec = archive_orch(tmp_path, monkeypatch)
    o.framedump_press_open(2)  # real monotonic critical interval
    offer(o)
    item = o._framedump_q.get_nowait()
    t = threading.Thread(target=o._framedump_write_item, args=(item,))
    t.start()
    time.sleep(.04)
    assert o._framedump_archive is None
    o._framedump_writer_stop.set()
    t.join(timeout=.5)
    assert not t.is_alive()
    assert o._framedump_press_stats['discarded'] == 1
    assert o._framedump_buffer_bytes == 0
    rec.stop()


def test_duplicate_close_does_not_extend_encoding_guard(tmp_path, monkeypatch):
    o, rec = archive_orch(tmp_path, monkeypatch)
    o.framedump_press_open(2, mono_now=10.)
    o.framedump_press_close(2, mono_now=10.6)
    guard = o._framedump_archive_protected_until
    o.framedump_press_close(2, mono_now=10.8)
    assert guard == pytest.approx(10.75)
    assert o._framedump_archive_protected_until == guard
    rec.stop()


def test_archive_error_disables_only_diagnostics_and_accounts_once(tmp_path, monkeypatch):
    o, rec = archive_orch(tmp_path, monkeypatch)
    offer(o)
    def fail_append(*_): raise OSError('injected codec failure')
    monkeypatch.setattr(LosslessFrameArchive, 'append', fail_append)
    assert not o._framedump_write_item(o._framedump_q.get_nowait())
    stats = o._framedump_press_stats
    assert stats['pending'] == stats['saved'] == stats['indexed'] == 0
    assert stats['discarded'] == 1 and o._framedump_buffer_bytes == 0
    assert not o._framedump_enabled
    assert o._framedump_disabled_reason == 'archive_write_failed'
    rec.stop()


def test_closed_old_csv_preserves_its_column_width(tmp_path, monkeypatch):
    o, rec = archive_orch(tmp_path, monkeypatch)
    o._framedump_format = 'bmp'
    offer(o)
    assert o._framedump_write_item(o._framedump_q.get_nowait())
    close_index(o)
    p = tmp_path / 'frames.csv'
    lines = list(csv.reader(p.open()))
    # Previous capture-clock schema ended at filename.
    with p.open('w', newline='') as f:
        csv.writer(f).writerows(row[:-1] for row in lines)
    offer(o, 1)
    assert o._framedump_write_item(o._framedump_q.get_nowait())
    close_index(o)
    rows = list(csv.reader(p.open()))
    assert len({len(row) for row in rows}) == 1
    rec.stop()


@pytest.mark.parametrize('ordinal', [True, 0.5, 1.0, '1.0', '01', '+1', ' 1', '1 '])
def test_bug_sweep_archive_ordinal_does_not_silently_select_another_frame(tmp_path, ordinal, monkeypatch):
    def unexpected_open(*_):
        raise AssertionError('invalid index reached the decoder')
    monkeypatch.setattr(archive_module.cv2, 'VideoCapture', unexpected_open)
    with pytest.raises(ValueError, match='ordinal'):
        read_frame(tmp_path / 'never_open.avi', ordinal)


def test_bug_sweep_all_discard_receipts_attempted_even_if_callback_raises(tmp_path):
    calls=[]
    def discard(item):
        calls.append(item[0])
        if item[0] == 0: raise RuntimeError('injected metadata failure')
    a=LosslessFrameArchive(tmp_path, lambda *args: None, discard, 4)
    for i in range(3):a.append((i, pixels(i), {}))
    with pytest.raises(RuntimeError, match='metadata failure'):
        a.discard_pending()
    assert calls == [0, 1, 2]
    assert not a.pending and a.writer is None



def test_bug_sweep_verification_failure_attempts_all_discards_and_keeps_primary_error(tmp_path, monkeypatch):
    calls=[]
    def discard(item):
        calls.append(item[0]);raise RuntimeError('secondary metadata failure')
    a=LosslessFrameArchive(tmp_path,lambda *args:None,discard,3)
    actual=archive_module.cv2.VideoCapture
    class Empty:
        def __init__(self,*args):self.cap=actual(*args)
        def isOpened(self):return True
        def read(self):return False,None
        def release(self):self.cap.release()
    monkeypatch.setattr(archive_module.cv2,'VideoCapture',Empty)
    a.append((0,pixels(0),{}));a.append((1,pixels(1),{}))
    with pytest.raises(IOError, match='pixel verification'):
        a.append((2,pixels(2),{}))
    assert calls == [0,1,2]
    assert not a.pending and a.writer is None


def test_bug_sweep_commit_and_discard_errors_do_not_strand_later_commits(tmp_path):
    committed=[];discarded=[]
    def commit(item,name,ordinal):
        if ordinal == 0:raise IOError('first commit failure')
        committed.append(item[0])
    def discard(item):
        discarded.append(item[0]);raise RuntimeError('discard failure')
    a=LosslessFrameArchive(tmp_path,commit,discard,3)
    a.append((0,pixels(0),{}));a.append((1,pixels(1),{}))
    with pytest.raises(IOError,match='first commit failure'):
        a.append((2,pixels(2),{}))
    assert committed == [1,2] and discarded == [0]


def test_bug_sweep_archive_canonical_text_and_numpy_index_still_read_exact_pixels(tmp_path):
    saved=[]
    a=LosslessFrameArchive(tmp_path,lambda *args:saved.append(args),lambda _:None,2)
    a.append((0,pixels(0),{}));a.append((1,pixels(1),{}))
    path=tmp_path/saved[0][1]
    assert np.array_equal(read_frame(path,'0'),pixels(0))
    assert np.array_equal(read_frame(path,np.int64(1)),pixels(1))
