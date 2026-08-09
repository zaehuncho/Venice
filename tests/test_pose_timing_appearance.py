"""Appearance-gated player lock (no-meter pose mode).

Validates the jersey-signature mechanism that stops the pose detector's full-frame fallback
from switching onto a different-coloured player (a contesting defender) -- the root cause of
the false-push noise. Pure CV/numpy: no YOLO, no video, so it runs in CI. The full-frame
selection cascade itself is exercised live by tools/diagnostics/eval_skele_pipeline.py.
"""
import numpy as np
import cv2
import pytest

from stamina_bar import _compute_appearance_hist
from pose_timing import PoseTimingDetector, APPEARANCE_MATCH_MIN


def _torso_frame(color_bgr, H=300, W=200):
    """A frame with a solid-coloured torso block where _torso_kpts() says the torso is."""
    f = np.zeros((H, W, 3), np.uint8)
    f[100:200, 75:125] = color_bgr
    return f


def _torso_kpts():
    """COCO keypoints with valid shoulders (5,6) and hips (11,12) framing the torso block."""
    kp = np.zeros((17, 3), np.float32)
    kp[5] = (80, 100, 0.9)    # left shoulder
    kp[6] = (120, 100, 0.9)   # right shoulder
    kp[11] = (85, 200, 0.9)   # left hip
    kp[12] = (115, 200, 0.9)  # right hip
    return kp


def _bare_detector():
    """A PoseTimingDetector with only the appearance fields set -- bypasses __init__ (YOLO)."""
    det = object.__new__(PoseTimingDetector)
    det._appearance_sig = None
    det._appearance_hist_fn = _compute_appearance_hist
    return det


def test_compute_hist_discriminates_jersey():
    """Same jersey -> ~identical histogram; opposite jersey -> correlation below the veto floor.
    Also pins the _compute_appearance_hist conf-index bug fix (kpts[5][2] must be readable)."""
    red = _compute_appearance_hist(_torso_frame((0, 0, 255)), None, _torso_kpts())
    blue = _compute_appearance_hist(_torso_frame((255, 0, 0)), None, _torso_kpts())
    assert red is not None and blue is not None
    same = cv2.compareHist(red, red, cv2.HISTCMP_CORREL)
    diff = cv2.compareHist(red, blue, cv2.HISTCMP_CORREL)
    assert same > 0.99
    assert diff < APPEARANCE_MATCH_MIN


def test_low_conf_shoulders_yields_no_hist():
    """Unmeasurable torso (low-conf shoulders) returns None so callers 'allow' rather than veto."""
    kp = _torso_kpts()
    kp[5][2] = 0.1
    kp[6][2] = 0.1
    assert _compute_appearance_hist(_torso_frame((0, 0, 255)), None, kp) is None


def test_sim_none_until_seeded_then_vetoes_other_jersey():
    det = _bare_detector()
    # No signature yet -> None == 'no opinion, allow' (lock behaviour unchanged).
    assert det._appearance_sim(_torso_frame((0, 0, 255)), _torso_kpts()) is None

    # Seed the signature from a (red-jersey) confident lock.
    det._refresh_signature(_torso_frame((0, 0, 255)), _torso_kpts())
    assert det._appearance_sig is not None
    assert det._appearance_sig.dtype == np.float32

    s_red = det._appearance_sim(_torso_frame((0, 0, 255)), _torso_kpts())
    s_blue = det._appearance_sim(_torso_frame((255, 0, 0)), _torso_kpts())
    assert s_red is not None and s_red > 0.9          # real player kept
    assert s_blue is not None and s_blue < APPEARANCE_MATCH_MIN   # defender vetoed


def test_sim_allows_when_candidate_torso_unmeasurable():
    """With a signature set, a candidate whose torso can't be measured returns None (allow),
    so a momentarily-occluded real shooter is never dropped on appearance grounds."""
    det = _bare_detector()
    det._refresh_signature(_torso_frame((0, 0, 255)), _torso_kpts())
    kp = _torso_kpts()
    kp[5][2] = 0.1
    kp[6][2] = 0.1
    assert det._appearance_sim(_torso_frame((0, 0, 255)), kp) is None


def test_refresh_is_noop_on_unmeasurable_torso():
    det = _bare_detector()
    kp = _torso_kpts()
    kp[5][2] = 0.1
    det._refresh_signature(_torso_frame((0, 0, 255)), kp)
    assert det._appearance_sig is None
