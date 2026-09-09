"""[ORION_READER_BOX_WIDTH_GATE 2026-09-03] A detector box far wider than this session's accepted
meter boxes is not the meter.

Live 2026-09-03 10:05 e50 (landing 84.9): after the previous shot's ~81% bar was evicted, a
39-41 px wide box (the session's boxes were 25-28 px) re-read that bar as 0.0/0.0/0.0/33.0/39.6/
46.2/83.0/0.0; the 33 -> 39.6 pair escaped the ghost zone as a "real onset" and the engine armed
at 39.6 with a 12 ms ETA.

Contract (reviews #55/#57): a hit is a REJECTED frame -- detected False, fill/coarse/raw 0.0, an
explicit rejection_reason -- judged on the width/height RATIO (scale-invariant) of the served box
and of the lifecycle-ACCEPTED fresh proposal (never a refused teleport proposal), wide side only;
three consecutive hits retire the lock so the detector can re-acquire. There is deliberately NO
consecutive-hit reset: the ratio key is scale-invariant, and clearing the baseline after N hits
would fail open on exactly the persistent false geometry this gate exists for (review #59).
Inert before min_n accepted reads.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_simple_reader_ghost_press import (  # noqa: E402
    BH, BW, METER_X, METER_Y, TRUE_BOX, _frame, _latch_press_clock, _reader,
)

WIDE_BOX = (METER_X - 8, METER_Y, int(BW * 1.6), BH)      # 38 px against a 24 px session
NARROW_BOX = (METER_X + 4, METER_Y, 17, BH)               # 17 px against a 23 px session
FAR_WIDE_BOX = (100, 100, int(BW * 1.6), BH)              # a teleport proposal the lifecycle refuses


def _feed_box(r, ts, box, fill_top):
    r._meter_detector.res = (True, box, 0.9, ts)
    return r.detect(_frame(fill_top), ts=ts)


def _stage_and_feed(r, box, t0=1.00):
    """The same realistic press staging the ghost suite uses (press-clock latch, then a rising
    low pair, then the climb), all through `box`. Returns every result."""
    _latch_press_clock(r, t0)
    out = [_feed_box(r, t0 + 0.50, box, fill_top=88), _feed_box(r, t0 + 0.53, box, fill_top=84)]
    for k in range(6):
        out.append(_feed_box(r, t0 + 0.56 + 0.02 * k, box, fill_top=40))
    return out


def _seed_history(r, n=10, width=BW, height=BH):
    for _ in range(n):
        r._box_w_hist.append(float(width) / float(height))


def _fill(res):
    return float(getattr(res, "fill_pct", 0.0) or 0.0)


def _assert_rejected(res):
    assert not res.detected, res
    assert res.rejection_reason == "box_width_implausible", res
    assert _fill(res) == 0.0
    assert float(getattr(res, "raw_fill_pct", 0.0) or 0.0) == 0.0
    assert float(getattr(res, "smoothed_fill_pct", 0.0) or 0.0) == 0.0


def test_wide_box_is_rejected_fail_closed_once_a_session_median_exists(monkeypatch):
    r = _reader(monkeypatch)
    _seed_history(r)
    results = _stage_and_feed(r, WIDE_BOX)
    for res in results:
        assert not res.detected, res
        assert _fill(res) == 0.0
    hits = [res for res in results if res.rejection_reason == "box_width_implausible"]
    assert hits, [res.rejection_reason for res in results]
    _assert_rejected(hits[0])
    assert r._box_w_gate_hits >= 1
    assert all(abs(v - float(WIDE_BOX[2]) / BH) > 1e-6 for v in r._box_w_hist)
    # coarse bookkeeping must not leak a prior value on a rejected frame
    assert float(getattr(r, "_last_fill_coarse", 0.0) or 0.0) == 0.0


def test_normal_box_reads_and_feeds_the_history(monkeypatch):
    r = _reader(monkeypatch)
    _seed_history(r)
    results = _stage_and_feed(r, TRUE_BOX)
    assert _fill(results[-1]) >= 40.0, [_fill(x) for x in results]
    assert r._box_w_gate_hits == 0
    assert abs(list(r._box_w_hist)[-1] - float(BW) / BH) < 1e-6


def test_gate_is_inert_without_a_session_history(monkeypatch):
    r = _reader(monkeypatch)
    results = _stage_and_feed(r, WIDE_BOX)
    assert _fill(results[-1]) >= 40.0, ("a fresh reader has no median to judge by",
                                        [_fill(x) for x in results])
    assert r._box_w_gate_hits == 0


def test_narrow_box_is_allowed_one_sided(monkeypatch):
    # Archived detframes_20260902_225802 / _231158 carry legitimate width-17 frames after a
    # 23 median; the gate is wide-only so those keep reading.
    r = _reader(monkeypatch)
    _seed_history(r, width=23)
    results = _stage_and_feed(r, NARROW_BOX)
    assert _fill(results[-1]) >= 40.0, [_fill(x) for x in results]
    assert r._box_w_gate_hits == 0


def test_prior_nominal_lock_then_wide_candidate_is_rejected_on_the_first_wide_frame(monkeypatch):
    # The served box is EMA-smoothed (24 -> 38 emits 29, 32, 34, ...): judged on the served width
    # alone two frames slip through. The lifecycle-ACCEPTED raw candidate is judged too, so the
    # very first wide frame is rejected and nothing positive is published afterwards.
    r = _reader(monkeypatch)
    results = _stage_and_feed(r, TRUE_BOX)
    assert _fill(results[-1]) >= 40.0
    assert len(r._box_w_hist) >= r._box_w_min_n, "the nominal lock must have fed the history"
    hits_before = r._box_w_gate_hits
    first = _feed_box(r, 1.80, WIDE_BOX, fill_top=40)
    _assert_rejected(first)
    assert r._box_w_gate_hits == hits_before + 1
    for k in range(1, 6):
        res = _feed_box(r, 1.80 + 0.02 * k, WIDE_BOX, fill_top=40)
        assert not res.detected, res
        assert _fill(res) == 0.0


def test_refused_teleport_proposal_cannot_reject_or_retire_the_good_lock(monkeypatch):
    # Review #57: a far, wide proposal the lifecycle REFUSES (teleport outlier) leaves the good
    # 24 px active box in place; the gate must judge that box, not the refused proposal.
    r = _reader(monkeypatch)
    results = _stage_and_feed(r, TRUE_BOX)
    assert _fill(results[-1]) >= 40.0
    active_before = tuple(int(v) for v in r._det_active_box)
    hits_before = r._box_w_gate_hits
    # the lifecycle's verdict on the far proposal is "refused" (teleport outlier): pin it
    monkeypatch.setattr(r, "_det_on_found", lambda *a, **k: False)
    for k in range(r._box_w_retire_n + 1):
        res = _feed_box(r, 1.80 + 0.02 * k, FAR_WIDE_BOX, fill_top=40)
        assert res.rejection_reason != "box_width_implausible", res
        assert getattr(r, "_det_fresh_accept", False) is False, "fixture: proposal must be refused"
    assert r._box_w_gate_hits == hits_before
    assert r._box_w_retired == 0
    assert r._det_active_box is not None
    assert tuple(int(v) for v in r._det_active_box)[2] == active_before[2]
    assert _fill(res) >= 40.0, "the good lock must keep reading through refused proposals"


def test_three_consecutive_hits_retire_the_lock(monkeypatch):
    r = _reader(monkeypatch)
    results = _stage_and_feed(r, TRUE_BOX)
    assert _fill(results[-1]) >= 40.0
    assert r._det_active_box is not None
    for k in range(r._box_w_retire_n):
        _feed_box(r, 1.80 + 0.02 * k, WIDE_BOX, fill_top=40)
    assert r._box_w_retired == 1
    assert r._det_active_box is None
    assert r.box is None


def test_scale_change_of_the_whole_box_is_invariant(monkeypatch):
    # A resolution/camera scale change scales width and height together: the ratio key does not
    # move, so a 1.5x larger meter box is NOT wide.
    r = _reader(monkeypatch)
    _seed_history(r)
    scaled = (METER_X, METER_Y, int(BW * 1.5), int(BH * 1.5))
    r._meter_detector.res = (True, scaled, 0.9, 1.50)
    _latch_press_clock(r, 1.00)
    assert float(scaled[2]) / float(scaled[3]) < float(np.median(list(r._box_w_hist))) * r._box_w_ratio
    res = _feed_box(r, 1.50, scaled, fill_top=88)
    assert res.rejection_reason != "box_width_implausible", res
    assert r._box_w_gate_hits == 0


def test_flag_off_restores_the_read(monkeypatch):
    r = _reader(monkeypatch, ORION_READER_BOX_WIDTH_GATE="0")
    _seed_history(r)
    results = _stage_and_feed(r, WIDE_BOX)
    assert _fill(results[-1]) >= 40.0
    assert r._box_w_gate_hits == 0


def test_ratio_knob_is_honoured(monkeypatch):
    r = _reader(monkeypatch, ORION_READER_BOX_WIDTH_RATIO="2.0")
    _seed_history(r)
    results = _stage_and_feed(r, WIDE_BOX)                 # 1.6x < 2.0x -> allowed
    assert _fill(results[-1]) >= 40.0
    assert r._box_w_gate_hits == 0
