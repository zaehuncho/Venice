"""Green make-window band: triangle-apex extension + fill-ruler unification.

[ORION_GREEN_APEX] The rendered 2K27 cap is a triangle that narrows toward the
meter tip; its top rows carry only 1-4 green px of the ~24-col box and fail the
20%-of-width row gate, which clipped the emitted band top ~1-2pp below the real
paint (pixel census 2026-08-30: gate top med 97.2 vs paint top 98.1, width 1.90
vs 3.77pp on 2514 plateau frames). The band top anchors the tip-relative aim
policy, so it must reflect the paint.

[ORION_GREEN_SCALE_UNIFY] The emitted band must live on the SAME ruler as the
fill emitted the same frame: the sub-pixel fill is base-anchored (box jitter
cancels), so the band must be mapped through the same (anchor, D) transform on
sub-pixel frames and through the box mapping on coarse frames. Before this, a
landing grade compared a sub-pixel peak against a box-ruled band (per-shot
offset IQR ~±2pp = grade noise).
"""
import numpy as np
import pytest

import cv2

from simple_meter_reader import SimpleMeterReader

BH, BW = 107, 24
SS = 8
BASE_ROW = 100.0


def _render_box(fill_edge_row, tri_rows=None, fleck_row=None, base_row=BASE_ROW):
    """White-meter patch with the fill edge at `fill_edge_row` and a green cap.

    tri_rows: {row: green_width_px} for a custom (triangle) cap; None renders the
    default rectangular cap on rows 2-3 (full interior width).
    """
    hr = np.zeros((BH * SS, BW, 3), np.float32)
    fill_c = np.zeros((BW, 3), np.float32)
    empty_c = np.zeros((BW, 3), np.float32)
    fill_c[:] = (250, 252, 253)
    empty_c[:] = (30, 70, 140)
    for c in list(range(0, 4)) + list(range(BW - 4, BW)):
        fill_c[c] = (60, 60, 65)
        empty_c[c] = (40, 40, 45)
    e = int(round(fill_edge_row * SS))
    b = int(round(base_row * SS))
    hr[:e] = empty_c[None, :]
    hr[e:b] = fill_c[None, :]
    sh = min(BH * SS, b + 3 * SS)
    hr[b:sh] = (168, 170, 171)
    hr[sh:] = (100, 105, 110)
    if tri_rows is None:
        tri_rows = {2: BW - 8, 3: BW - 8}
    for row, width in tri_rows.items():
        c0 = (BW - width) // 2
        hr[row * SS:(row + 1) * SS, c0:c0 + width] = (60, 200, 60)
    if fleck_row is not None:
        hr[fleck_row * SS:(fleck_row + 1) * SS, BW // 2 - 1:BW // 2 + 1] = (60, 200, 60)
    lr = hr.reshape(BH, SS, BW, 3).mean(axis=1)
    lr = cv2.GaussianBlur(lr, (0, 0), sigmaX=0.4, sigmaY=0.65)
    return np.clip(lr, 0, 255).astype(np.uint8)


def _frame_with_box(patch, x=600, y=300, pad=40):
    H, W = patch.shape[0] + 2 * pad, patch.shape[1] + 2 * pad
    f = np.full((H + y, W + x, 3), 35, np.uint8)
    f[y:y + patch.shape[0], x:x + patch.shape[1]] = patch
    return f, (x, y, patch.shape[1], patch.shape[0])


# A triangle cap: rows 5-8 pass the 20%-of-24-col row gate (>=5px), rows 3-4 are
# the narrowing apex (2/4 px) that the old gate clipped.
TRIANGLE = {3: 2, 4: 4, 5: 6, 6: 10, 7: 12, 8: 14}


def _measure(r, edge_row, box_dy=0, **kw):
    patch = _render_box(edge_row, **kw)
    frame, box = _frame_with_box(patch)
    box = (box[0], box[1] + box_dy, box[2], box[3] - box_dy)
    return r._measure_fill_in_box(frame, box, ts=None)


def test_triangle_apex_extends_band_top():
    """The band top must follow the >=2px connected paint, not the 20% row gate."""
    r = SimpleMeterReader(1280, 720)
    fill, green, _ = _measure(r, 62.0, tri_rows=TRIANGLE)
    assert green is not None
    denom = float(BH - 1)
    old_gate_end = (denom - 5.0) / denom * 100.0      # row 5 = first >=20% row
    apex_end = (denom - 3.0) / denom * 100.0          # row 3 = last >=2px apex row
    # emitted on the sub-pixel ruler when engaged; allow the ruler offset but the
    # extension itself is ~2 rows = ~1.9pp and must be present
    assert green[1] >= old_gate_end + 1.5, green
    assert green[1] == pytest.approx(apex_end, abs=1.5), green
    # bottom edge unchanged by the extension (row 8)
    assert green[0] == pytest.approx((denom - 8.0) / denom * 100.0, abs=1.5), green


def test_detached_fleck_does_not_extend():
    """A green fleck 2+ rows above the cap must not drag the band top up."""
    r = SimpleMeterReader(1280, 720)
    _, g_clean, _ = _measure(r, 62.0, tri_rows=TRIANGLE)
    r2 = SimpleMeterReader(1280, 720)
    _, g_fleck, _ = _measure(r2, 62.0, tri_rows=TRIANGLE, fleck_row=0)
    assert g_clean is not None and g_fleck is not None
    assert g_fleck[1] == pytest.approx(g_clean[1], abs=0.6), (g_clean, g_fleck)


def test_band_rides_the_emitted_fill_ruler_under_box_jitter():
    """With the sub-pixel latch seeded, +/-1px box jitter moves the box ruler but
    not the base-anchored fill ruler; the band must follow the FILL's ruler."""
    r = SimpleMeterReader(1280, 720)
    for k in range(3):
        _measure(r, 62.0)                     # seed _subpx_D/_subpx_off
    assert r._subpx_D is not None and r._subpx_off is not None
    ends, box_ends = [], []
    denom = float(BH - 1)
    for dy in (0, 1, -1, 1, 0, -1):
        fill, green, _ = _measure(r, 62.0, box_dy=dy)
        assert r._dbg_subpx and r._dbg_subpx["ok"] == 1
        assert green is not None
        ends.append(green[1])
        # the cap's top paint row sits at crop row (2 - dy) when the box slides by dy
        _dn = float(BH - dy - 1)
        box_ends.append((_dn - (2.0 - dy)) / _dn * 100.0)
    assert (max(ends) - min(ends)) < 0.35, ends              # unified: jitter cancels
    assert (max(box_ends) - min(box_ends)) > 0.8             # box ruler DID move


def test_flag_off_keeps_box_ruler(monkeypatch):
    """With sub-pixel emission off the fill is coarse (box ruler) and the band must
    stay on the box ruler too -- same-frame consistency in both modes."""
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL", "0")
    r = SimpleMeterReader(1280, 720)
    fill, green, _ = _measure(r, 62.0, tri_rows=TRIANGLE)
    assert fill == pytest.approx(r._last_fill_coarse, abs=1e-6)
    denom = float(BH - 1)
    assert green is not None
    assert green[1] == pytest.approx((denom - 3.0) / denom * 100.0, abs=0.01), green
    assert green[0] == pytest.approx((denom - 8.0) / denom * 100.0, abs=0.01), green


def test_band_clamped_and_ordered():
    r = SimpleMeterReader(1280, 720)
    fill, green, _ = _measure(r, 62.0, tri_rows=TRIANGLE)
    assert green is not None
    g_start, g_end, g_center, g_w, conf, gpx = green
    assert 0.0 <= g_start <= g_end <= 100.0
    assert g_center == pytest.approx(0.5 * (g_start + g_end), abs=0.01)
    assert g_w == pytest.approx(g_end - g_start, abs=0.01)
    assert gpx >= 8 and 0.0 < conf <= 1.0
