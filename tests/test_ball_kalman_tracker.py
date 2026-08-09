"""Synthetic-only tests for tools.tracking.ball_kalman_tracker.

The Kalman + IoU association mechanism is exercised on hand-controlled numpy
streams so each behavior (constant-velocity match, short-gap prediction,
long-gap searching/reset, false-positive velocity rejection, empty-stream
None, first-frame init) can be asserted without a video or v9 weights.

No torch, no ultralytics, no opencv. Pure numpy assertions.
"""
from __future__ import annotations

import numpy as np
import pytest

from tools.tracking.ball_kalman_tracker import BallKalmanTracker


# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def _det(cx, cy, w=40.0, h=40.0, conf=0.9):
    """Build a v9-shaped detection with a top-left bbox anchored at (cx, cy)."""
    return {"bbox": [float(cx - w / 2.0), float(cy - h / 2.0),
                     float(w), float(h)], "conf": float(conf), "class": "ball"}


def _center(out):
    x, y, w, h = out["bbox"]
    return x + w / 2.0, y + h / 2.0


# ------------------------------------------------------------------------------
# 1. Constant-velocity ball, no gaps -> tracker matches raw detections within 1px
# ------------------------------------------------------------------------------

def test_constant_velocity_matches_within_1px_after_warmup():
    tracker = BallKalmanTracker()
    n = 60
    vx, vy = 5.0, 2.0
    cx0, cy0 = 200.0, 300.0
    outs = []
    for i in range(n):
        cx = cx0 + vx * i
        cy = cy0 + vy * i
        outs.append(tracker.update([_det(cx, cy)], i))

    # Every frame should be a tracked frame with no FP rejections.
    assert all(o["state"] == "tracked" for o in outs)
    assert tracker.false_positives_rejected == 0
    assert tracker.resets == 0

    # After warm-up (Kalman gain converges within ~15-20 frames on this
    # measurement/process-noise ratio), each output center must be within 1px
    # of the true trajectory center.
    for i in range(20, n):
        true_cx = cx0 + vx * i
        true_cy = cy0 + vy * i
        out_cx, out_cy = _center(outs[i])
        assert abs(out_cx - true_cx) < 1.0, f"frame {i}: cx off by {out_cx - true_cx:.3f}"
        assert abs(out_cy - true_cy) < 1.0, f"frame {i}: cy off by {out_cy - true_cy:.3f}"

    # Estimated velocity must be within 0.5px/frame of the truth after warm-up.
    assert abs(outs[-1]["velocity"][0] - vx) < 0.5
    assert abs(outs[-1]["velocity"][1] - vy) < 0.5


# ------------------------------------------------------------------------------
# 2. 2-frame gap in mid-arc: tracker predicts through and resumes cleanly
# ------------------------------------------------------------------------------

def test_two_frame_gap_predicts_then_resumes():
    tracker = BallKalmanTracker(max_gap_frames=3)
    vx, vy = 4.0, 1.0
    cx0, cy0 = 500.0, 250.0

    # Warm-up: 25 tracked frames.
    for i in range(25):
        tracker.update([_det(cx0 + vx * i, cy0 + vy * i)], i)

    # Frames 25, 26: no detection -> should be "predicted" with rising gap.
    p1 = tracker.update([], 25)
    p2 = tracker.update([], 26)
    assert p1["state"] == "predicted" and p1["gap"] == 1
    assert p2["state"] == "predicted" and p2["gap"] == 2

    # During the gap the predicted center should be near the true trajectory.
    for i, out in ((25, p1), (26, p2)):
        true_cx = cx0 + vx * i
        true_cy = cy0 + vy * i
        c_cx, c_cy = _center(out)
        assert abs(c_cx - true_cx) < 3.0, f"gap frame {i}: cx off by {c_cx - true_cx:.3f}"
        assert abs(c_cy - true_cy) < 3.0

    # Frame 27: detection returns -> tracker resumes cleanly.
    resumed = tracker.update([_det(cx0 + vx * 27, cy0 + vy * 27)], 27)
    assert resumed["state"] == "tracked"
    assert resumed["gap"] == 0
    # No reset (gap never exceeded max_gap_frames=3).
    assert tracker.resets == 0


# ------------------------------------------------------------------------------
# 3. 5-frame gap exceeds max: tracker enters "searching", resets on next hi-conf det
# ------------------------------------------------------------------------------

def test_five_frame_gap_triggers_searching_then_reset():
    tracker = BallKalmanTracker(max_gap_frames=3, reset_conf_threshold=0.6)
    for i in range(15):
        tracker.update([_det(100.0 + i * 3.0, 200.0)], i)

    # Frames 15-17: predicted (gap 1, 2, 3 <= max_gap).
    for i in range(15, 18):
        out = tracker.update([], i)
        assert out["state"] == "predicted", f"frame {i}: {out['state']}"

    # Frames 18-19: gap now 4, 5 -> searching.
    for i in (18, 19):
        out = tracker.update([], i)
        assert out["state"] == "searching", f"frame {i}: {out['state']}"
        assert out["bbox"] is None

    # A low-confidence detection during searching must NOT reset the tracker.
    lc = tracker.update([_det(300.0, 200.0, conf=0.4)], 20)
    assert lc["state"] == "searching"
    assert tracker.resets == 0

    # A high-confidence detection triggers a reset (new trajectory).
    hc = tracker.update([_det(400.0, 250.0, conf=0.8)], 21)
    assert hc["state"] == "reset"
    assert tracker.resets == 1
    # Tracker is on the new trajectory now.
    c_cx, c_cy = _center(hc)
    assert abs(c_cx - 400.0) < 1e-6
    assert abs(c_cy - 250.0) < 1e-6


# ------------------------------------------------------------------------------
# 4. False-positive rejection: spurious high-conf detection 100px off is dropped
# ------------------------------------------------------------------------------

def test_high_velocity_false_positive_is_rejected():
    # min_iou_assoc=0.0 so the spurious detection isn't screened out by IoU
    # first -- we want the velocity gate to fire, not the association gate.
    tracker = BallKalmanTracker(max_gap_frames=5, min_iou_assoc=0.0,
                                max_velocity_pxf=20.0)

    # Warm-up: 15 legit high-conf detections at 2px/frame -> recent_conf > 0.5.
    cx0, cy0 = 100.0, 300.0
    for i in range(15):
        tracker.update([_det(cx0 + i * 2.0, cy0, conf=0.9)], i)
    assert tracker.false_positives_rejected == 0

    # Frame 15: single "detection" 100px off -> implied velocity ~100px/frame
    # >> max_velocity_pxf=20 AND recent_conf=0.9 > 0.5 -> reject.
    fp = tracker.update([_det(cx0 + 15 * 2.0 + 100.0, cy0, conf=0.95)], 15)
    assert tracker.false_positives_rejected == 1
    assert fp["state"] == "predicted"        # coasted through the FP
    assert fp["gap"] == 1
    # Predicted center should still be on the real trajectory, NOT at the FP.
    c_cx, _ = _center(fp)
    assert abs(c_cx - (cx0 + 15 * 2.0)) < 3.0, \
        f"tracker jumped to FP: predicted cx {c_cx:.2f}"

    # Frame 16: legit detection -> back to tracked.
    resumed = tracker.update([_det(cx0 + 16 * 2.0, cy0, conf=0.9)], 16)
    assert resumed["state"] == "tracked"


# ------------------------------------------------------------------------------
# 5. Empty detections stream: returns None throughout (no track ever seeded)
# ------------------------------------------------------------------------------

def test_empty_stream_returns_none_throughout():
    tracker = BallKalmanTracker()
    for i in range(30):
        out = tracker.update([], i)
        assert out is None, f"frame {i}: expected None, got {out}"
    assert tracker.resets == 0
    assert tracker.false_positives_rejected == 0
    assert not tracker.is_tracking


# ------------------------------------------------------------------------------
# 6. First-frame init: single detection initialises the filter correctly
# ------------------------------------------------------------------------------

def test_first_frame_initialises_state_from_single_detection():
    tracker = BallKalmanTracker()
    det = _det(cx=640.0, cy=360.0, w=48.0, h=52.0, conf=0.7)
    out = tracker.update([det], 0)
    assert out is not None
    assert out["state"] == "tracked"
    assert out["gap"] == 0
    assert out["velocity"] == [0.0, 0.0]     # velocity unknown at init
    # Predicted bbox on the seed frame must exactly match the seed's center + size.
    x, y, w, h = out["bbox"]
    assert x == pytest.approx(640.0 - 24.0)
    assert y == pytest.approx(360.0 - 26.0)
    assert w == pytest.approx(48.0)
    assert h == pytest.approx(52.0)
    assert tracker.is_tracking
