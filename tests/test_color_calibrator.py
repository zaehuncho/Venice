"""ColorCalibrator (plan B1) + calibration lifecycle (plan B3) coverage.

Commit-or-discard staging, envelope clamping (v1 envelope == the shipped constants: learning
strictly NARROWS), the channel-gap invariant, FROZEN bake-once, per-lock learned bands with the
same-frame default fallback, the SEED->LEARNING->LOCKED->PROVISIONAL->RELEARN state machine,
persistence to calibration/meter_color_profiles.json (replace-not-blend), and the fingerprint
arena-switch path. Reader-level tests drive real synthetic frames through hw-armed windows;
lifecycle-level tests inject staged histograms directly.
"""
import json
import os

import numpy as np
import pytest
import cv2

from simple_meter_reader import ColorCalibrator, SimpleMeterReader, _hist_median_mad, _band_iou

H, W = 1080, 1920
FLOOR_Y = 600
TRACK_TOP_Y = 470
COL_X = 900
COL_W = 24
RED_A = (0, 0, 255)
RED_B = (20, 20, 250)      # inside the shipped band AND inside a typical learned band
GREEN = (60, 200, 60)      # HSV hue 60, S/V comfortably above the 90 floors


def _frame(fill_frac, red_a=RED_A, red_b=RED_B, green_bgr=GREEN, tip_h=14):
    f = np.full((H, W, 3), 40, np.uint8)
    track_h = FLOOR_Y - TRACK_TOP_Y
    top = int(round(FLOOR_Y - fill_frac * track_h))
    half = COL_W // 2
    if fill_frac > 0:
        f[top:FLOOR_Y, COL_X:COL_X + half] = red_a
        f[top:FLOOR_Y, COL_X + half:COL_X + COL_W] = red_b
    f[TRACK_TOP_Y - tip_h:TRACK_TOP_Y, COL_X:COL_X + COL_W + 6] = green_bgr
    return f


def _mk_reader(tmp_path, **kw):
    cal = ColorCalibrator(profile_path=str(tmp_path / "meter_color_profiles.json"))
    r = SimpleMeterReader(W, H, calibrator=cal, **kw)
    return r, cal


def _run_shot(r, fills, ts0=0.0, frame_fn=_frame, release=False, reset=True):
    """One hardware-armed training shot: arm -> frames -> release marker -> disarm edge.

    `reset` runs the trailing reset_tracking(). That call means "the meter CHANGED colour/style"
    (the only thing that triggers it live), which is an EPOCH boundary: it now also drops the
    session-scoped scale calibration learned from the OLD meter (reader.reset_session_scale()).
    Pass reset=False when the test needs to inspect that session state after the shot.
    """
    r.set_shot_state(True, 1.0, True)
    t = ts0
    for i, fr in enumerate(fills):
        r.set_shot_state(True, 1.0, True)
        r.read(frame_fn(fr), ts=t)
        t += 1 / 60.0
    if release:
        r.notify_release(1)
    r.set_shot_state(False, 0.0, False)
    r.read(np.full((H, W, 3), 40, np.uint8), ts=t)   # falling hw edge -> end_shot
    if reset:
        r.reset_tracking()
    return t + 1 / 60.0


RISING = list(np.linspace(0.30, 1.00, 24))


# --------------------------------------------------------------------------- #
#  band derivation: envelope clamp, channel gap, strict narrowing
# --------------------------------------------------------------------------- #
def _conc_hist(vals_counts, size=256):
    h = np.zeros(size, np.int64)
    for v, c in vals_counts:
        h[int(v)] += int(c)
    return h


def _stage_hists(red_bgr=(10, 10, 250), hue=50, s=200, v=200, n=5000):
    red = np.stack([_conc_hist([(red_bgr[0], n)]), _conc_hist([(red_bgr[1], n)]),
                    _conc_hist([(red_bgr[2], n)])])
    green = np.stack([_conc_hist([(hue, n)]), _conc_hist([(s, n)]), _conc_hist([(v, n)])])
    return red, green


def test_derive_bands_strictly_narrow_inside_envelope(tmp_path):
    cal = ColorCalibrator(profile_path=str(tmp_path / "p.json"))
    red, green = _stage_hists()
    bands = cal._derive_bands(red, green)
    assert bands is not None
    for c in range(3):
        assert bands["red_lo"][c] >= cal.RED_ENV[0][c]
        assert bands["red_hi"][c] <= cal.RED_ENV[1][c]
        assert bands["green_lo"][c] >= cal.GREEN_ENV[0][c]
        assert bands["green_hi"][c] <= cal.GREEN_ENV[1][c]
    # channel-gap invariant: gray/mullion unrepresentable
    assert bands["red_hi"][0] <= bands["red_lo"][2] - 80
    assert bands["red_hi"][1] <= bands["red_lo"][2] - 80
    # genuinely narrower than the shipped band on the learnable channels
    assert bands["red_hi"][0] < cal.RED_ENV[1][0] or bands["red_lo"][2] > cal.RED_ENV[0][2]
    # green S/V floors are RELATIVE but never below the envelope floors
    assert bands["green_lo"][1] >= 90 and bands["green_lo"][2] >= 90


def test_derive_bands_seed_center_containment(tmp_path):
    cal = ColorCalibrator(profile_path=str(tmp_path / "p.json"))
    # extreme narrow red high in R: the band must still contain the envelope center per channel
    red, green = _stage_hists(red_bgr=(0, 0, 255), hue=80)
    bands = cal._derive_bands(red, green)
    assert bands is not None
    for c in range(3):
        center = 0.5 * (cal.RED_ENV[0][c] + cal.RED_ENV[1][c])
        assert bands["red_lo"][c] <= center <= bands["red_hi"][c]
    gc = 0.5 * (cal.GREEN_ENV[0][0] + cal.GREEN_ENV[1][0])
    assert bands["green_lo"][0] <= gc <= bands["green_hi"][0]


# --------------------------------------------------------------------------- #
#  commit-or-discard through the reader (real frames, hw-armed windows only)
# --------------------------------------------------------------------------- #
def test_training_shot_commits(tmp_path):
    r, cal = _mk_reader(tmp_path)
    _run_shot(r, RISING)
    assert cal.committed_shots == 1
    assert cal.state == "learning"          # SEED -> LEARNING on the first commit


def test_low_peak_shot_discarded(tmp_path):
    r, cal = _mk_reader(tmp_path)
    _run_shot(r, list(np.linspace(0.30, 0.55, 24)))   # peak ~55 < 70
    assert cal.committed_shots == 0


def test_cv_armed_window_never_trains(tmp_path):
    """The MERGED arm alone (armed=True, armed_hw=False) must never open a staging window --
    a CV self-armed false lock training itself was the failure mode B1 kills."""
    r, cal = _mk_reader(tmp_path)
    t = 0.0
    for fr in RISING:
        r.set_shot_state(True, 1.0, False)   # merged armed, NOT hw-armed
        r.read(_frame(fr), ts=t)
        t += 1 / 60.0
    r.set_shot_state(False, 0.0, False)
    r.read(np.full((H, W, 3), 40, np.uint8), ts=t)
    assert cal.committed_shots == 0
    assert cal._stage is None               # no staging window ever opened


def test_contaminated_green_hue_discards_whole_shot(tmp_path):
    """Green tip painted in 3 hue thirds (42/60/78) -> hue MAD ~18 > 8 -> the shot contributes
    NOTHING (commit gate, plan B1)."""
    hsv_cols = [cv2.cvtColor(np.uint8([[[h_, 200, 200]]]), cv2.COLOR_HSV2BGR)[0, 0]
                for h_ in (42, 60, 78)]

    def frame_fn(fr):
        f = _frame(fr, green_bgr=(0, 0, 0), tip_h=14)
        wthird = (COL_W + 6) // 3
        for i, c in enumerate(hsv_cols):
            x0 = COL_X + i * wthird
            f[TRACK_TOP_Y - 14:TRACK_TOP_Y, x0:x0 + wthird] = c
        return f

    r, cal = _mk_reader(tmp_path)
    _run_shot(r, RISING, frame_fn=frame_fn)
    assert cal.committed_shots == 0


def test_red_mad_commit_gate_direct(tmp_path):
    """The explicitly staged R-channel histogram gate (MAD <= 60): a bimodal R spread refuses
    the commit. (Under the v1 envelope R is confined to [220,255] so this gate only bites at a
    wider v2 envelope -- tested at the staging level.)"""
    cal = ColorCalibrator(profile_path=str(tmp_path / "p.json"))
    cal.begin_shot()
    red, green = _stage_hists()
    red[2] = _conc_hist([(100, 5000), (255, 5000)])   # bimodal R -> MAD ~77
    cal._stage["red"] = red
    cal._stage["green"] = green
    cal._stage["frames"] = 12
    cal._stage["peak"] = 95.0
    cal.end_shot()
    assert cal.committed_shots == 0


# --------------------------------------------------------------------------- #
#  FROZEN bake + learned bands + same-frame default fallback
# --------------------------------------------------------------------------- #
def _train_to_frozen(r, cal):
    t = 0.0
    for _ in range(ColorCalibrator.FROZEN_SHOTS):
        t = _run_shot(r, RISING, ts0=t)
    return t


def test_bake_once_at_frozen_and_reader_adopts(tmp_path):
    r, cal = _mk_reader(tmp_path)
    _train_to_frozen(r, cal)
    assert cal.state == "locked"
    assert cal.baked is not None
    assert cal.learned_date != ""
    # baked bands strictly inside the envelope
    for c in range(3):
        assert cal.baked["red_lo"][c] >= cal.RED_ENV[0][c]
        assert cal.baked["red_hi"][c] <= cal.RED_ENV[1][c]
    # reader adopted the learned bands...
    assert r._learned_red is not None
    # ...and a fresh acquire latches them per-lock
    out = r.read(_frame(0.8), ts=100.0)
    assert out["detected"] is True
    assert r._lock_red == r._learned_red


def test_persistence_written_at_frozen_replace_not_blend(tmp_path):
    path = tmp_path / "meter_color_profiles.json"
    r, cal = _mk_reader(tmp_path)
    _train_to_frozen(r, cal)
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["profiles"]) == 1
    p0 = data["profiles"][0]
    for k in ("red_lo", "red_hi", "green_lo", "green_hi", "learned_date", "fingerprint", "stats"):
        assert k in p0
    # re-learn + re-bake with the SAME fingerprint -> profile REPLACED, not appended/blended
    cal._relearn()
    r._apply_baked_bands()
    _train_to_frozen(r, cal)
    data2 = json.loads(path.read_text(encoding="utf-8"))
    assert len(data2["profiles"]) == 1


def test_same_frame_default_band_fallback_on_learned_miss(tmp_path):
    r, cal = _mk_reader(tmp_path)
    _train_to_frozen(r, cal)
    assert r._learned_red is not None and r._learned_red[1][0] < 60   # hi_B narrowed
    # a meter whose B/G sits outside the LEARNED band but inside the DEFAULT band: the learned
    # scan sees only half the column (too thin) -> the SAME-FRAME default retry must lock it.
    frame = _frame(0.9, red_b=(50, 50, 255))
    out = r.read(frame, ts=200.0)
    assert out["detected"] is True                     # no 8-frame streak wait (B1 [fix])
    assert r._lock_red is None                         # this lock rides the default band
    assert cal.learned_miss_streak == 1


# --------------------------------------------------------------------------- #
#  lifecycle: IoU resets, PROVISIONAL, RELEARN, fingerprint switch
# --------------------------------------------------------------------------- #
def _inject_shot(cal, hue=50, red_bgr=(10, 10, 250), peak=95.0, frames=12,
                 locked=True, release=False):
    cal.begin_shot()
    red, green = _stage_hists(red_bgr=red_bgr, hue=hue)
    cal._stage["red"] = red
    cal._stage["green"] = green
    cal._stage["frames"] = frames
    cal._stage["peak"] = peak
    cal._stage["locked_any"] = locked
    if locked:
        cal._stage["frames_to_lock"] = 3
    cal._stage["release"] = release
    cal.end_shot()


def test_iou_disagreement_resets_then_seed(tmp_path):
    cal = ColorCalibrator(profile_path=str(tmp_path / "p.json"))
    for i in range(3):
        _inject_shot(cal, hue=50)            # commit
        assert cal.committed_shots == 1
        _inject_shot(cal, hue=80)            # disagrees (hue IoU 0) -> reset
        assert cal.committed_shots == 0
        assert cal.reset_count == i + 1
    assert cal.state == "seed"               # 3 resets -> SEED


def test_locked_to_provisional_on_armed_no_locks_then_relearn(tmp_path):
    cal = ColorCalibrator(profile_path=str(tmp_path / "p.json"))
    for _ in range(ColorCalibrator.FROZEN_SHOTS):
        _inject_shot(cal, hue=50)
    assert cal.state == "locked"
    for _ in range(3):                       # 3 armed no-lock shots
        _inject_shot(cal, locked=False, peak=0.0, frames=0)
    assert cal.state == "provisional"
    assert cal.baked is not None             # learned bands FROZEN while provisional
    for _ in range(ColorCalibrator.NOLOCKS_TO_RELEARN):
        _inject_shot(cal, locked=False, peak=0.0, frames=0)
    assert cal.state == "relearn"
    assert cal.baked is None                 # revert to defaults, zero histograms


def test_provisional_back_to_locked_on_corroborated_clean_locks(tmp_path):
    cal = ColorCalibrator(profile_path=str(tmp_path / "p.json"))
    for _ in range(ColorCalibrator.FROZEN_SHOTS):
        _inject_shot(cal, hue=50)
    cal.state = "provisional"
    cal._provisional_clean = 0
    cal._provisional_nolocks = 0
    for _ in range(3):                       # corroborated = release marker + peak >= 70
        _inject_shot(cal, locked=True, peak=90.0, release=True)
    assert cal.state == "locked"


def test_fingerprint_arena_switch_reverts_without_profile(tmp_path):
    cal = ColorCalibrator(profile_path=str(tmp_path / "p.json"))
    for _ in range(ColorCalibrator.FROZEN_SHOTS):
        _inject_shot(cal, hue=50)
    assert cal.state == "locked"
    fpA = np.zeros(32); fpA[0] = 1.0
    cal.fingerprint = fpA
    fpB = np.zeros(32); fpB[16] = 1.0        # distance 1.0 > 0.35 -> arena switch
    cal.begin_shot(fingerprint=fpB)
    assert cal.state == "learning"           # no saved profile matches -> revert + LEARNING
    assert cal.baked is None


def test_fingerprint_arena_switch_adopts_matching_profile_verify_first(tmp_path):
    cal = ColorCalibrator(profile_path=str(tmp_path / "p.json"))
    for _ in range(ColorCalibrator.FROZEN_SHOTS):
        _inject_shot(cal, hue=50)
    fpA = np.zeros(32); fpA[0] = 1.0
    fpB = np.zeros(32); fpB[16] = 1.0
    cal.fingerprint = fpA
    cal._profiles.append({
        "style": "Arrow2", "color": "Red", "fingerprint": list(fpB),
        "red_lo": [0, 0, 230], "red_hi": [40, 40, 255],
        "green_lo": [45, 95, 95], "green_hi": [75, 255, 255],
        "learned_date": "2026-01-01",
    })
    cal.begin_shot(fingerprint=fpB)
    assert cal.state == "provisional"        # matched profile: verify-before-trust
    assert cal._verify_pending is True
    assert cal.baked["red_lo"] == (0, 0, 230)
    # first verification shot WITHOUT demonstrated inliers -> revert + LEARNING
    cal._stage["locked_any"] = True
    cal._stage["release"] = True
    cal._stage["peak"] = 90.0
    cal.end_shot()
    assert cal.state == "learning"


# --------------------------------------------------------------------------- #
#  calibrate_meter window (B3): snapshot / relearn / auto-bake / cancel / exhaustion
# --------------------------------------------------------------------------- #
def test_user_calibration_start_commits_bake_and_lock(tmp_path):
    cal = ColorCalibrator(profile_path=str(tmp_path / "p.json"))
    cal.start_user_calibration(target=5, window=10)
    assert cal.state == "relearn"
    st = cal.status()
    assert st["calibrating"] is True and st["shots_needed"] == 5 and st["shots_done"] == 0
    for i in range(5):
        _inject_shot(cal, hue=50)
    assert cal.state == "locked"             # auto-bake at 5 committed shots
    assert cal._user_cal is None
    assert cal.status()["calibrating"] is False


def test_user_calibration_cancel_restores_snapshot(tmp_path):
    cal = ColorCalibrator(profile_path=str(tmp_path / "p.json"))
    for _ in range(ColorCalibrator.FROZEN_SHOTS):
        _inject_shot(cal, hue=50)
    baked_before = dict(cal.baked)
    cal.start_user_calibration()
    assert cal.state == "relearn" and cal.baked is None
    cal.cancel_user_calibration()
    assert cal.state == "locked"
    assert dict(cal.baked) == baked_before


def test_user_calibration_window_exhaustion_restores(tmp_path):
    cal = ColorCalibrator(profile_path=str(tmp_path / "p.json"))
    for _ in range(ColorCalibrator.FROZEN_SHOTS):
        _inject_shot(cal, hue=50)
    cal.start_user_calibration(target=5, window=10)
    for _ in range(10):                      # 10 shots, zero commits
        _inject_shot(cal, locked=False, peak=0.0, frames=0)
    assert cal._user_cal is None
    assert cal.state == "locked"             # snapshot restored
    assert cal.baked is not None


def test_status_version_bumps_on_shot_boundaries(tmp_path):
    cal = ColorCalibrator(profile_path=str(tmp_path / "p.json"))
    v0 = cal.status()["version"]
    cal.begin_shot()
    v1 = cal.status()["version"]
    cal.end_shot()
    v2 = cal.status()["version"]
    assert v0 < v1 < v2


# --------------------------------------------------------------------------- #
#  corroborated scale rebase (B2, robust flag)
# --------------------------------------------------------------------------- #
def test_scale_rebase_after_corroborated_shot(tmp_path, monkeypatch):
    monkeypatch.setenv("ORION_READER_ROBUST", "1")
    r, cal = _mk_reader(tmp_path)
    assert r._robust is True
    fills = list(np.linspace(0.35, 1.0, 40))          # >= 30 locked red frames
    # reset=False: the trailing reset_tracking() is a meter-CHANGED signal, and the rebase being
    # asserted here is exactly the per-meter calibration an epoch boundary must discard (below).
    _run_shot(r, fills, release=True, reset=False)
    assert r._dims_rebased is True
    assert r._meas_w_min is not None and r._meas_w_max > r._meas_w_min
    assert r._meas_h_max > r._h_acq
    # ratios of the measured width, +-15%-clamped around the seed centre
    seed_w = 0.5 * (r._w_min + r._w_max)
    assert 0.70 * 0.85 * seed_w <= r._meas_w_min <= 0.70 * 1.15 * seed_w + 1
    # EPOCH: a colour/style change is a DIFFERENT meter, so the dimensions measured off the old
    # one must not gate the new one -- reset_tracking() drops the whole session scale calibration.
    r.reset_tracking()
    assert r._dims_rebased is False
    assert r._meas_w_min is None and r._meas_h_max is None and r._meas_g_area is None
    assert r._size_base == 0.0 and r._scale_est == 1.0


def test_no_rebase_without_release_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("ORION_READER_ROBUST", "1")
    r, cal = _mk_reader(tmp_path)
    fills = list(np.linspace(0.35, 1.0, 40))
    _run_shot(r, fills, release=False)                # peak fine, but no corroboration
    assert r._dims_rebased is False


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #
def test_hist_median_mad_and_band_iou():
    h = np.zeros(256, np.int64)
    h[100] = 60; h[110] = 40
    med, mad = _hist_median_mad(h)
    assert med == 100 and mad == 0
    assert _band_iou((0, 0, 220), (60, 60, 255), (0, 0, 220), (60, 60, 255)) == pytest.approx(1.0)
    assert _band_iou((40, 0, 0), (49, 10, 10), (61, 0, 0), (84, 10, 10)) == 0.0
