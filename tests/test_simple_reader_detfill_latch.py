"""DETECTOR-FILL STRUCTURE LATCH (ORION_METER_DETFILL_GREEN_LATCH).

The live silent-shot chain (session_20260830_112306 + orion_native.log): the sidecar
config said meter_color=Red against 2K27's WHITE meter, read()'s red-masked sample
starved _qualify_gameplay_sample (which runs BEFORE the colour-agnostic detector-fill
override), so the structure proof never latched -- 18 presses aborted
ownership_structure_stamp_missing with stamp_epoch_seen=0 while genuine rising frames
(fills 6->47, green cap on 91% of early rise frames) flowed to the engine unstamped.

The fix lets the authoritative detector-fill sample latch the proof on the meter's own
identity. A current-epoch low-fill frame with both the green chevron and white ribbon is
strong enough to stamp immediately during acquisition; high/static candidates still need
2 CONSECUTIVE frames of a locked lifecycle box. These tests pin both paths and refusal edges.
"""
import numpy as np

from simple_meter_reader import SimpleMeterReader

BH, BW = 107, 24
METER_X, METER_Y = 600, 300
TRUE_BOX = (METER_X, METER_Y, BW, BH)


class _CfgRed:
    """The DRIFTED live config: Red configured against a white meter."""
    meter_style = "Arrow2"
    meter_color = "Red"
    confidence_threshold = 0.32


def _meter_patch(green=True, fill_top=40):
    p = np.zeros((BH, BW, 3), np.uint8)
    p[:] = (30, 70, 140)
    p[fill_top:100] = (250, 252, 253)
    p[100:103] = (168, 170, 171)
    p[103:] = (100, 105, 110)
    p[:, :3] = (60, 60, 65)
    p[:, BW - 3:] = (60, 60, 65)
    if green:
        p[2:4, 4:BW - 4] = (60, 200, 60)     # chevron: 32 green px >= the 8px floor
    return p


def _frame(green=True, fill_top=40):
    f = np.full((720, 1280, 3), 35, np.uint8)
    f[METER_Y:METER_Y + BH, METER_X:METER_X + BW] = _meter_patch(green, fill_top)
    return f


class FakeLoc:
    provider = "fake"
    infer_ms = 0.0
    ok = True

    def __init__(self):
        self.res = (False, None, 0.0, -1.0)

    def submit(self, frame, ts):
        pass

    def latest(self):
        return self.res

    def detect_now(self, frame, ts):
        return self.res


def _reader(monkeypatch, epoch=7, **env):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    monkeypatch.setenv("ORION_METER_DETECTOR_TRACK", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    r = SimpleMeterReader(1280, 720, cfg=_CfgRed(), require_gameplay_eligibility=True)
    r._meter_detector = FakeLoc()
    r.set_shot_state(True, 1.0, True)
    r.notify_physical_shot_start(epoch)
    return r


def _feed(r, ts, green=True, fill_top=40, source_ts=None):
    r._meter_detector.res = (
        True, TRUE_BOX, 0.9, ts if source_ts is None else source_ts)
    return r.detect(_frame(green, fill_top), ts=ts)


def _isolate_detector_fill_latch(r):
    """Keep these regressions from succeeding through read()'s separate rise proof."""
    r._qualify_gameplay_sample = lambda sample, proof_epoch=None: sample


def test_low_fill_current_epoch_latches_on_first_acquisition_frame(monkeypatch):
    """A real meter's low ribbon + chevron must stamp before lifecycle lock so native
    ownership sees the early rise instead of first seeing the meter above its anchor."""
    r = _reader(monkeypatch, epoch=7)
    res = _feed(r, 1.00, green=True, fill_top=80)
    assert res.detected and 0.0 < res.fill_pct <= r._ghost_press_low_pct
    assert bool(res.gameplay_structure_verified)
    assert int(res.gameplay_structure_epoch) == 7


def test_green_streak_latches_under_drifted_colour(monkeypatch):
    """Two consecutive chevron-carrying detector-fill frames stamp the proof under the
    press epoch even though read()'s Red-masked path never qualifies -- the exact live
    silent-shot scenario."""
    # COLD_FIRST_READ_VETO + PRESS_ONSET_PLAUSIBILITY pinned OFF: this fixture
    # cold-locks a static ~62% fill inside the press window at press-age ~0, which both
    # sanity guards (correctly) withhold; the latch semantics under test are orthogonal.
    r = _reader(monkeypatch, epoch=7, ORION_READER_COLD_FIRST_READ_VETO="0",
                ORION_READER_PRESS_ONSET_PLAUSIBILITY="0")
    res1 = _feed(r, 1.00)
    res2 = _feed(r, 1.02)
    assert res2.detected and res2.fill_pct > 30.0
    assert bool(res2.gameplay_structure_verified)
    assert int(res2.gameplay_structure_epoch) == 7


def test_single_green_frame_does_not_latch(monkeypatch):
    r = _reader(monkeypatch)
    res1 = _feed(r, 1.00)
    assert not bool(res1.gameplay_structure_verified)


def test_no_green_no_latch(monkeypatch):
    """A capless (contested / decor) box must never latch through this path."""
    r = _reader(monkeypatch)
    for k in range(6):
        res = _feed(r, 1.00 + 0.02 * k, green=False)
    assert not bool(res.gameplay_structure_verified)


def test_capless_low_rise_latches_only_on_third_unique_locator_frame(monkeypatch):
    """A real no-chevron onset needs three distinct detector source frames.

    Re-consuming one async result while capture advances must not manufacture the
    third corroborating frame.
    """
    r = _reader(monkeypatch)
    _isolate_detector_fill_latch(r)
    res = _feed(r, 1.00, green=False, fill_top=98)
    assert res.detected and not bool(res.gameplay_structure_verified)
    res = _feed(r, 1.02, green=False, fill_top=93)
    assert res.detected and not bool(res.gameplay_structure_verified)
    res = _feed(r, 1.03, green=False, fill_top=90, source_ts=1.02)
    assert res.detected and not bool(res.gameplay_structure_verified)
    res = _feed(r, 1.04, green=False, fill_top=88)
    assert res.detected and 0.0 <= res.raw_fill_pct <= 20.0
    assert bool(res.gameplay_structure_verified)
    assert int(res.gameplay_structure_epoch) == 7


def test_capless_static_nonmonotonic_and_high_runs_never_latch(monkeypatch):
    cases = (
        (85, 85, 85, 85),             # static low court/decor
        (98, 90, 94, 88),             # rise, fall, then only one fresh rise
        (98, 93, 70, 65),             # leaves the strict 0..20 onset band
    )
    for tops in cases:
        r = _reader(monkeypatch)
        _isolate_detector_fill_latch(r)
        for k, top in enumerate(tops):
            res = _feed(r, 2.00 + 0.02 * k, green=False, fill_top=top)
        assert not bool(res.gameplay_structure_verified), tops


def test_capless_evidence_resets_at_physical_epoch_boundary(monkeypatch):
    r = _reader(monkeypatch, epoch=7, ORION_READER_GHOST_PRESS_BREAK="0",
                ORION_READER_PRESS_ONSET_PLAUSIBILITY="0")
    _isolate_detector_fill_latch(r)
    _feed(r, 1.00, green=False, fill_top=98)
    res = _feed(r, 1.02, green=False, fill_top=93)
    assert not bool(res.gameplay_structure_verified)

    r.notify_physical_shot_start(8)
    res = _feed(r, 1.04, green=False, fill_top=91)
    assert not bool(res.gameplay_structure_verified)
    _feed(r, 1.06, green=False, fill_top=88)
    res = _feed(r, 1.08, green=False, fill_top=86)
    assert bool(res.gameplay_structure_verified)
    assert int(res.gameplay_structure_epoch) == 8


def test_capless_evidence_resets_at_lifecycle_generation_boundary(monkeypatch):
    r = _reader(monkeypatch)
    _isolate_detector_fill_latch(r)
    _feed(r, 1.00, green=False, fill_top=98)
    res = _feed(r, 1.02, green=False, fill_top=93)
    assert not bool(res.gameplay_structure_verified)
    old_generation = r._det_lock_generation

    r._det_drop_lock(1.03, "test_generation_boundary")
    res = _feed(r, 1.04, green=False, fill_top=91)
    assert r._det_lock_generation > old_generation
    assert not bool(res.gameplay_structure_verified)
    _feed(r, 1.06, green=False, fill_top=88)
    res = _feed(r, 1.08, green=False, fill_top=86)
    assert bool(res.gameplay_structure_verified)


def test_capless_evidence_resets_on_box_discontinuity(monkeypatch):
    r = _reader(monkeypatch)
    _isolate_detector_fill_latch(r)
    _feed(r, 1.00, green=False, fill_top=98)
    generation = r._det_lock_generation
    jumped = (METER_X + 100, METER_Y, BW, BH)

    # The jump starts a new possible run; it is not frame two of the old box.
    latched = r._advance_detfill_nogreen_rise(
        shot_epoch=7, lock_generation=generation, source_ts=1.02,
        sample_ts=1.02, coarse_fill=7.0, locator_box=jumped,
        white_ribbon=True, locator_fresh=True)
    assert not latched and len(r._df_nogreen_samples) == 1
    latched = r._advance_detfill_nogreen_rise(
        shot_epoch=7, lock_generation=generation, source_ts=1.04,
        sample_ts=1.04, coarse_fill=11.0, locator_box=jumped,
        white_ribbon=True, locator_fresh=True)
    assert not latched
    latched = r._advance_detfill_nogreen_rise(
        shot_epoch=7, lock_generation=generation, source_ts=1.06,
        sample_ts=1.06, coarse_fill=15.0, locator_box=jumped,
        white_ribbon=True, locator_fresh=True)
    assert latched and r.gameplay_structure_verified


def test_capless_long_adjacent_gaps_never_accumulate(monkeypatch):
    r = _reader(monkeypatch)
    _isolate_detector_fill_latch(r)
    _feed(r, 1.00, green=False, fill_top=98)
    generation = r._det_lock_generation
    max_gap, _ = r._detfill_nogreen_time_limits()

    for ts, fill in ((1.00 + max_gap + 0.01, 8.0),
                     (1.00 + 2.0 * (max_gap + 0.01), 14.0)):
        latched = r._advance_detfill_nogreen_rise(
            shot_epoch=7, lock_generation=generation, source_ts=ts,
            sample_ts=ts, coarse_fill=fill, locator_box=TRUE_BOX,
            white_ribbon=True, locator_fresh=True)
        assert not latched
    assert not r.gameplay_structure_verified
    assert len(r._df_nogreen_samples) == 1


def test_capless_total_span_is_bounded_even_when_each_gap_is_valid(monkeypatch):
    r = _reader(monkeypatch)
    _isolate_detector_fill_latch(r)
    _feed(r, 1.00, green=False, fill_top=98)
    generation = r._det_lock_generation
    max_gap, max_span = r._detfill_nogreen_time_limits()
    half = max_span * 0.5 + 0.005
    assert half <= max_gap

    for ts, fill in ((1.00 + half, 8.0), (1.00 + 2.0 * half, 14.0)):
        latched = r._advance_detfill_nogreen_rise(
            shot_epoch=7, lock_generation=generation, source_ts=ts,
            sample_ts=ts, coarse_fill=fill, locator_box=TRUE_BOX,
            white_ribbon=True, locator_fresh=True)
    assert not latched and not r.gameplay_structure_verified
    assert len(r._df_nogreen_samples) == 1


def test_capless_happy_path_inside_time_boundary_latches(monkeypatch):
    r = _reader(monkeypatch)
    _isolate_detector_fill_latch(r)
    _feed(r, 1.00, green=False, fill_top=98)
    generation = r._det_lock_generation
    max_gap, max_span = r._detfill_nogreen_time_limits()
    half = max_span * 0.49
    assert half <= max_gap

    for ts, fill in ((1.00 + half, 8.0), (1.00 + 2.0 * half, 14.0)):
        latched = r._advance_detfill_nogreen_rise(
            shot_epoch=7, lock_generation=generation, source_ts=ts,
            sample_ts=ts, coarse_fill=fill, locator_box=TRUE_BOX,
            white_ribbon=True, locator_fresh=True)
    assert latched and r.gameplay_structure_verified


def test_capless_stale_source_resets_evidence(monkeypatch):
    r = _reader(monkeypatch)
    _isolate_detector_fill_latch(r)
    _feed(r, 1.00, green=False, fill_top=98)
    generation = r._det_lock_generation
    stale_now = 1.02 + r._det_ttl_s + 0.01
    latched = r._advance_detfill_nogreen_rise(
        shot_epoch=7, lock_generation=generation, source_ts=1.02,
        sample_ts=stale_now, coarse_fill=8.0, locator_box=TRUE_BOX,
        white_ribbon=True, locator_fresh=True)
    assert not latched and not r.gameplay_structure_verified
    assert r._df_nogreen_samples == []


def test_green_gap_resets_streak(monkeypatch):
    r = _reader(monkeypatch)
    _feed(r, 1.00, green=True)
    _feed(r, 1.02, green=False)              # streak broken
    res = _feed(r, 1.04, green=True)         # streak = 1 again
    assert not bool(res.gameplay_structure_verified)


def test_unarmed_epoch_never_stamps(monkeypatch):
    """Without a hw epoch the gate closes frames entirely -- and nothing latches."""
    r = _reader(monkeypatch)
    r.set_shot_state(False, 0.0, False)      # closes the hw window, epoch -> 0
    res1 = _feed(r, 1.00)
    res2 = _feed(r, 1.02)
    assert not bool(res2.gameplay_structure_verified)
    assert int(res2.gameplay_structure_epoch) == 0


def test_next_press_requires_fresh_streak(monkeypatch):
    """The chevron streak (and the proof) must not cross a press boundary.

    ORION_READER_GHOST_PRESS_BREAK is pinned OFF: this fixture carries a static ~62%
    lock across the press, which the ghost breaker (correctly) drops as a leftover
    meter. The streak-reset semantics under test are orthogonal to that eviction and
    are exercised on the surviving-lock path (a low/rising bridge keeps the lock)."""
    r = _reader(monkeypatch, epoch=7, ORION_READER_GHOST_PRESS_BREAK="0",
                ORION_READER_COLD_FIRST_READ_VETO="0",
                ORION_READER_PRESS_ONSET_PLAUSIBILITY="0")
    _feed(r, 1.00)
    res = _feed(r, 1.02)
    assert bool(res.gameplay_structure_verified)
    r.notify_physical_shot_start(8)          # next press
    res = _feed(r, 1.04)
    assert not bool(res.gameplay_structure_verified)   # streak must rebuild
    res = _feed(r, 1.06)
    assert bool(res.gameplay_structure_verified)
    assert int(res.gameplay_structure_epoch) == 8


def test_flag_off_is_inert(monkeypatch):
    r = _reader(monkeypatch, ORION_METER_DETFILL_GREEN_LATCH="0",
                ORION_READER_COLD_FIRST_READ_VETO="0",
                ORION_READER_PRESS_ONSET_PLAUSIBILITY="0")
    _feed(r, 1.00)
    res = _feed(r, 1.02)
    assert res.detected and res.fill_pct > 30.0
    assert not bool(res.gameplay_structure_verified)


def test_display_height_clamp_on_detector_fill(monkeypatch):
    """A colour-drift-poisoned _tight_src (track candidate spanning ~500px) must not
    stretch the engine-facing rectangle on a detector-fill frame past 1.6x the served
    meter box (live: 227 frames served 29x320..556 while the fill read fine)."""
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    monkeypatch.setenv("ORION_READER_BOX_TIGHT", "2")
    r = SimpleMeterReader(1280, 720)
    served = (600, 300, 24, 107)
    # garbage red-track source: bar column 30px tall at y=360, pre-track spanning y 7..539
    r._tight_src = ((610, 360, 8, 30), -1, (605, 7, 20, 532), (2, 2))
    out = r._tight_display_box(list(served), -1, h_cap=int(1.6 * served[3]))
    assert out[3] <= int(1.6 * served[3]) + 2, out
    # ...and the served box's vertical span stays inside the drawn rectangle
    assert out[1] <= served[1] + 4
    assert out[1] + out[3] >= served[1] + served[3] - 4


def test_display_transform_unchanged_without_cap(monkeypatch):
    """h_cap=0 (colour-path frames) keeps the shipped union behaviour: a short bar
    legitimately unions a taller live track."""
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    monkeypatch.setenv("ORION_READER_BOX_TIGHT", "2")
    r = SimpleMeterReader(1280, 720)
    r._tight_src = ((610, 400, 8, 30), -1, (605, 320, 20, 120), (2, 2))
    out = r._tight_display_box([610, 400, 8, 30], -1, h_cap=0)
    assert out[3] >= 120          # track union preserved


def test_structure_necropsy_names_the_starved_latch(monkeypatch, caplog):
    """An epoch that emitted detected frames but never latched must leave one ERROR
    breadcrumb naming the read-side starvation (the live silent-shot signature)."""
    import logging
    # ORION_READER_GHOST_PRESS_BREAK is pinned OFF here for the same reason the green
    # latch is: this fixture holds a static high fill under an armed press with no low
    # sighting, which is byte-for-byte the leftover-meter signature the ghost breaker
    # evicts after 3 frames. This test probes the NECROPSY accounting of a starved
    # latch, so the (correct) eviction defense is bypassed to let the epoch accumulate.
    r = _reader(monkeypatch, epoch=7, ORION_METER_DETFILL_GREEN_LATCH="0",
                ORION_READER_GHOST_PRESS_BREAK="0",
                ORION_READER_PRESS_ONSET_PLAUSIBILITY="0")
    with caplog.at_level(logging.ERROR):
        for k in range(12):
            _feed(r, 1.00 + 0.02 * k)
        r.notify_physical_shot_start(8)          # closes epoch 7 un-latched
    lines = [m for m in caplog.messages if "STRUCTURE NECROPSY" in m]
    assert len(lines) == 1, caplog.messages
    assert "epoch=7" in lines[0]
    assert "color=Red" in lines[0]


def test_structure_necropsy_silent_when_latched(monkeypatch, caplog):
    import logging
    r = _reader(monkeypatch, epoch=7)
    with caplog.at_level(logging.ERROR):
        for k in range(12):
            _feed(r, 1.00 + 0.02 * k)
        r.notify_physical_shot_start(8)
    assert not [m for m in caplog.messages if "STRUCTURE NECROPSY" in m]
