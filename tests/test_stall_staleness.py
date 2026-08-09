"""RC-2b / RC-3 acceptance tests for the 2026-07-06 live-fix build.

Grounded in docs/LIVE_ISSUES_2026-07-06.md. Covers the three staleness fixes that the
offline replay could not previously see (the framedump stores only UNIQUE frames, so a
capture stall was invisible offline -- RC-4, why last round's offline-green fixes died live):

  * RC-2b stall watchdog: a pure capture stall never produces a new unique frame, so
    meter_present:false (asserted only on a processed frame) is defeated by exactly the
    condition it was written for. _apply_stall_override synthesizes the no-meter state from
    the HONEST pixel age -> meter_present flips false within the stall budget.

  * RC-3 post-peak bound: the committed red-presence coast latches a FROZEN frame / static
    décor forever (frozen red satisfies red-presence). The wall-clock + content-hash bound
    decays a peaked track once its ROI stops changing -> HOLD never exceeds meter lifetime.

  * detect() ts param: lets an offline harness inject the ORIGINAL capture timestamp so the
    detector's wall-clock holds behave exactly as they did live.

Session-backed tests SKIP when logs/diagnostics/framedump/session_20260706_162326 is absent.
Run: C:/Python314/python.exe -m pytest tests/test_stall_staleness.py -v
"""
from __future__ import annotations

import glob
import inspect
import os
import re
import sys

import numpy as np
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
_SIDECAR_DIR = os.path.join(_REPO, "native_orion", "backend")
if _SIDECAR_DIR not in sys.path:
    sys.path.insert(0, _SIDECAR_DIR)

_SESSION = os.path.join(_REPO, "logs", "diagnostics", "framedump", "session_20260706_162326")
_STYLES = os.path.join(_REPO, "meter_styles")
_FRAME_RE = re.compile(r"f(\d{5})_[01]_raw\.png$")

# Module-scope stdlib-only imports in the sidecar -> cheap + side-effect free.
import autogreen_sidecar as sidecar  # noqa: E402


def _session_frames():
    if not os.path.isdir(_SESSION):
        return {}
    out = {}
    for p in glob.glob(os.path.join(_SESSION, "f*_raw.png")):
        m = _FRAME_RE.search(os.path.basename(p))
        if m:
            out[int(m.group(1))] = p
    return out


# =========================================================================== #
#  RC-2b -- sidecar stall override (pure, no orchestrator/capture needed)
# =========================================================================== #
def test_stall_override_below_threshold_is_noop():
    p = {"meter_present": True, "raw_fed": True, "shot": {"fill_pct": 71.5, "confidence": 0.91}}
    stalled = sidecar._apply_stall_override(p, pixel_age_ms=50.0, stall_ms=250.0)
    assert stalled is False
    assert p["meter_present"] is True and p["raw_fed"] is True
    assert p["stalled"] is False
    assert p["shot"]["fill_pct"] == 71.5


def test_stall_override_suppresses_meter_when_stale():
    # The 293s-HOLD / RISE-0.0 / idle_overlay echo: a frozen held fill must be dropped.
    p = {"meter_present": True, "raw_fed": True, "shot": {"fill_pct": 71.5, "confidence": 0.91}}
    stalled = sidecar._apply_stall_override(p, pixel_age_ms=300.0, stall_ms=250.0)
    assert stalled is True
    assert p["meter_present"] is False and p["raw_fed"] is False
    assert p["stalled"] is True
    assert p["shot"]["fill_pct"] == 0.0 and p["shot"]["confidence"] == 0.0


def test_stall_override_flips_false_within_300ms_of_stall():
    # Acceptance criterion: meter_present goes false within 300ms of a stall. With the default
    # 250ms watchdog, a pixel age anywhere in [250, 300] ms must already suppress.
    for age in (250.0, 275.0, 300.0):
        p = {"meter_present": True, "raw_fed": True}
        assert sidecar._apply_stall_override(p, age, 250.0) is True
        assert p["meter_present"] is False


def test_stall_override_handles_missing_shot():
    p = {"meter_present": True, "raw_fed": True}   # no 'shot'
    assert sidecar._apply_stall_override(p, 999.0, 250.0) is True
    assert p["meter_present"] is False


# =========================================================================== #
#  RC-4 -- detect() accepts + honors an injected timestamp
# =========================================================================== #
def test_detect_signature_has_ts_param():
    import meter_detector
    sig = inspect.signature(meter_detector.MeterDetector.detect)
    assert "ts" in sig.parameters, "detect() must accept an optional ts for offline stall replay"
    assert sig.parameters["ts"].default is None


def test_detect_runs_with_injected_ts():
    import meter_detector
    det = meter_detector.MeterDetector(_STYLES, meter_detector.DetectorConfig())
    blank = np.zeros((1080, 1920, 3), np.uint8)
    r1 = det.detect(blank, ts=1000.0)
    r2 = det.detect(blank, ts=1000.5)
    assert r1 is not None and r2 is not None
    assert r1.detected is False and r2.detected is False   # blank frame -> no meter


# =========================================================================== #
#  RC-3 -- post-peak wall-clock + content-hash bound (park tracker)
# =========================================================================== #
def _feed_park_to_peak(use_ts):
    """Feed real session frames into a fresh _ParkTracker until a track PEAKS while still
    returning a rect. Returns (park, frozen_frame, t, dt) or (None, ...) if no peak reached."""
    import cv2
    import meter_detector
    frames = _session_frames()
    park = meter_detector._ParkTracker()
    t, dt = 100000.0, 1.0 / 60.0
    for i in sorted(frames)[:120]:
        img = cv2.imread(frames[i])
        if img is None:
            continue
        t += dt
        rect = park.update(img, t if use_ts else None)
        if rect is not None and any(getattr(tk, "peaked", False) for tk in park._tracks):
            return park, img, t, dt
    return None, None, t, dt


def _frozen_hold_frames(use_ts):
    """After peaking, re-feed the FROZEN frame with advancing ts; count frames the park keeps
    returning a rect (== the frozen latch length)."""
    park, frozen, t, dt = _feed_park_to_peak(use_ts)
    if frozen is None:
        return None
    hold = 0
    for _ in range(90):
        t += dt
        if park.update(frozen, t if use_ts else None) is None:
            break
        hold += 1
    return hold


@pytest.mark.skipif(not _session_frames(), reason="framedump session_20260706_162326 absent")
def test_rc3_frozen_peaked_meter_decays_within_300ms_with_ts():
    hold = _frozen_hold_frames(use_ts=True)
    assert hold is not None, "no peaked park track reached in the session window"
    # 300ms budget @60fps == 18 frames. The content-hash + wall-clock bound must decay the frozen
    # latch inside that (HOLD never exceeds the true meter lifetime).
    assert hold <= 18, f"frozen peaked meter latched {hold} frames ({hold/60.0*1000:.0f}ms) > 300ms"


@pytest.mark.skipif(not _session_frames(), reason="framedump session_20260706_162326 absent")
def test_rc3_ts_is_load_bearing_vs_legacy_latch():
    # Without a ts (legacy pure frame-count coast) the same frozen peaked frame latches far longer;
    # proving the wall-clock/content bound (not some unrelated gate) is what decays it.
    with_ts = _frozen_hold_frames(use_ts=True)
    without_ts = _frozen_hold_frames(use_ts=False)
    assert with_ts is not None and without_ts is not None
    assert without_ts >= with_ts + 10, (
        f"ts bound not load-bearing: with_ts={with_ts} without_ts={without_ts}")


# =========================================================================== #
#  Wiring -- prove the fixes are present in the live sources
# =========================================================================== #
def test_orchestrator_frame_count_is_never_reset():
    src = open(os.path.join(_REPO, "remote_play_orchestrator.py"), encoding="utf-8").read()
    # Exactly one assignment to 0 (the __init__ seed); the per-second reset was removed so the
    # native monotonic dedup key can't collide on an fps drop.
    assert src.count("self._frame_count = 0") == 1, \
        "the per-second _frame_count reset must be gone (monotonic dedup key)"


def test_orchestrator_has_stall_watchdog():
    src = open(os.path.join(_REPO, "remote_play_orchestrator.py"), encoding="utf-8").read()
    assert "_stall_watchdog_ms" in src
    assert "stall watchdog" in src.lower()


def test_sidecar_emits_heartbeat_pixel_age_and_stall_override():
    src = open(sidecar.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    assert '"heartbeat"' in src, "sidecar must emit a per-emission heartbeat"
    assert '"pixel_age_ms"' in src, "sidecar must emit the honest pixel age"
    assert "_apply_stall_override(" in src, "sidecar telemetry loop must apply the stall override"


def test_locator_emits_load_verdict():
    src = open(os.path.join(_REPO, "meter_locator_infer.py"), encoding="utf-8").read()
    assert "METER_LOCATOR verdict=LOADED" in src
    assert "METER_LOCATOR verdict=FAILED" in src
    assert "verdict=disabled" in src


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
