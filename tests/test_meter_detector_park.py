"""PARK (rec-mode) temporal meter locator coverage.

The park is full of bright-red distractors — fire/pyro (orange, lower sat), red jerseys
(move), fixed HUD (shot-clock / score / stamina, outside the play band) — that the colour
scan + stability latch false-lock on. ``_ParkTracker`` (gated by
``DetectorConfig.park_temporal_enabled``) confirms the REAL meter by its temporal signature
— a thin PURE-RED column in the play band rising monotonically from a FIXED bottom — and
CONSTRAINS the colour scan to that region (authoritative: no confirmed meter -> no detection,
so distractors never leak in). Fill %, green + timing are still read by the existing pipeline.

These tests pin:
  * a rising pure-red meter LOCKS (and reports a track-relative fill that sweeps up);
  * orange fire (hue), washed red (sat), top HUD red (play band) and a STATIC pure-red blob
    (no rise) are ALL rejected — never returned as the meter;
  * the bbox stays a 4-tuple of ints (the wiring invariant the native grader depends on);
  * with the flag OFF, detection falls back to the normal path (additive).
"""
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from meter_detector import DetectorConfig, MeterDetector

ROOT = Path(__file__).resolve().parents[1]
STYLES = str(ROOT / "meter_styles")

# Fixed meter geometry (in the play band y 0.22..0.68 of a 720p frame).
MX, MW = 640, 18
BOT, FULL_H = 430, 100            # fixed bottom (notch); full meter top = BOT - FULL_H
FIRE_XYWH = (1010, 200, 28, 70)   # orange fire, IN the play band (rejected by HUE)
DESAT_XYWH = (400, 250, 18, 90)   # washed red (rejected by SAT)
HUD_XYWH = (560, 30, 120, 18)     # pure-red HUD bar above the play band (rejected by PLAY BAND)
STATIC_XYWH = (300, 350, 18, 44)  # static PURE-red column in the band (rejected by NO RISE)


def _park_frame(fill_h, *, meter=True, fire=True, desat=True, hud=True, static=True):
    rng = np.random.default_rng(7)
    fr = rng.integers(28, 52, size=(720, 1280, 3), dtype=np.uint8)
    if meter and fill_h > 0:
        top = BOT - fill_h
        cv2.rectangle(fr, (MX - 3, (BOT - FULL_H) - 3), (MX + MW + 3, BOT + 3), (8, 8, 8), 3)
        cv2.rectangle(fr, (MX, top), (MX + MW, BOT), (30, 30, 230), -1)          # pure-red fill
        cv2.rectangle(fr, (MX, BOT - FULL_H), (MX + MW, BOT - FULL_H + 6), (40, 255, 60), -1)  # green tip
    if fire:
        x, y, w, h = FIRE_XYWH
        cv2.rectangle(fr, (x, y), (x + w, y + h), (30, 140, 240), -1)            # orange (hue ~16)
    if desat:
        x, y, w, h = DESAT_XYWH
        cv2.rectangle(fr, (x, y), (x + w, y + h), (90, 90, 190), -1)            # washed red (sat ~134)
    if hud:
        x, y, w, h = HUD_XYWH
        cv2.rectangle(fr, (x, y), (x + w, y + h), (20, 20, 235), -1)            # pure red, ABOVE band
    if static:
        x, y, w, h = STATIC_XYWH
        cv2.rectangle(fr, (x, y), (x + w, y + h), (30, 30, 230), -1)            # pure red, never rises
    return fr


# Rising fill heights -> a monotonic appear->tip rise that the temporal gate confirms.
RISE = [6, 10, 16, 24, 34, 46, 60, 76, 92, 100, 100, 100]


def _park_cfg(enabled=True, color="Red"):
    cfg = DetectorConfig()
    cfg.meter_color = color
    cfg.auto_meter_color = False
    cfg.park_temporal_enabled = enabled
    return cfg


def _near(bbox, xywh, tol=22):
    bx, by, bw, bh = bbox
    bcx, bcy = bx + bw / 2.0, by + bh / 2.0
    x, y, w, h = xywh
    return (x - tol <= bcx <= x + w + tol) and (y - tol <= bcy <= y + h + tol)


def test_park_locks_rising_meter():
    det = MeterDetector(STYLES, _park_cfg())
    det.set_active_style("Arrow2")
    results = [det.detect(_park_frame(h)) for h in RISE]
    hits = [r for r in results if r.detected]
    assert hits, f"rising park meter never locked (reasons={[r.rejection_reason for r in results]})"
    # every hit sits on the meter column, never on a distractor
    for r in hits:
        bcx = r.bbox[0] + r.bbox[2] / 2.0
        assert MX - 30 <= bcx <= MX + MW + 30, f"box off the meter (cx={bcx:.0f})"
        for dist in (FIRE_XYWH, DESAT_XYWH, HUD_XYWH, STATIC_XYWH):
            assert not _near(r.bbox, dist), f"locked a distractor {dist} (bbox={r.bbox})"


def test_park_fill_sweeps_up_and_bbox_is_tuple():
    det = MeterDetector(STYLES, _park_cfg())
    det.set_active_style("Arrow2")
    fills = []
    for h in RISE:
        r = det.detect(_park_frame(h))
        assert isinstance(r.bbox, tuple) and len(r.bbox) == 4
        assert all(isinstance(v, int) for v in r.bbox), f"bbox not int 4-tuple: {r.bbox}"
        if r.detected:
            fills.append(r.fill_pct)
    assert fills, "no detected frames"
    assert max(fills) >= 70.0, f"fill never approached the tip (max={max(fills):.0f})"
    assert max(fills) >= fills[0], "fill did not rise"


def test_park_no_meter_no_false_lock():
    """Only distractors (fire + washed red + HUD + a STATIC pure-red column), no rising meter:
    the authoritative park path must never lock any of them."""
    det = MeterDetector(STYLES, _park_cfg())
    det.set_active_style("Arrow2")
    for _ in range(len(RISE) + 4):
        r = det.detect(_park_frame(0, meter=False))
        if r.detected:
            for dist in (FIRE_XYWH, DESAT_XYWH, HUD_XYWH, STATIC_XYWH):
                assert not _near(r.bbox, dist), f"false-locked distractor {dist} (bbox={r.bbox})"


def test_park_disabled_is_additive():
    """Flag OFF -> the park path is inert; a normal green-capped red meter still detects."""
    det = MeterDetector(STYLES, _park_cfg(enabled=False))
    det.set_active_style("Arrow2")
    results = [det.detect(_park_frame(h)) for h in RISE]
    assert any(r.detected for r in results), "anchor-off (normal) detection regressed"


def test_park_ema_does_not_learn_repeated_shot_position(monkeypatch):
    """2026-07-03 MyCourt regression: a shooter firing from the SAME spot pins the meter to the same
    static-red-EMA cells every shot — the EMA crossed the 'clear' cutoff within ~2 shots, killing
    instant-acquire (and heading for active mask suppression), so shots 1-3 timed perfectly and later
    ones went blind. The guard zeroes the EMA contribution of a LOCKED track's own cells. This test
    plays 10 identical shots from one spot (idle gaps between) and requires the LAST shots to detect
    as well as the first — while the static distractor column must STILL never lock (the EMA guard
    must not weaken true static-red suppression, which only ever locks via the rise gates anyway)."""
    import meter_detector as md
    # Warm re-acquire is time-based (1.2s); synthetic frames process far faster than real shots are
    # spaced, so it would latch shot N+1 from shot N's memory and mask an EMA regression. Disable.
    monkeypatch.setattr(md, "_WARM_REACQ_S", 0.0)
    det = MeterDetector(STYLES, _park_cfg())
    det.set_active_style("Arrow2")

    hits_per_shot = []
    for shot in range(10):
        for _ in range(30):                       # idle gap: no meter, distractors persist
            det.detect(_park_frame(0, meter=False))
        results = [det.detect(_park_frame(h)) for h in RISE]
        hits = [r for r in results if r.detected]
        hits_per_shot.append(len(hits))
        for r in hits:
            assert not _near(r.bbox, STATIC_XYWH), f"shot {shot}: locked the static distractor"

    first = hits_per_shot[0]
    assert first >= 3, f"shot 0 barely detected ({hits_per_shot}) — test premise broken"
    late = hits_per_shot[-3:]
    assert min(late) >= max(3, first // 2), (
        f"late shots degraded vs shot 0 ({hits_per_shot}) — the static-red EMA re-learned the "
        f"shooter's position (self-poisoning regression)")


# --------------------------------------------------------------------------- #
# T5 band-widening guards (2026-07-05): _Y_BOT 0.68 -> 0.85 admits the lower
# play band (low fade meters ride at ~0.75-0.82H). These pin that the newly
# admitted region cannot serve the things that actually live there — static or
# blinking HUD reds and wide nameplate-ish blobs — via ANY tier (park needs a
# monotone rise; the T5 wide-fallback demotes uncorroborated candidates).
# --------------------------------------------------------------------------- #
def test_low_band_static_red_never_serves():
    """A thin static pure-red element at ~0.78H (newly inside the park band) must never
    be detected, served, or remembered."""
    rng = np.random.default_rng(11)
    fr = rng.integers(28, 52, size=(720, 1280, 3), dtype=np.uint8)
    cv2.rectangle(fr, (500, 530), (518, 562), (30, 30, 230), -1)   # bottom 562/720 = 0.78H
    det = MeterDetector(STYLES, _park_cfg())
    det.set_active_style("Arrow2")
    for _ in range(14):
        r = det.detect(fr.copy())
        assert r.detected is False, f"static low-band red served: {r.bbox} {r.rejection_reason!r}"
    assert det._last_result is None


def test_low_band_blinking_red_never_serves():
    """A BLINKING low-band red (HUD flash cadence) must never serve — appearing anew each
    time gives it no rise history, so corroboration can never pass."""
    rng = np.random.default_rng(12)
    base = rng.integers(28, 52, size=(720, 1280, 3), dtype=np.uint8)
    det = MeterDetector(STYLES, _park_cfg())
    det.set_active_style("Arrow2")
    for i in range(16):
        fr = base.copy()
        if i % 2 == 0:
            cv2.rectangle(fr, (700, 540), (718, 575), (25, 25, 235), -1)
        r = det.detect(fr)
        assert r.detected is False, f"blinking low-band red served on frame {i}: {r.bbox}"


def test_low_band_rising_wide_blob_never_serves():
    """Worst case for the widened band: a nameplate-ish WIDE red blob at ~0.75H that grows
    upward (a rising signature). Width/aspect gates must reject it in every tier."""
    rng = np.random.default_rng(13)
    det = MeterDetector(STYLES, _park_cfg())
    det.set_active_style("Arrow2")
    for i in range(12):
        fr = rng.integers(28, 52, size=(720, 1280, 3), dtype=np.uint8)
        top = 540 - 4 * i                                          # grows upward like a rise
        cv2.rectangle(fr, (560, top), (680, 552), (30, 30, 230), -1)  # 120px wide blob
        r = det.detect(fr)
        assert r.detected is False, f"wide rising blob served on frame {i}: {r.bbox}"


def test_low_fade_meter_in_widened_band_serves():
    """The positive case the widening exists for: a REAL meter (thin bar + green tip)
    whose bottom sits at ~0.80H — below the old 0.68 park crop — must acquire."""
    det = MeterDetector(STYLES, _park_cfg())
    det.set_active_style("Arrow2")
    bot = 576                                                       # 576/720 = 0.80H
    served = []
    for i, h in enumerate(RISE[:8]):
        rng = np.random.default_rng(20 + i)
        fr = rng.integers(28, 52, size=(720, 1280, 3), dtype=np.uint8)
        cv2.rectangle(fr, (MX - 3, (bot - FULL_H) - 3), (MX + MW + 3, bot + 3), (8, 8, 8), 3)
        cv2.rectangle(fr, (MX, bot - h), (MX + MW, bot), (30, 30, 230), -1)
        cv2.rectangle(fr, (MX, bot - FULL_H), (MX + MW, bot - FULL_H + 6), (40, 255, 60), -1)
        served.append(det.detect(fr).detected)
    assert any(served), f"low-band meter never detected: {served}"
