"""GREEN-ZONE stack: live colour-derived window + detector-only release diagnostic.

Covers the new invariants:
  * ORION_GREEN_ZONE_WINDOW emits the make window [g_lo, 100] READ FROM THE NEON BAND COLOUR
    (the grader-validated #1CFE1C band), not the legacy clamped cap-band heuristic.
  * samples are harvested ONLY from un-occluded frames (red top strictly below the band bottom).
  * both flags OFF -> byte-identical emission (legacy window, no gz state touched).
  * ORION_GREEN_SELF_GRADE labels release-command position from fill-at-release vs the colour
    window. It is not an NBA 2K make/miss outcome.
"""
import importlib

import numpy as np
import pytest

import simple_meter_reader as smr


# [ORION_READER_IDLE_PUBLISH_GATE 2026-09-15] The fixtures below feed a meter with NO press
# armed and (mostly) a constant fill -- byte for byte the shape the reader's idle publication
# gate now withholds from the engine and the overlay (see SimpleMeterReader._idle_publish_ok).
# The gate is a PUBLICATION policy with its own suite (tests/test_idle_publish_gate.py); these
# tests are about what the reader MEASURES, so the gate is switched off here and they keep
# measuring it.
@pytest.fixture(autouse=True)
def _idle_publish_gate_off(monkeypatch):
    monkeypatch.setenv("ORION_READER_IDLE_PUBLISH_GATE", "0")


H, W = 1080, 1920
RED_A = (0, 0, 255)
RED_B = (40, 40, 255)
NEON = (28, 254, 28)          # BGR ~#1CFE1C -> lands inside the grader's HSV neon swatch
FLOOR_Y = 600
TRACK_TOP_Y = 470             # green cap top sits at TRACK_TOP_Y - 6
COL_X = 900
COL_W = 24
CAP_TOP = TRACK_TOP_Y - 6     # 464


def _frame(fill_frac=0.4, band_px=47, col_x=COL_X, col_w=COL_W, bg=40):
    """Synthetic Arrow2 meter whose green cap is the NEON make-window band extending
    band_px rows DOWN from the cap top (wide standstill band by default, ~34pp)."""
    f = np.full((H, W, 3), bg, np.uint8)
    track_h = FLOOR_Y - TRACK_TOP_Y
    red_top = int(round(FLOOR_Y - fill_frac * track_h))
    half = col_w // 2
    if fill_frac > 0:
        f[red_top:FLOOR_Y, col_x:col_x + half] = RED_A
        f[red_top:FLOOR_Y, col_x + half:col_x + col_w] = RED_B
    # neon band: cap top down band_px rows, only where the red has not yet risen
    band_bot = min(CAP_TOP + band_px, red_top) if fill_frac > 0 else CAP_TOP + band_px
    f[CAP_TOP:band_bot, col_x:col_x + col_w] = NEON
    return f


def _mk_reader(monkeypatch, window="0", grade="0", tmp_path=None):
    monkeypatch.setenv("ORION_GREEN_ZONE_WINDOW", window)
    monkeypatch.setenv("ORION_GREEN_SELF_GRADE", grade)
    r = smr.SimpleMeterReader(W, H)
    if tmp_path is not None:
        r._gz_log_path = str(tmp_path / "gz_labels.jsonl")
    return r


# --------------------------------------------------------------------------- #
#  emission invariants
# --------------------------------------------------------------------------- #
def test_flags_off_emission_is_legacy_and_gz_untouched(monkeypatch):
    monkeypatch.delenv("ORION_GREEN_ZONE_WINDOW", raising=False)
    monkeypatch.delenv("ORION_GREEN_SELF_GRADE", raising=False)
    r = smr.SimpleMeterReader(W, H)
    res = r.detect(_frame(fill_frac=0.4, band_px=47), ts=0.0)
    assert res.detected
    # legacy heuristic clamps the band to <=20pp and the start to >=80
    assert res.green_window_start_pct >= 80.0
    assert res.green_window_end_pct == 100.0
    # gz stack never ran: no samples, no grade, no per-shot state
    assert r._gz_starts == []
    assert r.last_green_grade is None


def test_window_emitted_from_neon_colour(monkeypatch):
    """A wide (~34pp) neon band must be emitted as the TRUE colour window [~66, 100] --
    strictly wider than the legacy 20pp clamp ever allows."""
    r = _mk_reader(monkeypatch, window="1")
    res = None
    for i in range(4):        # a few frames so the estimate confidence ramps
        res = r.detect(_frame(fill_frac=0.4, band_px=47), ts=i / 60.0)
    assert res.detected
    assert res.green_window_end_pct == 100.0
    # true colour edge ~66pp; the legacy path would have clamped to >= 80
    assert 58.0 <= res.green_window_start_pct <= 74.0
    assert res.green_window_width_pct == pytest.approx(100.0 - res.green_window_start_pct)
    assert res.green_window_confidence == 1.0     # >= 4 exposed samples
    assert res.green_window_center_pct == pytest.approx(
        (res.green_window_start_pct + 100.0) / 2.0, abs=0.02)


def test_thin_band_reads_narrow_window(monkeypatch):
    """A thin (moving/contested) neon sliver must read as a narrow top-pinned window."""
    r = _mk_reader(monkeypatch, window="1")
    res = None
    for i in range(4):
        res = r.detect(_frame(fill_frac=0.4, band_px=5), ts=i / 60.0)
    assert res.detected
    assert res.green_window_start_pct >= 90.0
    assert res.green_window_end_pct == 100.0


def test_occluded_band_yields_no_sample(monkeypatch):
    """Red risen INTO the band -> the un-occluded expose rule must reject the frame."""
    r = _mk_reader(monkeypatch, window="1")
    # fill 0.9 -> red top well above the band bottom -> band clipped by red
    r.detect(_frame(fill_frac=0.9, band_px=47), ts=0.0)
    assert r._gz_starts == []


def test_window_persists_after_red_covers_band(monkeypatch):
    """Once estimated early in the shot, the window keeps being emitted even after the
    rising red occludes the band (no more samples land, the estimate holds)."""
    r = _mk_reader(monkeypatch, window="1")
    for i in range(3):
        r.detect(_frame(fill_frac=0.4, band_px=47), ts=i / 60.0)
    n = len(r._gz_starts)
    assert n >= 1
    res = r.detect(_frame(fill_frac=0.95, band_px=47), ts=4 / 60.0)
    assert len(r._gz_starts) == n          # occluded frame added nothing
    assert res.green_window_start_pct <= 74.0   # colour window still emitted


# --------------------------------------------------------------------------- #
#  self-grader labels
# --------------------------------------------------------------------------- #
def _run_shot(r, peak_frac, release_frac=None, band_px=47, release_seq=1):
    """Rise to peak_frac, optionally firing notify_release at release_frac, then blank
    frames until the lock drops (the no-hw-arm shot end)."""
    ts = [0.0]

    def step(frac):
        r.detect(_frame(fill_frac=frac, band_px=band_px), ts=ts[0])
        ts[0] += 1 / 60.0

    fracs = list(np.linspace(0.3, peak_frac, 8))
    for fr in fracs:
        if release_frac is not None and fr >= release_frac:
            r.notify_release(release_seq, physical_epoch=101,
                             shot_attempt=201, identity_verified=True)
            release_frac = None
        step(fr)
    blank = np.full((H, W, 3), 40, np.uint8)
    for _ in range(12):                     # conf decay 0.01/frame -> drops below 0.95
        r.detect(blank, ts=ts[0])
        ts[0] += 1 / 60.0


def test_self_grader_green_label_peak_proxy(monkeypatch, tmp_path):
    r = _mk_reader(monkeypatch, window="1", grade="1", tmp_path=tmp_path)
    got = []
    r.set_green_grade_sink(got.append)
    _run_shot(r, peak_frac=0.95)           # peak ~95pp inside [66, 100]
    rec = r.last_green_grade
    assert rec is not None
    assert rec["label"] == "GREEN"
    assert rec["release_proxy"] is True
    assert rec["end_reason"] == "lock_drop"
    assert 58.0 <= rec["g_lo"] <= 74.0
    assert got and got[0]["label"] == "GREEN"
    # JSONL telemetry written
    assert (tmp_path / "gz_labels.jsonl").exists()


def test_self_grader_early_label(monkeypatch, tmp_path):
    r = _mk_reader(monkeypatch, window="1", grade="1", tmp_path=tmp_path)
    _run_shot(r, peak_frac=0.55)           # peak ~55pp short of the ~66pp window start
    rec = r.last_green_grade
    assert rec is not None
    assert rec["label"] == "EARLY"


def test_self_grader_uses_real_release_fill(monkeypatch, tmp_path):
    """notify_release latches the fill on the release frame: released EARLY (~55pp) even
    though the meter later peaked GREEN (~95pp) -> the label must be EARLY, not the proxy."""
    r = _mk_reader(monkeypatch, window="1", grade="1", tmp_path=tmp_path)
    _run_shot(r, peak_frac=0.95, release_frac=0.55)
    rec = r.last_green_grade
    assert rec is not None
    assert rec["release_proxy"] is False
    assert rec["release_seq"] == 1
    assert rec["fill_at_release"] < rec["g_lo"] - 2.0
    assert rec["label"] == "EARLY"
    assert rec["peak_fill"] >= 90.0


def test_release_identity_is_latched_with_shot_not_mutable_latest(monkeypatch, tmp_path):
    """A delayed shot-1 record retains shot 1 even after a shot-2 marker arrives."""
    r = _mk_reader(monkeypatch, window="1", grade="1", tmp_path=tmp_path)
    got = []
    r.set_green_grade_sink(got.append)
    r._gz_begin_shot()
    r._gz_starts = [66.0, 66.0, 66.0, 66.0]
    r._peak_fill = 96.0
    r.last_fill = 55.0
    r.notify_release(41, physical_epoch=141, shot_attempt=241,
                     identity_verified=True)
    r.last_fill = 96.0
    r.notify_release(42, physical_epoch=142, shot_attempt=242,
                     identity_verified=True)  # must not rewrite shot 1 identity/fill
    r._gz_end_shot("lock_drop")

    rec = got[0]
    assert rec["release_seq"] == 41
    assert rec["physical_epoch"] == 141
    assert rec["shot_attempt"] == 241
    assert rec["release_proxy"] is False
    assert rec["fill_at_release"] == 55.0
    assert rec["label"] == "EARLY"


def test_self_grader_hw_disarm_edge(monkeypatch, tmp_path):
    """The hw-arm falling edge is a shot boundary too (the live path)."""
    r = _mk_reader(monkeypatch, window="1", grade="1", tmp_path=tmp_path)
    r.set_shot_state(True, armed_hw=True)
    for i in range(6):
        r.detect(_frame(fill_frac=0.3 + 0.1 * i, band_px=47), ts=i / 60.0)
    r.set_shot_state(False, armed_hw=False)
    r.detect(_frame(fill_frac=0.9, band_px=47), ts=7 / 60.0)   # edge observed inside read()
    rec = r.last_green_grade
    assert rec is not None
    assert rec["end_reason"] == "hw_disarm"
    assert rec["label"] in ("GREEN", "EARLY")   # peak ~80pp vs window ~66 -> GREEN
    assert rec["label"] == "GREEN"


def test_grade_flag_off_no_grade(monkeypatch, tmp_path):
    r = _mk_reader(monkeypatch, window="1", grade="0", tmp_path=tmp_path)
    _run_shot(r, peak_frac=0.95)
    assert r.last_green_grade is None
    assert not (tmp_path / "gz_labels.jsonl").exists()
