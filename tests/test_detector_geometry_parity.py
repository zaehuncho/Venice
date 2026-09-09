"""Verdict logic of the detector geometry parity gate (tools/diagnostics/detector_geometry_parity.py).

Synthetic box lists only -- no ONNX, no model load. The scenarios mirror the real n3 -> n4
regression of 2026-09-08 (boxes 14 px taller, top -7 px, within-band h sd 2-5x) so the gate is
proven to catch exactly that shape of failure while passing a byte-identical candidate.
"""
import importlib.util
import os
import random

import numpy as np
import pytest

_TOOL = os.path.join(os.path.dirname(__file__), "..", "tools", "diagnostics", "detector_geometry_parity.py")
_spec = importlib.util.spec_from_file_location("detector_geometry_parity", _TOOL)
gp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gp)


def _ref_pool(n=300, seed=1):
    """Reference-like boxes: h ~107 +-1, w 23, spread evenly across fill 5-95."""
    rng = random.Random(seed)
    fills = [5.0 + 90.0 * i / (n - 1) for i in range(n)]
    boxes = []
    for f in fills:
        h = 107 + rng.choice((-1, 0, 0, 0, 1))
        y = 300 + rng.randint(-40, 40)
        boxes.append((320 + rng.randint(-60, 60), y, 23, h, 0.9))
    return boxes, fills


def test_identical_candidate_passes():
    ref, fills = _ref_pool()
    res = gp.evaluate_parity(ref, list(ref), fills)
    assert res["verdict"] == "PASS"
    assert res["checks"]["median_h"]["value"] == 0.0
    assert res["checks"]["median_top_y"]["value"] == 0.0
    assert res["checks"]["recall"]["value"] == 0.0
    assert all(b["ok"] for b in res["bands"] if b["judged"])


def test_taller_boxes_fail_height_and_top_checks_even_with_better_recall():
    """The n4 shape: +14 px h, top -7, bottom +7, more detections than the reference."""
    ref, fills = _ref_pool()
    cand = [(x, y - 7, w + 3, h + 14, c) for (x, y, w, h, c) in ref]
    # candidate also detects on frames the reference missed -> recall must NOT rescue it
    ref_holes = list(ref)
    for i in range(0, len(ref_holes), 25):
        ref_holes[i] = None
    res = gp.evaluate_parity(ref_holes, cand, fills)
    assert res["verdict"] == "FAIL"
    assert res["checks"]["median_h"]["ok"] is False
    assert res["checks"]["median_h"]["value"] == pytest.approx(14.0)
    assert res["checks"]["median_top_y"]["ok"] is False
    assert res["checks"]["median_top_y"]["value"] == pytest.approx(-7.0)
    assert res["paired"]["bottom_y_shift_med"] == pytest.approx(7.0)
    assert res["checks"]["recall"]["ok"] is True
    assert res["checks"]["recall"]["value"] > 0
    assert res["cand_recall_on_ref_pct"] == pytest.approx(100.0)


def test_within_band_height_noise_fails_even_when_medians_match():
    """Same median geometry but the height wobbles with fill (the 75-95 band at 5x sd)."""
    ref, fills = _ref_pool()
    rng = np.random.default_rng(3)
    cand = []
    for (x, y, w, h, c), f in zip(ref, fills):
        jitter = int(rng.integers(-8, 9)) if f >= 75 else 0
        # symmetric jitter keeps the median h / top-y unchanged
        cand.append((x, y - jitter // 2, w, h + jitter, c))
    res = gp.evaluate_parity(ref, cand, fills)
    assert res["checks"]["median_h"]["ok"] is True
    assert res["checks"]["median_top_y"]["ok"] is True
    assert res["checks"]["band_h_sd"]["ok"] is False
    failing = set(res["checks"]["band_h_sd"]["failing_bands"])
    assert failing and failing <= {"75-85", "85-95"}
    assert res["verdict"] == "FAIL"


def test_recall_drop_beyond_one_point_fails():
    ref, fills = _ref_pool()
    cand = list(ref)
    for i in range(0, len(cand), 20):      # drop 5 % of detections
        cand[i] = None
    res = gp.evaluate_parity(ref, cand, fills)
    assert res["checks"]["recall"]["ok"] is False
    assert res["checks"]["recall"]["value"] == pytest.approx(-5.0)
    assert res["verdict"] == "FAIL"
    # a sub-1pp drop is tolerated
    cand2 = list(ref)
    cand2[0] = None
    cand2[150] = None
    res2 = gp.evaluate_parity(ref, cand2, fills)
    assert res2["checks"]["recall"]["ok"] is True
    assert res2["verdict"] == "PASS"


def test_one_pixel_median_shift_is_the_boundary():
    ref, fills = _ref_pool()
    cand_ok = [(x, y - 1, w, h + 1, c) for (x, y, w, h, c) in ref]
    assert gp.evaluate_parity(ref, cand_ok, fills)["verdict"] == "PASS"
    cand_bad = [(x, y - 2, w, h + 2, c) for (x, y, w, h, c) in ref]
    res = gp.evaluate_parity(ref, cand_bad, fills)
    assert res["checks"]["median_h"]["ok"] is False
    assert res["checks"]["median_top_y"]["ok"] is False
    assert res["verdict"] == "FAIL"


def test_thin_bands_are_reported_not_judged():
    ref, fills = _ref_pool(n=40)          # ~4 frames per band < min_band_n
    cand = [(x, y, w, h + (5 if f > 50 else 0), c) for (x, y, w, h, c), f in zip(ref, fills)]
    res = gp.evaluate_parity(ref, cand, fills, thresholds=dict(min_band_n=8))
    assert all(b["judged"] is False and b["ok"] is None for b in res["bands"])
    # with no judged band the sd check cannot pass (fail-closed), regardless of medians
    assert res["checks"]["band_h_sd"]["ok"] is False
    assert res["verdict"] == "FAIL"


def test_sd_floor_protects_a_zero_variance_reference():
    ref, fills = _ref_pool()
    ref = [(x, y, w, 107, c) for (x, y, w, h, c) in ref]           # ref sd exactly 0 in every band
    rng = np.random.default_rng(5)
    cand = [(x, y, w, 107 + int(rng.integers(0, 2)), c) for (x, y, w, h, c) in ref]  # sd ~0.5
    res = gp.evaluate_parity(ref, cand, fills, thresholds=dict(sd_floor_px=0.5))
    assert res["checks"]["band_h_sd"]["ok"] is True
    res_strict = gp.evaluate_parity(ref, cand, fills, thresholds=dict(sd_floor_px=0.0))
    assert res_strict["checks"]["band_h_sd"]["ok"] is False


def test_misaligned_inputs_rejected():
    ref, fills = _ref_pool(n=10)
    with pytest.raises(ValueError):
        gp.evaluate_parity(ref, ref[:-1], fills)


def test_format_report_mentions_verdict_and_checks():
    ref, fills = _ref_pool()
    res = gp.evaluate_parity(ref, list(ref), fills)
    txt = gp.format_report(res)
    assert "VERDICT: PASS" in txt
    for key in ("median h delta", "median top-y", "band h sd ratio", "recall delta"):
        assert key in txt


def test_sample_frames_spreads_across_bands(tmp_path):
    sess = tmp_path / "session_x"
    sess.mkdir()
    rows = ["idx,t_ms,t_wall,detected,fill_pct,conf,green_center_pct,bbox_x,bbox_y,bbox_w,bbox_h,rejection,write_wall"]
    # 200 detected frames, 90 % of them at fill 80-95 (the real sessions are tip-heavy), plus misses
    fills = [82.0 + (i % 13) for i in range(180)] + [6.0 + (i % 70) for i in range(20)]
    for i, f in enumerate(fills):
        rows.append(f"{i},0,0,1,{f:.2f},1.0,99,300,300,23,107,,0")
        (sess / f"f{i:05d}_1_raw.png").write_bytes(b"")
    rows.append("999,0,0,0,0.0,0,-1,0,0,0,0,detector_no_meter,0")
    (sess / "frames.csv").write_text("\n".join(rows) + "\n")
    out = gp.sample_frames(str(sess), 60)
    assert len(out) == 60
    lows = [r for r in out if r["fill"] < 75]
    assert len(lows) == 20, "every frame of a thin band must be kept before the fat band is sampled"
    assert all(r["idx"] < 999 for r in out)
