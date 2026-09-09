"""ORION_METER_SUBPIXEL_SESSION_RULER: the sub-pixel fill ruler stops moving per shot.

The reader latches its fill ruler (scale D, box-to-base offset) from the first three clean
frames of every shot. Whatever the box does on those three frames becomes a constant error
for the whole shot -- measured live as a 2.2pp rMAD ruler move between shots and a green cap
(fixed meter structure) read anywhere from 93 to 99. With the flag on, the emitted ruler is
the median of the last N shots' seeds once M have accumulated, so a shot whose seed frames
caught the box a few pixels off reads the same physical bar height as every other shot.

Synthetic meter: a hard-edged white bar on a dark court inside a 24x120 box, green cap near
the box top, the meter base 12 rows above the box bottom. Each shot's first three frames
carry a per-shot box offset (the acquisition jitter); the rest of the shot sees the nominal
box. The probe frame (bar 80 rows tall, nominal box) must read identically across shots.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE_ROW = 250          # image row of the meter base (last white row)
BOX_X, BOX_W, BOX_H = 100, 24, 120
BAR_X0, BAR_W = 106, 12
CAP_ROWS = (BASE_ROW - 99, BASE_ROW - 96)     # fixed structure, 8 rows below the nominal box top
SEED_JITTER_PX = [2, -2, 0, 1, -1, 0]         # per-shot box offset on the three seed frames
RISE_RUN_LENS = [10, 14, 18, 24, 32, 40, 50, 60, 70, 80, 88]
PROBE_RUN_LEN = 80


class _Cfg:
    meter_style = "Arrow2"
    meter_color = "White"
    confidence_threshold = 0.32


def _frame(run_len: int) -> np.ndarray:
    img = np.full((300, 400, 3), 40, np.uint8)
    top = BASE_ROW - run_len + 1
    img[top:BASE_ROW + 1, BAR_X0:BAR_X0 + BAR_W] = 255
    r0, r1 = CAP_ROWS
    img[r0:r1 + 1, BAR_X0 + 2:BAR_X0 + BAR_W - 2] = (0, 200, 0)
    return img


def _box(jitter_px: int = 0):
    bottom = BASE_ROW + 12 + jitter_px
    return (BOX_X, bottom - (BOX_H - 1), BOX_W, BOX_H)


def _run_session(monkeypatch, flag: str):
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL", "1")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_SESSION_RULER", flag)
    monkeypatch.setenv("ORION_METER_SUBPIXEL_SESSION_MIN", "3")
    from simple_meter_reader import SimpleMeterReader
    reader = SimpleMeterReader(cfg=_Cfg())
    ts = 1000.0
    probes = []
    kinds = []
    for jitter in SEED_JITTER_PX:
        reader._det_reset_lock_state()           # the real per-lock reset (keeps the session)
        probe = None
        kind = ""
        for i, run_len in enumerate(RISE_RUN_LENS):
            box = _box(jitter if i < 3 else 0)
            fill, green, _top = reader._measure_fill_in_box(_frame(run_len), box, ts=ts)
            ts += 1.0 / 60.0
            if run_len == PROBE_RUN_LEN:
                probe = fill
                kind = str((reader._dbg_subpx or {}).get("ruler", ""))
        assert probe is not None
        probes.append(float(probe))
        kinds.append(kind)
        ts += 2.0
    return probes, kinds


def test_per_shot_seed_jitter_moves_the_ruler_without_the_flag(monkeypatch):
    probes, kinds = _run_session(monkeypatch, "0")
    # a +2 px seed offset reads ~+1.7pp for the whole shot: the same bar height differs by shot
    assert max(probes) - min(probes) > 2.5, probes
    assert all(k == "shot" for k in kinds), kinds
    # and the direction is the seed's: the +2 px shots read high, the -2 px shots read low
    assert probes[0] > probes[2] > probes[1]


def test_session_ruler_reads_the_same_bar_height_every_shot(monkeypatch):
    probes_off, _ = _run_session(monkeypatch, "0")
    probes, kinds = _run_session(monkeypatch, "1")
    # fail-open: below the minimum seed count the per-shot latch stands, byte-identical
    assert probes[:2] == probes_off[:2]
    assert kinds[:2] == ["shot", "shot"]
    # from the third seed on, the session median rules and the spread collapses
    assert kinds[2:] == ["session"] * 4, kinds
    settled = probes[2:]
    spread_on = max(settled) - min(settled)
    spread_off = max(probes_off) - min(probes_off)
    assert spread_on < 0.6, settled
    assert spread_on < 0.25 * spread_off, (settled, probes_off)
    # the session ruler sits inside the per-shot values it was built from
    assert min(probes_off) <= min(settled) and max(settled) <= max(probes_off)


def _run_session_probe_early(monkeypatch, provisional: str):
    """Like _run_session, but the probe is the SECOND frame of each shot (run_len 14): with the
    per-shot seed still incomplete, that frame is on the seeding path unless the provisional
    session ruler is on. Returns (early probes, ruler kinds on that frame, history lengths)."""
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL", "1")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_SESSION_RULER", "1")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_SESSION_MIN", "3")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_SESSION_PROVISIONAL", provisional)
    from simple_meter_reader import SimpleMeterReader
    reader = SimpleMeterReader(cfg=_Cfg())
    ts = 1000.0
    probes, kinds, hist = [], [], []
    for jitter in SEED_JITTER_PX:
        reader._det_reset_lock_state()
        for i, run_len in enumerate(RISE_RUN_LENS):
            box = _box(jitter if i < 3 else 0)
            fill, _green, _top = reader._measure_fill_in_box(_frame(run_len), box, ts=ts)
            ts += 1.0 / 60.0
            if run_len == 14:
                probes.append(float(fill))
                kinds.append(str((reader._dbg_subpx or {}).get("ruler", "")))
        hist.append(len(reader._subpx_ruler_hist))
        ts += 2.0
    return probes, kinds, hist


def test_anchor_band_frames_ride_the_seed_without_the_provisional_flag(monkeypatch):
    probes, kinds, hist = _run_session_probe_early(monkeypatch, "0")
    # the second frame of every shot is still seeding: the ruler is the frame's own box
    assert all(k == "" for k in kinds), kinds
    # seed frames with the box 2 px low/high read the same bar height ~1.7 pp apart
    assert max(probes[2:]) - min(probes[2:]) > 1.4, probes
    assert hist == [1, 2, 3, 4, 5, 6]


def test_provisional_session_ruler_puts_the_anchor_band_on_the_stable_ruler(monkeypatch):
    probes_off, _, _ = _run_session_probe_early(monkeypatch, "0")
    probes, kinds, hist = _run_session_probe_early(monkeypatch, "1")
    # below the minimum seed count nothing changes
    assert probes[:3] == probes_off[:3]
    assert kinds[:3] == ["", "", ""]
    # from the fourth lock on, the very first frames are on the session median
    assert kinds[3:] == ["session_provisional"] * 3, kinds
    settled = probes[3:]
    assert max(settled) - min(settled) < 0.6, settled
    # the lock's own seed still completes and joins the history
    assert hist == [1, 2, 3, 4, 5, 6]


def test_provisional_ruler_refuses_a_box_that_does_not_match_the_session(monkeypatch):
    monkeypatch.setenv("ORION_METER_SUBPIXEL_SESSION_RULER", "1")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_SESSION_PROVISIONAL", "1")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_SESSION_MIN", "2")
    from simple_meter_reader import SimpleMeterReader
    reader = SimpleMeterReader(cfg=_Cfg())
    reader._subpx_ruler_hist = [(119.0, 12.0), (119.0, 12.5), (120.0, 12.2)]
    # a 30% taller box is a rescale: no provisional adoption, the cold seed runs as before
    tall = (BOX_X, BASE_ROW + 12 - 155, BOX_W, 156)
    reader._det_reset_lock_state()
    reader._measure_fill_in_box(_frame(20), tall, ts=1.0)
    assert reader._subpx_D is None and not reader._subpx_provisional
    # a matching box adopts
    reader._det_reset_lock_state()
    reader._measure_fill_in_box(_frame(20), _box(0), ts=2.0)
    assert reader._subpx_provisional and reader._subpx_D == 119.0
    assert reader._subpx_ruler_kind == "session_provisional"


def test_session_ruler_is_bounded_and_restarts_on_a_rescale(monkeypatch):
    monkeypatch.setenv("ORION_METER_SUBPIXEL_SESSION_RULER", "1")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_SESSION_WINDOW", "4")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_SESSION_MIN", "2")
    from simple_meter_reader import SimpleMeterReader
    reader = SimpleMeterReader(cfg=_Cfg())
    assert reader._subpx_session_window == 4 and reader._subpx_session_min == 2
    for d, off in [(119.0, 11.0), (119.0, 12.0), (120.0, 13.0), (119.0, 12.5), (118.0, 11.5)]:
        reader._subpx_D, reader._subpx_off = d, off
        reader._adopt_session_ruler()
    assert len(reader._subpx_ruler_hist) == 4          # bounded window
    assert reader._subpx_ruler_kind == "session"
    assert reader._subpx_off == pytest.approx(np.median([12.0, 13.0, 12.5, 11.5]))
    # a seed 30% taller than the session's meter is a rescale: history restarts from it
    reader._subpx_D, reader._subpx_off = 155.0, 16.0
    reader._adopt_session_ruler()
    assert reader._subpx_ruler_hist == [(155.0, 16.0)]
    assert reader._subpx_ruler_kind == "shot"
    assert (reader._subpx_D, reader._subpx_off) == (155.0, 16.0)


def _run_lock_probe(monkeypatch, lock: str):
    """Provisional session ruler on; probe the emitted ruler (D, off) on the second frame of a
    lock (provisional) and on the sixth (after the 3-frame seed completed). With the ruler lock
    the two are identical; without it the seed re-emits its own ruler mid-shot."""
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL", "1")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_SESSION_RULER", "1")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_SESSION_MIN", "3")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_SESSION_PROVISIONAL", "1")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_RULER_LOCK", lock)
    from simple_meter_reader import SimpleMeterReader
    reader = SimpleMeterReader(cfg=_Cfg())
    ts = 1000.0
    early, late, hist = [], [], []
    for jitter in SEED_JITTER_PX:
        reader._det_reset_lock_state()
        for i, run_len in enumerate(RISE_RUN_LENS):
            box = _box(jitter if i < 3 else 0)
            reader._measure_fill_in_box(_frame(run_len), box, ts=ts)
            ts += 1.0 / 60.0
            if i == 1:
                early.append((reader._subpx_D, reader._subpx_off, reader._subpx_provisional))
            if i == 5:
                late.append((reader._subpx_D, reader._subpx_off, reader._subpx_provisional))
        hist.append(len(reader._subpx_ruler_hist))
        ts += 2.0
    return early, late, hist


def test_ruler_lock_keeps_the_provisional_ruler_for_the_whole_shot(monkeypatch):
    # Live e71 (2026-09-03): the seed completing mid-shot re-emitted the ruler and the engine
    # saw a +7.3 pp step 19 ms before a scheduled fire. With the lock, a lock that started on
    # the provisional session ruler never changes ruler mid-shot; its seed still feeds the
    # history for the next shot.
    early, late, hist = _run_lock_probe(monkeypatch, "1")
    for k in range(3, len(SEED_JITTER_PX)):
        assert early[k][2] is True, ("frame 2 of a provisional lock must be provisional", k, early[k])
        assert late[k][2] is False, ("the seed must complete", k, late[k])
        assert late[k][0] == early[k][0] and late[k][1] == early[k][1],             ("the emitted ruler moved mid-shot", k, early[k], late[k])
    assert hist == [1, 2, 3, 4, 5, 6]


def test_without_the_ruler_lock_the_seed_re_emits_its_own_ruler(monkeypatch):
    early, late, hist = _run_lock_probe(monkeypatch, "0")
    moved = [k for k in range(3, len(SEED_JITTER_PX))
             if (late[k][0], late[k][1]) != (early[k][0], early[k][1])]
    assert moved, "fixture: with jittered seeds the unlocked path must move the ruler mid-shot"
    assert hist == [1, 2, 3, 4, 5, 6]

