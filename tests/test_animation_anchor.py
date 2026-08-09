"""Synthetic-frame tests for the shadow-first animation anchor (animation_anchor.py).

Proves the MECHANISM offline (no real footage needed): uniform frames give an exactly-controllable
frame-diff motion energy, so we can drive a rise-then-fall and assert the peak fires once. The real
landmark->tip timing is a LIVE calibration (see docs/ANIMATION_ANCHOR.md); these tests only guard the
detector logic + the flag gate.
"""
import numpy as np

from animation_anchor import AnimationAnchor, MotionPeakDetector, AnchorResult


def _frame(val, shape=(40, 60, 3)):
    return np.full(shape, val, dtype=np.uint8)


# Per-frame deltas 5,10,20,30 (sustained rise) then 5,2,0 (fall) -> a smoothed motion-energy peak.
_RISE_FALL_VALUES = [0, 5, 15, 35, 65, 70, 72, 72]


def test_disabled_is_a_true_noop():
    a = AnimationAnchor(enabled=False)
    assert not any(a.update(_frame(v)).found for v in _RISE_FALL_VALUES)


def test_motion_peak_fires_exactly_once_on_rise_then_fall():
    a = AnimationAnchor(enabled=True)
    results = [a.update(_frame(v)) for v in _RISE_FALL_VALUES]
    found = [r for r in results if r.found]
    assert len(found) == 1, f"expected exactly one anchor, got {len(found)}"
    assert found[0].kind == "motion_peak"
    assert 0.0 < found[0].confidence <= 1.0
    assert found[0].ts > 0.0


def test_static_frames_never_fire():
    a = AnimationAnchor(enabled=True)
    assert not any(a.update(_frame(50)).found for _ in range(12))


def test_reset_allows_a_new_shot_to_fire_again():
    a = AnimationAnchor(enabled=True)
    for v in _RISE_FALL_VALUES:
        a.update(_frame(v))
    a.reset()
    results = [a.update(_frame(v)) for v in _RISE_FALL_VALUES]
    assert any(r.found for r in results), "after reset, a new shot's peak must fire again"


def test_region_crop_does_not_crash_and_still_detects():
    a = AnimationAnchor(enabled=True, detector=MotionPeakDetector(motion_floor=1.0))
    region = (10, 5, 30, 20)  # x, y, w, h within the 60x40 frame
    results = [a.update(_frame(v), region=region) for v in _RISE_FALL_VALUES]
    assert any(r.found for r in results)


def test_empty_frame_is_safe():
    a = AnimationAnchor(enabled=True)
    r = a.update(np.zeros((0, 0, 3), dtype=np.uint8))
    assert not r.found
