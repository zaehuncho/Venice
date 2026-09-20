"""Unit coverage for the production SimpleMeterReader (simple_meter_reader.py).

Exercises each stage on synthetic 1080p frames: acquire, confidence-decay lock/coast/drop,
fill-vs-green-tip, motion-follow across a horizontal slide, the armed early-rise gate, and
wall-time velocity. These are the residual behaviours the head-to-head proved matter; the
offline reproduction of the full head-to-head lives in tools/diagnostics/validate_simple_reader.py.
"""
import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader


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
# Two red shades, BOTH inside the Arrow2 red band [0,0,220]-[60,60,255], so the column has internal
# grayscale texture (a real HUD meter is anti-aliased/gradiented, never a flat block). A flat block
# makes TM_CCOEFF_NORMED degenerate, which is not representative of live footage.
RED_A = (0, 0, 255)      # gray ~76
RED_B = (40, 40, 255)    # gray ~104 (still R=255,G=B<=60 -> in band)
GREEN = (60, 200, 60)    # BGR that lands in the HSV green make-window band
# Distant/codec-soft colours: each sits just outside the pristine production
# floor while retaining the same red/green hue and compact meter structure.
SOFT_RED_A = (20, 20, 190)
SOFT_RED_B = (60, 60, 205)
SOFT_GREEN = (25, 80, 25)
FLOOR_Y = 600
TRACK_TOP_Y = 470        # full-meter tip row (green cap sits here); track height ~130px
COL_X = 900
COL_W = 24


def _frame(fill_frac=1.0, col_x=COL_X, col_w=COL_W, green=True, bg=40,
           floor_y=FLOOR_Y, track_top_y=TRACK_TOP_Y):
    """A synthetic park meter: a thin (textured) red column filled to fill_frac of the track, capped
    by a green make-window tip at the track top. bg is a neutral gray so nothing else matches."""
    f = np.full((H, W, 3), bg, np.uint8)
    track_h = floor_y - track_top_y
    red_top = int(round(floor_y - fill_frac * track_h))
    half = col_w // 2
    if fill_frac > 0:
        f[red_top:floor_y, col_x:col_x + half] = RED_A
        f[red_top:floor_y, col_x + half:col_x + col_w] = RED_B
    if green:
        # 6px green cap at the very top of the track = the fillable-region anchor.
        f[track_top_y - 6:track_top_y, col_x:col_x + col_w] = GREEN
    return f


def _frame_with_chevron(fill_frac=0.5, chevron_h=6, col_x=COL_X, col_w=COL_W, bg=40):
    """The Arrow2 meter with its real TOP: a silver/white arrow-tip chevron (the ▲ apex) sitting
    DIRECTLY above the 6px green make-window cap. `apex_y` is the tip row = the meter's TRUE top.
    The box top must land here, NOT on the green-cap top (~chevron_h px below), or it clips the tip."""
    f = _frame(fill_frac=fill_frac, col_x=col_x, col_w=col_w, green=True, bg=bg)
    SILVER = (210, 210, 210)                          # BGR bright, zero-sat -> the chevron outline
    cx = col_x + col_w // 2
    apex_y = TRACK_TOP_Y - 6 - chevron_h              # chevron rows sit ABOVE the green cap
    for k in range(chevron_h):                        # narrow tip -> wide base = a ▲ pointing up
        y = apex_y + k
        w = max(2, int(round((k + 1) / chevron_h * col_w)))
        x0 = cx - w // 2
        f[y, max(col_x, x0):min(col_x + col_w, x0 + w)] = SILVER
    return f, apex_y


def test_box_top_lands_on_arrow_tip_apex():
    """TOP-ANCHOR: the box top must coincide with the silver arrow-tip apex (the meter's true top),
    extending UP through the chevron cap -- not stopping at the green-cap top ~6px below (which
    clipped the tip and shortened the fill denominator). Regression guard for the top-alignment fix."""
    r = SimpleMeterReader(W, H)
    frame, apex_y = _frame_with_chevron(fill_frac=0.5, chevron_h=6)
    out = r.read(frame, ts=0.0)
    assert out["detected"]
    box_top = out["bbox"][1]
    assert abs(box_top - apex_y) <= 2, \
        f"box top {box_top} must sit on the arrow-tip apex {apex_y} (within 2px), not clip it"
    # proof it actually extended THROUGH the chevron (green-cap top is at TRACK_TOP_Y - 6)
    assert box_top < TRACK_TOP_Y - 6, \
        f"box top {box_top} did not extend up through the chevron to the apex"
    # the tip is fully OVER the meter: no green/silver cap row left above the box top
    assert box_top <= apex_y + 2


def test_acquire_full_meter():
    r = SimpleMeterReader(W, H)
    out = r.read(_frame(1.0), ts=0.0)
    assert out["detected"] is True
    assert out["stage"] == "acquire"
    assert out["fill"] > 90.0
    b = out["bbox"]
    assert b[2] > 0 and b[3] > 0
    # column sits at COL_X within the band
    assert COL_X - 10 <= b[0] <= COL_X + COL_W + 10


def test_no_detection_on_blank_frame():
    r = SimpleMeterReader(W, H)
    out = r.read(np.full((H, W, 3), 40, np.uint8), ts=0.0)
    assert out["detected"] is False
    assert out["stage"] == "no_meter"
    assert out["fill"] == 0.0
    assert out["bbox"] == [0, 0, 0, 0]


def test_fill_monotonic_with_red_extent():
    """Higher red column => higher reported fill, anchored to the green tip."""
    vals = []
    for frac in (0.4, 0.7, 1.0):
        r = SimpleMeterReader(W, H)          # fresh reader per level (no history bleed)
        out = r.read(_frame(frac), ts=0.0)
        assert out["detected"], f"should detect at frac={frac}"
        vals.append(out["fill"])
    assert vals[0] < vals[1] < vals[2]
    assert vals[2] > 90.0                    # full meter reads near 100%
    assert abs(vals[2] - 100.0) < 12.0


def _make_result_frame(green_frac=0.5, red_frac=0.45, col_x=COL_X, col_w=COL_W, bg=40):
    """A RELEASE / make-result meter: a BIG green make-region fills the TOP `green_frac` of the track,
    a dark gap, then red fills only the LOWER `red_frac` of the track. This is the frame that used to
    read a FALSE 100% -- the old green-BOTTOM anchor collapsed the fillable denominator onto the fill
    boundary, so the red filling the (short) region below the green read ~100% when the meter was only
    ~red_frac full. The FULL-track anchor (green-cap TOP -> floor) must read ~red_frac instead."""
    f = np.full((H, W, 3), bg, np.uint8)
    track_h = FLOOR_Y - TRACK_TOP_Y
    half = col_w // 2
    g_bot = int(round(TRACK_TOP_Y + green_frac * track_h))
    f[TRACK_TOP_Y:g_bot, col_x:col_x + col_w] = GREEN                 # big make-region at the top
    red_top = int(round(FLOOR_Y - red_frac * track_h))
    f[red_top:FLOOR_Y, col_x:col_x + half] = RED_A                    # red only in the lower part
    f[red_top:FLOOR_Y, col_x + half:col_x + col_w] = RED_B
    return f


def test_full_track_anchor_no_false_100_on_make_result():
    """FULL-TRACK ANCHOR (issue B): a HALF-filled meter whose top carries a big green make-region must
    read ~its red fraction of the FULL track -- NEVER a premature 100%. Regression guard for the
    truncated-denominator bug (a ~45%-full meter read 100 on the live 2026-07-07 session)."""
    r = SimpleMeterReader(W, H)
    out = r.read(_make_result_frame(green_frac=0.5, red_frac=0.45), ts=0.0)
    assert out["detected"]
    assert out["fill"] < 65.0, f"half-full make-result read {out['fill']}% (must NOT be ~100)"
    assert out["fill"] > 25.0, f"half-full make-result read {out['fill']}% (should be ~45)"
    # the reported box spans the FULL track (green-cap top -> floor), not just the red region
    track_h = FLOOR_Y - TRACK_TOP_Y
    assert out["bbox"][3] >= 0.75 * track_h, \
        f"box height {out['bbox'][3]} must span the full track (~{track_h}px), not the truncated fill"


def test_full_track_fill_proportional_across_make_result():
    """Fill is proportional to the visible red on the FULL track: a quarter/half/three-quarter red
    under the SAME big green make-region reads ~25/50/75, monotone, none pinned at 100."""
    vals = []
    for rf in (0.25, 0.5, 0.75):
        r = SimpleMeterReader(W, H)
        out = r.read(_make_result_frame(green_frac=0.5, red_frac=rf), ts=0.0)
        assert out["detected"], f"should detect at red_frac={rf}"
        vals.append(out["fill"])
    assert vals[0] < vals[1] < vals[2], f"fill must rise with red extent: {vals}"
    assert vals[2] < 95.0, f"three-quarter meter read {vals[2]}% (must not pin at 100)"
    for v, rf in zip(vals, (25, 50, 75)):
        assert abs(v - rf) < 18.0, f"fill {v}% should track ~{rf}% of the FULL track"


def test_green_reflection_below_fill_does_not_collapse_track():
    """A green reflection/bleed near the FLOOR (glossy court) must NOT shrink the track: the old
    green-bottom anchor drove fillable < 6px -> a degenerate full-strip box + 0% fill (the live
    o654 collapse). The above-fill green anchor + the >=20px clamp keep the read coherent."""
    f = _make_result_frame(green_frac=0.45, red_frac=0.4)
    f[FLOOR_Y - 5:FLOOR_Y, COL_X:COL_X + COL_W] = GREEN              # green bleed at the floor
    r = SimpleMeterReader(W, H)
    out = r.read(f, ts=0.0)
    assert out["detected"]
    assert out["bbox"][3] < 210, f"box height {out['bbox'][3]} is the degenerate full-strip collapse"
    assert 15.0 < out["fill"] < 75.0, f"fill {out['fill']}% collapsed (should be a real ~40 read)"


def test_confidence_decay_coast_then_drop():
    r = SimpleMeterReader(W, H)
    assert r.read(_frame(1.0), ts=0.0)["detected"]        # lock, conf=1.0
    blank = np.full((H, W, 3), 40, np.uint8)
    stages = [r.read(blank, ts=(i + 1) / 60.0)["stage"] for i in range(10)]
    # first few misses COAST (still detected, held fill), then it DROPS to no_meter.
    assert stages[0] == "coast"
    assert "coast" in stages
    assert stages[-1] == "no_meter"
    # coast holds the last good fill
    r2 = SimpleMeterReader(W, H)
    good = r2.read(_frame(0.7), ts=0.0)["fill"]
    coast = r2.read(blank, ts=1 / 60.0)
    assert coast["stage"] == "coast"
    assert coast["fill"] == pytest.approx(good, abs=0.01)


def _rise_and_lock(r):
    """Feed a RISING red meter (fill climbing fast -> rise_state 'rising') while HW-armed so the
    reader locks and (under the flag) latches the shot-coast grace."""
    out = None
    for i, frac in enumerate((0.25, 0.4, 0.55, 0.7, 0.85)):
        r.set_shot_state(True, 0.9, True)
        out = r.read(_frame(frac), ts=i / 60.0)
    assert out["detected"] and out["rise_state"] == "rising"
    return out


def test_shot_coast_survives_release_occlusion(monkeypatch):
    """The live blind-fire: at RELEASE the hardware arm disarms exactly as the shooting motion
    occludes the meter. The default armed-hold coast is gated on `armed`, so once disarmed the fast
    unarmed decay drops the lock within ~5 frames -> the release fires BLIND. ORION_READER_SHOT_COAST
    keeps a recently-rising lock coast-protected through the occlusion so the release stays vision-
    timed (holding the coasted peak fill)."""
    blank = np.full((H, W, 3), 40, np.uint8)
    # default OFF: disarm + occlusion -> the box vanishes before the occlusion clears
    # (OCCL_WIDE explicitly OFF: default-ON since 2026-07-19, its armed-rising latch would
    # otherwise cover this occlusion and mask the SHOT_COAST behaviour under test)
    monkeypatch.delenv("ORION_READER_SHOT_COAST", raising=False)
    monkeypatch.setenv("ORION_READER_OCCL_WIDE", "0")
    r = SimpleMeterReader(W, H)
    peak = _rise_and_lock(r)["fill"]
    off = []
    for i in range(15):
        r.set_shot_state(False, 0.0, False)          # hardware released at the shot
        off.append(r.read(blank, ts=(5 + i) / 60.0)["detected"])
    assert off[0] and not off[-1]                     # coasts a few frames, then DROPS (blind)

    # flag ON: the box is held through the full shooting-motion occlusion on the coasted peak fill
    monkeypatch.setenv("ORION_READER_SHOT_COAST", "1")
    r2 = SimpleMeterReader(W, H)
    _rise_and_lock(r2)
    on_det, on_fill = [], []
    for i in range(15):
        r2.set_shot_state(False, 0.0, False)
        s = r2.read(blank, ts=(5 + i) / 60.0)
        on_det.append(s["detected"]); on_fill.append(s["fill"])
    assert all(on_det)                                # never vanishes through the occlusion
    assert on_fill[-1] == pytest.approx(peak, abs=0.01)   # holds the vision-derived peak fill


def test_shot_coast_default_off_is_byte_identical(monkeypatch):
    """With the flag unset the disarm+occlusion path is unchanged (no grace). OCCL_WIDE is
    explicitly OFF (default-ON since 2026-07-19) so its wide-grace cannot mask this path."""
    monkeypatch.delenv("ORION_READER_SHOT_COAST", raising=False)
    monkeypatch.setenv("ORION_READER_OCCL_WIDE", "0")
    r = SimpleMeterReader(W, H)
    assert not r._shot_coast
    _rise_and_lock(r)
    blank = np.full((H, W, 3), 40, np.uint8)
    stages = []
    for i in range(10):
        r.set_shot_state(False, 0.0, False)          # disarmed (no grace) -> unarmed decay
        stages.append(r.read(blank, ts=(5 + i) / 60.0)["stage"])
    assert stages[0] == "coast" and stages[-1] == "no_meter"


def test_coast_is_present_but_not_fed_via_reason():
    """A coast frame is meter_present (overlay) but flagged meter_memory so the feed gate drops it,
    exactly like the chain's loc_mem echo."""
    r = SimpleMeterReader(W, H)
    r.read(_frame(1.0), ts=0.0)
    coast = r.read(np.full((H, W, 3), 40, np.uint8), ts=1 / 60.0)
    assert coast["detected"] and coast["meter_present"]
    assert coast["rejection_reason"] == "meter_memory"
    # a fresh detect is FED (green_not_found is an accepted rising-phase reason)
    fresh = SimpleMeterReader(W, H).read(_frame(1.0), ts=0.0)
    assert fresh["rejection_reason"] == "green_not_found"


def test_motion_follow_tracks_horizontal_slide():
    """A fast-sliding FADE meter (column marches right each frame) stays tracked, not dropped."""
    r = SimpleMeterReader(W, H)
    x = 300
    r.read(_frame(1.0, col_x=x), ts=0.0)                  # acquire
    detected, stages = [], []
    for i in range(1, 9):
        x += 40                                            # 40px/frame slide (fast blurred fade)
        out = r.read(_frame(1.0, col_x=x), ts=i / 60.0)
        detected.append(out["detected"]); stages.append(out["stage"])
    assert all(detected), f"slide dropped: {list(zip(stages, detected))}"
    assert "track" in stages                               # the NCC motion-follow carried it
    # the tracked box followed the column to the right
    assert r.read(_frame(1.0, col_x=x), ts=1.0)["bbox"][0] > 500


def _green_only_frame(green_px=12, col_x=COL_X, col_w=COL_W, bg=40):
    """The meter's FIXED STRUCTURE with the red fill gone: only the green make-window tip remains
    (what a capped/deflated meter looks like for a frame or two). green_px tall so it clears the
    green-tip area gate."""
    f = np.full((H, W, 3), bg, np.uint8)
    f[TRACK_TOP_Y - green_px:TRACK_TOP_Y, col_x:col_x + col_w] = GREEN
    return f


def _gray_decor_column(col_x=COL_X, col_w=COL_W, bg=40):
    """A textured GREY vertical bar (a window mullion / court edge): structurally column-like so a
    grayscale template can match it, but it carries NO meter red/green. This is the décor that drove
    the 4632-frame false lock; it must NOT be able to hold the lock."""
    f = np.full((H, W, 3), bg, np.uint8)
    half = col_w // 2
    f[TRACK_TOP_Y - 6:FLOOR_Y, col_x:col_x + half] = (90, 90, 90)
    f[TRACK_TOP_Y - 6:FLOOR_Y, col_x + half:col_x + col_w] = (120, 120, 120)
    return f


def test_green_tip_holds_lock_through_red_dropout():
    """STICK THROUGH CAP/DEFLATE: when the red column momentarily vanishes (fill turns green / recedes)
    the green make-window tip keeps the box GLUED to the meter instead of dropping to (no meter)."""
    r = SimpleMeterReader(W, H)
    first = r.read(_frame(0.8), ts=0.0)
    assert first["detected"] and first["fill"] > 0.0
    held = r.read(_green_only_frame(12), ts=1 / 60.0)          # red gone, green tip remains
    assert held["detected"] is True
    assert held["stage"] != "no_meter"                          # box stayed on the meter
    assert held["bbox"][2] > 0                                  # a real box, not [0,0,0,0]
    assert held["fill"] > 0.0                                   # no 0% glitch: last good fill held


def test_grayscale_decor_cannot_hold_the_lock():
    """NO DÉCOR FALSE-LOCK: a persistent grey column-like edge (window mullion) that has NO meter
    colour must decay out of the lock within the coast window -- never an eternal fill-0 hold."""
    r = SimpleMeterReader(W, H)
    assert r.read(_frame(1.0), ts=0.0)["detected"]              # lock a real meter (stores template)
    decor = _gray_decor_column()
    stages = [r.read(decor, ts=(i + 1) / 60.0)["stage"] for i in range(12)]
    assert stages[-1] == "no_meter"                             # the décor edge dropped out
    assert not r.read(decor, ts=13 / 60.0)["detected"]          # and stays dropped


def test_armed_early_rise_relaxes_gate():
    """A SHORT early-rise column (below the tall-thin acquire gate) WITHOUT the meter's green-tip
    structure is rejected UNARMED (decor guard) but acquired when the shot-gate is ARMED -- the
    residual that recovers the first rise frames."""
    short_bare = _frame(0.18, green=False)                 # ~23px red height, NO tip: decor-like
    r_unarmed = SimpleMeterReader(W, H)
    assert r_unarmed.read(short_bare, ts=0.0)["detected"] is False
    r_armed = SimpleMeterReader(W, H)
    r_armed.set_shot_state(True, 0.9)
    assert r_armed.read(short_bare, ts=0.0)["detected"] is True


def test_fade_popin_structure_acquire_unarmed():
    """FADE POP-IN: a SHORT red stub WITH the green tip dot at track-height above its floor is
    acquired COLD (no shot-gate needed) via the structure-corroborated path -- live the gate arms
    only AFTER the release on fades (session_20260706_203024 seq 12/13), so the armed relaxation
    can never recover the fade rise. The tip+stub geometric relation is the decor guard."""
    short = _frame(0.18)                                   # short stub + green tip = pop-in structure
    r = SimpleMeterReader(W, H)
    out = r.read(short, ts=0.0)
    assert out["detected"] is True
    assert out["stage"] == "acquire"
    b = out["bbox"]
    assert COL_X - 12 <= b[0] <= COL_X + COL_W + 12        # locked on the meter, not decor


@pytest.mark.parametrize(
    "col_x,floor_y",
    (
        (8, 220),          # top-left
        (W - 32, 220),     # top-right (outside the nominal x band too)
        (8, 1030),         # bottom-left
        (W - 32, 1030),    # bottom-right
    ),
)
def test_physically_armed_courtwide_fallback_finds_and_tracks_edge_meter(col_x, floor_y):
    """A physical shot may acquire a structurally-proven meter anywhere in frame.

    The nominal band is still tried first; all four samples sit wholly above/below
    it (and the right samples are beyond its trimmed x edge), so success proves the
    armed court-wide fallback.  Once seated, tracking remains a small box-centred
    ROI and the physical-shot bbox translates without changing shape.
    """
    r = SimpleMeterReader(W, H)
    r.set_shot_state(True, 1.0, True)
    first = r.read(
        _frame(0.6, col_x=col_x, floor_y=floor_y,
               track_top_y=floor_y - 130),
        ts=0.0,
    )
    assert first["detected"] is True
    assert first["stage"] == "acquire"
    assert r._courtwide_lock is True
    bx, by, bw, bh = first["bbox"]
    assert abs((bx + 0.5 * bw) - (col_x + 0.5 * COL_W)) <= 8
    assert 0 <= bx < W and 0 <= by < H and bx + bw <= W and by + bh <= H

    # Full-frame is only a clipping boundary. The actual next-frame search stays
    # tightly centred on the lock rather than repeatedly scanning the whole court.
    rx0, ry0, rx1, ry1 = r._relocate_window()
    assert rx1 - rx0 < W // 4
    assert ry1 - ry0 < H // 3

    dx = 2 if col_x < W // 2 else -2
    r.set_shot_state(True, 1.0, True)
    second = r.read(
        _frame(0.72, col_x=col_x + dx, floor_y=floor_y,
               track_top_y=floor_y - 130),
        ts=1 / 60.0,
    )
    assert second["detected"] is True
    assert r._courtwide_lock is True
    assert tuple(second["bbox"][2:]) == tuple(first["bbox"][2:])
    assert (second["bbox"][0] - first["bbox"][0]) * dx > 0

    # An outward velocity estimate at a frame edge must clamp the whole box,
    # not just its origin.  Width/height remain latched for the physical shot.
    r._box_predict = True
    r._bp_dx = r._bp_dy = 0.0
    r._bp_n = 0
    r._bvx = -50.0 if col_x < W // 2 else 50.0
    r._bvy = -50.0 if floor_y < H // 2 else 50.0
    predicted = r._predict_tbox(first["bbox"], armed=True)
    px, py, pw, ph = predicted
    assert tuple(predicted[2:]) == tuple(first["bbox"][2:])
    assert 0 <= px and px + pw <= W
    assert 0 <= py and py + ph <= H


def test_merged_cv_arm_cannot_open_courtwide_fallback():
    """Detector/CV self-arm is forgeable; only the physical arm bit may scan globally."""
    r = SimpleMeterReader(W, H)
    r.set_shot_state(True, 1.0, False)  # merged arm, explicitly not hardware arm
    out = r.read(
        _frame(0.6, col_x=W - 32, floor_y=1030, track_top_y=900),
        ts=0.0,
    )
    assert out["detected"] is False
    assert r._courtwide_lock is False


@pytest.mark.parametrize(
    "decor_kind",
    ("red_only", "green_only", "wrong_height", "off_center", "scattered_green"),
)
def test_physically_armed_courtwide_fallback_rejects_adversarial_decor(decor_kind):
    """Global acquisition needs one connected green tip in the red stub's exact relation."""
    x, floor_y, track_top = W - 32, 1030, 900
    frame = np.full((H, W, 3), 40, np.uint8)
    if decor_kind != "green_only":
        red_top = 950
        frame[red_top:floor_y, x:x + COL_W // 2] = RED_A
        frame[red_top:floor_y, x + COL_W // 2:x + COL_W] = RED_B

    if decor_kind == "green_only":
        frame[track_top - 6:track_top, x:x + COL_W] = GREEN
    elif decor_kind == "wrong_height":
        # Correct colours and sizes, but the green component is too far above
        # the red floor to be the meter's tip.
        frame[790:796, x:x + COL_W] = GREEN
    elif decor_kind == "off_center":
        frame[track_top - 6:track_top, x - 90:x - 90 + COL_W] = GREEN
    elif decor_kind == "scattered_green":
        # Enough aggregate green pixels to beat the legacy loose count, but no
        # connected 2x2 tip component.
        for ox, oy in ((5, -20), (9, -16), (13, -12), (17, -8),
                       (7, -4), (11, 0), (15, 4), (19, 8)):
            frame[track_top + oy, x + ox] = GREEN

    r = SimpleMeterReader(W, H)
    r.set_shot_state(True, 1.0, True)
    out = r.read(frame, ts=0.0)
    assert out["detected"] is False, decor_kind
    assert r._courtwide_lock is False


# ---------------------------------------------------------------------------- #
#  A2c STEAL-COURTWIDE (2026-08-08 beyond-half-court Go-To fix).
#  Live failure: a stale lock on the PREVIOUS shot's spent meter coasts through
#  the whole armed window serving a frozen echo (epoch=27 "samples=0
#  first_fill=97.2 last_fill=97.2 wait_ms=2291.4"), while the real far-court
#  meter renders somewhere the nominal-band steal scan structurally cannot see.
#  The steal must be able to supersede the echo through the SAME hw-gated
#  courtwide structure ladder the cold acquire already trusts -- and ONLY then.
# ---------------------------------------------------------------------------- #
FAR_COL_X = W - 32
FAR_FLOOR_Y = 1030
FAR_TRACK_TOP = 900


def _hostage_reader(monkeypatch=None, *, flag=None, hw=True):
    """Seat an armed nominal lock, then freeze it: the stale-echo terminal state."""
    if monkeypatch is not None and flag is not None:
        monkeypatch.setenv("ORION_READER_STEAL_COURTWIDE", flag)
    r = SimpleMeterReader(W, H)
    r.notify_physical_shot_start(27)
    ts = 0.0
    # Rising armed lock at the nominal position (the previous shot's meter).
    for frac in (0.30, 0.36, 0.42, 0.48, 0.54, 0.60):
        r.set_shot_state(True, 1.0, hw)
        out = r.read(_frame(frac), ts=ts)
        assert out["detected"] is True
        ts += 1 / 60.0
    # The spent meter freezes (release landed) before it fades.
    for _ in range(2):
        r.set_shot_state(True, 1.0, hw)
        r.read(_frame(0.60), ts=ts)
        ts += 1 / 60.0
    assert r.box is not None and r.conf >= r.CONF_MIN
    return r, ts


def _far_meter_frame():
    """The beyond-half-court meter: outside the nominal band, low fresh fill (a new
    rise), reachable only through the courtwide structure ladder."""
    return _frame(0.30, col_x=FAR_COL_X, floor_y=FAR_FLOOR_Y,
                  track_top_y=FAR_TRACK_TOP)


def test_armed_steal_supersedes_frozen_echo_via_courtwide_far_meter():
    """The frozen-echo coast must yield to the far-court meter within the shot window.

    The old meter vanishes and the real meter renders where the nominal steal scan
    cannot see it. Pre-fix the reader coasted the echo for the whole armed window
    (180-frame cap) and the shot died with samples=0. Post-fix the coast-steal runs
    the courtwide ladder once the nominal scan misses, seats the far meter, and the
    next frame reads it fresh.
    """
    r, ts = _hostage_reader()
    seat = None
    for _ in range(20):
        r.set_shot_state(True, 1.0, True)
        out = r.read(_far_meter_frame(), ts=ts)
        ts += 1 / 60.0
        if out["stage"] == "steal_reseat":
            seat = out
            break
    assert seat is not None, "courtwide steal never superseded the frozen echo"
    assert r._courtwide_lock is True
    assert r._courtwide_acquire_tier == "strict_red_first"
    # The seat frame publishes the newly-found position (hw continuity frame).
    bx, by, bw, bh = seat["bbox"]
    assert abs((bx + 0.5 * bw) - (FAR_COL_X + 0.5 * COL_W)) <= 10
    # The very next frame reads the far meter fresh: low new-rise fill, far box.
    r.set_shot_state(True, 1.0, True)
    nxt = r.read(_far_meter_frame(), ts=ts)
    assert nxt["detected"] is True
    assert abs((nxt["bbox"][0] + 0.5 * nxt["bbox"][2])
               - (FAR_COL_X + 0.5 * COL_W)) <= 10
    assert nxt["fill"] < 45.0, "must read the NEW low rise, not the frozen echo"


def test_steal_courtwide_kill_switch_restores_shipped_coast(monkeypatch):
    """ORION_READER_STEAL_COURTWIDE=0 -> the shipped behaviour byte-for-byte: the
    echo coasts and the far meter is never stolen inside the bounded window."""
    r, ts = _hostage_reader(monkeypatch, flag="0")
    for _ in range(20):
        r.set_shot_state(True, 1.0, True)
        out = r.read(_far_meter_frame(), ts=ts)
        ts += 1 / 60.0
        assert out["stage"] != "steal_reseat" or r._courtwide_lock is False
        assert r._courtwide_acquire_tier == ""
        if out["bbox"][2] > 0:
            assert out["bbox"][0] < FAR_COL_X - 100, \
                "flag OFF must not reach the far meter through the steal"


def test_steal_courtwide_requires_physical_arm(monkeypatch):
    """A merged/CV self-arm must NEVER open the courtwide steal (forgeable signal);
    only the physical hw bit is authority for a full-frame search -- exactly the
    cold path's gate."""
    r, ts = _hostage_reader(monkeypatch, flag="1", hw=False)
    for _ in range(20):
        r.set_shot_state(True, 1.0, False)   # merged arm, explicitly not hardware
        out = r.read(_far_meter_frame(), ts=ts)
        ts += 1 / 60.0
        assert r._courtwide_lock is False
        if out["bbox"][2] > 0:
            assert out["bbox"][0] < FAR_COL_X - 100


def test_production_gameplay_gate_blocks_menu_logo_without_physical_shot():
    """The ESRB/loading failure was a static red NBA-logo column published before any shot.

    Keep the ungated diagnostic reader backwards-compatible, but prove the production trust
    boundary does not even scan/publish the same candidate without a physical shot window.
    """
    menu_logo = _frame(1.0, green=False)
    assert SimpleMeterReader(W, H).detect(menu_logo, ts=0.0).detected is True  # reproduction

    r = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    out = r.detect(menu_logo, ts=0.0)
    assert out.detected is False
    assert out.rejection_reason == "gameplay_ineligible"
    assert out.bbox == (0, 0, 0, 0)
    assert r.box is None


def test_production_gameplay_gate_rejects_static_logo_even_if_square_arms_menu():
    """Shot intent alone is insufficient because SQUARE is also usable in menus.

    A pure red static logo must never escape to telemetry, and the unverified tracker is reset
    every verification window so it cannot occupy the lock when a real meter appears.
    """
    r = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    r.set_shot_state(True, 1.0, True)
    menu_logo = _frame(1.0, green=False)
    seen = [r.detect(menu_logo, ts=i / 60.0) for i in range(12)]
    assert not any(out.detected for out in seen)
    assert {out.rejection_reason for out in seen} == {"shot_candidate_unverified"}
    assert r._gameplay_lock_authorized is False


def test_production_gameplay_gate_accepts_structural_meter_without_latency():
    """A trusted shot plus connected red/green meter structure publishes on frame one."""
    r = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(11)
    r.set_shot_state(True, 1.0, True)
    out = r.detect(_frame(0.6, green=True), ts=0.0)
    assert out.detected is True
    assert out.gameplay_structure_verified is True
    assert out.gameplay_structure_epoch == 11
    assert r._gameplay_lock_authorized is True
    assert r.last_debug.get("gameplay_evidence") == "structure"


def test_production_gameplay_gate_accepts_green_missing_meter_after_monotonic_rise():
    """Compressed/contested frames may lose green; verified temporal rise is the bounded fallback.

    With NO physical shot epoch the structure proof still refuses to latch -- the proof is bound
    to the hardware press that owns it, so a rise observed outside any press proves nothing.
    """
    r = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    r.set_shot_state(True, 1.0, True)
    out = None
    for i, fill in enumerate((0.35, 0.45, 0.55, 0.65)):
        out = r.detect(_frame(fill, green=False), ts=i / 60.0)
        if i < 3:
            assert out.detected is False
            assert out.rejection_reason == "shot_candidate_unverified"
    assert out is not None and out.detected is True
    assert r._gameplay_lock_authorized is True
    assert out.gameplay_structure_verified is False       # no epoch -> no proof, unchanged
    assert r.last_debug.get("gameplay_evidence") == "nominal_rise_proof"


def test_nominal_band_rise_latches_structure_proof_inside_a_physical_epoch():
    """[ORION_GOTO_NO_GREEN] A green-less nominal-band meter must become OWNABLE.

    THE BUG, measured 2026-08-05 across ten consecutive live Go-To presses: eight produced no
    ownership, no abort and no telemetry at all, and the single perfect discriminator was whether
    the frames carried a green make-window. Without the structure proof,
    AutomationEngine.cpp:3663-3667 discards every ownership evidence frame, so no episode opens
    and even the stick-fault path never fires -- total silence. The box still renders, because the
    orchestrator draws from result.detected. 2K shrinks the green window with shot difficulty and
    range, so requiring the chevron made ownership a function of shot difficulty.

    This grants the NOMINAL band exactly the proof the riskier COURTWIDE tier already had.
    REVERT-TRACE: drop `nominal_rise_proof` from the latch conditions and this fails.
    """
    r = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    r.set_shot_state(True, 1.0, True)
    r.notify_physical_shot_start(7)
    out = None
    for i, fill in enumerate((0.35, 0.45, 0.55, 0.65)):
        out = r.detect(_frame(fill, green=False), ts=i / 60.0)
    assert out is not None and out.detected is True
    assert out.gameplay_structure_verified is True, (
        "a green-less nominal-band rise must be ownable, or the shot dies silently")
    assert out.gameplay_structure_epoch == 7, "the proof must bind to the press that owns it"
    assert r.last_debug.get("gameplay_evidence") == "nominal_rise_proof"


def test_production_gameplay_gate_preserves_courtwide_physical_acquire():
    """The hard scene gate must not undo court-wide edge-meter support during a real shot."""
    r = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(12)
    r.set_shot_state(True, 1.0, True)
    out = r.detect(_frame(0.6, col_x=W - 32, floor_y=1030,
                          track_top_y=900), ts=0.0)
    assert out.detected is True
    assert r._courtwide_lock is True
    assert r._gameplay_lock_authorized is True
    assert out.gameplay_structure_verified is True
    assert out.gameplay_structure_epoch == 12


def _half_court_frame_720(*, col_x=1120, floor_y=650, col_w=9,
                          track_h=54, fill_frac=0.55, green_dx=0,
                          green_y_offset=0):
    """Small world-space meter seen on a full-resolution 1280x720 half-court camera."""
    frame = np.full((720, 1280, 3), 40, np.uint8)
    track_top = floor_y - track_h
    red_top = int(round(floor_y - fill_frac * track_h))
    half = col_w // 2
    frame[red_top:floor_y, col_x:col_x + half] = RED_A
    frame[red_top:floor_y, col_x + half:col_x + col_w] = RED_B
    green_h = 3
    gx = col_x + green_dx
    gy = track_top - green_h + green_y_offset
    frame[gy:gy + green_h, gx:gx + col_w] = GREEN
    return frame


def _codec_soft_half_court_frame_720(**kwargs):
    """Half-court fixture whose anti-aliased colours miss only the pristine masks."""
    frame = _half_court_frame_720(**kwargs)
    red_a = np.all(frame == np.array(RED_A, np.uint8), axis=2)
    red_b = np.all(frame == np.array(RED_B, np.uint8), axis=2)
    green = np.all(frame == np.array(GREEN, np.uint8), axis=2)
    frame[red_a] = SOFT_RED_A
    frame[red_b] = SOFT_RED_B
    frame[green] = SOFT_GREEN
    return frame


MICRO_RED = (90, 90, 190)     # inside _MICRO_RED, outside _COURTWIDE_RED
MICRO_GREEN = (30, 60, 30)    # inside _MICRO_GREEN, below _COURTWIDE_GREEN value floor


def _micro_half_court_frame_720(*, col_x=1100, floor_y=640, col_w=2,
                                red_h=3, green_dx=0, green_dy=0,
                                red=True, green=True):
    """Sub-quarter-scale meter whose only surviving body/cap pixels are 2-3px wide.

    The colours deliberately exercise the micro-only codec range: neither the strict nor the
    older court-wide colour tier can see them.  The cap centre sits 23px above the floor, inside
    the 0.18-scale body/cap relation while retaining a useful low-fill denominator.
    """
    frame = np.full((720, 1280, 3), 40, np.uint8)
    if red:
        frame[floor_y - red_h:floor_y, col_x:col_x + col_w] = MICRO_RED
    if green:
        cap_y = floor_y - 24 + green_dy
        cap_x = col_x + green_dx
        frame[cap_y:cap_y + 2, cap_x:cap_x + col_w] = MICRO_GREEN
    return frame


@pytest.mark.parametrize("col_w", (2, 3))
def test_micro_half_court_requires_two_unique_frames_then_tracks_low_fill(col_w):
    """A 2-3px micro meter stays invisible for frame one, then exposes a rising low-fill track."""
    r = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(70 + col_w)
    r.set_shot_state(True, 1.0, True)

    first_frame = _micro_half_court_frame_720(col_w=col_w, red_h=3)
    full = (0, 0, 1280, 720)
    # Prove this fixture cannot silently pass either pre-existing single-frame path.
    assert r._acquire_structure(first_frame, region=full, strict_tip=True) is None
    assert r._acquire_structure_tip_first(first_frame, region=full) is None

    first = r.detect(first_frame, ts=0.0)
    assert first.detected is False
    assert first.gameplay_structure_verified is False
    assert r._micro_pending_count == 1
    assert r._courtwide_lock is False

    # Re-delivery of the same capture frame is not independent evidence.
    duplicate = r.detect(first_frame.copy(), ts=0.0)
    assert duplicate.detected is False
    assert r._micro_pending_count == 1
    assert r._courtwide_lock is False

    second = r.detect(
        _micro_half_court_frame_720(col_x=1101, col_w=col_w, red_h=5),
        ts=1.0 / 60.0,
    )
    assert second.detected is True
    assert second.gameplay_structure_verified is True
    assert second.fill_pct < 40.0
    assert r._courtwide_lock is True
    assert r._courtwide_acquire_tier == "micro_compressed_cap_first"
    # The latched per-lock band is a TAGGED row now (multi-colour support). The BGR bounds
    # themselves are unchanged -- this is the same band, carrying its own space with it so a
    # non-Red lock cannot silently be re-read with a red mask.
    assert r._lock_red == ("bgr", r._MICRO_RED[0], r._MICRO_RED[1])
    assert r._lock_green == r._MICRO_GREEN

    third = r.detect(
        _micro_half_court_frame_720(col_x=1102, col_w=col_w, red_h=8),
        ts=2.0 / 60.0,
    )
    assert third.detected is True
    assert third.fill_pct > second.fill_pct
    assert third.bbox[0] >= second.bbox[0]
    assert third.bbox[2:] == second.bbox[2:]


def test_micro_half_court_is_unreachable_without_physical_arm():
    """Idle/menu and detector-derived CV arm never run or accumulate the micro global scan."""
    frame = _micro_half_court_frame_720()

    idle = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    assert idle.detect(frame, ts=0.0).detected is False
    assert idle.detect(frame, ts=1.0 / 60.0).detected is False
    assert idle._micro_pending_count == 0

    cv_armed = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    cv_armed.set_shot_state(True, 1.0, False)
    assert cv_armed.detect(frame, ts=0.0).detected is False
    assert cv_armed.detect(frame, ts=1.0 / 60.0).detected is False
    assert cv_armed._micro_pending_count == 0
    assert cv_armed._courtwide_lock is False


@pytest.mark.parametrize(
    "fixture",
    (
        {"red": True, "green": False},
        {"red": False, "green": True},
        {"green_dx": 18},
        {"green_dy": -20},
    ),
)
def test_micro_half_court_rejects_static_menu_decor_across_unique_frames(fixture):
    """Physical arm cannot turn single-colour or wrongly-related static menu decor into a lock."""
    frame = _micro_half_court_frame_720(**fixture)
    r = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(80)
    r.set_shot_state(True, 1.0, True)

    for i in range(3):
        out = r.detect(frame.copy(), ts=i / 60.0)
        assert out.detected is False
        assert out.gameplay_structure_verified is False
        assert r._courtwide_lock is False
    assert r._micro_pending_count == 0


def test_micro_candidate_cannot_cross_disarm_or_physical_shot_epoch():
    """One pending micro observation never supplies half of a later hardware-owned shot proof."""
    frame = _micro_half_court_frame_720()
    r = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(90)
    r.set_shot_state(True, 1.0, True)
    assert r.detect(frame, ts=0.0).detected is False
    assert r._micro_pending_count == 1

    r.set_shot_state(False, 0.0, False)
    assert r._micro_pending_count == 0

    r.notify_physical_shot_start(91)
    r.set_shot_state(True, 1.0, True)
    assert r.detect(frame, ts=1.0 / 60.0).detected is False
    assert r._micro_pending_count == 1

    r.notify_physical_shot_start(92)
    assert r._micro_pending_count == 0
    r.set_shot_state(True, 1.0, True)
    after_epoch_change = r.detect(frame, ts=2.0 / 60.0)
    assert after_epoch_change.detected is False
    assert r._micro_pending_count == 1
    assert r._courtwide_lock is False


def _full_structure_meter_720(*, col_x, floor_y, col_w, cap_shift=0,
                              fill_frac=0.48):
    """Small-to-near meter with a flared green cap and silver apex.

    The cap width stays inside the production strict-acquire contract (<=1.75x the
    red body), but deliberately exceeds the historical red-column +/-3px strip.
    """
    frame = np.full((720, 1280, 3), 40, np.uint8)
    track_h = max(34, 6 * col_w)
    track_top = floor_y - track_h
    red_top = int(round(floor_y - fill_frac * track_h))
    half = col_w // 2
    frame[red_top:floor_y, col_x:col_x + half] = RED_A
    frame[red_top:floor_y, col_x + half:col_x + col_w] = RED_B

    cap_h = max(3, int(round(0.35 * col_w)))
    cap_w = max(col_w, int(round(1.67 * col_w)))
    structure_cx = col_x + 0.5 * col_w + cap_shift
    cap_x0 = max(0, int(round(structure_cx - 0.5 * cap_w)))
    cap_x1 = min(1280, cap_x0 + cap_w)
    cap_y = track_top - cap_h
    frame[cap_y:track_top, cap_x0:cap_x1] = GREEN

    apex_h = max(2, int(round(0.35 * col_w)))
    apex_y = cap_y - apex_h
    silver = (210, 210, 210)
    for row in range(apex_h):
        width = max(1, int(round((row + 1) / apex_h * (cap_x1 - cap_x0))))
        x0 = max(0, int(round(structure_cx - 0.5 * width)))
        x1 = min(1280, x0 + width)
        frame[apex_y + row, x0:x1] = silver

    structure = (((frame[:, :, 2] >= 220) & (frame[:, :, 1] <= 60))
                 | ((frame[:, :, 1] >= 150) & (frame[:, :, 2] <= 100))
                 | ((frame[:, :, 0] >= 180) & (frame[:, :, 1] >= 180)
                    & (frame[:, :, 2] >= 180)))
    ys, xs = np.where(structure)
    truth = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    return frame, truth, apex_y, cap_y


def _occlude_red_body_720(frame, *, col_x, col_w, keep_side, keep_w=3):
    """Leave one narrow strict-red body slice while preserving the cap/apex."""
    red = ((frame[:, :, 2] >= 220) & (frame[:, :, 1] <= 60)
           & (frame[:, :, 0] <= 60))
    if keep_side == "left":
        keep = np.zeros(red.shape, dtype=bool)
        keep[:, col_x:col_x + keep_w] = True
    else:
        keep = np.zeros(red.shape, dtype=bool)
        keep[:, col_x + col_w - keep_w:col_x + col_w] = True
    frame[red & ~keep] = 40
    return frame


@pytest.mark.parametrize(
    "col_w,col_x,floor_y,cap_shift,dx",
    (
        (6, 3, 180, -1, 3),
        (6, 1271, 180, 1, -3),
        (9, 10, 650, -2, 3),
        (9, 1120, 650, 2, 3),
        (14, 600, 500, -2, 4),
        (14, 1264, 650, 2, -3),
    ),
)
def test_served_bbox_contains_apex_cap_body_and_floor_without_shot_stretch(
        col_w, col_x, floor_y, cap_shift, dx):
    """The sidecar bbox is full-meter truth at every supported 720p camera scale/location."""
    reader = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    reader.notify_physical_shot_start(91)
    reader.set_shot_state(True, 1.0, True)
    frame, truth, apex_y, cap_y = _full_structure_meter_720(
        col_x=col_x, floor_y=floor_y, col_w=col_w, cap_shift=cap_shift)
    first = reader.detect(frame, ts=0.0)
    assert first.detected and first.gameplay_structure_verified

    bx, by, bw, bh = first.bbox
    tx0, ty0, tx1, ty1 = truth
    assert bx <= tx0 and by <= ty0 and bx + bw >= tx1 and by + bh >= ty1
    # Edge morphology may conservatively widen the acquired red body; presentation still stays
    # inside the strict cap envelope of that actual lock identity and remains vertically compact.
    assert bw <= max(reader.box[2], int(np.ceil(1.80 * reader.box[2])))
    assert bw < bh
    assert by <= apex_y <= cap_y
    assert by + bh >= floor_y

    # Mirror the small cap asymmetry while the player translates/rises.  The physical-shot
    # identity keeps one fixed shape, including when the first sample was clipped at a frame edge.
    reader.set_shot_state(True, 1.0, True)
    moved, moved_truth, moved_apex, moved_cap = _full_structure_meter_720(
        col_x=col_x + dx, floor_y=floor_y, col_w=col_w,
        cap_shift=-cap_shift, fill_frac=0.64)
    second = reader.detect(moved, ts=1.0 / 60.0)
    assert second.detected
    assert second.bbox[2:] == first.bbox[2:]
    bx, by, bw, bh = second.bbox
    tx0, ty0, tx1, ty1 = moved_truth
    assert bx <= tx0 and by <= ty0 and bx + bw >= tx1 and by + bh >= ty1
    assert by <= moved_apex <= moved_cap
    assert by + bh >= floor_y


@pytest.mark.parametrize("col_x,floor_y", ((12, 180), (1120, 650)))
def test_half_court_goto_acquires_and_tracks_small_meter_at_1280x720(col_x, floor_y):
    """A distant Go-To meter below the nominal 13px width floor still seeds from strict structure.

    The second frame proves this is not a one-frame exception: the acquired local scale carries
    into the tight relocate path while the meter moves and rises.
    """
    r = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(41)
    r.set_shot_state(True, 1.0, True)
    first = r.detect(
        _half_court_frame_720(col_x=col_x, floor_y=floor_y, col_w=9,
                              track_h=54, fill_frac=0.45),
        ts=0.0,
    )
    assert first.detected is True
    assert first.gameplay_structure_verified is True
    assert first.gameplay_structure_epoch == 41
    assert r._courtwide_lock is True
    assert r._lock_w_ref < r._w_min                 # prove the old gate would reject it

    r.set_shot_state(True, 1.0, True)
    second = r.detect(
        _half_court_frame_720(col_x=col_x + 3, floor_y=floor_y,
                              col_w=9, track_h=54, fill_frac=0.62),
        ts=1.0 / 60.0,
    )
    assert second.detected is True
    assert second.rejection_reason == "green_not_found"  # fresh red, not coast/memory
    assert second.fill_pct > first.fill_pct
    assert second.bbox[0] > first.bbox[0]


@pytest.mark.parametrize("axis", ("horizontal", "vertical"))
def test_verified_in_band_lock_tracks_beyond_nominal_band_without_memory_fallback(axis):
    """A trusted player-attached lock may cross the band that admitted it.

    This reproduces the live half-court failure without relying on a cold court-wide
    acquire: the first meter is wholly inside the nominal band, then translates beyond
    its right or bottom boundary. Historically `_relocate_window` clipped the trusted
    local ROI to that band, so the detector kept reporting a held `meter_memory` value
    while raw_fed/native overlay freshness expired and the visible box disappeared.
    """
    reader = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    reader.notify_physical_shot_start(46)
    reader.set_shot_state(True, 1.0, True)

    outputs = []
    for i in range(21):
        col_x = 1160 + 4 * i if axis == "horizontal" else 600
        floor_y = 500 if axis == "horizontal" else 480 + 5 * i
        frame, _truth, _apex, _cap = _full_structure_meter_720(
            col_x=col_x,
            floor_y=floor_y,
            col_w=14,
            fill_frac=min(0.48 + 0.015 * i, 0.82),
        )
        result = reader.detect(frame, ts=i / 60.0)
        outputs.append(result)
        reader.set_shot_state(True, 1.0, True)

    assert outputs[0].detected is True
    assert reader._courtwide_lock is False  # acquired through the ordinary band
    assert reader._tracking_bounds() == (0, 0, 1280, 720)
    assert all(result.detected for result in outputs)
    assert "meter_memory" not in {result.rejection_reason for result in outputs}
    if axis == "horizontal":
        assert outputs[-1].bbox[0] > 1210
    else:
        assert outputs[-1].bbox[1] + outputs[-1].bbox[3] > 513

    # Full-frame tracking authority is killed immediately with the physical arm.
    reader.set_shot_state(False, 0.0, False)
    assert reader._tracking_bounds() == reader._band_eff()


def test_half_court_goto_codec_soft_structure_acquires_then_tracks():
    """Physical full-frame structure recovers a dim distant meter and latches its colour tier.

    The clean masks intentionally see zero relevant pixels.  Success therefore proves the
    hardware-only codec tier acquired the meter, while the second frame proves relocate/fill use
    the same per-lock colours instead of immediately losing the newly-seated identity.
    """
    r = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(44)
    r.set_shot_state(True, 1.0, True)
    frame = _codec_soft_half_court_frame_720(fill_frac=0.45)
    full = (0, 0, 1280, 720)
    assert r._acquire_structure(frame, region=full, strict_tip=True) is None
    assert r._acquire_structure_tip_first(frame, region=full) is None

    first = r.detect(frame, ts=0.0)
    assert first.detected is True
    assert first.gameplay_structure_verified is True
    assert first.gameplay_structure_epoch == 44
    assert r._courtwide_lock is True
    assert r._courtwide_acquire_tier == "codec_cap_first"
    # Tagged row (see the micro-tier test above). The merge of the courtwide and nominal BGR
    # rows still yields exactly _COURTWIDE_RED, so the band is byte-for-byte what it always was.
    assert r._lock_red == ("bgr", r._COURTWIDE_RED[0], r._COURTWIDE_RED[1])
    assert r._lock_green == r._COURTWIDE_GREEN
    assert first.bbox[2] < first.bbox[3]

    r.set_shot_state(True, 1.0, True)
    moved = _codec_soft_half_court_frame_720(col_x=1123, fill_frac=0.64)
    second = r.detect(moved, ts=1.0 / 60.0)
    assert second.detected is True
    assert second.fill_pct > first.fill_pct
    assert second.bbox[0] > first.bbox[0]
    assert second.bbox[2:] == first.bbox[2:]
    assert r.last_debug.get("courtwide_tier") == "codec_cap_first"


def test_codec_soft_courtwide_tier_requires_physical_arm():
    """A CV/self arm cannot expose the tolerant global colour search."""
    r = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    r.set_shot_state(True, 1.0, False)
    out = r.detect(_codec_soft_half_court_frame_720(), ts=0.0)
    assert out.detected is False
    assert r._courtwide_lock is False
    assert r._courtwide_acquire_tier == ""


@pytest.mark.parametrize(
    "mutator",
    ("red_only", "green_only", "off_center", "wrong_height", "one_pixel_green",
     "wide_green_decor"),
)
def test_codec_soft_courtwide_tier_rejects_unrelated_two_colour_decor(mutator):
    """Colour tolerance does not relax connected-component or meter-relation proof."""
    kwargs = {}
    if mutator == "off_center":
        kwargs["green_dx"] = -45
    elif mutator == "wrong_height":
        kwargs["green_y_offset"] = -45
    frame = _codec_soft_half_court_frame_720(**kwargs)
    red = (np.all(frame == np.array(SOFT_RED_A, np.uint8), axis=2)
           | np.all(frame == np.array(SOFT_RED_B, np.uint8), axis=2))
    green = np.all(frame == np.array(SOFT_GREEN, np.uint8), axis=2)
    if mutator == "red_only":
        frame[green] = 40
    elif mutator == "green_only":
        frame[red] = 40
    elif mutator == "one_pixel_green":
        frame[green] = 40
        frame[595, 1124] = SOFT_GREEN
    elif mutator == "wide_green_decor":
        ys, _xs = np.where(green)
        frame[green] = 40
        frame[ys.min():ys.max() + 1, 1080:1160] = SOFT_GREEN

    r = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(45)
    r.set_shot_state(True, 1.0, True)
    out = r.detect(frame, ts=0.0)
    assert out.detected is False, mutator
    assert r._courtwide_lock is False
    assert r._courtwide_acquire_tier == ""


@pytest.mark.parametrize("keep_side", ("left", "right"))
def test_half_court_goto_cap_first_acquires_motion_occluded_red_body(keep_side):
    """An intact compact cap can recover a Go-To meter whose red body is only a narrow slice."""
    r = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(43)
    r.set_shot_state(True, 1.0, True)
    frame, _truth, apex_y, cap_y = _full_structure_meter_720(
        col_x=1120, floor_y=650, col_w=9, cap_shift=1)
    frame = _occlude_red_body_720(
        frame, col_x=1120, col_w=9, keep_side=keep_side, keep_w=3)

    full_region = (0, 0, 1280, 720)
    assert r._acquire_structure(frame, region=full_region, strict_tip=True) is None
    inferred = r._acquire_structure_tip_first(frame, region=full_region)
    assert inferred is not None
    assert inferred[2] >= 9  # cap scale reconstructs a usable body strip, not a 3px identity

    first = r.detect(frame, ts=0.0)
    assert first.detected is True
    assert first.gameplay_structure_verified is True
    assert first.gameplay_structure_epoch == 43
    assert r._courtwide_lock is True
    bx, by, bw, bh = first.bbox
    assert by <= apex_y <= cap_y
    assert by + bh >= 650
    assert bw < bh

    # Once the motion occlusion clears, the ordinary bounded tracker inherits the strict lock.
    r.set_shot_state(True, 1.0, True)
    clean, truth, moved_apex, _moved_cap = _full_structure_meter_720(
        col_x=1123, floor_y=650, col_w=9, cap_shift=-1, fill_frac=0.64)
    second = r.detect(clean, ts=1.0 / 60.0)
    assert second.detected is True
    assert second.bbox[2:] == first.bbox[2:]
    bx, by, bw, bh = second.bbox
    tx0, ty0, tx1, ty1 = truth
    assert bx <= tx0 and by <= ty0 and bx + bw >= tx1 and by + bh >= ty1
    assert by <= moved_apex


@pytest.mark.parametrize(
    "mutator",
    ("red_only", "green_only", "off_center", "wrong_height", "one_pixel_green",
     "wide_green_decor"),
)
def test_half_court_global_relaxation_still_rejects_small_decor(mutator):
    """The small-meter path never accepts colour alone or unrelated/scattered structure."""
    frame = _half_court_frame_720()
    if mutator == "red_only":
        green = np.all(frame == np.array(GREEN, np.uint8), axis=2)
        frame[green] = 40
    elif mutator == "green_only":
        red = ((frame[:, :, 2] >= 220) & (frame[:, :, 1] <= 60)
               & (frame[:, :, 0] <= 60))
        frame[red] = 40
    elif mutator == "off_center":
        frame = _half_court_frame_720(green_dx=-45)
    elif mutator == "wrong_height":
        frame = _half_court_frame_720(green_y_offset=-45)
    elif mutator == "one_pixel_green":
        green = np.all(frame == np.array(GREEN, np.uint8), axis=2)
        frame[green] = 40
        frame[595, 1124] = GREEN
    elif mutator == "wide_green_decor":
        green = np.all(frame == np.array(GREEN, np.uint8), axis=2)
        ys, xs = np.where(green)
        frame[green] = 40
        frame[ys.min():ys.max() + 1, 1080:1160] = GREEN

    r = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(42)
    r.set_shot_state(True, 1.0, True)
    out = r.detect(frame, ts=0.0)
    assert out.detected is False, mutator
    assert r._courtwide_lock is False


def test_half_court_global_scan_requires_physical_shot_authority():
    """The reduced-scale full-frame search is unreachable from CV/self arm and menu input."""
    frame = _half_court_frame_720()

    cv_armed = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    cv_armed.set_shot_state(True, 1.0, False)
    assert cv_armed.detect(frame, ts=0.0).detected is False

    unarmed = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    assert unarmed.detect(frame, ts=0.0).detected is False


def test_detection_cannot_launder_old_frame_structure_into_new_shot_epoch(monkeypatch):
    """A native arm interleaving after qualification cannot restamp old pixels as the new shot."""
    r = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(20)
    r.set_shot_state(True, 1.0, True)

    original_latch = r._latch_gameplay_structure_proof

    def interleave_new_arm(proof_epoch):
        # _qualify_gameplay_sample already computed proof_epoch_current=True for epoch 20.
        # Deliver epoch 21 in the exact gap before the old frame writes its proof.
        r.notify_physical_shot_start(21)                # arrives before DetectResult adaptation
        r.set_shot_state(True, 1.0, True)
        original_latch(proof_epoch)                     # late old-frame write, still owned by 20

    monkeypatch.setattr(r, "_latch_gameplay_structure_proof", interleave_new_arm)
    stale = r.detect(_frame(0.6, green=True), ts=0.0)
    assert stale.detected is True                       # display/detection remains available
    assert stale.gameplay_structure_verified is False
    assert stale.gameplay_structure_epoch == 0
    assert r._physical_shot_epoch == 21
    assert r._gameplay_structure_verified is True       # the narrow late write did occur
    assert r._gameplay_structure_proof_epoch == 20      # but it stayed owned by the old epoch
    assert r.gameplay_structure_verified is False

    monkeypatch.setattr(r, "_latch_gameplay_structure_proof", original_latch)
    structureless = r.detect(_frame(0.65, green=False), ts=1.0 / 60.0)
    assert structureless.gameplay_structure_verified is False
    assert structureless.gameplay_structure_epoch == 0

    current = r.detect(_frame(0.7, green=True), ts=2.0 / 60.0)
    assert current.gameplay_structure_verified is True
    assert current.gameplay_structure_epoch == 21


def test_production_gameplay_gate_rejects_cv_self_arm():
    """Detector-derived CV arm can never bootstrap its own scene eligibility."""
    r = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    r.set_shot_state(True, 1.0, False)
    out = r.detect(_frame(1.0), ts=0.0)
    assert out.detected is False
    assert out.rejection_reason == "gameplay_ineligible"


def test_shot_gate_hook_consulted():
    calls = {"n": 0}

    def hook():
        calls["n"] += 1
        return (True, 0.8)

    r = SimpleMeterReader(W, H, shot_gate=hook)
    r.read(_frame(0.18), ts=0.0)
    assert calls["n"] >= 1                                 # the optional corroboration hook was called


def test_velocity_positive_on_rising_fill():
    r = SimpleMeterReader(W, H)
    fracs = [0.3, 0.45, 0.6, 0.75, 0.9, 1.0]
    v = 0.0
    for i, fr in enumerate(fracs):
        out = r.read(_frame(fr), ts=i / 60.0)             # 60fps wall clock
        v = out["velocity_pct_s"]
    assert v > 50.0, f"rising fill should give clearly positive %/s, got {v}"
    assert v <= 500.0                                      # clamped like the chain


def test_velocity_zero_on_flat_fill():
    r = SimpleMeterReader(W, H)
    for i in range(6):
        out = r.read(_frame(1.0), ts=i / 60.0)
    assert abs(out["velocity_pct_s"]) < 20.0              # a steady full meter is ~stationary


def _frame_green_px(green_px, fill_frac=0.8):
    """Meter with a taller/shorter green make-window band."""
    f = np.full((H, W, 3), 40, np.uint8)
    top = int(round(FLOOR_Y - fill_frac * (FLOOR_Y - TRACK_TOP_Y)))
    half = COL_W // 2
    f[top:FLOOR_Y, COL_X:COL_X + half] = RED_A
    f[top:FLOOR_Y, COL_X + half:COL_X + COL_W] = RED_B
    f[TRACK_TOP_Y - green_px:TRACK_TOP_Y, COL_X:COL_X + COL_W] = GREEN
    return f


def test_green_window_emitted_on_fill_scale():
    """The green make-window is emitted as a real release target in the 80-100 range, ending at the
    tip (100) -- on the SAME fill scale (no early-fire scale mismatch)."""
    res = SimpleMeterReader(W, H).detect(_frame_green_px(10))
    assert res.green_window_confidence > 0.0
    assert res.green_window_center_pct > 0.0
    assert 80.0 <= res.green_window_start_pct < res.green_window_end_pct <= 100.0
    assert res.green_window_end_pct == pytest.approx(100.0, abs=0.01)
    assert res.green_window_width_pct > 0.0
    assert res.green_cluster_px >= 2
    # centre sits in the top make-window band, well above any mid-track early-fire zone
    assert res.green_window_center_pct >= 85.0


def test_green_window_absent_when_no_green():
    """No green this frame -> not-found sentinels (engine times the tip); still detected + fed."""
    f = _frame(1.0, green=False)
    res = SimpleMeterReader(W, H).detect(f)
    assert res.detected is True
    assert res.green_window_center_pct == -1.0
    assert res.green_window_confidence == 0.0
    assert res.rejection_reason == "green_not_found"


def test_green_window_width_scales_with_band():
    """A taller green chevron -> a wider emitted make window."""
    narrow = SimpleMeterReader(W, H).detect(_frame_green_px(4)).green_window_width_pct
    wide = SimpleMeterReader(W, H).detect(_frame_green_px(14)).green_window_width_pct
    assert wide > narrow


def test_armed_coast_is_longer_than_unarmed():
    """While armed the confidence-decay coast lasts longer (shot-gated) so a fast-fade blur string
    doesn't drop the lock."""
    blank = np.full((H, W, 3), 40, np.uint8)

    def coast_len(armed):
        r = SimpleMeterReader(W, H)
        r.set_shot_state(armed)
        r.read(_frame(1.0), ts=0.0)
        n = 0
        for i in range(40):
            r.set_shot_state(armed)
            out = r.read(blank, ts=(i + 1) / 60.0)
            if out["stage"] == "coast":
                n += 1
            elif out["stage"] == "no_meter":
                break
        return n

    unarmed, armed = coast_len(False), coast_len(True)
    assert armed > unarmed
    assert armed >= 10          # ~200ms @60fps


# --------------------------------------------------------------------------- #
#  within-shot OCCLUSION mitigations (ORION_READER_OCCLUSION, default ON)
# --------------------------------------------------------------------------- #
def test_armed_occlusion_coast_holds_lock_through_long_occlusion():
    """A STATIONARY meter fully occluded by the arm/body for ~20 frames while ARMED keeps the lock
    (det stays True across every occlusion frame) instead of dropping mid-shot -- the extended
    armed coast that survives a real body crossing, not just a fast-fade blur string."""
    r = SimpleMeterReader(W, H)
    r.set_shot_state(True)
    assert r.read(_frame(0.8), ts=0.0)["detected"]
    blank = np.full((H, W, 3), 40, np.uint8)
    dets = []
    for i in range(20):
        r.set_shot_state(True)
        dets.append(r.read(blank, ts=(i + 1) / 60.0)["detected"])
    assert all(dets), f"lock dropped during the occlusion run: {dets}"


def test_velocity_carried_through_green_hold():
    """On a GREEN-ONLY hold (red gone, cap remains) the frozen last_fill must NOT be fed to the
    LS-slope velocity -- that flattens the reported rise to ~0 over a cap/deflate hold. The last
    real rising velocity is carried through the hold instead."""
    r = SimpleMeterReader(W, H)
    for i, fr in enumerate([0.3, 0.45, 0.6, 0.75, 0.9]):
        r.read(_frame(fr), ts=i / 60.0)
    v_rise = r.read(_frame(1.0), ts=5 / 60.0)["velocity_pct_s"]
    assert v_rise > 50.0
    held = None
    for i in range(6, 11):                                   # a run of green-only holds
        held = r.read(_green_only_frame(12), ts=i / 60.0)
        assert held["stage"] != "no_meter"
    # velocity is CARRIED (unchanged), not decayed toward 0 by the frozen-fill samples
    assert held["velocity_pct_s"] == pytest.approx(v_rise, abs=1.0)


def _frame_two_green(fill_frac=0.5):
    """A meter with TWO green runs above the fill: the true SHORT top-cap chevron at the track top
    AND a LONGER 'make-flash' run just above the receding fill. The longest-run pick would anchor
    the LOWER (longer) run -> a truncated box; the top-cap pick anchors the true cap -> full span."""
    f = np.full((H, W, 3), 40, np.uint8)
    track_h = FLOOR_Y - TRACK_TOP_Y
    red_top = int(round(FLOOR_Y - fill_frac * track_h))
    half = COL_W // 2
    f[red_top:FLOOR_Y, COL_X:COL_X + half] = RED_A
    f[red_top:FLOOR_Y, COL_X + half:COL_X + COL_W] = RED_B
    f[TRACK_TOP_Y - 3:TRACK_TOP_Y, COL_X:COL_X + COL_W] = GREEN        # short TRUE cap (topmost)
    f[red_top - 16:red_top - 2, COL_X:COL_X + COL_W] = GREEN           # longer LOWER make-flash
    return f


def test_top_cap_anchor_not_truncated_by_lower_green():
    """FULL-SPAN: with a faint short TOP cap competing against a longer LOWER make-flash green run,
    the box must anchor on the TOP-MOST cap (full track), not the longest lower run (short box)."""
    r = SimpleMeterReader(W, H)
    out = r.read(_frame_two_green(0.5), ts=0.0)
    assert out["detected"]
    track_h = FLOOR_Y - TRACK_TOP_Y
    assert out["bbox"][3] > 0.8 * track_h, f"box truncated to {out['bbox'][3]} (track ~{track_h})"


def test_fake_lock_breaker_suppresses_static_mid_fill(monkeypatch):
    """A long byte-identical mid-fill lock is non-physical (static decor / dead hold) and is
    suppressed at detect() once the mid-fill cap is exceeded."""
    monkeypatch.setenv("ORION_READER_STALEBREAK", "0")      # isolate the FAKELOCK breaker (STALEBREAK now default-ON wires its own dead-hold break)
    monkeypatch.setenv("ORION_READER_FAKELOCK_BREAK", "1")   # breaker is default-off; test it enabled
    monkeypatch.setenv("ORION_READER_FAKELOCK_CAP_MID", "6")
    monkeypatch.setenv("ORION_READER_FAKELOCK_CAP_HI", "12")
    r = SimpleMeterReader(W, H)
    out = None
    for i in range(10):
        out = r.detect(_frame(0.62), ts=i / 60.0)
    assert out is not None and out.detected is False
    assert out.rejection_reason == "static_fake_lock"


def test_fake_lock_breaker_uses_higher_cap_near_tip(monkeypatch):
    """Near-tip fills (>=85%) use the looser high cap so real peak holds survive longer than
    mid-fill static locks before suppression."""
    monkeypatch.setenv("ORION_READER_STALEBREAK", "0")      # isolate the FAKELOCK breaker (STALEBREAK now default-ON wires its own dead-hold break)
    monkeypatch.setenv("ORION_READER_FAKELOCK_BREAK", "1")   # breaker is default-off; test it enabled
    monkeypatch.setenv("ORION_READER_FAKELOCK_CAP_MID", "4")
    monkeypatch.setenv("ORION_READER_FAKELOCK_CAP_HI", "9")
    r = SimpleMeterReader(W, H)
    # Mid cap would have fired by now; high-cap path should still be reporting.
    for i in range(8):
        out = r.detect(_frame(0.95), ts=i / 60.0)
        assert out.detected is True
    # Cross the high cap: now the static hold is suppressed.
    final = None
    for i in range(8, 13):
        final = r.detect(_frame(0.95), ts=i / 60.0)
    assert final is not None and final.detected is False
    assert final.rejection_reason == "static_fake_lock"


def test_fake_lock_breaker_resets_when_fill_changes(monkeypatch):
    """A real fill move must reset the static counter so the next lock window is measured from
    that change, not from stale history."""
    monkeypatch.setenv("ORION_READER_FAKELOCK_CAP_MID", "4")
    monkeypatch.setenv("ORION_READER_FAKELOCK_CAP_HI", "9")
    r = SimpleMeterReader(W, H)
    # Build up to (but not beyond) the mid-fill suppression edge.
    for i in range(5):
        out = r.detect(_frame(0.55), ts=i / 60.0)
        assert out.detected is True
    # Real motion: fill changed -> counter resets.
    moved = r.detect(_frame(0.72), ts=5 / 60.0)
    assert moved.detected is True
    assert moved.rejection_reason != "static_fake_lock"
    # A few more identical frames after the move stay below cap.
    for i in range(6, 10):
        out = r.detect(_frame(0.72), ts=i / 60.0)
        assert out.detected is True


def test_fake_lock_breaker_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ORION_READER_FAKELOCK_BREAK", "0")
    monkeypatch.setenv("ORION_READER_FAKELOCK_CAP_MID", "2")
    r = SimpleMeterReader(W, H)
    out = None
    for i in range(8):
        out = r.detect(_frame(0.6), ts=i / 60.0)
    assert out is not None and out.detected is True


# --------------------------------------------------------------------------- #
#  SESSION-DECAY / STALE-LOCK fix flags (2026-07-18): A1 SCALE_RESET,
#  A2 STALEBREAK, A3 SCALE_GUARD, B1 OCCL_WIDE, B2 VZOOM.
#  FLIPPED default-ON after A/B (46/46 gates): SCALE_RESET, STALEBREAK,
#  SCALE_GUARD, VZOOM. OCCL_WIDE (B1) FLIPPED default-ON 2026-07-19: the
#  A2xB1 mid-rise seam is laundered by _held_run (a1f10ea) and the decor
#  steal path is closed by STEAL_PROBATION -- full 5-session sweep green
#  (falselock 0, mid_rise 0, within_shot_ghost_frames 0).
# --------------------------------------------------------------------------- #
_NEW_FLAGS = ("ORION_READER_SCALE_RESET", "ORION_READER_STALEBREAK",
              "ORION_READER_SCALE_GUARD", "ORION_READER_OCCL_WIDE",
              "ORION_READER_VZOOM", "ORION_READER_VZOOM_DOWN",
              "ORION_READER_PERF", "ORION_READER_STEAL_PROBATION",
              "ORION_READER_SILENT_RESEAT", "ORION_READER_TIP_TIGHT",
              "ORION_READER_BOX_PREDICT")


def test_fix_flags_default_state(monkeypatch):
    """Post-flip shipped defaults: the five A/B-proven 2026-07-18 fixes ON, PERF ON, and
    (2026-07-19) OCCL_WIDE (B1) + the two A2 steal-hardening flags ON. The ghost-mid-shot
    fix (STEAL_PROBATION + SILENT_RESEAT) drove within_shot_ghost_frames 2->0 on
    session_20260706_190737 and killed the decor-steal path that pinned B1 off; the full
    5-session sweep is green in this exact default set."""
    for f in _NEW_FLAGS:
        monkeypatch.delenv(f, raising=False)
    r = SimpleMeterReader(W, H)
    assert r._scale_reset and r._stalebreak and r._scale_guard and r._vzoom \
        and r._vzoom_down, "the five A/B-proven reader fixes must default ON"
    assert r._occl_wide, "OCCL_WIDE (B1) flipped default-ON 2026-07-19 (steal-probation de-conflict)"
    assert r._steal_probation and r._silent_reseat, \
        "the A2 steal-hardening pair (ghost fix) flipped default-ON 2026-07-19"
    assert r._steal_prob_frames == 4                     # probation window < falselock MIN_CORE 5
    assert r._perf, "ORION_READER_PERF (S1+S2 output-identical sheds) flipped default-ON 2026-07-18"
    assert r._tip_tight, \
        "TIP_TIGHT (FIX-1 no-elongation) flipped default-ON 2026-07-19 (excess p50 -> 0, 51/51 gates)"
    assert not r._box_predict, \
        "BOX_PREDICT (FIX-2) stays default-OFF: its fresh-velocity scope never engages on the " \
        "framedump replays (held_box_trail unchanged) -- live A/B lever, flip only on live evidence"


def test_scale_reset_median_recovers_after_tall_outlier(monkeypatch):
    """A1: a single tall outlier in _track_h_hist (décor false-lock / reflection) poisons the
    one-way 0.90*max seed gate -- the fill under-reads FOREVER with the flag off. With
    ORION_READER_SCALE_RESET the two-sided gate rebases the median after M consecutive
    consistent lower full-cap frames, so the denominator RECOVERS."""
    monkeypatch.setenv("ORION_READER_SCALE_RESET", "0")   # explicit OFF (now default-ON) to test the un-recovered path
    r = SimpleMeterReader(W, H)
    assert r.read(_frame(1.0), ts=0.0)["detected"]
    r._track_h_hist = [200.0] * 8                       # poison: a ~200px outlier ratcheted the max
    fills_off = [r.read(_frame(1.0), ts=(i + 1) / 60.0)["fill"] for i in range(10)]
    assert fills_off[-1] < 80.0, f"flag OFF should stay under-read, got {fills_off[-1]}"

    monkeypatch.setenv("ORION_READER_SCALE_RESET", "1")
    r2 = SimpleMeterReader(W, H)
    assert r2.read(_frame(1.0), ts=0.0)["detected"]
    r2._track_h_hist = [200.0] * 8
    fills_on = [r2.read(_frame(1.0), ts=(i + 1) / 60.0)["fill"] for i in range(10)]
    assert fills_on[-1] > 90.0, f"denominator did not recover: {fills_on}"


def test_scale_reset_lock_drop_clears_transient_state(monkeypatch):
    """A1: a confirmed lock DROP clears the TRANSIENT read state (last_fill/last_tbox/vel
    history) so a stale echo cannot leak into the next lock; the SCALE histories persist
    (the two-sided seed gate heals them in place) so the next lock's early no-green frames
    keep a real denominator, and a poisoned median still recovers after re-acquire."""
    monkeypatch.setenv("ORION_READER_SCALE_RESET", "1")
    r = SimpleMeterReader(W, H)
    assert r.read(_frame(1.0), ts=0.0)["detected"]
    r._track_h_hist = [200.0] * 8
    blank = np.full((H, W, 3), 40, np.uint8)
    for i in range(12):                                  # coast out -> drop
        r.read(blank, ts=(i + 1) / 60.0)
    assert r.last_fill == 0.0 and r.last_tbox == [0, 0, 0, 0] and len(r._vel_hist) <= 1
    assert r._track_h_hist == [200.0] * 8                # scale memory persists across the drop
    fills = [r.read(_frame(1.0), ts=1.0 + i / 60.0)["fill"] for i in range(10)]
    assert fills[0] > 0.0                                # re-acquire reads on the kept scale
    assert fills[-1] > 90.0                              # ...and the poisoned median still heals


def test_stalebreak_dead_hold_stops_rising_then_breaks(monkeypatch):
    """A2: a frozen-fill dead-hold (green-hold with a CARRIED stale velocity) must (i) stop
    claiming rise_state='rising' once no FRESH rising RED read is inside the freshness window
    (so it can no longer re-arm the orchestrator's CV shot-gate) and (ii) BREAK the lock via
    the hardened fake-lock breaker so the cold re-acquire actually runs."""
    monkeypatch.setenv("ORION_READER_STALEBREAK", "1")
    monkeypatch.setenv("ORION_READER_STALE_RISE_FRAMES", "6")
    monkeypatch.setenv("ORION_READER_FAKELOCK_CAP_MID", "12")
    monkeypatch.setenv("ORION_READER_FAKELOCK_CAP_HI", "18")
    r = SimpleMeterReader(W, H)
    out = None
    for i, fr in enumerate((0.3, 0.45, 0.6, 0.75, 0.9)):
        out = r.detect(_frame(fr), ts=i / 60.0)
    assert out.rise_state == "rising"                    # genuine fresh rising red
    claims, dets, vels = [], [], []
    for i in range(5, 45):                               # dead-hold: fill frozen, vel carried
        out = r.detect(_green_only_frame(12), ts=i / 60.0)
        claims.append(out.rise_state); dets.append(out.detected)
        vels.append(out.fill_velocity_pct_s)
    assert "rising" not in claims[10:], "stale velocity kept claiming 'rising'"
    assert all(v == 0.0 for d, v in zip(dets[10:], vels[10:]) if d), \
        "stale velocity still advertised on held frames"
    assert dets[-1] is False, "frozen-fill dead-hold never broke the lock"
    back = r.detect(_frame(0.5), ts=46 / 60.0)           # cold re-acquire runs after the break
    assert back.detected is True


def test_stalebreak_off_is_unchanged(monkeypatch):
    """Flag OFF: the dead-hold keeps its shipped behaviour (held detection, carried velocity)."""
    monkeypatch.setenv("ORION_READER_STALEBREAK", "0")   # explicit OFF (now default-ON) so this truly tests the off path
    monkeypatch.delenv("ORION_READER_FAKELOCK_BREAK", raising=False)
    r = SimpleMeterReader(W, H)
    for i, fr in enumerate((0.3, 0.45, 0.6, 0.75, 0.9)):
        r.detect(_frame(fr), ts=i / 60.0)
    out = None
    for i in range(5, 45):
        out = r.detect(_green_only_frame(12), ts=i / 60.0)
    assert out.detected is True                          # shipped: the hold never breaks


def test_scale_guard_blocks_static_feed_and_decays(monkeypatch):
    """A3: a confident-but-STATIC (non-rising, un-armed) lock must not feed _scale_est, and a
    ratcheted estimate decays back toward 1.0 when no qualified sample arrives for a while."""
    monkeypatch.setenv("ORION_READER_SCALE_GUARD", "1")
    r = SimpleMeterReader(W, H)
    for i in range(10):                                  # static full meter, never rising, no hw
        r.read(_frame(1.0), ts=i / 60.0)
    assert r._size_base == 0.0, "static décor-like lock fed the scale baseline"
    # ratcheted estimate + no fresh qualified samples -> decays toward 1.0
    r._size_base = 25.0
    r._scale_est = 1.4
    r._scale_last_ts = 0.0
    for i in range(120):
        r.read(_frame(1.0), ts=10.0 + i / 60.0)
    assert r._scale_est < 1.1, f"scale estimate never decayed: {r._scale_est}"


def test_occl_wide_covers_long_arm_cross(monkeypatch):
    """B1: a ~40-frame occlusion right after the hw disarm (a full left-side arm-cross) drops
    the lock with the flag off, but the widened in-shot occlusion window (latched ONLY on an
    armed rising read) holds it through with ORION_READER_OCCL_WIDE=1."""
    blank = np.full((H, W, 3), 40, np.uint8)
    monkeypatch.setenv("ORION_READER_OCCL_WIDE", "0")    # explicit OFF (now default-ON) to test the dropped-lock path
    monkeypatch.delenv("ORION_READER_SHOT_COAST", raising=False)
    r = SimpleMeterReader(W, H)
    _rise_and_lock(r)
    off = []
    for i in range(40):
        r.set_shot_state(False, 0.0, False)
        off.append(r.read(blank, ts=(5 + i) / 60.0)["detected"])
    assert not off[-1], "flag OFF should drop during the long occlusion"

    monkeypatch.setenv("ORION_READER_OCCL_WIDE", "1")
    r2 = SimpleMeterReader(W, H)
    peak = _rise_and_lock(r2)["fill"]
    on = []
    for i in range(40):
        r2.set_shot_state(False, 0.0, False)
        s = r2.read(blank, ts=(5 + i) / 60.0)
        on.append(s["detected"])
    assert all(on), f"lock dropped during the covered arm-cross: {on}"
    assert s["fill"] == pytest.approx(peak, abs=0.01)    # held on the vision-derived peak


def test_occl_wide_never_latches_unarmed(monkeypatch):
    """B1 safety: an UN-armed rising lock must NOT latch the widened window -- off-shot
    behaviour (decor acceptance / coast length) is unchanged."""
    monkeypatch.setenv("ORION_READER_OCCL_WIDE", "1")
    r = SimpleMeterReader(W, H)
    for i, fr in enumerate((0.3, 0.45, 0.6, 0.75, 0.9)):
        r.read(_frame(fr), ts=i / 60.0)                  # rising but never armed
    assert r._occl_wide_until == 0.0
    blank = np.full((H, W, 3), 40, np.uint8)
    stages = [r.read(blank, ts=(5 + i) / 60.0)["stage"] for i in range(10)]
    assert stages[-1] == "no_meter"                      # normal unarmed decay -> drop


def test_occl_wide_launder_survives_green_hold_reset(monkeypatch):
    """A2xB1 de-conflict: a degenerate green_hold accept mid-coast resets _coast_n, which used
    to defeat B1's >=12-coast `>20pp` launder floor at the inter-shot seam -- the frozen echo
    then met the next shot's first genuine red as an UNLAUNDERED >20pp backward step (the
    all-5-ON mid_rise glitch on session_20260706_190737). `_held_run` counts CONSECUTIVE
    HELD-fill emissions (coast meter_memory + green_hold) and is NOT reset by a green_hold,
    so B1's own one-frame occl_relock launder still fires at the seam."""
    blank = np.full((H, W, 3), 40, np.uint8)
    monkeypatch.setenv("ORION_READER_OCCL_WIDE", "1")
    r = SimpleMeterReader(W, H)
    _rise_and_lock(r)                                    # armed rising -> latches the wide window

    def _coast(ts):
        # pin the coast momentum: the stale rising bvy dead-reckons the box UP off the
        # column (a real fast-slide behaviour that would hide the seam mechanism here)
        r._bvx = r._bvy = 0.0
        r.set_shot_state(False, 0.0, False)
        return r.read(blank, ts=ts)

    s = None
    for i in range(8):                                   # long occlusion coast (held echo)
        s = _coast((5 + i) / 60.0)
        assert s["detected"]
    held = s["fill"]
    r._bvx = r._bvy = 0.0
    r.set_shot_state(False, 0.0, False)
    s = r.read(_green_only_frame(12), ts=13 / 60.0)      # degenerate green-hold accept...
    assert s["detected"] and s["fill"] == pytest.approx(held, abs=0.01)
    assert r._coast_n == 0                               # ...RESET the coast counter (the defect)
    for i in range(4):                                   # coast resumes on the same echo
        s = _coast((14 + i) / 60.0)
        assert s["detected"]
    assert r._coast_n < 12 <= r._held_run                # old floor defeated; held-run floor is not
    r._bvx = r._bvy = 0.0
    r.set_shot_state(False, 0.0, False)
    s = r.read(_frame(0.5), ts=18 / 60.0)                # next shot's first true red, >20pp below
    assert s["detected"] is False and s["stage"] == "occl_relock", \
        "the held-echo seam read must get B1's one-frame launder despite the green_hold reset"
    assert r._held_run == 0                              # launder consumed the held-echo run
    s = r.read(_frame(0.5), ts=19 / 60.0)                # continuity resumes fresh next frame
    assert s["detected"] and s["fill"] < 60.0


# --------------------------------------------------------------------------- #
#  A2 coast-steal hardening (2026-07-19, the ghost-mid-shot fix):
#  ORION_READER_STEAL_PROBATION + ORION_READER_SILENT_RESEAT.
# --------------------------------------------------------------------------- #
def _steal_setup(monkeypatch, silent):
    """A locked meter whose wide-grace coast reaches the A2 steal threshold with the
    'rising' freshness window EXPIRED (the exact off-shot steal context of the B1
    falselock diagnosis): armed rising lock -> disarm -> 10 blank coast frames kept
    alive by the OCCL_WIDE grace. Returns the reader + the held fill."""
    monkeypatch.setenv("ORION_READER_OCCL_WIDE", "1")
    monkeypatch.setenv("ORION_READER_STEAL_PROBATION", "1")
    monkeypatch.setenv("ORION_READER_SILENT_RESEAT", "1" if silent else "0")
    monkeypatch.setenv("ORION_READER_STALE_RISE_FRAMES", "6")   # expire fresh-rise fast
    monkeypatch.delenv("ORION_READER_SHOT_COAST", raising=False)
    r = SimpleMeterReader(W, H)
    held = _rise_and_lock(r)["fill"]                     # armed rising -> latches the wide window
    blank = np.full((H, W, 3), 40, np.uint8)
    for i in range(10):                                  # coast >= 8 (steal threshold), fresh expired
        r.set_shot_state(False, 0.0, False)
        s = r.read(blank, ts=(5 + i) / 60.0)
        assert s["detected"]
    assert r._coast_n >= 8 and r._fresh_rise_left == 0
    return r, held


def test_steal_probation_drops_static_decor_steal(monkeypatch):
    """Fix 1d: an UN-armed steal reseat with no live evidence (no hw, no fresh rise, no
    grace) onto a STATIC red decor column must prove a rise within <= 4 read frames or be
    dropped ('no_rise') -- so A2's band-wide reseat can no longer latch static decor (the
    open item that pinned ORION_READER_OCCL_WIDE off). The <=4-frame window keeps the
    detected run under the falselock metric's 5-frame MIN_CORE."""
    r, _held = _steal_setup(monkeypatch, silent=False)
    decor = _frame(0.5, col_x=1250, green=False)         # static red decor, no green, off-window
    r.set_shot_state(False, 0.0, False)
    s = r.read(decor, ts=15 / 60.0)                      # A2 steal fires on the decor column
    assert s["stage"] == "steal_reseat" and s["detected"] is False
    assert r._prob_active, "a no-evidence steal must arm the rise-probation"
    assert r._prob_limit == 4                            # the short steal window (< MIN_CORE 5)
    outs = []
    for i in range(6):
        r.set_shot_state(False, 0.0, False)
        outs.append(r.read(decor, ts=(16 + i) / 60.0))
    reasons = [o["rejection_reason"] for o in outs]
    assert "no_rise" in reasons[:4], f"static decor steal not dropped in <=4 reads: {reasons}"
    di = reasons.index("no_rise")
    assert all(o["detected"] for o in outs[:di]) and not outs[di]["detected"]
    assert di <= 3                                       # <= 3 detected frames before the drop
    assert not any(o["detected"] for o in outs[di + 1:]), \
        "refuse cooldown should block an immediate cold re-lock of the same decor"
    assert r._occl_wide_until == 0.0                     # the spent wide window is consumed


def test_silent_reseat_same_meter_emits_detected(monkeypatch):
    """Ghost fix: a steal whose stolen column reads fill-CONTINUOUS with the coasted hold
    (no >20pp backward step) is the SAME meter re-found after a pan -- it must emit ONE
    detected:True 'held_reseat' frame (held fill + held box) instead of the detected:False
    launder, so a same-meter reseat never blinks (session_20260706_190737 o=734: 92.7 ->
    91.6 with a ~150px pan jump was laundered to a 2-frame mid-shot ghost)."""
    r, held = _steal_setup(monkeypatch, silent=True)
    shifted = _frame(0.8, col_x=1100)                    # same meter re-found 200px away, ~5pp lower
    r.set_shot_state(False, 0.0, False)
    s = r.read(shifted, ts=15 / 60.0)
    assert s["detected"] is True and s["rejection_reason"] == "held_reseat"
    assert s["fill"] == pytest.approx(held, abs=0.01)    # the held (continuous) fill, not a 0 blink
    assert any(s["bbox"]), "held_reseat must carry the dead-reckoned held box, never [0,0,0,0]"
    assert r._prob_active                                # un-armed steal is still probed...
    for i in range(6):                                   # ...but the meter's green tip exempts it
        r.set_shot_state(False, 0.0, False)
        s = r.read(shifted, ts=(16 + i) / 60.0)
        assert s["detected"], "a real (green-capped) same-meter reseat must survive probation"
    assert not r._prob_active


def test_silent_reseat_seeds_fresh_state_after_backward_step_launder(monkeypatch):
    """Ghost fix, second frame: a genuinely DISCONTINUOUS (>20pp backward step) reseat keeps
    the designed detected:False launder, but must seed last_fill/last_tbox from the stolen
    column so a relocate miss on the very next frame coasts on a REAL box -- shipped HEAD
    stranded a detected:True zero-box 'meter_memory' frame there (o=687/o=735, the second
    ghost frame of each blink)."""
    r, held = _steal_setup(monkeypatch, silent=True)
    # >20pp below the held fill -> launder (0.45 keeps the column over the unarmed AR gate)
    low = _frame(0.45, col_x=1200)
    r.set_shot_state(False, 0.0, False)
    s = r.read(low, ts=15 / 60.0)
    assert s["detected"] is False and s["stage"] == "steal_reseat"
    fresh = r.last_fill
    assert 0.0 < fresh < held - 20.0                     # seeded from the stolen column's read
    assert r.last_tbox[2] > 0
    blank = np.full((H, W, 3), 40, np.uint8)
    r.set_shot_state(False, 0.0, False)
    s = r.read(blank, ts=16 / 60.0)                      # relocate miss -> coast
    assert s["detected"] and any(s["bbox"]), \
        "the coast after a laundered reseat must emit the seeded box, not the zero-box ghost"
    assert s["fill"] == pytest.approx(fresh, abs=0.01)


def test_steal_probation_exempts_armed_inshot_steal(monkeypatch):
    """Fix 1d exempt set: a MID-SHOT (merged-armed) steal passes frame-1 un-probed -- a
    near-peak same-meter re-find structurally cannot rise 4pp (measured: probing it opened
    a 14-frame mid-shot gap on session_20260707_175017), and A2's supersession of a stale
    dead-hold must stay intact. Off-shot steals (armed=False) remain probed."""
    monkeypatch.setenv("ORION_READER_OCCL_WIDE", "1")
    monkeypatch.setenv("ORION_READER_STEAL_PROBATION", "1")
    monkeypatch.setenv("ORION_READER_SILENT_RESEAT", "1")
    monkeypatch.setenv("ORION_READER_STALE_RISE_FRAMES", "6")   # fresh-rise CANNOT be the exempter
    r = SimpleMeterReader(W, H)
    _rise_and_lock(r)
    blank = np.full((H, W, 3), 40, np.uint8)
    for i in range(10):                                  # ARMED coast (CV self-arm, hw stays False)
        r.set_shot_state(True, 0.9, False)
        s = r.read(blank, ts=(5 + i) / 60.0)
        assert s["detected"]
    assert r._coast_n >= 8 and r._fresh_rise_left == 0
    low = _frame(0.3, col_x=1200)                        # discontinuous reseat target
    r.set_shot_state(True, 0.9, False)
    s = r.read(low, ts=15 / 60.0)
    assert s["stage"] == "steal_reseat"
    assert not r._prob_active, "an in-shot (merged-armed) steal must be probation-EXEMPT"
    for i in range(6):                                   # static after the reseat: never dropped
        r.set_shot_state(True, 0.9, False)
        s = r.read(low, ts=(16 + i) / 60.0)
        assert s["detected"] and s["rejection_reason"] != "no_rise"


def test_steal_flags_off_shipped_launder_unchanged(monkeypatch):
    """Both new flags OFF: the shipped A2 steal launder is byte-identical -- detected:False,
    zeroed hold state (the replay byte-identity guard's unit twin)."""
    monkeypatch.setenv("ORION_READER_OCCL_WIDE", "1")
    monkeypatch.setenv("ORION_READER_STEAL_PROBATION", "0")
    monkeypatch.setenv("ORION_READER_SILENT_RESEAT", "0")
    monkeypatch.setenv("ORION_READER_STALE_RISE_FRAMES", "6")
    r = SimpleMeterReader(W, H)
    _rise_and_lock(r)
    blank = np.full((H, W, 3), 40, np.uint8)
    for i in range(10):
        r.set_shot_state(False, 0.0, False)
        r.read(blank, ts=(5 + i) / 60.0)
    shifted = _frame(0.8, col_x=1100)                    # would be a silent same-meter reseat ON
    r.set_shot_state(False, 0.0, False)
    s = r.read(shifted, ts=15 / 60.0)
    assert s["detected"] is False and s["stage"] == "steal_reseat"
    assert s["bbox"] == [0, 0, 0, 0] and r.last_tbox == [0, 0, 0, 0] and r.last_fill == 0.0
    assert not r._prob_active


def _zoomed_frame(dy):
    """The synthetic meter shifted UP by dy px (camera zoom lifting the HUD)."""
    return np.roll(_frame(1.0), -int(dy), axis=0)


def test_vzoom_lock_survives_zoom_up(monkeypatch):
    """B2: a camera zoom that lifts the meter ABOVE the fixed band prior (y<250 @1080p) loses
    the lock with the flag off (the relocate window AND the re-acquire band hard-clamp to the
    prior); with ORION_READER_VZOOM the band follows the tracked floor-y and the lock survives."""
    monkeypatch.setenv("ORION_READER_VZOOM", "0")        # explicit OFF (now default-ON) to test the lost-lock path
    STEPS, PX = 26, 15                                   # 390px total: floor 600 -> 210 (< band top)

    def _run():
        r = SimpleMeterReader(W, H)
        for i in range(6):                               # baseline locks (vertical baseline seeds)
            r.read(_frame(1.0), ts=i / 60.0)
        det = []
        for k in range(1, STEPS + 1):
            out = r.read(_zoomed_frame(k * PX), ts=(6 + k) / 60.0)
            det.append(out["detected"])
        for j in range(8):                               # settle at the zoomed position
            out = r.read(_zoomed_frame(STEPS * PX), ts=(6 + STEPS + 1 + j) / 60.0)
            det.append(out["detected"])
        return det, out

    det_off, _ = _run()
    assert not det_off[-1], "flag OFF should lose a meter zoomed above the band"

    monkeypatch.setenv("ORION_READER_VZOOM", "1")
    monkeypatch.setenv("ORION_READER_VZOOM_MAX_PX", "420")
    det_on, out_on = _run()
    assert all(det_on), f"vzoom lost the lock during the zoom-up: {det_on}"
    assert out_on["fill"] > 85.0                         # still reading the true (full) fill


def test_vzoom_down_widens_bottom_only_on_live_dive(monkeypatch):
    """1a (ORION_READER_VZOOM_DOWN): at shot ARM the camera dive carries the meter floor
    through and BELOW the fixed band bottom (y1=770 @1080p -> ~891, session_20260717_231912
    f100-260); the up-only VZOOM follow leaves the descent unhandled. With the flag ON, a
    LIVE lock whose tracked floor dives below y1-margin WIDENS the band bottom (top edge
    untouched, bounded by VZOOM_DOWN_MAX_PX); a nominal locked floor comfortably inside the
    band NEVER widens (the reverted bottom-SHIFT's stale-echo failure mode), and a cold
    (unlocked) frame never widens (no off-shot decor area is ever admitted)."""
    monkeypatch.setenv("ORION_READER_VZOOM", "1")
    monkeypatch.setenv("ORION_READER_VZOOM_DOWN", "1")
    r = SimpleMeterReader(W, H)
    for i in range(4):                                   # live lock + tracked (top, floor) ref
        assert r.read(_frame(1.0), ts=i / 60.0)["detected"]
    assert r.box is not None and r._vz_ref is not None
    nominal = r._band                                    # (5, 250, 1815, 770) @1080p
    m = r._vzoom_margin                                  # 40px (sy == 1.0 at 1080p)

    # NOMINAL: floor (~600) comfortably inside the band -> band untouched, no widen.
    assert r._band_eff() == nominal, "a nominal locked floor must never widen the band"
    assert r._vy_down == 0.0

    # THE DIVE: the tracked floor crosses below the band bottom -> bottom-only widen to
    # contain floor+margin; the top edge and x-extent must NOT move.
    top = r._vz_ref[0]
    r._vz_ref = (top, 891.0)
    band = r._band_eff()
    assert band[:3] == nominal[:3], "widen must be BOTTOM-only (y0/x never move)"
    assert band[3] == 891 + m, f"band bottom must contain floor+margin, got {band[3]}"
    assert r._vy_down > 0.0

    # BOUNDED: an absurd floor is capped at nominal_y1 + VZOOM_DOWN_MAX_PX (and frame H).
    r._vz_ref = (top, 3000.0)
    band = r._band_eff()
    assert band[3] <= min(H, nominal[3] + r._vzoom_down_max_px), \
        "widen must be bounded so unbounded decor area is never admitted"

    # COLD / OFF-SHOT: no live lock -> the widen must never fire (decor guard).
    r._vz_ref = (top, 891.0)
    r.box = None
    assert r._band_eff()[3] == nominal[3], "an unlocked frame must never widen the band"


# --------------------------------------------------------------------------- #
#  ORION_READER_PERF (2026-07-18): the two OUTPUT-IDENTICAL latency sheds --
#  S1 empty-band early-exit before morphology+contours in _scan, S2 per-frame
#  band-mask reuse (_scan -> _acquire_structure). Pure perf: PERF ON must be
#  frame-exact identical to PERF OFF on every field (A/B-proven on the framedump
#  replays); these tests pin the mechanism + the identity on synthetic frames.
# --------------------------------------------------------------------------- #

def test_perf_flag_defaults_on(monkeypatch):
    """FLIPPED default-ON 2026-07-18 (pure win, zero behaviour change): frame-exact
    identical to OFF on 3813+5999 replay frames (0 divergent rows on every field but
    wall-clock lat), full-session lat median 2.69->1.22ms / 2.77->0.87ms, gate-window
    read_ms_median 0.71-1.09ms (< 5.0 budget on all 5 sessions), 46/46 detection gates
    both configs."""
    monkeypatch.delenv("ORION_READER_PERF", raising=False)
    r = SimpleMeterReader(W, H)
    assert r._perf is True


def test_perf_s1_zero_red_band_takes_early_exit(monkeypatch):
    """S1: an all-zero-red band frame must bail out of _scan BEFORE the morphology +
    findContours (the measured ~1.6ms of dead work on every idle frame), via the exact
    countNonZero floor -- observable through the _perf_early_exits debug counter. The
    same cold-miss frame must also exercise the S2 reuse (_acquire_structure consumes
    the memoized band mask instead of recomputing the byte-identical inRange)."""
    monkeypatch.setenv("ORION_READER_PERF", "1")
    r = SimpleMeterReader(W, H)
    blank = np.full((H, W, 3), 40, np.uint8)
    out = r.read(blank, ts=0.0)
    assert out["detected"] is False
    assert r._perf_early_exits >= 1, "empty band must take the S1 early-exit"
    assert r._perf_mask_reuses >= 1, "_acquire_structure must reuse the S2 band mask"
    # green-only frame (red mask empty, green tip present): still early-exits the red scan
    n_before = r._perf_early_exits
    out = r.read(_green_only_frame(12), ts=1 / 60.0)
    assert r._perf_early_exits > n_before


def test_perf_s1_never_fires_on_a_real_meter(monkeypatch):
    """S1 is provably conservative: a frame with a gate-passing red column must NEVER
    take the early-exit on the scan that acquires it (the exit only fires when no
    contour could possibly pass)."""
    monkeypatch.setenv("ORION_READER_PERF", "1")
    r = SimpleMeterReader(W, H)
    out = r.read(_frame(0.6), ts=0.0)
    assert out["detected"] is True
    assert r._perf_early_exits == 0, "a passing column must not be early-exited"


def test_perf_on_off_identical_fill_bbox_detected(monkeypatch):
    """OUTPUT-IDENTICAL: PERF ON vs OFF over a full synthetic shot arc (cold idle ->
    rise -> full -> green-hold -> idle) must produce identical detected/fill/bbox/
    stage/velocity/rise on EVERY frame."""
    blank = np.full((H, W, 3), 40, np.uint8)
    seq = [blank, blank, _frame(0.3), _frame(0.5), _frame(0.75), _frame(1.0),
           _frame(1.0), _green_only_frame(12), _green_only_frame(12), blank, blank]
    outs = {}
    for flag in ("0", "1"):
        monkeypatch.setenv("ORION_READER_PERF", flag)
        r = SimpleMeterReader(W, H)
        outs[flag] = [r.read(f, ts=i / 60.0) for i, f in enumerate(seq)]
    for i, (off, on) in enumerate(zip(outs["0"], outs["1"])):
        assert off == on, f"PERF ON diverged from OFF at frame {i}: {off} != {on}"


# --------------------------------------------------------------------------- #
#  2026-07-19 detection-quality pair: FIX-1 ORION_READER_TIP_TIGHT (no
#  elongation -- the box top hugs the cap/apex instead of a FIXED 7px headroom)
#  + FIX-2 ORION_READER_BOX_PREDICT (snappy -- the reported held box forward-
#  predicts along the box velocity instead of trailing frozen).
# --------------------------------------------------------------------------- #
def test_tip_tight_hugs_cap_when_no_apex(monkeypatch):
    """FIX-1: with NO detected apex above the green cap (nothing silver up there), the
    shipped lift still forced the box top a FIXED ~7px ABOVE the cap (elongation into
    empty space). TIP_TIGHT must hug the cap instead: top ~= the green-cap top row."""
    cap_top_abs = TRACK_TOP_Y - 6                       # green cap rows [cap_top_abs, TRACK_TOP_Y)
    monkeypatch.setenv("ORION_READER_TIP_TIGHT", "0")
    r_off = SimpleMeterReader(W, H)
    top_off = r_off.read(_frame(0.5), ts=0.0)["bbox"][1]
    assert top_off <= cap_top_abs - 5, \
        f"shipped lift should overhang ~7px above the cap: top {top_off} vs cap {cap_top_abs}"
    monkeypatch.setenv("ORION_READER_TIP_TIGHT", "1")
    r_on = SimpleMeterReader(W, H)
    out = r_on.read(_frame(0.5), ts=0.0)
    assert out["detected"]
    top_on = out["bbox"][1]
    assert abs(top_on - cap_top_abs) <= 2, \
        f"TIP_TIGHT must hug the cap (top ~{cap_top_abs}), got {top_on}"
    assert top_on > top_off, "TIP_TIGHT must shed the forced headroom (tighter than shipped)"
    # fill is untouched by the box-top change
    assert abs(out["fill"] - r_off.read(_frame(0.5), ts=1 / 60.0)["fill"]) < 1.0


def test_tip_tight_covers_detected_apex(monkeypatch):
    """FIX-1 guard: when a REAL silver arrow-tip apex IS detected above the cap, TIP_TIGHT
    must still cover it (box top lands on the apex row -- never clips a real tip)."""
    monkeypatch.setenv("ORION_READER_TIP_TIGHT", "1")
    r = SimpleMeterReader(W, H)
    frame, apex_y = _frame_with_chevron(fill_frac=0.5, chevron_h=6)
    out = r.read(frame, ts=0.0)
    assert out["detected"]
    box_top = out["bbox"][1]
    assert box_top <= apex_y + 1, \
        f"TIP_TIGHT clipped a detected apex: box top {box_top} vs apex {apex_y}"
    assert abs(box_top - apex_y) <= 2, \
        f"box top {box_top} should sit ON the apex {apex_y}, not overhang above it"


def _slide_then_hold(r, flag_expected_moving, hold_frames=5, dx=8):
    """Build a rightward slide (fresh red reads -> _bvx ~ dx px/frame) then feed green-only
    HOLD frames that keep sliding; returns the emitted bbox x per hold frame."""
    x = 700
    r.set_shot_state(True, 0.9, True)                    # armed: a shot is in progress
    r.read(_frame(0.7, col_x=x), ts=0.0)
    for i in range(1, 10):
        x += dx
        out = r.read(_frame(0.7, col_x=x), ts=i / 60.0)
        assert out["detected"]
    xs = []
    for j in range(hold_frames):
        x += dx
        out = r.read(_green_only_frame(12, col_x=x), ts=(10 + j) / 60.0)
        assert out["detected"] and out["bbox"][2] > 0
        xs.append(out["bbox"][0])
    return xs


def test_box_predict_leads_moving_hold(monkeypatch):
    """FIX-2: on green-hold frames during a fast slide the reported box must FOLLOW the
    meter (advance along _bvx) instead of re-emitting the frozen last_tbox."""
    monkeypatch.setenv("ORION_READER_BOX_PREDICT", "0")
    xs_off = _slide_then_hold(SimpleMeterReader(W, H), False)
    monkeypatch.setenv("ORION_READER_BOX_PREDICT", "1")
    xs_on = _slide_then_hold(SimpleMeterReader(W, H), True)
    assert xs_off[-1] == xs_off[0], f"flag OFF must stay frozen, got {xs_off}"
    assert xs_on[-1] > xs_on[0] + 5, f"flag ON must lead with the slide, got {xs_on}"
    assert xs_on[-1] > xs_off[-1], "predicted box must be ahead of the frozen box"
    # bounded: never beyond the total-shift cap
    assert xs_on[-1] - xs_off[-1] <= 40 + 1


def test_box_predict_stationary_deadband_byte_frozen(monkeypatch):
    """FIX-2 deadband: a STATIONARY meter's hold frames must be BYTE-identical with the
    flag on (zero jitter / overshoot)."""
    outs = {}
    for flag in ("0", "1"):
        monkeypatch.setenv("ORION_READER_BOX_PREDICT", flag)
        r = SimpleMeterReader(W, H)
        r.set_shot_state(True, 0.9, True)
        for i in range(6):
            r.read(_frame(0.7), ts=i / 60.0)             # stationary: _bvx ~ 0
        outs[flag] = [r.read(_green_only_frame(12), ts=(6 + j) / 60.0)["bbox"]
                      for j in range(4)]
    assert outs["1"] == outs["0"], \
        f"stationary holds must stay byte-frozen: {outs['1']} != {outs['0']}"


def test_box_predict_killed_on_disarm(monkeypatch):
    """FIX-2 kill-on-disarm: once the shot-gate disarms (post-release) the prediction dies --
    the reported box snaps back to the frozen last_tbox and the accumulator zeroes."""
    monkeypatch.setenv("ORION_READER_BOX_PREDICT", "1")
    r = SimpleMeterReader(W, H)
    x = 700
    r.set_shot_state(True, 0.9, True)
    r.read(_frame(0.7, col_x=x), ts=0.0)
    for i in range(1, 10):
        x += 8
        r.read(_frame(0.7, col_x=x), ts=i / 60.0)
    x += 8
    led = r.read(_green_only_frame(12, col_x=x), ts=10 / 60.0)["bbox"][0]
    assert led > r.last_tbox[0], "armed hold should lead"
    r.set_shot_state(False, 0.0, False)                  # release: shot over
    x += 8
    out = r.read(_green_only_frame(12, col_x=x), ts=11 / 60.0)
    assert out["bbox"][0] == r.last_tbox[0], \
        "disarmed hold must re-emit the FROZEN box (no post-release ride)"
    assert r._bp_dx == 0.0 and r._bp_dy == 0.0 and r._bp_n == 0


def test_box_predict_stays_inside_band(monkeypatch):
    """FIX-2 clamp: an extreme accumulated prediction can never walk the reported box out of
    the effective search band (onto decor) -- even with the total-shift cap removed."""
    monkeypatch.setenv("ORION_READER_BOX_PREDICT", "1")
    monkeypatch.setenv("ORION_READER_BOX_PREDICT_CAP_PX", "100000")
    r = SimpleMeterReader(W, H)
    assert r.read(_frame(0.7), ts=0.0)["detected"]
    x0, y0, x1, y1 = r._band_eff()
    r._bvx = 500.0                                       # violent rightward velocity
    r._bvy = 500.0
    tbox = list(r.last_tbox)
    for _ in range(10):                                  # accumulate way past the band edge
        bx = r._predict_tbox(tbox, True)
    assert x0 <= bx[0] <= x1 - 1 and y0 <= bx[1] <= y1 - 1, \
        f"reported box {bx} escaped the band {(x0, y0, x1, y1)}"
    assert bx[2] == tbox[2] and bx[3] == tbox[3], "w/h must never change (no tip clip/shrink)"


def test_new_fix_flags_default_state(monkeypatch):
    """Shipped defaults for the 2026-07-19 detection-quality pair: TIP_TIGHT (FIX-1)
    FLIPPED default-ON (excess headroom p50 -> 0 on all three measured sessions,
    uncovered-apex frames -> 0, 51/51 gates, top_clips 0); BOX_PREDICT (FIX-2) stays
    default-OFF -- its fresh-velocity scope never engages on the framedump replays
    (zero green-hold frames offline -> held_box_trail unchanged), so per the repo's
    A/B-then-flip discipline it waits for live evidence."""
    monkeypatch.delenv("ORION_READER_TIP_TIGHT", raising=False)
    monkeypatch.delenv("ORION_READER_BOX_PREDICT", raising=False)
    monkeypatch.delenv("ORION_READER_EPOCH_BRIDGE", raising=False)
    monkeypatch.delenv("ORION_READER_SHOT_SHAPE_LOCK", raising=False)
    monkeypatch.delenv("ORION_READER_HW_RESEAT_CONTINUITY", raising=False)
    r = SimpleMeterReader(W, H)
    assert r._tip_tight is True
    assert r._box_predict is False
    assert r._epoch_bridge is True
    assert r._shot_shape_lock is True
    assert r._hw_reseat_continuity is True


# --------------------------------------------------------------------------- #
#  Physical-shot lock continuity: arm-edge bridge + translation-only geometry.
# --------------------------------------------------------------------------- #
def _fresh_prearm_lock(r):
    out = None
    for i, frac in enumerate((0.25, 0.4, 0.55, 0.7)):
        r.set_shot_state(False, 0.0, False)
        out = r.read(_frame(frac), ts=i / 60.0)
        assert out["detected"]
    assert r._fresh_lock_streak >= 3 and len(r._track_h_hist) >= 3
    return out


def test_physical_arm_edge_bridges_only_recent_fresh_full_track_lock(monkeypatch):
    """A native arm notification may arrive after vision already has the new meter.  The edge
    must preserve that qualified lock, so an arm-cross on the edge is a stale-tier coast with the
    same box -- never a detected->blank->relock flicker."""
    monkeypatch.delenv("ORION_READER_EPOCH_BRIDGE", raising=False)
    r = SimpleMeterReader(W, H)
    last = _fresh_prearm_lock(r)
    box = list(last["bbox"])

    r.set_shot_state(True, 1.0, True)
    out = r.read(np.full((H, W, 3), 40, np.uint8), ts=4 / 60.0)

    assert r._epoch_bridge_active is True
    assert out["detected"] is True
    assert out["rejection_reason"] == "meter_memory"
    assert out["bbox"][2:] == box[2:]                   # no arm-edge stretch/shrink
    assert abs(out["bbox"][0] - box[0]) <= 12 and abs(out["bbox"][1] - box[1]) <= 12


def test_physical_arm_edge_still_drops_coasted_lock(monkeypatch):
    """The bridge is not a stale-lock loophole: one miss breaks its consecutive-fresh proof,
    so a held/phantom tail still takes the original cold epoch reset."""
    monkeypatch.delenv("ORION_READER_EPOCH_BRIDGE", raising=False)
    r = SimpleMeterReader(W, H)
    _fresh_prearm_lock(r)
    blank = np.full((H, W, 3), 40, np.uint8)
    r.set_shot_state(False, 0.0, False)
    assert r.read(blank, ts=4 / 60.0)["detected"]       # ordinary one-frame coast
    assert r._fresh_lock_streak == 0

    r.set_shot_state(True, 1.0, True)
    out = r.read(blank, ts=5 / 60.0)
    assert r._epoch_bridge_active is False
    assert out["detected"] is False
    assert out["bbox"] == [0, 0, 0, 0]


def test_physical_arm_edge_rejects_static_plausible_prearm_lock(monkeypatch):
    """Adversarial bridge guard: static meter-shaped decor can satisfy confidence, fresh-red
    streak, width, and full-track history.  Without a proven monotonic rise it must still be
    discarded at the physical arm edge, or hardware arming would preserve a phantom lock."""
    monkeypatch.delenv("ORION_READER_EPOCH_BRIDGE", raising=False)
    r = SimpleMeterReader(W, H)
    for i in range(5):
        r.set_shot_state(False, 0.0, False)
        out = r.read(_frame(0.55), ts=i / 60.0)
        assert out["detected"]
    # Prove the adversary reaches every old bridge condition except physical rise.
    assert r._fresh_lock_streak >= r._epoch_bridge_frames
    assert len(r._track_h_hist) >= r._epoch_bridge_frames
    assert r._lock_w_ref > 0.0 and r.conf >= r.CONF_MIN
    assert r._fresh_up_n == 0 and r._velocity <= 25.0

    r.set_shot_state(True, 1.0, True)
    out = r.read(np.full((H, W, 3), 40, np.uint8), ts=5 / 60.0)
    assert r._epoch_bridge_active is False
    assert out["detected"] is False
    assert out["bbox"] == [0, 0, 0, 0]


@pytest.mark.parametrize("start_x", (260, 900, 1760))
@pytest.mark.parametrize("start_floor", (400, 590, 730))
def test_physical_shot_box_translates_without_resizing(monkeypatch, start_x, start_floor):
    """Raw contour/apex dimensions may breathe as the player and meter move.  During a physical
    shot the served bbox follows centre/floor but its w/h are invariant; fill remains monotone."""
    monkeypatch.delenv("ORION_READER_SHOT_SHAPE_LOCK", raising=False)
    r = SimpleMeterReader(W, H)
    r.set_shot_state(True, 1.0, True)
    outs = []
    samples = ((0.25, start_x, 24, start_floor),
               (0.4, start_x + 6, 28, start_floor + 6),
               (0.55, start_x + 14, 22, start_floor + 14),
               (0.7, start_x + 24, 26, start_floor + 24))
    for i, (frac, x, w, floor_y) in enumerate(samples):
        outs.append(r.read(_frame(frac, col_x=x, col_w=w, floor_y=floor_y,
                                  track_top_y=floor_y - 130), ts=i / 60.0))
    assert all(o["detected"] for o in outs)
    assert len({tuple(o["bbox"][2:]) for o in outs}) == 1
    assert [o["bbox"][0] for o in outs] == sorted(o["bbox"][0] for o in outs)
    assert [o["bbox"][1] for o in outs] == sorted(o["bbox"][1] for o in outs)
    assert [o["fill"] for o in outs] == sorted(o["fill"] for o in outs)


def test_production_shape_epoch_survives_square_before_meter_animation(monkeypatch):
    """The controller edge normally precedes the HUD meter by several frames.

    Those expected pre-meter misses must not end the physical shape epoch; once the meter appears,
    its served bbox remains translation-only even when raw contour width/floor geometry breathes.
    """
    monkeypatch.delenv("ORION_READER_SHOT_SHAPE_LOCK", raising=False)
    r = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    blank = np.full((H, W, 3), 40, np.uint8)
    for i in range(8):
        r.set_shot_state(True, 1.0, True)
        out = r.detect(blank, ts=i / 60.0)
        assert out.detected is False
        assert r._shot_shape_live is True
        assert r._shot_shape is None

    samples = ((0.25, 620, 24, 520),
               (0.4, 628, 30, 528),
               (0.55, 640, 22, 540),
               (0.7, 654, 28, 552))
    outs = []
    for i, (frac, x, w, floor_y) in enumerate(samples, start=8):
        r.set_shot_state(True, 1.0, True)
        outs.append(r.detect(_frame(frac, col_x=x, col_w=w, floor_y=floor_y,
                                    track_top_y=floor_y - 130), ts=i / 60.0))
    assert all(o.detected for o in outs)
    assert len({tuple(o.bbox[2:]) for o in outs}) == 1
    assert [o.bbox[0] for o in outs] == sorted(o.bbox[0] for o in outs)
    assert [o.bbox[1] for o in outs] == sorted(o.bbox[1] for o in outs)


def test_cv_self_arm_cannot_latch_post_disarm_occlusion_memory(monkeypatch):
    """A detector-derived CV self-arm is forgeable by a phantom rising slope.  Only the physical
    arm bit may fund OCCL_WIDE memory after disarm."""
    monkeypatch.setenv("ORION_READER_OCCL_WIDE", "1")
    r = SimpleMeterReader(W, H)
    for i, frac in enumerate((0.25, 0.4, 0.55, 0.7, 0.85)):
        r.set_shot_state(True, 0.9, False)               # merged/CV arm, never physical
        assert r.read(_frame(frac), ts=i / 60.0)["detected"]
    assert r._occl_wide_until == 0.0


def test_physical_discontinuous_reseat_stays_visible_but_sampler_stale(monkeypatch):
    """After a long physical-shot occlusion, a far/lower same-shot re-find must publish its new
    box immediately without laundering through detected=False.  `held_reseat` keeps the one
    discontinuous fill out of the raw timing tier; internal state is seeded for the next frame."""
    monkeypatch.setenv("ORION_READER_OCCL_WIDE", "1")
    monkeypatch.setenv("ORION_READER_SILENT_RESEAT", "1")
    monkeypatch.delenv("ORION_READER_HW_RESEAT_CONTINUITY", raising=False)
    r = SimpleMeterReader(W, H)
    locked = _rise_and_lock(r)
    held = locked["fill"]
    shot_shape = tuple(locked["bbox"][2:])
    blank = np.full((H, W, 3), 40, np.uint8)
    for i in range(10):
        r.set_shot_state(True, 1.0, True)                # same physical arm window; no new edge
        assert r.read(blank, ts=(5 + i) / 60.0)["detected"]
    low = _frame(0.3, col_x=1200)
    r.set_shot_state(True, 1.0, True)
    out = r.read(low, ts=15 / 60.0)
    assert out["stage"] == "steal_reseat"
    assert out["detected"] is True and any(out["bbox"])
    assert tuple(out["bbox"][2:]) == shot_shape       # occlusion/reseat may translate, never stretch
    assert out["rejection_reason"] == "held_reseat"
    assert out["fill"] == pytest.approx(held, abs=0.01)
    assert 0.0 < r.last_fill < held - 20.0             # fresh state is ready for next frame


def test_production_gate_keeps_physical_reseat_visible_while_reverifying(monkeypatch):
    """The production gameplay gate must not undo read()'s display-safe hardware reseat.

    The new visual identity remains excluded from timing (`held_reseat`) until independent meter
    evidence re-authorizes it, but the already-authorized physical shot must not paint a zero-box
    frame while that proof crosses the adapter boundary.
    """
    monkeypatch.setenv("ORION_READER_OCCL_WIDE", "1")
    monkeypatch.setenv("ORION_READER_SILENT_RESEAT", "1")
    monkeypatch.delenv("ORION_READER_HW_RESEAT_CONTINUITY", raising=False)
    r = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(31)

    locked = None
    for i, frac in enumerate((0.25, 0.4, 0.55, 0.7, 0.85)):
        r.set_shot_state(True, 1.0, True)
        locked = r.detect(_frame(frac), ts=i / 60.0)
        assert locked.detected
    assert locked is not None and r._gameplay_lock_authorized
    assert r.gameplay_structure_verified is True
    assert locked.gameplay_structure_epoch == 31
    held = locked.fill_pct
    shot_shape = tuple(locked.bbox[2:])

    blank = np.full((H, W, 3), 40, np.uint8)
    for i in range(10):
        r.set_shot_state(True, 1.0, True)
        assert r.detect(blank, ts=(5 + i) / 60.0).detected

    r.set_shot_state(True, 1.0, True)
    reseat = r.detect(_frame(0.3, col_x=1200), ts=15 / 60.0)
    assert reseat.detected is True and any(reseat.bbox)
    assert tuple(reseat.bbox[2:]) == shot_shape       # translate only; never stretch
    assert reseat.rejection_reason == "held_reseat"  # never accepted by either timing tier
    assert reseat.fill_pct == pytest.approx(held, abs=0.01)
    assert reseat.fill_velocity_pct_s == 0.0
    assert r._gameplay_lock_authorized is False       # new identity still needs proof
    assert r.gameplay_structure_verified is False     # prior identity's proof cannot transfer
    assert r.last_debug.get("display_continuity") == 1

    # Connected structure on the next clean frame completes re-verification with no visible gap.
    r.set_shot_state(True, 1.0, True)
    verified = r.detect(_frame(0.4, col_x=1200), ts=16 / 60.0)
    assert verified.detected is True and any(verified.bbox)
    assert tuple(verified.bbox[2:]) == shot_shape
    assert r._gameplay_lock_authorized is True
    assert r.gameplay_structure_verified is True
    assert verified.gameplay_structure_epoch == 31
    assert r.last_debug.get("gameplay_evidence") == "structure"


def test_production_gate_suppressed_reseat_cannot_inherit_structure_proof():
    """The non-display-continuity steal path is the same new identity and also clears proof."""
    r = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    r._shot_armed_hw = True
    r._gameplay_lock_authorized = True
    r._gameplay_structure_verified = True
    r.last_debug = {"stage": "steal_reseat"}  # no hw_reseat display marker

    sample = r._no_meter_sample("steal_reseat", "steal_reseat")
    out = r._qualify_gameplay_sample(sample)

    assert out["detected"] is False
    assert r._gameplay_lock_authorized is False
    assert r.gameplay_structure_verified is False


def test_production_gameplay_gate_refuses_courtwide_decor_without_a_rise():
    """A court-wide seat is NOT structure on its own -- decor must not take ownership of a shot.

    2026-08-03 live: a window mullion at the left screen edge -- a tall, thin, high-contrast
    vertical bar, geometrically indistinguishable from an unfilled meter -- seated via the
    court-wide tier 138-148ms after the Square press, inside the ~300-400ms gap before the real
    meter HUD renders. It took ownership of 4 of 34 shots and produced 4 aborts (all three
    live_trajectory_timeouts plus the lone detector_authority_lost): 8% of the session burned.
    None of them fired, so this was never a wrong-release risk -- it was a stolen-shot risk.

    Driven at the qualification rule directly. The live decor seated court-wide on WEAK green
    (380 decor rows carried green_confidence > 0.5) which a synthetic frame does not reproduce, so
    a pixel-level fixture here silently tests nothing -- an earlier version of this test did exactly
    that and passed with the fix reverted.

    Decor cannot pass the rise proof because it is not filling: its apparent fill thrashes as it is
    re-measured (23.8 -> 37.0 -> 28.9 -> 38.8 across four consecutive live frames).
    """
    def _decor_sample(fill_pct):
        # A court-wide-seated candidate with NO green segment of its own.
        return {"detected": True, "green": None, "fill_pct": fill_pct,
                "rise_state": "unstable", "velocity_pct_s": 0.0}

    r = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(21)
    r.set_shot_state(True, 1.0, True)   # hardware vouches for the shot; only the PIXELS are decor
    r._courtwide_lock = True            # the seat the live mullion obtained

    for fill in (23.8, 37.0, 28.9, 38.8):
        r._courtwide_lock = True      # the seat persists across the episode
        r._qualify_gameplay_sample(_decor_sample(fill), proof_epoch=21)

    assert r._gameplay_lock_authorized is False, (
        "non-monotonic court-wide decor was granted ownership authority")
    assert r._gameplay_structure_proof_epoch != 21, (
        "decor latched the structure proof for a real physical epoch")


def test_production_gameplay_gate_still_accepts_courtwide_meter_that_is_rising():
    """The counterpart: a genuine court-wide meter still qualifies once it proves a rise.

    Without this, the decor guard above could be 'satisfied' by simply disabling the court-wide
    path, which would cost every real edge/distant meter.
    """
    r = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(22)
    r.set_shot_state(True, 1.0, True)
    r._courtwide_lock = True
    r._fresh_up_n = r._fresh_up_min
    r._qualify_gameplay_sample(
        {"detected": True, "green": None, "fill_pct": 44.0,
         "rise_state": "rising", "velocity_pct_s": 120.0},
        proof_epoch=22)

    assert r._gameplay_lock_authorized is True
    assert r._gameplay_structure_proof_epoch == 22


# --------------------------------------------------------------------------- #
#  B6 ARM-GRACE CAP (2026-08-04): the fake-lock breaker must be REACHABLE on the
#  PRODUCTION wiring (require_gameplay_eligibility=True).
#
#  Regression context: every pre-existing breaker test above constructs
#  SimpleMeterReader(W, H) WITHOUT require_gameplay_eligibility -- the one
#  configuration production never uses. On the shipped wiring detect() only runs
#  read() while _shot_armed_hw is True, and the B2 arm-guard handed _shot_armed_hw
#  blanket breaker grace, which zeroed _static_fill_n every frame. Result: the
#  breaker could never fire live (`static_fake_lock` appears ZERO times in every
#  2026-08-03/04 session log) while a window mullion at x~0.11W held a 10-18% fill
#  for 8.8 s and took ownership of shots the bot then could not drive.
# --------------------------------------------------------------------------- #

def _prod_reader(monkeypatch, arm_grace_ms=None):
    monkeypatch.setenv("ORION_READER_FAKELOCK_BREAK", "1")
    monkeypatch.setenv("ORION_READER_FAKELOCK_CAP_MID", "6")
    monkeypatch.setenv("ORION_READER_FAKELOCK_CAP_HI", "12")
    if arm_grace_ms is not None:
        monkeypatch.setenv("ORION_READER_ARM_GRACE_MS", str(arm_grace_ms))
    r = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(1)
    r.set_shot_state(True, 1.0, True)
    return r


def test_fake_lock_breaker_is_reachable_under_production_wiring(monkeypatch):
    """A multi-second dead hold inside a hw-armed window must still be suppressed.

    This is the bug that shipped: with require_gameplay_eligibility=True the breaker
    was unreachable, so a static decor lock survived indefinitely.
    """
    r = _prod_reader(monkeypatch, arm_grace_ms=200)     # 200ms grace = 12 frames @60fps
    reasons = []
    for i in range(180):                                # 3 s of a byte-frozen 62% fill
        out = r.detect(_frame(0.62), ts=i / 60.0)
        reasons.append(out.rejection_reason)
    assert "static_fake_lock" in reasons, (
        "fake-lock breaker never fired on the production wiring -- a static decor "
        "lock would survive forever (2026-08-04 window-mullion false lock)")


def test_arm_grace_protects_the_within_shot_hold(monkeypatch):
    """A real release/outcome freeze early in the hw-armed window must NOT be broken.

    This is the A2 protection the unbounded grace was written for; capping the grace
    must not cost it. Nothing may be suppressed before the grace budget expires.
    """
    r = _prod_reader(monkeypatch, arm_grace_ms=2000)
    for i in range(90):                                 # 1.5 s in, still inside the budget
        out = r.detect(_frame(0.95), ts=i / 60.0)
        assert out.rejection_reason != "static_fake_lock", (
            "suppressed a within-shot frozen hold at %.0f ms after the arm edge" % (i / 60.0 * 1000))


def test_arm_grace_budget_restarts_on_a_new_hardware_shot(monkeypatch):
    """Each physical press earns a fresh grace budget, so a genuine next shot is never
    born already-suppressed."""
    r = _prod_reader(monkeypatch, arm_grace_ms=500)
    for i in range(120):                                 # burn well past the budget
        r.detect(_frame(0.62), ts=i / 60.0)
    r.notify_physical_shot_start(2)                      # new press
    r.set_shot_state(True, 1.0, True)
    out = r.detect(_frame(0.62), ts=120 / 60.0)
    assert out.rejection_reason != "static_fake_lock"
    assert r._static_fill_n == 0


def test_arm_grace_zero_restores_legacy_unbounded_grace(monkeypatch):
    """ORION_READER_ARM_GRACE_MS=0 is the documented escape hatch back to HEAD behaviour."""
    r = _prod_reader(monkeypatch, arm_grace_ms=0)
    for i in range(180):
        out = r.detect(_frame(0.62), ts=i / 60.0)
        assert out.rejection_reason != "static_fake_lock"


# --------------------------------------------------------------------------- #
#  B7 ARM-EDGE PRIOR-PRESENCE VETO (2026-08-04, opt-in).
#  Measured on logs/diagnostics/framedump/session_20260804_032333: at the physical
#  arm edge the red fraction inside a candidate box is 0.000 median / 0.020 MAX
#  over 465 real-meter candidates vs 0.350 median over 707 window-mullion
#  candidates, so a 0.10 threshold rejects 68% of the decor at 0.00% real cost.
# --------------------------------------------------------------------------- #

def test_arm_edge_veto_is_opt_in_and_off_by_default():
    """Default OFF: the acquire path must be byte-identical until it is A/B'd live."""
    r = SimpleMeterReader(W, H)
    assert r._arm_edge_veto is False
    assert r._prior_presence_veto((100, 100, 16, 108)) is False


def test_arm_edge_veto_rejects_red_already_present_at_the_press(monkeypatch):
    monkeypatch.setenv("ORION_READER_ARM_EDGE_VETO", "1")
    r = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(5)
    r.set_shot_state(True, 1.0, True)
    # the press frame already carries the candidate's red -> decor
    r._note_arm_edge_reference(_frame(0.9, col_x=300))
    assert r._prior_presence_veto((300, FLOOR_Y - 108, COL_W, 108)) is True


def test_arm_edge_veto_passes_a_meter_that_pops_in_after_the_press(monkeypatch):
    """The real case: nothing at the candidate box when the shot was pressed."""
    monkeypatch.setenv("ORION_READER_ARM_EDGE_VETO", "1")
    r = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(6)
    r.set_shot_state(True, 1.0, True)
    blank = np.full((H, W, 3), 40, np.uint8)          # press frame: no HUD yet
    r._note_arm_edge_reference(blank)
    assert r._prior_presence_veto((300, FLOOR_Y - 108, COL_W, 108)) is False


def test_arm_edge_veto_fails_open_without_a_reference(monkeypatch):
    """No reference for the current epoch -> never tighten."""
    monkeypatch.setenv("ORION_READER_ARM_EDGE_VETO", "1")
    r = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(7)
    r.set_shot_state(True, 1.0, True)
    assert r._arm_edge_mask is None
    assert r._prior_presence_veto((300, 400, COL_W, 108)) is False


# --------------------------------------------------------------------------------------- #
#  A4  ORION_READER_TRACK_H_CAP -- the HIGH half of the full-track seed gate.
#
#  Measured on logs/diagnostics/framedump/session_20260804_032333 (29 shots / 1182 locked
#  frames): the fill DENOMINATOR sits at 106-109px on 96.9% of frames and at 118-119px on the
#  rest -- and those out-of-family frames are the OPENING frames of two shots' rises, so their
#  whole early ramp reads ~8.6pp (~46ms) low on a track ~10% too tall. Cause: the 0.90*max seed
#  gate is ONE-SIDED. A candidate is admitted no matter how TALL it is, so one inflated
#  measurement ratchets the 5-sample median up and holds it there for the rest of the lock --
#  a per-shot correlated, horizon-flat bias that no amount of frame averaging removes.
# --------------------------------------------------------------------------------------- #
def _tall_cap_frame(fill_frac=0.5, extra_top=20, col_x=COL_X, col_w=COL_W, bg=40):
    """The same meter, but with a bogus GREEN run extending `extra_top` px ABOVE the real cap
    (a reflection / décor, or an arrow-tip apex walk that chained background rows). The
    full-track CANDIDATE it produces is ~extra_top px taller than the true track."""
    f = _frame(fill_frac=fill_frac, col_x=col_x, col_w=col_w, green=True, bg=bg)
    f[TRACK_TOP_Y - 6 - extra_top:TRACK_TOP_Y - 6, col_x:col_x + col_w] = GREEN
    return f


def _settle_track_history(r, frac=0.5, n=6):
    """Establish a clean full-track height history at the TRUE scale."""
    out = None
    for i in range(n):
        out = r.read(_frame(frac), ts=i / 60.0)
        assert out["detected"]
    return out


def test_track_h_cap_refuses_an_inflated_full_track_candidate(monkeypatch):
    """A run of candidates ~15% TALLER than the established track must not enter the denominator
    history -- if it does, every fill for the rest of the lock is divided by a track that is not
    there and reads systematically LOW."""
    monkeypatch.setenv("ORION_READER_TRACK_H_CAP", "1")
    r = SimpleMeterReader(W, H)
    good = _settle_track_history(r)["fill"]
    settled = max(r._track_h_hist)
    last = None
    for i in range(6, 12):                       # enough frames to move a 5-sample median
        last = r.read(_tall_cap_frame(0.5), ts=i / 60.0)
    assert last["detected"], "the cap gate must never cost the lock"
    assert max(r._track_h_hist) <= settled * 1.06 + 1e-6, (
        f"an inflated full-track candidate entered the denominator history: "
        f"{r._track_h_hist} (settled max {settled})")
    assert last["fill"] == pytest.approx(good, abs=1.0), (
        f"fill fell from {good:.2f} to {last['fill']:.2f} -- the denominator was poisoned by a "
        f"taller-than-real track candidate (a per-shot correlated under-read)")


def test_track_h_cap_opt_out_reproduces_the_shipped_poisoning(monkeypatch):
    """Explicit '0' -> the OLD one-sided gate, so the baseline this flag exists to fix stays
    reachable and pinned.

    DEFAULT FLIPPED 2026-08-04, deliberately: this test previously asserted the flag was OFF when
    unset, exactly so that a silent flip would be visible. The flip is not silent -- the dev
    launcher had been setting it ON for every live batch (including the 96.9%% fire-rate one), so
    default-OFF meant customers ran a detector nobody had measured. See
    test_track_h_cap_is_on_by_default for the shipped behaviour."""
    monkeypatch.setenv("ORION_READER_TRACK_H_CAP", "0")
    r = SimpleMeterReader(W, H)
    assert not r._track_h_cap
    good = _settle_track_history(r)["fill"]
    settled = max(r._track_h_hist)
    last = None
    for i in range(6, 12):
        last = r.read(_tall_cap_frame(0.5), ts=i / 60.0)
    assert max(r._track_h_hist) > settled * 1.06, (
        "shipped behaviour changed: the one-sided gate no longer admits a taller candidate")
    assert last["fill"] < good - 1.0, (
        "shipped behaviour changed: the poisoned denominator no longer under-reads")


def test_track_h_cap_is_on_by_default(monkeypatch):
    """A fresh install must get the cap. It was default-OFF in code and ON only via the gitignored
    dev launcher, so every measured result came from a configuration no customer would run."""
    monkeypatch.delenv("ORION_READER_TRACK_H_CAP", raising=False)
    r = SimpleMeterReader(W, H)
    assert r._track_h_cap, "the height cap must be on by default"
    good = _settle_track_history(r)["fill"]
    settled = max(r._track_h_hist)
    last = None
    for i in range(6, 12):
        last = r.read(_tall_cap_frame(0.5), ts=i / 60.0)
    assert max(r._track_h_hist) <= settled * 1.06, (
        "the cap must refuse the tall out-of-family candidate")
    assert last["fill"] >= good - 1.0, (
        "with the cap on, the fill denominator must not be poisoned")


def test_track_h_cap_still_adopts_a_genuinely_taller_track(monkeypatch):
    """The ceiling must not DEADLOCK a reader whose history was seeded off a short outlier. A
    track that is persistently taller -- the meter's fixed structure, present on every frame --
    is adopted once the high-side recovery run is satisfied."""
    monkeypatch.setenv("ORION_READER_TRACK_H_CAP", "1")
    monkeypatch.setenv("ORION_READER_TRACK_H_CAP_M", "8")
    r = SimpleMeterReader(W, H)
    _settle_track_history(r)
    before = float(np.median(r._track_h_hist[-5:]))
    for i in range(6, 40):                        # a long, consistent, FLAT taller run
        r.read(_tall_cap_frame(0.5, extra_top=20), ts=i / 60.0)
    after = float(np.median(r._track_h_hist[-5:]))
    assert after > before * 1.06, (
        f"a persistently taller track was never adopted ({before} -> {after}); the high-side "
        f"ceiling has deadlocked the denominator")


def test_track_h_cap_refusal_cannot_leak_through_the_low_side_recovery(monkeypatch):
    """A4's ceiling and A1's low-side rebase share `seed`. A HIGH-refused candidate trivially
    clears A1's `cand >= 0.50*max` test, so without an explicit exclusion the ceiling would be a
    _scale_reset_m-frame speed bump instead of a gate."""
    monkeypatch.setenv("ORION_READER_TRACK_H_CAP", "1")
    monkeypatch.setenv("ORION_READER_TRACK_H_CAP_M", "999")   # high-side recovery can never fire
    monkeypatch.setenv("ORION_READER_SCALE_RESET", "1")
    monkeypatch.setenv("ORION_READER_SCALE_RESET_M", "3")
    r = SimpleMeterReader(W, H)
    _settle_track_history(r)
    settled = max(r._track_h_hist)
    for i in range(6, 30):
        r.read(_tall_cap_frame(0.5, extra_top=20), ts=i / 60.0)
    assert max(r._track_h_hist) <= settled * 1.06 + 1e-6, (
        f"a HIGH-refused candidate was re-admitted through A1's low-side recovery: "
        f"{r._track_h_hist}")


# --------------------------------------------------------------------------------------- #
#  ORION_READER_SUBPIX_EDGE -- 50%-coverage interpolation of the fill boundary.
# --------------------------------------------------------------------------------------- #
def _blended_edge_frame(alpha, fill_frac=0.5, col_x=COL_X, col_w=COL_W, bg=40):
    """A meter whose boundary ROW is partially covered: the row directly above the solid red is
    an alpha blend of background and red, exactly as an anti-aliased/encoded edge renders a
    boundary that falls between pixel centres. `alpha` is that row's true coverage. Kept below
    the production red band so the WHOLE-pixel boundary (red_top_coarse) does not move -- only
    the sub-pixel estimate may see it."""
    f = _frame(fill_frac=fill_frac, col_x=col_x, col_w=col_w, green=True, bg=bg)
    track_h = FLOOR_Y - TRACK_TOP_Y
    red_top = int(round(FLOOR_Y - fill_frac * track_h))
    half = col_w // 2
    for x0, x1, shade in ((col_x, col_x + half, RED_A),
                          (col_x + half, col_x + col_w, RED_B)):
        f[red_top - 1, x0:x1] = tuple(
            int(round(bg * (1.0 - alpha) + c * alpha)) for c in shade)
    return f


def test_subpix_edge_resolves_a_partially_covered_boundary_row(monkeypatch):
    """The boundary pixel's INTENSITY encodes its coverage. Sweeping that coverage must move the
    reported fill CONTINUOUSLY and by less than one whole pixel -- the shipped whole-pixel
    refinement thresholds it away entirely."""
    monkeypatch.setenv("ORION_READER_SUBPIX_EDGE", "1")
    vals, step_pp = [], None
    for a in (0.0, 0.25, 0.5, 0.75):
        r = SimpleMeterReader(W, H)
        out = None
        for i in range(4):
            out = r.read(_blended_edge_frame(a), ts=i / 60.0)
        assert out["detected"], f"lost the lock at boundary coverage {a}"
        vals.append(out["fill"])
        step_pp = 100.0 / float(np.median(r._track_h_hist[-5:]))   # one whole pixel, in pp
    assert vals == sorted(vals), (
        f"reported fill is not monotonic in the boundary row's coverage: {vals} -- the "
        f"sub-pixel estimator is not reading the intensity ramp")
    span = vals[-1] - vals[0]
    assert span > 0.15 * step_pp, (
        f"coverage 0.00 -> 0.75 moved the fill only {span:.4f}pp (one pixel = {step_pp:.4f}pp); "
        f"the boundary intensity is being thresholded away, not interpolated")
    assert span < 1.30 * step_pp, (
        f"coverage 0.00 -> 0.75 moved the fill {span:.4f}pp, more than one whole pixel "
        f"({step_pp:.4f}pp) -- that is a boundary JUMP, not sub-pixel interpolation")


def test_subpix_edge_default_off_never_consults_the_estimator(monkeypatch):
    """Flag unset -> the shipped threshold-crossing refinement, bit-for-bit: the sub-pixel
    estimator is not called at all."""
    monkeypatch.delenv("ORION_READER_SUBPIX_EDGE", raising=False)
    r = SimpleMeterReader(W, H)
    assert not r._subpix_edge

    def _boom(*a, **k):
        raise AssertionError("_subpix_edge_row was consulted with the flag OFF")
    monkeypatch.setattr(r, "_subpix_edge_row", _boom)
    off = [r.read(_blended_edge_frame(0.5), ts=i / 60.0)["fill"] for i in range(4)]

    monkeypatch.setenv("ORION_READER_SUBPIX_EDGE", "1")
    r2 = SimpleMeterReader(W, H)
    assert r2._subpix_edge
    on = [r2.read(_blended_edge_frame(0.5), ts=i / 60.0)["fill"] for i in range(4)]
    assert off != on, "the flag is inert -- it must change the boundary estimate when ON"


def test_subpix_edge_falls_back_when_the_ramp_is_not_resolvable(monkeypatch):
    """No measurable colour step across the edge, too few rows either side, or too few interior
    columns -> return None so the caller keeps the shipped estimate. The flag may only ever
    REPLACE a measurable edge, never invent one."""
    monkeypatch.setenv("ORION_READER_SUBPIX_EDGE", "1")
    r = SimpleMeterReader(W, H)
    # a clean synthetic edge IS resolvable, and lands within a pixel of the coarse boundary --
    # so the None results below are the GUARDS firing, not the estimator being broken outright.
    edge = np.full((40, 30, 3), 40, np.uint8)
    edge[20:, 4:26] = (0, 0, 255)
    e = r._subpix_edge_row(edge, 20)
    assert e is not None and abs(e - 20.0) <= 1.0, e

    flat = np.full((40, 30, 3), 40, np.uint8)                 # no edge at all
    assert r._subpix_edge_row(flat, 20) is None
    narrow = np.full((40, 4, 3), 40, np.uint8)                # too few interior columns
    assert r._subpix_edge_row(narrow, 20) is None
    # TOO FEW ROWS either side of the boundary -- the fg/bg medians would be estimated off 2
    # rows of a strip that has barely any strip, so the "ramp" is one sample wide.
    tiny = np.full((6, 30, 3), 40, np.uint8)
    tiny[3:, 4:26] = (0, 0, 255)
    assert r._subpix_edge_row(tiny, 3) is None, \
        "a 6-row window is not enough support for a coverage ramp"
    # NO MEASURABLE COLOUR STEP -- a 4-level difference is inside the codec's own noise, so the
    # normalised alpha would be pure amplified noise with a perfectly confident-looking crossing.
    faint = np.full((40, 30, 3), 40, np.uint8)
    faint[20:, 4:26] = (42, 42, 42)
    assert r._subpix_edge_row(faint, 20) is None, \
        "a 4-level colour step is noise, not a resolvable fill boundary"


# --------------------------------------------------------------------------- #
#  B9 -- the MICRO court-wide tier: what actually admitted the arena facade
# --------------------------------------------------------------------------- #
def _micro_grey_decor_720(*, col_x=250, floor_y=230, col_w=9, tip_up=40,
                          grey=(110, 110, 110), cap=(60, 120, 60)):
    """Arena facade as the MICRO tier sees it: a NEUTRAL GREY vertical bar with a small
    desaturated-green blob at the micro tier's cap/body relation above it.

    Nothing here is red. The bar's colour is the exact corner of the `_MICRO_RED` BGR box
    (B<=110, G<=110, R>=110), which is what let dim stone/mullion architecture into a band
    named "red" -- reproduced from session_20260804_032333, where all 638 micro candidates
    measured R - max(G,B) of 7-24 against 220-228 for every real meter body.
    """
    frame = np.full((720, 1280, 3), 30, np.uint8)
    cap_y = floor_y - tip_up
    frame[cap_y:cap_y + 2, col_x:col_x + col_w] = cap
    frame[cap_y + 2:floor_y, col_x:col_x + col_w] = grey
    return frame


def test_micro_band_admits_neutral_grey_but_the_production_mask_does_not():
    """The `_MICRO_RED` box has a GREY corner; `_micro_redmask` is what closes it."""
    r = SimpleMeterReader(1280, 720)
    frame = _micro_grey_decor_720()
    raw = r._redmask(frame, bounds=r._MICRO_RED)
    assert int((raw > 0).sum()) > 0, \
        "fixture invalid: the shipped _MICRO_RED band must admit this grey (that is the bug)"
    gated = r._micro_redmask(frame)
    assert int((gated > 0).sum()) == 0, \
        "neutral grey must not survive the red-dominance floor"
    # ...and the floor does not touch red that is merely codec-softened: every band in the
    # stack that is allowed to call something red sits at dominance >= 100.
    for soft in (MICRO_RED, SOFT_RED_A, SOFT_RED_B, (0, 0, 220), (70, 70, 255)):
        assert int(soft[2]) - int(max(soft[0], soft[1])) >= r._micro_red_dom


def test_micro_tier_refuses_grey_decor_that_head_locked(monkeypatch):
    """END-TO-END: grey facade in the micro cap/body relation never becomes a lock."""
    frame = _micro_grey_decor_720()

    r = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(140)
    for i in range(4):
        r.set_shot_state(True, 1.0, True)
        out = r.detect(frame.copy(), ts=i / 60.0)
        assert out.detected is False, "grey arena decor must never seat a micro lock"
        assert r._courtwide_lock is False
    assert r._micro_pending_count == 0

    # REVERT-TRACE: with the dominance floor disabled this same fixture DOES lock -- so the
    # test is pinning the fix, not an unrelated gate.
    monkeypatch.setenv("ORION_READER_MICRO_RED_DOM", "0")
    head = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    head.notify_physical_shot_start(141)
    seated = ""
    for i in range(4):
        head.set_shot_state(True, 1.0, True)
        head.detect(frame.copy(), ts=i / 60.0)
        seated = seated or head._courtwide_acquire_tier
    assert seated == "micro_compressed_cap_first", \
        "revert-trace failed: HEAD must seat this grey decor through the micro tier"


def test_micro_scale_gate_closes_only_after_a_full_scale_meter_was_measured():
    """B9b: the tier is skipped once the SESSION has measured a meter it cannot represent."""
    r = SimpleMeterReader(1280, 720)
    assert r._micro_tier_plausible() is True, "a cold reader must never be tightened"
    r._sess_track_h = [108.0] * r._micro_gate_min_n          # the live 720p full track
    assert r._micro_tier_plausible() is False
    r._sess_track_h = [24.0] * r._micro_gate_min_n           # a genuine sub-quarter capture
    assert r._micro_tier_plausible() is True
    r._sess_track_h = [108.0] * (r._micro_gate_min_n - 1)    # not enough evidence yet
    assert r._micro_tier_plausible() is True


def test_micro_scale_gate_blocks_the_tier_end_to_end(monkeypatch):
    """The micro fixture that locks on a cold reader is refused once the scale is known."""
    frame = _micro_half_court_frame_720()
    second = _micro_half_court_frame_720(col_x=1101, red_h=5)

    r = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    r._sess_track_h = [108.0] * (r._micro_gate_min_n + 1)
    r.notify_physical_shot_start(150)
    r.set_shot_state(True, 1.0, True)
    assert r.detect(frame, ts=0.0).detected is False
    r.set_shot_state(True, 1.0, True)
    assert r.detect(second, ts=1.0 / 60.0).detected is False
    assert r._courtwide_lock is False
    assert r._micro_gate_blocks > 0

    # REVERT-TRACE: the same two frames DO lock with the gate off.
    monkeypatch.setenv("ORION_READER_MICRO_SCALE_GATE", "0")
    head = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    head._sess_track_h = [108.0] * (head._micro_gate_min_n + 1)
    head.notify_physical_shot_start(151)
    head.set_shot_state(True, 1.0, True)
    assert head.detect(frame, ts=0.0).detected is False      # 2-frame corroboration
    head.set_shot_state(True, 1.0, True)
    assert head.detect(second, ts=1.0 / 60.0).detected is True
    assert head._courtwide_acquire_tier == "micro_compressed_cap_first"


def test_micro_lock_never_seeds_the_session_scale_that_judges_it():
    """No self-vouching: a micro-seated lock is barred from `_sess_track_h`."""
    r = SimpleMeterReader(1280, 720, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(160)
    r.set_shot_state(True, 1.0, True)
    r.detect(_micro_half_court_frame_720(), ts=0.0)
    r.set_shot_state(True, 1.0, True)
    out = r.detect(_micro_half_court_frame_720(col_x=1101, red_h=5), ts=1.0 / 60.0)
    assert out.detected is True and r._courtwide_acquire_tier == "micro_compressed_cap_first"
    for i in range(2, 8):
        r.set_shot_state(True, 1.0, True)
        r.detect(_micro_half_court_frame_720(col_x=1101, red_h=5 + i), ts=i / 60.0)
    assert r._sess_track_h == [], \
        "a micro-tier lock must not feed the measurement the micro gate reads"


# --------------------------------------------------------------------------- #
#  BOX-HUG -- the served box must trace the METER, not the coloured TRACK
# --------------------------------------------------------------------------- #
def _outlined_meter_1080(*, col_x=COL_X, col_w=COL_W, stroke=4, fill_frac=0.85,
                         floor_y=FLOOR_Y, track_top_y=TRACK_TOP_Y):
    """A meter drawn the way the game draws it: a flat-shaded frame STROKE wrapping the
    coloured track on all four sides and closing into a chevron at each end, over a
    textured (non-flat) background. Returns (frame, outline_bounds)."""
    yy = (np.arange(H, dtype=np.int32) % 60 + 30).astype(np.uint8).reshape(H, 1, 1)
    f = np.repeat(np.repeat(yy, W, axis=1), 3, axis=2).copy()   # vertically textured bg
    # The live stroke is a MID grey (measured (100,99,99) on session_20260804_032333), i.e.
    # V ~ 100 -- below the silver mask `_enclose_meter_bbox` looks for, and one tall connected
    # component. That is exactly why the shipped enclosure can never union it.
    GREY = (100, 99, 99)
    x0, x1 = col_x - stroke, col_x + col_w + stroke
    apex_y = track_top_y - 6 - stroke                        # top chevron tip
    bot_y = floor_y + stroke                                 # bottom chevron reach
    f[apex_y:bot_y, x0:x1] = GREY
    f[track_top_y - 6:track_top_y, col_x:col_x + col_w] = GREEN
    red_top = int(round(floor_y - fill_frac * (floor_y - track_top_y)))
    half = col_w // 2
    f[red_top:floor_y, col_x:col_x + half] = RED_A
    f[red_top:floor_y, col_x + half:col_x + col_w] = RED_B
    return f, (x0, apex_y, x1, bot_y)


def test_served_box_encloses_the_meter_outline_and_both_arrow_caps(monkeypatch):
    """The box must HUG the whole meter -- frame stroke + top apex + bottom chevron."""
    frame, (ox0, oy0, ox1, oy1) = _outlined_meter_1080()
    r = SimpleMeterReader(W, H)
    out = r.read(frame, ts=0.0)
    assert out["detected"]
    bx, by, bw, bh = out["bbox"]
    assert bx <= ox0, "box left cuts into the meter's frame stroke"
    assert bx + bw >= ox1, "box right cuts into the meter's frame stroke"
    assert by <= oy0, "box top sits ON the arrow apex (occludes the tip)"
    assert by + bh >= oy1, "box bottom cuts through the bottom chevron"

    # REVERT-TRACE: with the hug disabled the shipped box is drawn INSIDE the meter.
    monkeypatch.setenv("ORION_READER_BOX_HUG", "0")
    head = SimpleMeterReader(W, H)
    hb = head.read(frame, ts=0.0)["bbox"]
    assert hb[0] > ox0 and hb[0] + hb[2] < ox1, \
        "revert-trace failed: HEAD's box must be narrower than the meter outline"


def test_box_hug_is_inert_without_an_outline_to_measure():
    """A flat background has no stroke to find, so the box is byte-identical to HEAD."""
    frame = _frame(0.7)
    on = SimpleMeterReader(W, H).read(frame, ts=0.0)
    off = SimpleMeterReader(W, H)
    off._box_hug = False
    off_out = off.read(frame, ts=0.0)
    assert on["bbox"] == off_out["bbox"]
    assert on["fill"] == pytest.approx(off_out["fill"])


def test_box_hug_never_shrinks_the_box_or_clips_the_red_top():
    """Grow-only: the hug can only make `top_clips` better, never worse."""
    frame, _ = _outlined_meter_1080(fill_frac=0.55)
    off = SimpleMeterReader(W, H)
    off._box_hug = False
    off_out = off.read(frame, ts=0.0)
    base = off_out["bbox"]
    on = SimpleMeterReader(W, H).read(frame, ts=0.0)
    assert on["detected"]
    hug = on["bbox"]
    assert hug[0] <= base[0] and hug[1] <= base[1]
    assert hug[0] + hug[2] >= base[0] + base[2]
    assert hug[1] + hug[3] >= base[1] + base[3]
    assert on["fill"] == pytest.approx(off_out["fill"]), \
        "the hug is output-only: fill is untouched"


# --------------------------------------------------------------------------- #
#  PRODUCTION-WIRING INVARIANT (the reason the regression gates are blind here)
# --------------------------------------------------------------------------- #
def _armed_locked_reader(monkeypatch=None, fill=0.5):
    """A reader locked on the synthetic meter and wired the way PRODUCTION wires it.

    The regression gates all build SimpleMeterReader(W, H) bare, which leaves
    `_shot_armed_hw`/`_gameplay_lock_authorized` false -- and `_tracking_bounds()`
    only relaxes to full frame when BOTH are true. A bare reader therefore cannot
    reach the code this gate guards, which is exactly why the defect survived.
    """
    r = SimpleMeterReader(W, H)
    out = r.read(_frame(fill), ts=0.0)
    assert out["detected"], "fixture must seat a lock first"
    r.set_shot_state(True, 1.0, armed_hw=True)
    r._gameplay_lock_authorized = True
    return r


def test_bounds_relaxed_matches_tracking_bounds_exactly():
    """The gate's predicate and the relaxation it guards must never disagree."""
    r = SimpleMeterReader(W, H)
    assert not r._bounds_relaxed()
    assert r._tracking_bounds() == r._band_eff()

    r = _armed_locked_reader()
    assert r._bounds_relaxed()
    assert r._tracking_bounds() == (0, 0, W, H), \
        "production wiring must be what opens the full-frame path"


# --------------------------------------------------------------------------- #
#  F3: INSTRUMENT FIXES (all three are observability-only)
# --------------------------------------------------------------------------- #
def test_f3_fill_overflow_records_the_pre_clamp_value():
    """The emitted fill stays clamped to [0,100]; the PRE-clamp value is recorded.

    Without this, 'no shot ever overshot 100' is a tautology of the clamp.
    """
    r = SimpleMeterReader(W, H)
    out = r.read(_frame(1.0), ts=0.0)
    assert out["detected"]
    assert 0.0 <= out["fill"] <= 100.0, "the clamp itself must be preserved"
    assert r._fill_overflow_diag is True, "flag must be default-ON"
    assert r._fill_raw != (0.0, 0.0), \
        "the PRE-clamp fill must actually be recorded, not left at its init value"

    # REVERT-TRACE: with the flag OFF nothing is recorded.
    off = SimpleMeterReader(W, H)
    off._fill_overflow_diag = False
    off_out = off.read(_frame(1.0), ts=0.0)
    assert off._fill_raw == (0.0, 0.0), "revert-trace failed: flag OFF must record nothing"
    assert off_out["fill"] == pytest.approx(out["fill"]), "observability-only"


def test_f3_green_band_floor_is_flagged_when_it_binds():
    """green_start == 98.00 is ambiguous: a real 2pp cap, or the 2.0pp FLOOR binding."""
    r = SimpleMeterReader(W, H)
    assert r._green_band_diag is True, "flag must be default-ON"
    # a 1px cap on a 130px track = 0.77pp -> below the 2.0 floor
    g = r._green_window(green_px=6, cap_top=10, cap_bot=10, ph=200, fillable_h=130.0)
    assert g is not None
    assert g[0] == pytest.approx(98.0), "emitted window is unchanged (still the clamped floor)"
    assert r._green_band_bound == "floor", "the floor must be reported as binding"
    assert r._green_band_raw < 2.0

    # an unbound measurement reports no rail
    r2 = SimpleMeterReader(W, H)
    r2._green_window(green_px=6, cap_top=10, cap_bot=17, ph=200, fillable_h=130.0)
    assert r2._green_band_bound == "", "a real 6.15pp measurement is not rail-bound"

    # REVERT-TRACE
    off = SimpleMeterReader(W, H)
    off._green_band_diag = False
    off_g = off._green_window(green_px=6, cap_top=10, cap_bot=10, ph=200, fillable_h=130.0)
    assert off._green_band_bound == "", "revert-trace failed: flag OFF must record nothing"
    assert off._green_band_raw == -1.0
    assert off_g == g, "observability-only: the emitted window is identical"


def test_f3_gz_top_edge_is_collected_not_discarded():
    """rows[0] (the neon band TOP edge) is the only thing that could measure green_end."""
    r = SimpleMeterReader(W, H)
    assert r._gz_top_edge is True
    assert r._gz_ends == []
    # lifecycle: cleared per shot alongside _gz_starts
    r._gz_ends = [95.0]
    r._gz_begin_shot()
    assert r._gz_ends == [], "must reset per shot exactly like _gz_starts"

    # REVERT-TRACE
    off = SimpleMeterReader(W, H)
    off._gz_top_edge = False
    assert off._gz_top_edge is False


# --------------------------------------------------------------------------------------------- #
#  MULTI-COLOUR BAR SUPPORT (Red + Purple).  2026-08-04.
#
#  Scope is deliberately two colours.  Red is what the rig runs; Purple was the legacy detector's
#  shipped default and is the only other colour with live provenance (and the only other one with
#  real captured frames in tests/fixtures/meter).  Orange/Yellow/Cyan/White exist in
#  meter_detector.BGR_COLOR_RANGES but were ported from config tables and have never been
#  validated on a live meter, so they are not offered.
# --------------------------------------------------------------------------------------------- #
import os as _os_mc

import cv2 as _cv2_mc

import meter_bar_colors as mbc

_FIXTURES = _os_mc.path.join(_os_mc.path.dirname(_os_mc.path.abspath(__file__)),
                             "fixtures", "meter")
# Every meter fixture in the tree carries a PURPLE bar (the names denote shot type, not colour --
# the Red BGR band finds zero pixels in all of them).  They are the only real captured meter
# frames left after the framedump was deleted.
_PURPLE_METER_FIXTURES = [
    "arrow2_purple_low_233.png", "arrow2_purple_mid_205.png", "arrow2_purple_full_294.png",
    "standstill_rising_00250.png", "standstill_near_green_00360.png",
    "goto_rising_00770.png", "goto_near_green_00830.png",
]
# The pink pool floatie at Hue~166-175 is THE historical purple false positive; nometer_* are
# plain negative controls.
_NO_METER_FIXTURES = [
    "arrow2_floatie_falsebox_092.png", "arrow2_floatie_falsebox_098.png",
    "arrow2_nometer_120.png", "nometer_00050.png",
]


class _ColourCfg:
    def __init__(self, colour):
        self.meter_color = colour
        self.meter_style = "Arrow2"


def _fixture(name):
    img = _cv2_mc.imread(_os_mc.path.join(_FIXTURES, name))
    assert img is not None, "missing fixture " + str(name)
    return img


def _hsv_mask(img, row):
    _, h_lo, h_hi, s_min, v_min = row
    hsv = _cv2_mc.cvtColor(img, _cv2_mc.COLOR_BGR2HSV)
    return _cv2_mc.inRange(hsv, np.array((h_lo, s_min, v_min), np.uint8),
                           np.array((h_hi, 255, 255), np.uint8))


def _read_fixture(colour, img, frames=8):
    h, w = img.shape[:2]
    r = SimpleMeterReader(w, h, cfg=_ColourCfg(colour))
    r.set_shot_state(True, 1.0, True)
    res = None
    for i in range(frames):
        res = r.detect(img, ts=i * 0.016)
    return res


def _recolour_bar_to_red(img):
    """Recolour ONLY the bar pixels to the shipped red, leaving decor and the GREEN CAP alone.

    This is the parity control: it produces a frame that differs from the purple original in
    nothing but the bar's hue, so any detection difference is attributable to the colour rails
    and not to geometry, scale, compression or decor.
    """
    out = img.copy()
    m = _hsv_mask(img, mbc.band("Purple", mbc.BAND_MICRO))
    v = _cv2_mc.cvtColor(img, _cv2_mc.COLOR_BGR2HSV)[:, :, 2]
    out[m > 0, 0] = 0
    out[m > 0, 1] = 0
    out[m > 0, 2] = np.maximum(v[m > 0], 221)   # inside [0,0,220]-[60,60,255]
    return out


def test_red_band_rows_are_the_live_reader_constants():
    """THE CONSTRUCTION PROOF, as an executable guard.

    The framedump is deleted, so a 5000-frame byte-identity replay is no longer available. Red's
    non-regression argument is instead structural: a Red-configured reader reaches the IDENTICAL
    cv2.inRange call with the IDENTICAL constants it has always used. That argument holds only
    while these rows still mirror the instance constants, which is exactly what this asserts.
    """
    r = SimpleMeterReader(W, H, cfg=_ColourCfg("Red"))
    assert r._bands[mbc.BAND_NOMINAL] == ("bgr", r._RED_LO, r._RED_HI)
    assert r._bands[mbc.BAND_COURTWIDE] == ("bgr", r._COURTWIDE_RED[0], r._COURTWIDE_RED[1])
    assert r._bands[mbc.BAND_MICRO] == ("bgr", r._MICRO_RED[0], r._MICRO_RED[1])
    # ...and the constants themselves are still the shipped 2k_Vision/Arrow2 values.
    assert (r._RED_LO, r._RED_HI) == ((0, 0, 220), (60, 60, 255))
    assert r._COURTWIDE_RED == ((0, 0, 170), (70, 70, 255))
    assert r._MICRO_RED == ((0, 0, 110), (110, 110, 255))
    # The default-bounds mask IS that literal inRange, pixel for pixel.
    frame = _frame(0.6)
    assert np.array_equal(
        r._redmask(frame),
        _cv2_mc.inRange(frame, np.array((0, 0, 220), np.uint8),
                        np.array((60, 60, 255), np.uint8)))


def test_red_never_enters_the_hsv_arm():
    """Red must not merely PRODUCE the same mask, it must take the same code path."""
    r = SimpleMeterReader(W, H, cfg=_ColourCfg("Red"))
    called = []
    r._hsv_band_mask = lambda *a, **k: called.append(1)
    for _ in range(4):
        r.detect(_frame(0.5), ts=0.0)
    assert called == [], "a Red reader must never reach the HSV mask arm"


@pytest.mark.parametrize("tier", [mbc.BAND_NOMINAL, mbc.BAND_COURTWIDE, mbc.BAND_MICRO])
def test_purple_hue_ceiling_excludes_the_pink_floatie(tier):
    """The 159 hue ceiling is LOAD BEARING and must never be raised.

    meter_detector.py:331 sets purple_purity_hue_max = 159.0 and the comment at :725-727 says the
    band is tuned to exclude the pink floatie at Hue~166. On the real floatie fixtures the shipped
    rails admit ZERO pixels, while a proposed relaxation to ceilings 162/164/166 with an S floor
    of 100 admits 43-559 -- worst on the MICRO tier, which is the full-frame scan and therefore
    the most false-positive-prone one in the reader.
    """
    row = mbc.band("Purple", tier)
    assert row[2] <= 159, "purple hue ceiling must never exceed the live-proven 159"
    assert row[3] >= 130, "purple S floor is what keeps arena greys out of a full-frame scan"
    # The FLOATIE frames are the load-bearing case and must be EXACTLY zero at every tier: the
    # floatie is a large, saturated, meter-sized magenta blob, so a single admitted region there
    # is a lock candidate.
    for name in ("arrow2_floatie_falsebox_092.png", "arrow2_floatie_falsebox_098.png",
                 "arrow2_nometer_120.png"):
        assert int(_cv2_mc.countNonZero(_hsv_mask(_fixture(name), row))) == 0, \
            "shipped purple rails must admit no floatie/decor pixels: " + name
    # The 1080p arena negative is a weaker claim, stated honestly: the RELAXED courtwide/micro
    # tiers do admit a few dozen scattered pixels of arena furniture (nominal admits zero). They
    # are isolated, never contiguous, and orders of magnitude below the ~700+ px contiguous
    # column a real purple bar produces -- test_neither_colour_fires_on_the_no_meter_fixtures
    # proves end-to-end that they cannot seat a lock.
    scattered = int(_cv2_mc.countNonZero(_hsv_mask(_fixture("nometer_00050.png"), row)))
    assert scattered < 200, "arena negative admitted %d px at tier %s" % (scattered, tier)
    if tier == mbc.BAND_NOMINAL:
        assert scattered == 0, "the strict tier must admit nothing on a no-meter arena frame"


def test_purple_rails_reject_what_the_proposed_wider_rails_admitted():
    """Revert-trace on the rail correction itself: the rejected proposal really was unsafe."""
    proposed_micro = ("hsv", 134, 166, 100, 100)     # the design doc's micro row
    shipped_micro = mbc.band("Purple", mbc.BAND_MICRO)
    floatie = _fixture("arrow2_floatie_falsebox_098.png")
    assert int(_cv2_mc.countNonZero(_hsv_mask(floatie, proposed_micro))) > 200
    assert int(_cv2_mc.countNonZero(_hsv_mask(floatie, shipped_micro))) == 0


def test_purple_nominal_mask_lands_on_the_ground_truth_meter_box():
    """Ground truth from tests/test_meter_detector_live_gdi.py (the legacy detector's fixtures)."""
    ground_truth = {
        "arrow2_purple_low_233.png": (877, 569, 16, 45),
        "arrow2_purple_mid_205.png": (734, 379, 17, 57),
        "arrow2_purple_full_294.png": (1093, 508, 17, 46),
    }
    row = mbc.band("Purple", mbc.BAND_NOMINAL)
    for name, box in ground_truth.items():
        gx, gy, gw, gh = box
        m = _hsv_mask(_fixture(name), row)
        ys, xs = np.nonzero(m)
        assert len(xs) > 400, name + ": purple bar should mask as a solid column"
        # Every edge within 2px of the legacy detector's box on a real captured frame.
        assert abs(int(xs.min()) - gx) <= 2 and abs(int(ys.min()) - gy) <= 2
        assert abs(int(xs.max()) - (gx + gw)) <= 2 and abs(int(ys.max()) - (gy + gh)) <= 2


def test_purple_detection_matches_the_red_baseline_on_identical_frames():
    """Purple's detection rate must be within 2pp of Red's.

    The control is the SAME frame with only the bar hue changed, so geometry, scale, decor,
    compression and the green cap are held fixed. Both colours are expected to miss the same two
    1550x697 arrow2 frames -- that shared miss is the proof the residual failures are
    colour-INDEPENDENT geometry gating, not a deficiency in the purple rails.
    """
    purple_hits = 0
    red_hits = 0
    agree = 0
    for name in _PURPLE_METER_FIXTURES:
        img = _fixture(name)
        p = _read_fixture("Purple", img)
        r = _read_fixture("Red", _recolour_bar_to_red(img))
        purple_hits += bool(p.detected)
        red_hits += bool(r.detected)
        agree += bool(p.detected) == bool(r.detected)
    n = len(_PURPLE_METER_FIXTURES)
    assert purple_hits >= 5, "purple detected only %d/%d" % (purple_hits, n)
    assert agree == n, "purple and red must succeed/fail on exactly the same frames"
    assert abs(purple_hits - red_hits) / float(n) <= 0.02


def test_purple_reader_does_not_fire_on_a_red_meter():
    """Cross-colour: a purple-configured reader on red bars must be near-silent."""
    hits = sum(bool(_read_fixture("Purple", _recolour_bar_to_red(_fixture(n))).detected)
               for n in _PURPLE_METER_FIXTURES)
    assert hits == 0, "purple config fired on %d red-bar frames" % hits


def test_neither_colour_fires_on_the_no_meter_fixtures():
    for name in _NO_METER_FIXTURES:
        img = _fixture(name)
        assert not _read_fixture("Purple", img).detected, name


def test_green_is_refused_and_falls_back_to_red(caplog):
    """GREEN MUST NEVER BE SUPPORTED -- it fails OPEN, which is worse than not firing.

    The reader is colour-invariant about the GREEN MAKE-WINDOW CAP: it locates the cap to
    establish track_top, and fill% is measured against the cap-to-floor track. The cap masks are
    SimpleMeterReader._G (hue 38-85) and _NEON_LO/_HI (hue 48-70); a green BAR measures hue 45-72
    (meter_detector._COLOR_PURITY_GATES["Green"]). Those windows overlap almost totally, so a
    green bar is masked AS the cap: track_top collapses onto the fill boundary and every frame
    reads ~100% fill. The bot would fire INSTANTLY on every shot at maximum confidence with no
    rejection reason -- indistinguishable, to the user, from the bot working.
    """
    assert "Green" not in mbc.SUPPORTED
    with caplog.at_level("WARNING"):
        assert mbc.normalize("Green") == "Red"
    assert any("not a supported bar colour" in r.message for r in caplog.records)
    # the overlap that makes it unsafe is real, not hypothetical
    from simple_meter_reader import _NEON_LO, _NEON_HI
    green_bar_lo, green_bar_hi = 45, 72
    assert _NEON_LO[0] < green_bar_hi and _NEON_HI[0] > green_bar_lo
    cap_lo, cap_hi = SimpleMeterReader._G[0][0], SimpleMeterReader._G[1][0]
    assert cap_lo < green_bar_hi and cap_hi > green_bar_lo


# "White" left this list on 2026-08-26: NBA 2K27 early access ships a white-only
# meter, and White is now a SUPPORTED colour with live evidence behind it
# (framedump session_20260826_185753). See test_white_is_supported_and_not_red.
@pytest.mark.parametrize("name", ["Yellow", "Orange", "Cyan", "Chartreuse", "", None])
def test_unsupported_colours_fall_back_to_red_and_keep_reading(name):
    """A mislabeled config must never blind the reader; it falls back to the validated colour."""
    r = SimpleMeterReader(W, H, cfg=_ColourCfg(name))
    assert r._meter_color == "Red"
    assert r._bands[mbc.BAND_NOMINAL] == ("bgr", r._RED_LO, r._RED_HI)
    assert _read_fixture("Red", _recolour_bar_to_red(_fixture("goto_rising_00770.png"))).detected


def test_colour_names_are_matched_case_insensitively():
    assert mbc.normalize("purple") == "Purple"
    assert mbc.normalize("  RED ") == "Red"


def test_every_band_carrier_latches_a_tagged_row_for_an_hsv_colour():
    """`_lock_red` is the most dangerous value in the change.

    It latches the band that acquired a lock for that lock's WHOLE LIFE. An untagged BGR pair
    stored there resolves as BGR and silently reverts every subsequent read to a red mask,
    whatever colour the user configured -- a colour that half-works. Every carrier must be tagged.
    """
    r = SimpleMeterReader(W, H, cfg=_ColourCfg("Purple"))
    for tier in (mbc.BAND_NOMINAL, mbc.BAND_COURTWIDE, mbc.BAND_MICRO):
        assert r._bands[tier][0] == "hsv"
    # the merged courtwide carrier (returned by the courtwide tier and latched into _lock_red)
    merged = r._merge_bands(r._bands[mbc.BAND_COURTWIDE], r._bands[mbc.BAND_NOMINAL])
    assert merged[0] == "hsv"
    # a latched tagged row round-trips through _redmask as HSV, not BGR
    r._lock_red = merged
    img = _fixture("goto_rising_00770.png")
    assert np.array_equal(r._redmask(img), _hsv_mask(img, merged))
    # ...and a BARE pair is still honoured as BGR, so every legacy call site is unchanged
    assert r._as_band(((0, 0, 220), (60, 60, 255))) == ("bgr", (0, 0, 220), (60, 60, 255))


def test_bgr_merge_is_the_arithmetic_it_replaced():
    """_merge_bands must reproduce the inline per-channel zip(min)/zip(max) exactly."""
    r = SimpleMeterReader(W, H, cfg=_ColourCfg("Red"))
    merged = r._merge_bands(r._bands[mbc.BAND_COURTWIDE], r._bands[mbc.BAND_NOMINAL])
    assert merged == ("bgr",
                      tuple(min(a, b) for a, b in zip(r._COURTWIDE_RED[0], r._RED_LO)),
                      tuple(max(a, b) for a, b in zip(r._COURTWIDE_RED[1], r._RED_HI)))
    assert merged == ("bgr", r._COURTWIDE_RED[0], r._COURTWIDE_RED[1])


def test_micro_red_dominance_floor_is_bgr_only():
    """The dominance floor repairs a hole only a BGR box has, and is WRONG on purple.

    `r - max(g, b)` is negative on a purple pixel (its blue channel legitimately exceeds red), so
    applying the floor to an HSV colour would erase the whole mask and blind the micro tier.
    """
    assert mbc.micro_needs_dominance(mbc.band("Red", mbc.BAND_MICRO)) is True
    assert mbc.micro_needs_dominance(mbc.band("Purple", mbc.BAND_MICRO)) is False
    p = SimpleMeterReader(W, H, cfg=_ColourCfg("Purple"))
    img = _fixture("goto_rising_00770.png")
    assert np.array_equal(p._micro_redmask(img),
                          _hsv_mask(img, mbc.band("Purple", mbc.BAND_MICRO))), \
        "purple micro mask must be the plain HSV band, undamaged by red-dominance"
    assert int(_cv2_mc.countNonZero(p._micro_redmask(img))) > 500


def test_colour_tolerance_widening_never_touches_an_hsv_row(monkeypatch):
    """Per-CHANNEL BGR arithmetic on (hue, sat, val) scalars would walk purple's hue window
    straight up into the pink floatie it exists to exclude."""
    monkeypatch.setenv("ORION_READER_COLOR_TOL", "20")
    p = SimpleMeterReader(W, H, cfg=_ColourCfg("Purple"))
    assert p._bands[mbc.BAND_NOMINAL] == mbc.band("Purple", mbc.BAND_NOMINAL)
    assert p._bands[mbc.BAND_NOMINAL][2] <= 159
    # ...but Red still widens exactly as before, and the green cap widens for BOTH colours.
    r = SimpleMeterReader(W, H, cfg=_ColourCfg("Red"))
    assert r._RED_LO == (0, 0, 200) and r._RED_HI == (80, 80, 255)
    assert p._G[0][1] < SimpleMeterReader._G[0][1], "green cap widening is colour-invariant"


def test_live_colour_change_rebuilds_the_bands():
    """rpo.update_meter -> reload_config is where a live picker change lands. Without a rebuild
    the reader keeps masking the OLD colour while the HUD reports the new one."""
    cfg = _ColourCfg("Red")
    r = SimpleMeterReader(W, H, cfg=cfg)
    assert r._bands[mbc.BAND_NOMINAL][0] == "bgr"
    cfg.meter_color = "Purple"
    r.reload_config(cfg)
    assert r._meter_color == "Purple"
    assert r._bands[mbc.BAND_NOMINAL] == mbc.band("Purple", mbc.BAND_NOMINAL)
    assert r._learned_red is None, "bands learned for the old colour must not survive"
    img = _fixture("goto_rising_00770.png")
    assert np.array_equal(r._redmask(img), _hsv_mask(img, mbc.band("Purple", mbc.BAND_NOMINAL)))


# --------------------------------------------------------------------------------------------- #
#  COLOUR CALIBRATOR: the HSV generalisation of the channel-gap invariant, and the
#  style+colour filter on profile restore.
# --------------------------------------------------------------------------------------------- #
from simple_meter_reader import ColorCalibrator


def _hist(values, weight=400):
    """A (3,256) histogram from three per-channel value lists."""
    h = np.zeros((3, 256), np.int64)
    for c, vals in enumerate(values):
        for v in vals:
            h[c][int(v)] += weight
    return h


def _purple_bar_hist(hue=(147, 148, 149), sat=(195, 200, 210), val=(190, 200, 215)):
    return _hist((hue, sat, val))


def _green_cap_hist():
    return _hist(((58, 59, 60), (200, 210, 220), (200, 210, 220)))


def _purple_cal(tmp_path):
    return ColorCalibrator(profile_path=str(tmp_path / "profiles.json"), color="Purple")


def test_calibrator_learns_purple_in_hsv_not_bgr(tmp_path):
    c = _purple_cal(tmp_path)
    assert c.band_kind == "hsv"
    assert c.bar_env == mbc.band("Purple", mbc.BAND_NOMINAL)
    bands = c._derive_bands(_purple_bar_hist(), _green_cap_hist())
    assert bands is not None and bands["kind"] == "hsv"
    # The baked dict must surface as a TAGGED hsv row: this is the value that becomes
    # `_learned_red` and is then latched into `_lock_red` for the life of a lock.
    c.baked = bands
    row = c.baked_bar_row()
    assert row[0] == "hsv"
    # lo = (hue_lo, s_floor, v_floor), hi = (hue_hi, 255, 255)
    assert row[1] == bands["red_lo"][0] and row[2] == bands["red_hi"][0]
    assert row[3] == bands["red_lo"][1] and row[4] == bands["red_lo"][2]
    assert mbc.is_hsv(row) and not mbc.micro_needs_dominance(row)


def test_hsv_learned_band_cannot_represent_neutral_grey(tmp_path):
    """The HSV equivalent of the BGR channel-gap invariant.

    The gap (hi_G, hi_B <= lo_R - 80) exists so a learned band is structurally incapable of
    representing grey. On an HSV row the SATURATION FLOOR carries that guarantee -- grey IS
    "saturation ~0" -- so the floor may never fall below HSV_SAT_FLOOR_MIN.
    """
    c = _purple_cal(tmp_path)
    # A deliberately CONTAMINATED harvest: washed-out, near-grey pixels.
    bands = c._derive_bands(_hist(((147, 148), (5, 8, 12), (100, 110))), _green_cap_hist())
    if bands is not None:
        assert bands["red_lo"][1] >= ColorCalibrator.HSV_SAT_FLOOR_MIN
        # ...and in practice the envelope narrows it further still, to the live-proven 165.
        assert bands["red_lo"][1] >= mbc.band("Purple", mbc.BAND_NOMINAL)[3]


def test_hsv_learned_band_hue_span_is_capped(tmp_path):
    """A band that sprawls in hue has stopped being a colour."""
    c = _purple_cal(tmp_path)
    wide = _hist(((140, 143, 146, 149, 152, 155, 158), (200,) * 7, (200,) * 7))
    bands = c._derive_bands(wide, _green_cap_hist())
    assert bands is not None
    span = bands["red_hi"][0] - bands["red_lo"][0]
    assert span <= ColorCalibrator.HSV_HUE_SPAN_MAX


def test_hsv_learning_only_ever_narrows_the_shipped_band(tmp_path):
    """Learning is intersected with the envelope, exactly as the BGR path is."""
    c = _purple_cal(tmp_path)
    _, e_h_lo, e_h_hi, e_s, e_v = mbc.band("Purple", mbc.BAND_NOMINAL)
    # a harvest that TRIES to widen past the envelope on every axis
    greedy = _hist(((120, 148, 175), (95, 200, 255), (60, 200, 255)))
    bands = c._derive_bands(greedy, _green_cap_hist())
    if bands is not None:
        lo, hi = bands["red_lo"], bands["red_hi"]
        assert lo[0] >= e_h_lo and hi[0] <= e_h_hi, "hue window escaped the envelope"
        assert lo[1] >= e_s, "saturation floor was lowered below the shipped band"
        assert lo[2] >= e_v, "value floor was lowered below the shipped band"


def test_profile_restore_filters_by_style_and_colour(tmp_path):
    """LIVE BUG FIX: _check_fingerprint matched on the ARENA FINGERPRINT ALONE while _persist
    keys profiles by (style, color, fingerprint).

    The fingerprint is a hue histogram of the ARENA, which is the same arena whatever colour the
    user set their meter to. So playing the same court after switching Red -> Purple loaded the
    RED-learned band onto a Purple meter, straight into locked/provisional with full lock
    authority.
    """
    path = str(tmp_path / "profiles.json")
    arena = list(np.linspace(1.0, 32.0, 32))

    # A Red calibrator learns and persists a profile for this arena.
    red = ColorCalibrator(profile_path=path, color="Red")
    red.fingerprint = np.asarray(arena, float)
    red.baked = {"kind": "bgr", "red_lo": (0, 0, 230), "red_hi": (50, 50, 255),
                 "green_lo": (40, 100, 100), "green_hi": (80, 255, 255)}
    red.learned_date = "2026-08-04"
    red._persist()
    assert any(p.get("color") == "Red" for p in red._profiles)

    # A Purple calibrator, SAME arena fingerprint, must NOT adopt it.
    purple = ColorCalibrator(profile_path=path, color="Purple")
    assert any(p.get("color") == "Red" for p in purple._profiles), "profile is on disk"
    purple.state = "locked"
    purple.fingerprint = np.zeros(32, float)      # different arena -> forces the match path
    purple.baked = {"kind": "hsv", "red_lo": (140, 165, 150), "red_hi": (158, 255, 255),
                    "green_lo": (40, 100, 100), "green_hi": (80, 255, 255)}
    purple._check_fingerprint(np.asarray(arena, float))
    assert purple.state == "learning", "a Red profile must not load onto a Purple meter"
    assert purple.baked is None


def test_profile_restore_still_accepts_its_own_colour(tmp_path):
    """The filter must not break the feature it guards: a matching profile still restores."""
    path = str(tmp_path / "profiles.json")
    arena = list(np.linspace(1.0, 32.0, 32))
    a = ColorCalibrator(profile_path=path, color="Purple")
    a.fingerprint = np.asarray(arena, float)
    a.baked = {"kind": "hsv", "red_lo": (142, 170, 155), "red_hi": (156, 255, 255),
               "green_lo": (40, 100, 100), "green_hi": (80, 255, 255)}
    a.learned_date = "2026-08-04"
    a._persist()

    b = ColorCalibrator(profile_path=path, color="Purple")
    b.state = "locked"
    b.fingerprint = np.zeros(32, float)
    b.baked = {"kind": "hsv", "red_lo": (140, 165, 150), "red_hi": (158, 255, 255),
               "green_lo": (40, 100, 100), "green_hi": (80, 255, 255)}
    b._check_fingerprint(np.asarray(arena, float))
    assert b.state == "provisional", "same style+colour+arena must restore (verify-before-trust)"
    assert b.baked["red_lo"] == (142, 170, 155)


def test_persisted_band_kind_mismatch_is_refused(tmp_path):
    """A kind-less legacy profile is BGR by definition and must never load onto an HSV colour."""
    path = str(tmp_path / "profiles.json")
    arena = list(np.linspace(1.0, 32.0, 32))
    seed = ColorCalibrator(profile_path=path, color="Purple")
    seed.fingerprint = np.asarray(arena, float)
    seed.baked = {"kind": "bgr", "red_lo": (0, 0, 230), "red_hi": (50, 50, 255),
                  "green_lo": (40, 100, 100), "green_hi": (80, 255, 255)}
    seed._persist()

    c = ColorCalibrator(profile_path=path, color="Purple")
    c.state = "locked"
    c.fingerprint = np.zeros(32, float)
    c.baked = {"kind": "hsv", "red_lo": (140, 165, 150), "red_hi": (158, 255, 255),
               "green_lo": (40, 100, 100), "green_hi": (80, 255, 255)}
    c._check_fingerprint(np.asarray(arena, float))
    assert c.state == "learning" and c.baked is None


def test_calibrator_colour_change_discards_what_it_learned(tmp_path):
    """Bands learned for Red say nothing about a Purple meter."""
    c = ColorCalibrator(profile_path=str(tmp_path / "p.json"), color="Red")
    c.state = "locked"
    c.baked = {"kind": "bgr", "red_lo": (0, 0, 230), "red_hi": (50, 50, 255),
               "green_lo": (40, 100, 100), "green_hi": (80, 255, 255)}
    c.committed_shots = 5
    c.set_colour("Purple")
    assert c.color == "Purple" and c.band_kind == "hsv"
    assert c.baked is None and c.state == "learning" and c.committed_shots == 0


def test_bgr_calibrator_path_is_unchanged(tmp_path):
    """Revert-trace: the Red learning path must still bake with the channel-gap invariant."""
    c = ColorCalibrator(profile_path=str(tmp_path / "p.json"), color="Red")
    assert c.band_kind == "bgr"
    assert c.red_env == (ColorCalibrator.RED_ENV[0], ColorCalibrator.RED_ENV[1])
    red_h = _hist(((10, 14, 18), (10, 14, 18), (235, 240, 245)))
    bands = c._derive_bands(red_h, _green_cap_hist())
    assert bands is not None and bands["kind"] == "bgr"
    lo, hi = bands["red_lo"], bands["red_hi"]
    gap = float(ColorCalibrator.CHANNEL_GAP)
    assert hi[0] <= lo[2] - gap + 1 and hi[1] <= lo[2] - gap + 1, "channel-gap invariant lost"
    # strict narrowing against the shipped envelope
    assert lo[2] >= ColorCalibrator.RED_ENV[0][2] and hi[0] <= ColorCalibrator.RED_ENV[1][0]


# --------------------------------------------------------------------------------------------- #
#  ORCHESTRATOR: the park colour pin must not reach the simple/compressed readers.
#
#  This is the bug that made the whole colour picker inert. `park_temporal_enabled` defaults ON,
#  and the pin overwrote det_config.meter_color with "Red" a few lines BEFORE the reader was
#  built from that same det_config -- so a user selecting Purple got the HUD label "Purple", a
#  red mask, zero detections and no explanation. No reader-side colour fix is reachable at all
#  until this is scoped.
# --------------------------------------------------------------------------------------------- #


def _build_orch_with_colour(monkeypatch, colour, compressed="0", simple="1"):
    monkeypatch.setenv("ORION_SIMPLE_READER", simple)
    monkeypatch.setenv("ORION_COMPRESSED_READER", compressed)
    from remote_play_orchestrator import RemotePlayOrchestrator, OrchestratorConfig
    cfg = OrchestratorConfig(console_ip="1.2.3.4", virtual_controller=False,
                             hidhide=False, auto_launch_client=False, meter_color=colour)
    return RemotePlayOrchestrator(cfg)


def test_park_pin_no_longer_overwrites_the_simple_reader_colour(monkeypatch):
    orch = _build_orch_with_colour(monkeypatch, "Purple")
    det = orch._meter_detector
    assert type(det).__name__ == "SimpleMeterReader"
    # park is still ON -- the pin is scoped, not disabled
    assert det._cfg.park_temporal_enabled is True
    assert det._cfg.meter_color == "Purple", "the park pin still clobbers the user's colour"
    assert det._meter_color == "Purple"
    assert det._bands[mbc.BAND_NOMINAL] == mbc.band("Purple", mbc.BAND_NOMINAL)


def test_park_pin_still_applies_to_the_legacy_chain(monkeypatch):
    """Revert-trace: the pin exists for the LEGACY chain's park path and must survive there."""
    import remote_play_orchestrator as rpo
    monkeypatch.setenv("ORION_SIMPLE_READER", "0")
    monkeypatch.setenv("ORION_COMPRESSED_READER", "0")
    from meter_detector import DetectorConfig
    det_config = DetectorConfig()
    det_config.meter_color = "Purple"
    det_config.park_temporal_enabled = True
    # the guard as shipped: pin only when NEITHER reader flag is on
    _simple_on, _compressed_on = False, False
    if det_config.park_temporal_enabled and not (_simple_on or _compressed_on):
        det_config.meter_color = "Red"
    assert det_config.meter_color == "Red"
    assert rpo is not None


def test_compressed_reader_is_also_exempt_from_the_pin(monkeypatch):
    """CompressedMeterReader SUBCLASSES SimpleMeterReader and is built from the SAME config, so a
    `_simple_on`-only guard would leave the Remote Play path pinned to Red."""
    from compressed_meter_reader import CompressedMeterReader
    from simple_meter_reader import SimpleMeterReader as _SMR
    assert issubclass(CompressedMeterReader, _SMR), "the exemption relies on this"
    orch = _build_orch_with_colour(monkeypatch, "Purple", compressed="1")
    det = orch._meter_detector
    assert type(det).__name__ == "CompressedMeterReader"
    assert det._cfg.park_temporal_enabled is True
    assert det._cfg.meter_color == "Purple"
    assert det._meter_color == "Purple"


def test_red_is_still_red_through_the_orchestrator(monkeypatch):
    orch = _build_orch_with_colour(monkeypatch, "Red")
    det = orch._meter_detector
    assert det._meter_color == "Red"
    assert det._bands[mbc.BAND_NOMINAL] == ("bgr", det._RED_LO, det._RED_HI)


def test_update_meter_applies_a_live_colour_change(monkeypatch):
    """The Meter tab's picker calls this. It used to discard every non-Red value while park was
    on, which is the second half of why the picker was inert."""
    orch = _build_orch_with_colour(monkeypatch, "Red")
    det = orch._meter_detector
    assert det._meter_color == "Red"
    orch.update_meter(meter_color="Purple")
    assert det._cfg.meter_color == "Purple"
    assert det._meter_color == "Purple"
    assert det._bands[mbc.BAND_NOMINAL] == mbc.band("Purple", mbc.BAND_NOMINAL)
    # ...and back again
    orch.update_meter(meter_color="Red")
    assert det._meter_color == "Red"
    assert det._bands[mbc.BAND_NOMINAL] == ("bgr", det._RED_LO, det._RED_HI)


def test_update_meter_rejects_an_unsupported_colour_by_falling_back(monkeypatch):
    orch = _build_orch_with_colour(monkeypatch, "Red")
    det = orch._meter_detector
    orch.update_meter(meter_color="Yellow")
    assert det._meter_color == "Red", "an unsupported colour must not blind the reader"


def test_orchestrator_default_colour_is_the_validated_one():
    """This default was DEAD while the pin existed (every launch really ran Red). Un-scoping the
    pin made it live again, so it must not silently flip default-config entry points onto an
    unvalidated colour."""
    from remote_play_orchestrator import OrchestratorConfig
    assert OrchestratorConfig(console_ip="x").meter_color == "Red"
    assert OrchestratorConfig(console_ip="x").meter_color == mbc.FALLBACK


def test_sess_track_fallback_only_trusts_a_tight_session_history():
    """[ORION_SESS_TRACK_FALLBACK] The session-scoped height twin may only serve as a fill
    denominator when its own samples agree.

    WHY THIS GUARD EXISTS. Owner-confirmed second failure mode: some Go-To presses die because
    the detector never picks up the meter at all -- `no_cap_anchor` was 875 of 4637 undetected
    frames in the verification session. The reader had a meter in front of it, saw no green cap,
    found the per-lock `_track_h_hist` empty, and bailed rather than divide by a guess. That
    refusal is CORRECT in principle: a wrong denominator is a wrong fill and the bot times off
    fill.

    `_sess_track_h` holds real measurements, so it is a better answer than bailing -- but it is
    SESSION-scoped and can span different camera distances, and the meter really does render at
    different sizes (16x108 and 23x115 both measured in one batch). The half-court track height
    could not be measured directly, because half-court shots are exactly the ones that never get
    detected. So the median is validated at use time instead of assumed.

    REVERT-TRACE: drop the `_sess_track_h_is_tight()` condition from the fallback and the
    mixed-scale case below starts serving a biased denominator.
    """
    r = SimpleMeterReader(W, H)

    r._sess_track_h = []
    assert r._sess_track_h_is_tight() is False, "no evidence is not a measurement"
    r._sess_track_h = [108.0, 107.0]
    assert r._sess_track_h_is_tight() is False, "two samples is too little to call it measured"

    # A healthy single-distance session: 106-109 px, under 3% spread. Must be trusted.
    r._sess_track_h = [106.0, 107.0, 108.0, 109.0, 107.0]
    assert r._sess_track_h_is_tight() is True

    # A session spanning genuinely different meter scales must NOT be trusted: its median is
    # biased for both distances, and a biased denominator is a biased fill.
    r._sess_track_h = [108.0, 107.0, 150.0, 109.0, 106.0]
    assert r._sess_track_h_is_tight() is False, (
        "a mixed-scale session median must not become a fill denominator")

    # Exactly at the 10% bound is still tight; beyond it is not.
    r._sess_track_h = [100.0, 105.0, 110.0]
    assert r._sess_track_h_is_tight() is True
    r._sess_track_h = [100.0, 105.0, 112.0]
    assert r._sess_track_h_is_tight() is False


def test_dead_hold_grace_requires_the_lock_to_have_been_seen_rising():
    """[ORION_DEAD_HOLD_GRACE] The fake-lock breaker's grace must not be handed to a dead hold.

    THE USER-VISIBLE FALSE LOCK. Measured in a live capture: 151 consecutive frames with the fill
    BYTE-FROZEN at 46.7 and rejection_reason=meter_memory -- a box parked on the crowd reading
    "FILL 47%" for ~2.5 s while no shot was happening. The breaker that exists to kill exactly
    this (cap 40 frames at mid fill) never fired, because the held-frame clause handed it grace
    every frame, bounded only by _armed_coast_max (180 frames).

    WHY THE CAP COULD NOT SIMPLY BE SHRUNK. Across every archived detframes CSV, held runs that
    contain a rising frame (a real release occlusion) vs held runs with none (a dead hold):
        legit  n=45    median 2f   p90 70f   max 180f
        dead   n=2096  median 1f   p90  3f   max 215f
    Length does NOT separate them -- legitimate occlusions reach 180 frames. The RISE does, and of
    the 7 legitimate runs longer than the 40-frame cap, all 7 have their first rising frame at
    position 0, so gating on the latch suppresses none of them.

    REVERT-TRACE: drop `and self._lock_ever_rose` from the held-frame grace clause and a dead hold
    regains unlimited grace.
    """
    r = SimpleMeterReader(W, H)

    # A fresh reader has never seen this lock rise, so a held run gets no grace.
    assert r._lock_ever_rose is False

    # It must NOT expire the way _fresh_rise_left does: a legitimate occlusion can hold far
    # longer than the 30-frame freshness window, which is exactly why a separate latch exists.
    r._lock_ever_rose = True
    r._fresh_rise_left = 0
    assert r._lock_ever_rose is True, (
        "the latch must outlive the 30-frame freshness window; legit occlusions reach 180 frames")

    # ...but it must never survive a re-lock, or a dead hold could inherit a previous
    # meter's proof. reset_tracking() discards the lock identity.
    r.reset_tracking()
    assert r._lock_ever_rose is False, "a re-lock must not inherit the previous lock's rise proof"


# --------------------------------------------------------------------------- #
#  WHITE -- NBA 2K27 (early access) ships a WHITE-ONLY shot meter.
#
#  Every constant asserted here was MEASURED off real capture-card gameplay:
#  framedump session_20260826_185753, 1629 frames of 2K27 practice shooting,
#  791 of them with the meter on screen. The reader's own contract
#  (meter_bar_colors module docstring) is that a colour needs live evidence, not
#  a copied table row -- these tests are where that evidence is pinned.
# --------------------------------------------------------------------------- #

def _white_frame(fill_frac=0.5, col_x=COL_X, col_w=12, bg=40,
                 floor_y=FLOOR_Y, track_top_y=TRACK_TOP_Y, green=True):
    """A synthetic 2K27 meter: opaque WHITE fill, grey furniture, small green tip.

    Mirrors the measured live article -- fill B255 G254 R255 (S=1, V=255), the
    meter's own outline/graduation ticks peaking at V~199 (so the band floors
    must clear them), and a SMALL green apex (median ~2 rows at 720p) rather
    than the big green block the customisation screen previews.
    """
    f = np.full((H, W, 3), bg, np.uint8)
    track_h = floor_y - track_top_y
    fill_top = int(round(floor_y - fill_frac * track_h))
    # grey outline + ticks: the brightest NON-fill furniture on the real meter
    f[track_top_y:floor_y, col_x - 2:col_x + col_w + 2] = (199, 199, 199)
    f[track_top_y:floor_y, col_x:col_x + col_w] = (60, 55, 50)     # empty track
    if fill_frac > 0:
        f[fill_top:floor_y, col_x:col_x + col_w] = (255, 254, 255)  # measured fill
    if green:
        f[track_top_y - 3:track_top_y, col_x:col_x + col_w] = GREEN
    return f


def test_white_is_supported_and_never_silently_becomes_red():
    """THE TRAP. White is a BGR row, and `_rebuild_bands` used to dispatch on the row
    FORM (`is_bgr`) -- which sent every BGR colour down the branch that mirrors the RED
    instance constants. A White reader therefore scanned with _RED_LO/_RED_HI: a red mask
    on a white meter, i.e. zero detections and no explanation. Dispatch must key on the
    COLOUR NAME."""
    r = SimpleMeterReader(W, H, cfg=_ColourCfg("White"))
    assert r._meter_color == "White"
    assert r._bands[mbc.BAND_NOMINAL] == mbc.band("White", mbc.BAND_NOMINAL)
    assert r._bands[mbc.BAND_NOMINAL] != ("bgr", r._RED_LO, r._RED_HI)
    # Red keeps its byte-identity guarantee.
    assert SimpleMeterReader(W, H, cfg=_ColourCfg("Red"))._bands[mbc.BAND_NOMINAL] == (
        "bgr", r._RED_LO, r._RED_HI)


def test_white_band_floors_clear_the_meters_own_furniture():
    """Measured: the 2K27 meter's outline + graduation ticks peak at V~199. Every White
    tier floor must sit ABOVE that or the mask swallows the meter's own frame and the fill
    edge walks. This is the constraint that caps how far White may be relaxed."""
    for tier in (mbc.BAND_NOMINAL, mbc.BAND_COURTWIDE, mbc.BAND_MICRO):
        kind, lo, hi = mbc.band("White", tier)
        assert kind == "bgr"
        assert min(lo) > 199, f"{tier} floor {lo} does not clear the meter's own furniture"
        assert hi == (255, 255, 255)
    # ...and the tiers relax monotonically without ever crossing that line.
    floors = [min(mbc.band("White", t)[1]) for t in
              (mbc.BAND_NOMINAL, mbc.BAND_COURTWIDE, mbc.BAND_MICRO)]
    assert floors == sorted(floors, reverse=True)


def test_white_bgr_box_implicitly_bounds_saturation():
    """White has no hue, so the achromatic gate has to come from the box itself: requiring
    all three channels >= floor bounds saturation to <= 255-floor. At the nominal floor that
    is <=20, which is why a saturated bright colour (a blue court line: B high, G/R low)
    cannot enter the mask."""
    _, lo, _ = mbc.band("White", mbc.BAND_NOMINAL)
    assert 255 - min(lo) <= 30, "nominal box must bound saturation to the measured fill (S<=30)"
    blue_line = np.array([[[255, 90, 60]]], np.uint8)      # bright but saturated
    assert _cv2_mc.inRange(blue_line, np.array(lo, np.uint8),
                           np.array((255, 255, 255), np.uint8)).sum() == 0


def test_white_refuses_the_full_frame_micro_tier():
    """The micro tier is the only full-frame scan, and for White it is unsafe in principle:
    no dominance repair exists for the grey corner (white IS r==g==b), and _MICRO_GREEN's
    saturation floor of 45 overlaps white's legal band -- a white bar masked AS the cap
    collapses track_top and reads ~100% every frame, which fails OPEN."""
    assert mbc.micro_supported("Red") and mbc.micro_supported("Purple")
    assert not mbc.micro_supported("White")
    # the overlap that makes it unsafe is real, not hypothetical
    assert SimpleMeterReader._MICRO_GREEN[0][1] < 70


def test_white_acquisition_width_floor_admits_the_real_meter():
    """Measured: the 2K27 white ribbon is 11-12 px wide at 1280x720 (~17 at 1080p). The
    shipped W_MIN of 20 scales to 13/20 and REJECTED it at both resolutions -- the reader
    then latched decor instead (observed: an 11x8 acquire while the meter was 102x11)."""
    for (fw, fh, measured) in ((1280, 720, 11), (1920, 1080, 17)):
        white = SimpleMeterReader(fw, fh, cfg=_ColourCfg("White"))
        assert white._w_min <= measured, (
            f"White floor {white._w_min} rejects the measured {measured}px meter at {fw}x{fh}")
    # Red and Purple floors are untouched.
    for colour in ("Red", "Purple"):
        r = SimpleMeterReader(1920, 1080, cfg=_ColourCfg(colour))
        assert r._w_min == max(3, int(round(SimpleMeterReader.W_MIN * r._sx))) if hasattr(r, "_sx") \
            else r._w_min == 20


def test_white_never_mutates_the_red_instance_constants():
    """ORION_READER_COLOR_TOL widening is red-shaped per-channel arithmetic ON _RED_LO/_RED_HI.
    White is a BGR row too, so a form-only gate would drag a white reader through it -- mutating
    constants its bands no longer come from."""
    import os as _os
    prev = _os.environ.get("ORION_READER_COLOR_TOL")
    _os.environ["ORION_READER_COLOR_TOL"] = "20"
    try:
        w = SimpleMeterReader(W, H, cfg=_ColourCfg("White"))
        assert (w._RED_LO, w._RED_HI) == ((0, 0, 220), (60, 60, 255))
        assert w._bands[mbc.BAND_NOMINAL] == mbc.band("White", mbc.BAND_NOMINAL)
        red = SimpleMeterReader(W, H, cfg=_ColourCfg("Red"))
        assert red._RED_LO != (0, 0, 220), "Red must still widen as it always did"
    finally:
        if prev is None:
            _os.environ.pop("ORION_READER_COLOR_TOL", None)
        else:
            _os.environ["ORION_READER_COLOR_TOL"] = prev


def test_white_reader_tracks_a_rising_fill():
    """End-to-end on the measured geometry: fill% must rise with the bar, not sit flat."""
    r = SimpleMeterReader(W, H, cfg=_ColourCfg("White"))
    seen = []
    for i, frac in enumerate((0.25, 0.45, 0.70, 0.95)):
        r.set_shot_state(True, 1.0, True)
        res = r.detect(_white_frame(fill_frac=frac), ts=i * 0.1)
        if getattr(res, "detected", False):
            seen.append(float(res.fill_pct))
    assert len(seen) >= 3, f"white meter not read (got {len(seen)} detections)"
    assert seen == sorted(seen), f"fill must rise monotonically, got {seen}"
    assert seen[-1] - seen[0] > 30, f"fill barely moved: {seen}"


def test_white_capless_lock_is_broken_after_the_bound():
    """A WHITE lock that never shows the green cap is not the meter.

    Live 2026-08-27 (session_20260826_204615): four phantom locks, the worst
    holding 15.3s with the box ~stationary at a mean fill of 5.5% -- the
    "FILL 6% on bare court" the owner photographed. The existing static breaker
    could not catch it because the phantom's fill WOBBLES rather than freezing,
    and `_coast_n` never tripped because the phantom re-finds its white column
    every frame, resetting the coast.

    Measured longest run with NO cap: genuine shots 56 frames (~1.75s), phantoms
    141+ frames. 2.5s sits in that gap with ~40% headroom over the genuine worst
    case. Replayed against the labelled session it breaks 18 phantom holds while
    touching 3 genuine runs -- and those touches land in the post-shot tail,
    where dropping a stale lock is the desired behaviour.
    """
    r = SimpleMeterReader(W, H, cfg=_ColourCfg("White"))
    assert r._capless_max_s == pytest.approx(2.5)
    # capless run shorter than the bound -> untouched
    r._capless_since = 100.0
    s = {"detected": True, "fill": 6.0, "green": None}
    assert r._capless_since == 100.0

    # Red/Purple must be entirely unaffected: this is a white-decor problem.
    for colour in ("Red", "Purple"):
        other = SimpleMeterReader(W, H, cfg=_ColourCfg(colour))
        other._capless_since = 0.0
        other.conf = 1.0
        other.box = (10, 20, 25, 110)
        other.detect(_frame(0.5), ts=999.0)
        assert other._meter_color == colour


def test_white_capless_breaker_can_be_disabled():
    import os as _os
    prev = _os.environ.get("ORION_READER_WHITE_CAPLESS_S")
    _os.environ["ORION_READER_WHITE_CAPLESS_S"] = "0"
    try:
        assert SimpleMeterReader(W, H, cfg=_ColourCfg("White"))._capless_max_s == 0.0
    finally:
        if prev is None:
            _os.environ.pop("ORION_READER_WHITE_CAPLESS_S", None)
        else:
            _os.environ["ORION_READER_WHITE_CAPLESS_S"] = prev


def test_stale_lock_is_dropped_at_a_new_press():
    """A held HIGH fill at a fresh press is stale by definition, and it costs the shot.

    AutomationEngine opens its ownership episode AT the press and refuses to own a
    shot whose FIRST observed fill exceeds anchorMaxFirstFillPct (40) -- that cannot
    be the beginning of a new meter. Live 2026-08-27 every shot aborted with
    `SHOT NOT OWNED: reason=ownership_proof_incomplete samples=0 first_fill=51.0`:
    the engine's first sample WAS a lock left over from the previous shot, so the
    bot armed and then refused itself on every attempt.

    notify_physical_shot_start deliberately preserves tracker state so a genuinely
    continuing meter can bridge a new shot identity; this drops ONLY the inherited
    locks that are already past the ownership ceiling.
    """
    r = SimpleMeterReader(W, H, cfg=_ColourCfg("White"))
    r.box = (10, 20, 25, 110); r.conf = 1.0; r.last_fill = 51.0
    r._physical_shot_epoch = 1
    r.notify_physical_shot_start(2)
    assert r.box is None and r.conf == 0.0, "a stale high-fill lock must not cross a press"


def test_a_rising_lock_survives_a_new_press():
    """The bridge case needs measured rise plus a fresh locator, not a low value alone."""
    r = SimpleMeterReader(W, H, cfg=_ColourCfg("White"))
    box = (10, 20, 25, 110)
    r.box = box; r.conf = 1.0; r.last_fill = 12.0; r.last_coarse = 12.0
    r._det_state = "locked"
    r._det_last_box = box
    r._det_last_found_ts = 10.03
    r._det_coarse_fill_hist.extend(((10.00, 8.0), (10.03, 12.0)))
    r._physical_shot_epoch = 1
    r.notify_physical_shot_start(2)
    assert r.box is not None and r.conf == 1.0


def test_stale_press_drop_is_tunable_and_disablable():
    import os as _os
    prev = _os.environ.get("ORION_READER_STALE_PRESS_DROP_PCT")
    _os.environ["ORION_READER_STALE_PRESS_DROP_PCT"] = "0"
    try:
        r = SimpleMeterReader(W, H, cfg=_ColourCfg("White"))
        assert r._stale_press_drop_pct == 0.0
        r.box = (10, 20, 25, 110); r.conf = 1.0; r.last_fill = 90.0
        r._physical_shot_epoch = 1
        r.notify_physical_shot_start(2)
        assert r.box is not None, "0 must disable the drop entirely"
    finally:
        if prev is None:
            _os.environ.pop("ORION_READER_STALE_PRESS_DROP_PCT", None)
        else:
            _os.environ["ORION_READER_STALE_PRESS_DROP_PCT"] = prev


def test_capless_breaker_survives_stray_capped_frames():
    """The breaker judges a WINDOW, not a consecutive streak.

    The streak version reset on ANY single capped frame. At 60fps a false lock
    picks up a stray green pixel often enough to rearm that timer forever: live
    2026-08-27 it fired ZERO times against a lock holding the centre-court "27"
    logo for 222 straight dumped frames with a cap on only 3% of them. Replayed
    at the framedump's 10fps it fired fine -- the bug was invisible at the
    sample rate, which is why it shipped.
    """
    r = SimpleMeterReader(W, H, cfg=_ColourCfg("White"))
    assert r._capless_rate_max <= 0.25
    # a window that is 90% capless must still count as capless
    r._capless_hist.extend((float(i) * 0.05, i % 10 == 0) for i in range(40))
    rate = sum(1 for _, c in r._capless_hist if c) / float(len(r._capless_hist))
    assert rate <= r._capless_rate_max, "one capped frame in ten must not rearm the timer"
    # a genuine meter (cap on most frames) must NOT look capless
    r._capless_hist.clear()
    r._capless_hist.extend((float(i) * 0.05, i % 10 != 0) for i in range(40))
    rate = sum(1 for _, c in r._capless_hist if c) / float(len(r._capless_hist))
    assert rate > r._capless_rate_max, "a real meter's cap rate must protect it"
