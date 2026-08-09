"""Regression tests for the _StabilityValidator tracking latch.

Root cause of the live "fade freeze": the validator HARD-RESET its consecutive-valid
counter whenever the meter's bbox moved more than position_jump_max_px (80px). On a
fade the meter slides across the court every frame, so it was permanently flagged
bbox_unstable -> the orchestrator feed gate dropped those frames -> the timing
engine's fill froze at the first sample -> timeout_fallback. Standstill/go-to (meter
roughly stationary) tracked fine, which is exactly the live pattern we saw.

Fix: once a shot is ACQUIRED (min_consecutive clean frames), latch tracking and FOLLOW
the meter through motion; only a teleport-sized jump breaks the lock. The acquisition
gate stays tight (and now scales with frame width) so idle blobs still can't latch.
"""
from types import SimpleNamespace

from meter_detector import DetectorConfig, _StabilityValidator


def _match(found=True, conf=0.8, x=200.0, y=400.0):
    return SimpleNamespace(found=found, confidence=float(conf), x=float(x), y=float(y))


def _cfg(**kw):
    c = DetectorConfig()
    c.min_consecutive_valid_frames = 3
    c.position_jump_max_px = 80.0
    c.tracking_jump_scale = 3.0
    c.jump_gate_ref_width = 720
    c.confidence_threshold = 0.35
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_acquires_after_min_consecutive():
    v = _StabilityValidator(_cfg())
    v.note_frame_width(1280)
    ts = 0.0
    assert v.validate(_match(x=200), 10, ts).valid is False
    assert v.validate(_match(x=201), 12, ts + 0.016).valid is False
    assert v.validate(_match(x=202), 14, ts + 0.032).valid is True  # 3rd -> latched


def test_fade_motion_stays_valid_once_tracking():
    """THE fix: a moving (fading) meter must keep producing VALID samples instead of
    collapsing to bbox_unstable every frame."""
    v = _StabilityValidator(_cfg())
    v.note_frame_width(1280)  # acq gate ~142px, track gate ~426px
    ts = 0.0
    for i in range(3):  # acquire while stationary
        v.validate(_match(x=200), 10 + i, ts)
        ts += 0.016
    assert v._tracking is True

    x = 200.0
    valids = []
    for i in range(8):  # meter slides ~100px/frame (a fade) -> would trip old 80px gate
        x += 100.0
        st = v.validate(_match(x=x, y=400), 20 + i, ts)
        ts += 0.016
        valids.append(st.valid)
    assert all(valids), f"fade motion should stay valid once tracking, got {valids}"


def test_teleport_breaks_tracking_and_reacquires():
    v = _StabilityValidator(_cfg())
    v.note_frame_width(1280)
    ts = 0.0
    for i in range(3):
        v.validate(_match(x=200), 10 + i, ts)
        ts += 0.016
    assert v._tracking is True
    st = v.validate(_match(x=200 + 5000), 30, ts)  # teleport across the frame
    assert st.valid is False
    assert v._tracking is False


def test_idle_blob_two_frames_never_latches():
    """Anti-false-positive preserved: a blob that only persists 2 frames then vanishes
    must never become valid (it can't reach min_consecutive)."""
    v = _StabilityValidator(_cfg())
    v.note_frame_width(1280)
    ts = 0.0
    assert v.validate(_match(x=300), 5, ts).valid is False
    ts += 0.016
    assert v.validate(_match(x=300), 5, ts).valid is False
    ts += 0.016
    assert v.validate(_match(found=False, x=0, y=0, conf=0.0), 0, ts).valid is False
    assert v._tracking is False


def test_low_confidence_drops_tracking():
    v = _StabilityValidator(_cfg())
    v.note_frame_width(1280)
    ts = 0.0
    for i in range(3):
        v.validate(_match(x=200), 10 + i, ts)
        ts += 0.016
    assert v._tracking is True
    st = v.validate(_match(x=205, conf=0.1), 20, ts)  # below confidence_threshold
    assert st.valid is False
    assert v._tracking is False


def test_meter_far_from_stale_anchor_still_acquires():
    """Regression for the live all-bbox_unstable shots: a shot's meter appears FAR from
    the previous detection's position. Before the re-anchor fix, the stale _last_valid
    made every frame a >gate jump -> perpetual bbox_unstable -> engine starved. Now it
    re-anchors and acquires at the new position."""
    v = _StabilityValidator(_cfg())
    v.note_frame_width(1550)  # acq gate ~172px
    ts = 0.0
    v.validate(_match(x=200), 10, ts)  # establish a stale anchor far away
    ts += 0.016
    valids = []
    x = 1110.0  # meter appears ~910px away and STAYS (stable, rising), like shot 7
    for i in range(8):
        st = v.validate(_match(x=x, y=300 + i, conf=0.9), 30 + i, ts)
        ts += 0.016
        valids.append(st.valid)
    assert any(valids), f"meter far from stale anchor never acquired (the live bug): {valids}"
    assert valids[-1] is True, f"should be solidly tracking by the end: {valids}"


def test_wandering_blob_never_latches():
    """The re-anchor must NOT weaken anti-false-positive: a blob teleporting >gate every
    frame can never get min_consecutive in a row, so it never latches."""
    v = _StabilityValidator(_cfg())
    v.note_frame_width(1550)
    ts = 0.0
    valids = []
    for i in range(12):
        x = 200 + (i % 2) * 1000  # ping-pong >gate every frame
        valids.append(v.validate(_match(x=x), 10, ts).valid)
        ts += 0.016
    assert not any(valids), f"a wandering blob must never latch: {valids}"


def test_acq_jump_gate_scales_with_frame_width():
    """A 120px jump exceeds the gate at 720px (80) but is within it at 1550px (~172),
    so the same proportional jitter isn't over-rejected at full-res."""
    v_small = _StabilityValidator(_cfg())
    v_small.note_frame_width(720)
    ts = 0.0
    v_small.validate(_match(x=200), 10, ts); ts += 0.016
    v_small.validate(_match(x=205), 11, ts); ts += 0.016
    assert v_small.validate(_match(x=325), 12, ts).valid is False  # 120px > 80 gate

    v_big = _StabilityValidator(_cfg())
    v_big.note_frame_width(1550)
    ts = 0.0
    v_big.validate(_match(x=200), 10, ts); ts += 0.016
    v_big.validate(_match(x=205), 11, ts); ts += 0.016
    assert v_big.validate(_match(x=325), 12, ts).valid is True  # 120px < ~172 gate -> 3rd valid
