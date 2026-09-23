"""Fresh-acquisition discipline after a leftover (ghost) meter, and the static-zone quarantine.

MEASURED MECHANISM (live 2026-09-15 22:36-22:38Z, rapid drill at one press every 2.5-3s,
epochs 9-15 graded LATE; `logs/orion_native.log` from ~line 59624):

  * Every one of those presses dropped a leftover meter reading 83-94%% (STALE LOCK DROPPED AT
    PRESS), evicted it again post-press (GHOST LOCK DROPPED POST-PRESS) and retired its identity
    after full absence -- and then did it 2 to 11 MORE times inside the same press window
    (GHOST PRESS SUMMARY evictions=2..11).
  * DETECTOR HEALTH over the same 12s: locks 42 -> 84 and seeded 23 -> 65 with drops FLAT at
    26-27. Locks without drops are eviction re-locks: the evict path resets the lifecycle to
    idle directly (never _det_drop_lock, so no drop is counted) and the next proposal re-locks.
  * WHY the next proposal was the ghost again: meter_locator_cv keeps its own positional memory
    (_last_box/_last_ts), and a leftover meter at 86-94%% fill has its green tip WASHED OUT by
    the white fill on top of it -- so it is proposed at conf 0.75 ("the tip was not confirmed")
    and is accepted ONLY by the tip-corroborate co-location escape, i.e. purely because
    something was accepted at that spot a moment ago. The reader's eviction cleared every
    reader-side seed and never touched that memory, so the retired ghost seeded its successor.
  * Every evict/re-lock cycle also re-derives the per-lock ruler (_det_reset_lock_state clears
    _subpx_D / _coarse_denom_ref / _det_scale_reference) -- the project's "fill denominator is
    the detector box, latch it" invariant, thrown away 5-11 times per press.

  * SEPARATELY, with NO press anywhere in the window (22:36:26-22:36:58Z), a static column at
    box=[777,342,26,110] was locked and dropped ~20 times in 40s (DETECTOR HEALTH locks 1->26
    tracking drops 1->25 one for one): _det_drop_lock arms the warm re-acquire memory at the
    spot the lock died and _det_on_found's `warm` shortcut re-seats it there instantly. That is
    the owner's "false locks onto the court white lines".

What is pinned here:
  1. MeterContourLocator.forget_position() makes a washed-tip column re-earn its own
     corroboration (it costs a REAL meter exactly one frame, which is also pinned).
  2. The reader calls it on every leftover refusal, so the proposer cannot hand the ghost back.
  3. A rapid re-press locks the FRESH meter's box, never the ghost's, and publishes no ghost read.
  4. A never-risen lock arms no warm seed and, after _static_zone_strikes such drops, its
     position publishes nothing until a lock there proves a rise.
  5. While the engine owns the shot the box identity is LATCHED: the teleport re-seed may not
     swap the fill denominator mid-shot; a break must go through the drop path.
  6. Every layer is pinned off by its own flag.

Everything is synthetic (numpy/cv2) and drives the REAL SimpleMeterReader over the REAL
MeterContourLocator (through AsyncMeterLocator's inline sync mode). No framedump, no disk.
The drawn meter follows meter_locator_cv's own docstring @720p (26 x 120 track, 11 px white
column, green tip 100 px above the white bottom), the same geometry tests/test_player_anchor.py
uses.
"""
from __future__ import annotations

import os

import cv2
import numpy as np
import pytest

import meter_locator_cv as mlc
import player_anchor as pa
from meter_detector_yolo import AsyncMeterLocator


# [ORION_READER_IDLE_PUBLISH_GATE 2026-09-15] The fixtures below feed a meter with NO press
# armed and (mostly) a constant fill -- byte for byte the shape the reader's idle publication
# gate now withholds from the engine and the overlay (see SimpleMeterReader._idle_publish_ok).
# The gate is a PUBLICATION policy with its own suite (tests/test_idle_publish_gate.py); these
# tests are about what the reader MEASURES, so the gate is switched off here and they keep
# measuring it.
@pytest.fixture(autouse=True)
def _idle_publish_gate_off(monkeypatch):
    monkeypatch.setenv("ORION_READER_IDLE_PUBLISH_GATE", "0")

from simple_meter_reader import SimpleMeterReader

W, H = 1280, 720
FRAME_DT = 1.0 / 60.0

TRACK_W, TRACK_H = 26, 120
COL_X, COL_W = 7, 11
TIP_TOP, TIP_GAP = 8, 100
WHITE_BOT = TIP_TOP + TIP_GAP

COURT_BG = (70, 30, 10)
WHITE = (255, 255, 255)
GREEN = (0, 200, 0)
TRACK = (45, 40, 38)
OUTLINE = (190, 190, 190)

GHOST_X, MET_Y = 600, 300
FRESH_X = 640                      # same zone (40px), different box
GHOST_BOX = (600, 304, 26, 107)    # what the locator returns for GHOST_X/MET_Y
FRESH_BOX = (640, 304, 26, 107)


@pytest.fixture
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("ORION_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")   # we attach the locator by hand
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


def draw_meter(f, x, y, fill_pct, green=True):
    """`green=False` is the LEFTOVER's real appearance: past ~88% fill the white column
    overlays the tip and washes its green out (meter_locator_cv's own conf-0.75 branch)."""
    cv2.rectangle(f, (x, y), (x + TRACK_W - 1, y + TRACK_H - 1), TRACK, -1)
    for dx in (3, 21):
        f[y:y + TRACK_H, x + dx] = OUTLINE
    gtop = y + TIP_TOP
    if green:
        tri = np.array([[x + 12, gtop], [x + 13, gtop],
                        [x + 19, gtop + 10], [x + 6, gtop + 10]], np.int32)
        cv2.fillPoly(f, [tri], GREEN)
    wbot = y + WHITE_BOT
    hpx = int(round(TIP_GAP * fill_pct / 100.0))
    if hpx > 0:
        f[wbot - hpx:wbot, x + COL_X:x + COL_X + COL_W] = WHITE
    notch = np.array([[x + 7, wbot + 1], [x + 17, wbot + 1], [x + 12, wbot + 7]], np.int32)
    cv2.fillPoly(f, [notch], OUTLINE)
    return f


def frame_with(x, fill_pct, green=True):
    return draw_meter(court(), x, MET_Y, fill_pct, green)


class _CfgWhite:
    meter_style = "Arrow2"
    meter_color = "White"
    confidence_threshold = 0.32


def _reader(**env):
    for k, v in env.items():
        os.environ[k] = v
    r = SimpleMeterReader(W, H, cfg=_CfgWhite(), require_gameplay_eligibility=True)
    r._meter_detector = AsyncMeterLocator(base=mlc.MeterContourLocator(), sync=True)
    r.set_shot_state(True, 1.0, True)
    return r


class _Clock:
    def __init__(self, t0=100.0):
        self.t = float(t0)

    def tick(self, n=1):
        self.t += FRAME_DT * n
        return self.t


def _serve(r, clk, frame):
    """One capture callback at the synthetic 60fps clock."""
    return r.detect(frame, ts=clk.tick())


# =================================================================== 1. the locator itself
def test_locator_forgets_the_ghost_position_so_a_washed_tip_must_re_earn_it(clean_env):
    """The exact live path: a leftover at 92% has no confirmable tip, so it is only ever
    accepted at conf 0.75 by co-location with the box accepted a moment ago. Forgetting the
    position refuses it -- and a genuine column re-earns acceptance on the very next frame,
    so the amnesia costs one frame and can never blind the reader."""
    loc = mlc.MeterContourLocator()
    t = 100.0
    seeded = loc.detect_box(frame_with(GHOST_X, 92, green=True), ts=t)
    assert seeded is not None and seeded[4] == pytest.approx(0.90)

    t += FRAME_DT
    washed = loc.detect_box(frame_with(GHOST_X, 92, green=False), ts=t)
    assert washed is not None and washed[4] == pytest.approx(0.75)   # co-location alone

    loc.forget_position()
    t += FRAME_DT
    assert loc.detect_box(frame_with(GHOST_X, 92, green=False), ts=t) is None

    t += FRAME_DT
    again = loc.detect_box(frame_with(GHOST_X, 92, green=False), ts=t)
    assert again is not None and again[4] == pytest.approx(0.75)     # exactly one frame lost


def test_forget_position_keeps_the_learned_identity(clean_env):
    """It is amnesia about WHERE, not a reset: the anchor and the pipeline survive."""
    loc = mlc.MeterContourLocator()
    loc.detect_box(frame_with(GHOST_X, 92), ts=100.0)
    loc._anchor = "sentinel"
    loc.forget_position()
    assert loc._last_box is None
    assert loc._tip_pending is None
    assert loc._anchor == "sentinel"


def test_async_wrapper_forwards_forget_position_without_dropping_work(clean_env):
    """AsyncMeterLocator must NOT bump its generation or clear its published result: an
    in-flight inference of the REAL meter has to survive the ghost's refusal."""
    loc = AsyncMeterLocator(base=mlc.MeterContourLocator(), sync=True)
    loc.submit(frame_with(GHOST_X, 92), 100.0)
    found, box, conf, ts = loc.latest()
    assert found and box == GHOST_BOX
    gen = loc._generation
    loc.forget_position()
    assert loc._base._last_box is None
    assert loc.latest() == (found, box, conf, ts)
    assert loc._generation == gen


# =================================================================== 2. the reader calls it
def test_press_time_stale_drop_forgets_the_locator_position(clean_env):
    """The reader's own seeds were always cleared here; the PROPOSER's were not.

    [SHIP CONFIG 2026-09-17] FRESH_AFTER_GHOST is armed explicitly because its default moved to
    OFF for ship (GHOST_FORGET, rate-limited, is the layer the ship sessions were graded with;
    this one overlaps it and has no live hours of its own). The forget itself -- what this test
    is about -- is unchanged and still ON by default.
    """
    r = _reader(ORION_READER_FRESH_AFTER_GHOST="1")
    clk = _Clock()
    for _ in range(4):
        _serve(r, clk, frame_with(GHOST_X, 92))
    assert r._meter_detector._base._last_box is not None
    assert float(r.last_fill or 0.0) >= 40.0

    r.notify_physical_shot_start(8)                 # STALE LOCK DROPPED AT PRESS
    assert r._det_state == "idle"
    assert r._meter_detector._base._last_box is None
    assert r._press_fresh_zone is not None


def test_ghost_forget_locator_flag_pins_the_layer_off(clean_env):
    r = _reader(ORION_READER_GHOST_FORGET_LOCATOR="0")
    clk = _Clock()
    for _ in range(4):
        _serve(r, clk, frame_with(GHOST_X, 92))
    r.notify_physical_shot_start(8)
    assert r._meter_detector._base._last_box is not None


# =================================================================== 3. the rapid re-press
def test_rapid_fire_locks_the_fresh_meter_not_the_ghost(clean_env):
    """The live rapid-drill shape: the previous shot's meter is still locked at ~92% when the
    next press lands, then the real meter renders in the SAME zone ~500ms later at a slightly
    different column. The published lock must be the fresh meter's box, and no read of the
    ghost may ever reach the engine."""
    r = _reader()
    clk = _Clock()
    for _ in range(4):
        _serve(r, clk, frame_with(GHOST_X, 92))     # idling on the leftover

    r.notify_physical_shot_start(9)
    published = []
    for _ in range(6):                              # leftover still rendered after the press
        res = _serve(r, clk, frame_with(GHOST_X, 92))
        if res.detected:
            published.append(tuple(res.bbox))
    for _ in range(6):                              # leftover fades out
        _serve(r, clk, court())
    while clk.t < 100.0 + 0.50:                     # dead air until a real onset can render
        _serve(r, clk, court())

    fresh = []
    for pct in (8, 14, 20, 26, 32, 38):
        res = _serve(r, clk, frame_with(FRESH_X, pct))
        if res.detected:
            fresh.append((tuple(res.bbox), round(float(res.fill_pct), 1)))

    assert fresh, "the fresh meter must be acquired and published"
    assert all(b == FRESH_BOX for b, _ in fresh), fresh
    assert all(b != GHOST_BOX for b in published), published
    fills = [f for _, f in fresh]
    assert fills == sorted(fills) and fills[-1] > fills[0]


def test_retired_ghost_zone_withholds_an_unproven_high_relock(clean_env):
    """GHOST IDENTITY RETIRED used to clear the quarantine outright, after which ANY next lock
    there published freely -- including a re-rendered leftover. A >=40% read in that zone is
    withheld until this press sights its own meter low and rising (the engine's ownership
    anchor refuses a first sight above 40 anyway, so nothing ownable is lost).

    [SHIP CONFIG 2026-09-17] Armed explicitly: this layer's default moved to OFF for ship (see
    test_press_time_stale_drop_forgets_the_locator_position). The mechanism is unchanged.
    """
    r = _reader(ORION_READER_FRESH_AFTER_GHOST="1")
    clk = _Clock()
    for _ in range(4):
        _serve(r, clk, frame_with(GHOST_X, 92))
    r.notify_physical_shot_start(9)
    press_t = clk.t
    for _ in range(8):                              # ghost fades -> identity retired
        _serve(r, clk, court())
    assert r._press_ghost_zone is None              # retired
    assert r._press_fresh_zone is not None          # ...but the requirement stands
    while clk.t < press_t + 0.50:                   # dead air: a real onset could render now
        _serve(r, clk, court())

    # A second leftover re-renders in the zone and FADES (spread far wider than
    # _ghost_press_spread_pp), so the 3-frame static window can never re-identify it, and at
    # this press age the onset clock allows every one of these reads. Before this layer,
    # everything past the two cold-veto frames published.
    seen = []
    for pct in (92, 88, 84, 80, 76, 72, 68, 64):
        seen.append(_serve(r, clk, frame_with(GHOST_X, pct)))
    assert not any(x.detected for x in seen), [(x.detected, round(float(x.fill_pct), 1))
                                               for x in seen]
    assert any(x.rejection_reason == "ghost_zone_unproven" for x in seen), \
        [x.rejection_reason for x in seen]


def test_fresh_after_ghost_flag_decides_the_layer_in_both_directions(clean_env):
    """[SHIP CONFIG 2026-09-17] OFF is now the DEFAULT, so this pins both ends of the switch --

    an explicit "0" and a scrubbed environment must both leave the layer inert, and "1" must
    still arm it, so neither direction can rot into a no-op.
    """
    for value, expect_zone in (("0", False), (None, False), ("1", True)):
        env = {} if value is None else {"ORION_READER_FRESH_AFTER_GHOST": value}
        if value is None:
            os.environ.pop("ORION_READER_FRESH_AFTER_GHOST", None)
        r = _reader(**env)
        clk = _Clock()
        for _ in range(4):
            _serve(r, clk, frame_with(GHOST_X, 92))
        r.notify_physical_shot_start(9)
        assert (r._press_fresh_zone is not None) is expect_zone, value


# =================================================================== 4. static-zone quarantine
def _static_cycle(r, clk, fill=30, reads=5, gap=8, x=GHOST_X):
    """Lock a column that never rises, then starve the locator so the lock dies."""
    for _ in range(reads):
        _serve(r, clk, frame_with(x, fill))
    for _ in range(gap):
        _serve(r, clk, court())


def test_static_column_is_locked_at_most_once_until_it_rises(clean_env):
    """The owner's court-line false locks: ~20 lock/drop cycles in 40s with no press. A drop
    whose lock never rose arms NO warm seed and scores a strike; at _static_zone_strikes the
    position publishes nothing until a lock there proves a rise."""
    r = _reader()
    r.set_shot_state(False, 0.0, False)             # no press anywhere: the idle case
    clk = _Clock()

    _static_cycle(r, clk)
    assert len(r._static_zones) == 1 and r._static_zones[0]["n"] == 1
    # The FIRST never-risen drop still arms the warm memory: a genuine mid-shot dropout has to
    # re-latch on one proposal (pinned in test_simple_reader_lock_lifecycle).
    assert r._det_warm_pos is not None

    _static_cycle(r, clk)
    assert r._static_zones[0]["n"] >= 2             # repeat offender -> quarantined
    assert r._det_warm_pos is None, "a quarantined position must not re-seed from its corpse"

    seen = [_serve(r, clk, frame_with(GHOST_X, 30)) for _ in range(5)]
    assert not any(x.detected for x in seen)
    assert any(x.rejection_reason == "static_zone_quarantined" for x in seen), \
        [x.rejection_reason for x in seen]


def test_static_zone_releases_the_moment_a_meter_rises_there(clean_env):
    """A real meter renders in a quarantined zone: it rises IN ORDER, so the record is retired
    and the zone is ordinary again. Idle (no press), the cost is the _fresh_up_req() frames the
    ordered proof takes; under an armed press it is still the one frame past the first rise."""
    r = _reader()
    r.set_shot_state(False, 0.0, False)
    clk = _Clock()
    _static_cycle(r, clk)
    _static_cycle(r, clk)
    assert r._static_zones and r._static_zones[0]["n"] >= 2

    published = []
    for pct in (6, 12, 18, 24, 30, 36, 42):
        res = _serve(r, clk, frame_with(GHOST_X, pct))
        if res.detected:
            published.append(round(float(res.fill_pct), 1))
    assert published, "a rising meter must escape the quarantine"
    assert not r._static_zones                      # record retired
    assert published == sorted(published)


def test_static_zone_ignores_one_idle_jitter_read(clean_env):
    """Live 2026-09-21: five quarantine releases, none of them a meter. One jittery read
    >= _static_zone_rise_pp above the lock's first read used to "prove a rise" and retire the
    record; the same spot was re-quarantined 1-2 s later. With no press armed the release now
    needs the ORDERED proof: _fresh_up_req() consecutive reads each _fresh_up_pp higher."""
    r = _reader()
    r.set_shot_state(False, 0.0, False)
    clk = _Clock()
    _static_cycle(r, clk)
    _static_cycle(r, clk)
    assert r._static_zones and r._static_zones[0]["n"] >= 2

    seen = [_serve(r, clk, frame_with(GHOST_X, pct)) for pct in (30, 33, 30, 30, 33, 30)]
    assert not any(x.detected for x in seen), [(x.detected, round(float(x.fill_pct), 1))
                                               for x in seen]
    assert any(x.rejection_reason == "static_zone_quarantined" for x in seen),         [x.rejection_reason for x in seen]
    assert r._static_zones, "a jitter read is not a rise: the record must survive"


def test_static_zone_releases_for_an_armed_press_on_the_first_rise(clean_env):
    """A real shot whose meter lands in a quarantined zone pays exactly what it paid before the
    2026-09-21 tightening: with the physical press armed, the FIRST read >= _static_zone_rise_pp
    releases the record -- it does not have to spend _fresh_up_req() frames proving order.

    No dead air before the meter renders: 30 consecutive full-frame nofinds DECAY a strike
    (_note_static_zone_absence), which would lift the quarantine on its own and make this test
    pass without the armed path ever running. Releasing empties the list; decaying would leave
    a record at n=1, so the emptiness assert is what separates the two."""
    r = _reader()
    r.set_shot_state(False, 0.0, False)
    clk = _Clock()
    _static_cycle(r, clk)
    _static_cycle(r, clk)
    assert r._static_zones and r._static_zones[0]["n"] >= 2

    r.set_shot_state(True, 1.0, True)
    r.notify_physical_shot_start(31)
    # 18% is the first fill the locator proposes a box for; 24% is +5.7pp on it, so the armed
    # release fires on the SECOND read -- one frame past the rise, as before.
    # Exactly two frames: long enough for the armed release, far too short for the ordered
    # proof (_fresh_up_req() == 3 rising reads), so a pass can only come from the armed escape.
    seen = [_serve(r, clk, frame_with(GHOST_X, pct)) for pct in (18, 24)]
    first_pub = next((i for i, x in enumerate(seen) if x.detected), None)
    assert first_pub == 1, \
        [(x.detected, x.rejection_reason, round(float(x.fill_pct), 1)) for x in seen]
    assert r._static_zones == [], "the armed release must RETIRE the record, not decay it"
    assert r._det_lock_up_n < r._fresh_up_req(), \
        "the ordered proof must not have been satisfied -- this pins the ARMED escape"


def test_repeat_offender_zone_keeps_a_longer_quarantine(clean_env):
    """The left-edge HUD element at (85, 526-557) was quarantined four times in four minutes:
    the record expired after the base TTL and the object had to be caught lying twice more
    each time. A repeat offender gets a doubled TTL per prior quarantine of its cell."""
    r = _reader()
    r.set_shot_state(False, 0.0, False)
    clk = _Clock()
    base = float(r._static_zone_ttl_s)
    _static_cycle(r, clk)
    _static_cycle(r, clk)
    assert r._static_zones and r._static_zones[0]["n"] >= 2
    assert r._static_zones[0]["ttl"] == pytest.approx(base)     # first offence: base TTL

    clk.tick(int((base + 5.0) * 60))                             # let the record expire
    _static_cycle(r, clk)
    _static_cycle(r, clk)
    assert r._static_zones and r._static_zones[0]["n"] >= 2
    assert r._static_zones[0]["ttl"] == pytest.approx(2.0 * base)

    clk.tick(int((base + 5.0) * 60))                             # past the BASE ttl only
    seen = [_serve(r, clk, frame_with(GHOST_X, 30)) for _ in range(4)]
    assert not any(x.detected for x in seen), "the doubled TTL must still be holding"
    assert any(x.rejection_reason == "static_zone_quarantined" for x in seen)

    # The ledger forgets a cell after _static_zone_repeat_forget_s; a real meter clears it.
    cx, cy = r._static_zones[0]["cx"], r._static_zones[0]["cy"]
    assert r._static_zone_repeat_ttl(cx, cy, clk.t + r._static_zone_repeat_forget_s + 1.0)         == pytest.approx(base)
    r._static_zone_release(r._static_zones[0], "test")
    assert r._static_zone_repeat_ttl(cx, cy, clk.t) == pytest.approx(base)


def test_repeat_offender_escalation_flag_pins_it_off(clean_env):
    r = _reader(ORION_READER_STATIC_ZONE_REPEAT_ESCALATE="0")
    r.set_shot_state(False, 0.0, False)
    clk = _Clock()
    base = float(r._static_zone_ttl_s)
    for _ in range(2):
        _static_cycle(r, clk)
        _static_cycle(r, clk)
        assert r._static_zones and r._static_zones[0]["ttl"] == pytest.approx(base)
        clk.tick(int((base + 5.0) * 60))


def test_idle_proposals_never_meet_the_anchor_and_are_counted(clean_env):
    """[ANCHOR INSTRUMENT 2026-09-21] _update_anchor is skipped whenever no press is armed, so
    an idle proposal reaches the reader with no geometric gate at all. The counters make that
    visible: idle_hit counts those proposals and the anchor_* histogram stays at zero for them;
    an ARMED evaluation lands in exactly one bucket (this court has no nameplate -> none)."""
    r = _reader()
    base = r._meter_detector._base
    r.set_shot_state(False, 0.0, False)
    clk = _Clock()
    for _ in range(3):
        _serve(r, clk, frame_with(GHOST_X, 30))
    assert base.stats["idle_hit"] >= 1
    assert base.stats["idle_hit"] == base.stats["hit"]
    buckets = ("anchor_none", "anchor_conf_lo", "anchor_conf_mid", "anchor_conf_hi")
    assert sum(base.stats[k] for k in buckets) == 0, "idle frames must never be evaluated"

    idle_before = base.stats["idle_hit"]
    r.set_shot_state(True, 1.0, True)
    r.notify_physical_shot_start(41)
    for _ in range(3):
        _serve(r, clk, frame_with(GHOST_X, 30))
    assert base.stats["idle_hit"] == idle_before, "armed proposals are not idle"
    assert sum(base.stats[k] for k in buckets) >= 1
    assert base.stats["anchor_none"] >= 1        # no plate on the synthetic court

    line = r._proposer_stats()
    for key in ("idle_hit=", "refused_outside=", "anchor_patch_hit=", "anchor_none="):
        assert key in line, line


def test_static_zone_repeat_withholds_are_counted(clean_env):
    """The proposer handing the same quarantined object back frame after frame (live epoch
    60: 90 withheld reads, meter sighted 1499 ms late) shows up as staticq_repeat."""
    r = _reader()
    r.set_shot_state(False, 0.0, False)
    clk = _Clock()
    _static_cycle(r, clk)
    _static_cycle(r, clk)
    assert r._static_zones and r._static_zones[0]["n"] >= 2
    for _ in range(6):
        _serve(r, clk, frame_with(GHOST_X, 30))
    assert r._static_zone_withheld >= 4
    assert r._static_zone_withheld_repeat >= 3
    assert r._static_zone_withheld_repeat < r._static_zone_withheld


def test_static_zone_quarantine_flag_pins_the_layer_off(clean_env):
    r = _reader(ORION_READER_STATIC_ZONE_QUARANTINE="0")
    r.set_shot_state(False, 0.0, False)
    clk = _Clock()
    _static_cycle(r, clk)
    _static_cycle(r, clk)
    assert r._static_zones == []
    seen = [_serve(r, clk, frame_with(GHOST_X, 30)) for _ in range(4)]
    assert any(x.detected for x in seen)


# =================================================================== 5. the box latch
class _Proposals:
    """Hand-fed proposals: the teleport re-seed needs three CONSISTENT far results, which a
    drawn frame cannot express without also moving the meter the reader is measuring."""
    provider = "fake"
    infer_ms = 0.0
    ok = True
    scope = "full"

    def __init__(self):
        self.res = (False, None, 0.0, -1.0)
        self.forgotten = 0

    def submit(self, frame, ts):
        pass

    def latest(self):
        return self.res

    def latest_details(self):
        found, box, conf, ts = self.res
        return found, box, conf, ts, self.scope

    def forget_position(self):
        # deliberately WITHOUT `box=`: a proposer that predates the surgical forget must still
        # be driven (the reader falls back to the old total amnesia on the TypeError).
        self.forgotten += 1


def _stub_reader(**env):
    for k, v in env.items():
        os.environ[k] = v
    r = SimpleMeterReader(W, H, cfg=_CfgWhite(), require_gameplay_eligibility=True)
    r._meter_detector = _Proposals()
    r.set_shot_state(True, 1.0, True)
    return r


def _stub_serve(r, clk, box, frame):
    ts = clk.tick()
    r._meter_detector.res = (True, box, 0.9, ts)
    return r.detect(frame, ts=ts)


def _own_the_shot(r, clk):
    """Press, then a genuine rising onset at a live-measured press age -> the engine owns it."""
    r.notify_physical_shot_start(11)
    r._meter_detector.res = (False, None, 0.0, clk.t)
    r.detect(court(), ts=clk.tick())                # latch the press clock
    while clk.t < 100.0 + 0.50:
        r._meter_detector.res = (False, None, 0.0, clk.t)
        r.detect(court(), ts=clk.tick())
    for pct in (8, 14, 20, 26):
        _stub_serve(r, clk, GHOST_BOX, frame_with(GHOST_X, pct))


def test_midshot_reseed_is_refused_while_the_engine_owns_the_shot(clean_env):
    """Three consistent far proposals adopt a brand-new lock in _det_on_found -- silently, with
    no drop for the engine to see -- which swaps the box the fill denominator is measured
    against mid-shot. While the latch is live that re-seat is refused."""
    r = _stub_reader()
    clk = _Clock()
    _own_the_shot(r, clk)
    assert r._press_low_seen
    assert r._box_latch_epoch == 11
    latched = r._box_latch_box
    assert latched is not None

    far = (960, 304, 26, 107)
    for _ in range(6):
        _stub_serve(r, clk, far, frame_with(GHOST_X, 32))
    assert r._box_latch_refused >= 1
    assert r._box_latch_box == latched
    assert r._det_diag.get("reseed", 0) == 0


def test_a_real_drop_clears_the_latch_so_the_engine_sees_the_break(clean_env):
    """The latch forbids a SILENT swap, never a genuine break: a drop still goes through
    _det_drop_lock and clears it."""
    r = _stub_reader()
    clk = _Clock()
    _own_the_shot(r, clk)
    assert r._box_latch_epoch == 11
    for _ in range(6):                              # confident full-frame no-meter verdicts
        ts = clk.tick()
        r._meter_detector.res = (False, None, 0.0, ts)
        r.detect(court(), ts=ts)
    assert r._det_state == "idle"
    assert r._box_latch_epoch == 0


def test_box_latch_flag_pins_the_layer_off(clean_env):
    r = _stub_reader(ORION_READER_BOX_LATCH="0")
    clk = _Clock()
    _own_the_shot(r, clk)
    assert r._box_latch_epoch == 0
    far = (960, 304, 26, 107)
    for _ in range(6):
        _stub_serve(r, clk, far, frame_with(GHOST_X, 32))
    assert r._box_latch_refused == 0


# =================================================================== 6. the forget RATE LIMIT
#
# [ORION_READER_FORGET_RATE_LIMIT 2026-09-16] THE FIX ABOVE, UNRATED, CAUSED A BLIND RUN.
#
# Live 09-16 14:20:46-14:21:11, epochs 22-27: SIX consecutive presses with no vision sample at
# all (every one backstopped blind). DETECTOR HEALTH across the window: `loc_forget` 0 -> 148
# climbing in lockstep with `locks` while `drops` stayed FLAT (39 locks / 42 seeds / 39 forgets
# in 4 s) -- ~10 evictions per SECOND while the previous shot's meter sat on screen through a
# rapid re-press. Every one of those called forget_position(), which then wiped the locator's
# TWO-FRAME PROMOTION PAIRS along with the ghost's box, so the REAL meter's first sight never
# survived to its second frame: first publication landed 100-170 ms late (BOX LATCHED 810-912 ms
# after the press against the 750 ms deadline). The framedump (8.8 fps, 0.76 evictions/press)
# structurally cannot reproduce that rate, which is why the offline replay said "0 diff".
#
# Two bounds: ONE forget per press per zone, and never while a pair is mid-promotion.
def _rapid_ghost_press(r, clk, epoch=9, ghost_frames=60, onset=(8, 12, 16, 20, 24, 28, 32, 36)):
    """The live shape in miniature: a leftover on screen across a press at 60 fps (one eviction
    per frame -- 10x the framedump's rate), then this press's own meter rendering nearby.
    -> the frame index of the onset's FIRST publication (None if it never published)."""
    for _ in range(4):
        _serve(r, clk, frame_with(GHOST_X, 92))
    r.notify_physical_shot_start(epoch)
    for _ in range(ghost_frames):
        _serve(r, clk, frame_with(GHOST_X, 92))
    first = None
    for i, pct in enumerate(onset):
        res = _serve(r, clk, frame_with(FRESH_X, pct))
        if res.detected and first is None:
            first = i
    return first


def test_a_press_long_eviction_storm_costs_exactly_one_forget(clean_env, monkeypatch):
    """(c) 60 evictions of the SAME ghost in one press -> one forget, 59 no-ops. The second
    eviction of a zone can only destroy memory formed SINCE the first, which by definition is
    not the ghost's."""
    r = _reader()
    first_on = _rapid_ghost_press(r, _Clock())
    assert r._loc_forget_n == 1, r._loc_forget_n
    assert r._loc_forget_suppressed >= 50                      # the storm was absorbed
    assert r._det_diag.get("loc_forget", 0) == 1               # and the health counter agrees

    monkeypatch.setenv("ORION_READER_GHOST_FORGET_LOCATOR", "0")
    first_off = _rapid_ghost_press(_reader(), _Clock())
    assert first_on is not None and first_off is not None
    assert first_on <= first_off, (first_on, first_off)        # the layer costs no frame


def test_the_rate_limit_flag_restores_the_unrated_call(clean_env, monkeypatch):
    monkeypatch.setenv("ORION_READER_FORGET_RATE_LIMIT", "0")
    r = _reader()
    _rapid_ghost_press(r, _Clock())
    assert r._loc_forget_n > 50, "pinned off, every eviction forgets again (the 09-16 shape)"
    assert r._loc_forget_suppressed == 0


def test_one_forget_per_zone_not_one_per_press(clean_env):
    """A DIFFERENT object refused in the same press is a different fact and gets its own
    forget; the same object refused again does not. A new press re-opens both."""
    r = _reader()
    r._physical_shot_epoch = 11
    ghost_a = (600, 300, 26, 120)
    ghost_b = (900, 300, 26, 120)                              # 300 px away: another object
    assert r._det_forget_locator_position('evict', box=ghost_a, ts=100.00) is True
    assert r._det_forget_locator_position('evict', box=ghost_a, ts=100.02) is False
    assert r._det_forget_locator_position('evict', box=ghost_b, ts=100.03) is True
    assert (r._loc_forget_n, r._loc_forget_suppressed) == (2, 1)
    r._physical_shot_epoch = 12
    assert r._det_forget_locator_position('evict', box=ghost_a, ts=100.05) is True
    assert r._loc_forget_n == 3


def test_a_forget_never_interrupts_a_pair_that_is_about_to_decide(clean_env, monkeypatch):
    """(c) the second bound. A first sight is one frame from promotion; the eviction waits for
    it and fires afterwards, if the ghost is still there."""
    r = _reader()
    r._physical_shot_epoch = 11
    live = ["tipless"]
    monkeypatch.setattr(r._meter_detector, "pending_pairs",
                        lambda ts=None: tuple(live), raising=False)
    ghost = (600, 300, 26, 120)
    assert r._det_forget_locator_position('ghost_press_evict', box=ghost, ts=100.0) is False
    assert (r._loc_forget_n, r._loc_forget_deferred) == (0, 1)
    live.clear()                                               # the pair decided
    assert r._det_forget_locator_position('ghost_press_evict', box=ghost, ts=100.02) is True
    assert r._loc_forget_n == 1


def test_a_self_renewing_pair_cannot_hold_the_forget_off_for_ever(clean_env, monkeypatch):
    """Fail-safe on the deferral: an object that keeps minting first sights of itself must not
    be able to buy permanent immunity from eviction."""
    r = _reader()
    r._physical_shot_epoch = 11
    monkeypatch.setattr(r._meter_detector, "pending_pairs",
                        lambda ts=None: ("pending",), raising=False)
    ghost = (600, 300, 26, 120)
    for i in range(r._forget_defer_max):
        assert r._det_forget_locator_position('evict', box=ghost, ts=100.0 + i * 0.016) is False
    assert r._det_forget_locator_position('evict', box=ghost, ts=101.0) is True


def test_the_forget_logs_one_error_line_per_press(clean_env, caplog):
    """The census the live log needs: one ERROR line naming the zone and what was KEPT."""
    import logging
    r = _reader()
    r._physical_shot_epoch = 11
    with caplog.at_level(logging.ERROR, logger="simple_reader"):
        r._det_forget_locator_position('ghost_press_evict', box=(600, 300, 26, 120), ts=100.0)
        r._det_forget_locator_position('ghost_press_evict', box=(900, 300, 26, 120), ts=100.02)
    lines = [m for m in caplog.messages if m.startswith("LOCATOR POSITION FORGOTTEN")]
    assert len(lines) == 1, lines
    assert lines[0] == ("LOCATOR POSITION FORGOTTEN: epoch=11 zone=(613,360) kept_pairs=- "
                        "why=ghost_press_evict")


# =================================================================== 7. the live counters
#
# [ORION_READER_HEALTH_FIELD_ORDER / _PRESS_WITHHOLD_SUMMARY 2026-09-16] The 09-16 blind run
# took a day to attribute because three of the six publication layers had no number that
# survived to the native log: RemotePlaySession relays `trimmed.left(300)` of every sidecar
# WARNING/ERROR, which cut DETECTOR HEALTH in the middle of `lifecycle(...)` -- `idle_unpublished=`
# and `idle_reuse=` were never once visible -- and STATIC ZONE WITHHELD / RESEED REFUSED (repeat)
# / COLD FIRST-READ VETOED are DEBUG lines that never leave the sidecar at all.
RELAY_TRIM = 300
LOG_PREFIX = len("2026-09-16 14:20:46,123 ERROR simple_reader: ")


def test_health_line_keeps_every_layer_counter_inside_the_relay_trim(clean_env, caplog):
    """(d) field order. Everything that can WITHHOLD a read is in the first ~150 chars; the
    descriptive lifecycle/scan census stays behind it, where the trim costs nothing."""
    import logging
    r = _reader()
    clk = _Clock()
    with caplog.at_level(logging.ERROR, logger="simple_reader"):
        _serve(r, clk, frame_with(GHOST_X, 40))
    lines = [m for m in caplog.messages if m.startswith("DETECTOR HEALTH")]
    assert lines, caplog.messages
    line = lines[-1]
    head = line[:RELAY_TRIM - LOG_PREFIX]          # what the native relay actually forwards
    fields = ["loc_forget=", "reseed_refused=", "staticq=", "staticq_repeat=", "anchor=",
              "idle_unpublished=", "idle_reuse=", "press_fresh_withheld=", "tipless="]
    where = [head.index(f) for f in fields]        # ValueError here = trimmed away again
    assert where == sorted(where), list(zip(fields, where))
    assert head.index("provider=") < where[0]
    assert line.index("lifecycle(") > where[-1]
    assert line.index("cv={") > where[-1]


def test_press_close_emits_one_withhold_summary(clean_env, caplog):
    """(d) the summary line. One ERROR per press, idempotent, carrying every layer."""
    import logging
    r = _reader()
    r.notify_physical_shot_start(21)
    r._pw_ghost_static, r._pw_cold_first, r._pw_idle_gate = 7, 1, 3
    r._pw_fresh_zone, r._pw_static_zone, r._pw_reseed = 2, 5, 4
    with caplog.at_level(logging.ERROR, logger="simple_reader"):
        r.notify_physical_shot_release(21, 640.0, "release")
        r.notify_physical_shot_release(21, 640.0, "release")
    hits = [m for m in caplog.messages if m.startswith("PRESS WITHHOLD SUMMARY")]
    assert hits == ["PRESS WITHHOLD SUMMARY: epoch=21 ghost_static=7 cold_first=1 idle_gate=3 "
                    "fresh_zone=2 static_zone=5 reseed=4 end=release"]


def test_a_press_that_withheld_nothing_says_nothing(clean_env, caplog):
    import logging
    r = _reader()
    r.notify_physical_shot_start(22)
    with caplog.at_level(logging.ERROR, logger="simple_reader"):
        r.notify_physical_shot_release(22, 640.0, "release")
    assert not [m for m in caplog.messages if m.startswith("PRESS WITHHOLD SUMMARY")]


def test_a_press_closed_by_the_next_press_still_reports(clean_env, caplog):
    """An aborted/cancelled press never issues a release marker; the next arm closes its books."""
    import logging
    r = _reader()
    r.notify_physical_shot_start(31)
    r._pw_ghost_static = 4
    with caplog.at_level(logging.ERROR, logger="simple_reader"):
        r.notify_physical_shot_start(32)
    hits = [m for m in caplog.messages if m.startswith("PRESS WITHHOLD SUMMARY")]
    assert hits == ["PRESS WITHHOLD SUMMARY: epoch=31 ghost_static=4 cold_first=0 idle_gate=0 "
                    "fresh_zone=0 static_zone=0 reseed=0 end=next_press"]
    assert r._pw_epoch == 32 and r._pw_ghost_static == 0


def test_the_ghost_breaker_feeds_the_withhold_census(clean_env):
    """The counters are not decoration: a real eviction storm shows up in ghost_static."""
    r = _reader()
    _rapid_ghost_press(r, _Clock(), epoch=41)
    assert r._pw_ghost_static > 0


def test_a_proposer_without_the_box_argument_still_forgets(clean_env):
    """Back-compat: the ONNX/stub proposers take no `box=`. The reader must not silently stop
    forgetting because of a signature -- it falls back to the original total amnesia."""
    r = _stub_reader()
    r._physical_shot_epoch = 11
    assert r._det_forget_locator_position('evict', box=(600, 300, 26, 120), ts=100.0) is True
    assert r._meter_detector.forgotten == 1
    assert r._loc_forget_n == 1
