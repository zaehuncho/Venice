"""DynamicROILock re-acquire coverage across court positions.

Guards the stale-lock pathology: once locked on a right-side meter, the tight
locked crop excluded a new left-side meter (next shot) until the lock timed out
(~8 miss frames = most of a fast shot) — so left/top/wing shots were missed. The
fix: a MISS falls back to the wide search immediately, and a found meter at a far
horizontal position SNAPS the lock to it instead of EMA-drifting.
"""
from meter_detector import DynamicROILock, DetectorConfig

W, H = 1280, 720
RIGHT = (1050, 400, 30, 120)   # right-side meter bbox (x,y,w,h)
LEFT = (180, 400, 30, 120)     # left-side meter, far horizontal jump


def _zone_label(lock):
    return lock.get_search_zones(W, H, DetectorConfig())[0][0]


def _zone_cx(lock):
    _, x1, _, x2, _ = lock.get_search_zones(W, H, DetectorConfig())[0]
    return (x1 + x2) / 2.0


def test_locks_then_widens_on_miss():
    lock = DynamicROILock(lock_after_frames=1, timeout_frames=8)
    lock.update(True, RIGHT)
    assert _zone_label(lock) == "locked"          # solid lock -> tight crop
    lock.update(False, None)                       # one miss
    assert _zone_label(lock) == "wide"             # must widen immediately, not after timeout


def test_snaps_lock_to_new_court_position():
    lock = DynamicROILock(lock_after_frames=1, timeout_frames=8)
    for _ in range(3):
        lock.update(True, RIGHT)
    right_cx = _zone_cx(lock)
    assert right_cx > W * 0.6                       # locked crop is on the right

    # New shot on the left: a miss (lock widens), then the wide search finds the
    # left meter and the lock snaps to it.
    lock.update(False, None)
    lock.update(True, LEFT)
    assert _zone_label(lock) == "locked"
    left_cx = _zone_cx(lock)
    assert left_cx < W * 0.4, f"lock should snap to the left meter, got cx={left_cx}"


def test_small_drift_does_not_snap():
    # Normal sub-threshold drift stays EMA-smoothed (no snap), lock stays put.
    lock = DynamicROILock(lock_after_frames=1, timeout_frames=8)
    for _ in range(3):
        lock.update(True, RIGHT)
    base = _zone_cx(lock)
    lock.update(True, (RIGHT[0] + 6, RIGHT[1], RIGHT[2], RIGHT[3]))  # 6px drift
    assert abs(_zone_cx(lock) - base) < 40
