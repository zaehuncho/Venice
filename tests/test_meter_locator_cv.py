"""Unit tests for meter_locator_cv.MeterContourLocator -- the pure-CV meter proposer.

Every frame here is synthetic (numpy/cv2), no framedump files. The drawn meter follows the
module docstring @720p: a 26 x 120 dark track with a 1-px light-grey edge on each side of the
column, an 11 px white fill column at track x+7..x+17 rising from row (track top + 108), and a
green tip whose top is track top + 8 -- so the tip top sits 100 px above the white bottom.

The review's defects (even-kernel row shift, outline bands over the fill, tile-local state, top-12 cap
before the width gate, wrapper reset, pad_bot) were fixed on 2026-09-10; their tests now assert the fix;
they flip to XPASS (and fail the run) the moment the fix lands, so the mark can be dropped then.
"""
from __future__ import annotations

import os
import time

import cv2
import numpy as np
import pytest

import meter_locator_cv as mlc

W, H = 1280, 720
FRAME_DT = 1.0 / 60.0

TRACK_W, TRACK_H = 26, 120
COL_X, COL_W = 7, 11              # white column offset inside the track / width
TIP_TOP = 8                       # green tip top = track top + 8
TIP_GAP = 100                     # green top -> white bottom (the locator's tip_gap @720p)
WHITE_BOT = TIP_TOP + TIP_GAP     # 108: white bottom row (exclusive) from the track top
OUTLINE_DX = (3, 21)              # light-grey vertical edges beside the column

COURT_BG = (70, 30, 10)           # dark blue court (BGR)
WHITE = (255, 255, 255)
GREEN = (0, 200, 0)               # HSV H=60 S=255 V=200: inside the tip gate
WASHED_GREEN = (200, 235, 200)    # S~38 (< 90: not green) and spread 35 (> 25: not white)
TRACK = (45, 40, 38)
OUTLINE = (190, 190, 190)         # "light" for the outline gate, never "white" (V < 225)
MENU_GREY = (128, 128, 128)

# Every knob the module reads from the environment; cleared so a dev launcher that pins
# ORION_METER_PROPOSER / ORION_CV_* cannot change what these tests measure.
ENV_KNOBS = (
    "ORION_METER_PROPOSER", "ORION_METER_DETECTOR_MIN_INTERVAL_MS", "ORION_METER_DETECTOR_SYNC",
    "ORION_METER_BAND_TOP", "ORION_METER_GATE_TOP", "ORION_METER_BAND_BOTTOM", "ORION_METER_W_MIN_FRAC",
    "ORION_METER_W_MAX_FRAC", "ORION_METER_H_MIN_FRAC", "ORION_METER_H_MAX_FRAC",
)


# ---------------------------------------------------------------------------- fixtures
@pytest.fixture
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("ORION_CV_") or key in ENV_KNOBS:
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def locator(clean_env):
    """Shipped defaults: outline gate OFF (priced into stats['outline_weak'], not enforced)."""
    return mlc.MeterContourLocator()


@pytest.fixture
def gated_locator(clean_env, monkeypatch):
    """Outline gate at its documented operating point (opt-in via ORION_CV_OUTLINE_MIN=0.45)."""
    monkeypatch.setenv("ORION_CV_OUTLINE_MIN", "0.45")
    loc = mlc.MeterContourLocator()
    assert loc.outline_min == pytest.approx(0.45)
    return loc


@pytest.fixture(params=["default", "gated"])
def any_locator(request):
    return request.getfixturevalue("locator" if request.param == "default" else "gated_locator")


@pytest.fixture
def yolo_module(clean_env, monkeypatch):
    """meter_detector_yolo with its process singletons parked for the test's duration."""
    import meter_detector_yolo as mdy
    monkeypatch.setattr(mdy, "_SINGLETON", None)
    monkeypatch.setattr(mdy, "_ASYNC", None)
    return mdy


# ---------------------------------------------------------------------------- synthetic frames
def court(lines: bool = True) -> np.ndarray:
    f = np.empty((H, W, 3), np.uint8)
    f[:] = COURT_BG
    if lines:
        cv2.line(f, (0, 520), (W - 1, 560), WHITE, 2)                       # baseline, slanted
        cv2.line(f, (300, 200), (330, 700), WHITE, 2)                       # sideline in perspective
        cv2.ellipse(f, (640, 640), (260, 90), 0, 180, 360, WHITE, 2)        # three-point arc
    return f


def draw_meter(f: np.ndarray, x: int, y: int, fill_pct: float, *, outline: bool = True,
               tip: str = "green") -> dict:
    """Arrow2/White meter with its track's top-left at (x, y). Returns the landmarks."""
    cv2.rectangle(f, (x, y), (x + TRACK_W - 1, y + TRACK_H - 1), TRACK, -1)
    if outline:
        for dx in OUTLINE_DX:
            f[y:y + TRACK_H, x + dx] = OUTLINE
    gtop = y + TIP_TOP
    tri = np.array([[x + 12, gtop], [x + 13, gtop], [x + 19, gtop + 10], [x + 6, gtop + 10]], np.int32)
    if tip == "green":
        cv2.fillPoly(f, [tri], GREEN)
    elif tip == "washed":
        cv2.fillPoly(f, [tri], WASHED_GREEN)
    wbot = y + WHITE_BOT
    hpx = int(round(TIP_GAP * fill_pct / 100.0))
    if hpx > 0:
        # drawn AFTER the tip: past ~88 % the fill overlays the tip, as on the real meter
        f[wbot - hpx:wbot, x + COL_X:x + COL_X + COL_W] = WHITE
    notch = np.array([[x + 7, wbot + 1], [x + 17, wbot + 1], [x + 12, wbot + 7]], np.int32)
    cv2.fillPoly(f, [notch], OUTLINE)
    return {"x": x, "y": y, "gtop": gtop, "wbot": wbot, "cx": x + COL_X + COL_W / 2.0}


def meter_frame(x: int = 600, y: int = 300, fill_pct: float = 40.0, **kw):
    f = court()
    lm = draw_meter(f, x, y, fill_pct, **kw)
    return f, lm


def expected_box(loc: mlc.MeterContourLocator, lm: dict, s: float = 1.0):
    """The box the locator's contract builds from the two landmarks."""
    bw = round(loc.box_w * s)
    bx = round(lm["cx"] * s - bw * 0.5)
    by = round(lm["gtop"] * s - loc.box_pad_top * s)
    bh = round((lm["wbot"] - lm["gtop"]) * s + (loc.box_pad_top + loc.box_pad_bot) * s)
    return bx, by, bw, bh


def assert_box_close(box, exp, tol: int = 2):
    assert box is not None, "no detection"
    for name, got, want in zip("xywh", box[:4], exp):
        assert abs(got - want) <= tol, f"{name}: got {got}, expected {want} +-{tol} (box={box[:4]})"


# ---------------------------------------------------------------------------- 1. geometry
# 15 % is the lowest fill judged on first sight since gate 9 (2026-09-12): a run shorter than 14 px
# is deferred one frame (see test_short_fill_is_deferred_until_it_is_judgeable).
@pytest.mark.parametrize("fill", [15.0, 40.0, 90.0])
def test_detects_drawn_meter_within_2px_at_fill(locator, fill):
    frame, lm = meter_frame(fill_pct=fill)
    box = locator.detect_box(frame, ts=1.0)
    assert_box_close(box, expected_box(locator, lm))
    assert box[4] == pytest.approx(0.90)
    assert locator.stats["hit"] == 1 and locator.stats["no_tip"] == 0


def test_meter_tall_column_with_washed_tip_is_found_at_conf_075(locator):
    # 96 %: the white reaches the tip and washes its green out; the column alone is meter-tall.
    #
    # [ORION_CV_TIP_CORROBORATE 2026-09-11] Geometry alone no longer admits an unconfirmed column
    # on FIRST sight -- a court line at the right angle is also an achromatic ~90 px bar, and the
    # owner sees it as the box snapping onto the court for a split second. The unconfirmed path now
    # costs one frame: remembered, then accepted when it is still there. Live, that is ~17 ms, and
    # a real meter has already been confirmed at conf 0.90 all the way up its rise.
    frame, lm = meter_frame(fill_pct=96.0, tip="washed")
    assert locator.detect_box(frame, ts=1.0) is None, "first sight of an unconfirmed column is remembered, not taken"
    assert locator.stats["no_tip_lone"] == 1
    box = locator.detect_box(frame, ts=1.0 + 1.0 / 60.0)      # same place, next frame
    assert_box_close(box, expected_box(locator, lm))
    assert box[4] == pytest.approx(0.75)


def test_one_frame_court_stranger_never_locks(locator):
    # [ORION_CV_TIP_CORROBORATE 2026-09-11] The defect this closes, in its live shape: a meter-tall
    # achromatic bar that appears for a single frame and is gone. It must never produce a box.
    frame, _ = meter_frame(fill_pct=96.0, tip="washed")
    assert locator.detect_box(frame, ts=5.0) is None
    empty = court()
    assert locator.detect_box(empty, ts=5.0 + 1.0 / 60.0) is None
    # and it must not linger: the same bar a full second later is a fresh stranger again
    assert locator.detect_box(frame, ts=6.5) is None
    assert locator.stats["hit"] == 0


def test_confirmed_green_tip_is_never_delayed_by_the_corroboration(locator):
    # The 0.90 path must be untouched: a real rising meter locks on its FIRST frame, as before.
    frame, lm = meter_frame(fill_pct=40.0)
    box = locator.detect_box(frame, ts=9.0)
    assert_box_close(box, expected_box(locator, lm))
    assert box[4] == pytest.approx(0.90)
    assert locator.stats["no_tip_lone"] == 0


def test_box_geometry_is_landmark_built_not_track_built(locator):
    # The docstring's contract: box top = tip top - 8, width 26, height = gap + both pads.
    frame, lm = meter_frame(fill_pct=40.0)
    x, y, w, h, _ = locator.detect_box(frame, ts=1.0)
    assert abs(y - (lm["gtop"] - round(locator.box_pad_top))) <= 1
    assert abs(x - lm["x"]) <= 1 and w == round(locator.box_w)
    assert abs(h - round(TIP_GAP + locator.box_pad_top + locator.box_pad_bot)) <= 1


def test_white_bottom_landmark_is_not_shifted_by_the_morphology(locator):
    frame, lm = meter_frame(fill_pct=40.0)
    x, y, w, h, _ = locator.detect_box(frame, ts=1.0)
    assert y + h == lm["wbot"] + round(locator.box_pad_bot)
    assert h == round(TIP_GAP + locator.box_pad_top + locator.box_pad_bot)


def test_box_bottom_lands_on_the_track_bottom(locator):
    frame, lm = meter_frame(fill_pct=40.0)
    x, y, w, h, _ = locator.detect_box(frame, ts=1.0)
    # pads 4/3 (2026-09-11): the box ends box_pad_bot below the white bottom, not on the track bottom
    assert y + h == lm["wbot"] + round(locator.box_pad_bot)


def test_scales_with_frame_height_1080p(locator):
    frame720, lm = meter_frame(fill_pct=40.0)
    frame = cv2.resize(frame720, (1920, 1080), interpolation=cv2.INTER_NEAREST)
    box = locator.detect_box(frame, ts=1.0)
    assert_box_close(box, expected_box(locator, lm, s=1.5), tol=3)
    assert box[4] == pytest.approx(0.90)


# ---------------------------------------------------------------------------- 2. negatives
def test_no_detection_on_empty_court(locator):
    assert locator.detect_box(court(), ts=1.0) is None
    assert locator.stats["hit"] == 0


def test_no_detection_on_nameplate_under_a_green_ball(locator):
    f = court()
    cv2.rectangle(f, (560, 418), (700, 442), (50, 50, 50), -1)                     # translucent plate
    cv2.putText(f, "J. TATUM", (566, 436), cv2.FONT_HERSHEY_SIMPLEX, 0.6, WHITE, 2)
    cv2.circle(f, (630, 338), 9, GREEN, -1)                                       # ball 100 px above the text bottom
    assert locator.detect_box(f, ts=1.0) is None
    assert locator.stats["hit"] == 0


def test_no_detection_on_menu_screen(locator):
    f = np.empty((H, W, 3), np.uint8)
    f[:] = MENU_GREY
    cv2.putText(f, "SETTINGS", (380, 300), cv2.FONT_HERSHEY_SIMPLEX, 2.2, WHITE, 8)
    cv2.putText(f, "CONTROLLER", (380, 400), cv2.FONT_HERSHEY_SIMPLEX, 1.6, WHITE, 6)
    cv2.putText(f, "DISPLAY", (380, 480), cv2.FONT_HERSHEY_SIMPLEX, 1.6, WHITE, 6)
    assert locator.detect_box(f, ts=1.0) is None
    assert locator.stats["hit"] == 0


def test_no_detection_on_a_white_court_line_alone(locator):
    f = court(lines=False)
    cv2.line(f, (640, 250), (640, 550), WHITE, 4)                                 # vertical, 4 px
    cv2.line(f, (200, 600), (1100, 600), WHITE, 3)                                # horizontal, 3 px
    assert locator.detect_box(f, ts=1.0) is None
    assert locator.stats["hit"] == 0


# ---------------------------------------------------------------------------- 3. outline gate
def test_default_outline_gate_is_off_and_only_priced(locator):
    # Shipped default (ORION_CV_OUTLINE_MIN unset -> 0.0): an outline-less meter is accepted at
    # first sight, full confidence, and the weak outline is only counted.
    frame, lm = meter_frame(fill_pct=30.0, outline=False)
    box = locator.detect_box(frame, ts=50.0)
    assert_box_close(box, expected_box(locator, lm))
    assert box[4] == pytest.approx(0.90)
    assert locator.stats.get("outline_weak", 0) == 1
    assert locator.stats["no_outline"] == 0 and locator.stats["outline_bridged"] == 0
    # ... and a meter WITH its outline is not counted weak
    frame, _ = meter_frame(fill_pct=30.0, outline=True)
    assert locator.detect_box(frame, ts=50.0 + FRAME_DT) is not None
    assert locator.stats.get("outline_weak", 0) == 1


def test_outline_less_meter_refused_first_then_bridged_next_frame(gated_locator):
    loc = gated_locator
    frame, lm = meter_frame(fill_pct=30.0, outline=False)
    t0 = 50.0
    assert loc.detect_box(frame, ts=t0) is None
    assert loc.stats["no_outline"] == 1 and loc.stats["outline_bridged"] == 0
    box = loc.detect_box(frame, ts=t0 + FRAME_DT)
    assert_box_close(box, expected_box(loc, lm))
    assert box[4] <= 0.7
    assert loc.stats["outline_bridged"] == 1 and loc.stats["hit"] == 1
    # once accepted, the accepted box itself keeps bridging (still capped at 0.7)
    box2 = loc.detect_box(frame, ts=t0 + 2 * FRAME_DT)
    assert box2 is not None and box2[4] <= 0.7
    assert loc.stats["outline_bridged"] == 2


def test_outlined_meter_passes_the_gate_at_first_sight(gated_locator):
    frame, lm = meter_frame(fill_pct=30.0, outline=True)
    box = gated_locator.detect_box(frame, ts=50.0)
    assert_box_close(box, expected_box(gated_locator, lm))
    assert box[4] == pytest.approx(0.90)
    assert gated_locator.stats["no_outline"] == 0 and gated_locator.stats["outline_bridged"] == 0


def test_outline_less_stranger_then_gone_stays_refused(gated_locator):
    loc = gated_locator
    stranger, _ = meter_frame(fill_pct=30.0, outline=False)
    t0 = 50.0
    assert loc.detect_box(stranger, ts=t0) is None
    assert loc.detect_box(court(), ts=t0 + FRAME_DT) is None
    assert loc.stats["hit"] == 0 and loc.stats["outline_bridged"] == 0
    assert loc.stats["no_outline"] == 1


def test_outline_less_candidate_is_not_bridged_after_the_recent_window(gated_locator):
    loc = gated_locator
    frame, _ = meter_frame(fill_pct=30.0, outline=False)
    t0 = 50.0
    assert loc.detect_box(frame, ts=t0) is None
    assert loc.detect_box(frame, ts=t0 + loc.outline_recent_s + 0.05) is None
    assert loc.stats["no_outline"] == 2 and loc.stats["outline_bridged"] == 0


def test_outline_gate_is_not_satisfied_by_the_fill_itself(gated_locator):
    frame, _ = meter_frame(fill_pct=80.0, outline=False)
    assert gated_locator.detect_box(frame, ts=50.0) is None
    assert gated_locator.stats["no_outline"] == 1


# ---------------------------------------------------------------------------- 4. roaming ROI
def test_moving_meter_is_followed_by_the_roaming_roi(any_locator):
    loc = any_locator
    n = 12
    for i in range(n):
        x = 500 + 6 * i
        frame, lm = meter_frame(x=x, fill_pct=20.0 + 5.0 * i)
        box = loc.detect_box(frame, ts=100.0 + i * FRAME_DT)
        assert_box_close(box, expected_box(loc, lm))
        assert box[4] == pytest.approx(0.90)
    assert loc.stats["hit"] == n
    assert loc.stats["roi"] >= n - 1
    assert loc.stats["full"] == 1
    assert loc.stats["no_outline"] == 0 and loc.stats["outline_bridged"] == 0


def test_roi_miss_reopens_the_band_in_the_same_call(locator):
    frame_a, _ = meter_frame(x=500, fill_pct=40.0)
    frame_b, lm_b = meter_frame(x=900, fill_pct=40.0)
    assert locator.detect_box(frame_a, ts=100.0) is not None
    full_before, roi_before = locator.stats["full"], locator.stats["roi"]
    box = locator.detect_box(frame_b, ts=100.0 + FRAME_DT)
    assert_box_close(box, expected_box(locator, lm_b))
    assert locator.stats["roi"] == roi_before + 1
    assert locator.stats["full"] == full_before + 1


def test_roi_hold_expires_on_frame_time(locator):
    frame, _ = meter_frame(fill_pct=40.0)
    assert locator.detect_box(frame, ts=100.0) is not None
    assert locator.detect_box(frame, ts=100.0 + locator.roi_hold_s + 0.1) is not None
    assert locator.stats["roi"] == 0 and locator.stats["full"] == 2


def test_none_or_nan_ts_falls_back_to_monotonic_clock(locator):
    frame, _ = meter_frame(fill_pct=40.0)
    assert locator.detect_box(frame) is not None
    assert abs(locator._last_ts - time.monotonic()) < 2.0
    assert locator.detect_box(frame, ts=float("nan")) is not None
    assert abs(locator._last_ts - time.monotonic()) < 2.0


def test_wide_white_blobs_do_not_crowd_out_the_column(locator):
    f = court(lines=False)
    for i in range(13):
        x = 40 + 90 * i
        cv2.rectangle(f, (x, 150), (x + 40, 190), WHITE, -1)                     # 40 x 40, width > 22
    lm = draw_meter(f, 600, 300, 40.0)
    assert_box_close(locator.detect_box(f, ts=1.0), expected_box(locator, lm))


# ---------------------------------------------------------------------------- 4b. shape gate (2026-09-12)
def jersey_strip(f: np.ndarray, x: int, y: int, h: int = 50) -> None:
    """A lit white strip between two jersey folds -- 9..16 px wide, wandering row by row -- with a
    big green blob 100 px above its bottom (the owner's alien head). Gates 1-8 all pass it."""
    for r in range(h):
        w = 9 + int(7 * abs(np.sin(r / 6.0)))
        f[y + r, x + (r % 3):x + (r % 3) + w] = WHITE
    cv2.circle(f, (x + 8, y + h - TIP_GAP + 4), 22, GREEN, -1)


def test_shape_gate_refuses_a_jersey_strip_under_a_green_blob(locator, monkeypatch):
    f = court(lines=False)
    jersey_strip(f, 600, 300)
    assert locator.detect_box(f, ts=1.0) is None
    assert locator.stats["shape_irregular"] + locator.stats["tip_spill"] >= 1
    # the same frame with the gate off IS taken: the older gates never saw the difference
    monkeypatch.setenv("ORION_CV_SHAPE_GATE", "0")
    off = mlc.MeterContourLocator()
    assert off.detect_box(f, ts=1.0) is not None


def test_shape_gate_refuses_a_straight_strip_whose_green_is_not_a_tip(locator):
    # a clean 11 px white rectangle (a sock, the scoreboard's white bar) under a WIDE green band:
    # the fill shape passes, the tip does not -- a real tip is confined to the capsule
    f = court(lines=False)
    f[350:400, 600:611] = WHITE
    cv2.rectangle(f, (560, 298), (650, 306), GREEN, -1)
    assert locator.detect_box(f, ts=1.0) is None
    assert locator.stats["tip_spill"] == 1 and locator.stats["shape_irregular"] == 0


def test_a_green_court_line_behind_the_meter_is_not_a_spill(locator):
    # the 3-pt line passes at tip height; the capsule covers it inside the 8-14 px ring
    f = court(lines=False)
    cv2.line(f, (300, 320), (900, 296), GREEN, 3)
    lm = draw_meter(f, 600, 300, 40.0)
    box = locator.detect_box(f, ts=1.0)
    assert_box_close(box, expected_box(locator, lm))
    assert locator.stats["tip_spill"] == 0


def test_short_fill_is_deferred_until_it_is_judgeable(locator):
    # at 10 % the white run is 10 px: not judged, not taken (a static 8 x 10 px jersey bit is the
    # false lock that survives every other gate). A real fill grows ~4 px per 60 fps frame and is
    # taken at 14+ px: one frame live.
    f10, _ = meter_frame(fill_pct=10.0)
    assert locator.detect_box(f10, ts=1.0) is None
    assert locator.stats["shape_short"] == 1 and locator.stats["hit"] == 0
    f15, lm = meter_frame(fill_pct=15.0)
    box = locator.detect_box(f15, ts=1.0 + FRAME_DT)
    assert_box_close(box, expected_box(locator, lm))
    assert box[4] == pytest.approx(0.90)


def test_shape_gate_bridges_a_meter_that_was_just_accepted(locator):
    # a meter accepted a moment ago is not first sight: the frame where an arm crosses the fill
    # (an irregular run) keeps the box; the same frame cold is refused
    f40, lm = meter_frame(fill_pct=40.0)
    assert locator.detect_box(f40, ts=1.0) is not None
    f_occ, _ = meter_frame(fill_pct=40.0)
    f_occ[lm["wbot"] - 25:lm["wbot"] - 15, lm["x"] + COL_X + 6:lm["x"] + COL_X + COL_W] = TRACK
    box = locator.detect_box(f_occ, ts=1.0 + FRAME_DT)
    assert_box_close(box, expected_box(locator, lm))
    assert locator.stats["shape_bridged"] >= 1 and locator.stats["shape_irregular"] == 0
    cold = mlc.MeterContourLocator()
    assert cold.detect_box(f_occ, ts=5.0) is None
    assert cold.stats["shape_irregular"] == 1


def test_full_meter_with_its_arrow_top_is_taken_at_first_sight(locator):
    # past ~90 % the fill narrows into the capsule's arrow head; those rows are not "irregular
    # width", they are the meter. A post-release meter re-acquired cold must still be proposed
    # (the landing grade needs it), so the arrow rows are excluded from the judged body.
    f, lm = meter_frame(fill_pct=100.0)
    gtop, x = lm["gtop"], lm["x"]
    for r in range(10):                                      # apex: 1 px wide at the top, full at +10
        keep = max(1, int(round(COL_W * (r + 1) / 10.0)))
        cut = (COL_W - keep) // 2
        f[gtop + r, x + COL_X:x + COL_X + cut] = TRACK
        f[gtop + r, x + COL_X + cut + keep:x + COL_X + COL_W] = TRACK
    box = locator.detect_box(f, ts=1.0)
    assert box is not None and locator.stats["shape_irregular"] == 0
    assert abs(box[0] - lm["x"]) <= 2


def test_shape_gate_lets_the_meter_win_over_a_bigger_jersey_strip(locator):
    # the shooter's own white jersey out-ranks the meter on area; refusing it INSIDE the loop
    # hands the frame to the next candidate instead of to nothing
    f = court(lines=False)
    jersey_strip(f, 500, 280, h=80)
    lm = draw_meter(f, 640, 300, 30.0)
    box = locator.detect_box(f, ts=1.0)
    assert_box_close(box, expected_box(locator, lm))


# ---------------------------------------------------------------------------- 5. tile path
def test_tile_maps_boxes_back_to_full_frame_coordinates(locator):
    frame, lm = meter_frame(x=900, fill_pct=40.0)
    assert_box_close(locator.detect_box_tile(frame, "right"), expected_box(locator, lm))
    assert locator.detect_box_tile(frame, "left") is None
    frame, lm = meter_frame(x=100, fill_pct=40.0)
    assert_box_close(locator.detect_box_tile(frame, "left"), expected_box(locator, lm))
    assert locator.detect_box_tile(frame, "right") is None
    assert locator.detect_box_tile(frame, "middle") is None


def test_gate_top_follows_the_scan_band_not_the_old_band_top(locator, clean_env, monkeypatch):
    # [ORION_METER_GATE_TOP 2026-09-12] Track top at row 60: box centre at row 118 (0.164). The
    # scan has always covered it (band_top - 0.12 = 0.08 -> row 57) but the acceptance gate sat at
    # band_top (0.20 -> row 144) and refused it -- a jumping fade or a far shot puts the real
    # meter exactly here (highest live centre measured 0.216). The gate now sits at the scan's
    # own top, so this meter is taken on both paths...
    frame, lm = meter_frame(x=900, y=60, fill_pct=40.0)
    assert_box_close(locator.detect_box_tile(frame, "right"), expected_box(locator, lm))
    assert_box_close(locator.detect_box(frame, ts=1.0), expected_box(locator, lm))
    # ...and the old behaviour is one env knob away (the gate alone; the scan band is untouched).
    monkeypatch.setenv("ORION_METER_GATE_TOP", "0.20")
    old = mlc.MeterContourLocator()
    assert old.detect_box_tile(frame, "right") is None
    assert old.detect_box(frame, ts=1.0) is None
    assert old.stats["col_cands"] >= 2 and old.stats["hit"] == 0


def test_tile_hit_seeds_the_roi_in_full_frame_coordinates(locator):
    frame, lm = meter_frame(x=900, fill_pct=40.0)
    assert locator.detect_box_tile(frame, "right") is not None
    assert abs(locator._last_box[0] - lm["x"]) <= 2
    full_before = locator.stats["full"]
    assert locator.detect_box(frame, ts=locator._last_ts + FRAME_DT) is not None
    assert locator.stats["full"] == full_before


# ---------------------------------------------------------------------------- 6. wrapper
def test_get_locator_returns_cv_locator_and_detect_now_passes_frame_ts(yolo_module, monkeypatch):
    monkeypatch.setenv("ORION_METER_PROPOSER", "cv")
    base = yolo_module.get_locator()
    assert isinstance(base, mlc.MeterContourLocator)
    assert base.provider == "cv-contour" and getattr(base, "accepts_ts", False) is True
    assert yolo_module.get_locator() is base

    wrapper = yolo_module.AsyncMeterLocator(base, sync=True)
    assert wrapper.ok and wrapper._min_interval_s == 0.0

    frame, lm = meter_frame(fill_pct=40.0)
    ts = 123.456
    found, box, conf, out_ts = wrapper.detect_now(frame, ts)
    assert found and out_ts == ts and conf == pytest.approx(0.90)
    assert_box_close((*box, conf), expected_box(base, lm))
    assert base._last_ts == ts
    assert wrapper.latest() == (True, box, conf, ts)

    frame, lm = meter_frame(x=900, fill_pct=40.0)
    ts2 = ts + FRAME_DT
    found, box, conf, out_ts = wrapper.detect_now_region(frame, ts2, "right")
    assert found and out_ts == ts2 and base._last_ts == ts2
    assert_box_close((*box, conf), expected_box(base, lm))
    assert wrapper.latest_details()[-1] == "right"


def test_get_async_locator_wraps_the_cv_singleton(yolo_module, monkeypatch):
    monkeypatch.setenv("ORION_METER_PROPOSER", "cv")
    monkeypatch.setenv("ORION_METER_DETECTOR_SYNC", "1")
    wrapper = yolo_module.get_async_locator()
    try:
        assert wrapper is not None and wrapper._base is yolo_module.get_locator()
        assert wrapper.provider == "cv-contour" and wrapper._sync
        frame, _ = meter_frame(fill_pct=40.0)
        wrapper.submit(frame, 5.0)
        found, box, conf, ts = wrapper.latest()
        assert found and ts == 5.0 and wrapper._base._last_ts == 5.0
    finally:
        wrapper.stop()


def test_wrapper_reset_clears_the_base_temporal_state(yolo_module):
    base = mlc.MeterContourLocator()
    wrapper = yolo_module.AsyncMeterLocator(base, sync=True)
    frame, _ = meter_frame(fill_pct=40.0)
    assert wrapper.detect_now(frame, 10.0)[0]
    assert base._last_box is not None
    wrapper.reset()
    assert base._last_box is None and base._pending is None


def test_async_worker_passes_the_frame_ts_to_the_base(clean_env):
    import meter_detector_yolo as mdy
    base = mlc.MeterContourLocator()
    wrapper = mdy.AsyncMeterLocator(base)          # threaded, as live constructs it
    try:
        assert wrapper._thread is not None and wrapper._thread.is_alive()
        frame, lm = meter_frame(fill_pct=40.0)
        wrapper.submit(frame, 777.0)
        deadline = time.perf_counter() + 0.8
        result = wrapper.latest()
        while not result[0] and time.perf_counter() < deadline:
            time.sleep(0.002)
            result = wrapper.latest()
        found, box, conf, ts = result
        assert found and ts == 777.0
        assert_box_close((*box, conf), expected_box(base, lm))
        assert base._last_ts == 777.0
    finally:
        wrapper.stop()


# ------------------------------------------------- 10. ORION_METER_TOP_STRIP (far shots)
# The scan sub-image starts at (band_top - 0.12) * H = row 57 @720p. Gate 5 reads the green
# tip a fixed 100 px ABOVE the white column's bottom, so for a meter whose tip sits above
# row 57 that window is clamped to the sub-image's first row, lands on the wrong pixels and
# the meter dies as `no_tip` -- even though the ACCEPTANCE gate (_gate_top 0.08 = centre row
# 57.6, i.e. box top ~4) would have taken it. Measured on 245,821 detected boxes across 18
# detframes sessions the box-top histogram is censored exactly there (223 boxes at y 70-79,
# 4 at 60-69, 0 at 50-59). ORION_METER_TOP_STRIP reads those two small windows from the
# parent frame instead; it scans no extra pixels and is default-OFF.
# Empirical (parameter sweep, 2 px steps): the shipped locator is BLIND for a track top
# <= 40 (box top <= 44) and, between 44 and 48, still finds the meter but seats the box
# up to 5 px low and 5 px short -- a silently wrong fill denominator. Exact from 50 up.
TOP_STRIP_BLIND_MAX_Y = 40        # highest track top the shipped scan cannot see at all
TOP_STRIP_EXACT_MIN_Y = 50        # lowest track top the shipped scan seats exactly


@pytest.fixture
def top_strip_locator(clean_env, monkeypatch):
    monkeypatch.setenv("ORION_METER_TOP_STRIP", "1")
    loc = mlc.MeterContourLocator()
    assert loc.top_strip is True
    return loc


@pytest.fixture
def clamped_locator(clean_env, monkeypatch):
    """The pre-2026-09-14 behaviour (tip/spill windows clamped to the scan sub-image)."""
    monkeypatch.setenv("ORION_METER_TOP_STRIP", "0")
    loc = mlc.MeterContourLocator()
    assert loc.top_strip is False
    return loc


def test_top_strip_knob_defaults_off(locator):
    # Offline-validated (0/12,000 frames changed, 61/80 recovered) but kept opt-in until a live
    # A/B clears it: it shipped alongside two other regressions on 2026-09-14 (see the locator).
    assert locator.top_strip is False


@pytest.mark.parametrize("track_top", [4, 10, 25, 34, 40])
def test_meter_above_the_scan_top_is_refused_with_the_knob_off(clamped_locator, track_top):
    frame, _ = meter_frame(y=track_top, fill_pct=40.0)
    assert clamped_locator.detect_box(frame, ts=1.0) is None


@pytest.mark.parametrize("track_top", [4, 10, 25, 34, 40])
def test_meter_above_the_scan_top_is_found_with_the_knob(top_strip_locator, track_top):
    frame, lm = meter_frame(y=track_top, fill_pct=40.0)
    box = top_strip_locator.detect_box(frame, ts=1.0)
    assert_box_close(box, expected_box(top_strip_locator, lm))
    assert box[4] == pytest.approx(0.90)        # the green tip was CONFIRMED, not assumed


@pytest.mark.parametrize("fill", [15.0, 40.0, 90.0])
def test_high_meter_is_found_at_every_fill_with_the_knob(top_strip_locator, fill):
    frame, lm = meter_frame(y=10, fill_pct=fill)
    assert_box_close(top_strip_locator.detect_box(frame, ts=1.0),
                     expected_box(top_strip_locator, lm))


def test_shipped_floor_is_the_scan_top_not_the_acceptance_gate(clamped_locator, top_strip_locator):
    """Either side of the floor: the clamped scan goes blind, the knob does not."""
    locator = clamped_locator
    frame_ok, lm_ok = meter_frame(y=TOP_STRIP_EXACT_MIN_Y, fill_pct=40.0)
    frame_blind, lm_blind = meter_frame(y=TOP_STRIP_BLIND_MAX_Y, fill_pct=40.0)
    assert_box_close(locator.detect_box(frame_ok, ts=1.0), expected_box(locator, lm_ok))
    locator.reset()
    assert locator.detect_box(frame_blind, ts=2.0) is None
    assert_box_close(top_strip_locator.detect_box(frame_blind, ts=2.0),
                     expected_box(top_strip_locator, lm_blind))


def test_knob_also_fixes_the_box_seat_in_the_boundary_band(clamped_locator, top_strip_locator):
    """Between the blind zone and the exact zone the clamped tip window seats the box low
    and short -- the box IS the fill denominator, so that is a silent timing error."""
    locator = clamped_locator
    frame, lm = meter_frame(y=46, fill_pct=40.0)
    exp = expected_box(locator, lm)
    shipped = locator.detect_box(frame, ts=1.0)
    assert shipped is not None and abs(shipped[1] - exp[1]) >= 3
    assert_box_close(top_strip_locator.detect_box(frame, ts=1.0), exp)


def test_knob_does_not_lift_the_acceptance_gate(top_strip_locator):
    """A meter clipped by the frame's own top edge still fails _plausible (centre < 0.08)."""
    frame, _ = meter_frame(y=0, fill_pct=40.0)
    assert top_strip_locator.detect_box(frame, ts=1.0) is None


@pytest.mark.parametrize("track_top,fill", [(300, 15.0), (300, 40.0), (300, 90.0),
                                            (120, 40.0), (60, 40.0)])
def test_knob_is_a_no_op_on_a_normal_height_meter(locator, top_strip_locator, track_top, fill):
    frame, _ = meter_frame(y=track_top, fill_pct=fill)
    assert locator.detect_box(frame, ts=1.0) == top_strip_locator.detect_box(frame, ts=1.0)


def test_knob_does_not_open_the_known_false_locks(top_strip_locator):
    """The frames the gates were built to refuse stay refused with the knob on -- including
    the nameplate/ball pair pushed up into the strip the knob unlocks."""
    assert top_strip_locator.detect_box(court(), ts=1.0) is None
    for plate_y in (418, 118):
        f = court()
        cv2.rectangle(f, (560, plate_y), (700, plate_y + 24), (50, 50, 50), -1)
        cv2.putText(f, "J. TATUM", (566, plate_y + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.6, WHITE, 2)
        cv2.circle(f, (630, plate_y - 80), 9, GREEN, -1)     # ball ~100 px above the text bottom
        top_strip_locator.reset()
        assert top_strip_locator.detect_box(f, ts=2.0) is None, f"plate at y={plate_y}"
    menu = np.empty((H, W, 3), np.uint8)
    menu[:] = MENU_GREY
    cv2.putText(menu, "SETTINGS", (380, 120), cv2.FONT_HERSHEY_SIMPLEX, 2.2, WHITE, 8)
    cv2.putText(menu, "CONTROLLER", (380, 220), cv2.FONT_HERSHEY_SIMPLEX, 1.6, WHITE, 6)
    top_strip_locator.reset()
    assert top_strip_locator.detect_box(menu, ts=3.0) is None
    line = court(lines=False)
    cv2.line(line, (640, 20), (640, 320), WHITE, 4)
    top_strip_locator.reset()
    assert top_strip_locator.detect_box(line, ts=4.0) is None
    assert top_strip_locator.stats["hit"] == 0


def test_knob_costs_nothing_on_an_idle_frame(locator, top_strip_locator):
    """No candidate -> the parent-frame path is never reached: same work, same time."""
    frame = court()
    for loc in (locator, top_strip_locator):
        for _ in range(3):
            loc.detect_box(frame, ts=0.0)          # warm
    def median_ms(loc):
        t = []
        for i in range(21):
            loc.detect_box(frame, ts=float(i))
            t.append(loc.last_ms)
        return sorted(t)[len(t) // 2]
    off_ms, on_ms = median_ms(locator), median_ms(top_strip_locator)
    assert on_ms <= off_ms + 1.5, f"idle cost {on_ms:.2f} ms vs {off_ms:.2f} ms"
    assert locator.stats["col_cands"] == top_strip_locator.stats["col_cands"]


def test_top_strip_counter_only_moves_when_the_path_fires(top_strip_locator):
    normal, _ = meter_frame(y=300, fill_pct=40.0)
    assert top_strip_locator.detect_box(normal, ts=1.0) is not None
    assert top_strip_locator.stats["top_strip"] == 0
    top_strip_locator.reset()
    high, _ = meter_frame(y=10, fill_pct=40.0)
    assert top_strip_locator.detect_box(high, ts=2.0) is not None
    assert top_strip_locator.stats["top_strip"] >= 1


def test_tile_scan_also_recovers_the_high_meter(clamped_locator, top_strip_locator):
    """detect_box_tile hands _find a COLUMN slice; the parent window must be resolved in
    that slice's coordinates, not the full frame's."""
    frame, lm = meter_frame(x=1000, y=12, fill_pct=40.0)
    assert clamped_locator.detect_box_tile(frame, "right", 0.55, ts=1.0) is None
    box = top_strip_locator.detect_box_tile(frame, "right", 0.55, ts=1.0)
    assert_box_close(box, expected_box(top_strip_locator, lm))


# ------------------------------------------------- 11. ORION_CV_GREEN_S_MIN (decoder route)
def test_green_saturation_floor_defaults_to_the_capture_card_value(locator):
    assert locator._GREEN_LO == (38, 90, 90)


def test_green_saturation_floor_is_a_per_route_knob(clean_env, monkeypatch):
    """The sidecar sets 60 on the decoder route (H.264 4:2:0 washes the tip's chroma); the
    capture-card path never sets it and stays byte-identical at 90."""
    monkeypatch.setenv("ORION_CV_GREEN_S_MIN", "60")
    loc = mlc.MeterContourLocator()
    assert loc._GREEN_LO == (38, 60, 90)
    assert mlc.MeterContourLocator._GREEN_LO == (38, 90, 90)   # class constant untouched
    frame, lm = meter_frame(y=200, fill_pct=40.0)
    assert_box_close(loc.detect_box(frame, ts=1.0), expected_box(loc, lm))


# ------------------------------------- 12. ORION_CV_TIPLESS_ARMED (the tip-less acceptance path)
# Measured on session_20260915_185359, epochs 8 (Right Fade) and 21 (Left Fade): the meter was on
# screen, unoccluded, mid-frame, its white column rising 19 -> 42 -> 63 -> 95 px (ep8) and
# 26 -> 49 -> 72 -> 104 px (ep21), and the strict-green count in the apex window read 0-4 px for the
# WHOLE rise (every locked shot in that session reads 12-36). Both died `no_tip` and the engine fired
# blind. `tip="washed"` is exactly that frame: bright, achromatic-ish, and not green.
T0 = 1000.0                       # press time used by the armed fixtures below


@pytest.fixture
def arm(clean_env):
    """Arm a press on the locator's own (frame) clock and hand it back; always disarmed."""
    def _arm(shot_type="Standstill", epoch=7, press_ts=T0):
        mlc._pa.ARM.note_press(epoch, press_ts, shot_type)
        return epoch
    mlc._pa.ARM.reset()
    try:
        yield _arm
    finally:
        mlc._pa.ARM.reset()


def rising_pair(locator, *, fills=(40.0, 46.0), dt=FRAME_DT, t=T0 + 0.20, **kw):
    """Two consecutive frames of a GROWING tip-less meter. -> (first_box, second_box, lm)."""
    f1, _ = meter_frame(fill_pct=fills[0], tip="washed", **kw)
    b1 = locator.detect_box(f1, ts=t)
    f2, lm = meter_frame(fill_pct=fills[1], tip="washed", **kw)
    b2 = locator.detect_box(f2, ts=t + dt)
    return b1, b2, lm


def test_tipless_knob_is_armed_by_default(locator):
    assert locator.tipless_armed is True
    assert locator.tipless_conf == pytest.approx(0.70)


def test_tipless_needs_a_press__idle_frames_are_the_shipped_refusal(locator):
    """The court-line false-lock class: nothing may change while no press is armed."""
    mlc._pa.ARM.reset()
    b1, b2, _ = rising_pair(locator)
    assert b1 is None and b2 is None
    assert locator.stats["no_tip"] == 2
    assert locator.stats["tipless_pending"] == 0 and locator.stats["tipless_accept"] == 0


def test_tipless_locks_on_the_second_rising_frame(locator, arm):
    """ep8/ep21 in miniature: refused on first sight, taken once it has GROWN."""
    arm("Standstill")
    b1, b2, lm = rising_pair(locator)
    assert b1 is None, "first sight of a tip-less column is remembered, not taken"
    assert locator.stats["tipless_pending"] == 1
    assert_box_close(b2, expected_box(locator, lm))
    assert b2[4] == pytest.approx(0.70)
    assert locator.stats["tipless_accept"] == 1
    assert locator.stats["no_tip"] == 0


def test_tipless_uses_this_shot_types_own_onset_window(locator, arm):
    """A fade's meter is due 575-1075 ms after the press; 200 ms is outside ITS window."""
    arm("Right Fade")
    early1, early2, _ = rising_pair(locator, t=T0 + 0.20)
    assert early1 is None and early2 is None
    assert locator.stats["tipless_pending"] == 0 and locator.stats["no_tip"] == 2
    _, late, lm = rising_pair(locator, t=T0 + 0.80)
    assert_box_close(late, expected_box(locator, lm))
    assert late[4] == pytest.approx(0.70)


def test_tipless_is_refused_after_the_onset_window_closes(locator, arm):
    arm("Standstill")                                   # window (50, 550) ms
    b1, b2, _ = rising_pair(locator, t=T0 + 1.20)
    assert b1 is None and b2 is None
    assert locator.stats["no_tip"] == 2


def test_tipless_refuses_a_column_that_does_not_grow(locator, arm):
    """A jersey number, a sock and a court line are all static -- that is the whole test."""
    arm("Standstill")
    b1, b2, _ = rising_pair(locator, fills=(40.0, 40.0))
    assert b1 is None and b2 is None
    assert locator.stats["tipless_pending"] == 2 and locator.stats["tipless_accept"] == 0


def test_tipless_refuses_a_column_that_is_shrinking(locator, arm):
    arm("Standstill")
    b1, b2, _ = rising_pair(locator, fills=(60.0, 40.0))
    assert b1 is None and b2 is None
    assert locator.stats["tipless_accept"] == 0


def test_tipless_refuses_a_rise_that_moved_to_another_column(locator, arm):
    """Two different white bars growing in turn are not one meter rising."""
    arm("Standstill")
    f1, _ = meter_frame(x=300, fill_pct=40.0, tip="washed")
    assert locator.detect_box(f1, ts=T0 + 0.20) is None
    f2, _ = meter_frame(x=900, fill_pct=46.0, tip="washed")
    assert locator.detect_box(f2, ts=T0 + 0.20 + FRAME_DT) is None
    assert locator.stats["tipless_accept"] == 0


def test_tipless_never_reaches_a_column_gate_9_can_only_abstain_on(locator, arm):
    """Below shape_min_h gate 9 ABSTAINS, and an abstention is not evidence: with the tip gone
    it is the only gate left, so the tipless path may not inherit the low-fill escape."""
    arm("Standstill")
    b1, b2, _ = rising_pair(locator, fills=(8.0, 12.0))
    assert b1 is None and b2 is None
    assert locator.stats["tipless_accept"] == 0
    assert locator.stats["hit"] == 0


def test_tipless_never_outranks_a_confirmed_green_tip(locator, arm):
    """A real meter and a tip-less impostor in one frame: the tip wins, at 0.90, first sight."""
    arm("Standstill")
    f = court(lines=False)
    draw_meter(f, 300, 300, 70.0, tip="washed")          # bigger, but tip-less
    lm = draw_meter(f, 900, 300, 30.0)                   # smaller, real green tip
    box = locator.detect_box(f, ts=T0 + 0.20)
    assert_box_close(box, expected_box(locator, lm))
    assert box[4] == pytest.approx(0.90)


def test_tipless_keeps_tracking_once_the_fill_tops_out(locator, arm):
    """The rise pair buys FIRST sight. Dropping the box the moment the bar stops growing would
    lose it at the fire, so a candidate co-located with the box accepted a moment ago tracks."""
    arm("Standstill")
    _, box, lm = rising_pair(locator)
    assert box is not None
    top, _ = meter_frame(fill_pct=46.0, tip="washed")     # same fill: no longer rising
    held = locator.detect_box(top, ts=T0 + 0.20 + 2 * FRAME_DT)
    assert_box_close(held, expected_box(locator, lm))
    assert locator.stats["tipless_track"] >= 1


def test_tipless_records_one_forensics_entry_per_press(locator, arm):
    ep = arm("Standstill", epoch=21)
    assert locator.tipless["epoch"] == 0                 # nothing opened before a frame is seen
    _, box, _ = rising_pair(locator)
    assert box is not None
    rec = locator.tipless
    assert rec["epoch"] == ep and rec["rise_frames"] == 2
    assert rec["conf"] == pytest.approx(0.70) and rec["logged"] == 0
    assert rec["fill"] == pytest.approx(46.0, abs=2.0)


def test_tipless_knob_off_is_the_shipped_refusal(clean_env, monkeypatch, arm):
    monkeypatch.setenv("ORION_CV_TIPLESS_ARMED", "0")
    loc = mlc.MeterContourLocator()
    assert loc.tipless_armed is False
    arm("Standstill")
    b1, b2, _ = rising_pair(loc)
    assert b1 is None and b2 is None
    assert loc.stats["no_tip"] == 2


def test_tipless_does_not_lock_an_empty_court_while_armed(locator, arm):
    arm("Standstill")
    empty = court()
    for i in range(6):
        assert locator.detect_box(empty, ts=T0 + 0.20 + i * FRAME_DT) is None
    assert locator.stats["hit"] == 0


def test_tipless_does_not_lock_a_menu_screen_while_armed(locator, arm):
    arm("Standstill")
    f = np.full((H, W, 3), MENU_GREY, np.uint8)
    for x in range(200, 1000, 40):
        f[300:360, x:x + 12] = WHITE
    for i in range(4):
        assert locator.detect_box(f, ts=T0 + 0.20 + i * FRAME_DT) is None


def test_tipless_forgets_its_pair_when_the_position_is_forgotten(locator, arm):
    """forget_position() is the ghost eviction: the remembered rise IS the ghost's own column."""
    arm("Standstill")
    f1, _ = meter_frame(fill_pct=40.0, tip="washed")
    assert locator.detect_box(f1, ts=T0 + 0.20) is None
    locator.forget_position()
    f2, _ = meter_frame(fill_pct=46.0, tip="washed")
    assert locator.detect_box(f2, ts=T0 + 0.20 + FRAME_DT) is None
    assert locator.stats["tipless_accept"] == 0


def test_tipless_is_a_no_op_on_a_normal_tipped_meter(locator, arm):
    """The whole shipped population: a meter with its green tip is unaffected by any of this."""
    arm("Standstill")
    for i, fill in enumerate((15.0, 40.0, 70.0, 90.0)):
        f, lm = meter_frame(fill_pct=fill)
        box = locator.detect_box(f, ts=T0 + 0.20 + i * FRAME_DT)
        assert_box_close(box, expected_box(locator, lm))
        assert box[4] == pytest.approx(0.90)
    assert locator.stats["tipless_pending"] == 0 and locator.stats["tipless_accept"] == 0


def test_tipless_refuses_a_candidate_whose_tip_window_was_clamped_away(locator):
    """A window we never actually read is not evidence that the tip is missing.

    Measured on session_20260915_185359 frame 335 with the path armed for every frame: the
    roaming ROI cut a real meter's tip window off, the shipped code answered `no_tip`, the
    caller RE-OPENED THE BAND and found the same meter with its tip at conf 0.90. Minting a
    tipless candidate off the clamped window instead ENDED that search at 0.70 -- and the
    rise pair then refused it, so a real 0.90 proposal was lost outright.
    """
    frame, lm = meter_frame(y=300, fill_pct=40.0, tip="washed")
    clipped = frame[lm["gtop"] + 4:, :]                  # the tip prior now starts above row 0
    assert locator._find(clipped, 1.0, None, None, tipless=True) is None
    assert locator.stats["no_tip"] == 1, "a clamped window must answer no_tip, as it always has"
    whole = frame[lm["gtop"] - 30:, :]                   # the same pixels, window fully inside
    assert locator._find(whole, 1.0, None, None, tipless=True) is not None
    assert locator._find_tipless is True


# ------------------------------------------- 13. SURGICAL forget_position(box=) (2026-09-16)
#
# THE LIVE FAILURE the surgery answers (09-16 14:20:46-14:21:11, epochs 22-27, six consecutive
# blind presses): with the previous shot's meter still on screen through a rapid re-press the
# reader evicted it ~10x/s (`loc_forget` 0->148 in lockstep with `locks`, `drops` flat: 39 locks
# / 42 seeds / 39 forgets in 4 s), and EVERY eviction wiped the locator's two-frame promotion
# pairs -- `_pending`, `_expect`, `_tipless`. A pair needs two CONSECUTIVE frames; at that rate
# the real meter's pair never reached its second frame and first publication landed 100-170 ms
# late (BOX LATCHED 810-912 ms after the press against a 750 ms deadline). So a forget now takes
# the ghost's own memory and nothing else.
def test_forget_with_a_ghost_box_keeps_a_pair_on_another_column(locator, arm):
    """(a) The ghost is evicted mid-rise of the REAL meter; that meter's first sight survives
    and promotes on the very next frame, exactly as if nothing had been forgotten."""
    arm("Standstill")
    f1, _ = meter_frame(fill_pct=40.0, tip="washed")            # real meter at x=600
    assert locator.detect_box(f1, ts=T0 + 0.20) is None          # first sight: remembered
    assert locator.stats["tipless_pending"] == 1

    kept = locator.forget_position(box=(900, 300, 26, 120))      # the ghost, 300 px away
    assert "tipless" in kept
    assert locator._last_box is None                             # the position ALWAYS goes

    f2, lm = meter_frame(fill_pct=46.0, tip="washed")
    assert_box_close(locator.detect_box(f2, ts=T0 + 0.20 + FRAME_DT), expected_box(locator, lm))
    assert locator.stats["tipless_accept"] == 1


def test_forget_with_the_pair_s_own_box_still_wipes_it(locator, arm):
    """(b) When the box IS the remembered column the pair is the ghost's own first sight and
    goes with it -- the original amnesia, which is what closes the re-seed loop."""
    arm("Standstill")
    f1, lm = meter_frame(fill_pct=40.0, tip="washed")
    assert locator.detect_box(f1, ts=T0 + 0.20) is None
    assert locator.stats["tipless_pending"] == 1

    assert locator.forget_position(box=expected_box(locator, lm)) == ()

    f2, _ = meter_frame(fill_pct=46.0, tip="washed")
    assert locator.detect_box(f2, ts=T0 + 0.20 + FRAME_DT) is None
    assert locator.stats["tipless_accept"] == 0


def test_forget_without_a_box_is_the_original_total_amnesia(locator, arm):
    """A caller that cannot name the object it refused stays fail-closed."""
    arm("Standstill")
    f1, _ = meter_frame(fill_pct=40.0, tip="washed")
    locator.detect_box(f1, ts=T0 + 0.20)
    assert locator.forget_position() == ()
    assert locator._tipless is None and locator._last_box is None


def test_forget_never_keeps_a_pair_the_locator_itself_could_have_bridged(locator, arm):
    """The surgery's safety rule: the co-location tolerance is the WIDEST of the locator's own
    pair tolerances (outline 12 / tip-less 14 / expectation 16 px), so a pair near enough for
    any acceptance path to bridge onto the forgotten box is cleared."""
    assert locator._forget_tol_px >= max(locator.outline_recent_px, locator.tipless_tol_px)
    arm("Standstill")
    f1, lm = meter_frame(fill_pct=40.0, tip="washed")
    locator.detect_box(f1, ts=T0 + 0.20)
    bx, by, bw, bh = expected_box(locator, lm)
    near = (bx + int(locator._forget_tol_px) - 1, by, bw, bh)    # inside the tolerance
    assert locator.forget_position(box=near) == ()


def test_pending_pairs_reports_only_live_first_sights(locator, arm):
    """What the reader asks before it evicts: a pair inside its promotion window is LIVE, and
    one whose window has passed is not (so a stale pair can never hold a forget off)."""
    assert locator.pending_pairs(T0) == ()
    arm("Standstill")
    f1, _ = meter_frame(fill_pct=40.0, tip="washed")
    locator.detect_box(f1, ts=T0 + 0.20)
    assert "tipless" in locator.pending_pairs(T0 + 0.20 + FRAME_DT)
    assert locator.pending_pairs(T0 + 0.20 + locator.tipless_pair_s + 0.05) == ()


def test_forget_keeps_an_outline_less_pair_on_another_column(gated_locator):
    """(a) again, on the OTHER pipeline pair: gate 7's outline-less first sight. A ghost
    evicted between the two frames must not cost the real meter its bridge."""
    loc = gated_locator
    frame, lm = meter_frame(fill_pct=30.0, outline=False)
    t0 = 50.0
    assert loc.detect_box(frame, ts=t0) is None
    assert loc.stats["no_outline"] == 1

    assert "pending" in loc.forget_position(box=(900, 300, 26, 120))

    box = loc.detect_box(frame, ts=t0 + FRAME_DT)
    assert_box_close(box, expected_box(loc, lm))
    assert loc.stats["outline_bridged"] == 1


def test_every_pair_field_follows_the_same_co_location_rule(locator):
    """The contract, stated on all four memories at once (the pipeline can only reach two of
    them per configuration): same column -> cleared with the ghost, other column -> kept.
    `_expect` is the RELAXATION TRIO's pair -- how a 12-14 % onset is accepted at all -- and
    `_tipless` the tip-less rise pair; those two are what the 09-16 blind presses kept losing.
    """
    ghost = (600, 300, 26, 120)                     # centre column 613
    far = 900.0
    for name in mlc.MeterContourLocator._PAIR_FIELDS:
        locator._tip_pending = (613.0, 300.0, 50.0)
        locator._pending = (613.0, 300.0, 50.0)
        locator._expect = (613.0, 400.0, 10.0, 50.0)
        locator._tipless = (613.0, 400.0, 10.0, 50.0, 1)
        # move exactly ONE of them off the ghost's column
        mem = list(getattr(locator, name))
        mem[0] = far
        setattr(locator, name, tuple(mem))

        kept = locator.forget_position(box=ghost)
        assert kept == (name.lstrip("_"),), (name, kept)
        assert getattr(locator, name) is not None
        for other in mlc.MeterContourLocator._PAIR_FIELDS:
            if other != name:
                assert getattr(locator, other) is None, other


@pytest.mark.parametrize("with_real_meter", [False, True])
def test_remembered_position_cannot_waive_contradictory_green_shape(locator, with_real_meter):
    # A valid meter previously occupied this position. A straight clothing/HUD
    # strip under a broad green blob must not inherit the old shape verdict.
    warm, _ = meter_frame(x=593, y=292, fill_pct=40.0)
    assert locator.detect_box(warm, ts=1.0) is not None
    previous = (locator._last_box, locator._last_ts)
    false_frame = court(lines=False)
    false_frame[350:400, 600:611] = WHITE
    cv2.rectangle(false_frame, (560, 298), (650, 306), GREEN, -1)
    if with_real_meter:
        lm = draw_meter(false_frame, 800, 300, 40.0)
    for i in range(1, 5):
        result = locator.detect_box(false_frame, ts=1.0 + i * FRAME_DT)
        if with_real_meter:
            assert_box_close(result, expected_box(locator, lm))
        else:
            assert result is None, "a remembered box is not evidence that this green blob is a tip"
            assert (locator._last_box, locator._last_ts) == previous


def test_shape_measurement_failure_is_not_positive_evidence(locator, monkeypatch):
    frame, _ = meter_frame(fill_pct=40.0)
    def broken_measurement(*args, **kwargs):
        raise ValueError("fixture: shape measurement failed")
    monkeypatch.setattr(mlc.np, "median", broken_measurement)
    assert locator.detect_box(frame, ts=1.0) is None
    assert locator.stats["shape_error"] > 0


@pytest.mark.parametrize("fill_pct", [90.0, 95.0, 100.0])
def test_tracked_full_meter_survives_success_particles(locator, fill_pct):
    warm, _ = meter_frame(fill_pct=70.0)
    assert locator.detect_box(warm, ts=1.0) is not None
    frame, lm = meter_frame(fill_pct=fill_pct)
    # Archived green-release frames show a sparkle cloud beside the cap. It
    # fails the compact-tip test, but not the current full white body's shape.
    cv2.rectangle(frame, (lm["x"] - 30, lm["gtop"] - 1),
                  (lm["x"] + 4, lm["gtop"] + 7), GREEN, -1)
    assert_box_close(locator.detect_box(frame, ts=1.0 + FRAME_DT),
                     expected_box(locator, lm))
    assert locator.stats.get("shape_full_body_bridged", 0) > 0


def test_tracked_full_height_clothing_still_needs_current_white_shape(locator):
    warm, _ = meter_frame(x=593, y=242, fill_pct=70.0)
    assert locator.detect_box(warm, ts=1.0) is not None
    previous = (locator._last_box, locator._last_ts)
    frame = court(lines=False)
    jersey_strip(frame, 600, 250, h=100)
    for i in range(1, 5):
        assert locator.detect_box(frame, ts=1.0 + i * FRAME_DT) is None
        assert (locator._last_box, locator._last_ts) == previous


def test_full_body_proof_does_not_take_a_sparkle_as_its_arrow_top(locator):
    warm, _ = meter_frame(fill_pct=70.0)
    assert locator.detect_box(warm, ts=1.0) is not None
    frame, lm = meter_frame(fill_pct=100.0)
    x, gtop = lm["x"], lm["gtop"]
    for row in range(10):
        keep = max(1, round(COL_W * (row + 1) / 10.0))
        cut = (COL_W - keep) // 2
        frame[gtop + row, x + COL_X:x + COL_X + cut] = TRACK
        frame[gtop + row, x + COL_X + cut + keep:x + COL_X + COL_W] = TRACK
    cv2.rectangle(frame, (x - 30, gtop - 7), (x + 11, gtop - 1), GREEN, -1)
    assert locator.detect_box(frame, ts=1.0 + FRAME_DT) is not None
    assert locator.stats.get("shape_full_body_bridged", 0) > 0
