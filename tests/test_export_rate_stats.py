"""Export-rate / decode-vs-CV attribution coverage for OrionFramePipeBackend.

The backend exposes export_stats() = (wire_fps, wire_gap_fps). OrionStream v2
assigns callback_seq at decoder-callback entry, before FPS gating, GPU readback,
the one-slot latest-wins queue, and pipe I/O. Gaps therefore expose producer-side
skips/overwrites as well as transport loss. These tests pin that accounting
contract without a live pipe.
"""
import time

import pytest

cv2 = pytest.importorskip("cv2")

from chiaki_backend import OrionFramePipeBackend


def _backend():
    # Construct without start()/threads -- we drive the wire-seq counters directly.
    return OrionFramePipeBackend()


def _account(b, seq):
    """Mirror the reader loop's wire-seq accounting for one received frame."""
    if b._wire_seq_last and seq > b._wire_seq_last + 1:
        b._wire_seq_gaps += (seq - b._wire_seq_last - 1)
    b._wire_seq_last = seq
    b._export_count += 1


def test_export_stats_zero_before_any_frame():
    b = _backend()
    assert b.export_stats() == (0.0, 0.0)


def test_contiguous_seq_has_no_gaps():
    b = _backend()
    for s in range(1, 11):
        _account(b, s)
    # 9 deltas, all == 1 -> no dropped frames accumulated.
    assert b._wire_seq_gaps == 0
    assert b._export_count == 10


def test_seq_jump_counts_observed_wire_discontinuities():
    b = _backend()
    _account(b, 1)
    _account(b, 2)
    _account(b, 7)   # wire identities 3,4,5,6 were not observed
    _account(b, 8)
    assert b._wire_seq_gaps == 4


def test_first_seq_never_counts_as_a_gap():
    # A session can attach mid-stream at a high seq; the first frame must not be
    # mistaken for thousands of dropped frames.
    b = _backend()
    _account(b, 5000)
    _account(b, 5001)
    assert b._wire_seq_gaps == 0


def test_export_stats_snapshot_is_a_rate_pair():
    # After a measurement window the snapshot reports (export_fps, dropped_fps).
    b = _backend()
    b._export_count = 38
    b._wire_seq_gaps = 22
    b._export_fps_t0 = time.perf_counter() - 1.0
    # Force the window to roll by replaying the reader's window-close branch.
    now = time.perf_counter()
    elapsed = now - b._export_fps_t0
    b._export_fps = b._export_count / elapsed
    b._export_gap_fps = b._wire_seq_gaps / elapsed
    fps, gap = b.export_stats()
    assert 30.0 <= fps <= 46.0      # ~38 fps delivered
    assert 15.0 <= gap <= 30.0      # ~22 missing wire identities/s
