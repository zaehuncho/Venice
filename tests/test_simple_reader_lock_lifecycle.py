"""Lock LIFECYCLE tests: the retired meter_detector.py ACQUIRE/KEEP/COAST/DROP port
(simple_meter_reader detector path, 2026-08-30).

The retired detector's stability came from asymmetric evidence requirements (harder to
acquire than to keep, harder to drop than to keep), a bounded corroborated coast, and
outlier-proof position serving. These tests pin each transition and its hysteresis so a
future edit cannot silently regress the owner-visible churn fixes.
"""
import os

import numpy as np


class _Cfg:
    meter_style = "Arrow2"
    meter_color = "White"
    confidence_threshold = 0.32


class FakeDet:
    """Duck-typed AsyncMeterLocator: scripted per-timestamp results."""
    provider = "fake"
    infer_ms = 1.0

    def __init__(self):
        self.script = {}
        self._res = (False, None, 0.0, -1.0)
        self.priority_calls = []

    def key(self, ts):
        return round(ts, 3)

    def submit(self, frame, ts):
        self._res = self.script.get(self.key(ts), (False, None, 0.0, float(ts)))

    def latest(self):
        return self._res

    def submit_priority(self, frame, ts):
        self.priority_calls.append(float(ts))
        self.submit(frame, ts)

    def detect_now(self, frame, ts):
        self.submit(frame, ts)
        return self._res


def _frame(x=600, y=300, w=24, h=107, fill=0.5):
    """720p frame with a white meter ribbon at (x,y,w,h), filled from the bottom."""
    f = np.zeros((720, 1280, 3), np.uint8)
    f[y + int(h * (1.0 - fill)):y + h, x:x + w] = 255
    return f


def _mk_reader(monkeypatch, lifecycle="1", **env):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")   # no real model; we inject a fake
    monkeypatch.setenv("ORION_METER_LIFECYCLE", lifecycle)
    monkeypatch.delenv("ORION_METER_DETECTOR_PHASED_ACQUIRE", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from simple_meter_reader import SimpleMeterReader
    r = SimpleMeterReader(cfg=_Cfg(), require_gameplay_eligibility=False)
    r._meter_detector = FakeDet()
    return r


STEP = 1.0 / 60.0


def test_released_epoch_cannot_acquire_court_candidate(monkeypatch):
    r = _mk_reader(monkeypatch)
    r._physical_shot_epoch = 41
    r._shot_armed_hw = True  # the diagnostic tail outlives the physical release
    assert r.notify_physical_shot_release(41)
    monkeypatch.setattr(r, '_det_result_this_arm', lambda _: True)
    for i in range(5):
        assert not r._det_on_found((600, 300, 24, 107), 0.99, 10.0 + i * STEP, True)
    assert r._det_state != 'locked'
    r.notify_physical_shot_start(42)
    assert r._det_on_found((600, 300, 24, 107), 0.99, 11.0, True)


def test_release_keeps_same_meter_but_never_reseeds_to_court(monkeypatch):
    r = _mk_reader(monkeypatch)
    r._physical_shot_epoch = 41
    r._det_state = 'locked'
    r._det_last_box = (600, 300, 24, 107)
    assert r.notify_physical_shot_release(41)
    # Continued measurement of this shot remains available to the landing oracle.
    assert r._det_on_found((605, 300, 24, 107), 0.99, 10.0, True)
    for i in range(8):
        assert not r._det_on_found((100, 300, 24, 107), 0.99, 10.1 + i * STEP, True)
    assert r._det_diag['reseed'] == 0
    assert r._det_last_box == (600, 300, 24, 107)


def test_old_release_never_closes_new_press_acquisition(monkeypatch):
    r = _mk_reader(monkeypatch)
    r._physical_shot_epoch = 42
    r._shot_armed_hw = True
    assert not r.notify_physical_shot_release(41)
    monkeypatch.setattr(r, '_det_result_this_arm', lambda _: True)
    assert r._det_on_found((600, 300, 24, 107), 0.99, 10.0, True)


def _feed(r, t, box=None, conf=0.9, fill=0.5, x=None, armed_hw=False):
    """Script one detector result at t and run one detect()."""
    fd = r._meter_detector
    if box is None:
        fd.script[fd.key(t)] = (False, None, 0.0, t)
    else:
        fd.script[fd.key(t)] = (True, tuple(box), float(conf), t)
    r.set_shot_state(True, 1.0, bool(armed_hw))
    fx = x if x is not None else (box[0] if box else 600)
    return r.detect(_frame(x=fx, fill=fill), ts=t)


# ------------------------------- ACQUIRE ------------------------------------------- #

def test_single_weak_proposal_does_not_lock(monkeypatch):
    """One sub-strong proposal is PENDING, not a lock (retired _MIN_FRAMES=2). This is
    what stops a 1-frame YOLO misfire from becoming an owner-visible false lock."""
    r = _mk_reader(monkeypatch)
    res = _feed(r, 1000.0, box=(600, 300, 24, 107), conf=0.40)
    assert not res.detected
    assert r._det_state == "pending"


def test_two_consistent_proposals_lock(monkeypatch):
    r = _mk_reader(monkeypatch)
    _feed(r, 1000.0, box=(600, 300, 24, 107), conf=0.40)
    res = _feed(r, 1000.0 + STEP, box=(602, 300, 24, 107), conf=0.40)
    assert res.detected and r._det_state == "locked"


def test_two_inconsistent_proposals_do_not_lock(monkeypatch):
    """Positionally wandering proposals never build a streak (retired acq_jump_reset)."""
    r = _mk_reader(monkeypatch)
    _feed(r, 1000.0, box=(600, 300, 24, 107), conf=0.40)
    res = _feed(r, 1000.0 + STEP, box=(100, 300, 24, 107), conf=0.40, x=100)
    assert not res.detected
    assert r._det_streak == 1          # restarted, not accumulated


def test_strong_conf_plus_physical_arm_locks_instantly(monkeypatch):
    """The armed onset frame may latch on ONE strong proposal (retired T-a4 pairing:
    learned-detector confidence + behavioural evidence). Guards first-detected fill:
    the engine cannot fire above ~33%, and the sync-acquire path delivers 0-10% today."""
    r = _mk_reader(monkeypatch)
    res = _feed(r, 1000.0, box=(600, 300, 24, 107), conf=0.90, fill=0.05, armed_hw=True)
    assert res.detected and r._det_state == "locked"
    assert res.fill_pct < 12.0


def test_strong_conf_without_arm_does_not_instalock(monkeypatch):
    r = _mk_reader(monkeypatch)
    res = _feed(r, 1000.0, box=(600, 300, 24, 107), conf=0.90, armed_hw=False)
    assert not res.detected and r._det_state == "pending"


def test_fresh_post_arm_far_court_proposal_still_locks_immediately(monkeypatch):
    """The source-epoch fence is temporal, never spatial: a fresh meter near the
    far-court/top-right positions seen in the live batch keeps the one-result armed
    acquire path with no extra frame of latency.
    """
    r = _mk_reader(monkeypatch)
    fd = r._meter_detector
    t = 1000.0
    box = (1130, 219, 23, 107)
    fd.script[fd.key(t)] = (True, box, 0.90, t)
    r.set_shot_state(True, 1.0, True)
    r.notify_physical_shot_start(61)
    res = r.detect(_frame(x=box[0], y=box[1], w=box[2], h=box[3], fill=0.05), ts=t)

    assert res.detected
    assert r._det_state == "locked"
    assert abs((res.bbox[0] + res.bbox[2] * 0.5) - (box[0] + box[2] * 0.5)) < 20.0


# ------------------------------- KEEP / OUTLIER ------------------------------------ #

def _lock(r, t0=1000.0, x=600, fill=0.5):
    _feed(r, t0, box=(x, 300, 24, 107), conf=0.90, armed_hw=True, fill=fill)
    assert r._det_state == "locked"
    return t0


def test_teleport_outlier_does_not_move_the_lock(monkeypatch):
    """A single far proposal must serve the COASTED position, never the raw jump (the
    live 543px |dcx| box teleport; retired MeterBoxKalman 'reject')."""
    r = _mk_reader(monkeypatch)
    t = _lock(r)
    res = _feed(r, t + STEP, box=(100, 300, 24, 107), conf=0.95)
    assert not res.detected  # pixels exist only at the rejected teleport, not the held box
    box = r._det_active_box
    assert abs((box[0] + box[2] * 0.5) - 612) < 40   # lock remains near x=600
    assert r._det_strike_n == 1


def test_three_consistent_strikes_reseed(monkeypatch):
    """A GENUINE relocation (3 mutually-consistent far proposals) re-seeds the lock
    (retired 3-strike re-seed) instead of starving forever."""
    r = _mk_reader(monkeypatch)
    t = _lock(r)
    for i in range(3):
        res = _feed(r, t + STEP * (i + 1), box=(100, 300, 24, 107), conf=0.95, x=100)
    assert r._det_diag["reseed"] == 1
    assert r._det_state == "locked"
    assert abs((res.bbox[0] + res.bbox[2] * 0.5) - 112) < 40   # adopted the new spot


def test_inconsistent_outliers_never_reseed(monkeypatch):
    """Scattered outliers (a different spot each time) reset the strike count -- a
    wandering distractor can never win the re-seed."""
    r = _mk_reader(monkeypatch)
    t = _lock(r)
    for i, x in enumerate((100, 1100, 300)):
        _feed(r, t + STEP * (i + 1), box=(x, 300, 24, 107), conf=0.95)
    assert r._det_diag["reseed"] == 0
    assert r._det_strike_n == 1


# ------------------------------- DROP HYSTERESIS ----------------------------------- #

def test_fresh_no_meter_still_drops_an_uncorroborated_lock(monkeypatch):
    """THE VETO'S SAFETY PROPERTY: a lock with no proven rise (the decor false-lock
    case) dies after _det_nm_drop consecutive fresh no-meter verdicts -- faster than
    the old 0.35s hold expiry, so the decor protection is strictly stronger."""
    r = _mk_reader(monkeypatch)
    t = _lock(r)
    for i in range(3):
        res = _feed(r, t + STEP * (i + 1), box=None)
    assert not res.detected
    assert r._det_state == "idle"


def test_one_no_meter_blip_does_not_drop(monkeypatch):
    """Drop needs MORE evidence than keep (retired low_conf_grace / max_freeze_frames
    hysteresis): a single fresh no-meter is a blip, not a loss. This is the direct fix
    for the owner's 'locks onto the meter then locks off then locks back on'."""
    r = _mk_reader(monkeypatch)
    t = _lock(r)
    res = _feed(r, t + STEP, box=None)
    assert res.detected and r._det_state == "locked"
    res = _feed(r, t + 2 * STEP, box=(602, 300, 24, 107), conf=0.9)
    assert res.detected and r._det_nm_strikes == 0   # fresh evidence heals the strikes


# ------------------------------- COAST --------------------------------------------- #

def _lock_and_rise(r, t0=1000.0):
    """Lock LOW and prove a >=8pp rise so the extended coast is earned."""
    t = _lock(r, t0, fill=0.05)
    for i, fill in enumerate((0.10, 0.20, 0.30, 0.40)):
        t2 = t + STEP * (i + 1)
        _feed(r, t2, box=(600, 300, 24, 107), conf=0.9, fill=fill)
    return t + STEP * 4


def test_risen_lock_coasts_through_a_long_no_meter_gap(monkeypatch):
    """Measured on session_20260830_003937: YOLO returns found=0 for 17-33 CONSECUTIVE
    frames INSIDE real shot runs (cap blindness). A lock that proved a rise and still
    measures the white ribbon must bridge such a gap (retired peak-hold coast)."""
    r = _mk_reader(monkeypatch)
    t = _lock_and_rise(r)
    alive = 0
    for i in range(25):                       # ~0.42s < the 0.6s bound
        res = _feed(r, t + STEP * (i + 1), box=None, fill=0.5)
        alive += 1 if res.detected else 0
    assert alive == 25
    assert r._det_state == "locked"


def test_coast_is_wall_clock_bounded(monkeypatch):
    """The coast cannot hallucinate a meter forever: past _det_coast_max_s (retired
    _PEAK_HOLD_S=0.6) the lock DROPS even with the ribbon still on screen."""
    r = _mk_reader(monkeypatch)
    t = _lock_and_rise(r)
    for i in range(40):                       # ~0.67s > 0.6s bound
        res = _feed(r, t + STEP * (i + 1), box=None, fill=0.5)
    assert not res.detected
    assert r._det_state == "idle"


def test_unrisen_lock_gets_only_the_base_hold(monkeypatch):
    """No proven rise -> no extended coast: the hold stays at _det_hold_s (0.35s), so
    static decor never earns the long bridge."""
    r = _mk_reader(monkeypatch)
    t = _lock(r)   # locked but never rose (fill constant 0.5 on lock frame only)
    # starve with STALE results (no fresh no-meter verdicts, so only the hold governs)
    r._meter_detector.script.clear()
    alive_at = []
    for i in range(40):
        t2 = t + STEP * (i + 1)
        r._meter_detector.script[r._meter_detector.key(t2)] = (False, None, 0.0, -1.0)
        r.set_shot_state(True, 1.0, False)
        res = r.detect(_frame(fill=0.5), ts=t2)
        if res.detected:
            alive_at.append(i)
    assert alive_at and alive_at[-1] < 25     # died around 0.35s (~21 frames), not 0.6s


# ------------------------------- WARM RE-ACQUIRE ----------------------------------- #

def test_warm_reacquire_relatches_on_one_proposal(monkeypatch):
    """A find near where a lock was just lost re-latches on ONE proposal (retired
    _WARM_REACQ_S=1.2): a mid-shot dropout must not re-prove the acquire streak."""
    r = _mk_reader(monkeypatch)
    t = _lock(r)
    for i in range(3):
        _feed(r, t + STEP * (i + 1), box=None)          # force-drop via strikes
    assert r._det_state == "idle"
    res = _feed(r, t + STEP * 5, box=(605, 302, 24, 107), conf=0.40)
    assert res.detected and r._det_state == "locked"


def test_far_find_after_drop_is_not_warm(monkeypatch):
    r = _mk_reader(monkeypatch)
    t = _lock(r)
    for i in range(3):
        _feed(r, t + STEP * (i + 1), box=None)
    res = _feed(r, t + STEP * 5, box=(100, 300, 24, 107), conf=0.40, x=100)
    assert not res.detected and r._det_state == "pending"


# ------------------------------- ASYNC UNIQUENESS ---------------------------------- #

def test_repeated_async_result_counts_once(monkeypatch):
    """latest() repeats one result across frames in async mode; a single detector box
    must NOT fake a 2-frame acquire streak."""
    r = _mk_reader(monkeypatch)
    fd = r._meter_detector
    fd._res = (True, (600, 300, 24, 107), 0.40, 1000.0)
    fd.submit = lambda frame, ts: None        # result never refreshes
    fd.detect_now = lambda frame, ts: fd._res
    for i in range(3):
        r.set_shot_state(True, 1.0, False)
        res = r.detect(_frame(), ts=1000.0 + STEP * i)
    assert not res.detected
    assert r._det_streak == 1


def test_pre_arm_async_result_cannot_acquire_new_shot(monkeypatch):
    """A detector result is evidence about the frame stamped by its result timestamp,
    not about whichever physical-shot epoch happens to be current when ``latest()`` is
    consumed.  A slow pre-press inference must therefore never use the armed/strong
    shortcut to install its old box in the new shot.

    Live regression (session_20260831_210338, epoch 53): the prior meter result was
    consumed after the new press, immediately locked on ``strong`` despite predating
    the press, and the held box measured a false 0->53% rise on bare court before the
    first post-press detector result arrived.
    """
    r = _mk_reader(monkeypatch)
    fd = r._meter_detector
    old_ts = 1000.0
    fd._res = (True, (848, 421, 24, 107), 0.90, old_ts)
    fd.submit = lambda frame, ts: None
    fd.detect_now = lambda frame, ts: fd._res

    r.set_shot_state(True, 1.0, True)
    r.notify_physical_shot_start(53)
    # The first detect latches this epoch's source-frame boundary at 1000.100.
    # ``old_ts`` is still unseen (new_result=True), but its pixels predate the shot.
    res = r.detect(np.zeros((720, 1280, 3), np.uint8), ts=1000.100)

    assert not res.detected
    assert r._det_state == "idle"
    assert r._det_last_box is None


def test_pre_arm_latest_does_not_starve_current_frame_priority_acquire(monkeypatch):
    """A fresh-by-TTL pre-arm slot is still unusable for THIS arm.  It must not make
    priority acquisition replay the old slot until the 200ms locator TTL expires.
    The current frame is queued without running ``detect_now`` on the caller.
    """
    r = _mk_reader(
        monkeypatch,
        ORION_METER_DETECTOR_SYNC_ACQUIRE="1",
        ORION_METER_DETECTOR_ACQ_INTERVAL_MS="0",
    )
    fd = r._meter_detector
    old_ts = 1000.0
    now = 1000.100
    old_box = (848, 421, 24, 107)
    current_box = (1130, 219, 23, 107)
    fd._res = (True, old_box, 0.95, old_ts)  # fresh by TTL, but pre-arm
    fd.submit = lambda frame, ts: None
    priority_calls = []

    def _current_priority(frame, ts):
        priority_calls.append(float(ts))
        fd._res = (True, current_box, 0.90, float(ts))

    fd.submit_priority = _current_priority
    fd.detect_now = lambda *_args: (_ for _ in ()).throw(
        AssertionError("capture callback must not run detector inference"))
    r.set_shot_state(True, 1.0, True)
    r.notify_physical_shot_start(62)
    res = r.detect(
        _frame(x=current_box[0], y=current_box[1], w=current_box[2],
               h=current_box[3], fill=0.05),
        ts=now,
    )

    assert priority_calls == [now]
    assert res.detected
    assert r._det_state == "locked"
    assert abs((res.bbox[0] + res.bbox[2] * 0.5)
               - (current_box[0] + current_box[2] * 0.5)) < 20.0


def test_prior_frame_pending_result_does_not_starve_current_priority_acquire(monkeypatch):
    """A weak post-arm result is valid evidence, but only once.  If that result leaves
    the lifecycle PENDING, the next sync opportunity must inspect the current frame
    instead of replaying the still-fresh ``latest()`` slot until its 200ms TTL expires.

    This mechanism is consistent with session_20260901_141000's moving-shot failures:
    first-owned fill landed around 30-33% and the command deadline was already gone.
    """
    r = _mk_reader(
        monkeypatch,
        ORION_METER_DETECTOR_SYNC_ACQUIRE="1",
        ORION_METER_DETECTOR_ACQ_INTERVAL_MS="0",
    )
    fd = r._meter_detector
    first_ts = 1000.000
    second_ts = first_ts + STEP
    first_box = (600, 300, 24, 107)
    second_box = (603, 300, 24, 107)
    fd._res = (True, first_box, 0.40, first_ts)
    fd.submit = lambda frame, ts: None
    priority_calls = []

    def _current_priority(frame, ts):
        priority_calls.append(float(ts))
        # The first request duplicates a result already computed from that source
        # frame; publishing it again is harmless because uniqueness is timestamped.
        if float(ts) > first_ts:
            fd._res = (True, second_box, 0.40, float(ts))

    fd.submit_priority = _current_priority
    fd.detect_now = lambda *_args: (_ for _ in ()).throw(
        AssertionError("capture callback must not run detector inference"))
    r.set_shot_state(True, 1.0, True)
    r.notify_physical_shot_start(63)

    first = r.detect(_frame(x=first_box[0], fill=0.05), ts=first_ts)
    assert not first.detected
    assert r._det_state == "pending"
    assert r._det_streak == 1
    assert priority_calls == [first_ts]

    second = r.detect(_frame(x=second_box[0], fill=0.10), ts=second_ts)
    assert priority_calls == [first_ts, second_ts]
    assert second.detected
    assert r._det_state == "locked"


def test_phased_priority_acquire_rotates_one_region_per_opportunity(monkeypatch):
    """The edge fallback is a cadence rotation, never full+left+right stacking.

    A tile miss is partial evidence and therefore cannot increment the global
    no-meter veto/strike counter. The full phase retains that fail-closed role.
    """
    r = _mk_reader(
        monkeypatch,
        ORION_METER_DETECTOR_SYNC_ACQUIRE="1",
        ORION_METER_DETECTOR_ACQ_INTERVAL_MS="0",
        ORION_METER_DETECTOR_PHASED_ACQUIRE="1",
    )

    class _RegionDet(FakeDet):
        def __init__(self):
            super().__init__()
            self.regions = []
            self.async_submits = 0
            self.scope = "full"

        def submit(self, frame, ts):
            self.async_submits += 1
            super().submit(frame, ts)

        def latest_details(self):
            return (*self._res, self.scope)

        def submit_priority_region(self, frame, ts, region):
            self.regions.append(region)
            self.scope = region
            self._res = (False, None, 0.0, float(ts))

        def detect_now_region(self, *_args):
            raise AssertionError("capture callback must not run detector inference")

    fd = _RegionDet()
    r._meter_detector = fd
    r.set_shot_state(True, 1.0, True)
    r.notify_physical_shot_start(70)
    frame = np.zeros((720, 1280, 3), np.uint8)

    for ts in (1000.000, 1000.120, 1000.240):
        r.detect(frame, ts=ts)

    assert fd.regions == ["full", "left", "right"]
    assert fd.async_submits == 0
    assert r._det_diag["sync_acq"] == 0
    assert r._det_diag["priority_acq"] == 3
    # Scope-aware partial misses remain excluded from the global no-meter veto.
    assert r._det_diag["nofound"] == 1


def test_stuck_arm_cannot_create_continuous_priority_inference(monkeypatch):
    """One physical epoch gets one edge wake, not an inference every 33ms forever.

    Live epoch 115 remained hardware-armed after release. The old inline branch kept
    firing 44-70ms ORT calls and collapsed source FPS into the 30s. Ordinary async
    submissions continue here, so a meter appearing later is still observable.
    """
    def _run(**env):
        r = _mk_reader(
            monkeypatch,
            ORION_METER_DETECTOR_SYNC_ACQUIRE="1",
            ORION_METER_DETECTOR_ACQ_INTERVAL_MS="0",
            **env,
        )
        fd = r._meter_detector
        ordinary = []
        priority = []

        def _ordinary(_frame_arg, ts):
            ordinary.append(float(ts))
            fd._res = (False, None, 0.0, float(ts))

        def _priority(_frame_arg, ts):
            priority.append(float(ts))
            fd._res = (False, None, 0.0, float(ts))

        fd.submit = _ordinary
        fd.submit_priority = _priority
        fd.detect_now = lambda *_args: (_ for _ in ()).throw(
            AssertionError("capture callback must not run detector inference"))
        r.set_shot_state(True, 1.0, True)
        r.notify_physical_shot_start(71)
        frame = np.zeros((720, 1280, 3), np.uint8)
        for i in range(20):
            r.detect(frame, ts=1000.0 + i * STEP)
        return r, ordinary, priority

    # the sync-acquire edge wake alone: one per epoch
    r, ordinary, priority = _run(ORION_METER_DETECTOR_ARMED_HOT="0")
    assert priority == [1000.0]
    assert len(ordinary) == 19
    assert r._det_diag["priority_acq"] == 1

    # [ORION_METER_DETECTOR_ARMED_HOT 2026-09-08] the per-frame worker wake while the button is
    # held is BOUNDED per epoch (here 100 ms = 6 frames + the edge wake), then the stuck arm
    # falls back to ordinary submissions -- still never inference on the capture callback
    r, ordinary, priority = _run(ORION_METER_DETECTOR_ARMED_HOT="1",
                                 ORION_METER_DETECTOR_ARMED_HOT_MAX_MS="100")
    assert priority[0] == 1000.0
    assert 6 <= len(priority) <= 8, priority
    assert len(ordinary) == 20 - len(priority)
    assert all(t > 1000.0 + 0.1 - 1e-9 for t in ordinary), ordinary
    assert r._det_diag["priority_acq"] == 1


def test_phased_sync_acquire_prioritizes_proven_side_but_keeps_full_sweep(monkeypatch):
    r = _mk_reader(
        monkeypatch,
        ORION_METER_DETECTOR_PHASED_ACQUIRE="1",
    )
    r._det_acq_last_side = "left"
    assert [r._det_next_acq_scan_region(80) for _ in range(3)] == [
        "left", "full", "right"]

    # A new epoch restarts at the remembered side; changing the hint only changes
    # order. Both the full view and opposite edge remain mandatory.
    r._det_acq_last_side = "right"
    assert [r._det_next_acq_scan_region(81) for _ in range(3)] == [
        "right", "full", "left"]


def test_pending_acquire_evidence_cannot_cross_physical_press_epoch(monkeypatch):
    """The two-result weak acquire streak is an epoch-local proof.  One weak box
    before the press plus one after it remains pending; it cannot become a lock.
    """
    r = _mk_reader(monkeypatch, ORION_METER_DETECTOR_SYNC_ACQUIRE="0")
    box = (600, 300, 24, 107)
    _feed(r, 1000.0, box=box, conf=0.40, armed_hw=False)
    assert r._det_state == "pending" and r._det_streak == 1

    now = 1000.100
    fd = r._meter_detector
    fd.script[fd.key(now)] = (True, box, 0.40, now)
    r.set_shot_state(True, 1.0, True)
    r.notify_physical_shot_start(63)
    res = r.detect(_frame(x=box[0], fill=0.05), ts=now)

    assert not res.detected
    assert r._det_state == "pending"
    assert r._det_streak == 1


def test_missing_source_timestamp_fails_closed_for_cold_armed_acquire(monkeypatch):
    """Without a source-frame clock, a result cannot prove it belongs to this arm."""
    r = _mk_reader(monkeypatch)
    r._shot_armed_hw = True
    r._hw_arm_ts = 1000.0

    accepted = r._det_on_found(
        (600, 300, 24, 107), 0.95, 1000.100, new_result=True, result_ts=None)

    assert not accepted
    assert r._det_state == "idle"


def test_repeated_result_cannot_reacquire_after_lock_reset(monkeypatch):
    """The armed/strong shortcut must also require a UNIQUE detector result.  A
    production-boundary ghost eviction resets the lock but not the async locator's
    ``latest()`` slot; replaying that same result must not re-install the evicted box.
    """
    r = _mk_reader(monkeypatch)
    fd = r._meter_detector
    t = 1000.0
    _feed(r, t, box=(600, 300, 24, 107), conf=0.90, armed_hw=True)
    assert r._det_state == "locked"
    r._det_reset_lock_state()

    repeated = (True, (600, 300, 24, 107), 0.90, t)
    fd._res = repeated
    fd.submit = lambda frame, ts: None
    fd.detect_now = lambda frame, ts: repeated
    r.set_shot_state(True, 1.0, True)
    res = r.detect(_frame(fill=0.05), ts=t + STEP)

    assert not res.detected
    assert r._det_state == "idle"
    assert r._det_last_box is None


# ------------------------------- EMIT SMOOTHING ------------------------------------ #

def test_emit_box_is_slew_capped_between_detections(monkeypatch):
    """A jittery fresh box moves the SERVED rectangle at most ~slew px/frame when the
    NCC match is not vouching for the position (retired _Tk display cap: 'a ~50px
    1-frame contour-SPLIT spike can't stride the box').

    The ribbon stays at its true position (x=600): detector JITTER is a wrong box on a
    static meter. (Drawing the ribbon at the jittered position instead models a real
    60px meter teleport, which the READ-RESCUE now legitimately snaps to -- an
    unreadable served box reseats on the same-frame detection by design.)"""
    r = _mk_reader(monkeypatch, ORION_METER_DETECTOR_TRACK="0")   # no NCC -> slew path
    t = _lock(r)
    res0 = _feed(r, t + STEP, box=(600, 300, 24, 107), conf=0.9)
    cx0 = res0.bbox[0] + res0.bbox[2] * 0.5
    # in-gate but 60px away (inside the ~427px tracking gate, far beyond the slew)
    res1 = _feed(r, t + 2 * STEP, box=(660, 300, 24, 107), conf=0.9, x=600)
    cx1 = res1.bbox[0] + res1.bbox[2] * 0.5
    assert abs(cx1 - cx0) <= r._det_emit_slew + 1.0


# ------------------------------- KILL SWITCH --------------------------------------- #

def test_lifecycle_off_restores_single_frame_adoption(monkeypatch):
    """ORION_METER_LIFECYCLE=0 must reproduce the pre-port behaviour: one fresh box is
    adopted immediately (no pending phase), and a teleport moves the lock."""
    r = _mk_reader(monkeypatch, lifecycle="0")
    res = _feed(r, 1000.0, box=(600, 300, 24, 107), conf=0.40)
    assert res.detected                        # instant adoption, no hysteresis
    res = _feed(r, 1000.0 + STEP, box=(100, 300, 24, 107), conf=0.40, x=100)
    assert res.detected
    assert abs((res.bbox[0] + res.bbox[2] * 0.5) - 112) < 40   # teleport followed


# ------------------------------- TRACK CONTINUITY ---------------------------------- #
# The Left_Fade zero-fill dropout fix (2026-08-30): during a camera pan the detector's
# newest box is ~60-180ms stale (7-24px behind at pan speed) and the shipped tracker
# seeded its template from the CURRENT frame at that STALE box, so the NCC self-match
# pinned the box behind the meter and the fill read returned 0.00 on most of the rise
# (measured: session_20260828_195034 e12, 69% dropout, detector_authority_lost_abort).
# _det_track_step keeps the tracked box across frames and re-seeds from the TRACKED
# position, bounded to the detector by a reconcile pull + snap gate.

def _pan_run(monkeypatch, cont, vx_px_s=-240.0, lag_s=0.10, refresh=3, n=60):
    """Ribbon pans left at vx; detector results refresh every `refresh` frames and are
    always `lag_s` STALE (position where the ribbon WAS), like the async YOLO worker."""
    r = _mk_reader(monkeypatch, ORION_METER_TRACK_CONTINUITY=cont)
    fd = r._meter_detector
    x0, y, w, h = 700.0, 300, 24, 107
    out = []
    last = None
    for i in range(n):
        t = 1000.0 + i * STEP
        x_true = x0 + vx_px_s * (i * STEP)
        fill = min(0.95, 0.05 + 0.015 * i)
        if i % refresh == 0:
            t_cap = t - lag_s
            x_stale = x0 + vx_px_s * max(0.0, (i * STEP) - lag_s)
            last = (True, (int(round(x_stale)), y, w, h), 0.9, t_cap)
        fd.script[fd.key(t)] = last
        r.set_shot_state(True, 1.0, True)
        res = r.detect(_frame(x=int(round(x_true)), fill=fill), ts=t)
        out.append((i, x_true, res))
    return out


def test_pan_with_stale_detector_keeps_genuine_fill(monkeypatch):
    """Continuity ON: through a fade-speed pan with a stale detector, every served
    frame past the bias-decay window must carry a GENUINE non-zero fill and a box
    that actually sits on the ribbon."""
    out = _pan_run(monkeypatch, cont="1")
    late = [(i, xt, res) for i, xt, res in out if i >= 24 and res.detected]
    assert len(late) >= 20
    zeros = [i for i, _, res in late if res.fill_pct <= 0.5]
    assert zeros == []
    errs = [abs((res.bbox[0] + res.bbox[2] * 0.5) - (xt + 12.0)) for _, xt, res in late]
    assert max(errs) < 10.0


def test_pan_dropout_reproduces_with_continuity_off(monkeypatch):
    """The same pan with ORION_METER_TRACK_CONTINUITY=0 must reproduce the measured
    defect (missing white-edge frames) -- proving the ON-arm test has teeth."""
    out = _pan_run(monkeypatch, cont="0")
    late = [(i, xt, res) for i, xt, res in out if i >= 24]
    missing = [i for i, _, res in late
               if not res.detected and res.rejection_reason == "detector_white_ribbon_missing"]
    assert len(missing) >= 3
    assert all(not res.detected or res.fill_pct > 0.5 for _, _, res in late)


def test_no_fabricated_fill_when_ribbon_vanishes(monkeypatch):
    """If the meter disappears while the detector still repeats a stale found result,
    the tracker must NOT fabricate a fill from a wrong box: the honest output is a
    zero-fill sample (a wrong box with a confident fill is worse than a dropout)."""
    r = _mk_reader(monkeypatch)
    t = _lock(r)
    fd = r._meter_detector
    stale = (True, (600, 300, 24, 107), 0.9, t)
    for i in range(1, 4):
        fd.script[fd.key(t + i * STEP)] = stale        # repeat: no fresh verdict
        r.set_shot_state(True, 1.0, True)
        res = r.detect(np.zeros((720, 1280, 3), np.uint8), ts=t + i * STEP)
    assert res.fill_pct == 0.0


def test_track_state_cleared_on_lock_reset(monkeypatch):
    """The tracked box and template stamp are per-lock artefacts: a reset must clear
    them so a dead lock's position cannot steer the next one."""
    r = _mk_reader(monkeypatch)
    t = _lock(r)
    assert r._det_track_box is not None
    r._det_reset_lock_state()
    assert r._det_track_box is None
    assert r._det_tmpl is None
    assert r._det_tmpl_ts < -1.0e8
