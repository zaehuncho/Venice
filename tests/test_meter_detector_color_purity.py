"""Per-colour purity-gate coverage (generalisation of the Purple floatie fix).

The Purple meter's masked-median purity gate (live-proven: rejects the pink
floatie at Hue ~166 / Sat ~142 while keeping the true meter at Hue ~150 /
Sat ~203) is now mirrored for the other selectable meter colours via
``_COLOR_PURITY_GATES``. These tests pin:

  * the gate logic itself — in-band medians accepted, off-hue / washed-out /
    wrong-brightness medians rejected, including the Red hue wrap through 0;
  * end-to-end: a saturated meter-like bar of each colour still produces a
    candidate with the gates ON (the gate must never reject a true meter).

Real-court fixtures only exist for Purple/Arrow2 (see
test_meter_detector_live_gdi.py); other colours are validated synthetically
until live footage exists.
"""
import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from meter_detector import (
    _COLOR_PURITY_GATES,
    _purity_gate_rejects,
)


# ---------------------------------------------------------------------------
# Gate logic
# ---------------------------------------------------------------------------

ACCEPT_CASES = [
    # (color, med_h, med_s, med_v) — typical true-meter medians
    ("Yellow", 30.0, 220.0, 230.0),
    ("Orange", 14.0, 230.0, 235.0),
    ("Red", 4.0, 210.0, 220.0),
    ("Red", 175.0, 210.0, 220.0),   # red wraps: H just below 180 is still red
    ("Green", 58.0, 200.0, 210.0),
    ("Cyan", 98.0, 190.0, 215.0),
    ("White", 90.0, 25.0, 245.0),   # white: hue is meaningless, low sat high val
]

REJECT_CASES = [
    # Off-hue blobs (jersey / court paint sitting inside the BGR prefilter)
    ("Yellow", 55.0, 220.0, 230.0),   # greenish — not the yellow meter
    ("Orange", 32.0, 220.0, 230.0),   # drifted to yellow
    ("Red", 25.0, 210.0, 220.0),      # orange blob is not the red meter
    ("Red", 150.0, 210.0, 220.0),     # magenta is not red (wrap must not over-accept)
    ("Green", 80.0, 200.0, 210.0),    # teal drift
    ("Cyan", 120.0, 190.0, 215.0),    # blue drift
    # Washed-out (low-sat) blobs — cosmetic, not the HUD overlay
    ("Yellow", 30.0, 100.0, 230.0),
    ("Red", 4.0, 90.0, 220.0),
    # White inversions: tinted (sat too high) or grey (val too low)
    ("White", 90.0, 120.0, 245.0),
    ("White", 90.0, 25.0, 150.0),
]


@pytest.mark.parametrize("color,h,s,v", ACCEPT_CASES)
def test_purity_gate_accepts_true_meter_medians(color, h, s, v):
    assert not _purity_gate_rejects(color, h, s, v), (
        f"{color}: true-meter medians H={h} S={s} V={v} must pass the purity gate"
    )


@pytest.mark.parametrize("color,h,s,v", REJECT_CASES)
def test_purity_gate_rejects_offband_medians(color, h, s, v):
    assert _purity_gate_rejects(color, h, s, v), (
        f"{color}: off-band medians H={h} S={s} V={v} must be rejected"
    )


def test_unknown_color_never_rejects():
    # Purple is handled by its dedicated knobs, not this table; an unlisted
    # colour must never be silently filtered.
    assert not _purity_gate_rejects("Purple", 166.0, 142.0, 200.0)
    assert not _purity_gate_rejects("Magenta", 0.0, 0.0, 0.0)


def test_every_gate_color_has_an_hsv_or_white_band():
    for color, gate in _COLOR_PURITY_GATES.items():
        has_hue = "hue_min" in gate and "hue_max" in gate
        has_white = "sat_max" in gate or "val_min" in gate
        assert has_hue or has_white, f"{color}: purity gate has no usable band"


# ---------------------------------------------------------------------------
# End-to-end: the gate must not reject a TRUE saturated meter bar
# ---------------------------------------------------------------------------

# Saturated, hue-correct BGR fills per colour (inside BGR_COLOR_RANGES and the
# colour's purity band).
TRUE_METER_BGR = {
    "Yellow": (25, 215, 225),
    "Red": (30, 30, 230),
    "White": (250, 250, 250),
}


def _synthetic_meter_frame(fill_bgr):
    """A 720p dark-court frame with one meter-like vertical bar: dark outline,
    saturated fill, drawn tall+thin like the live HUD meter."""
    rng = np.random.default_rng(7)
    frame = rng.integers(28, 52, size=(720, 1280, 3), dtype=np.uint8)
    x, y, w, h = 640, 320, 18, 120
    cv2.rectangle(frame, (x - 3, y - 3), (x + w + 3, y + h + 3), (8, 8, 8), thickness=3)
    cv2.rectangle(frame, (x, y), (x + w, y + h), fill_bgr, thickness=-1)
    return frame, (x, y, w, h)


@pytest.mark.parametrize("color", sorted(TRUE_METER_BGR))
def test_true_meter_bar_survives_purity_gate(color):
    from meter_detector import DetectorConfig, MeterDetector
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    cfg = DetectorConfig()
    cfg.meter_style = "Arrow2"
    cfg.meter_color = color
    det = MeterDetector(str(root / "meter_styles"), cfg)
    det.set_active_style("Arrow2")

    frame, (mx, my, mw, mh) = _synthetic_meter_frame(TRUE_METER_BGR[color])
    results = [det.detect(frame) for _ in range(8)]

    # The purity gate must never be the reason a true bar disappears: at least
    # one settled frame must detect it ON the bar.
    hits = [r for r in results if r.detected]
    assert hits, (
        f"{color}: saturated meter bar never detected with purity gates on "
        f"(reasons={[r.rejection_reason for r in results]})"
    )
    for r in hits:
        x, y, w, h = r.bbox
        cx, cy = x + w / 2.0, y + h / 2.0
        assert mx - 30 <= cx <= mx + mw + 30, f"{color}: box off the bar (cx={cx:.0f})"
        assert my - 30 <= cy <= my + mh + 30, f"{color}: box off the bar (cy={cy:.0f})"
