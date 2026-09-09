"""ORION_METER_SUBPIXEL_BASE_HOLD: an unmeasurable base no longer drops the ruler to the box.

Same synthetic meter as test_simple_reader_session_ruler.py. The three seed frames catch the box
2 px low (acquisition jitter), so the latched box-to-base offset differs from the box's steady
placement by 2 px. Mid-shot, bright furniture is painted from the bar's base to the bottom of the
image, so the 0.8*B falling crossing that measures the base cannot be found. Without the flag those
frames are measured on the BOX anchor (a different ruler family: a 1.7 pp step and an estimator
generation change native must fence or tolerate); with the flag the base is placed on the frame's
box with the lock's last measured base-to-box offset and the fill continues on the base ruler.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE_ROW = 250
BOX_X, BOX_W, BOX_H = 100, 24, 120
BAR_X0, BAR_W = 106, 12
CAP_ROWS = (BASE_ROW - 99, BASE_ROW - 96)
PP_PER_ROW = 100.0 / (BOX_H - 1)


class _Cfg:
    meter_style = "Arrow2"
    meter_color = "White"
    confidence_threshold = 0.32


def _frame(run_len: int, shelf: bool = False) -> np.ndarray:
    img = np.full((300, 400, 3), 40, np.uint8)
    top = BASE_ROW - run_len + 1
    img[top:BASE_ROW + 1, BAR_X0:BAR_X0 + BAR_W] = 255
    if shelf:
        # bright furniture from the base to the bottom of the image: no falloff to find
        img[BASE_ROW + 1:, BAR_X0 - 2:BAR_X0 + BAR_W + 2] = 235
    r0, r1 = CAP_ROWS
    img[r0:r1 + 1, BAR_X0 + 2:BAR_X0 + BAR_W - 2] = (0, 200, 0)
    return img


def _box(jitter_px: int = 0):
    bottom = BASE_ROW + 12 + jitter_px
    return (BOX_X, bottom - (BOX_H - 1), BOX_W, BOX_H)


PLAN = [(10, 2, False), (14, 2, False), (18, 2, False),     # seed frames, box 2 px low
        (24, 0, False), (32, 0, False),                     # steady box, base measured
        (40, 0, True), (50, 0, True), (60, 0, True), (70, 0, True),   # base unmeasurable
        (80, 0, False)]


def _run(monkeypatch, flag: str):
    monkeypatch.setenv("ORION_METER_SUBPIXEL_FILL", "1")
    monkeypatch.setenv("ORION_METER_SUBPIXEL_BASE_HOLD", flag)
    monkeypatch.setenv("ORION_METER_SUBPIXEL_BRIDGE_MAX_S", "0.02")   # age the velocity hold out
    from simple_meter_reader import SimpleMeterReader
    reader = SimpleMeterReader(cfg=_Cfg())
    reader._det_reset_lock_state()
    ts = 1000.0
    out = []
    for run_len, jitter, shelf in PLAN:
        fill, _green, _top = reader._measure_fill_in_box(_frame(run_len, shelf), _box(jitter), ts=ts)
        q = dict(reader._dbg_subpx or {})
        out.append(dict(run_len=run_len, shelf=shelf, fill=float(fill), anchor=q.get("anchor", ""),
                        hold=q.get("hold", "")))
        ts += 0.10
    return out


def test_shelf_frames_measure_no_base_without_the_flag(monkeypatch):
    out = _run(monkeypatch, "0")
    assert all(o["anchor"] in ("base", "box0") for o in out if not o["shelf"]), out
    assert all(o["anchor"] == "box" for o in out if o["shelf"]), out
    # the box family is a different ruler: a step of the seed-vs-steady box offset (2 rows)
    last_clean = next(o for o in reversed(out[:5]))
    first_shelf = out[5]
    growth = PP_PER_ROW * (first_shelf["run_len"] - last_clean["run_len"])
    assert abs((first_shelf["fill"] - last_clean["fill"]) - growth) > 1.4


def test_base_hold_keeps_the_base_ruler_and_the_fill_continuous(monkeypatch):
    off = _run(monkeypatch, "0")
    on = _run(monkeypatch, "1")
    assert all(o["anchor"] == "base_held" and o["hold"] == "box_rel" for o in on if o["shelf"]), on
    # held frames read ~1.7 pp apart from the box family for the same physical bar height
    for a, b in zip(off, on):
        if a["shelf"]:
            assert abs(a["fill"] - b["fill"]) > 1.4, (a, b)
        else:
            assert a["fill"] == b["fill"]          # clean frames byte-identical with the flag
    # continuity: every step is the bar's own growth, including into and out of the hold
    steps = [b["fill"] - a["fill"] for a, b in zip(on, on[1:])]
    growth = [PP_PER_ROW * (b["run_len"] - a["run_len"]) for a, b in zip(on, on[1:])]
    for s, g, a in zip(steps, growth, on):
        assert abs(s - g) < 0.9, (s, g, a)
