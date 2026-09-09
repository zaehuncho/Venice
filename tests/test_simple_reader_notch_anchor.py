"""[ORION_METER_SUBPIXEL_NOTCH 2026-09-08] The sub-pixel fill anchors on the meter's own chevron
notch, measured on whiteness, and stays on that ruler through the green cap.

Why (agent_ruler, 3 framedump sessions of 2026-09-03, 2519 frames): the old anchor -- the 0.8*B
falling crossing on the 9-column core MEAN -- is smeared by the ^ arms (they extend lower on the
outer columns) and wandered 0.4-0.5 px within a rise; the notch apex on the 2 centre columns
holds 0.065 px. Over the red court paint the semi-transparent track reads V 228 (26 counts of
contrast, the V-only edge fails) but min(B,G,R) 27. And at 90-97 % the fill edge sits inside the
green cap: on V every row above the edge is green and excluded (a_rows) and the sharpness gate
fails, so the frame flipped to the coarse BOX ruler (93.40 vs 94.10 on neighbours, one estimator
generation per flip; the graded peak was often that one coarse frame).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_simple_reader_ghost_press import FakeLoc  # noqa: E402

BH, BW = 107, 24
BOX_X, BOX_Y = 600, 300
NOTCH_APEX = 92            # centre columns' last white row + 1 (the ^ apex)
ARM_BOTTOM = 98            # outer core columns' last white row + 1 (the ^ arms)
CAP_ROWS = (3, 8)          # green make-window cap rows inside the box


class _Cfg:
    meter_style = "Arrow2"
    meter_color = "White"
    confidence_threshold = 0.32


def _patch(fill_top, cap=True, track=(30, 70, 140)):
    """Arrow2-ish meter with a CHEVRON notch: the white ribbon (cols 6..17) ends at row 98 on the
    outer core columns and steps up to row 92 on the two centre columns."""
    p = np.zeros((BH, BW, 3), np.uint8)
    p[:] = track
    p[:, :3] = (60, 60, 65)
    p[:, BW - 3:] = (60, 60, 65)
    ends = {6: 98, 7: 97, 8: 96, 9: 95, 10: 94, 11: 92, 12: 92, 13: 94, 14: 95, 15: 96, 16: 97, 17: 98}
    for c, end in ends.items():
        if fill_top is not None and fill_top < end:
            p[fill_top:end, c] = (250, 252, 253)
    if cap:
        r0, r1 = CAP_ROWS
        p[r0:r1, 5:BW - 5] = (40, 200, 40)
        if fill_top is not None:          # the fill paints OVER the cap where it has risen into it
            for c, end in ends.items():
                if fill_top < r1:
                    p[fill_top:min(r1, end), c] = (250, 252, 253)
    return p


def _frame(fill_top, **kw):
    f = np.full((720, 1280, 3), 35, np.uint8)
    f[BOX_Y:BOX_Y + BH, BOX_X:BOX_X + BW] = _patch(fill_top, **kw)
    return f


def _reader(monkeypatch, **env):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL", "1")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_SESSION_RULER", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from simple_meter_reader import SimpleMeterReader
    r = SimpleMeterReader(1280, 720, cfg=_Cfg())
    r._det_reset_lock_state()
    return r


def _measure(r, fill_top, ts, box=(BOX_X, BOX_Y, BW, BH), **kw):
    fill, _green, top = r._measure_fill_in_box(_frame(fill_top, **kw), box, ts=ts)
    q = dict(r._dbg_subpx or {})
    return float(fill), int(top), q, str(r._last_fill_estimator_mode), int(r._last_fill_estimator_generation)


# ----------------------------------------------------------------------------- unit: the landmark

def test_notch_anchor_finds_the_centre_column_apex_not_the_arms():
    from simple_meter_reader import SimpleMeterReader
    Wn = _patch(40).min(axis=2)
    core = np.zeros(BW, bool); core[6:18] = True
    notch = SimpleMeterReader._subpixel_notch_anchor(Wn, core, 40, BH)
    assert notch is not None
    # 0.8*B falling crossing between the last white centre row (91) and the first track row (92)
    assert NOTCH_APEX - 1.0 < notch < NOTCH_APEX, notch
    assert notch < ARM_BOTTOM - 4       # the arms would have put the old base ~5 rows lower


def test_notch_anchor_fails_closed_on_a_run_that_leaves_the_box_or_is_cut_mid_meter():
    from simple_meter_reader import SimpleMeterReader
    core = np.zeros(BW, bool); core[6:18] = True
    # fill runs off the bottom of the box: no notch inside the box
    p = _patch(40); p[92:, 6:18] = (250, 252, 253)
    assert SimpleMeterReader._subpixel_notch_anchor(p.min(axis=2), core, 40, BH) is None
    # a dark occluder band cuts the ribbon 40 rows above the box bottom: not a notch
    p = _patch(20); p[60:64, 4:20] = (20, 20, 20)
    assert SimpleMeterReader._subpixel_notch_anchor(p.min(axis=2), core, 20, BH) is None
    # too short a white run below the edge
    assert SimpleMeterReader._subpixel_notch_anchor(_patch(90).min(axis=2), core, 90, BH) is None


# ------------------------------------------------------------------ integration: the fill ruler

def test_fill_anchors_on_the_notch_and_stays_continuous_through_the_rise(monkeypatch):
    r = _reader(monkeypatch)
    ts = 1000.0
    out = []
    for fill_top in (80, 72, 64, 56, 48, 40, 32):
        out.append(_measure(r, fill_top, ts)); ts += 1 / 60
    # once the latch is complete every frame is a measured notch anchor on the base ruler
    latched = out[3:]
    assert all(o[2].get("anchor") == "base" and o[2].get("anchor_src") == "notch" for o in latched), out
    assert all(o[3] == "subpixel" for o in latched)
    assert len({o[4] for o in latched}) == 1, "the ruler identity must not change mid-rise"
    # 8 rows of growth per frame on a ~106 px ruler ~= 7.5 pp, and each step must match that
    steps = [b[0] - a[0] for a, b in zip(latched, latched[1:])]
    assert all(abs(s - 8.0 * 100.0 / (BH - 1)) < 0.6 for s in steps), steps
    # the anchor is the notch apex row, not the arms' bottom
    assert all(abs(o[2]["base_sub"] - (NOTCH_APEX - 0.5)) < 0.8 for o in latched), [o[2]["base_sub"] for o in latched]


def test_edge_inside_the_green_cap_keeps_the_subpixel_ruler(monkeypatch):
    r = _reader(monkeypatch)
    ts = 1000.0
    for fill_top in (60, 52, 44, 36, 28, 20, 12):
        _measure(r, fill_top, ts); ts += 1 / 60
    _, _, q0, mode0, gen0 = _measure(r, 12, ts); ts += 1 / 60
    assert mode0 == "subpixel"
    # the fill rises INTO the cap (rows 3..7 are green): rows above the edge are all green
    for fill_top in (6, 5, 4):
        fill, top, q, mode, gen = _measure(r, fill_top, ts); ts += 1 / 60
        assert mode == "subpixel", (fill_top, q, mode)
        assert q.get("anchor") in ("base", "base_int"), (fill_top, q)
        assert gen == gen0, "no estimator generation flip inside the cap"
        assert 92.0 <= fill <= 100.0, fill


def test_red_court_track_is_still_measured_on_whiteness(monkeypatch):
    """Over the red paint the track reads V~228 (the V-only plateau gate fails: contrast 26);
    whiteness sees 210 counts and the frame stays sub-pixel."""
    r = _reader(monkeypatch)
    ts = 1000.0
    red = (27, 40, 228)          # BGR of the semi-transparent track over the red paint (measured f1611)
    out = []
    for fill_top in (80, 72, 64, 56, 48, 40):
        out.append(_measure(r, fill_top, ts, track=red)); ts += 1 / 60
    assert all(o[3] == "subpixel" for o in out[3:]), [(o[3], o[2].get("gate")) for o in out]
    assert all(o[2].get("anchor_src") == "notch" for o in out[3:])


def test_flag_off_restores_the_core_mean_base_path(monkeypatch):
    r = _reader(monkeypatch, ORION_METER_SUBPIXEL_NOTCH="0")
    ts = 1000.0
    out = []
    for fill_top in (80, 72, 64, 56, 48, 40):
        out.append(_measure(r, fill_top, ts)); ts += 1 / 60
    assert all("anchor_src" not in o[2] for o in out)
    assert all(o[2].get("anchor") in ("base", "box0") for o in out), out
    # the old anchor sits on the arms' smeared falloff, several rows below the notch apex
    assert all(o[2]["base_sub"] > NOTCH_APEX + 1.5 for o in out if o[2].get("anchor") == "base")


# --------------------------------------------------------------- cadence: armed-hot submissions

class _CadenceLoc(FakeLoc):
    def __init__(self):
        super().__init__()
        self.calls = []

    def submit(self, frame, ts):
        self.calls.append(("submit", float(ts)))

    def submit_priority(self, frame, ts):
        self.calls.append(("priority", float(ts)))


def _cadence_reader(monkeypatch, **env):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    monkeypatch.setenv("ORION_METER_DETECTOR_TRACK", "0")
    monkeypatch.setenv("ORION_METER_DETECTOR_SYNC_ACQUIRE", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from simple_meter_reader import SimpleMeterReader
    r = SimpleMeterReader(1280, 720, cfg=_Cfg(), require_gameplay_eligibility=True)
    r._meter_detector = _CadenceLoc()
    return r


def _blank():
    return np.full((720, 1280, 3), 35, np.uint8)


def test_every_frame_is_a_priority_submit_while_the_shot_button_is_held(monkeypatch):
    r = _cadence_reader(monkeypatch)
    r.set_shot_state(False, 0.0, False)
    r.detect(_blank(), ts=10.00)
    r.set_shot_state(True, 1.0, True)          # physical press -> hardware arm
    r.notify_physical_shot_start(3)
    for k in range(6):
        r.detect(_blank(), ts=10.02 + k / 60)
    kinds = [c[0] for c in r._meter_detector.calls]
    assert kinds[0] == "submit"                 # idle: ordinary cadence
    assert kinds[1:] == ["priority"] * 6, kinds  # armed: worker wake on every frame
    assert r._det_diag.get("hot_submit") == 6


def test_hot_window_is_bounded_per_arm_epoch_and_closes_on_disarm(monkeypatch):
    r = _cadence_reader(monkeypatch, ORION_METER_DETECTOR_ARMED_HOT_MAX_MS="100")
    r.set_shot_state(True, 1.0, True)
    r.notify_physical_shot_start(5)
    for k in range(4):
        r.detect(_blank(), ts=20.00 + k * 0.04)         # 0, 40, 80, 120 ms into the epoch
    kinds = [c[0] for c in r._meter_detector.calls]
    assert kinds == ["priority", "priority", "priority", "submit"], kinds   # a stuck arm cannot pin the worker
    r.set_shot_state(False, 0.0, False)
    r.detect(_blank(), ts=20.20)
    assert r._meter_detector.calls[-1][0] == "submit"
    # a NEW physical epoch reopens the window
    r.set_shot_state(True, 1.0, True)
    r.notify_physical_shot_start(6)
    r.detect(_blank(), ts=20.30)
    assert r._meter_detector.calls[-1][0] == "priority"


def test_hot_cadence_can_be_switched_off(monkeypatch):
    r = _cadence_reader(monkeypatch, ORION_METER_DETECTOR_ARMED_HOT="0")
    r.set_shot_state(True, 1.0, True)
    r.notify_physical_shot_start(9)
    for k in range(3):
        r.detect(_blank(), ts=30.0 + k / 60)
    assert all(c[0] == "submit" for c in r._meter_detector.calls)
