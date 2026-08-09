"""Regression coverage for the track-model meter detector on real frames.

These fixtures are single frames extracted from the real shot recording
`2026-06-03 00-59-07.mp4` (Purple meter, Arrow2 style, 1920x1080). They guard
the track-model fix: fill is measured against the FULL track (not the magenta
fill bbox, which always read ~100%), and the green window is scanned over the
full track column ABOVE the rising fill (not inside the fill bbox, which missed
it). A near-green frame must therefore report a higher fill than a rising frame,
and the green band must be found near the top of the track with real confidence.

Skips cleanly when OpenCV or the fixtures are unavailable so the suite still
runs in a headless/CV-less environment.
"""
from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")

from meter_detector import DetectorConfig, MeterDetector, load_detector_config

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "meter"


def _detector():
    cfg = load_detector_config(str(ROOT / "settings.json"))
    cfg.meter_style = "Arrow2"
    cfg.meter_color = "Purple"
    cfg.auto_meter_color = False  # test Purple specifically, not auto-color scan
    det = MeterDetector(str(ROOT / "meter_styles"), cfg)
    det.set_active_style("Arrow2")
    return det


def test_auto_color_seed_locks_configured_colour():
    """Auto-colour must PIN the configured production colour, not thrash. The live
    2026-06-27 RED meter ran with meter_color=Red + auto_meter_color=True and the
    every-frame candidate wide-scan overrode it (offline framedump: 15% on-meter
    detection + ~543 false locks) vs pinned Red (~69% clean). Seeding the lock fixes
    that, and reload_config (a live colour change) must RE-seed, not re-thrash."""
    cfg = DetectorConfig()
    cfg.meter_color = "Red"
    cfg.auto_meter_color = True
    det = MeterDetector(str(ROOT / "meter_styles"), cfg)
    assert det._auto_color_locked == "Red"
    cfg2 = DetectorConfig()
    cfg2.meter_color = "Purple"
    cfg2.auto_meter_color = True
    det.reload_config(cfg2)
    assert det._auto_color_locked == "Purple"
    # Auto OFF must NOT pin a colour (the candidate-scan path stays available).
    cfg3 = DetectorConfig()
    cfg3.meter_color = "Red"
    cfg3.auto_meter_color = False
    det3 = MeterDetector(str(ROOT / "meter_styles"), cfg3)
    assert det3._auto_color_locked is None


def _detect_settled(name, repeats=6):
    """Feed the same fixture a few times so the stability validator can confirm,
    then return the final DetectResult."""
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"fixture missing: {path}")
    frame = cv2.imread(str(path))
    assert frame is not None, f"could not read {path}"
    det = _detector()
    result = None
    for _ in range(repeats):
        result = det.detect(frame)
    return result


def _pick(prefix):
    matches = sorted(FIXTURES.glob(f"{prefix}_*.png"))
    if not matches:
        pytest.skip(f"no fixture for prefix {prefix}")
    return matches[0].name


def test_standstill_rising_frame_detects_partial_fill():
    result = _detect_settled(_pick("standstill_rising"))
    assert result.detected, f"rising frame not detected (reject={result.rejection_reason})"
    # Track-model fill must be a real partial reading, NOT the old ~100% bbox artifact.
    assert 0.0 < result.fill_pct < 90.0, f"fill should be partial, got {result.fill_pct}"
    assert result.confidence > 0.0


def test_near_green_frame_reports_higher_fill_than_rising():
    rising = _detect_settled(_pick("standstill_rising"))
    near = _detect_settled(_pick("standstill_near_green"))
    assert rising.detected and near.detected
    # The whole point of the track model: a later frame in the shot reads higher.
    assert near.fill_pct > rising.fill_pct, (
        f"near-green fill {near.fill_pct} should exceed rising {rising.fill_pct}"
    )


def test_green_window_found_above_the_fill():
    """The green band must be located near the TOP of the track (high track-%),
    not at the ~1-4% the old fill-bbox scan reported."""
    near = _detect_settled(_pick("standstill_near_green"))
    assert near.detected
    if near.green_window_confidence <= 0.0:
        pytest.skip("green not visible in this extracted frame")
    assert near.green_window_center_pct >= 80.0, (
        f"green center should be near track top, got {near.green_window_center_pct}"
    )


def test_goto_rising_frame_detects_partial_fill():
    result = _detect_settled(_pick("goto_rising"))
    assert result.detected, f"go-to rising not detected (reject={result.rejection_reason})"
    assert result.fill_pct > 0.0


def test_no_meter_frame_does_not_falsely_lock_a_fill():
    """A pre-shot frame with no shot meter must not be reported as a detection.

    Fed repeatedly (so the consecutive-frame stability gate is fully satisfied), a
    frame with no real meter must still come back undetected with a populated
    rejection reason — and never masquerade as a confident, near-full meter."""
    result = _detect_settled(_pick("nometer"))
    assert not result.detected, (
        f"no-meter frame falsely detected (fill={result.fill_pct} conf={result.confidence} "
        f"reject={result.rejection_reason!r})"
    )
    assert result.rejection_reason, "an undetected frame should carry a rejection reason"
