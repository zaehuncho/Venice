"""N7 EARLY-STUB: the cold (unarmed) acquire floor for a green-corroborated red stub.

WHY THIS EXISTS
---------------
Live (logs/orion_native.log, session 2026-08-03T02:48:45..02:49:27) the sidecar's
"POSE ARM: shot-start received -> armed shot-gate" line lands 1 ms AFTER the
"Evidence-backed ownership" decision on EVERY shot.  So for the whole acquisition the
reader is UNARMED: `hw` and `armed` are both false, the armed `_scan` relaxation never
applies and the hardware-only court-wide fallback never runs.  The only low-fill path
left is the nominal-band pop-in `_acquire_structure`, whose stub floor is STUB_H_MIN=10 px
(~8.5% fill on a ~160 px track) -- and the best live shot in that session first read at
first_fill=5.4, i.e. exactly that floor.

Measured on a REAL-PIXEL ramp (framedump shot frames re-cut to a known fill using their
OWN unfilled-track pixels, so background/track/leading-edge/tip/codec artefacts are all
real): the unarmed floor was 10 px and the armed court-wide floor was 2-4 px.  N7 gives
the cold path the same 4 px floor the court-wide path already trusts, by paying for it
with the STRICTER connected-tip proof instead of the loose tip-pixel count.

These tests pin the two halves of that bargain: the stub gets in EARLIER, and it only
gets in with GENUINE meter structure.
"""
import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader

H, W = 1080, 1920
RED_A = (0, 0, 255)
RED_B = (40, 40, 255)
GREEN = (60, 200, 60)
FLOOR_Y = 600
TRACK_TOP_Y = 470          # track height 130 px -> inside the 110..190 tip-relation window
COL_X = 900
COL_W = 24


def _stub_frame(stub_h, col_x=COL_X, col_w=COL_W, green=True, bg=40,
                floor_y=FLOOR_Y, track_top_y=TRACK_TOP_Y, green_y=None):
    """A park meter whose red fill is exactly `stub_h` px tall, capped by the green tip."""
    f = np.full((H, W, 3), bg, np.uint8)
    half = col_w // 2
    if stub_h > 0:
        f[floor_y - stub_h:floor_y, col_x:col_x + half] = RED_A
        f[floor_y - stub_h:floor_y, col_x + half:col_x + col_w] = RED_B
    if green:
        gy = track_top_y if green_y is None else green_y
        f[gy - 6:gy, col_x:col_x + col_w] = GREEN
    return f


def _cold_read(frame, monkeypatch, early="1"):
    """A COLD, UNARMED reader -- the live acquisition condition."""
    monkeypatch.setenv("ORION_READER_EARLY_STUB", early)
    r = SimpleMeterReader(W, H)
    r.set_shot_state(False, 0.0, False)
    return r, r.read(frame, ts=0.0)


# --------------------------------------------------------------------------- the win
@pytest.mark.parametrize("stub_h", [4, 6, 8, 9])
def test_early_stub_acquires_below_the_popin_floor(monkeypatch, stub_h):
    """A 4-9px stub with a genuine green tip is acquired COLD; before N7 it was not."""
    frame = _stub_frame(stub_h)
    _, on = _cold_read(frame, monkeypatch, early="1")
    _, off = _cold_read(frame, monkeypatch, early="0")
    assert off["detected"] is False, \
        f"pre-N7 baseline moved: a {stub_h}px stub must be BELOW the shipped pop-in floor"
    assert on["detected"] is True, f"N7 must acquire a corroborated {stub_h}px stub"
    assert on["stage"] == "acquire"
    b = on["bbox"]
    assert COL_X - 12 <= b[0] <= COL_X + COL_W + 12, f"acquired somewhere else: {b}"


def test_early_stub_is_purely_additive_at_and_above_the_old_floor(monkeypatch):
    """At/above STUB_H_MIN the shipped paths still win: identical detection either way."""
    for stub_h in (10, 14, 20, 40, 90):
        frame = _stub_frame(stub_h)
        _, on = _cold_read(frame, monkeypatch, early="1")
        _, off = _cold_read(frame, monkeypatch, early="0")
        assert off["detected"] is True and on["detected"] is True, stub_h
        assert on["bbox"] == off["bbox"], f"N7 changed the box at stub_h={stub_h}"
        assert on["fill"] == off["fill"], f"N7 changed the fill at stub_h={stub_h}"


# ------------------------------------------------------- corroboration is still the guard
def test_early_stub_refuses_a_stub_with_no_green_tip(monkeypatch):
    """Red alone is decor.  Without the tip the low floor must not be reachable at all."""
    for stub_h in (4, 6, 8, 9):
        _, out = _cold_read(_stub_frame(stub_h, green=False), monkeypatch)
        assert out["detected"] is False, f"{stub_h}px bare red stub must never acquire"


def test_early_stub_refuses_a_tip_outside_the_track_relation(monkeypatch):
    """The tip must sit TIP_UP_MIN..TIP_UP_MAX above the stub floor, centred on it.

    A green blob 60px above the floor (too low) or 260px above (too high) is a different
    on-screen object, not this meter's cap.
    """
    for green_y in (FLOOR_Y - 60, FLOOR_Y - 260):
        _, out = _cold_read(_stub_frame(6, green_y=green_y), monkeypatch)
        assert out["detected"] is False, \
            f"green cap at {FLOOR_Y - green_y}px above the floor must not corroborate"


def test_early_stub_refuses_an_off_centre_tip(monkeypatch):
    """A green component that is not horizontally centred on the stub is not its cap."""
    frame = _stub_frame(6, green=False)
    frame[TRACK_TOP_Y - 6:TRACK_TOP_Y, COL_X + 90:COL_X + 90 + COL_W] = GREEN
    _, out = _cold_read(frame, monkeypatch)
    assert out["detected"] is False


def test_early_stub_keeps_the_nominal_width_gate(monkeypatch):
    """The half-court 0.45x WIDTH relaxation must not leak into the band pass.

    `strict_tip` used to select the strict tip proof AND the half-court scale floors
    together.  N7 passes `strict_scale=False` precisely so a sliver far narrower than a
    real nominal meter column cannot be admitted just because it carries a green dot.
    """
    for col_w in (8, 10, 12):
        frame = _stub_frame(6, col_w=col_w)
        _, out = _cold_read(frame, monkeypatch)
        assert out["detected"] is False, \
            f"a {col_w}px-wide stub is below the nominal width floor and must not acquire"


def test_early_stub_refuses_a_blank_and_a_flat_menu_frame(monkeypatch):
    """No structure anywhere -> no lock, and no exception from the extra pass."""
    for bg in (0, 40, 128, 200):
        _, out = _cold_read(np.full((H, W, 3), bg, np.uint8), monkeypatch)
        assert out["detected"] is False
        assert out["bbox"] == [0, 0, 0, 0]


# ------------------------------------------------------------------ structure-proof contract
def test_early_stub_lock_still_carries_green_for_the_structure_proof(monkeypatch):
    """Ownership needs `green is not None` (simple_meter_reader.py:2429).

    An N7 acquire must therefore emit a real green window, not just a box -- otherwise it
    would buy an earlier detection that can never become an owned shot.
    """
    monkeypatch.setenv("ORION_READER_EARLY_STUB", "1")
    r = SimpleMeterReader(W, H)
    r.set_shot_state(False, 0.0, False)
    out = r.read(_stub_frame(6), ts=0.0)
    assert out["detected"] is True
    assert out.get("green") is not None, "N7 lock must expose green-window structure"


def test_early_stub_never_latches_structure_proof_without_a_physical_epoch(monkeypatch):
    """The epoch binding is untouched: no hardware epoch -> no proof, however early we lock."""
    monkeypatch.setenv("ORION_READER_EARLY_STUB", "1")
    r = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    r.set_shot_state(False, 0.0, False)
    res = r.detect(_stub_frame(6), ts=0.0)
    assert not bool(getattr(res, "detected", False)), \
        "an unarmed frame must stay behind the gameplay gate"
    assert r._gameplay_structure_verified is False
    assert r._gameplay_structure_proof_epoch == 0


def test_early_stub_proof_binds_to_the_current_physical_epoch(monkeypatch):
    """Armed + a live epoch: the early lock's green proves structure for THAT epoch only."""
    monkeypatch.setenv("ORION_READER_EARLY_STUB", "1")
    r = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    r.notify_physical_shot_start(77)
    r.set_shot_state(True, 0.0, True)
    res = r.detect(_stub_frame(6), ts=0.0)
    assert bool(getattr(res, "detected", False))
    assert r._gameplay_structure_verified is True
    assert r._gameplay_structure_proof_epoch == 77
    # a new physical press must invalidate it immediately
    r.notify_physical_shot_start(78)
    assert r._gameplay_structure_verified is False
    assert r._gameplay_structure_proof_epoch == 0


# --------------------------------------------------- subclass signature (latent crash guard)
def test_compressed_reader_accepts_the_keyword_acquire_paths(monkeypatch):
    """CompressedMeterReader overrides `_acquire_structure`; it must accept the kwargs.

    Both the hardware court-wide fallback (`region=`, `strict_tip=`, `red_mask=`) and the
    N7 band pass (`strict_scale=`, `stub_h_min=`) call the override polymorphically.  With
    the old `(self, frame)` signature a hardware-armed cold frame raised
    ``TypeError: _acquire_structure() got an unexpected keyword argument 'region'``.
    """
    cv2 = pytest.importorskip("cv2")  # noqa: F841
    from compressed_meter_reader import CompressedMeterReader

    monkeypatch.setenv("ORION_READER_EARLY_STUB", "1")
    r = CompressedMeterReader(W, H)
    r.notify_physical_shot_start(5)
    r.set_shot_state(True, 0.0, True)
    # Red must be PRESENT (but structureless) or the court-wide path short-circuits on its
    # `codec_red_possible` pixel-count gate and never reaches its `region=`/`strict_tip=`
    # call at all -- which is what hid this crash.  A bare red patch with no green cap
    # forces every acquire tier to run and every one of them to decline.
    decor = np.full((H, W, 3), 40, np.uint8)
    decor[300:340, 1500:1560] = RED_A
    out = r.read(decor, ts=0.0)
    assert out["detected"] is False

    # and the luma fallback stays reserved for the plain pop-in call
    assert r._acquire_structure(np.full((H, W, 3), 40, np.uint8),
                                region=(0, 0, W, H), strict_tip=True) is None
