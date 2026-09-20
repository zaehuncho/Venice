"""Unit tests for player_anchor.py + the anchored search it feeds in meter_locator_cv.

Everything here is synthetic (numpy/cv2); no framedump files, no network, no disk writes.
The drawn meter follows meter_locator_cv's own docstring @720p (26 x 120 track, 11 px white
column, green tip 100 px above the white bottom); the drawn NAMEPLATE is the module's own
embedded PS-disc template blitted onto the court, so a match there is exact by construction
and the tests measure the SEARCH, not the template.

The four things that must hold:
  1. the anchor finds the plate and predicts a patch that contains the meter at the measured
     offsets (icon_x = box_centre - 30, icon_y = box_bottom + 150 @720p);
  2. a CONFIDENT anchor refuses a candidate outside that patch -- the false-lock guarantee;
  3. the expectation window accepts a sub-floor fill only when it is inside the patch, inside
     the press's onset window, AND seen RISING on two frames;
  4. with the knobs at their shipped values the locator is byte-identical to the same locator
     with player_anchor absent entirely.
"""
from __future__ import annotations

import os

import cv2
import numpy as np
import pytest

import meter_locator_cv as mlc
import player_anchor as pa

W, H = 1280, 720
FRAME_DT = 1.0 / 60.0

TRACK_W, TRACK_H = 26, 120
COL_X, COL_W = 7, 11
TIP_TOP = 8
TIP_GAP = 100
WHITE_BOT = TIP_TOP + TIP_GAP

COURT_BG = (70, 30, 10)
WHITE = (255, 255, 255)
GREEN = (0, 200, 0)
TRACK = (45, 40, 38)
OUTLINE = (190, 190, 190)

# The measured priors this module ships with (player_anchor._priors defaults).
DX = -30.0          # icon_x - box_centre_x
DY = 150.0          # icon_y - box_bottom

ANCHOR_KNOBS = ("ORION_PLAYER_ANCHOR", "ORION_ANCHORED_SEARCH", "ORION_EXPECTATION_WINDOW",
                "ORION_CV_SHAPE_MIN_H_ARMED", "ORION_ANCHOR_REFUSE_CONF", "ORION_ANCHOR_DX",
                "ORION_ANCHOR_DY", "ORION_ANCHOR_TOL_X", "ORION_ANCHOR_TOL_Y",
                "ORION_ANCHOR_BOX_H", "ORION_ANCHOR_PS_MIN", "ORION_ANCHOR_TX_MIN",
                "ORION_ANCHOR_ACQ_MS", "ORION_ANCHOR_HOLD_MS", "ORION_ANCHOR_LEARN",
                "ORION_ANCHOR_REFUSE_MODE", "ORION_ANCHOR_COARSE_MIN", "ORION_EXPECT_PAIR_MS",
                "ORION_EXPECT_PAIR_TOL_PX", "ORION_EXPECT_PRE_MS", "ORION_EXPECT_POST_MS",
                "ORION_METER_PROPOSER", "ORION_METER_BAND_TOP", "ORION_METER_GATE_TOP",
                "ORION_METER_BAND_BOTTOM")


@pytest.fixture
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("ORION_CV_") or key.startswith("ORION_ANCHOR") or key in ANCHOR_KNOBS:
            monkeypatch.delenv(key, raising=False)
    pa.ARM.reset()
    pa.ANCHOR.reset(keep_identity=False)
    yield
    pa.ARM.reset()
    pa.ANCHOR.reset(keep_identity=False)


# ------------------------------------------------------------------ synthetic frames
def court() -> np.ndarray:
    f = np.empty((H, W, 3), np.uint8)
    f[:] = COURT_BG
    cv2.line(f, (0, 520), (W - 1, 560), WHITE, 2)
    cv2.line(f, (300, 200), (330, 700), WHITE, 2)
    cv2.ellipse(f, (640, 640), (260, 90), 0, 180, 360, WHITE, 2)
    return f


def draw_meter(f, x, y, fill_pct):
    cv2.rectangle(f, (x, y), (x + TRACK_W - 1, y + TRACK_H - 1), TRACK, -1)
    for dx in (3, 21):
        f[y:y + TRACK_H, x + dx] = OUTLINE
    gtop = y + TIP_TOP
    tri = np.array([[x + 12, gtop], [x + 13, gtop], [x + 19, gtop + 10], [x + 6, gtop + 10]],
                   np.int32)
    cv2.fillPoly(f, [tri], GREEN)
    wbot = y + WHITE_BOT
    hpx = int(round(TIP_GAP * fill_pct / 100.0))
    if hpx > 0:
        f[wbot - hpx:wbot, x + COL_X:x + COL_X + COL_W] = WHITE
    notch = np.array([[x + 7, wbot + 1], [x + 17, wbot + 1], [x + 12, wbot + 7]], np.int32)
    cv2.fillPoly(f, [notch], OUTLINE)
    return {"cx": x + COL_X + COL_W / 2.0, "gtop": gtop, "wbot": wbot,
            "box_bottom": y + 4 + 107}


def draw_plate(f, icon_cx, icon_cy, tag="OWNER99"):
    """Blit the module's own PS-disc template, with a gamertag block to its right."""
    t = pa.PlayerAnchor()._template()
    assert t is not None and t.shape == (27, 27)
    bgr = cv2.cvtColor(t.astype(np.uint8), cv2.COLOR_GRAY2BGR)
    x0, y0 = int(icon_cx) - 13, int(icon_cy) - 13
    f[y0:y0 + 27, x0:x0 + 27] = bgr
    cv2.putText(f, tag, (x0 + 30, int(icon_cy) + 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                (235, 235, 235), 2, cv2.LINE_AA)
    return (float(icon_cx), float(icon_cy))


def meter_xy_for(icon_cx, icon_cy):
    """Track top-left that puts the meter exactly where the priors predict."""
    return (int(round(icon_cx - DX - COL_X - COL_W / 2.0)),
            int(round(icon_cy - DY - 111)))


# ------------------------------------------------------------------ 1. the anchor
def test_anchor_finds_the_plate_and_predicts_the_meter_patch(clean_env, monkeypatch):
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    icx, icy = 900.0, 560.0
    f = court()
    draw_plate(f, icx, icy)
    mx, my = meter_xy_for(icx, icy)
    lm = draw_meter(f, mx, my, 40.0)

    a = pa.PlayerAnchor().update(f, ts=0.0, armed=True)
    assert a is not None, "plate not found"
    assert abs(a.icon_x - icx) <= 4 and abs(a.icon_y - icy) <= 4, (a.icon_x, a.icon_y)
    assert a.valid()
    # the meter this plate belongs to is inside its patch...
    assert a.contains_box(mx + 0.5, my + 4, 26, 107)
    assert a.x0 <= lm["cx"] <= a.x1 and a.y0 <= lm["box_bottom"] <= a.y1
    # ...and a meter 300 px away is not
    assert not a.contains_box(mx + 300, my + 4, 26, 107)
    assert not a.contains_box(mx, my + 260, 26, 107)


def test_anchor_without_an_identity_may_help_but_never_refuse(clean_env, monkeypatch):
    """A plate with no learned gamertag is, by construction, only 'a plate' -- one of five
    on the floor. It may steer the search; it may not veto the real meter."""
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    f = court()
    draw_plate(f, 900.0, 560.0)
    a = pa.PlayerAnchor().update(f, ts=0.0, armed=True)
    assert a is not None
    assert a.conf <= 0.70
    assert not pa.PlayerAnchor.refuse_ok(a)


def test_anchor_costs_well_under_the_frame_budget(clean_env, monkeypatch):
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    f = court()
    draw_plate(f, 900.0, 560.0)
    A = pa.PlayerAnchor()
    A.update(f, ts=0.0, armed=True)                 # warm OpenCV kernels
    t = 0.0
    track = []
    for _ in range(30):
        t += FRAME_DT
        A.update(f, ts=t, armed=True)
        track.append(A.last_ms)
    track.sort()
    assert track[len(track) // 2] < 1.0, track[len(track) // 2]
    assert track[-1] < 3.0, track[-1]


def test_onset_window_is_the_union_when_the_shot_type_is_unknown(clean_env):
    lo, hi = pa.onset_window_ms("")
    assert lo == pytest.approx(50.0) and hi == pytest.approx(1100.0)
    lo, hi = pa.onset_window_ms("Standstill")
    assert lo == pytest.approx(50.0) and hi == pytest.approx(550.0)
    lo, hi = pa.onset_window_ms("Right Fade")
    assert lo == pytest.approx(575.0) and hi == pytest.approx(1075.0)


def test_arm_state_round_trip(clean_env):
    pa.ARM.note_press(7, 12.5, "Right Fade", True)
    assert pa.ARM.armed and pa.ARM.epoch == 7
    assert pa.ARM.press_ts == pytest.approx(12.5)
    assert pa.ARM.shot_type == "Right Fade"
    assert pa.ARM.rhythm is True
    assert pa.ARM.note_release(7, 13.0) is True
    assert not pa.ARM.armed
    assert pa.ARM.release_ts == pytest.approx(13.0)


# ------------------------------------------------------------------ 1b. the type channel
def test_note_shot_type_retypes_in_place_without_moving_the_press(clean_env):
    """[ORION_SHOT_GATE_TYPE] The engine's blind 200 ms grace re-types Standstill -> fade and
    re-sends the SAME epoch. The press did not move -- only the expected onset."""
    pa.ARM.note_press(11, 4.0, "Standstill", False)
    lo_before, hi_before = pa.onset_window_ms(pa.ARM.shot_type)

    assert pa.ARM.note_shot_type(11, "Left Fade", True) is True
    assert pa.ARM.shot_type == "Left Fade"
    assert pa.ARM.rhythm is True
    assert pa.ARM.press_ts == pytest.approx(4.0)       # the press is untouched
    assert pa.ARM.armed and pa.ARM.epoch == 11

    lo_after, hi_after = pa.onset_window_ms(pa.ARM.shot_type)
    assert (lo_after, hi_after) != (lo_before, hi_before)
    assert lo_after > hi_before                        # the fade's onset is past standing's


def test_note_shot_type_refuses_another_epoch_and_an_unarmed_gate(clean_env):
    pa.ARM.note_press(11, 4.0, "Standstill")
    assert pa.ARM.note_shot_type(10, "Left Fade") is False      # retired press
    assert pa.ARM.note_shot_type(12, "Left Fade") is False      # not this one either
    assert pa.ARM.shot_type == "Standstill"
    pa.ARM.note_release(11, 4.5)
    assert pa.ARM.note_shot_type(11, "Left Fade") is False      # window already closed
    assert not pa.ARM.armed


def test_note_release_refuses_a_retired_epoch(clean_env):
    """A marker for the PREVIOUS shot must never close the press that replaced it."""
    pa.ARM.note_press(20, 1.0, "Standstill")
    assert pa.ARM.note_release(19, 1.1) is False
    assert pa.ARM.armed and pa.ARM.epoch == 20
    assert pa.ARM.note_release(0, 1.2) is True                  # 0 = "whatever is armed"
    assert not pa.ARM.armed


def test_arm_state_tuple_is_append_only(clean_env):
    """meter_locator_cv indexes ArmState.state(); the first four fields are its contract."""
    pa.ARM.note_press(5, 2.0, "No Dip", True)
    st = pa.ARM.state()
    assert len(st) >= 5
    assert (st[0], st[1], st[2], st[3]) == (5, 2.0, "No Dip", True)


# ------------------------------------------------------------------ 2. the refusal
def _armed_locator(monkeypatch, **extra):
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    monkeypatch.setenv("ORION_ANCHORED_SEARCH", "1")
    # the refusal needs a CONFIDENT anchor; a synthetic frame has no gamertag history, so
    # the threshold is lowered here to exercise the mechanism (the cap itself is asserted
    # by test_anchor_without_an_identity_may_help_but_never_refuse)
    monkeypatch.setenv("ORION_ANCHOR_REFUSE_CONF", "0.40")
    for k, v in extra.items():
        monkeypatch.setenv(k, str(v))
    return mlc.MeterContourLocator()


def test_candidate_outside_the_patch_is_refused_while_the_anchor_is_confident(
        clean_env, monkeypatch):
    loc = _armed_locator(monkeypatch)
    icx, icy = 900.0, 560.0
    f = court()
    draw_plate(f, icx, icy)
    draw_meter(f, 200, 300, 40.0)                   # a meter far from this plate
    pa.ARM.note_press(1, 0.0, "")

    box = None
    t = 0.0
    for _ in range(4):
        t += FRAME_DT
        box = loc.detect_box(f, ts=t)
    assert box is None, box
    assert loc.stats["refused_outside"] >= 1


def test_the_same_candidate_is_accepted_once_it_is_inside_the_patch(clean_env, monkeypatch):
    loc = _armed_locator(monkeypatch)
    icx, icy = 900.0, 560.0
    f = court()
    draw_plate(f, icx, icy)
    mx, my = meter_xy_for(icx, icy)
    draw_meter(f, mx, my, 40.0)
    pa.ARM.note_press(1, 0.0, "")

    box = loc.detect_box(f, ts=FRAME_DT)
    assert box is not None, "the anchored patch refused its OWN meter"
    assert abs((box[0] + box[2] / 2.0) - (mx + COL_X + COL_W / 2.0)) <= 3
    assert loc.stats["refused_outside"] == 0
    assert loc.stats["anchor_patch_hit"] >= 1


def test_refusal_never_fires_without_a_press(clean_env, monkeypatch):
    """No press -> no anchor -> today's full-band behaviour, unchanged."""
    loc = _armed_locator(monkeypatch)
    f = court()
    draw_plate(f, 900.0, 560.0)
    draw_meter(f, 200, 300, 40.0)
    pa.ARM.reset()
    box = loc.detect_box(f, ts=FRAME_DT)
    assert box is not None
    assert loc.stats["refused_outside"] == 0
    assert loc.stats["anchor_hit"] == 0


def test_the_patch_does_not_weaken_the_lone_column_gate(clean_env, monkeypatch):
    """The patch straddles the shooter's torso, so his jersey number is inside it. Gate 8
    ('a meter has no white twin beside it') must still refuse a digit pair found there --
    which is why the patch is SCANNED with a sideways pad wider than lone_gap."""
    loc = _armed_locator(monkeypatch, ORION_CV_SHAPE_MIN_H_ARMED=8,
                         ORION_EXPECTATION_WINDOW=1)
    icx, icy = 900.0, 560.0
    f = court()
    draw_plate(f, icx, icy)
    mx, my = meter_xy_for(icx, icy)
    # two white glyphs of similar height, 12 px apart, where the meter's fill would be
    wbot = my + WHITE_BOT
    f[wbot - 22:wbot, mx + COL_X:mx + COL_X + COL_W] = WHITE
    f[wbot - 22:wbot, mx + COL_X + COL_W + 12:mx + COL_X + 2 * COL_W + 12] = WHITE
    gtop = my + TIP_TOP                               # a green blob playing the tip
    cv2.circle(f, (mx + 12, gtop + 4), 5, GREEN, -1)
    pa.ARM.note_press(1, 0.0, "")

    t = 0.150
    for _ in range(6):
        assert loc.detect_box(f, ts=t) is None, "a jersey-number twin pair was accepted"
        t += FRAME_DT
    assert loc.stats["not_lone"] >= 1


# ------------------------------------------------------------------ 3. the window
def _expect_locator(monkeypatch):
    return _armed_locator(monkeypatch, ORION_EXPECTATION_WINDOW=1,
                          ORION_CV_SHAPE_MIN_H_ARMED=8, ORION_EXPECT_PAIR_MS=60)


def _frame_at(icx, icy, fill):
    f = court()
    draw_plate(f, icx, icy)
    mx, my = meter_xy_for(icx, icy)
    draw_meter(f, mx, my, fill)
    return f, mx, my


def test_sub_floor_fill_needs_a_RISING_pair_inside_the_window(clean_env, monkeypatch):
    loc = _expect_locator(monkeypatch)
    icx, icy = 900.0, 560.0
    pa.ARM.note_press(1, 0.0, "")
    f1, mx, my = _frame_at(icx, icy, 10.0)          # 10 px column: below gate 9's 14 px floor
    f2, _, _ = _frame_at(icx, icy, 13.0)            # still sub-floor, but RISING

    assert loc.detect_box(f1, ts=0.200) is None, "a lone sub-floor sighting was accepted"
    assert loc.stats["expect_pending"] >= 1
    box = loc.detect_box(f2, ts=0.200 + FRAME_DT)
    assert box is not None, "the rising pair was not accepted"
    assert loc.stats["expect_accept"] >= 1
    assert abs((box[0] + box[2] / 2.0) - (mx + COL_X + COL_W / 2.0)) <= 3


def _ragged_frame(icx, icy, fill_px, seed):
    """A sub-floor fill as it actually renders: the first few percent of the column do not
    reach the full 11 px and the run is ragged (measured on session_20260912_201355, live
    fill 1-15 %: the achromatic run is 7 px wide on 25 of 120 frames and 8-9 px on 69).
    A straight synthetic column cannot stand in for this -- it passes gate 9's edge test on
    two rows and hides the very failure the abstain path exists for."""
    f = court()
    draw_plate(f, icx, icy)
    mx, my = meter_xy_for(icx, icy)
    cv2.rectangle(f, (mx, my), (mx + TRACK_W - 1, my + TRACK_H - 1), TRACK, -1)
    gtop = my + TIP_TOP
    cv2.fillPoly(f, [np.array([[mx + 12, gtop], [mx + 13, gtop], [mx + 19, gtop + 10],
                               [mx + 6, gtop + 10]], np.int32)], GREEN)
    wbot = my + WHITE_BOT
    rng = np.random.RandomState(seed)
    for k in range(fill_px):
        w = int(rng.randint(7, 10))
        x0 = mx + COL_X + int(rng.randint(0, 3))
        f[wbot - 1 - k, x0:x0 + w] = WHITE
    return f, mx, my


def test_a_ragged_sub_floor_fill_is_not_judged_on_shape(clean_env, monkeypatch):
    """Gate 9 ABSTAINS between the relaxed floor and the shipped one -- it does not judge a
    column it cannot measure. Without that, lowering the floor only moves the frame from
    `shape_short` to `shape_irregular` and recall does not move (measured, 2026-09-15)."""
    icx, icy = 900.0, 560.0
    monkeypatch.setenv("ORION_CV_COL_W_MIN_ARMED", "6")
    loc = _expect_locator(monkeypatch)
    pa.ARM.note_press(1, 0.0, "")
    f1, mx, _ = _ragged_frame(icx, icy, 10, 3)
    f2, _, _ = _ragged_frame(icx, icy, 13, 4)
    assert loc.detect_box(f1, ts=0.200) is None
    box = loc.detect_box(f2, ts=0.200 + FRAME_DT)
    assert box is not None, "the ragged rising pair was refused"
    assert loc.stats["shape_abstain"] >= 1
    assert abs((box[0] + box[2] / 2.0) - (mx + COL_X + COL_W / 2.0)) <= 6

    # The same two frames with the knobs OFF: still refused. [SHIP CONFIG 2026-09-17] this
    # used to be spelled "delete the env and take the default"; the default is now the RELAXED
    # configuration, so the off arm names its values instead of inheriting them. What is being
    # compared is unchanged -- relaxed-floor-with-abstain vs the 14 px floor.
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "0")
    monkeypatch.setenv("ORION_ANCHORED_SEARCH", "0")
    monkeypatch.setenv("ORION_EXPECTATION_WINDOW", "0")
    monkeypatch.setenv("ORION_CV_SHAPE_MIN_H_ARMED", "14")
    monkeypatch.setenv("ORION_CV_COL_W_MIN_ARMED", "8")
    shipped = mlc.MeterContourLocator()
    assert shipped.shape_min_h_armed == pytest.approx(14.0)
    assert shipped.detect_box(f1, ts=0.200) is None
    assert shipped.detect_box(f2, ts=0.200 + FRAME_DT) is None
    assert shipped.stats["shape_abstain"] == 0
    assert shipped.stats["shape_short"] >= 1


def test_a_static_sub_floor_column_is_never_accepted(clean_env, monkeypatch):
    """The 8-13 px white bits the 14 px floor was invented to refuse -- a jersey number, a
    sock, a scoreboard digit -- do not GROW. Neither does this one."""
    loc = _expect_locator(monkeypatch)
    icx, icy = 900.0, 560.0
    pa.ARM.note_press(1, 0.0, "")
    f, _, _ = _frame_at(icx, icy, 10.0)
    t = 0.200
    for _ in range(6):
        assert loc.detect_box(f, ts=t) is None
        t += FRAME_DT
    assert loc.stats["expect_accept"] == 0


def test_sub_floor_outside_the_onset_window_keeps_todays_confirmation(clean_env, monkeypatch):
    loc = _expect_locator(monkeypatch)
    icx, icy = 900.0, 560.0
    pa.ARM.note_press(1, 0.0, "")
    f1, _, _ = _frame_at(icx, icy, 10.0)
    f2, _, _ = _frame_at(icx, icy, 13.0)
    # 3.0 s after the press is past every measured onset (max 700 + 400 ms)
    assert loc.detect_box(f1, ts=3.000) is None
    assert loc.detect_box(f2, ts=3.000 + FRAME_DT) is None
    assert loc.stats["expect_accept"] == 0


def test_at_or_above_the_shipped_floor_the_window_is_not_consulted(clean_env, monkeypatch):
    """Gate 9 already judged such a candidate; the window must not add a frame of latency."""
    loc = _expect_locator(monkeypatch)
    icx, icy = 900.0, 560.0
    pa.ARM.note_press(1, 0.0, "")
    f, _, _ = _frame_at(icx, icy, 30.0)
    assert loc.detect_box(f, ts=0.200) is not None
    assert loc.stats["expect_pending"] == 0


def test_pickup_forensics_are_recorded_for_the_press(clean_env, monkeypatch):
    loc = _expect_locator(monkeypatch)
    icx, icy = 900.0, 560.0
    pa.ARM.note_press(4, 0.0, "")
    f, _, _ = _frame_at(icx, icy, 30.0)
    assert loc.detect_box(f, ts=0.200) is not None
    p = loc.pickup
    assert p["epoch"] == 4
    assert p["first_sight_fill"] == pytest.approx(30.0, abs=3.0)
    assert p["first_sight_ms_after_press"] == pytest.approx(200.0, abs=1.0)
    assert p["anchor_used"] == 1
    assert p["anchor_conf"] > 0.0


# ------------------------------------------------------------------ 4. knobs off
@pytest.fixture
def knobs_off(clean_env, monkeypatch):
    """[SHIP CONFIG 2026-09-17] The five anchor knobs now DEFAULT ON, so "off" is explicit.

    The off configuration is still a supported, tested one -- it is the kill switch an owner
    reaches for, and it is the only honest "before" for the equivalence tests below -- but it
    is no longer what a scrubbed environment produces. tests/test_ship_defaults.py owns the
    defaults; this section owns what happens when they are turned off.
    """
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "0")
    monkeypatch.setenv("ORION_ANCHORED_SEARCH", "0")
    monkeypatch.setenv("ORION_EXPECTATION_WINDOW", "0")
    monkeypatch.setenv("ORION_CV_SHAPE_MIN_H_ARMED", "14")
    monkeypatch.setenv("ORION_CV_COL_W_MIN_ARMED", "8")


@pytest.mark.parametrize("fill", [8.0, 15.0, 40.0, 90.0])
def test_knobs_off_is_identical_to_the_module_being_absent(knobs_off, monkeypatch, fill):
    """The shipped path must not change by one pixel. Compared against the SAME locator with
    player_anchor removed entirely, which is the only honest 'before'."""
    frames = []
    icx, icy = 900.0, 560.0
    for k in range(6):
        f = court()
        draw_plate(f, icx + 4 * k, icy)
        mx, my = meter_xy_for(icx, icy)
        draw_meter(f, mx + 2 * k, my, fill)
        frames.append(f)

    pa.ARM.note_press(1, 0.0, "")                   # armed: still must change nothing
    with_mod = mlc.MeterContourLocator()
    got_a = [with_mod.detect_box(f, ts=(i + 1) * FRAME_DT) for i, f in enumerate(frames)]

    monkeypatch.setattr(mlc, "_pa", None)
    without = mlc.MeterContourLocator()
    got_b = [without.detect_box(f, ts=(i + 1) * FRAME_DT) for i, f in enumerate(frames)]

    assert got_a == got_b
    assert with_mod.stats["anchor_hit"] == 0
    assert with_mod.stats["anchor_patch_hit"] == 0
    assert with_mod.stats["refused_outside"] == 0


def test_knobs_off_never_calls_the_anchor(knobs_off):
    pa.ANCHOR.stats["calls"] = 0
    loc = mlc.MeterContourLocator()
    pa.ARM.note_press(1, 0.0, "")
    f = court()
    draw_plate(f, 900.0, 560.0)
    draw_meter(f, *meter_xy_for(900.0, 560.0), 40.0)
    for i in range(5):
        loc.detect_box(f, ts=(i + 1) * FRAME_DT)
    assert pa.ANCHOR.stats["calls"] == 0


# ------------------------------------------------------------------ 5. the reader hook
class _StubReader:
    """Just enough of SimpleMeterReader to exercise the press-window publisher; the real
    class needs a capture source, a detector and a settings file to construct."""

    def __init__(self, locator):
        import simple_meter_reader as smr
        self._physical_shot_epoch = 0
        self._shot_armed_hw = False
        self._pa_epoch = 0
        # [ORION_SHOT_GATE_TYPE / _RELEASE 2026-09-15] the engine-supplied type channel and
        # the explicit press-window close, both published with the press on the frame clock.
        self._pa_shot_type = ""
        self._pa_rhythm = False
        self._pa_shot_type_epoch = 0
        self._pa_released_epoch = 0
        self._pa_last_ts = None

        class _Det:
            pass
        self._meter_detector = _Det()
        self._meter_detector._base = locator
        self._flush_pickup_line = lambda: smr.SimpleMeterReader._flush_pickup_line(self)
        # [ORION_PICKUP_PER_PRESS 2026-09-15] the press-time record opener + the
        # close-then-open roll both press edges go through.
        self._open_pickup_record = (
            lambda *a, **k: smr.SimpleMeterReader._open_pickup_record(self, *a, **k))
        self._roll_pickup_record = (
            lambda *a, **k: smr.SimpleMeterReader._roll_pickup_record(self, *a, **k))
        self._pa_shot_type_for = lambda e: smr.SimpleMeterReader._pa_shot_type_for(self, e)
        self.notify_physical_shot_type = (
            lambda *a, **k: smr.SimpleMeterReader.notify_physical_shot_type(self, *a, **k))
        self.notify_physical_shot_release = (
            lambda *a, **k: smr.SimpleMeterReader.notify_physical_shot_release(self, *a, **k))


def test_reader_publishes_the_press_on_the_frame_clock(clean_env, monkeypatch, caplog):
    import logging
    import simple_meter_reader as smr

    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    loc = mlc.MeterContourLocator()
    r = _StubReader(loc)
    pub = smr.SimpleMeterReader._publish_press_window
    flush = smr.SimpleMeterReader._flush_pickup_line

    pub(r, 10.0)                                     # not armed -> nothing published
    assert not pa.ARM.armed

    r._physical_shot_epoch = 9
    r._shot_armed_hw = True
    pub(r, 10.5)
    assert pa.ARM.armed and pa.ARM.epoch == 9
    assert pa.ARM.press_ts == pytest.approx(10.5)

    pub(r, 10.6)                                     # same epoch -> the press does not move
    assert pa.ARM.press_ts == pytest.approx(10.5)

    loc.pickup.update(epoch=9, first_sight_fill=11.0, first_sight_ms_after_press=190.0,
                      anchor_used=1, anchor_conf=0.81)
    r._shot_armed_hw = False
    r._physical_shot_epoch = 0
    with caplog.at_level(logging.WARNING, logger="simple_reader"):
        pub(r, 11.4)
    assert not pa.ARM.armed
    msgs = [rec.getMessage() for rec in caplog.records if "PICKUP:" in rec.getMessage()]
    assert len(msgs) == 1, msgs
    assert "epoch=9" in msgs[0] and "first_sight_fill=11.0" in msgs[0]
    assert "anchor_used=1" in msgs[0]

    caplog.clear()                                   # exactly one line per press
    with caplog.at_level(logging.WARNING, logger="simple_reader"):
        flush(r)
    assert not [rec for rec in caplog.records if "PICKUP:" in rec.getMessage()]


def test_every_press_gets_its_own_pickup_line(clean_env, monkeypatch, caplog):
    """[ORION_PICKUP_PER_PRESS 2026-09-15] THREE PRESSES -> THREE LINES, and the two the
    locator never saw a meter for are lines too.

    The 2026-09-15 session logged TWO PICKUP lines for 37 presses: the record was opened only
    inside the locator's own detect path, so once one press had flushed it (`logged=1`) every
    later press whose meter was never picked up inherited that same spent record and closed
    silently. The misses are exactly the presses the owner is complaining about, so the census
    has to contain them: an `anchor_used=0`, `first_sight_fill=-1.0` line IS the evidence.
    """
    import logging
    import simple_meter_reader as smr

    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    loc = mlc.MeterContourLocator()
    r = _StubReader(loc)
    pub = smr.SimpleMeterReader._publish_press_window

    msgs = []
    t = 10.0
    for i, epoch in enumerate((21, 22, 23)):
        r._physical_shot_epoch = epoch
        r._shot_armed_hw = True
        with caplog.at_level(logging.WARNING, logger="simple_reader"):
            pub(r, t)
        assert pa.ARM.armed and pa.ARM.epoch == epoch
        # The record is open for THIS press, however the last one ended.
        assert loc.pickup["epoch"] == epoch
        assert loc.pickup["logged"] == 0
        if i == 0:
            # Only the first press is given a meter; the other two are the misses.
            loc.pickup.update(first_sight_fill=12.0, first_sight_ms_after_press=540.0,
                              anchor_used=1, anchor_conf=0.77)
        t += 0.5
        r._shot_armed_hw = False
        r._physical_shot_epoch = 0
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="simple_reader"):
            pub(r, t)
        line = [m for m in (rec.getMessage() for rec in caplog.records) if "PICKUP:" in m]
        assert len(line) == 1, (epoch, line)
        msgs.append(line[0])
        t += 0.5

    assert "epoch=21" in msgs[0] and "first_sight_fill=12.0" in msgs[0]
    assert "anchor_used=1" in msgs[0]
    for miss, epoch in zip(msgs[1:], (22, 23)):
        assert f"epoch={epoch}" in miss
        assert "first_sight_fill=-1.0" in miss      # the miss is a line, not a silence
        assert "anchor_used=0" in miss


def test_a_press_that_never_closed_is_still_flushed_by_the_next_one(clean_env, monkeypatch,
                                                                    caplog):
    """[ORION_PICKUP_PER_PRESS] A lost release marker must cost one line's lateness, not the
    line: the next press flushes its predecessor before opening its own record."""
    import logging
    import simple_meter_reader as smr

    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    loc = mlc.MeterContourLocator()
    r = _StubReader(loc)
    pub = smr.SimpleMeterReader._publish_press_window

    r._physical_shot_epoch = 31
    r._shot_armed_hw = True
    pub(r, 40.0)
    loc.pickup.update(first_sight_fill=8.0, first_sight_ms_after_press=610.0)

    # No close at all: the next press simply arrives (the hardware arm never fell).
    r._physical_shot_epoch = 32
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="simple_reader"):
        pub(r, 41.0)
    msgs = [m for m in (rec.getMessage() for rec in caplog.records) if "PICKUP:" in m]
    assert len(msgs) == 1, msgs
    assert "epoch=31" in msgs[0] and "first_sight_fill=8.0" in msgs[0]
    assert loc.pickup["epoch"] == 32 and loc.pickup["logged"] == 0


def test_reader_publishes_the_engines_shot_type_with_the_press(clean_env, monkeypatch):
    """[ORION_SHOT_GATE_TYPE] The type reaches the LOCATOR's expectation window, not just the
    reader: the whole point is that a fade's meter is not due for ~675 ms."""
    import simple_meter_reader as smr

    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    r = _StubReader(mlc.MeterContourLocator())
    pub = smr.SimpleMeterReader._publish_press_window

    r.notify_physical_shot_type(9, "Left Fade", True)
    r._physical_shot_epoch = 9
    r._shot_armed_hw = True
    pub(r, 10.0)
    assert pa.ARM.shot_type == "Left Fade" and pa.ARM.rhythm is True
    assert pa.onset_window_ms(pa.ARM.shot_type) == pytest.approx((575.0, 1075.0))

    # the mid-press re-type lands on the live press without moving it
    r.notify_physical_shot_type(9, "Right Fade", False)
    assert pa.ARM.shot_type == "Right Fade" and pa.ARM.rhythm is False
    assert pa.ARM.press_ts == pytest.approx(10.0)


def test_a_type_for_the_previous_press_never_narrows_this_one(clean_env, monkeypatch):
    """An unknown type costs a WIDER window; a stale type would cost a refused meter."""
    import simple_meter_reader as smr

    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    r = _StubReader(mlc.MeterContourLocator())
    pub = smr.SimpleMeterReader._publish_press_window

    r.notify_physical_shot_type(9, "Left Fade")
    r._physical_shot_epoch = 10                    # the NEXT press, arriving untyped
    r._shot_armed_hw = True
    pub(r, 10.0)
    assert pa.ARM.epoch == 10 and pa.ARM.shot_type == ""
    assert pa.onset_window_ms(pa.ARM.shot_type) == pytest.approx((50.0, 1100.0))


def test_release_marker_closes_the_press_window_at_that_instant(clean_env, monkeypatch, caplog):
    """[ORION_SHOT_GATE_RELEASE] The window ends on the engine's release edge -- and the very
    next frame must NOT re-arm it while the reader's hardware arm window is still open."""
    import logging
    import simple_meter_reader as smr

    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    loc = mlc.MeterContourLocator()
    r = _StubReader(loc)
    pub = smr.SimpleMeterReader._publish_press_window

    r._physical_shot_epoch = 12
    r._shot_armed_hw = True
    pub(r, 20.0)
    assert pa.ARM.armed and pa.ARM.epoch == 12

    loc.pickup.update(epoch=12, first_sight_fill=9.0, first_sight_ms_after_press=210.0,
                      anchor_used=1, anchor_conf=0.7)
    with caplog.at_level(logging.WARNING, logger="simple_reader"):
        assert r.notify_physical_shot_release(12, 1757913600123.4) is True
    assert not pa.ARM.armed
    assert pa.ARM.release_ts == pytest.approx(20.0)      # the reader's FRAME clock, not epoch ms
    assert [m for m in (rec.getMessage() for rec in caplog.records) if "PICKUP:" in m]

    pub(r, 20.02)                                        # hw arm still open, press is over
    assert not pa.ARM.armed
    assert r._pa_epoch == 0


def test_release_marker_for_a_retired_press_is_ignored(clean_env, monkeypatch):
    import simple_meter_reader as smr

    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    r = _StubReader(mlc.MeterContourLocator())
    pub = smr.SimpleMeterReader._publish_press_window

    r._physical_shot_epoch = 13
    r._shot_armed_hw = True
    pub(r, 30.0)
    assert r.notify_physical_shot_release(12, 1.0) is False   # the PREVIOUS shot's release
    assert pa.ARM.armed and pa.ARM.epoch == 13


def test_a_closed_press_reopens_on_the_next_epoch(clean_env, monkeypatch):
    import simple_meter_reader as smr

    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    r = _StubReader(mlc.MeterContourLocator())
    pub = smr.SimpleMeterReader._publish_press_window

    r._physical_shot_epoch = 14
    r._shot_armed_hw = True
    pub(r, 40.0)
    r.notify_physical_shot_release(14, 1.0)
    assert not pa.ARM.armed

    r._physical_shot_epoch = 15                       # the next press
    pub(r, 41.0)
    assert pa.ARM.armed and pa.ARM.epoch == 15
    assert pa.ARM.press_ts == pytest.approx(41.0)


def test_release_marker_is_inert_when_the_knob_is_off(knobs_off):
    """The close must cost nothing when the anchor is not in play (shipped default)."""
    import simple_meter_reader as smr

    r = _StubReader(mlc.MeterContourLocator())
    r._physical_shot_epoch = 16
    r._shot_armed_hw = True
    smr.SimpleMeterReader._publish_press_window(r, 50.0)
    assert not pa.ARM.armed
    assert r.notify_physical_shot_release(16, 1.0) is True
    assert not pa.ARM.armed and pa.ARM.epoch == 0


def test_reader_hook_is_inert_when_the_knob_is_off(knobs_off):
    import simple_meter_reader as smr
    loc = mlc.MeterContourLocator()
    r = _StubReader(loc)
    r._physical_shot_epoch = 3
    r._shot_armed_hw = True
    smr.SimpleMeterReader._publish_press_window(r, 5.0)
    assert not pa.ARM.armed and pa.ARM.epoch == 0


def test_the_ship_default_is_the_relaxed_armed_floor(clean_env):
    """[SHIP CONFIG 2026-09-17] The default FLIPPED: this used to assert "today's floor".

    Every graded session from 09-15 on ran PLAYER_ANCHOR=1 ANCHORED_SEARCH=1
    EXPECTATION_WINDOW=1 CV_SHAPE_MIN_H_ARMED=8 CV_COL_W_MIN_ARMED=6 from the dev launch line,
    which no customer has, so those are the SOURCE defaults now. The UNARMED gates are the
    thing that must not have moved -- the relaxation is licensed by the anchor's patch, and
    outside a press nothing changed at all.
    """
    loc = mlc.MeterContourLocator()
    assert loc.shape_min_h == pytest.approx(14.0)          # unarmed: untouched
    assert loc.col_w_min == pytest.approx(8.0)             # unarmed: untouched
    assert loc.shape_min_h_armed == pytest.approx(8.0)
    assert loc.col_w_min_armed == pytest.approx(6.0)
    assert loc.anchored_search is True
    assert loc.expect_window is True
    assert loc.refuse_mode == 1
    assert pa.enabled() is True


def test_the_knobs_off_fixture_really_restores_the_pre_ship_configuration(knobs_off):
    """The guard for section 4: if the off fixture stopped biting, its tests would be vacuous."""
    loc = mlc.MeterContourLocator()
    assert loc.shape_min_h_armed == pytest.approx(14.0)
    assert loc.col_w_min_armed == pytest.approx(8.0)
    assert loc.anchored_search is False
    assert loc.expect_window is False
    assert pa.enabled() is False


# ------------------------------------------------------------------ 6. the REAL object graph
#
# [ORION_PICKUP_PER_PRESS 2026-09-15] Everything above drives _StubReader, and every one of
# those tests passed while the live sidecar printed ZERO `PICKUP:` lines for 30 presses. A
# stub cannot catch that class of bug, because the stub IS the hypothesis. These tests build
# the objects the orchestrator actually builds -- a real SimpleMeterReader whose
# `_meter_detector` is the real AsyncMeterLocator wrapping the real MeterContourLocator
# (ORION_METER_PROPOSER=cv) -- and drive them through the exact call sequence
# remote_play_orchestrator uses:
#
#   _arm_shot_gate   -> reader.notify_physical_shot_start(epoch); reader.set_shot_state(1,1,1)
#   _processing_loop -> reader.set_shot_state(armed, 0.0, armed_hw); reader.detect(frame, ts)
#   (there is NO release marker: no session log has ever contained a
#    "SHOT-GATE RELEASE RECEIPT", so notify_physical_shot_release never runs live)
#
# and the arm window is 20 s (_shot_gate_max_seconds), so the hardware arm does NOT fall
# between presses -- which is why the press-time flush is the only close a rally ever sees.


@pytest.fixture
def real_locator_singletons():
    """ORION_METER_PROPOSER=cv builds a process-wide locator singleton; give the test its own."""
    import meter_detector_yolo as mdy
    prev = (mdy._SINGLETON, mdy._ASYNC)
    mdy._SINGLETON = None
    mdy._ASYNC = None
    yield mdy
    try:
        stop = getattr(mdy._ASYNC, "stop", None) or getattr(mdy._ASYNC, "close", None)
        if callable(stop):
            stop()
    except Exception:
        pass
    mdy._SINGLETON, mdy._ASYNC = prev


def _real_reader(monkeypatch, real_locator_singletons):
    """The reader the orchestrator builds at remote_play_orchestrator.py:1700, with the
    detector knobs the owner's launcher injects."""
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    monkeypatch.setenv("ORION_ANCHORED_SEARCH", "1")
    monkeypatch.setenv("ORION_ANCHOR_REFUSE_MODE", "1")
    monkeypatch.setenv("ORION_EXPECTATION_WINDOW", "1")
    monkeypatch.setenv("ORION_CV_SHAPE_MIN_H_ARMED", "8")
    monkeypatch.setenv("ORION_CV_COL_W_MIN_ARMED", "6")
    monkeypatch.setenv("ORION_METER_DETECTOR", "1")
    monkeypatch.setenv("ORION_METER_PROPOSER", "cv")
    # Inference inline instead of on the worker thread: the object graph is identical
    # (AsyncMeterLocator._base is the MeterContourLocator either way) and the test does not
    # race a background thread.
    monkeypatch.setenv("ORION_METER_DETECTOR_SYNC", "1")
    from simple_meter_reader import SimpleMeterReader
    r = SimpleMeterReader(cfg=None, require_gameplay_eligibility=True)
    assert r._meter_detector is not None, "the orchestrator's locator did not load"
    assert type(r._meter_detector).__name__ == "AsyncMeterLocator"
    assert isinstance(r._meter_detector._base, mlc.MeterContourLocator)
    return r


def _press_frames(r, epoch, t0, n=18, icx=900.0, icy=560.0):
    """One press, orchestrator-shaped: arm on the control thread, then n gate+detect frames."""
    mx, my = meter_xy_for(icx, icy)
    r.notify_physical_shot_start(epoch)          # _arm_shot_gate, control thread
    r.set_shot_state(True, 1.0, True)            # _arm_shot_gate, control thread
    t = t0
    for i in range(n):
        f = court()
        draw_plate(f, icx, icy)
        draw_meter(f, mx, my, 5.0 + 4.5 * i)
        r.set_shot_state(True, 0.0, True)        # _processing_loop, gate still armed (20 s)
        r.detect(f, ts=t)                        # _processing_loop
        t += FRAME_DT
    return t


def _pickup_lines(caplog):
    return [m for m in (rec.getMessage() for rec in caplog.records) if "PICKUP:" in m]


def test_real_reader_and_locator_log_one_pickup_line_per_press(clean_env, monkeypatch,
                                                               caplog, real_locator_singletons):
    """THREE presses through the REAL object graph -> THREE lines, hits and misses alike."""
    import logging

    r = _real_reader(monkeypatch, real_locator_singletons)
    base = r._meter_detector._base

    t = 1000.0
    with caplog.at_level(logging.WARNING, logger="simple_reader"):
        for epoch in (101, 102, 103):
            t = _press_frames(r, epoch, t) + 0.25
            # the record open for THIS press is this press's, and still unflushed
            assert base.pickup["epoch"] == epoch, base.pickup
            assert base.pickup["logged"] == 0
        # the rally ends: the hardware arm window falls (_shot_gate_max_seconds) and the
        # last press closes on that edge -- the only close production ever delivers.
        r.set_shot_state(True, 0.0, False)

    msgs = _pickup_lines(caplog)
    assert len(msgs) == 3, msgs
    for line, epoch in zip(msgs, (101, 102, 103)):
        assert ("epoch=%d " % epoch) in line, (epoch, line)
        assert "first_sight_fill=" in line and "anchor_used=" in line


def test_real_reader_logs_a_press_whose_meter_was_never_seen(clean_env, monkeypatch, caplog,
                                                             real_locator_singletons):
    """A miss is the line that matters: a press with no meter on screen still closes with
    first_sight_fill=-1.0, so `grep PICKUP:` is a census of PRESSES, not of hits."""
    import logging

    r = _real_reader(monkeypatch, real_locator_singletons)

    t = 1000.0
    with caplog.at_level(logging.WARNING, logger="simple_reader"):
        r.notify_physical_shot_start(201)
        r.set_shot_state(True, 1.0, True)
        for _ in range(18):                       # court only: no plate, no meter
            r.set_shot_state(True, 0.0, True)
            r.detect(court(), ts=t)
            t += FRAME_DT
        r.set_shot_state(True, 0.0, False)

    msgs = _pickup_lines(caplog)
    assert len(msgs) == 1, msgs
    assert "epoch=201 " in msgs[0]
    assert "first_sight_fill=-1.0" in msgs[0]
    assert "anchor_used=0" in msgs[0]


def test_real_reader_keeps_the_press_when_the_gate_snapshot_is_stale(clean_env, monkeypatch,
                                                                     caplog,
                                                                     real_locator_singletons):
    """THE RACE THE FRAME-CLOCK OPEN LOSES.

    _processing_loop reads (armed, armed_hw) from _shot_gate_state BEFORE _arm_shot_gate
    extends the deadlines, then pushes that stale snapshot into set_shot_state AFTER
    notify_physical_shot_start installed the epoch. set_shot_state's hw-disarm branch zeroes
    _physical_shot_epoch, so _publish_press_window never sees this press and the frame-clock
    open never runs for it. The control-thread open is what keeps its line.
    """
    import logging

    r = _real_reader(monkeypatch, real_locator_singletons)
    mx, my = meter_xy_for(900.0, 560.0)

    t = 1000.0
    with caplog.at_level(logging.WARNING, logger="simple_reader"):
        r.notify_physical_shot_start(301)
        r.set_shot_state(True, 1.0, True)
        r.set_shot_state(True, 0.0, False)        # the stale snapshot lands here
        assert r._physical_shot_epoch == 0        # ... and the press identity is gone
        for i in range(10):
            f = court()
            draw_plate(f, 900.0, 560.0)
            draw_meter(f, mx, my, 10.0 + 4.5 * i)
            r.set_shot_state(True, 0.0, True)
            r.detect(f, ts=t)
            t += FRAME_DT
        # the NEXT press still closes the lost one
        r.notify_physical_shot_start(302)
        r.set_shot_state(True, 1.0, True)

    msgs = _pickup_lines(caplog)
    assert len(msgs) == 1, msgs
    assert "epoch=301 " in msgs[0]


def test_pickup_line_is_logged_at_error_so_the_native_relay_cannot_throttle_it(
        clean_env, monkeypatch, caplog, real_locator_singletons):
    """[ORION_PICKUP_LEVEL 2026-09-15] THE LIVE ROOT CAUSE, pinned.

    RemotePlaySession.cpp:2884 relays sidecar stderr; every WARNING line shares ONE global
    1000 ms slot (kSidecarWarnThrottleMs, RemotePlaySession.h:530) while ERROR/CRITICAL
    bypass it. The flush fires ~one frame after the orchestrator's own `SHOT-GATE ARM
    RECEIPT` WARNING, so at WARNING the PICKUP line loses that race on EVERY press. If this
    ever goes back to WARNING the line goes silent live again with every unit test green.
    """
    import logging

    r = _real_reader(monkeypatch, real_locator_singletons)

    with caplog.at_level(logging.WARNING, logger="simple_reader"):
        _press_frames(r, 401, 1000.0)
        r.set_shot_state(True, 0.0, False)

    recs = [rec for rec in caplog.records if "PICKUP:" in rec.getMessage()]
    assert len(recs) == 1, [rec.getMessage() for rec in recs]
    assert recs[0].levelno >= logging.ERROR, recs[0].levelname


def test_real_reader_is_silent_when_the_anchor_knob_is_off(clean_env, monkeypatch, caplog,
                                                           real_locator_singletons):
    """Shipped default: no anchor, no record, no line, and no new per-press log noise."""
    import logging

    r = _real_reader(monkeypatch, real_locator_singletons)
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "0")

    with caplog.at_level(logging.WARNING, logger="simple_reader"):
        _press_frames(r, 501, 1000.0)
        r.set_shot_state(True, 0.0, False)

    assert not _pickup_lines(caplog)
