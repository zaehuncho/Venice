"""Unit coverage for the two 2026-08-06 reader acquisition flags (both default OFF).

P1  ORION_READER_POST_RELEASE_YIELD  -- post-release lock yield: once OUR OWN release has been
    relayed (notify_release) and the lock's fill has sat static at/above 90 for the configured
    number of emissions, the first would-be meter_memory coast frame force-unlocks to hunting
    instead of coasting the spent echo.
P2  ORION_READER_FRESH_RISE_STEPS_ARMED -- armed-window override of the N-consecutive-rising-
    frames proof count; unarmed/off-shot frames always keep the base rule.

Each ON behaviour is pinned alongside its OFF (shipped) twin so a silent default flip fails here,
not on a counted live batch. Frames are the synthetic park meter from test_simple_meter_reader.
"""
import numpy as np

from simple_meter_reader import SimpleMeterReader

H, W = 1080, 1920
RED_A = (0, 0, 255)
RED_B = (40, 40, 255)
GREEN = (60, 200, 60)
FLOOR_Y = 600
TRACK_TOP_Y = 470
COL_X = 900
COL_W = 24
BLANK = np.full((H, W, 3), 40, np.uint8)


def _frame(fill_frac=1.0, green=True, bg=40):
    f = np.full((H, W, 3), bg, np.uint8)
    track_h = FLOOR_Y - TRACK_TOP_Y
    red_top = int(round(FLOOR_Y - fill_frac * track_h))
    half = COL_W // 2
    if fill_frac > 0:
        f[red_top:FLOOR_Y, COL_X:COL_X + half] = RED_A
        f[red_top:FLOOR_Y, COL_X + half:COL_X + COL_W] = RED_B
    if green:
        f[TRACK_TOP_Y - 6:TRACK_TOP_Y, COL_X:COL_X + COL_W] = GREEN
    return f


def _drive_release_freeze(reader, *, release=True, frozen_frames=14):
    """Rise the meter to near-full, then freeze it static; relay notify_release after the
    first frozen frame when `release`. Returns the next ts to use."""
    i = 0
    for frac in (0.30, 0.40, 0.50, 0.62, 0.74, 0.86):
        res = reader.detect(_frame(frac), ts=i / 60.0)
        assert res.detected, f"rise frame {i} must detect"
        i += 1
    frozen = _frame(0.97)
    for k in range(frozen_frames):
        res = reader.detect(frozen, ts=i / 60.0)
        assert res.detected, f"frozen frame {k} must detect"
        assert res.fill_pct >= 90.0
        i += 1
        if release and k == 0:
            reader.notify_release(seq=1)
    return i / 60.0


def test_post_release_yield_default_off_coasts_meter_memory():
    """SHIPPED default: the spent frozen meter is coasted as meter_memory when it vanishes.
    This leg failing means the P1 default silently flipped ON."""
    r = SimpleMeterReader(W, H)
    assert r._pr_yield is False
    ts = _drive_release_freeze(r)
    res = r.detect(BLANK, ts=ts)
    assert res.detected is True
    assert res.rejection_reason == "meter_memory"
    assert r._pr_yield_n == 0


def test_post_release_yield_unlocks_instead_of_coasting(monkeypatch):
    """Flag ON: same sequence -> the first would-be coast frame drops the lock to hunting
    (canonical no_meter emission), exactly once per release."""
    monkeypatch.setenv("ORION_READER_POST_RELEASE_YIELD", "1")
    r = SimpleMeterReader(W, H)
    assert r._pr_yield is True
    ts = _drive_release_freeze(r)
    assert r._pr_release_pending is True
    assert r._pr_frozen_n >= r._pr_yield_frames
    res = r.detect(BLANK, ts=ts)
    assert res.detected is False, "the yield must drop the lock, not coast the echo"
    assert res.rejection_reason == "roi_not_found"
    assert r.box is None, "the reader must be hunting (no lock) after the yield"
    assert r._pr_yield_n == 1
    assert r._pr_release_pending is False, "one yield per release"
    # ...and the very next real meter is acquirable cold (hunting actually works).
    res2 = r.detect(_frame(0.25), ts=ts + 1 / 60.0)
    assert res2.detected is True


def test_post_release_yield_never_fires_mid_rise(monkeypatch):
    """The precondition (own release + static >=90) cannot occur mid-rise: with the fill
    still climbing, a vanish keeps the shipped coast even with the flag ON and a (stray)
    release marker latched."""
    monkeypatch.setenv("ORION_READER_POST_RELEASE_YIELD", "1")
    r = SimpleMeterReader(W, H)
    i = 0
    for frac in (0.30, 0.38, 0.46, 0.54, 0.62, 0.70, 0.78, 0.86):
        res = r.detect(_frame(frac), ts=i / 60.0)
        assert res.detected
        i += 1
        if i == 2:
            r.notify_release(seq=1)   # adversarial: marker while the meter still rises
    assert r._pr_frozen_n == 0, "a rising fill must never accumulate the frozen streak"
    res = r.detect(BLANK, ts=i / 60.0)
    assert res.detected is True and res.rejection_reason == "meter_memory", \
        "mid-rise vanish must keep the shipped occlusion coast"
    assert r._pr_yield_n == 0


def test_post_release_yield_cleared_by_new_arm_edge(monkeypatch):
    """A NEW physical press ends the post-release window: its meter is never yieldable off
    the previous shot's release."""
    monkeypatch.setenv("ORION_READER_POST_RELEASE_YIELD", "1")
    r = SimpleMeterReader(W, H)
    ts = _drive_release_freeze(r)
    assert r._pr_release_pending is True
    r.set_shot_state(True, 0.0, True)         # the next physical press
    res = r.detect(_frame(0.97), ts=ts)       # arm-edge frame
    assert r._pr_release_pending is False
    assert r._pr_frozen_n == 0
    r.notify_physical_shot_start(7)           # explicit relay clears it too
    r._pr_release_pending = True
    r.notify_physical_shot_start(8)
    assert r._pr_release_pending is False


def test_fresh_rise_steps_armed_default_matches_base():
    """Default: the armed count mirrors the base count -- behaviour byte-identical, whatever
    the base is configured to."""
    r = SimpleMeterReader(W, H)
    assert r._fresh_up_min_armed == r._fresh_up_min
    r._shot_armed_hw = True
    assert r._fresh_up_req() == r._fresh_up_min
    r._shot_armed_hw = False
    assert r._fresh_up_req() == r._fresh_up_min


def test_fresh_rise_steps_armed_two_only_inside_hw_window(monkeypatch):
    """ORION_READER_FRESH_RISE_STEPS_ARMED=2 lowers the proof count ONLY while the physical
    gate vouches; off-shot frames keep the base 3-step rule (the CV self-arm cannot lower
    its own bar)."""
    monkeypatch.setenv("ORION_READER_FRESH_RISE_STEPS_ARMED", "2")
    r = SimpleMeterReader(W, H)
    assert r._fresh_up_min == 3
    assert r._fresh_up_min_armed == 2
    assert r._fresh_up_req() == 3, "unarmed frames must keep the base rule"
    r._shot_armed_hw = True
    assert r._fresh_up_req() == 2
    r._shot_armed_hw = False
    assert r._fresh_up_req() == 3
