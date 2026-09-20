"""[ORION_BANNER_DISTANCE 2026-09-17] the panel's DISTANCE cell, read as a number.

WHY THIS MATTERS.  The game prints the shot's own distance in the banner (`23'5"`).  That
is a FREE, EXACT range label on every graded shot -- the label shot_range.py's calibration
is specified against and could not get, because nothing recorded it.  The read is
fail-closed by construction: an unreadable cell carries no distance and must never delay or
alter the timing verdict.

Everything here is synthetic.  The value band is rendered FROM the module's own class-mean
digit templates, so segmentation, classification and the feet/inches split are exercised
end to end without a framedump; the corpus accuracy numbers live in
tools/diagnostics/banner_distance_study.py, which reproduces them on demand.
"""
from __future__ import annotations

import numpy as np
import pytest

import banner_distance as bd


# --------------------------------------------------------------------------- rendering
def _glyph(ch, h=36, w=30):
    """The module's own class-mean template for `ch`, back as an 8-bit glyph.

    Rendered LARGER than the real 7-9 px value line on purpose: a 12x10 class mean
    upscaled back to 12x10 is not a faithful glyph (every digit re-reads as a `4`), while
    at 36x30 all ten round-trip.  These tests are therefore about the SEGMENTATION, the
    feet/inches split and the wiring; the real glyph size is exercised against the real
    corpora by tools/diagnostics/banner_distance_study.py.
    """
    import cv2
    T, L = bd._lib()
    v = T[L.index(ch)].reshape(bd.GH, bd.GW)
    v = (v - v.min()) / max(1e-6, (v.max() - v.min()))
    g = cv2.resize(v, (w, h), interpolation=cv2.INTER_NEAREST)
    return np.clip(g * 255.0, 0, 255).astype(np.uint8)


GLYPH_H, GLYPH_W = 36, 30


def render_cell(text, cell_w=360, cell_h=58):
    """A BGR panel strip carrying ONE distance cell that reads `text`.

    Structure copied from the real panel: a dark backing plate, a GREY label line
    ("DISTANCE"), a blank row, then the white value line -- the same structure
    panel_grade.cell_word_mask isolates and the same one read_distance walks.
    """
    strip = np.full((90, 600, 3), 12, np.uint8)
    x0, y0 = 40, 6
    cell = dict(x0=x0, y0=y0, x1=x0 + cell_w, y1=y0 + cell_h, color="white")
    strip[y0:y0 + cell_h, x0:x0 + cell_w] = 18            # the dark backing plate
    strip[y0 + 5:y0 + 11, x0 + 20:x0 + 160] = 120         # grey "DISTANCE" label line
    ink = []
    for ch in text:
        if ch.isdigit():
            ink.append(_glyph(ch))
        elif ch == "'":
            ink.append(np.full((14, 3), 255, np.uint8))
        elif ch == '"':
            # the real inch mark is two strokes; one wide blob would read as a DIGIT
            ink.append(np.full((14, 3), 255, np.uint8))
            ink.append(np.full((14, 3), 255, np.uint8))
    total = sum(g.shape[1] for g in ink) + 3 * (len(ink) - 1)
    x = x0 + (cell_w - total) // 2
    top = y0 + 16                                   # inside the inner box, BELOW the label
    for g in ink:
        h, w = g.shape
        strip[top:top + h, x:x + w] = g[:, :, None]  # a tick's ink stops in the top rows
        x += w + 3
    return strip, cell


# --------------------------------------------------------------------------- the library
def test_the_templates_ship_inside_the_module():
    T, L = bd._lib()
    assert L == [str(d) for d in range(10)]
    assert T.shape == (10, bd.GH * bd.GW)
    # each template is zero-mean unit-norm, which is what makes the dot product equal to
    # TM_CCOEFF_NORMED at one position (the number panel_grade's own matcher reports)
    assert np.allclose(T.mean(axis=1), 0.0, atol=1e-5)
    assert np.allclose(np.linalg.norm(T, axis=1), 1.0, atol=1e-4)


def test_the_cell_box_is_read_in_either_shape():
    assert bd._box(dict(x0=1, y0=2, x1=3, y1=4)) == (1, 2, 3, 4)
    assert bd._box({"box": (5, 6, 7, 8)}) == (5, 6, 7, 8)


# --------------------------------------------------------------------------- the read
# NOTE on the corpus: `5` is deliberately absent. Re-rendering a 12x10 CLASS MEAN as a
# glyph reconstructs nine digits faithfully and turns `5` into something the same mean
# scores as a `6` -- an artefact of the synthetic renderer, not of the read (the real
# corpus reads 172/172 digits, 5s included; see tools/diagnostics/banner_distance_study.py).
@pytest.mark.parametrize("text,feet", [
    ("24'9\"", 24 + 9 / 12.0),
    ("6'4\"", 6 + 4 / 12.0),
    ("18'11\"", 18 + 11 / 12.0),
    ("27'0\"", 27.0),
    ("9'2\"", 9 + 2 / 12.0),
    ("20'7\"", 20 + 7 / 12.0),
])
def test_a_rendered_cell_reads_back_exactly(text, feet):
    strip, cell = render_cell(text)
    got, ft, dbg = bd.read_distance(strip, cell)
    assert got == text, (got, dbg)
    assert ft == pytest.approx(feet, abs=1e-6)


def test_the_label_line_is_not_mistaken_for_the_value():
    """The grey DISTANCE label sits ABOVE the value; the read takes the LAST white band."""
    strip, cell = render_cell("24'9\"")
    got, _ft, _dbg = bd.read_distance(strip, cell)
    assert got == "24'9\""


# --------------------------------------------------------------------------- fail-closed
def test_a_blank_cell_reads_nothing():
    strip = np.full((90, 600, 3), 12, np.uint8)
    cell = dict(x0=40, y0=6, x1=400, y1=64, color="white")
    got, ft, dbg = bd.read_distance(strip, cell)
    assert got == "" and ft is None and dbg.get("reason")


def test_a_cell_with_no_inch_mark_reads_nothing():
    strip, cell = render_cell("235")           # digits, no ' and no "
    got, ft, dbg = bd.read_distance(strip, cell)
    assert got == "" and ft is None


def test_noise_reads_nothing():
    rng = np.random.default_rng(7)
    misses = 0
    for _ in range(40):
        strip = rng.integers(0, 255, (90, 600, 3), dtype=np.uint8)
        got, _ft, _dbg = bd.read_distance(strip, dict(x0=40, y0=6, x1=400, y1=64))
        misses += int(bool(got))
    assert misses == 0


def test_an_absurd_distance_is_refused():
    strip, cell = render_cell("99'99\"")        # inches > 11
    got, _ft, _dbg = bd.read_distance(strip, cell)
    assert got == ""


# --------------------------------------------------------------------------- the vote
def test_the_vote_takes_the_mode_of_the_appearance():
    """The three wrong reads in 1,845 corpus frames were single ghosted frames whose NCC
    and top1-top2 margin sat INSIDE the correct reads' range -- no per-frame gate removes
    them, and the mode over the appearance's samples does."""
    v = bd.DistanceVote()
    for _ in range(21):
        v.add("15'5\"")
    for _ in range(3):
        v.add("18'8\"")
    text, votes, total = v.best()
    assert text == "15'5\"" and votes == 21 and total == 24
    assert v.feet() == pytest.approx(15 + 5 / 12.0)


def test_the_vote_is_empty_until_something_reads():
    v = bd.DistanceVote()
    v.add("")
    assert v.best() == ("", 0, 0) and v.feet() is None


def test_reset_drops_the_previous_appearance():
    v = bd.DistanceVote()
    v.add("23'5\"")
    v.reset()
    assert v.best() == ("", 0, 0)


# --------------------------------------------------------------------------- the fallback
def test_the_thumbnail_fallback_is_a_fixed_shape():
    strip, cell = render_cell("24'9\"")
    th, st = bd.distance_thumb(strip, cell)
    assert th is not None and th.shape == (12, 32) and th.dtype == np.uint8
    assert st["w"] > 0 and st["h"] > 0 and st["ink"] > 0


# --------------------------------------------------------------------------- the cost
def test_one_read_is_affordable_on_the_banner_worker():
    import time
    strip, cell = render_cell("24'9\"")
    pl = bd.planes(strip)
    bd.read_distance(strip, cell, planes_=pl)              # warm
    t0 = time.perf_counter()
    for _ in range(50):
        bd.read_distance(strip, cell, planes_=pl)
    ms = (time.perf_counter() - t0) / 50.0 * 1000.0
    assert ms < 3.0, ms
