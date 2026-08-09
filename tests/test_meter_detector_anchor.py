"""Trained TEMPLATE ANCHOR (meter LOCATION) coverage.

The HSV colour scan alone cannot reliably LOCATE the meter: a saturated red/orange
UI blob passes the one-sided Purple purity gate and the stability latch holds it
(the live 2026-06-22 false-lock — fill frozen 41.18, bbox stuck at (1018,340) 22x42,
med_h=11). A user-trained template of a STABLE, shot-type-invariant HUD glyph is
matched each frame (``cv2.matchTemplate``); the meter sits at a fixed pixel offset
from it, so the colour scan is CONSTRAINED to the true meter region and the blob is
never considered.

These tests pin:
  * ``_TemplateAnchor.locate()`` — recovers the meter offset from a planted glyph,
    returns None below the match floor, scales with the training resolution;
  * the FALSE-LOCK fix at the ``detect()`` level — a decoy at the canonical
    (1018,340) is physically outside the anchored crop and can never be locked;
  * ``_anchor_clear_stale`` — a held echo far from the located region is dropped;
  * the additive guarantee — with no anchor trained, detection is unchanged.
"""
import os
import tempfile
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from meter_detector import DetectorConfig, MeterDetector, _TemplateAnchor, DetectResult

ROOT = Path(__file__).resolve().parents[1]
STYLES = str(ROOT / "meter_styles")

# Canonical live false-lock coordinates (med_h=11 red blob, 22x42).
DECOY_XYWH = (1018, 340, 22, 42)


def _build_frame(glyph_xy=None, meter_xy=(640, 320, 18, 120),
                 meter_bgr=(30, 30, 230), decoy=None, size=(720, 1280)):
    """720p dark-court frame with one meter-like vertical bar (dark outline,
    saturated fill, green cap), an optional high-contrast HUD glyph, and an
    optional red/orange decoy blob."""
    rng = np.random.default_rng(7)
    fr = rng.integers(28, 52, size=(size[0], size[1], 3), dtype=np.uint8)
    if meter_xy is not None:
        mx, my, mw, mh = meter_xy
        cv2.rectangle(fr, (mx - 3, my - 3), (mx + mw + 3, my + mh + 3), (8, 8, 8), 3)
        cv2.rectangle(fr, (mx, my), (mx + mw, my + mh), meter_bgr, -1)
        cv2.rectangle(fr, (mx, my), (mx + mw, my + 6), (40, 255, 60), -1)  # green cap
    if glyph_xy is not None:
        gx, gy = glyph_xy
        cv2.rectangle(fr, (gx, gy), (gx + 34, gy + 30), (245, 245, 245), -1)
        for off in (5, 13, 21):
            cv2.rectangle(fr, (gx + 6, gy + off), (gx + 28, gy + off + 5), (10, 10, 10), -1)
    if decoy is not None:
        dx, dy, dw, dh = decoy
        cv2.rectangle(fr, (dx, dy), (dx + dw, dy + dh), (20, 80, 235), -1)  # saturated red/orange
    return fr


def _save_template(frame, gx, gy, gw=34, gh=30):
    crop = frame[gy:gy + gh, gx:gx + gw].copy()
    td = tempfile.mkdtemp(prefix="orion_anchor_")
    path = os.path.join(td, "glyph.png")
    cv2.imwrite(path, crop)
    return path


def _anchor_cfg(template_path, offset, *, min_score=0.6, band=None, ref_wh=None,
                color="Red"):
    cfg = DetectorConfig()
    cfg.meter_color = color
    cfg.template_anchor_enabled = True
    cfg.template_anchor_path = template_path
    cfg.template_anchor_offset = list(offset)
    cfg.template_anchor_min_score = min_score
    if band is not None:
        cfg.template_anchor_search_band = list(band)
    if ref_wh is not None:
        cfg.template_anchor_ref_wh = list(ref_wh)
    return cfg


# ---------------------------------------------------------------------------
# _TemplateAnchor.locate() unit behaviour
# ---------------------------------------------------------------------------

def test_anchor_disabled_without_config():
    a = _TemplateAnchor(DetectorConfig())
    assert a.enabled is False
    assert a.locate(np.zeros((100, 100, 3), np.uint8)) is None


def test_anchor_locate_recovers_offset():
    gx, gy = 900, 120
    mx, my, mw, mh = 640, 320, 18, 120
    fr = _build_frame(glyph_xy=(gx, gy), meter_xy=(mx, my, mw, mh))
    tp = _save_template(fr, gx, gy)
    a = _TemplateAnchor(_anchor_cfg(tp, [mx - gx, my - gy, mw, mh]))
    assert a.enabled is True
    loc = a.locate(fr)
    assert loc is not None
    lx, ly, lw, lh, score = loc
    assert score >= 0.6
    assert abs(lx - mx) <= 3 and abs(ly - my) <= 3
    assert lw == mw and lh == mh


def test_anchor_returns_none_when_glyph_absent():
    gx, gy = 900, 120
    fr = _build_frame(glyph_xy=(gx, gy))
    tp = _save_template(fr, gx, gy)
    a = _TemplateAnchor(_anchor_cfg(tp, [-260, 200, 18, 120], min_score=0.7))
    # A frame WITHOUT the glyph must fall below the match floor.
    fr_no_glyph = _build_frame(glyph_xy=None)
    assert a.locate(fr_no_glyph) is None
    assert a.last_score < 0.7


def test_anchor_scales_with_ref_wh():
    gx, gy = 900, 120
    mx, my, mw, mh = 640, 320, 18, 120
    train = _build_frame(glyph_xy=(gx, gy), meter_xy=(mx, my, mw, mh))
    tp = _save_template(train, gx, gy)
    a = _TemplateAnchor(_anchor_cfg(tp, [mx - gx, my - gy, mw, mh],
                                    min_score=0.5, ref_wh=[1280, 720]))
    # Live stream at 1.5x the trained resolution.
    live = cv2.resize(train, (1920, 1080), interpolation=cv2.INTER_LINEAR)
    loc = a.locate(live)
    assert loc is not None
    lx, ly, lw, lh, _ = loc
    assert abs(lx - int(mx * 1.5)) <= 12 and abs(ly - int(my * 1.5)) <= 12
    assert abs(lw - int(mw * 1.5)) <= 4 and abs(lh - int(mh * 1.5)) <= 6


# ---------------------------------------------------------------------------
# detect()-level: the false-lock fix
# ---------------------------------------------------------------------------

def test_anchor_constrains_scan_to_meter_not_decoy():
    """Real meter + a decoy at the canonical (1018,340). With the anchor armed at
    the meter, the detector locks the meter and NEVER returns the decoy."""
    gx, gy = 300, 120
    mx, my, mw, mh = 640, 320, 18, 120
    fr = _build_frame(glyph_xy=(gx, gy), meter_xy=(mx, my, mw, mh), decoy=DECOY_XYWH)
    tp = _save_template(fr, gx, gy)
    det = MeterDetector(STYLES, _anchor_cfg(tp, [mx - gx, my - gy, mw, mh]))
    det.set_active_style("Arrow2")

    results = [det.detect(fr) for _ in range(8)]
    hits = [r for r in results if r.detected]
    assert hits, f"meter not located via anchor (reasons={[r.rejection_reason for r in results]})"
    dx, dy = DECOY_XYWH[0], DECOY_XYWH[1]
    for r in hits:
        bx, by, bw, bh = r.bbox
        bcx, bcy = bx + bw / 2.0, by + bh / 2.0
        # On the meter...
        assert mx - 30 <= bcx <= mx + mw + 30, f"box off the meter (cx={bcx:.0f})"
        # ...never on the decoy.
        assert not (dx - 20 <= bcx <= dx + DECOY_XYWH[2] + 20
                    and dy - 20 <= bcy <= dy + DECOY_XYWH[3] + 20), "locked the decoy!"
    assert det.last_debug.get("zone") == "anchor"
    assert det.last_debug.get("anchor_found") == 1


def test_anchor_decoy_outside_region_never_locked():
    """A frame whose ONLY coloured blob is the decoy at (1018,340), with the anchor
    located on an EMPTY region, must yield no detection — the decoy is physically
    outside the scanned crop (the core anti-false-lock guarantee)."""
    gx, gy = 300, 120
    # Offset points the meter region to an empty patch far from the decoy.
    empty_x, empty_y = 520, 300
    fr = _build_frame(glyph_xy=(gx, gy), meter_xy=None, decoy=DECOY_XYWH)
    tp = _save_template(fr, gx, gy)
    det = MeterDetector(STYLES, _anchor_cfg(tp, [empty_x - gx, empty_y - gy, 18, 120]))
    det.set_active_style("Arrow2")

    results = [det.detect(fr) for _ in range(6)]
    dx, dy = DECOY_XYWH[0], DECOY_XYWH[1]
    for r in results:
        if not r.detected:
            continue
        bx, by, bw, bh = r.bbox
        bcx, bcy = bx + bw / 2.0, by + bh / 2.0
        assert not (dx - 20 <= bcx <= dx + DECOY_XYWH[2] + 20
                    and dy - 20 <= bcy <= dy + DECOY_XYWH[3] + 20), "decoy locked despite anchor!"


def test_anchor_clear_stale_drops_far_echo():
    """A held meter-memory echo sitting far from the located region is discarded so
    it can never be re-fed as fill."""
    gx, gy = 300, 120
    empty_x, empty_y = 520, 300
    fr = _build_frame(glyph_xy=(gx, gy), meter_xy=None, decoy=DECOY_XYWH)
    tp = _save_template(fr, gx, gy)
    det = MeterDetector(STYLES, _anchor_cfg(tp, [empty_x - gx, empty_y - gy, 18, 120]))
    det.set_active_style("Arrow2")

    # Seed a stale held result on the decoy (simulating the false-lock latch).
    det._last_result = DetectResult(
        True, "Arrow2", "Red", DECOY_XYWH, 41.18, 0.9, 3)
    det._meter_memory_left = 3

    r = det.detect(fr)
    assert det._last_result is None, "stale far echo not cleared"
    assert det._meter_memory_left == 0
    assert r.rejection_reason != "meter_memory"
    bx, by, bw, bh = r.bbox
    assert not (bx == DECOY_XYWH[0] and by == DECOY_XYWH[1]), "echoed the decoy bbox"


# ---------------------------------------------------------------------------
# Additive guarantee: no anchor trained -> unchanged detection
# ---------------------------------------------------------------------------

def test_no_anchor_detection_unchanged(monkeypatch):
    # This test isolates the TEMPLATE anchor's additivity (det._anchor off -> normal scan). The
    # green-chevron-first anchor is a SEPARATE mechanism (default ON, validated by the full-detector
    # A/B) that legitimately sets zone="anchor"/anchor_found when it locates the meter, so disable it
    # here to assert the template anchor specifically stays inert.
    monkeypatch.setattr("meter_detector._GREEN_FIRST_ANCHOR", False)
    mx, my, mw, mh = 640, 320, 18, 120
    fr = _build_frame(glyph_xy=None, meter_xy=(mx, my, mw, mh))
    cfg = DetectorConfig()
    cfg.meter_color = "Red"
    det = MeterDetector(STYLES, cfg)
    det.set_active_style("Arrow2")
    assert det._anchor.enabled is False
    results = [det.detect(fr) for _ in range(8)]
    hits = [r for r in results if r.detected]
    assert hits, "plain (anchor-off) detection regressed"
    assert det.last_debug.get("anchor_found") == 0
    assert det.last_debug.get("zone") != "anchor"
