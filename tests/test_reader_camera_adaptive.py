"""Camera-adaptive / bulletproofing coverage for SimpleMeterReader (simple_meter_reader.py).

Guards the four robustness guarantees added on top of the (already accurate) reader, each of
which defaults to the current tuned behaviour on a NOMINAL standstill camera:

  1. CAMERA-ANGLE scale adaptation (ORION_READER_SCALE_ADAPT, default ON): on a confident lock
     the reader learns the meter's on-screen size and widens the acquire/track gates around a
     rolling estimate, so a zoomed-in / zoomed-out meter still reads on RED. Dormant (and
     byte-identical) while the camera is nominal.
  2. TIP-CAPTURE guarantee (ORION_READER_TIP_ENFORCE, default ON): the box top always covers the
     arrow-tip apex above the green cap, even when the silver-chevron walk finds nothing.
  3. NO-DISAPPEAR-DURING-SHOT (ORION_READER_ARMED_HOLD, default ON): while the shot-gate is ARMED
     the lock is held through an arbitrarily long detection gap, up to the wall-clock coast cap.
  4. Colour tolerance + band-override toggles.
"""
import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader

H, W = 1080, 1920
RED_A = (0, 0, 255)
RED_B = (40, 40, 255)
GREEN = (60, 200, 60)
FLOOR_Y = 600
COL_X = 900
NOM_W = 24
NOM_TRACK = 130


def _zoom_frame(scale=1.0, fill=0.85, green=True, bg=40, col_x=COL_X):
    """A UNIFORM-zoom park meter: width AND track height both scale with `scale` (a real camera
    zoom), the green make-window cap at the track top."""
    f = np.full((H, W, 3), bg, np.uint8)
    col_w = max(4, int(round(NOM_W * scale)))
    th = max(8, int(round(NOM_TRACK * scale)))
    top = FLOOR_Y - th
    red_top = int(round(FLOOR_Y - fill * th))
    half = col_w // 2
    f[red_top:FLOOR_Y, col_x:col_x + half] = RED_A
    f[red_top:FLOOR_Y, col_x + half:col_x + col_w] = RED_B
    if green:
        gh = max(4, int(round(6 * scale)))
        f[top - gh:top, col_x:col_x + col_w] = GREEN
    return f


def _red_reads(reader, scales, ts0=0):
    """Read a sequence of uniform-zoom frames; return the per-frame EVIDENCE ('red' = a real red
    column read, 'green'/'ncc'/None = a fallback hold, not an accurate fill).

    ARM the shot-gate each frame: live, a meter is only ever on screen during an armed shot
    (hardware hold-trigger / CV self-arm on a rising read), and ORION_READER_SCALE_GUARD
    (default ON) deliberately feeds the scale estimate ONLY from armed/fresh locks (anti-poisoning
    of _scale_est by décor). An UN-armed visible zooming meter does not occur in play, so scale
    adaptation is exercised in its live-representative armed path."""
    ev = []
    for i, sc in enumerate(scales):
        try:
            reader.set_shot_state(True, 0.0, True)
        except Exception:
            pass
        reader.read(_zoom_frame(sc), ts=(ts0 + i) / 60.0)
        ev.append(reader.last_debug.get("evidence"))
    return ev


# --------------------------------------------------------------------------- #
#  1. CAMERA-ANGLE SCALE ADAPTATION
# --------------------------------------------------------------------------- #
def test_scale_adapt_dormant_and_byte_identical_at_nominal(monkeypatch):
    """A NOMINAL standstill meter must keep the scale estimate at ~1.0 (inside the deadband) so
    the gates never move -- and the per-frame read is byte-identical to the reader with scale
    adaptation switched OFF."""
    monkeypatch.setenv("ORION_READER_ARMED_HOLD", "0")
    monkeypatch.setenv("ORION_READER_SCALE_ADAPT", "1")
    r_on = SimpleMeterReader(W, H)
    monkeypatch.setenv("ORION_READER_SCALE_ADAPT", "0")
    r_off = SimpleMeterReader(W, H)
    for i in range(20):
        a = r_on.read(_zoom_frame(1.0), ts=i / 60.0)
        b = r_off.read(_zoom_frame(1.0), ts=i / 60.0)
        assert a["bbox"] == b["bbox"], f"frame {i}: adapt changed the bbox {a['bbox']} vs {b['bbox']}"
        assert a["fill"] == b["fill"], f"frame {i}: adapt changed the fill {a['fill']} vs {b['fill']}"
    assert not r_on._scale_active(), "scale-adapt must stay dormant on a nominal camera"
    assert abs(r_on._scale_est - 1.0) <= r_on._scale_deadband


def test_scale_adapt_accepts_zoomed_in_meter(monkeypatch):
    """A meter that ZOOMS IN past the nominal size gates (its column grows taller/wider than
    H_MAX / W_MAX) keeps reading on RED with scale-adapt, where the fixed gates drop it to a
    green-only hold (frozen fill)."""
    monkeypatch.setenv("ORION_READER_ARMED_HOLD", "0")
    # ramp 1.0 -> 1.6 then hold zoomed-in
    scales = [1.0] * 6 + [1.0 + 0.06 * k for k in range(1, 11)] + [1.6] * 8

    monkeypatch.setenv("ORION_READER_SCALE_ADAPT", "1")
    on = _red_reads(SimpleMeterReader(W, H), scales)
    monkeypatch.setenv("ORION_READER_SCALE_ADAPT", "0")
    off = _red_reads(SimpleMeterReader(W, H), scales)

    on_red = sum(e == "red" for e in on[-8:])
    off_red = sum(e == "red" for e in off[-8:])
    assert on_red >= 6, f"scale-adapt should keep RED reads while zoomed in (got {on_red}/8)"
    assert on_red > off_red, f"scale-adapt ({on_red}) must beat fixed gates ({off_red}) when zoomed in"


def test_scale_adapt_accepts_zoomed_out_meter(monkeypatch):
    """A meter that ZOOMS OUT below the nominal width floor (W_MIN) stays tracked on RED with
    scale-adapt; the fixed gates lose the too-thin column."""
    monkeypatch.setenv("ORION_READER_ARMED_HOLD", "0")
    scales = [1.0] * 6 + [1.0 - 0.05 * k for k in range(1, 9)] + [0.6] * 8

    monkeypatch.setenv("ORION_READER_SCALE_ADAPT", "1")
    on = _red_reads(SimpleMeterReader(W, H), scales)
    monkeypatch.setenv("ORION_READER_SCALE_ADAPT", "0")
    off = _red_reads(SimpleMeterReader(W, H), scales)

    on_red = sum(e == "red" for e in on[-8:])
    off_red = sum(e == "red" for e in off[-8:])
    assert on_red >= 5, f"scale-adapt should keep RED reads while zoomed out (got {on_red}/8)"
    assert on_red > off_red, f"scale-adapt ({on_red}) must beat fixed gates ({off_red}) when zoomed out"


# --------------------------------------------------------------------------- #
#  2. TIP-CAPTURE GUARANTEE
# --------------------------------------------------------------------------- #
def _faint_cap_frame(fill=0.5):
    """A meter whose green cap has NO silver arrow-tip chevron above it (faint/blurred tip): the
    chevron walk finds nothing, so ONLY the enforced style-lift can keep the box top above the
    tip. Green cap TOP row sits at FLOOR_Y - NOM_TRACK - 6."""
    return _zoom_frame(1.0, fill=fill, green=True)


def test_tip_capture_enforced_when_chevron_absent(monkeypatch):
    """TIP GUARANTEE (legacy blind style-lift): with no silver chevron to walk, the box top is
    still lifted a style-height above the green cap top. This is the pre-TIP_TIGHT behavior, so
    TIP_TIGHT is pinned OFF here; the default-ON TIP_TIGHT deliberately hugs the cap when no apex
    is detected (a real detected apex is still covered -- see test_tip_never_clipped_*)."""
    monkeypatch.setenv("ORION_READER_TIP_TIGHT", "0")   # legacy blind-lift path (TIP_TIGHT now default-ON)
    cap_top_row = FLOOR_Y - NOM_TRACK - 6            # topmost green row of the cap

    monkeypatch.setenv("ORION_READER_TIP_ENFORCE", "1")
    r_on = SimpleMeterReader(W, H)
    top_on = r_on.read(_faint_cap_frame(), ts=0.0)["bbox"][1]
    monkeypatch.setenv("ORION_READER_TIP_ENFORCE", "0")
    r_off = SimpleMeterReader(W, H)
    top_off = r_off.read(_faint_cap_frame(), ts=0.0)["bbox"][1]

    lift = r_on._apex_lift
    assert lift >= 4
    # enforced top sits >= lift above the green cap top (the tip is inside the box)
    assert top_on <= cap_top_row - lift + 1, \
        f"enforced box top {top_on} must be >= {lift}px above the cap top {cap_top_row}"
    # and strictly higher than the un-enforced top
    assert top_on < top_off, f"enforce ON top {top_on} must be above enforce OFF top {top_off}"


def test_tip_never_clipped_across_fill_levels(monkeypatch):
    """At EVERY fill level the box top must be at/above the true apex -- no green cap row may
    remain above the reported box top."""
    monkeypatch.setenv("ORION_READER_TIP_ENFORCE", "1")
    cap_top_row = FLOOR_Y - NOM_TRACK - 6
    for fill in (0.2, 0.4, 0.6, 0.8, 1.0):
        r = SimpleMeterReader(W, H)
        top = r.read(_faint_cap_frame(fill=fill), ts=0.0)["bbox"][1]
        assert top <= cap_top_row, f"fill {fill}: box top {top} clipped below the cap top {cap_top_row}"


# --------------------------------------------------------------------------- #
#  3. NO-DISAPPEAR-DURING-SHOT
# --------------------------------------------------------------------------- #
def test_armed_hold_holds_lock_through_long_gap(monkeypatch):
    """While ARMED the lock survives a LONG detection gap (100 blank frames) with zero within-gap
    disappearances -- the whole-shot coast. With the hold disabled it drops within the short
    confidence-decay coast."""
    monkeypatch.setenv("ORION_READER_ARMED_HOLD", "1")
    r = SimpleMeterReader(W, H)
    r.set_shot_state(True)
    assert r.read(_zoom_frame(1.0), ts=0.0)["detected"]
    blank = np.full((H, W, 3), 40, np.uint8)
    dets = []
    for i in range(100):
        r.set_shot_state(True)
        dets.append(r.read(blank, ts=(i + 1) / 60.0)["detected"])
    assert all(dets), f"armed hold dropped mid-shot after {dets.index(False)} frames"

    monkeypatch.setenv("ORION_READER_ARMED_HOLD", "0")
    r2 = SimpleMeterReader(W, H)
    r2.set_shot_state(True)
    r2.read(_zoom_frame(1.0), ts=0.0)
    dropped = False
    for i in range(100):
        r2.set_shot_state(True)
        if not r2.read(blank, ts=(i + 1) / 60.0)["detected"]:
            dropped = True
            break
    assert dropped, "with the armed hold OFF the lock should drop within the decay coast"


def test_armed_coast_cap_eventually_releases(monkeypatch):
    """The armed hold is bounded by a wall-clock coast cap (stuck-armed safety): past the cap the
    lock is released even while still armed."""
    monkeypatch.setenv("ORION_READER_ARMED_HOLD", "1")
    monkeypatch.setenv("ORION_READER_ARMED_COAST", "12")
    r = SimpleMeterReader(W, H)
    r.set_shot_state(True)
    r.read(_zoom_frame(1.0), ts=0.0)
    blank = np.full((H, W, 3), 40, np.uint8)
    stages = []
    for i in range(30):
        r.set_shot_state(True)
        stages.append(r.read(blank, ts=(i + 1) / 60.0)["stage"])
    assert "no_meter" in stages, "the armed coast cap must eventually release the lock"
    assert stages.count("coast") >= 10, "it should still hold for ~the cap length first"


# --------------------------------------------------------------------------- #
#  4. TOGGLES: colour tolerance + band override
# --------------------------------------------------------------------------- #
def test_color_tol_widens_red_band(monkeypatch):
    """A dimmer red column (R below the strict 220 floor) is missed by the default band but
    acquired once the colour tolerance widens it. Default (tol 0) stays strict."""
    dim = np.full((H, W, 3), 40, np.uint8)
    th = NOM_TRACK
    top = FLOOR_Y - th
    red_top = int(round(FLOOR_Y - 0.9 * th))
    dim[red_top:FLOOR_Y, COL_X:COL_X + NOM_W] = (30, 30, 205)     # R=205 < 220 floor
    dim[top - 6:top, COL_X:COL_X + NOM_W] = GREEN

    monkeypatch.setenv("ORION_READER_COLOR_TOL", "0")
    assert SimpleMeterReader(W, H).read(dim, ts=0.0)["detected"] is False
    monkeypatch.setenv("ORION_READER_COLOR_TOL", "30")
    assert SimpleMeterReader(W, H).read(dim, ts=0.0)["detected"] is True


def test_band_override_moves_search_band(monkeypatch):
    """An explicit search-band override that EXCLUDES the meter column must suppress the lock
    (proves the override is honoured); the default band still finds it."""
    frame = _zoom_frame(1.0)
    assert SimpleMeterReader(W, H).read(frame, ts=0.0)["detected"] is True
    # band far to the left of COL_X=900 -> the meter is outside it
    monkeypatch.setenv("ORION_READER_BAND", "5,250,600,770")
    assert SimpleMeterReader(W, H).read(frame, ts=0.0)["detected"] is False
