"""[ORION_PROOF_DETECTOR_BOX 2026-09-19] The engine-facing rectangle is TWO rectangles.

THE BUG. The native ownership proof refuses a shape change between consecutive frames
(IoU >= 0.10, <= 1.50x per dimension, <= 1.25x aspect) so a jersey / the scoreboard / a court
line cannot be taken for a meter. It applied that test to ``result.bbox`` -- which, with the
launcher's ``ORION_READER_BOX_TIGHT=2`` (run_orion.local.ps1:575), is not the detector's
rectangle at all but the overlay HUG: ``_tight_display_box`` re-derives it from each frame's
colour pixels and may extend it by up to ``_tight_side_reach`` / ``_tight_top_reach`` /
``_tight_bot_reach`` (18 px) past each edge of the served box.

THE EVIDENCE. ``logs/diagnostics/detframes_20260917_142730.csv`` carries both rectangles for
2 465 live detections: ``det_w`` is 26 px on every one while the published ``w`` runs 26..44,
and the surplus lands at EXACTLY 18 px on one edge with 0 on the other. Live
2026-09-18T23:00:53Z epoch 72: the reader had latched ``box=[831,318,26,110]`` and the engine
logged ``old_box=813,318,44,118 new_box=830,319,26,118 width_scale=0.591`` -- 831-18 = 813 and
26+18 = 44. Three such restarts pushed the first ACCEPTED proof frame from ~17 % fill to 36.7 %
and the shot fired ``live_tip_fired_late`` (LATE, oracle gap 8 px).

THE FIX. The reader stamps the pre-transform rectangle on every published frame; the
orchestrator and sidecar carry it as ``det_bbox``; native judges shape continuity on it. These
tests pin the flap's shape (so the magnitude the engine used to judge is documented in code)
and the transport of the second rectangle end to end. The engine-side behaviour is pinned by
native_orion/tests/AutomationEngineTests.cpp::ownershipProofIgnoresTheDisplayHugFlap and its
two siblings.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SIDECAR_DIR = os.path.join(_REPO_ROOT, "native_orion", "backend")
if _SIDECAR_DIR not in sys.path:
    sys.path.insert(0, _SIDECAR_DIR)

from simple_meter_reader import SimpleMeterReader  # noqa: E402

# The engine's own shape-gate bounds (AutomationEngine.cpp, geometryMatches).
ENGINE_MAX_DIMENSION_SCALE = 1.50
ENGINE_MAX_ASPECT_SCALE = 1.25

# The live rectangle of 2026-09-18T23:00:53Z epoch 72, as the reader latched it.
SERVED = [831, 318, 26, 110]


def _reader(monkeypatch, tight="2"):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    monkeypatch.setenv("ORION_READER_BOX_TIGHT", tight)
    r = SimpleMeterReader(1280, 720)
    r._tight_src = None
    r._tight_off = None
    return r


def test_the_hug_flap_is_a_pure_side_reach_on_a_still_meter(monkeypatch):
    """One frame without a hug and one with it: the DRAWN width swings by exactly the side
    reach with the opposite edge invariant, and that swing is outside the engine's own
    dimension bound -- i.e. the proof was being restarted by the overlay, every time."""
    r = _reader(monkeypatch)
    unhugged = r._tight_display_box(list(SERVED), top_row=-1, h_cap=int(1.6 * SERVED[3]))
    assert tuple(unhugged) == tuple(SERVED)        # no source, no remembered shape

    reach = int(r._tight_side_reach)
    assert reach == 18                              # the shipped ORION_..._SIDE_REACH
    # A remembered tight shape reaching out to the LEFT only (the live signature).
    r._tight_off = (-reach, 0, SERVED[2] + reach, SERVED[3])
    hugged = r._tight_display_box(list(SERVED), top_row=-1, h_cap=int(1.6 * SERVED[3]))
    assert hugged[0] == SERVED[0] - reach                       # 831-18 = 813
    assert hugged[2] == SERVED[2] + reach                       # 26+18 = 44
    assert hugged[0] + hugged[2] == SERVED[0] + SERVED[2]       # right edge invariant
    # This is what the engine was comparing frame to frame.
    width_scale = hugged[2] / float(unhugged[2])
    assert width_scale > ENGINE_MAX_DIMENSION_SCALE
    assert (width_scale / (hugged[3] / float(unhugged[3]))) > ENGINE_MAX_ASPECT_SCALE


def test_reader_stamps_the_pre_transform_rectangle_unconditionally(monkeypatch):
    """``last_debug['det_box']`` is the rectangle the hug started from. It is stamped on EVERY
    published frame (not only when BOX-TIGHT reshapes one) so the value forwarded to native can
    never be one frame stale -- a proof comparing a fresh drawn box against a stale detector box
    would be the same failure with a different cause."""
    src = open(os.path.join(_REPO_ROOT, "simple_meter_reader.py"), encoding="utf-8").read()
    marker = 'self.last_debug["det_box"] = ('
    # one unconditional stamp before the transform, plus the pre-existing one inside it
    assert src.count(marker) >= 1
    head, _, tail = src.partition("        if self._box_tight:\n            # BOX-TIGHT:")
    assert tail, "the BOX-TIGHT block moved; re-point this guard"
    assert marker in head, "the det_box stamp must run before/outside the BOX-TIGHT branch"


def test_snapshot_and_sidecar_carry_the_detector_rectangle():
    """End to end on the real transport helpers: the immutable per-frame snapshot carries
    ``det_bbox`` and the sidecar reader forwards it, on both the modern and the legacy path."""
    import autogreen_sidecar as sidecar
    from remote_play_orchestrator import _ProcessedFrameSnapshot

    snap = _ProcessedFrameSnapshot(bbox=(813, 318, 44, 118),
                                   det_bbox=(831, 318, 26, 110))

    class _Orch:
        _processed_frame_snapshot = snap

    out = sidecar._read_processed_frame_snapshot(_Orch())
    assert out["bbox"] == (813, 318, 44, 118)
    assert out["det_bbox"] == (831, 318, 26, 110)

    # Legacy orchestrator (no atomic snapshot) -- same two rectangles, same keys.
    class _Legacy:
        _processed_frame_snapshot = None
        _last_meter_bbox = (813, 318, 44, 118)
        _last_meter_det_bbox = (831, 318, 26, 110)

    legacy = sidecar._read_processed_frame_snapshot(_Legacy())
    assert legacy["det_bbox"] == (831, 318, 26, 110)

    # Absent -> empty, so native falls back to the drawn box exactly as it shipped.
    class _Old:
        _processed_frame_snapshot = None
        _last_meter_bbox = (813, 318, 44, 118)

    assert sidecar._read_processed_frame_snapshot(_Old())["det_bbox"] == ()


def test_sidecar_telemetry_emits_det_bbox_beside_bbox():
    """Source scan, in the style of test_event_driven_meter_emission: the live telemetry loop
    must actually put the key on the wire, inside the same try/except as ``bbox`` so the two
    rectangles are always the same frame's or neither is sent."""
    src = open(os.path.join(_SIDECAR_DIR, "autogreen_sidecar.py"), encoding="utf-8").read()
    assert 'payload["det_bbox"] =' in src
    assert 'payload.pop("det_bbox", None)' in src
    bbox_at = src.index('payload["bbox"] = [int(bbox[0])')
    det_at = src.index('payload["det_bbox"] =')
    assert 0 < det_at - bbox_at < 1200


def test_native_parses_and_judges_on_the_detector_rectangle():
    """The native contract this Python side exists to feed."""
    session = open(os.path.join(_REPO_ROOT, "native_orion", "src", "RemotePlaySession.cpp"),
                   encoding="utf-8").read()
    assert 'msg.value(QStringLiteral("det_bbox"))' in session
    assert "sidecarResult.detWidth = detWidth;" in session

    engine = open(os.path.join(_REPO_ROOT, "native_orion", "src", "AutomationEngine.cpp"),
                  encoding="utf-8").read()
    assert "ORION_OWNERSHIP_PROOF_DETECTOR_BOX" in engine
    assert "config_.ownershipProofDetectorBox && r.detWidth > 0 && r.detHeight > 0" in engine


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
