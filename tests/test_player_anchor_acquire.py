"""[ORION_ANCHOR_ACQUIRE 2026-09-17] the acquisition fix, and its kill switches.

WHAT THIS GUARDS.  The 09-17 night sessions logged ``anchor=1`` on 2 of 360 pickup lines
while the owner's plate ``[3][PS][trimuzis]`` was plainly on screen, because the search was
built on one session's court and one session's camera distance:

  * the plate's disc runs 11..30 px across the corpus, and the ladder was (0.92, 1.0, 1.08);
  * the 27 x 27 template carries the 09-12 court's blond wood in its corners, which costs
    0.23 of correlation on a dark blue court -- more than the whole margin over PS_MIN;
  * the quarter-resolution coarse pass sees a 0.6-scale disc as 2.8 px and cannot rank it;
  * the icon->box y offset is not a constant, it scales with the plate;
  * and note_meter could only learn an offset when the CURRENT (wrong) patch already
    contained the box, so a wrong prior could never correct itself.

Everything here is synthetic: the plate is the module's own embedded template blitted onto
a court at a chosen SCALE, so a match is exact by construction and the tests measure the
SEARCH.  No framedump, no disk, no network.
"""
from __future__ import annotations

import os

import cv2
import numpy as np
import pytest

import player_anchor as pa

W, H = 1280, 720
COURT_BG = (70, 30, 10)

# every knob this file touches, so one fixture can restore the shipped configuration
ACQ_KNOBS = ("ORION_ANCHOR_CORE_PX", "ORION_ANCHOR_SCALE_WIDE", "ORION_ANCHOR_COARSE_DIV",
             "ORION_ANCHOR_COARSE_TOPK", "ORION_ANCHOR_COARSE_STRIPS",
             "ORION_ANCHOR_PS_ADAPT", "ORION_ANCHOR_PS_FLOOR_MIN",
             "ORION_ANCHOR_DY_SCALED", "ORION_ANCHOR_BOOT_TOL_Y",
             "ORION_ANCHOR_OFFSET_IDENT", "ORION_ANCHOR_SCALE_ROW",
             "ORION_ANCHOR_SCALE_A", "ORION_ANCHOR_SCALE_B")

# the pre-09-17 configuration, in one place: this IS the kill switch set
SHIPPED_09_15 = {
    "ORION_ANCHOR_CORE_PX": "0",
    "ORION_ANCHOR_SCALE_WIDE": "0",
    "ORION_ANCHOR_COARSE_DIV": "4",
    "ORION_ANCHOR_COARSE_TOPK": "3",
    "ORION_ANCHOR_COARSE_STRIPS": "0",
    "ORION_ANCHOR_PS_ADAPT": "0",
    "ORION_ANCHOR_DY_SCALED": "0",
    "ORION_ANCHOR_BOOT_TOL_Y": "70",
    "ORION_ANCHOR_OFFSET_IDENT": "0",
    "ORION_ANCHOR_SCALE_ROW": "0",
}


@pytest.fixture
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("ORION_ANCHOR") or key == "ORION_PLAYER_ANCHOR":
            monkeypatch.delenv(key, raising=False)
    pa.ARM.reset()
    pa.ANCHOR.reset(keep_identity=False)
    yield
    pa.ARM.reset()
    pa.ANCHOR.reset(keep_identity=False)


@pytest.fixture
def shipped_env(clean_env, monkeypatch):
    for k, v in SHIPPED_09_15.items():
        monkeypatch.setenv(k, v)
    yield


def court(bg=COURT_BG) -> np.ndarray:
    f = np.empty((H, W, 3), np.uint8)
    f[:] = bg
    cv2.line(f, (0, 520), (W - 1, 560), (255, 255, 255), 2)
    cv2.ellipse(f, (640, 640), (260, 90), 0, 180, 360, (255, 255, 255), 2)
    return f


def blit_plate(f, icon_cx, icon_cy, scale=1.0, tag="OWNER99"):
    """Blit the module's own PS disc at `scale`, with a gamertag block to its right.

    scale is the plate's apparent size, exactly the number PlayerAnchor reports back.
    """
    t = pa.PlayerAnchor()._template()
    assert t is not None and t.shape == (27, 27)
    n = int(round(27 * scale))
    tt = cv2.resize(t, (n, n), interpolation=cv2.INTER_AREA)
    bgr = cv2.cvtColor(tt.astype(np.uint8), cv2.COLOR_GRAY2BGR)
    x0, y0 = int(icon_cx) - n // 2, int(icon_cy) - n // 2
    f[y0:y0 + n, x0:x0 + n] = bgr
    cv2.putText(f, tag, (x0 + n + 3, int(icon_cy) + int(8 * scale)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6 * scale, (235, 235, 235),
                max(1, int(round(2 * scale))), cv2.LINE_AA)
    return (float(icon_cx), float(icon_cy))


# ------------------------------------------------------------------ 1. the template
def test_the_match_template_is_the_discs_inscribed_square(clean_env):
    a = pa.PlayerAnchor()
    full = a._template()
    core = a._match_tmpl()
    assert full.shape == (27, 27)
    assert core.shape == (19, 19)
    # it is the CENTRE of the shipped template, so every (icon_x, icon_y) is unchanged
    assert np.array_equal(core, full[4:23, 4:23])


def test_core_px_zero_restores_the_shipped_template(clean_env, monkeypatch):
    monkeypatch.setenv("ORION_ANCHOR_CORE_PX", "0")
    a = pa.PlayerAnchor()
    assert a._match_tmpl().shape == (27, 27)


# ------------------------------------------------------------------ 2. the scale range
def test_the_ladder_spans_the_measured_plate_sizes(clean_env):
    a = pa.PlayerAnchor()
    lad = a._scales()
    assert min(lad) <= 0.50 and max(lad) >= 1.15, lad
    # ...and it is NOT narrowed by what the session has seen: the plate's size follows the
    # ROW it is on, so a session median collapses the ladder onto the last few shots.
    a._scale_seen = [0.78] * 40
    a._ps_seen = [0.8] * 40
    assert a._scales() == lad


def test_shipped_ladder_returns_when_the_knob_is_off(clean_env, monkeypatch):
    monkeypatch.setenv("ORION_ANCHOR_SCALE_WIDE", "0")
    assert pa.PlayerAnchor()._scales() == pa.PlayerAnchor.SCALES_ACQ


@pytest.mark.parametrize("scale,cy", [(0.55, 400.0), (0.75, 470.0), (1.0, 600.0)])
def test_a_plate_at_any_measured_size_is_acquired(clean_env, monkeypatch, scale, cy):
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    f = court()
    icx, icy = blit_plate(f, 900.0, cy, scale=scale)
    a = pa.PlayerAnchor().update(f, ts=0.0, armed=True)
    assert a is not None, "plate at scale %.2f not found" % scale
    assert abs(a.icon_x - icx) <= 6 and abs(a.icon_y - icy) <= 6, (a.icon_x, a.icon_y)
    assert abs(a.scale - scale) <= 0.16, (a.scale, scale)


def test_the_shipped_ladder_is_what_missed_the_small_plate(shipped_env, monkeypatch):
    """The regression this fix is for, stated as a test: the pre-09-17 configuration
    cannot find a plate whose disc is 15 px, and the new one can."""
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    f = court()
    icx, icy = blit_plate(f, 900.0, 420.0, scale=0.55)
    old = pa.PlayerAnchor().update(f, ts=0.0, armed=True)
    missed = old is None or abs(old.icon_x - icx) > 20 or abs(old.icon_y - icy) > 20
    assert missed, "the shipped ladder was supposed to miss this plate"
    for k in SHIPPED_09_15:
        monkeypatch.delenv(k, raising=False)
    # A 0.55 plate at y=420 is OFF the row->scale relation the coarse pass keys on -- i.e.
    # an unfamiliar camera -- so it is the THROTTLED second pass that recovers it, one
    # acquisition period later. That is the documented trade: a camera the fit does not
    # describe costs TIME, never a lock.
    A = pa.PlayerAnchor()
    new = None
    for i in range(4):
        new = A.update(f, ts=i * 0.2, armed=True)
        if new is not None and abs(new.icon_x - icx) <= 6 and abs(new.icon_y - icy) <= 6:
            break
        A.reset(keep_identity=True)
    assert new is not None and abs(new.icon_x - icx) <= 6 and abs(new.icon_y - icy) <= 6


# ------------------------------------------------------------------ 3. the coarse pass
def test_the_coarse_map_sizes_its_template_by_the_row(clean_env):
    """One template size per y-strip. Checked on the map itself: the peak for a small
    plate high in the band and a big plate low in it must BOTH survive."""
    a = pa.PlayerAnchor()
    f = court()
    blit_plate(f, 400.0, 360.0, scale=0.55)          # far player, high in the band
    blit_plate(f, 1000.0, 660.0, scale=1.10)         # near player, low in the band
    by0, by1 = int(a.BAND_Y0_F * H), int(min(H, a.BAND_Y1_F * H))
    gb = cv2.cvtColor(f[by0:by1], cv2.COLOR_BGR2GRAY)
    gs = cv2.resize(gb, (gb.shape[1] // 2, gb.shape[0] // 2), interpolation=cv2.INTER_AREA)
    fx, fy = gs.shape[1] / float(W), gs.shape[0] / float(by1 - by0)
    flat = a._coarse_map(gs, a._match_tmpl(), 1.0, fx, fy, by0, a._scales())
    assert flat is not None
    for cx, cy in ((400.0, 360.0), (1000.0, 660.0)):
        win = flat[int((cy - by0) * fy) - 3:int((cy - by0) * fy) + 4,
                   int(cx * fx) - 3:int(cx * fx) + 4]
        assert win.size and float(win.max()) >= 0.45, (cx, cy, float(win.max()) if win.size else None)


def test_coarse_strips_zero_is_a_single_size_pass(clean_env, monkeypatch):
    monkeypatch.setenv("ORION_ANCHOR_COARSE_STRIPS", "0")
    a = pa.PlayerAnchor()
    f = court()
    blit_plate(f, 900.0, 600.0, scale=1.0)
    by0, by1 = int(a.BAND_Y0_F * H), int(min(H, a.BAND_Y1_F * H))
    gb = cv2.cvtColor(f[by0:by1], cv2.COLOR_BGR2GRAY)
    gs = cv2.resize(gb, (gb.shape[1] // 4, gb.shape[0] // 4), interpolation=cv2.INTER_AREA)
    flat = a._coarse_map(gs, a._template(), 1.0, gs.shape[1] / float(W),
                         gs.shape[0] / float(by1 - by0), by0, a.SCALES_ACQ)
    assert flat is not None and flat.shape == gs.shape


def test_refine_scales_picks_the_rungs_the_row_allows(clean_env):
    a = pa.PlayerAnchor()
    lad = a._scales()
    hi = a._refine_scales(lad, 700.0)               # low on screen -> a big plate
    lo = a._refine_scales(lad, 330.0)               # high on screen -> a small plate
    assert len(hi) == 3 and len(lo) == 3
    assert max(hi) > max(lo) and min(hi) > min(lo), (lo, hi)


def test_refine_scales_off_returns_the_whole_ladder(clean_env, monkeypatch):
    monkeypatch.setenv("ORION_ANCHOR_SCALE_ROW", "0")
    a = pa.PlayerAnchor()
    assert a._refine_scales(a._scales(), 500.0) == a._scales()


# ------------------------------------------------------------------ 4. the tracked scale
def test_the_tracked_scale_cannot_random_walk_out_of_the_ladder(clean_env, monkeypatch):
    """_track re-scales by 0.92/1.09 every frame it re-locks; over a 60-frame hold that
    random-walked to 0.39 on a ladder that bottoms out at 0.50, which then resized the
    learned gamertag wrongly and shrank the predicted patch."""
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    f = court()
    blit_plate(f, 900.0, 560.0, scale=0.8)
    A = pa.PlayerAnchor()
    a = A.update(f, ts=0.0, armed=True)
    assert a is not None
    lo = min(pa.PlayerAnchor.SCALES_WIDE) * 0.9
    hi = max(pa.PlayerAnchor.SCALES_WIDE) * 1.1
    for i in range(1, 120):
        a = A.update(f, ts=i / 60.0, armed=True)
        if a is not None:
            assert lo - 1e-6 <= a.scale <= hi + 1e-6, (i, a.scale)


# ------------------------------------------------------------------ 5. the patch geometry
def test_the_patch_y_offset_scales_with_the_plate(clean_env):
    """The meter is a fixed-size HUD panel and the plate is not, so the gap between them
    is per unit of PLATE SCALE. A half-size plate predicts a bottom band half as far up."""
    A = pa.PlayerAnchor()
    big = A._anchor_from(900.0, 600.0, 1.0, 0.8, -9.0, 3, 0.0, W, H)
    small = A._anchor_from(900.0, 600.0, 0.5, 0.8, -9.0, 3, 0.0, W, H)
    assert big.bot_hi < small.bot_hi                 # the small plate's band sits LOWER
    mid_big = (big.bot_lo + big.bot_hi) * 0.5
    mid_small = (small.bot_lo + small.bot_hi) * 0.5
    assert abs((600.0 - mid_big) - 150.0) <= 1.0
    assert abs((600.0 - mid_small) - 75.0) <= 1.0


def test_dy_scaled_off_is_the_shipped_constant(clean_env, monkeypatch):
    monkeypatch.setenv("ORION_ANCHOR_DY_SCALED", "0")
    A = pa.PlayerAnchor()
    for sc in (0.5, 1.0):
        a = A._anchor_from(900.0, 600.0, sc, 0.8, -9.0, 3, 0.0, W, H)
        assert abs((600.0 - (a.bot_lo + a.bot_hi) * 0.5) - 150.0) <= 1.0


# ------------------------------------------------------------------ 6. the offsets
def _fake_anchor(A, icon_x, icon_y, scale, tx):
    a = A._anchor_from(icon_x, icon_y, scale, 0.85, tx, 4, 0.0, W, H)
    return a


def test_an_identity_confirmed_plate_teaches_the_offsets_outside_the_patch(clean_env):
    """The circularity this breaks: the old rule only kept an offset when the CURRENT
    patch already contained the box, so a session whose prior was 75 px wrong could never
    collect the samples that would have corrected it."""
    A = pa.PlayerAnchor()
    A._tag = np.full((26, 92), 128, np.uint8)        # identity exists
    f = court()
    box = (900.0, 300.0, 26.0, 120.0)                # bottom = 420, centre = 913
    a = _fake_anchor(A, 890.0, 700.0, 1.0, 0.8)      # plate well below the patch
    assert not a.contains_box(*box)
    A.note_meter(f, box, a, True)
    assert len(A._off_dy) == 1
    assert abs(A._off_dy[0] - (700.0 - 420.0)) <= 1.0


def test_offset_ident_off_keeps_the_old_circular_rule(clean_env, monkeypatch):
    monkeypatch.setenv("ORION_ANCHOR_OFFSET_IDENT", "0")
    A = pa.PlayerAnchor()
    A._tag = np.full((26, 92), 128, np.uint8)
    a = _fake_anchor(A, 890.0, 700.0, 1.0, 0.8)
    A.note_meter(court(), (900.0, 300.0, 26.0, 120.0), a, True)
    assert A._off_dy == []


def test_the_learned_y_offset_is_stored_per_unit_of_plate_scale(clean_env):
    A = pa.PlayerAnchor()
    A._tag = np.full((26, 92), 128, np.uint8)
    box = (900.0, 300.0, 26.0, 120.0)                # bottom = 420
    A.note_meter(court(), box, _fake_anchor(A, 890.0, 480.0, 0.5, 0.8), True)
    # 60 px of measured gap at scale 0.5 is a 120 px prior at scale 1.0
    assert abs(A._off_dy[0] - 120.0) <= 1.0


def test_note_meter_is_inert_without_a_confirmation(clean_env):
    A = pa.PlayerAnchor()
    A._tag = np.full((26, 92), 128, np.uint8)
    A.note_meter(court(), (900.0, 300.0, 26.0, 120.0),
                 _fake_anchor(A, 890.0, 700.0, 1.0, 0.8), False)
    assert A._off_dy == [] and A._ps_seen == []


def test_an_implausible_offset_never_reaches_the_prior(clean_env):
    """The prior is a MEDIAN, and a median only survives a MINORITY of bad samples: one
    plate on the wrong player, taken on 40 frames of one shot, would walk it off court."""
    A = pa.PlayerAnchor()
    A._tag = np.full((26, 92), 128, np.uint8)
    box = (900.0, 300.0, 26.0, 120.0)                # bottom = 420
    A.note_meter(court(), box, _fake_anchor(A, 890.0, 1000.0, 1.0, 0.8), True)
    assert A._off_dy == []                           # 580 px below the box: not a plate
    A.note_meter(court(), box, _fake_anchor(A, 300.0, 520.0, 1.0, 0.8), True)
    assert A._off_dy == []                           # 613 px left of it: not a plate


def test_offset_sane_off_lets_the_outlier_through(clean_env, monkeypatch):
    monkeypatch.setenv("ORION_ANCHOR_OFFSET_SANE", "0")
    A = pa.PlayerAnchor()
    A._tag = np.full((26, 92), 128, np.uint8)
    A.note_meter(court(), (900.0, 300.0, 26.0, 120.0),
                 _fake_anchor(A, 890.0, 1000.0, 1.0, 0.8), True)
    assert len(A._off_dy) == 1


def test_a_weak_gamertag_match_may_rank_but_may_not_move_the_geometry(clean_env):
    """ORION_ANCHOR_TX_MIN (0.35) answers 'which of these two plates', not 'move the
    prior to this one'. The offset path needs ORION_ANCHOR_OFFSET_TX."""
    A = pa.PlayerAnchor()
    A._tag = np.full((26, 92), 128, np.uint8)
    box = (900.0, 300.0, 26.0, 120.0)
    A.note_meter(court(), box, _fake_anchor(A, 890.0, 700.0, 1.0, 0.40), True)
    assert A._off_dy == []
    A.note_meter(court(), box, _fake_anchor(A, 890.0, 700.0, 1.0, 0.70), True)
    assert len(A._off_dy) == 1


# ------------------------------------------------------------------ 6b. the plate gate
def test_a_disc_with_no_gamertag_beside_it_is_not_a_plate(clean_env, monkeypatch):
    """The discrimination the wide ladder cost, bought back: a bare disc on empty court
    is refused, the same disc with a gamertag next to it is not."""
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    bare = court()
    t = pa.PlayerAnchor()._template()
    n = 22
    tt = cv2.resize(t, (n, n), interpolation=cv2.INTER_AREA)
    bare[560 - n // 2:560 + n - n // 2, 900 - n // 2:900 + n - n // 2] =         cv2.cvtColor(tt.astype(np.uint8), cv2.COLOR_GRAY2BGR)
    assert pa.PlayerAnchor().update(bare, ts=0.0, armed=True) is None

    withtag = court()
    icx, icy = blit_plate(withtag, 900.0, 560.0, scale=0.82)
    a = pa.PlayerAnchor().update(withtag, ts=0.0, armed=True)
    assert a is not None and abs(a.icon_x - icx) <= 6 and abs(a.icon_y - icy) <= 6


def test_plate_gate_off_accepts_the_bare_disc_again(clean_env, monkeypatch):
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    monkeypatch.setenv("ORION_ANCHOR_PLATE_GATE", "0")
    bare = court()
    t = pa.PlayerAnchor()._template()
    n = 22
    tt = cv2.resize(t, (n, n), interpolation=cv2.INTER_AREA)
    bare[560 - n // 2:560 + n - n // 2, 900 - n // 2:900 + n - n // 2] =         cv2.cvtColor(tt.astype(np.uint8), cv2.COLOR_GRAY2BGR)
    assert pa.PlayerAnchor().update(bare, ts=0.0, armed=True) is not None


def test_the_tag_stats_floors_sit_far_below_the_true_population(clean_env):
    """Guards the numbers, not the code: the floors must stay ~5x under the measured
    p10 of the TRUE plate population (grad 153, bright 0.106, std 54)."""
    f = court()
    icx, icy = blit_plate(f, 900.0, 560.0, scale=1.0)
    A = pa.PlayerAnchor()
    score, ok = A._tag_stats(f, icx, icy, 1.0)
    assert ok and score > 0.5, (score, ok)
    # bare court in the same place: the court line through it still has gradient, so it
    # is the BRIGHT-text fraction that refuses -- which is the point, a nameplate is white
    # glyphs and a court line is not a word.
    blank, blank_ok = A._tag_stats(court(), 900.0, 560.0, 1.0)
    assert not blank_ok, (blank, blank_ok)
    flat = np.full((H, W, 3), 60, np.uint8)
    _, flat_ok = A._tag_stats(flat, 900.0, 560.0, 1.0)
    assert not flat_ok


# ------------------------------------------------------------------ 7. the adaptive floor
def test_the_floor_does_not_move_without_an_identity(clean_env):
    A = pa.PlayerAnchor()
    A._ps_seen = [0.50] * 20
    assert A._ps_floor() == pytest.approx(0.58)


def test_a_confirmed_identity_may_lower_its_own_floor(clean_env):
    A = pa.PlayerAnchor()
    A._tag = np.full((26, 92), 128, np.uint8)
    A._ps_seen = [0.52, 0.54, 0.55, 0.60, 0.62]
    f = A._ps_floor()
    assert 0.42 <= f < 0.58, f


def test_the_floor_never_goes_below_the_hard_minimum(clean_env):
    A = pa.PlayerAnchor()
    A._tag = np.full((26, 92), 128, np.uint8)
    A._ps_seen = [0.10] * 20
    assert A._ps_floor() == pytest.approx(0.42)


def test_ps_adapt_off_pins_the_floor(clean_env, monkeypatch):
    monkeypatch.setenv("ORION_ANCHOR_PS_ADAPT", "0")
    A = pa.PlayerAnchor()
    A._tag = np.full((26, 92), 128, np.uint8)
    A._ps_seen = [0.10] * 20
    assert A._ps_floor() == pytest.approx(0.58)


def test_a_reset_that_drops_identity_drops_the_learned_floor(clean_env):
    A = pa.PlayerAnchor()
    A._tag = np.full((26, 92), 128, np.uint8)
    A._ps_seen = [0.45] * 10
    A._scale_seen = [0.7] * 10
    A.reset(keep_identity=True)
    assert A._ps_seen and A._tag is not None
    A.reset(keep_identity=False)
    assert A._ps_seen == [] and A._scale_seen == [] and A._tag is None


# ------------------------------------------------------------------ 8. the whole switch
def test_every_acquisition_knob_off_is_the_pre_09_17_search(shipped_env):
    A = pa.PlayerAnchor()
    A._tag = np.full((26, 92), 128, np.uint8)
    A._ps_seen = [0.10] * 20
    assert A._match_tmpl().shape == (27, 27)
    assert A._scales() == pa.PlayerAnchor.SCALES_ACQ
    assert A._ps_floor() == pytest.approx(0.58)
    assert A._refine_scales(A._scales(), 500.0) == pa.PlayerAnchor.SCALES_ACQ
    assert A._dy_scaled() is False
    a = A._anchor_from(900.0, 600.0, 0.5, 0.8, -9.0, 3, 0.0, W, H)
    assert abs((600.0 - (a.bot_lo + a.bot_hi) * 0.5) - 150.0) <= 1.0


def test_the_master_switch_still_makes_the_module_inert(clean_env, monkeypatch):
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "0")
    assert pa.enabled() is False


# ------------------------------------------------------------------ 9. the cost
def test_acquisition_and_tracking_stay_inside_the_frame_budget(clean_env, monkeypatch):
    """Acquisition is rate-limited to ORION_ANCHOR_ACQ_MS, so it is the TRACK path that
    runs on nearly every armed frame and it is the one that has to be free."""
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    f = court()
    blit_plate(f, 900.0, 560.0, scale=0.8)
    A = pa.PlayerAnchor()
    A.update(f, ts=0.0, armed=True)                  # warm OpenCV's kernels
    acq = []
    for i in range(8):
        B = pa.PlayerAnchor()
        B.update(f, ts=float(i), armed=True)
        acq.append(B.last_ms)
    trk = []
    for i in range(1, 40):
        A.update(f, ts=i / 60.0, armed=True)
        trk.append(A.last_ms)
    assert max(acq) < 12.0, max(acq)                 # one rate-limited full-band search
    assert sorted(trk)[len(trk) // 2] < 2.0, sorted(trk)[len(trk) // 2]




# ------------------------------------------------------------------ 10. per-strip peaks
# [ORION_ANCHOR_COARSE_PER_STRIP 2026-09-19] _coarse_map already sizes the template by the
# ROW, and that is exactly why a GLOBAL top-k cannot rank across the band: the top strip's
# template is about half the bottom strip's, and a small template's TM_CCOEFF_NORMED runs
# high on anything -- crowd, stands, scoreboard, and every OTHER player's nameplate, which
# is drawn small because those players are further away. Measured on the six presses the
# 09-19 baseline lost to `not_in_topk` (050219 seq 9/18/23, 135725 seq 2/15, 152024 seq 8):
# the upper band scores 0.62-0.82 while the owner's real plate at the foot of the court
# scores 0.46-0.61, and the true plate is not in the GLOBAL top-16 on most of those frames
# while it is in its OWN strip's top-3 on every frame the oracle sees it.


def far_nameplates(f, n=10, y=300.0, scale=0.55):
    """Other players' plates, high in the band and therefore SMALL -- the population that
    ate the whole global top-k on the real dumps."""
    for i in range(n):
        blit_plate(f, 140.0 + 112.0 * i, y + 18.0 * (i % 4), scale=scale,
                   tag="RIVAL%d" % i)
    return f


def _owner_scene():
    """Ten far plates high in the band, the owner's own plate low in it, identity known."""
    A = pa.PlayerAnchor()
    f = far_nameplates(court())
    icx, icy = blit_plate(f, 520.0, 620.0, scale=1.0, tag="OWNER99")
    assert A._learn_tag(f, icx, icy, 1.0)
    return A, f, icx, icy


def _refined_rows(A, f, ts=0.0):
    """The y720 rows the acquisition actually refined, via _refine_scales."""
    rows = []
    orig = pa.PlayerAnchor._refine_scales

    def spy(self, scales, y720, k=3):
        rows.append(float(y720))
        return orig(self, scales, y720, k)

    pa.PlayerAnchor._refine_scales = spy
    try:
        a = A.update(f, ts=ts, armed=True)
    finally:
        pa.PlayerAnchor._refine_scales = orig
    return a, rows


def test_the_coarse_peaks_are_taken_per_strip_not_globally(clean_env, monkeypatch):
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    A, f, icx, icy = _owner_scene()
    a, rows = _refined_rows(A, f)
    assert a is not None, "the owner's plate low in the band was never refined"
    assert abs(a.icon_x - icx) <= 8 and abs(a.icon_y - icy) <= 8, (a.icon_x, a.icon_y)
    assert max(rows) >= 560.0, rows          # a candidate came from the bottom strip


def test_per_strip_zero_is_the_09_17_global_pick(clean_env, monkeypatch):
    """The kill switch is a real switch, and this is the regression it restores: every
    candidate comes from the crowded top strip and the owner's plate is never refined."""
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    monkeypatch.setenv("ORION_ANCHOR_COARSE_PER_STRIP", "0")
    A, f, _icx, icy = _owner_scene()
    a, rows = _refined_rows(A, f)
    assert rows and max(rows) < 500.0, rows
    assert a is None or abs(a.icon_y - icy) > 20, a


def test_the_split_does_not_buy_itself_more_refine_work(clean_env, monkeypatch):
    """The same budget, divided -- not a bigger budget. ORION_ANCHOR_COARSE_TOPK still
    bounds the candidates the refine pass pays for."""
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    A, f, _icx, _icy = _owner_scene()
    _a, split = _refined_rows(A, f)
    assert len(split) <= 8 + 3, (len(split), split)


def test_a_single_strip_pass_is_unchanged_by_the_split(clean_env, monkeypatch):
    """The throttled second pass runs FLAT (one template size for the whole band), so it
    has one strip and the split must be a no-op there."""
    monkeypatch.setenv("ORION_ANCHOR_COARSE_STRIPS", "1")
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    f = court()
    icx, icy = blit_plate(f, 900.0, 600.0, scale=1.0)
    a = pa.PlayerAnchor().update(f, ts=0.0, armed=True)
    assert a is not None and abs(a.icon_x - icx) <= 8 and abs(a.icon_y - icy) <= 8


# ------------------------------------------------------------------ 11. identity vs track
# [ORION_ANCHOR_TRACK_ID_MISS 2026-09-19] The track re-locks on DISC CORRELATION ALONE, so
# an acquisition that opened on a teammate's plate is then carried for the whole press by
# the cheap path: on session_20260918_135725 seq 14 (a far Left Fade, plate scale 0.65, the
# owner's plate at coarse rank 5) the anchor locked a neighbour 15 ms after the press and
# tracked it for 157 of 167 frames -- `identity` in the failure histogram.
#
# The synthetic gamertags below are text blocks on the same court, so TM_CCOEFF_NORMED
# never drops an impostor to the live 0.35 floor (it scores ~0.64 against the owner's
# 1.00). The floor is a knob, so these raise it: what is under test is the RUN RULE, not
# where the floor happens to sit.


def _tracking_anchor(tag="OWNER99"):
    A = pa.PlayerAnchor()
    f = court()
    icx, icy = blit_plate(f, 900.0, 560.0, scale=1.0, tag=tag)
    a = A.update(f, ts=0.0, armed=True)
    assert a is not None
    assert A._learn_tag(f, icx, icy, 1.0)
    other = court()
    blit_plate(other, icx, icy, scale=1.0, tag="ZZZZZZZZ")
    return A, f, other


def test_a_run_of_identity_misses_drops_the_track(clean_env, monkeypatch):
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    monkeypatch.setenv("ORION_ANCHOR_TX_MIN", "0.90")
    monkeypatch.setenv("ORION_ANCHOR_TRACK_ID_MISS", "3")
    A, _own, other = _tracking_anchor()
    for i in range(1, 6):
        A.update(other, ts=i / 60.0, armed=True)
    assert A.stats["id_drop"] >= 1, A.stats


def test_one_identity_miss_does_not_drop_the_track(clean_env, monkeypatch):
    """A RUN, not one frame: the ball, a crossing player or a scale wobble costs a single
    frame's gamertag correlation on the owner's OWN plate too."""
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    monkeypatch.setenv("ORION_ANCHOR_TX_MIN", "0.90")
    monkeypatch.setenv("ORION_ANCHOR_TRACK_ID_MISS", "3")
    A, own, other = _tracking_anchor()
    A.update(other, ts=1 / 60.0, armed=True)
    A.update(own, ts=2 / 60.0, armed=True)
    A.update(other, ts=3 / 60.0, armed=True)
    assert A.stats["id_drop"] == 0, A.stats


def test_the_shipped_default_is_off_so_the_track_is_never_dropped(clean_env, monkeypatch):
    """[2026-09-19] The run rule SHIPS OFF: measured on all five press dumps it bought
    zero extra locks (every one of the +6 came from ORION_ANCHOR_COARSE_PER_STRIP), it did
    not close either real `identity` press, and it cost acquisitions. The mechanism stays
    behind its knob; the default must not move without a fresh refusal study."""
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    monkeypatch.setenv("ORION_ANCHOR_TX_MIN", "0.90")
    A, _own, other = _tracking_anchor()
    for i in range(1, 8):
        A.update(other, ts=i / 60.0, armed=True)
    assert A.stats["id_drop"] == 0, A.stats


def test_track_id_miss_zero_keeps_the_wrong_plate(clean_env, monkeypatch):
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    monkeypatch.setenv("ORION_ANCHOR_TX_MIN", "0.90")
    monkeypatch.setenv("ORION_ANCHOR_TRACK_ID_MISS", "0")
    A, _own, other = _tracking_anchor()
    for i in range(1, 8):
        A.update(other, ts=i / 60.0, armed=True)
    assert A.stats["id_drop"] == 0, A.stats


def test_without_an_identity_the_track_is_never_dropped(clean_env, monkeypatch):
    """Until the owner's gamertag exists there is nothing to contradict: an anchor with no
    identity may help the search but may never refuse, and it may never drop itself."""
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    monkeypatch.setenv("ORION_ANCHOR_TX_MIN", "0.90")
    f = court()
    blit_plate(f, 900.0, 560.0, scale=1.0)
    A = pa.PlayerAnchor()
    for i in range(10):
        A.update(f, ts=i / 60.0, armed=True)
    assert A._tag is None and A.stats["id_drop"] == 0, A.stats


def test_a_dropped_track_re_acquires_instead_of_going_blind(clean_env, monkeypatch):
    """Dropping the track is only worth it because the next ACQUISITION re-ranks with
    identity -- the term that picks the owner out of five nameplates."""
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    monkeypatch.setenv("ORION_ANCHOR_TX_MIN", "0.90")
    monkeypatch.setenv("ORION_ANCHOR_TRACK_ID_MISS", "3")
    monkeypatch.setenv("ORION_ANCHOR_ACQ_MS", "0")
    A, own, other = _tracking_anchor()
    for i in range(1, 6):
        A.update(other, ts=i / 60.0, armed=True)
    assert A.stats["id_drop"] >= 1, A.stats
    a = A.update(own, ts=0.2, armed=True)
    assert a is not None and abs(a.icon_x - 900.0) <= 8, a


def test_a_reset_clears_the_identity_miss_run(clean_env, monkeypatch):
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    monkeypatch.setenv("ORION_ANCHOR_TX_MIN", "0.90")
    monkeypatch.setenv("ORION_ANCHOR_TRACK_ID_MISS", "3")
    A, _own, other = _tracking_anchor()
    A.update(other, ts=1 / 60.0, armed=True)
    A.reset(keep_identity=True)
    assert A._id_miss == 0
