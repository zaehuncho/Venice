"""[ORION_READER_GHOST_PRESS_BREAK] Leftover-meter (ghost) eviction at/after a press.

The measured live mechanism (session_20260830_112306, epochs 4/21/23/33/34/35/44/45 and
session_20260830_113732 epochs 12/13): 2K27 leaves the previous rep's meter rendered for
seconds between shots -- frozen at ~43-55% after an early/aborted release, ~88-95% after a
full release. The reader idles LOCKED on it and the lock bridges the next press, so the
engine's first genuine samples are a static high fill its ownership anchor rightly refuses
(anchorMaxFirstFillPct = 40), and the lock is BUSY when the real meter renders ~450-650ms
later. These tests pin the press-scoped defenses:

  (1) notify_physical_shot_start judges the RECENT nonzero fill (history), not just the
      instantaneous read -- the ghost's fill flickers to 0.0 on pan-blurred frames -- and
      the drop now kills the detector lifecycle too (state/warm/history), which used to
      re-seat the same box one frame later.
  (2) a post-press breaker evicts a lock reading a static (spread <= 2.5pp) fill at/above
      the stale floor (40%) on >= 3 nonzero frames while this press has never seen its own
      meter low; zero reads neither feed nor reset the run.
  (3) [ORION_READER_PRESS_ONSET_PLAUSIBILITY] (2026-08-31, live 04:51-04:54Z bout, all four
      detector_authority_lost_abort = epochs 32/50/51/58): the leftover meter's FADE-OUT
      declines THROUGH the low band and manufactures the guard's "real onset" signature --
      one garbage low read stood the guard down (at press ages where NO meter can render;
      capture+render alone is ~230ms, measured real onsets 226-805ms), the next read
      re-locked the fading leftover high (63.2/94.3/51.4/56.2), the engine anchored on the
      trace, the leftover finished fading to 0.0 and authority was lost. Two press-clock
      invariants now bound every pre-onset serve AT/ABOVE the engine's 40% anchor bound
      (onset clock: fill <= margin + max_rate*(age-floor); rise step: fill <= last_pub +
      margin + max_rate*dt); sub-40 serves are never withheld (genuine pop-in/carried
      serves live there). The stand-down demands a RISING pair or a sustained 3-step
      plausible rise -- which a fade/drain can never produce and a real onset always does
      -- and a rapid re-press BRIDGES a spared low/rising lock (the continuing meter).

Fail-closed edges pinned here: a genuine rising onset (in or out of zone) disarms the
guard; an epoch-less arm keeps it idle; a late mid-rise catch (occlusion, weird animation)
publishes the moment the press clock allows it; the flags pin every layer off.

Timelines are REALISTIC on purpose: the press clock is latched by a blank read at the
press instant and onsets are fed at live-measured press ages (>= ~0.4s). The old
compressed timelines (onset reads ~20ms after the press) encoded exactly the physically
impossible serve pattern that cost the four live authority losses.
"""
import numpy as np

from simple_meter_reader import SimpleMeterReader

BH, BW = 107, 24
METER_X, METER_Y = 600, 300
TRUE_BOX = (METER_X, METER_Y, BW, BH)


class _CfgWhite:
    meter_style = "Arrow2"
    meter_color = "White"
    confidence_threshold = 0.32


def _meter_patch(fill_top=40, green=True):
    """Arrow2-ish meter: white fill from row `fill_top` down to the base at ~row 100.
    fill_top=40 reads ~62% (the ghost band); fill_top=None draws NO white fill at all
    (the pan-blur read: the box is held but the ribbon reads 0.0)."""
    p = np.zeros((BH, BW, 3), np.uint8)
    p[:] = (30, 70, 140)
    if fill_top is not None:
        p[fill_top:100] = (250, 252, 253)
    p[100:103] = (168, 170, 171)
    p[103:] = (100, 105, 110)
    p[:, :3] = (60, 60, 65)
    p[:, BW - 3:] = (60, 60, 65)
    if green:
        p[2:4, 4:BW - 4] = (60, 200, 60)
    return p


def _frame(fill_top=40, green=True):
    f = np.full((720, 1280, 3), 35, np.uint8)
    f[METER_Y:METER_Y + BH, METER_X:METER_X + BW] = _meter_patch(fill_top, green)
    return f


class FakeLoc:
    provider = "fake"
    infer_ms = 0.0
    ok = True
    scope = "full"

    def __init__(self):
        self.res = (False, None, 0.0, -1.0)

    def submit(self, frame, ts):
        pass

    def latest(self):
        return self.res

    def latest_details(self):
        found, box, conf, ts = self.res
        return found, box, conf, ts, self.scope

    def detect_now(self, frame, ts):
        return self.res


def _reader(monkeypatch, epoch=7, **env):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    monkeypatch.setenv("ORION_METER_DETECTOR_TRACK", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    r = SimpleMeterReader(1280, 720, cfg=_CfgWhite(), require_gameplay_eligibility=True)
    r._meter_detector = FakeLoc()
    r.set_shot_state(True, 1.0, True)
    if epoch:
        r.notify_physical_shot_start(epoch)
    return r


def _feed(r, ts, fill_top=40, green=True):
    r._meter_detector.res = (True, TRUE_BOX, 0.9, ts)
    return r.detect(_frame(fill_top, green), ts=ts)


def _feed_forced_rulers(monkeypatch, r, ts, subpixel, coarse):
    """Serve one detector-fill frame whose two rulers deliberately disagree.

    The live sub-pixel estimator can sit a few pp above the row-quantized value.  Native
    ownership intentionally consumes ``raw_fill_pct`` (coarse), so press guards must make
    their 40% ownership decision on that same value.  Keeping this helper at the detector
    measure seam exercises the real production-boundary code instead of a copied predicate.
    """
    def _measure(_frame_bgr, _box, ts=None):
        r._last_fill_coarse = float(coarse)
        r._last_fill_estimator_mode = "subpixel"
        r._last_fill_estimator_generation = 91
        return (float(subpixel), (94.0, 98.0, 96.0, 4.0, 1.0, 24), 60)

    monkeypatch.setattr(r, "_measure_fill_in_box", _measure)
    r._meter_detector.res = (True, TRUE_BOX, 0.9, ts)
    return r.detect(_frame(fill_top=60), ts=ts)


def _latch_press_clock(r, ts):
    """Latch the press's arm-edge wall time with a no-detection read at the press instant
    (live, reads run continuously at 60fps so the edge trails the press by <= 1 frame)."""
    r._meter_detector.res = (False, None, 0.0, ts)
    r.detect(np.full((720, 1280, 3), 35, np.uint8), ts=ts)


def _disarm_low(r, t_press=1.00, t_onset=1.50):
    """Stand the current press's guard down with a REALISTIC genuine onset: press-clock
    latch at the press, then a rising low pair at a live-measured press age."""
    _latch_press_clock(r, t_press)
    _feed(r, t_onset, fill_top=88)              # onset low #1 (~12%)
    _feed(r, t_onset + 0.03, fill_top=84)       # onset low #2, rising (~17%) -> guard down


def test_static_high_lock_evicted_after_press(monkeypatch):
    """Three static nonzero reads at/above the stale floor with no low sighting = the
    leftover meter; the third frame is evicted and the whole lock (detector lifecycle
    included) dies without arming the warm re-acquire memory.

    STRENGTHENED 2026-08-30: the first two reads used to PUBLISH while the rolling
    window identified the ghost -- the cold first-read veto now withholds them too
    (fill >= 40 on a cold lock's first reads inside an un-low-sighted press window is
    unownable by the engine either way), so NOTHING of the leftover reaches the engine."""
    r = _reader(monkeypatch, epoch=7)
    res1 = _feed(r, 1.00)
    res2 = _feed(r, 1.02)
    assert not res1.detected and not res2.detected
    assert res1.rejection_reason == "cold_first_read_unproven"
    assert res2.rejection_reason == "cold_first_read_unproven"
    assert r._det_state == "locked"          # veto withholds, never resets
    res3 = _feed(r, 1.04)
    assert not res3.detected
    assert res3.rejection_reason == "ghost_static_press"
    assert r._det_state == "idle"
    assert r._det_last_box is None
    assert r._det_warm_pos is None
    assert r.box is None


def test_low_sighting_disarms_guard(monkeypatch):
    """A press that has seen its own meter low keeps every later high fill -- that is the
    real rise and, later, the settle plateau the release grading needs.
    STRENGTHENED 2026-08-31: the low sighting is a RISING PAIR at a realistic press age
    (a single low read is exactly what the live fade-out forged on epochs 32/50/58)."""
    r = _reader(monkeypatch, epoch=7)
    _latch_press_clock(r, 1.00)
    low1 = _feed(r, 1.50, fill_top=88)
    assert low1.detected and 0.0 < float(low1.fill_pct) <= 35.0
    low2 = _feed(r, 1.53, fill_top=84)                  # rising -> guard down
    assert low2.detected
    for k in range(6):
        res = _feed(r, 1.56 + 0.02 * k)
    assert res.detected
    assert float(res.fill_pct) >= 40.0


def test_flicker_zero_reads_do_not_shield(monkeypatch):
    """Pan-blur frames read 0.0 on the held ghost box; they must neither count as the low
    sighting nor reset the guard. STRENGTHENED 2026-08-31: the leftover's 62% reads are
    now withheld by the cold first-read veto (reads 1-2) and then by the press-clock veto
    (62% at press_age 40ms is physically impossible -- the veto also evicts the busy lock
    and arms the quarantine), so NOTHING of the leftover ever reaches the engine and the
    zone machinery still kills the re-locks."""
    r = _reader(monkeypatch, epoch=7)
    res1 = _feed(r, 1.00)                               # ghost 62%  (cold veto withholds)
    assert not res1.detected
    assert res1.rejection_reason == "cold_first_read_unproven"
    assert _feed(r, 1.02, fill_top=None).detected       # blur 0.0   (guard unmoved)
    res2 = _feed(r, 1.04)                               # ghost 62%  (press clock forbids)
    assert not res2.detected
    assert res2.rejection_reason == "press_onset_implausible"
    _feed(r, 1.06, fill_top=None)                       # blur 0.0 in-zone (held, no disarm)
    res = _feed(r, 1.08)                                # ghost 62% re-lock in-zone
    assert not res.detected
    assert res.rejection_reason == "ghost_static_press"


def test_epochless_arm_keeps_guard_idle(monkeypatch):
    """No physical shot epoch -> no press to guard; a static high lock stays published
    (pre-press idling on the leftover meter is harmless and feeds the preview)."""
    r = _reader(monkeypatch, epoch=0)
    for k in range(8):
        res = _feed(r, 1.00 + 0.02 * k)
    assert res.detected
    assert float(res.fill_pct) >= 40.0


def test_notify_drop_judges_recent_history(monkeypatch):
    """The press-time stale drop must fire even when the very last read flickered to 0.0
    (measured epochs 41/45: held ghost read 0.00 at the press instant, 88.9/45.1 one frame
    later) -- and it must kill the detector lifecycle, not just the colour-reader box."""
    r = _reader(monkeypatch, epoch=7)
    _disarm_low(r)                                      # epoch 7 saw its meter low (rising)
    for k in range(3):
        _feed(r, 1.56 + 0.02 * k)                       # ...then the leftover 62% holds
    _feed(r, 1.62, fill_top=None)                       # blur: last_fill -> 0.0
    assert float(r.last_fill or 0.0) == 0.0
    assert r._det_state == "locked"
    r.notify_physical_shot_start(8)
    assert r.box is None
    assert r._det_state == "idle"
    assert r._det_last_box is None
    assert r._det_warm_pos is None


def test_zone_quarantines_relock_and_real_onset_escapes(monkeypatch):
    """After the press-time drop seeds the quarantine, a ghost RE-LOCK at that spot is
    suppressed on its FIRST read (no noisy sub-40 frames can reach the engine -- the
    replay-measured false-episode source), while a real meter rendering in-zone escapes
    via two consecutive RISING low reads at a realistic press age."""
    r = _reader(monkeypatch, epoch=7)
    _seed_zone(r)
    res = _feed(r, 1.66)                                # ghost re-lock in-zone
    assert not res.detected
    assert res.rejection_reason == "ghost_static_press"
    res = _feed(r, 1.68)                                # still suppressed, every frame
    assert not res.detected
    # real meter renders inside the zone: onset reads low+rising on consecutive frames
    low1 = _feed(r, 2.16, fill_top=95)
    assert low1.detected and 0.0 < float(low1.fill_pct) <= 25.0
    low2 = _feed(r, 2.19, fill_top=94)
    assert low2.detected
    # guard stood down: the rise (and later the settle plateau) publishes normally
    res = _feed(r, 2.22)
    assert res.detected and float(res.fill_pct) >= 40.0


def test_level_aware_escape_frees_midband_real_onset(monkeypatch):
    """session_20260830_113732 epoch 18 regression: a ~93% leftover (the previous shot's
    meter finishing its rise) bridges the press; the real onset renders IN-ZONE reading
    ~26/31 -- above any fixed low threshold, far below the ghost's level. The level-aware
    band must publish those reads and stand the guard down on the second (rising) one."""
    r = _reader(monkeypatch, epoch=7)
    _disarm_low(r)                                      # epoch 7 saw its meter low
    for k in range(3):
        _feed(r, 1.56 + 0.02 * k, fill_top=8)           # near-full leftover (~95%) holds
    r.notify_physical_shot_start(8)                     # drop fires, zone level ~95
    res = _feed(r, 1.66, fill_top=8)                    # ghost re-lock in-zone: suppressed
    assert not res.detected
    mid1 = _feed(r, 2.16, fill_top=75)                  # real onset in-zone (~26%)
    assert mid1.detected and 25.0 < float(mid1.fill_pct) < 40.0
    mid2 = _feed(r, 2.19, fill_top=70)                  # rising (~31%) -> guard down
    assert mid2.detected
    res = _feed(r, 2.22, fill_top=55)                   # rise continues, publishes
    assert res.detected and float(res.fill_pct) > float(mid2.fill_pct)


def test_press_guards_use_native_coarse_ruler_at_ownership_boundary(monkeypatch):
    """Live epochs 23/25 regression: sub-pixel read 41.5 while native's canonical coarse
    read was 38.7.  A quarantined true onset must not become a >=40 ghost merely because
    the two estimators have different origins.  Two rising coarse reads below 40 escape
    the zone and remain available for native's two-frame ownership proof.
    """
    r = _reader(monkeypatch, epoch=7)
    _latch_press_clock(r, 1.00)
    r._press_ghost_zone = (METER_X + BW * 0.5, METER_Y + BH * 0.5)
    r._press_ghost_level = 55.0       # band floor=40: 41.5 is ghost, 38.7 is onset

    first = _feed_forced_rulers(monkeypatch, r, 1.70, subpixel=41.5, coarse=38.7)
    assert first.detected
    assert float(first.fill_pct) == 41.5
    assert float(first.raw_fill_pct) == 38.7
    assert float(r._press_last_pub_fill) == 38.7
    assert not r._press_low_seen

    second = _feed_forced_rulers(monkeypatch, r, 1.73, subpixel=42.4, coarse=39.6)
    assert second.detected
    assert float(second.raw_fill_pct) == 39.6
    assert r._press_low_seen
    assert r._press_ghost_zone is None


def test_press_clock_and_new_epoch_bridge_use_coarse_ruler(monkeypatch):
    """The same dual-ruler sample must stay sub-40 through every press gate, including
    the early serve clock and rapid re-press bridge.  The display remains sub-pixel; only
    ownership identity is canonicalized.
    """
    r = _reader(monkeypatch, epoch=7)
    _latch_press_clock(r, 1.00)
    r._det_lock_was_warm = True       # isolate press clock from cold-settle policy
    first = _feed_forced_rulers(monkeypatch, r, 1.07, subpixel=37.0, coarse=35.0)
    assert first.detected
    sample = _feed_forced_rulers(monkeypatch, r, 1.10, subpixel=41.5, coarse=38.7)
    assert sample.detected
    assert sample.rejection_reason == ""
    assert float(r._press_last_pub_fill) == 38.7

    r.notify_physical_shot_start(8)
    assert r._det_state == "locked"
    assert r._press_low_seen             # low/rising bridge, not stale-high drop
    assert r._press_ghost_zone is None


def test_static_low_prearm_lock_cannot_bridge_new_press(monkeypatch):
    """Live epoch 38 regression: a pre-existing court/player false box sat at 14.3%
    through the press. A sub-40 value is not continuity; without a recent rising pair it
    must be retired so only fresh post-arm locator/rise evidence can reach ownership.
    """
    r = _reader(monkeypatch, epoch=7)
    _latch_press_clock(r, 1.00)
    a = _feed(r, 1.50, fill_top=88)
    b = _feed(r, 1.53, fill_top=88)
    assert a.detected and b.detected
    assert not r._press_low_seen
    assert r._det_state == "locked"

    r.notify_physical_shot_start(8)
    assert not r._press_low_seen
    assert r._det_state == "idle"
    assert r._det_last_box is None
    assert r.box is None
    assert r._det_warm_pos is None

    _latch_press_clock(r, 1.56)
    low1 = _feed(r, 2.06, fill_top=95)
    low2 = _feed(r, 2.09, fill_top=91)
    assert low1.detected and low2.detected
    assert r._press_low_seen


def test_coarse_high_sample_keeps_fail_closed_press_defense(monkeypatch):
    """Canonicalization is bidirectional: emitted sub-pixel below 40 cannot hide a
    row-quantized >=40 cold sample from the existing fail-closed guard.
    """
    r = _reader(monkeypatch, epoch=7)
    _latch_press_clock(r, 1.00)
    sample = _feed_forced_rulers(monkeypatch, r, 1.10, subpixel=39.0, coarse=41.0)
    assert not sample.detected
    assert sample.rejection_reason == "cold_first_read_unproven"


def test_detector_structure_low_latch_uses_coarse_ownership_ruler(monkeypatch):
    """A valid green-cap/ribbon sample whose native coarse fill is low earns the same
    current-epoch structure stamp even when its display estimator has crossed 35%.
    """
    r = _reader(monkeypatch, epoch=7)
    _latch_press_clock(r, 1.00)
    sample = _feed_forced_rulers(monkeypatch, r, 1.50, subpixel=36.0, coarse=34.0)
    assert sample.detected
    assert sample.gameplay_structure_verified
    assert sample.gameplay_structure_epoch == 7


def test_flag_off_is_inert(monkeypatch):
    """Both press-guard flags pinned off restores raw publication of a held high lock."""
    r = _reader(monkeypatch, epoch=7,
                ORION_READER_GHOST_PRESS_BREAK="0",
                ORION_READER_PRESS_ONSET_PLAUSIBILITY="0")
    for k in range(8):
        res = _feed(r, 1.00 + 0.02 * k)
    assert res.detected
    assert float(res.fill_pct) >= 40.0


def _seed_zone(r):
    """Epoch 7 sees its meter low (rising pair at a realistic press age), the leftover
    then holds; the press-8 stale drop seeds the quarantine zone at the leftover's
    position (the shared preamble of the zone tests). Press 8's clock is latched at the
    press instant, exactly as the live 60fps read stream does."""
    _disarm_low(r)
    for k in range(3):
        _feed(r, 1.56 + 0.02 * k)
    r.notify_physical_shot_start(8)
    _latch_press_clock(r, 1.62)


def test_zero_read_hold_keeps_lock_for_real_onset(monkeypatch):
    """session_20260830_191051: 51/336 evictions carried fill=0.0 and the late cluster sat
    exactly where the real onset renders its first EMPTY frame (epochs 8/27/30/33/60...).
    An in-zone zero read must be SUPPRESSED but the lock KEPT (no cold re-acquire, no
    reseeded sub-pixel calibration) so the onset's next nonzero read classifies it."""
    r = _reader(monkeypatch, epoch=7)
    _seed_zone(r)
    res = _feed(r, 1.66, fill_top=None)                 # in-zone ZERO read
    assert not res.detected
    assert res.rejection_reason == "ghost_static_press"
    assert r._det_state == "locked"                     # lock survives the zero read
    assert r.box is not None
    low1 = _feed(r, 2.16, fill_top=95)                  # onset reads low on the SAME lock
    assert low1.detected and 0.0 < float(low1.fill_pct) <= 25.0
    low2 = _feed(r, 2.19, fill_top=94)                  # second consecutive, rising -> down
    assert low2.detected
    res = _feed(r, 2.22)                                # plateau publishes normally
    assert res.detected and float(res.fill_pct) >= 40.0


def test_zero_read_run_still_evicts_fading_ghost(monkeypatch):
    """A fading/blurred leftover keeps reading zero: after _ghost_zero_evict_n CONSECUTIVE
    zeros the lock still dies (the busy-locker protection is delayed, not removed)."""
    r = _reader(monkeypatch, epoch=7)
    _seed_zone(r)
    for k in range(2):
        res = _feed(r, 1.66 + 0.02 * k, fill_top=None)  # zeros 1-2: held
        assert not res.detected
        assert res.rejection_reason == "ghost_static_press"
        assert r._det_state == "locked"
    res = _feed(r, 1.70, fill_top=None)                 # zero 3: evicted
    assert not res.detected
    assert res.rejection_reason == "ghost_static_press"
    assert r._det_state == "idle"
    assert r.box is None
    assert r._det_warm_pos is None                      # warm memory must NOT re-latch it


def test_zero_evict_n_1_restores_instant_evict(monkeypatch):
    """ORION_READER_GHOST_ZERO_EVICT_N=1 pins the original suppress+evict-on-first-zero."""
    r = _reader(monkeypatch, epoch=7, ORION_READER_GHOST_ZERO_EVICT_N="1")
    _seed_zone(r)
    res = _feed(r, 1.66, fill_top=None)
    assert not res.detected
    assert res.rejection_reason == "ghost_static_press"
    assert r._det_state == "idle"


def test_full_absence_retires_zone_but_partial_misses_do_not(monkeypatch):
    """Live epochs 73/74 regression: the previous high meter disappeared for two full
    locator results, then the next real meter spawned in the same zone. The old identity
    must end at proven FULL-frame absence; edge-tile misses cannot make that claim.
    """
    r = _reader(monkeypatch, epoch=7)
    _seed_zone(r)
    assert r._press_ghost_zone is not None
    blank = np.full((720, 1280, 3), 35, np.uint8)

    # A fresh full-frame re-find proves the old identity is still present and resets
    # any nofind observed at the press edge before this test's absence sequence.
    held = _feed(r, 1.64)
    assert not held.detected
    assert r._press_ghost_full_nofind_n == 0

    r._meter_detector.scope = "left"
    for ts in (1.66, 1.72):
        r._meter_detector.res = (False, None, 0.0, ts)
        r.detect(blank, ts=ts)
    assert r._press_ghost_zone is not None
    assert r._press_ghost_full_nofind_n == 0

    r._meter_detector.scope = "full"
    r._meter_detector.res = (False, None, 0.0, 1.78)
    r.detect(blank, ts=1.78)
    assert r._press_ghost_zone is not None
    r._meter_detector.res = (False, None, 0.0, 1.84)
    r.detect(blank, ts=1.84)
    assert r._press_ghost_zone is None
    assert r._det_state == "idle"
    assert not r._press_low_seen

    low1 = _feed(r, 2.16, fill_top=95)
    low2 = _feed(r, 2.19, fill_top=91)
    assert low1.detected and low2.detected
    assert r._press_low_seen


def test_eviction_log_throttled_with_one_summary(monkeypatch, caplog):
    """336 ERROR lines / 62 presses spammed the throttled native relay (5.4 per press,
    every 15-20ms on epochs 29/30/56/61). Per-frame eviction STAYS (it frees the locker);
    the ERROR line fires once per ghost (zone move >64px or 0.5s re-arms it) and ONE
    summary per press carries the totals when the guard stands down."""
    import logging
    r = _reader(monkeypatch, epoch=7)
    _seed_zone(r)
    with caplog.at_level(logging.DEBUG, logger="simple_reader"):
        for k in range(12):
            res = _feed(r, 1.66 + 0.016 * k)            # in-zone ghost re-lock, every frame
            assert not res.detected
        errs = [rec for rec in caplog.records
                if rec.levelno >= logging.ERROR
                and "GHOST LOCK DROPPED POST-PRESS" in rec.getMessage()]
        assert len(errs) == 1                           # < 0.5s, same zone -> one ERROR
        # every suppressed frame is still visible at DEBUG for offline forensics
        dbg = [rec for rec in caplog.records
               if rec.levelno == logging.DEBUG
               and "GHOST LOCK DROPPED POST-PRESS" in rec.getMessage()]
        assert len(dbg) == 11
        _feed(r, 2.16, fill_top=95)                     # real onset in-zone (sub-band)
        _feed(r, 2.19, fill_top=94)                     # 2nd consecutive, rising -> down
        summ = [rec for rec in caplog.records
                if "GHOST PRESS SUMMARY" in rec.getMessage()]
        assert len(summ) == 1
        assert "epoch=8" in summ[0].getMessage()
        assert "evictions=12" in summ[0].getMessage()
    # stand-down flushed the totals: the next press arm must not re-emit
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="simple_reader"):
        r.notify_physical_shot_start(9)
        assert not [rec for rec in caplog.records
                    if "GHOST PRESS SUMMARY" in rec.getMessage()]


def test_window_expiry_summary_flushes_at_next_press(monkeypatch, caplog):
    """A press whose guard never stands down (leftover on screen the whole window) still
    gets exactly one summary -- emitted at the NEXT press arm."""
    import logging
    r = _reader(monkeypatch, epoch=7)
    _seed_zone(r)
    with caplog.at_level(logging.DEBUG, logger="simple_reader"):
        for k in range(5):
            _feed(r, 1.66 + 0.016 * k)                  # evictions, no stand-down
        r.notify_physical_shot_start(9)
        summ = [rec for rec in caplog.records
                if "GHOST PRESS SUMMARY" in rec.getMessage()]
        assert len(summ) == 1
        assert "epoch=8" in summ[0].getMessage()
        assert "evictions=5" in summ[0].getMessage()
        assert "end=window_end" in summ[0].getMessage()


def test_cold_first_read_veto_spares_stick_first_sight(monkeypatch):
    """[ORION_READER_COLD_FIRST_READ_VETO] session_20260830_191051 epochs 46/52: a
    stick-shot's first cold-lock read measured 72.6/73.6 on the raw proposal box, then
    corrected to ~11-13 one frame later -- and the engine's ownership anchor (first
    sight > 40) refused the whole press on the garbage frame. The veto withholds it so
    the first PUBLISHED sight is the ownable low read."""
    r = _reader(monkeypatch, epoch=7)
    _latch_press_clock(r, 1.00)
    bad = _feed(r, 1.50)                     # misread ~62% on the fresh cold lock
    assert not bad.detected
    assert bad.rejection_reason == "cold_first_read_unproven"
    assert r._det_state == "locked"          # withheld, not reset
    good = _feed(r, 1.53, fill_top=95)       # corrected low read publishes at once
    assert good.detected and 0.0 < float(good.fill_pct) <= 25.0
    res = _feed(r, 1.56, fill_top=90)        # rising pair -> guard down; rise publishes
    assert res.detected


def test_cold_first_read_veto_inert_outside_press(monkeypatch):
    """Press-scoped: an idle (epoch-less) cold lock on a mid/high fill publishes its
    first read exactly as before (preview/idle overlay must not regress)."""
    r = _reader(monkeypatch, epoch=0)
    res = _feed(r, 1.00)
    assert res.detected
    assert float(res.fill_pct) >= 40.0


def test_cold_first_read_veto_warm_relock_exempt(monkeypatch):
    """A WARM re-acquire continues a recently dropped lock mid-shot; its first read is
    trusted (the sub-pixel state it resumes is the same meter's) and must publish --
    at a press age the clock allows (a 62% warm read 550ms after the press is a
    legitimate mid-rise continuation; the same read at 0ms is physically impossible
    and stays vetoed, see test_impossible_early_serve_vetoed)."""
    r = _reader(monkeypatch, epoch=7)
    _latch_press_clock(r, 1.00)
    r._det_warm_pos = (METER_X + BW / 2.0, METER_Y + BH / 2.0)
    r._det_warm_ts = 1.54
    res = _feed(r, 1.55)                     # warm re-lock at ~62%, press age 550ms
    assert res.detected
    assert float(res.fill_pct) >= 40.0


def test_cold_first_read_veto_flag_off_restores_publication(monkeypatch):
    r = _reader(monkeypatch, epoch=7, ORION_READER_COLD_FIRST_READ_VETO="0")
    _latch_press_clock(r, 1.00)
    res = _feed(r, 1.55)                     # press age 550ms: the clock allows 62%
    assert res.detected
    assert float(res.fill_pct) >= 40.0


# --------------------------------------------------------------------------------------- #
#  [ORION_READER_PRESS_ONSET_PLAUSIBILITY] the four live authority-loss signatures
#  (2026-08-31 04:51-04:54Z bout) and the animation cases that must KEEP publishing.
# --------------------------------------------------------------------------------------- #

def test_fade_low_then_jump_is_vetoed(monkeypatch):
    """Epochs 32/58 live signature: a garbage low read stands in for the onset (2.8/5.1 at
    press age 30-60ms), then the fading leftover re-locks HIGH (63.2/94.3) and the engine
    anchors on it. The low read may publish (harmless), but it must NOT stand the guard
    down alone, and the jump read must be withheld, the lock evicted, the zone armed --
    then the REAL onset (rising pair at a real press age) publishes normally."""
    r = _reader(monkeypatch, epoch=7)
    _latch_press_clock(r, 1.00)
    low = _feed(r, 1.29, fill_top=95)                   # garbage ~10% at press age 290ms
    assert low.detected                                  # low serve itself is harmless
    jump1 = _feed(r, 1.32)                              # leftover re-lock at ~62% (read 2:
    assert not jump1.detected                           #  the cold veto already withholds)
    assert jump1.rejection_reason == "cold_first_read_unproven"
    jump2 = _feed(r, 1.35)                              # read 3: past the cold veto -- the
    assert not jump2.detected                           #  press clock still forbids it
    assert jump2.rejection_reason == "press_onset_implausible"
    assert r._det_state == "idle"                        # busy locker freed
    res = _feed(r, 1.38)                                # same object again: quarantined
    assert not res.detected
    assert res.rejection_reason == "ghost_static_press"
    low1 = _feed(r, 1.60, fill_top=88)                  # REAL onset, rising pair in-zone
    assert low1.detected
    low2 = _feed(r, 1.63, fill_top=84)
    assert low2.detected
    res = _feed(r, 1.70, fill_top=55)                   # the rise publishes
    assert res.detected and float(res.fill_pct) > float(low2.fill_pct)


def test_impossible_early_serve_vetoed(monkeypatch):
    """Epoch 50 live signature: 32.4% at press age 85ms, then 56.2% -- the guard used to
    call the first read a 'low sighting', stand down, and the >=40 jump completed the
    engine's ownership of a fading leftover. The sub-40 read itself SERVES (harmless
    alone: the engine cannot complete ownership without a plausible rise, and a rapid
    re-press's carried-over meter legitimately reads ~30 at age 0); the >=40 read that
    the press clock forbids is what gets withheld -- no stand-down, no ownership."""
    r = _reader(monkeypatch, epoch=7)
    _latch_press_clock(r, 1.00)
    res = _feed(r, 1.06, fill_top=68)                   # ~33% at press age 60ms: serves
    assert res.detected and float(res.fill_pct) < 40.0
    j1 = _feed(r, 1.09, fill_top=45)                    # ~57% at 90ms (cold read 2:
    assert not j1.detected                              #  the cold veto already holds it)
    assert j1.rejection_reason == "cold_first_read_unproven"
    j2 = _feed(r, 1.12, fill_top=45)                    # read 3: the press clock forbids
    assert not j2.detected
    assert j2.rejection_reason == "press_onset_implausible"
    assert r._det_state == "idle"                        # busy locker freed


def test_declining_fade_pair_never_stands_down(monkeypatch):
    """Epoch 51 live signature: the quarantined leftover DRAINS/FADES down through the
    level band (46.3 -> ... -> 12.7) and its two sub-band reads used to count as the
    onset pair -- the guard stood down and the engine owned a 12.7 -> 51.4 ghost trace.
    A declining pair must never stand the guard down; the follow-up high re-lock stays
    quarantined; the real rising pair still escapes."""
    r = _reader(monkeypatch, epoch=7)
    _seed_zone(r)                                       # zone level ~62 at press 8
    d1 = _feed(r, 1.90, fill_top=71)                    # fade passes down: ~30% (sub-band)
    assert d1.detected                                   # single low frames stay harmless
    d2 = _feed(r, 1.93, fill_top=79)                    # ~22%: declining -> NO stand-down
    assert d2.detected
    high = _feed(r, 1.96, fill_top=48)                  # fade flickers back high (~53%)
    assert not high.detected                             # in-band -> quarantined, unserved
    assert high.rejection_reason in ("ghost_static_press", "press_onset_implausible")
    low1 = _feed(r, 2.30, fill_top=95)                  # real onset: rising pair
    assert low1.detected
    low2 = _feed(r, 2.33, fill_top=94)
    assert low2.detected
    res = _feed(r, 2.36, fill_top=55)                   # guard down: the rise publishes
    assert res.detected and float(res.fill_pct) >= 40.0


def test_press_clock_allows_late_midrise_catch(monkeypatch):
    """The owner's requirement: a meter first CAUGHT mid-rise (occlusion, pan, a weird
    animation's late/slow meter) must still be served. At press age 600ms the clock
    allows ~174%, so a 60% first sight passes the plausibility veto -- only the cold
    first-read veto's measured 2-frame settling applies, exactly as before."""
    r = _reader(monkeypatch, epoch=7)
    _latch_press_clock(r, 1.00)
    r1 = _feed(r, 1.60, fill_top=42)                    # ~60% first sight, cold read 1
    r2 = _feed(r, 1.63, fill_top=40)                    # cold read 2
    assert r1.rejection_reason == "cold_first_read_unproven"
    assert r2.rejection_reason == "cold_first_read_unproven"
    res = _feed(r, 1.66, fill_top=38)                   # read 3 publishes the mid-rise
    assert res.detected
    assert float(res.fill_pct) >= 40.0


def test_step_clamp_spares_real_rise_after_gap(monkeypatch):
    """A genuine rise that vanishes behind the player for ~200ms and returns much higher
    must publish: the step allowance grows with the gap (max_rate * dt)."""
    r = _reader(monkeypatch, epoch=7)
    _latch_press_clock(r, 1.00)
    low1 = _feed(r, 1.45, fill_top=92)                  # onset ~8%
    low2 = _feed(r, 1.48, fill_top=88)                  # rising pair -> guard down anyway
    assert low1.detected and low2.detected
    # (also prove the clamp math itself on a still-guarded press: fresh reader)
    r2 = _reader(monkeypatch, epoch=9)
    _latch_press_clock(r2, 1.00)
    a = _feed(r2, 1.45, fill_top=92)                    # published onset read ~8%
    assert a.detected
    for k in range(6):                                  # ~200ms of pan-blur zeros
        _feed(r2, 1.48 + 0.03 * k, fill_top=None)
    b = _feed(r2, 1.66, fill_top=52)                    # returns at ~49%: 8 + 4 + .35*210
    assert b.detected
    assert float(b.fill_pct) >= 40.0


def test_popin_superphysical_step_still_serves(monkeypatch):
    """Replay-A/B regression pin (session_20260830_191051 epochs 25/26/40): a GENUINE
    pop-in serve out-steps the ribbon rate while the bar is still growing (measured
    4.8 -> 16.0 in 19ms = 0.59 pct/ms on a real rise). The step clamp must only judge
    jumps landing AT/ABOVE the engine's 40% anchor bound -- clamping the low pop-in
    steps benched three real shots before the narrowing."""
    r = _reader(monkeypatch, epoch=7)
    _latch_press_clock(r, 1.00)
    a = _feed(r, 1.50, fill_top=98)                     # first sight ~8%
    assert a.detected
    b = _feed(r, 1.519, fill_top=84)                    # +13pp in 19ms: pop-in growth
    assert b.detected                                    # lands <40 -> must serve
    assert float(b.fill_pct) < 40.0
    c = _feed(r, 1.549, fill_top=79)                    # rising pair -> guard down
    assert c.detected
    res = _feed(r, 1.579)                               # the rise publishes (~62%)
    assert res.detected and float(res.fill_pct) >= 40.0


def test_rapid_repress_bridges_low_rising_lock(monkeypatch):
    """session_20260830_191051 press 16 (641ms after press 15): the previous press's
    meter is still RISING (~27-34%) when the next press lands. The stale drop rightly
    spares the low/rising lock; the guard must BRIDGE (stand down at the press) so the
    continuing meter keeps serving -- demanding this press's own low sighting from a
    meter already at 30-40%% starved a real shot in the replay A/B. Also pins the
    history hygiene: press 15's cold-VETOED garbage-high first read (72.9 live) must
    not stand as held-fill evidence and mis-fire the stale drop on the rising lock."""
    r = _reader(monkeypatch, epoch=15)
    _latch_press_clock(r, 1.00)
    bad = _feed(r, 1.50)                                # cold read 1 measures ~62%: vetoed
    assert bad.rejection_reason == "cold_first_read_unproven"
    low1 = _feed(r, 1.53, fill_top=92)                  # real onset ~8%
    low2 = _feed(r, 1.56, fill_top=88)                  # rising -> guard down
    assert low1.detected and low2.detected
    mid = _feed(r, 1.59, fill_top=72)                   # rise reaches ~30%
    assert mid.detected
    r.notify_physical_shot_start(16)                    # rapid re-press MID-RISE
    cont = _feed(r, 1.62, fill_top=68)                  # ~33%: keeps serving (bridged)
    assert cont.detected
    assert 0.0 < float(cont.fill_pct) < 40.0
    high = _feed(r, 1.65, fill_top=40)                  # the rise continues through 62%
    assert high.detected
    assert float(high.fill_pct) >= 40.0


def test_sustained_rise_stands_guard_down(monkeypatch):
    """A mid-rise catch may never read <= 35 (measured epoch 52: first sight 31.4), so
    the low-pair path alone would leave the guard up through the settle -- where the
    static-spread identification would wrongly evict the shot's own plateau. Three
    consecutive published steps rising within the physical cap are onset proof no fade,
    drain or static leftover can produce."""
    r = _reader(monkeypatch, epoch=7)
    _latch_press_clock(r, 1.00)
    a = _feed(r, 1.55, fill_top=66)                     # first sight ~37% (cold reads
    b = _feed(r, 1.58, fill_top=64)                     #  1-2 are sub-40 so they publish)
    assert a.detected and b.detected
    c = _feed(r, 1.61, fill_top=60)                     # third rising step (~43%)
    d = _feed(r, 1.64, fill_top=56)                     # rise-run >= 3 -> guard down
    assert c.detected and d.detected
    # settle plateau: static reads must publish (the run stood the guard down; the
    # static-spread window was cleared and cannot evict the shot's own plateau)
    for k in range(5):
        res = _feed(r, 1.67 + 0.03 * k, fill_top=16)    # ~87% static
        assert res.detected
        assert float(res.fill_pct) >= 80.0


def test_press_onset_flag_off_restores_publication(monkeypatch):
    """ORION_READER_PRESS_ONSET_PLAUSIBILITY=0 pins the exact pre-fix behaviour: a single
    low read stands the guard down and the following jump publishes."""
    r = _reader(monkeypatch, epoch=7,
                ORION_READER_PRESS_ONSET_PLAUSIBILITY="0")
    _latch_press_clock(r, 1.00)
    low = _feed(r, 1.03, fill_top=96)                   # single low at 30ms: stands down
    assert low.detected
    res = _feed(r, 1.06)                                # jump to 62% publishes (old bug)
    assert res.detected
    assert float(res.fill_pct) >= 40.0
