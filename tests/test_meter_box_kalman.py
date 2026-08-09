"""MeterBoxKalman — the persistent position+size tracker over the SERVED meter geometry.

Unit tests drive the filter directly (static / pan / sliver / jump / predict / gating); integration
tests drive MeterDetector end-to-end on synthetic park frames and prove (a) the flag OFF is inert
(byte-identical served geometry), (b) the flag ON strictly reduces served-bbox jitter, and (c) a
one-frame width sliver can no longer collapse the served width.
"""
import os
import sys
from pathlib import Path

import numpy as np
import cv2
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from meter_box_kalman import MeterBoxKalman, try_load
from meter_detector import DetectorConfig, MeterDetector

from test_meter_detector_track_t import (  # reuse the synthetic-park helpers
    _park_cfg, _park_frame, _FakeLocator, MX, MW, BOT, FULL_H, STYLES,
)


# --------------------------------------------------------------------------- #
# unit — the filter itself
# --------------------------------------------------------------------------- #
def _bt():
    return MeterBoxKalman()


def test_static_meter_converges_and_holds():
    bt = _bt()
    t = 0.0
    for _ in range(12):
        assert bt.update_pose(400.0, 700.0, 32.0, t, 120.0) in ("seed", "accept")
        t += 1 / 30
    assert bt.ready
    assert abs(bt.cx() - 400.0) < 0.5
    assert abs(bt.cb() - 700.0) < 0.5
    assert abs(bt.width() - 32.0) < 0.5


def test_linear_pan_tracks_with_low_lag():
    bt = _bt()
    t = 0.0
    x = 400.0
    for _ in range(20):
        bt.update_pose(x, 700.0, 32.0, t, 120.0)
        t += 1 / 30
        x += 240.0 / 30          # 240 px/s pan
    lag = abs(bt.cx() - (x - 240.0 / 30))
    assert lag < 3.0, f"CV filter must track a steady pan (lag {lag:.2f}px)"


def test_sliver_width_rejected_position_still_updates():
    bt = _bt()
    t = 0.0
    for _ in range(8):
        bt.update_pose(400.0, 700.0, 32.0, t, 120.0)
        t += 1 / 30
    ev = bt.update_pose(404.0, 700.0, 7.0, t, 120.0)   # w=7 sliver, small position step
    assert ev == "accept"
    assert bt.width() > 25.0, "a one-frame sliver must not collapse the width channel"
    assert bt.cx() > 400.0, "position must still update on a size-outlier frame"


def test_jump_rejects_then_reseeds_on_third():
    bt = _bt()
    t = 0.0
    for _ in range(6):
        bt.update_pose(400.0, 700.0, 32.0, t, 120.0)
        t += 1 / 30
    assert bt.update_pose(1500.0, 300.0, 30.0, t, 120.0) == "reject"
    assert abs(bt.cx() - 400.0) < 2.0, "a single teleport must not yank the track"
    t += 1 / 30
    assert bt.update_pose(1500.0, 300.0, 30.0, t, 120.0) == "reject"
    t += 1 / 30
    assert bt.update_pose(1500.0, 300.0, 30.0, t, 120.0) == "reseed"
    assert abs(bt.cx() - 1500.0) < 1.0, "3 consecutive far measurements = a real new meter"


def test_predict_is_pure_and_coast_bounded():
    bt = _bt()
    t = 0.0
    for _ in range(5):
        bt.update_pose(400.0, 700.0, 32.0, t, 120.0)
        t += 1 / 30
    p1 = bt.predict_pose(t + 0.1)
    p2 = bt.predict_pose(t + 0.1)
    assert p1 is not None and p1 == p2, "predict must not mutate state"
    assert bt.predict_pose(t + 1.0) is None, "beyond the coast horizon -> None"
    assert MeterBoxKalman().predict_pose(0.0) is None, "not ready -> None"


def test_gap_reseeds():
    bt = _bt()
    bt.update_pose(400.0, 700.0, 32.0, 0.0, 120.0)
    for i in range(4):
        bt.update_pose(400.0, 700.0, 32.0, (i + 1) / 30, 120.0)
    assert bt.update_pose(900.0, 500.0, 28.0, 5.0, 120.0) == "reseed"
    assert abs(bt.cx() - 900.0) < 1.0


def test_height_channel_green_fed_and_can_decrease():
    bt = _bt()
    for i in range(4):
        bt.update_height(100.0, float(i))
    assert bt.ready_h and abs(bt.height() - 100.0) < 2.0
    for i in range(4, 12):
        bt.update_height(70.0, float(i))   # genuine zoom-out: full height shrinks
    assert bt.height() < 90.0, "the height channel must be allowed to DECREASE (no max-hold)"
    h_before = bt.height()
    bt.update_height(20.0, 12.0)           # sliver-guard: an implausible height is rejected
    assert abs(bt.height() - h_before) < 2.0


def test_try_load_flag_gating(monkeypatch):
    monkeypatch.setenv("ORION_METER_TRACK", "0")
    assert try_load() is None
    monkeypatch.setenv("ORION_METER_TRACK", "1")
    assert try_load() is not None


# --------------------------------------------------------------------------- #
# integration — through MeterDetector on synthetic park frames
# --------------------------------------------------------------------------- #
def _jitter_frames(n=36):
    """A rising meter whose drawn x jitters +/-3px frame-to-frame (contour measurement noise)."""
    frames = []
    for i in range(n):
        fill = min(FULL_H - 5, 20 + int(i * (FULL_H - 25) / n))
        dx = 3 if (i % 2) else -3
        rng = np.random.default_rng(7)
        fr = rng.integers(28, 52, size=(720, 1280, 3), dtype=np.uint8)
        x = MX + dx
        cv2.rectangle(fr, (x - 3, (BOT - FULL_H) - 3), (x + MW + 3, BOT + 3), (8, 8, 8), 3)
        cv2.rectangle(fr, (x, BOT - fill), (x + MW, BOT), (30, 30, 230), -1)
        cv2.rectangle(fr, (x, BOT - FULL_H), (x + MW, BOT - FULL_H + 6), (40, 255, 60), -1)
        frames.append(fr)
    return frames


def _run_detector(frames, track_on, monkeypatch):
    monkeypatch.setenv("ORION_METER_TRACK", "1" if track_on else "0")
    det = MeterDetector(STYLES, _park_cfg())
    det._locator = _FakeLocator((MX - 40, BOT - FULL_H - 30, MW + 80, FULL_H + 60), 0.92)
    det._stability._locator_mode = True
    xs, ws = [], []
    for fr in frames:
        r = det.detect(fr)
        if r.detected:
            xs.append(r.bbox[0])
            ws.append(r.bbox[2])
    return xs, ws, det


def test_flag_off_is_inert_and_deterministic(monkeypatch):
    frames = _jitter_frames()
    xs1, ws1, det1 = _run_detector(frames, False, monkeypatch)
    xs2, ws2, det2 = _run_detector(frames, False, monkeypatch)
    assert det1._box_track is None
    assert xs1 == xs2 and ws1 == ws2, "flag OFF must be deterministic (inertness baseline)"


def test_flag_on_reduces_served_jitter(monkeypatch):
    frames = _jitter_frames()
    xs_off, _, _ = _run_detector(frames, False, monkeypatch)
    xs_on, _, det = _run_detector(frames, True, monkeypatch)
    assert det._box_track is not None
    assert len(xs_on) >= len(xs_off) - 2, "smoothing must not lose detections"

    def _jit(xs):
        d = np.diff(np.asarray(xs, dtype=float))
        return float(np.std(d)) if d.size else 0.0

    j_off, j_on = _jit(xs_off), _jit(xs_on)
    assert j_on < j_off * 0.7, f"served-x jitter must drop with the tracker on ({j_on:.2f} vs {j_off:.2f})"


def test_flag_on_sliver_does_not_collapse_width(monkeypatch):
    frames = _jitter_frames(24)
    # one-frame sliver: redraw frame 15's fill at w=5 (contour catches a fragment)
    fr = frames[15].copy()
    rng = np.random.default_rng(7)
    fr[:, :, :] = rng.integers(28, 52, size=fr.shape, dtype=np.uint8)
    x = MX - 3 if (15 % 2) == 0 else MX + 3
    cv2.rectangle(fr, (x - 3, (BOT - FULL_H) - 3), (x + MW + 3, BOT + 3), (8, 8, 8), 3)
    cv2.rectangle(fr, (x, BOT - 70), (x + 5, BOT), (30, 30, 230), -1)   # w=5 sliver
    cv2.rectangle(fr, (x, BOT - FULL_H), (x + MW, BOT - FULL_H + 6), (40, 255, 60), -1)
    frames[15] = fr

    _, ws_on, _ = _run_detector(frames, True, monkeypatch)
    assert ws_on, "detections expected"
    w_med = float(np.median(ws_on))
    assert min(ws_on) > 0.55 * w_med, (
        f"a one-frame sliver must not collapse the served width (min {min(ws_on)} vs median {w_med})")


# --------------------------------------------------------------------------- #
# unit — VFX/carryover adjudication (confirm_feed strikes, rollback, belts)
# --------------------------------------------------------------------------- #
def test_confirm_feed_keeps_strikes_so_carryover_cannot_starve_reseed():
    """Live cut: the spent carryover bar keeps pixel-confirming at the OLD spot while the real
    next meter rises elsewhere — confirm-fed accepts must glue the track WITHOUT clearing the
    outlier strikes, so the 3rd consistent raw strike still adopts the new meter."""
    bt = _bt()
    t = 0.0
    for _ in range(6):
        bt.update_pose(1000.0, 300.0, 25.0, t, 150.0)
        t += 1 / 30
    evs = []
    for _ in range(3):
        evs.append(bt.update_pose(500.0, 400.0, 25.0, t, 150.0))          # raw: new meter far away
        t += 0.001
        if evs[-1] == "reject":
            evs.append(bt.update_pose(1000.0, 300.0, 25.0, t, 150.0,
                                      confirm_feed=True))                 # pixel-confirm at old spot
            t += 1 / 30 - 0.001
    assert evs == ["reject", "accept", "reject", "accept", "reseed"], evs
    assert abs(bt.cx() - 500.0) < 1.0 and abs(bt.cb() - 400.0) < 1.0


def test_rollback_reseed_restores_track_and_stale_undo_is_dropped():
    bt = _bt()
    t = 0.0
    for _ in range(6):
        bt.update_pose(1000.0, 300.0, 25.0, t, 150.0)
        t += 1 / 30
    for _ in range(2):
        assert bt.update_pose(500.0, 400.0, 25.0, t, 150.0) == "reject"
        t += 1 / 30
    assert bt.update_pose(500.0, 400.0, 25.0, t, 150.0) == "reseed"
    assert bt._reseed_undo is not None
    assert bt.rollback_reseed() is True, "caller pixel-vetoed the adopted spot"
    assert abs(bt.cx() - 1000.0) < 3.0 and abs(bt.cb() - 300.0) < 3.0
    assert bt.rollback_reseed() is False, "undo is single-use"
    t += 1 / 30
    bt.update_pose(1000.0, 300.0, 25.0, t, 150.0)
    assert bt._reseed_undo is None, "any later update invalidates the snapshot"


def test_sliver_width_strikes_never_adopt():
    """The green-flame VFX blob measures a sliver (w~12 vs the w~25 track): even three
    co-located strikes must not re-seed onto it."""
    bt = _bt()
    t = 0.0
    for _ in range(6):
        bt.update_pose(1000.0, 300.0, 25.0, t, 150.0)
        t += 1 / 30
    for i in range(4):
        assert bt.update_pose(1040.0, 90.0, 12.0, t, 150.0) == "reject", f"strike {i}"
        t += 1 / 30
    assert abs(bt.cx() - 1000.0) < 3.0, "track must still be on the meter"


def test_fast_linear_relocation_adopts_with_velocity():
    """A relocated meter panning >45px/frame defeats the mean-spread test but lies on one
    constant-velocity line — it must adopt, seeded WITH that velocity so the very next
    measurement accepts instead of re-entering reject cycling."""
    bt = _bt()
    t = 0.0
    for _ in range(6):
        bt.update_pose(400.0, 700.0, 32.0, t, 200.0)
        t += 1 / 30
    xs = [1500.0, 1554.0, 1608.0]                       # 54 px/frame @30fps
    evs = [bt.update_pose(x, 300.0, 30.0, t + i / 30, 200.0) for i, x in enumerate(xs)]
    assert evs == ["reject", "reject", "reseed"], evs
    assert abs(bt.cx() - 1608.0) < 1.0
    ev = bt.update_pose(1662.0, 300.0, 30.0, t + 3 / 30, 200.0)
    assert ev == "accept", "velocity-seeded track must accept the continuing pan"
