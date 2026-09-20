"""Regressions for the LIVE shot-verdict reader (banner_verdict_live.BannerVerdictLive).

The reader runs tools/timing/panel_grade.py's validated panel grader on the detector's own
frames.  These tests pin the three things that can silently break the owner's live tally:

  * the vectorised matcher must return exactly what panel_grade's OpenCV matcher returns;
  * exactly ONE event per banner appearance (the panel is up for 10-20 sampled frames and
    fades in, so both the repeat-suppression and the "wait for the coverage cell" paths are
    covered), and a new banner after a gap must be a new event;
  * the cost on the detect thread must stay negligible (<= 0.3 ms averaged per frame).

Synthetic panels are built by pasting the SHIPPED templates from panel_templates.npz into a
strip with the real HUD geometry, so a change to panel_grade's thresholds or cell logic that
would break live reading breaks these tests too.
"""

import logging
import os
import time

import cv2
import numpy as np
import pytest

import banner_verdict_live as bvl

pg = bvl.load_panel_grade()
pytestmark = pytest.mark.skipif(
    pg is None or not os.path.isfile(getattr(pg, "LIB_PATH", "")),
    reason="tools/timing/panel_grade.py + panel_templates.npz are untracked dev tooling")

STRIP_W = (pg.X1 - pg.X0) if pg else 440
STRIP_H = (pg.Y1 - pg.Y0) if pg else 40

# Cell colour -> BGR. The exact values only have to land in panel_grade's hue bands.
_COLORS = {"green": (90, 255, 90), "red": (60, 60, 255),
           "yellow": (40, 190, 255), "white": (235, 235, 235)}
# Panel geometry that round-trips every shipped template through the reader (swept: a word
# pasted at 0.90 of the cell's inner width into a 32 px-tall panel re-reads at ncc >= 0.90
# for all 13 distinct words, in both the timing and the coverage slot).
_PANEL_Y0, _PANEL_Y1 = 4, 36
_WORD_FRAC = 0.90


@pytest.fixture(scope="module")
def library():
    masks, labels = pg.load_library()
    assert masks, "empty template library"
    return masks, [str(x) for x in labels]


@pytest.fixture(scope="module")
def matcher(library):
    masks, labels = library
    return bvl._Matcher(pg, masks, labels)


def _template(library, word):
    masks, labels = library
    return masks[labels.index(word)]


def build_strip(library, cells, widths=(135, 150, 135)):
    """A panel strip with one cell per (word, colour) pair; [] -> plain court, no panel."""
    strip = np.full((STRIP_H, STRIP_W, 3), 26, np.uint8)
    if not cells:
        return strip
    y0, y1, dw = _PANEL_Y0, _PANEL_Y1, 3
    xs = [10]
    for w in widths[:len(cells)]:
        xs.append(xs[-1] + w)
    strip[y0:y1 + 1, xs[0]:xs[-1] + dw] = (16, 16, 16)          # the panel's dark plate
    strip[y0:y0 + 2, xs[0]:xs[-1] + dw] = (155, 155, 155)       # grey frame
    strip[y1 - 1:y1 + 1, xs[0]:xs[-1] + dw] = (155, 155, 155)
    for x in xs:                                                 # dividers
        strip[y0:y1 + 1, x:x + dw] = (155, 155, 155)
    for k, (word, color) in enumerate(cells):
        a, b = xs[k] + dw + 3, xs[k + 1] - 3
        w = int((b - a) * _WORD_FRAC)
        a += ((b - a) - w) // 2
        b = a + w
        if word:
            tpl = _template(library, word)
            vh = max(7, min(int(round(28 * (w / 240.0))), (y1 - 3) - (y0 + 5)))
            vy1 = y1 - 3
            small = cv2.resize(tpl, (w, vh), interpolation=cv2.INTER_AREA)
            strip[vy1 - vh:vy1, a:b][small > 128] = _COLORS[color]
            # the white LABEL line (TIMING / COVERAGE) with a gap row under it
            cv2.putText(strip, "X", (a + 2, vy1 - vh - 2), cv2.FONT_HERSHEY_PLAIN, 0.5,
                        (220, 220, 220), 1)
        if color != "white":                                     # the active cell's border
            bx0, bx1 = xs[k] + dw, xs[k + 1]
            strip[y0:y0 + 2, bx0:bx1] = _COLORS[color]
            strip[y1 - 1:y1 + 1, bx0:bx1] = _COLORS[color]
            strip[y0:y1 + 1, bx0:bx0 + 2] = _COLORS[color]
            strip[y0:y1 + 1, bx1 - 2:bx1] = _COLORS[color]
    return strip


def build_frame(library, cells, size=(1280, 720)):
    """A full detector frame carrying that strip at the HUD's fixed position."""
    w, h = size
    frame = np.full((h, w, 3), 70, np.uint8)
    strip = build_strip(library, cells)
    if (w, h) != (1280, 720):
        strip = cv2.resize(strip, (int(round(STRIP_W * w / 1280.0)),
                                   int(round(STRIP_H * h / 720.0))),
                           interpolation=cv2.INTER_NEAREST)
        y0 = int(round(pg.Y0 * h / 720.0))
        x0 = int(round(pg.X0 * w / 1280.0))
        frame[y0:y0 + strip.shape[0], x0:x0 + strip.shape[1]] = strip
    else:
        frame[pg.Y0:pg.Y1, pg.X0:pg.X1] = strip
    return frame


def make_reader(matcher, sink=None, stride=1, debounce_ms=bvl.DEFAULT_DEBOUNCE_MS,
                require_release=False):
    """An inline reader (no worker thread) so tests drive process() deterministically.

    ``require_release=False`` by default so the EDGE-TRIGGER tests below exercise the
    appearance state machine on its own; the attribution gate has its own tests.
    """
    emit = None
    if sink is not None:
        emit = sink.append
    return bvl.BannerVerdictLive(pg, matcher, emit_line=emit, stride=stride,
                                 debounce_ms=debounce_ms, start_worker=False,
                                 require_release=require_release)


def feed(live, strip, n=bvl._SIG_CONFIRM, t=1000.0, step=100.0, seq=0):
    """Push `strip` through n consecutive samples; returns the next timestamp.

    A COMPLETE read is emitted only once it repeats _SIG_CONFIRM times (one sample is a
    misread), so every test that wants a verdict has to show the panel at least that often
    -- which a 10 Hz reader does within ~100 ms of the panel becoming legible.
    """
    for _ in range(n):
        live.process(strip, seq, 0, t)
        t += step
    return t


def _events(sink):
    import json
    return [json.loads(line) for line in sink]


# --------------------------------------------------------------------------- matcher
def test_matcher_agrees_with_panel_grade(library, matcher):
    """The vectorised NCC must be panel_grade's OpenCV NCC, word for word and score for score.

    This is the whole safety argument for not calling cv2.matchTemplate 29 times per read.
    """
    masks, labels = library
    checked = 0
    for word in sorted(set(labels)):
        for color in ("green", "white"):
            strip = build_strip(library, [(word, color), ("OPEN", "white"),
                                          ("OPEN", "white")])
            for cell in pg.read_cells(strip):
                if cell["mask"] is None:
                    continue
                want_word, want_score = pg.match_word(cell["mask"], masks, labels)
                got_word, got_score = matcher.match(cell["mask"])
                assert got_word == want_word, (word, color, cell["role"])
                assert got_score == pytest.approx(want_score, abs=2e-3)
                checked += 1
    assert checked >= 20


def test_every_template_word_round_trips(library, matcher):
    """Each shipped word must survive paste -> read as itself, in both cell roles.

    A COVERAGE word in the first cell is deliberately re-roled by panel_grade (the game
    sometimes shows a lone COVERAGE panel) and must never be scored as a timing verdict.
    """
    masks, labels = library
    for word in sorted(set(labels)):
        slot = "coverage" if word in pg.COVERAGE_WORDS else "timing"
        # The word under test always goes in the WIDE (150 px) cell: a template pasted into
        # a 135 px cell and read back is a 240 -> 128 -> 240 round trip, and the longest
        # words (HEAVY CONTEST) lose enough stroke detail there to fall under NCC 0.90.
        # Real cells on this rig are ~118 px but carry real glyphs, not a resampled mask.
        first = bvl.read_strip(pg, matcher,
                               build_strip(library, [(word, "green"), ("OPEN", "white"),
                                                     ("OPEN", "white")],
                                           widths=(150, 135, 135)))
        assert first is not None, ("first cell", word)
        assert first[slot] == word, ("first cell", word, first)
        if slot == "coverage":
            assert first["timing"] == "", ("coverage word scored as timing", word)
        second = bvl.read_strip(pg, matcher,
                                build_strip(library, [("EXCELLENT", "green"), (word, "white"),
                                                      ("OPEN", "white")]))
        assert second is not None, ("second cell", word)
        assert second["timing"] == "EXCELLENT", ("second cell", word, second)
        assert second["coverage"] == word, ("second cell", word, second)


# --------------------------------------------------------------------------- event shape
def test_event_shape_and_vocabulary(library, matcher):
    sink = []
    live = make_reader(matcher, sink, require_release=True)
    strip = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    live.note_release(77, 1757800000000.0 - 1000.0)
    feed(live, strip, t=1757800000000.0, seq=4242)
    (ev,) = _events(sink)
    assert ev["event"] == "banner_verdict"
    assert ev["timing"] == "EXCELLENT"
    assert ev["coverage"] == "WIDE OPEN"
    assert ev["timing_color"] == "green"
    assert ev["green"] is True
    assert ev["ncc"] >= pg.NCC_MATCH and ev["cov_ncc"] >= pg.NCC_MATCH
    assert ev["frame_seq"] == 4242
    assert ev["frame_epoch_ms"] == pytest.approx(1757800000000.0, abs=101.0)
    assert ev["onset_ms"] == pytest.approx(1757800000000.0, abs=1.0)
    assert ev["emit_latency_ms"] == pytest.approx(100.0, abs=1.0)
    assert ev["seq"] == 1
    assert ev["cells"] == "green|white|white"
    assert ev["attributed"] == 1
    assert ev["release_seq"] == 77
    assert ev["release_delay_ms"] == pytest.approx(1000.0, abs=1.0)
    # the vocabulary is the library's, never invented
    _, labels = pg.load_library()
    assert ev["timing"] in {str(x) for x in labels}
    assert ev["coverage"] in {str(x) for x in labels}
    # [ORION_BANNER_COVERAGE_ABSENT 2026-09-19] `has_coverage` is the panel's LAYOUT: True
    # here because this IS a 3-cell panel. See the coverage-absent tests below.
    assert ev["has_coverage"] is True
    assert set(ev) == {"event", "timing", "timing_color", "green", "coverage",
                       "has_coverage", "distance_color", "distance", "distance_ft",
                       "ncc", "cov_ncc", "cells", "frame_ts_ns",
                       "frame_epoch_ms", "frame_seq", "seq", "attributed", "release_seq",
                       "release_delay_ms", "onset_ms", "emit_latency_ms"}
    # [ORION_BANNER_DISTANCE 2026-09-17] the synthetic strip carries word templates, not a
    # printed distance, so the read fails CLOSED and the verdict still goes out.
    assert ev["distance"] == "" and ev["distance_ft"] == -1.0


def test_non_green_verdict_is_not_flagged_green(library, matcher):
    sink = []
    live = make_reader(matcher, sink)
    feed(live, build_strip(library, [("LATE", "red"), ("LIGHT CONTEST", "white"),
                                     ("OPEN", "white")]))
    (ev,) = _events(sink)
    assert (ev["timing"], ev["timing_color"], ev["green"]) == ("LATE", "red", False)


def test_unreadable_panel_emits_nothing(library, matcher):
    """A panel whose word is not in the library is UNKNOWN, and UNKNOWN is never reported."""
    sink = []
    live = make_reader(matcher, sink)
    strip = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    # scribble over the timing word so it cannot match any template
    rng = np.random.default_rng(3)
    box = strip[_PANEL_Y1 - 16:_PANEL_Y1 - 3, 20:130]
    box[...] = np.where(rng.random(box.shape[:2] + (1,)) > 0.5, 90, 20).astype(np.uint8) \
        * np.array([0, 1, 0], np.uint8)
    live.process(strip, 1, 0, 1000.0)
    assert sink == []


# --------------------------------------------------------------------------- debounce
def test_one_event_per_banner_appearance(library, matcher):
    """A banner up for ~1.5 s (15 samples at 10 Hz) is ONE verdict, not fifteen."""
    sink = []
    live = make_reader(matcher, sink)
    panel = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    for i in range(15):
        live.process(panel, i, 0, 1000.0 + 100.0 * i)
    assert len(_events(sink)) == 1
    assert live.panel_samples == 15


def test_fade_in_does_not_double_count(library, matcher):
    """The panel draws the TIMING cell before COVERAGE; that must still be ONE event."""
    sink = []
    live = make_reader(matcher, sink)
    partial = build_strip(library, [("EXCELLENT", "green"), ("", "white"), ("", "white")])
    full = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                 ("OPEN", "white")])
    live.process(partial, 0, 0, 1000.0)
    live.process(full, 1, 0, 1100.0)
    live.process(full, 2, 0, 1200.0)
    evs = _events(sink)
    assert len(evs) == 1
    assert (evs[0]["timing"], evs[0]["coverage"]) == ("EXCELLENT", "WIDE OPEN")


def test_next_shot_after_the_banner_drops_is_a_new_event(library, matcher):
    sink = []
    live = make_reader(matcher, sink)
    court = build_strip(library, [])
    first = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    second = build_strip(library, [("LATE", "red"), ("LIGHT CONTEST", "white"),
                                   ("OPEN", "white")])
    t = 1000.0
    for strip, n in ((first, 10), (court, 40), (second, 10)):
        for _ in range(n):
            live.process(strip, 0, 0, t)
            t += 100.0
    evs = _events(sink)
    assert [(e["timing"], e["seq"]) for e in evs] == [("EXCELLENT", 1), ("LATE", 2)]


def test_identical_verdict_after_a_gap_is_a_new_event(library, matcher):
    """Two EXCELLENTs in a row are two shots — as long as the banner really went away."""
    sink = []
    live = make_reader(matcher, sink)
    court = build_strip(library, [])
    panel = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    t = 1000.0
    for strip, n in ((panel, 10), (court, 60), (panel, 10)):
        for _ in range(n):
            live.process(strip, 0, 0, t)
            t += 100.0
    assert [e["seq"] for e in _events(sink)] == [1, 2]


def test_flicker_inside_the_debounce_window_is_one_event(library, matcher):
    """A single dropped/misread frame mid-banner must not re-arm and double-count."""
    sink = []
    live = make_reader(matcher, sink)
    court = build_strip(library, [])
    panel = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    t = 1000.0
    for strip in [panel] * 5 + [court] + [panel] * 5:
        live.process(strip, 0, 0, t)
        t += 100.0
    assert len(_events(sink)) == 1


def test_same_words_reappearing_inside_debounce_ms_is_suppressed(library, matcher):
    """The game's white inter-panel wipe can hide a 'gone' banner; 1.5 s covers it."""
    sink = []
    live = make_reader(matcher, sink, debounce_ms=1500.0)
    court = build_strip(library, [])
    panel = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    t = 1000.0
    for strip in [panel] * 3 + [court] * 3 + [panel] * 3:   # 300 ms away: same banner
        live.process(strip, 0, 0, t)
        t += 100.0
    assert len(_events(sink)) == 1


def test_two_different_panels_back_to_back_are_two_events(library, matcher):
    """The game can swap one panel for the next with NO blank frame between them.

    A change of words, held long enough not to be a misread cell, is a new appearance.
    """
    sink = []
    live = make_reader(matcher, sink)
    first = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    second = build_strip(library, [("LATE", "red"), ("LIGHT CONTEST", "white"),
                                   ("OPEN", "white")])
    t = 1000.0
    for strip in [first] * 5 + [second] * 5:          # not one absent sample between them
        live.process(strip, 0, 0, t)
        t += 100.0
    assert [(e["timing"], e["coverage"], e["seq"]) for e in _events(sink)] == [
        ("EXCELLENT", "WIDE OPEN", 1), ("LATE", "LIGHT CONTEST", 2)]


def test_a_single_misread_cell_is_not_a_second_panel(library, matcher):
    """One frame reading a different coverage word must not re-arm: _SIG_CONFIRM samples.

    The panel is up the whole time, so there is exactly one appearance and one verdict.
    """
    sink = []
    live = make_reader(matcher, sink)
    good = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                 ("OPEN", "white")])
    blip = build_strip(library, [("EXCELLENT", "green"), ("OPEN", "white"),
                                 ("OPEN", "white")])
    t = 1000.0
    for strip in [good] * 3 + [blip] + [good] * 3:
        live.process(strip, 0, 0, t)
        t += 100.0
    assert len(_events(sink)) == 1


def test_a_coverage_cell_that_stops_resolving_is_not_a_new_panel(library, matcher):
    """Losing the coverage word is a LOSS of information, never a panel change.

    Without this the panel would re-arm on its own fade-out and flush a second, WORSE
    verdict (timing only) for the very same shot.
    """
    sink = []
    live = make_reader(matcher, sink)
    full = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                 ("OPEN", "white")])
    partial = build_strip(library, [("EXCELLENT", "green"), ("", "white"), ("", "white")])
    t = 1000.0
    for strip in [full] * 3 + [partial] * 6:
        live.process(strip, 0, 0, t)
        t += 100.0
    evs = _events(sink)
    assert len(evs) == 1
    assert evs[0]["coverage"] == "WIDE OPEN"


def _two_cell(library, word, color):
    """The `TIMING | DISTANCE` layout: 2 cells, the second white.

    panel_grade.cell_roles names a white second cell `distance`, so this panel has NO
    coverage cell and never will. All 30 panels of the 2026-09-15 18:53 drill were this
    layout -- the in-game feedback banner IS 2-cell; the 3-cell one with a COVERAGE word
    belongs to the replay/feedback screen.
    """
    return build_strip(library, [(word, color), ("OPEN", "white")], widths=(150, 150))


def test_two_cell_timing_distance_panel_fires_without_waiting_for_coverage(library, matcher):
    """The regression the 18:53 drill exposed: 16 of 30 panels reported.

    Waiting for a coverage word in a layout that has no coverage cell meant the verdict
    could only ever come out on the panel's DOWN edge -- 2.5-3 s after onset, by which time
    the next panel had merged into it. A 2-cell read is COMPLETE the moment its timing word
    resolves, so the verdict lands ~1 sample later.
    """
    sink = []
    live = make_reader(matcher, sink, require_release=True)
    panel = _two_cell(library, "LATE", "red")
    court = build_strip(library, [])
    t0 = 1_789_522_696_635.0
    live.note_release(9, t0 - 1200.0)
    t = feed(live, panel, n=25, t=t0)              # a 2.5 s panel, as in the drill
    (ev,) = _events(sink)
    assert ev["timing"] == "LATE"
    assert ev["coverage"] == ""                    # there is no coverage cell to read
    assert ev["cells"] == "red|white"
    assert ev["onset_ms"] == pytest.approx(t0, abs=1.0)
    assert ev["emit_latency_ms"] <= 300.0, "emitted on the DOWN edge, not the onset"
    assert ev["release_delay_ms"] == pytest.approx(1200.0, abs=1.0)
    # ...and it is still exactly ONE verdict for the whole 2.5 s panel
    feed(live, court, n=4, t=t)
    assert live.verdicts == 1


# ------------------------------------------- coverage ABSENT vs coverage UNREADABLE (09-19)
def test_a_two_cell_panel_reports_coverage_absent_on_the_wire_and_in_the_log(
        library, matcher, caplog):
    """[ORION_BANNER_COVERAGE_ABSENT 2026-09-19] `coverage=""` is TWO different facts.

    The reader has always known the difference -- `_complete()` is built on it -- but the
    payload carried only the WORD, so the native saw the same empty string for "this panel
    has no coverage cell" (the 2-cell drill panel) and "it has one and it was unreadable".
    The engine's trim only calibrates on OPEN / WIDE OPEN, so every drill panel was excluded
    from the loop: 98 of the 281 graded releases across the 2026-09-18 sessions.
    """
    sink = []
    live = make_reader(matcher, sink, require_release=True)
    panel = _two_cell(library, "LATE", "red")
    t0 = 1_789_522_696_635.0
    live.note_release(11, t0 - 1200.0)
    with caplog.at_level(logging.INFO):
        feed(live, panel, n=6, t=t0)
    (ev,) = _events(sink)
    assert ev["timing"] == "LATE"
    assert ev["coverage"] == ""
    assert ev["has_coverage"] is False
    (line,) = [r.getMessage() for r in caplog.records
               if r.getMessage().startswith("BANNER VERDICT:")]
    assert "coverage=- has_cov=0" in line, line


def test_a_three_cell_panel_reports_coverage_present(library, matcher, caplog):
    """The other half of the same fact: a panel that really HAS a coverage cell says so,
    so an unreadable cell on a 3-cell panel can never be read as "no defender context"."""
    sink = []
    live = make_reader(matcher, sink, require_release=True)
    panel = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    t0 = 1_789_522_697_000.0
    live.note_release(12, t0 - 1200.0)
    with caplog.at_level(logging.INFO):
        feed(live, panel, n=6, t=t0)
    (ev,) = _events(sink)
    assert ev["coverage"] == "WIDE OPEN"
    assert ev["has_coverage"] is True
    (line,) = [r.getMessage() for r in caplog.records
               if r.getMessage().startswith("BANNER VERDICT:")]
    assert "coverage=WIDE OPEN has_cov=1" in line, line


def test_the_unattributed_line_also_carries_has_cov(library, matcher, caplog):
    """The UNATTRIBUTED line is the only record of a panel that never reaches the native,
    so it must be readable for the same distinction."""
    sink = []
    live = make_reader(matcher, sink, require_release=True)
    panel = _two_cell(library, "EXCELLENT", "green")
    with caplog.at_level(logging.INFO):
        feed(live, panel, n=6, t=1_789_522_698_000.0)
    assert sink == []
    (line,) = [r.getMessage() for r in caplog.records if "UNATTRIBUTED" in r.getMessage()]
    assert "coverage=- has_cov=0" in line, line


def test_back_to_back_panels_split_on_a_single_blank_sample(library, matcher):
    """In rapid fire the drill's panels are ~437 ms apart and ONE boundary is a single
    no-panel sample -- panel_grade.events_from splits there and so must the reader.

    Two consecutive EXCELLENT panels separated by exactly one blank sample were merged by
    the first cut of this module, and the second shot's verdict was lost.
    """
    sink = []
    live = make_reader(matcher, sink, require_release=True)
    panel = _two_cell(library, "EXCELLENT", "green")
    court = build_strip(library, [])
    t = 1_789_522_690_000.0
    live.note_release(1, t - 1200.0)
    t = feed(live, panel, n=14, t=t)
    live.note_release(2, t - 1200.0)
    t = feed(live, court, n=1, t=t)                # ONE blank sample: a real boundary
    feed(live, panel, n=14, t=t)
    evs = _events(sink)
    assert [(e["timing"], e["release_seq"]) for e in evs] == [
        ("EXCELLENT", 1), ("EXCELLENT", 2)]
    assert evs[1]["onset_ms"] == pytest.approx(t, abs=1.0)


def test_the_white_inter_panel_flash_splits_two_same_verdict_panels(library, matcher):
    """The game separates panels with a white flash rather than blank frames; the dark
    plate never lifts, so an absence test alone merges them. The reader cuts the event
    where panel_grade does: on a change of colour CLASS or cell layout."""
    sink = []
    live = make_reader(matcher, sink, require_release=True)
    panel = _two_cell(library, "EXCELLENT", "green")
    flash = build_strip(library, [("", "white"), ("", "white")], widths=(150, 150))
    t = 1_789_522_690_000.0
    live.note_release(1, t - 1200.0)
    t = feed(live, panel, n=12, t=t)
    live.note_release(2, t - 1200.0)
    t = feed(live, flash, n=2, t=t)                # no blank frame anywhere in here
    feed(live, panel, n=12, t=t)
    assert [(e["timing"], e["release_seq"]) for e in _events(sink)] == [
        ("EXCELLENT", 1), ("EXCELLENT", 2)]


def test_onset_is_the_first_sample_not_the_emit(library, matcher):
    """Attribution tests the panel's ONSET. On the drill, release -> emit reaches 3.3 s,
    past the 2.6 s window: testing the emit throws the verdict away."""
    sink = []
    live = make_reader(matcher, sink, require_release=True)
    panel = build_strip(library, [("EXCELLENT", "green"), ("", "white"), ("", "white")])
    full = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                 ("OPEN", "white")])
    t0 = 1_789_522_700_000.0
    live.note_release(5, t0 - 2400.0)              # onset at +2.4 s: inside the window
    t = feed(live, panel, n=8, t=t0)               # 800 ms with the coverage cell unreadable
    feed(live, full, t=t)
    (ev,) = _events(sink)
    assert ev["onset_ms"] == pytest.approx(t0, abs=1.0)
    assert ev["emit_latency_ms"] >= 800.0
    assert ev["attributed"] == 1 and ev["release_seq"] == 5
    assert ev["release_delay_ms"] == pytest.approx(2400.0, abs=1.0)
    # the emit is 3.2 s after the release -- outside the window the ONSET passed
    assert ev["release_delay_ms"] + ev["emit_latency_ms"] > 2600.0


# --------------------------------------------------------------------------- attribution
def test_static_panel_reread_is_one_unattributed_verdict_and_is_not_forwarded(
        library, matcher, caplog):
    """The 2026-09-15 live regression, as a gate.

    The game sat on a shot-feedback/replay screen with ZERO bot presses and the reader put
    16 ``EXCELLENT / WIDE OPEN`` verdicts on the wire in 39 s, each one an activity line and
    a ShotVerdictTally increment. Two things must now be true: the same panel is ONE
    appearance however often it is re-read, and with no release behind it that appearance
    never reaches the native -- it lands in the log at ERROR instead.
    """
    sink = []
    live = make_reader(matcher, sink, require_release=True)
    panel = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    with caplog.at_level(logging.INFO):
        t = 1000.0
        for _ in range(10):
            live.process(panel, 0, 0, t)
            t += 100.0
    assert live.verdicts == 1
    assert live.verdicts_unattributed == 1
    assert live.verdicts_forwarded == 0
    assert sink == [], "an unattributed verdict reached the native"
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("BANNER VERDICT UNATTRIBUTED:" in m for m in msgs) == 1, msgs
    assert not any(m.startswith("BANNER VERDICT:") for m in msgs)
    # ERROR, not WARNING: the native relay throttles sidecar WARNINGs to 1/s and a storm
    # of these is exactly what has to stay visible.
    (rec,) = [r for r in caplog.records if "UNATTRIBUTED" in r.getMessage()]
    assert rec.levelno == logging.ERROR
    assert "attributed=0" in rec.getMessage()


def test_static_panel_with_dropouts_is_still_one_verdict(library, matcher):
    """The actual mechanism of the storm: the panel read as ABSENT for 2-4 samples every
    ~2 s, which re-armed the state machine, and only the 1.5 s time debounce stood between
    that and an event -- so the repeats arrived at exactly the debounce period.

    20 s of a static panel with a 3-sample dropout every 2 s = 10 re-arms. One verdict.
    """
    sink = []
    live = make_reader(matcher, sink, require_release=True)
    court = build_strip(library, [])
    panel = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    t = 1000.0
    for _ in range(10):
        for strip in [panel] * 17 + [court] * 3:
            live.process(strip, 0, 0, t)
            t += 100.0
    assert live.verdicts == 1, "the static panel was reported more than once"
    assert sink == [], "an unattributed re-read reached the native"
    assert live.repeats_suppressed > 0


def test_a_fresh_panel_after_a_release_is_attributed(library, matcher):
    sink = []
    live = make_reader(matcher, sink, require_release=True)
    court = build_strip(library, [])
    panel = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    t = 1_789_511_846_400.0                       # a real release wall clock from the session
    live.note_release(9, t)
    for _ in range(6):                            # 600 ms of court after the release
        live.process(court, 0, 0, t)
        t += 100.0
    t += 400.0                                    # banner appears 1.0 s after the release
    feed(live, panel, t=t, seq=1234)
    (ev,) = _events(sink)
    assert ev["attributed"] == 1
    assert ev["release_seq"] == 9
    assert ev["release_delay_ms"] == pytest.approx(1000.0, abs=1.0)
    assert live.verdicts_forwarded == 1 and live.verdicts_unattributed == 0


def test_one_release_can_only_mint_one_verdict(library, matcher):
    """A second panel behind the SAME release is never a second tally entry."""
    sink = []
    live = make_reader(matcher, sink, require_release=True)
    court = build_strip(library, [])
    first = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    second = build_strip(library, [("LATE", "red"), ("LIGHT CONTEST", "white"),
                                   ("OPEN", "white")])
    t = 1_789_511_846_400.0
    live.note_release(9, t)
    t += 900.0
    t = feed(live, first, t=t, seq=1)              # +0.9 s: attributed to release 9
    for strip in [court] * 6 + [second] * 2:      # a second panel, still inside the window
        live.process(strip, 2, 0, t)
        t += 100.0
    evs = _events(sink)
    assert [e["release_seq"] for e in evs] == [9]
    assert live.verdicts_unattributed == 1


def test_a_stale_panel_is_never_stamped_with_the_next_shot(library, matcher):
    """A banner is up ~2 s and shots land ~2.5 s apart, so the PREVIOUS shot's panel is
    often still on screen when the next release fires.

    A reader dropout across that boundary must not re-read the old panel and hand it the
    new shot's release: that is a fabricated verdict AND it burns the release the real
    panel needs.
    """
    sink = []
    live = make_reader(matcher, sink, require_release=True)
    court = build_strip(library, [])
    stale = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    fresh = build_strip(library, [("LATE", "red"), ("LIGHT CONTEST", "white"),
                                  ("OPEN", "white")])
    t = 1_789_511_800_000.0
    live.note_release(1, t)
    t += 1000.0
    t = feed(live, stale, n=6, t=t, seq=1)        # shot 1's panel, up and reported
    live.note_release(2, t)                       # shot 2 goes up while it is still there
    for strip in [court] * 2 + [stale] * 8:       # a dropout, then the SAME panel again
        live.process(strip, 2, 0, t)
        t += 100.0
    assert [(e["timing"], e["release_seq"]) for e in _events(sink)] == [("EXCELLENT", 1)]
    t = feed(live, court, n=6, t=t, seq=3)        # shot 2's own panel finally arrives
    feed(live, fresh, t=t, seq=3)
    assert [(e["timing"], e["release_seq"]) for e in _events(sink)] == [
        ("EXCELLENT", 1), ("LATE", 2)]


def test_a_panel_outside_the_window_is_unattributed(library, matcher):
    """The replay-screen orphan measured in the session: 3.6 s after the last release."""
    sink = []
    live = make_reader(matcher, sink, require_release=True)
    panel = build_strip(library, [("LATE", "red"), ("LIGHT CONTEST", "white"),
                                  ("OPEN", "white")])
    t = 1_789_511_849_000.0
    live.note_release(11, t)
    feed(live, panel, t=t + 3645.0, seq=1)
    assert sink == []
    assert live.verdicts_unattributed == 1
    # ...and a panel BEFORE any release at all (0.2 s: the previous shot's feedback still up)
    live2 = make_reader(matcher, [], require_release=True)
    live2.note_release(12, t)
    feed(live2, panel, t=t + 200.0, seq=1)
    assert live2.verdicts_unattributed == 1


def test_each_bot_shot_gets_its_own_attributed_verdict(library, matcher):
    """Three shots at the session's real cadence: release, ~1 s, banner, ~1.5 s of court."""
    sink = []
    live = make_reader(matcher, sink, require_release=True)
    court = build_strip(library, [])
    panels = [build_strip(library, [(w, c), ("WIDE OPEN", "white"), ("OPEN", "white")])
              for w, c in (("EXCELLENT", "green"), ("LATE", "red"), ("EXCELLENT", "green"))]
    t = 1_789_511_800_000.0
    for n, panel in enumerate(panels, start=1):
        live.note_release(n, t)
        for _ in range(10):                       # 1.0 s from release to the banner
            live.process(court, 0, 0, t)
            t += 100.0
        for _ in range(12):                       # banner up ~1.2 s
            live.process(panel, 0, 0, t)
            t += 100.0
        for _ in range(8):                        # court again before the next press
            live.process(court, 0, 0, t)
            t += 100.0
    evs = _events(sink)
    assert [(e["timing"], e["release_seq"], e["seq"]) for e in evs] == [
        ("EXCELLENT", 1, 1), ("LATE", 2, 2), ("EXCELLENT", 3, 3)]
    for e in evs:
        assert 900.0 <= e["release_delay_ms"] <= 1100.0, e
    assert live.verdicts_unattributed == 0


def test_require_release_off_forwards_everything(library, matcher):
    """The owner shooting by hand: the escape hatch still reports every panel."""
    sink = []
    live = make_reader(matcher, sink, require_release=False)
    feed(live, build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                     ("OPEN", "white")]))
    (ev,) = _events(sink)
    assert (ev["attributed"], ev["release_seq"]) == (0, -1)


def test_note_release_is_cheap_and_never_raises(matcher):
    live = bvl.BannerVerdictLive(pg, matcher, emit_line=None, stride=1, start_worker=False)
    assert live.note_release(1, 1_789_511_846_400.0) is True
    assert live.note_release(1, 1_789_511_846_400.0) is False    # the same edge, retried
    assert live.note_release(2, 0.0) is True                     # no stamp -> now()
    assert live.note_release("junk", "junk") is False
    assert live.note_release(None, None) is False
    # the ring never grows without bound
    for i in range(100):
        live.note_release(100 + i, 1_789_511_846_400.0 + i)
    assert len(live._releases) <= bvl._RELEASE_RING


# --------------------------------------------------------------------------- cadence
def test_stride_samples_every_sixth_frame(library, matcher):
    live = bvl.BannerVerdictLive(pg, matcher, emit_line=None, stride=6, start_worker=False)
    frame = build_frame(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    taken = [live.submit(frame, i) for i in range(60)]
    assert sum(taken) == 10                      # 60 frames -> 10 samples (~10 Hz at 60 fps)
    assert taken[:6] == [False] * 5 + [True]
    assert live.frames_sampled == 10


def test_submit_crops_the_strip_at_both_contract_sizes(library, matcher):
    live = bvl.BannerVerdictLive(pg, matcher, emit_line=None, stride=1, start_worker=False)
    for size in ((1280, 720), (1920, 1080)):
        frame = build_frame(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                      ("OPEN", "white")], size=size)
        strip = live._crop(frame)
        assert strip.shape == (STRIP_H, STRIP_W, 3), size
        ev = bvl.read_strip(pg, matcher, strip)
        assert ev is not None and ev["timing"] == "EXCELLENT", size


def test_worker_thread_delivers_one_verdict(library, matcher):
    """End-to-end through the real mailbox + worker thread."""
    import json
    import time as _time
    sink = []
    lock = __import__("threading").Lock()

    def emit(line):
        with lock:
            sink.append(line)

    live = bvl.BannerVerdictLive(pg, matcher, emit_line=emit, stride=1)
    live.note_release(4, 1757800000000.0 - 1000.0)   # the shot this banner belongs to
    try:
        frame = build_frame(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                      ("OPEN", "white")])
        deadline = _time.monotonic() + 5.0
        while _time.monotonic() < deadline:
            live.submit(frame, 1, frame_ts=1.0, epoch_ms=1757800000000.0)
            with lock:
                if sink:
                    break
            _time.sleep(0.01)
    finally:
        live.stop()
    assert sink, "worker produced no verdict"
    ev = json.loads(sink[0])
    assert ev["timing"] == "EXCELLENT"
    assert (ev["attributed"], ev["release_seq"]) == (1, 4)


# --------------------------------------------------------------------------- cost
def test_cost_per_frame_under_budget(library, matcher):
    """<= 0.3 ms per CAPTURED frame, averaged over the reduced cadence.

    Budgeted against the worst realistic duty cycle: a banner is up ~2 s of every ~12 s shot
    cycle, so ~1 sampled frame in 6 sees a panel.  Both halves are measured separately and
    recombined, because the expensive half (cell split + NCC) only runs when a panel is up.
    """
    import time as _time
    cv2.setNumThreads(1)
    stride = bvl.DEFAULT_STRIDE
    panel = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    court = build_strip(library, [])
    frame = build_frame(library, [])
    live = bvl.BannerVerdictLive(pg, matcher, emit_line=None, stride=stride,
                                 start_worker=False)

    def bench(fn, n):
        fn()
        t0 = _time.perf_counter()
        for _ in range(n):
            fn()
        return (_time.perf_counter() - t0) / n * 1000.0

    crop_ms = bench(lambda: live._crop(frame), 400)
    court_ms = bench(lambda: bvl.read_strip(pg, matcher, court), 200)
    panel_ms = bench(lambda: bvl.read_strip(pg, matcher, panel), 100)
    # per CAPTURED frame: one crop every `stride` frames, and one read every `stride`
    # frames of which at most 1 in 6 hits a live panel.
    per_frame = (crop_ms + (5.0 * court_ms + panel_ms) / 6.0) / stride
    print("\n  crop=%.4f ms  no-panel read=%.4f ms  panel read=%.4f ms  -> %.4f ms/frame"
          % (crop_ms, court_ms, panel_ms, per_frame))
    assert per_frame <= 0.3, "banner reader over budget: %.4f ms/frame" % per_frame
    # The detect thread itself only ever pays the crop.
    assert crop_ms / stride <= 0.05


# --------------------------------------------------------------------------- fast path
def _hud_strip(rng=None, cand_rows=20):
    """A dark HUD plate with bright horizontal furniture: what the strip really looks like
    between banners (2K draws the score bug / clock at y=12..52), and the shape that made
    panel_grade's O(n^2) candidate-pair loop expensive."""
    strip = np.full((STRIP_H, STRIP_W, 3), 20, np.uint8)
    step = max(1, STRIP_H // max(1, cand_rows))
    for y in range(0, STRIP_H, step):
        strip[y, 40:400] = 190
    return strip


def _diff_corpus(library):
    """Panels, HUD furniture, plain court and random noise — everything the splitter sees."""
    rng = np.random.default_rng(11)
    out = [("empty", build_strip(library, []))]
    _, labels = library
    for word in sorted(set(labels)):
        for color in ("green", "red", "yellow", "white"):
            out.append(("panel %s/%s" % (word, color),
                        build_strip(library, [(word, color), ("OPEN", "white"),
                                              ("WIDE OPEN", "white")])))
    for widths in ((150, 135, 135), (135, 150, 135), (200, 110, 110), (110, 110, 200)):
        out.append(("widths %s" % (widths,),
                    build_strip(library, [("LATE", "red"), ("LIGHT CONTEST", "white"),
                                          ("OPEN", "white")], widths=widths)))
    out.append(("2-cell", build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white")],
                                      widths=(210, 210))))
    for k in (1, 2, 3, 4, 8, 20, 40):
        out.append(("hud cand~%d" % k, _hud_strip(cand_rows=k)))
    for k in range(40):
        out.append(("noise%d" % k, rng.integers(0, 256, (STRIP_H, STRIP_W, 3), dtype=np.uint8)))
    for k in range(30):
        out.append(("court%d" % k,
                    np.clip(rng.normal(rng.integers(20, 200), 40, (STRIP_H, STRIP_W, 3)),
                            0, 255).astype(np.uint8)))
    for k in range(30):   # a panel with a random scratch through it
        s = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")]).copy()
        y, x = int(rng.integers(0, STRIP_H)), int(rng.integers(0, STRIP_W - 60))
        s[y:y + int(rng.integers(1, 6)), x:x + int(rng.integers(5, 60))] = \
            int(rng.integers(0, 255))
        out.append(("damaged%d" % k, s))
    return out


def test_fast_cells_match_panel_grade_exactly(library):
    """The live reader's GIL-free cell splitter must be panel_grade's, output for output.

    banner_verdict_live replaces two PURE-PYTHON pixel loops inside panel_grade.find_cells
    (per-row run scan; O(n^2) candidate-PAIR scan) with a 1x70 erosion and one gemm, because
    a Python loop of small numpy calls never releases the GIL and stalled the detect thread.
    Correctness is not argued from the rewrite — it is diffed here over every strip shape the
    splitter can see. A single difference means the live grade has drifted from the validated
    offline one.
    """
    corpus = _diff_corpus(library)
    assert len(corpus) >= 150
    for name, strip in corpus:
        want = pg.find_cells(strip)
        got = bvl._find_cells(pg, strip, bvl._planes(pg, strip))
        assert got == want, name
        want_cells = pg.read_cells(strip)
        got_cells = bvl.read_cells(pg, strip)
        assert len(got_cells) == len(want_cells), name
        for a, b in zip(want_cells, got_cells):
            assert (a["role"], a["color"], a["box"]) == (b["role"], b["color"], b["box"]), name
            assert (a["mask"] is None) == (b["mask"] is None), name
            if a["mask"] is not None:
                assert np.array_equal(a["mask"], b["mask"]), name


def test_live_read_matches_panel_grade_match_event(library, matcher):
    """End to end: the live verdict must be the verdict panel_grade's own event path gives.

    Covers the whole chain at once — cv2 signature/classify, the fast splitter, the shared
    planes, the vectorised matcher and the COVERAGE re-roling — against
    panel_grade.match_event over every colour of every shipped word plus random noise.
    """
    masks, labels = library
    rng = np.random.default_rng(3)
    strips = [build_strip(library, [(w, c), ("OPEN", "white"), ("WIDE OPEN", "white")])
              for w in sorted(set(labels)) for c in ("green", "red", "yellow", "white")]
    strips += [rng.integers(0, 256, (STRIP_H, STRIP_W, 3), dtype=np.uint8)
               for _ in range(120)]
    compared = 0
    for strip in strips:
        got = bvl.read_strip(pg, matcher, strip)
        if got is None:
            continue           # no panel: panel_grade's event path yields no verdict either
        want = dict(color=pg.classify(pg.signature(strip)), cells=pg.read_cells(strip))
        pg.match_event(want, masks, labels)
        assert got["timing"] == want["word"]
        assert got["timing_color"] == want["timing_color"]
        # the one documented divergence: panel_grade keeps "UNKNOWN" in its CSV column, the
        # live event reports no coverage at all rather than a word the UI does not know
        assert got["coverage"] == ("" if want["coverage"] == "UNKNOWN" else want["coverage"])
        compared += 1
    assert compared >= 20


def test_fast_path_is_faster_on_every_strip_shape(library):
    """The point of the rewrite: no strip may cost more than a fraction of a millisecond."""
    import time as _time
    cv2.setNumThreads(1)
    matcher = bvl._Matcher(pg, *library)
    cases = {
        "HUD plate, 20 candidate rows": _hud_strip(cand_rows=20),
        "HUD plate, every row a candidate": _hud_strip(cand_rows=STRIP_H),
        "live panel": build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                            ("OPEN", "white")]),
        "bright court (gate rejects)": np.full((STRIP_H, STRIP_W, 3), 150, np.uint8),
    }

    def bench(fn, n=40):
        """Best-of-n, not mean: this box runs the live app, and a mean is a measure of
        whatever else was scheduled. The floor is the cost of the code under test."""
        fn()
        best = float("inf")
        for _ in range(n):
            t0 = _time.perf_counter()
            fn()
            best = min(best, (_time.perf_counter() - t0) * 1000.0)
        return best

    print()
    for name, strip in cases.items():
        old = bench(lambda s=strip: (pg.planes(s), pg.read_cells(s)))
        new = bench(lambda s=strip: bvl.read_strip(pg, matcher, strip=s))
        print("  %-36s panel_grade %6.3f ms -> live %6.3f ms (%4.1fx)"
              % (name, old, new, old / max(new, 1e-9)))
        assert new <= 1.0, "%s: %.3f ms per read" % (name, new)
        assert new < old


# --------------------------------------------------------------------------- stall budget
def _detect_work(frame, _canvas=[None]):
    """One iteration with the real detector's GIL profile.

    meter_detector_yolo.MeterYoloLocator per frame: a letterbox cv2.resize (GIL released),
    a numpy canvas paint + blob cast (GIL HELD — the source calls this out: "the numpy blob
    ... held the GIL"), then the ORT Run (GIL released, ~9.7 ms). A competing Python thread
    can only steal the GIL during the released stretches, which is exactly the failure mode
    under test.
    """
    small = cv2.resize(frame, (960, 540), interpolation=cv2.INTER_LINEAR)
    canvas = np.zeros((960, 960, 3), np.uint8)
    canvas[210:750] = small
    blob = (canvas.astype(np.float32) * (1.0 / 255.0)).transpose(2, 0, 1)[None]
    cv2.GaussianBlur(frame, (9, 9), 0)            # stands in for the ORT Run
    return float(blob[0, 0, 0, 0])


class _SidecarLoad:
    """The other Python threads the detect thread really shares the GIL with: the 60 fps
    preview encoder (~130 KB base64, chunked), the 60 Hz telemetry builder, the ~1 kHz input
    router. Without them a single-threaded box shows no contention and the test is vacuous."""

    def __init__(self, frame):
        import base64
        import json
        import threading
        self._stop = threading.Event()
        ok, jpg = cv2.imencode(".jpg", cv2.resize(frame, (960, 540)),
                               [cv2.IMWRITE_JPEG_QUALITY, 70])
        raw = jpg.tobytes() if ok else b"x" * 90000
        payload = {"event": "telemetry", **{"k%d" % i: i * 1.5 for i in range(120)}}

        def preview():
            while not self._stop.is_set():
                b = base64.b64encode(raw)
                for i in range(0, len(b), 6000):
                    _ = b[i:i + 6000]
                time.sleep(1 / 60.0)

        def telemetry():
            while not self._stop.is_set():
                _ = json.dumps(payload, separators=(",", ":"))
                time.sleep(1 / 60.0)

        def router():
            while not self._stop.is_set():
                _ = sum(range(200))
                time.sleep(0.001)

        self._threads = [threading.Thread(target=f, daemon=True)
                         for f in (preview, telemetry, router)]
        for t in self._threads:
            t.start()

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(1.0)


def test_detect_thread_stall_budget_with_the_reader_active(library):
    """The 2026-09-14 live regression, as a gate.

    A 60 fps detect thread with the real detector's GIL profile, inside the sidecar's real
    thread population, raised above normal priority the way ORION_DETECT_THREAD_PRIORITY
    raises it. ON and OFF blocks are INTERLEAVED so machine drift cancels. Budget: the
    reader may cost at most +1 ms of p99 and must leave no iteration over 20 ms.
    """
    import ctypes
    import threading
    cv2.setNumThreads(1)
    matcher = bvl._Matcher(pg, *library)

    rng = np.random.default_rng(5)
    base = np.clip(rng.normal(110, 30, (720, 1280, 3)), 0, 255).astype(np.uint8)
    court = base.copy()
    court[pg.Y0:pg.Y1, pg.X0:pg.X1] = _hud_strip(cand_rows=20)
    panel_frame = base.copy()
    panel_frame[pg.Y0:pg.Y1, pg.X0:pg.X1] = build_strip(
        library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"), ("OPEN", "white")])

    reader = bvl.BannerVerdictLive(pg, matcher, emit_line=None,
                                   stride=bvl.DEFAULT_STRIDE)
    load = _SidecarLoad(base)
    blocks = {}                 # block index -> [iteration ms]; even = OFF, odd = ON
    stop = threading.Event()

    def detect_loop():
        try:                                   # THREAD_PRIORITY_HIGHEST, as the sidecar does
            ctypes.windll.kernel32.SetThreadPriority(
                ctypes.windll.kernel32.GetCurrentThread(), 2)
        except Exception:
            pass
        i = 0
        nxt = time.perf_counter()
        while not stop.is_set():
            nxt += 1 / 60.0
            blk = i // 15                            # 0.25 s blocks, alternating OFF/ON:
            block_on = (blk % 2) == 1                 # short enough that thermal/scheduler
            # drift lands on both arms equally, and adjacent blocks pair up cleanly.
            # The banner occupies the same slice of EVERY block, so the ON and OFF arms see
            # byte-identical frame content and the reader is guaranteed panel work to do.
            # 8 frames in 15 is far more banner than a real session ever shows.
            frame = panel_frame if (i % 15) < 8 else court
            t0 = time.perf_counter()
            _detect_work(frame)
            dt = (time.perf_counter() - t0) * 1000.0
            blocks.setdefault(blk, []).append(dt)
            if block_on:
                reader.submit(frame, i, frame_ts=t0, epoch_ms=time.time() * 1000.0)
            i += 1
            d = nxt - time.perf_counter()
            if d > 0:
                time.sleep(d)
            else:
                nxt = time.perf_counter()

    th = threading.Thread(target=detect_loop, daemon=True)
    th.start()
    try:
        time.sleep(12.0)
    finally:
        stop.set()
        th.join(3.0)
        reader.stop()
        load.stop()

    # drop the first two blocks (thread start-up / first-touch page faults)
    keys = sorted(k for k in blocks if k >= 2 and len(blocks[k]) >= 10)
    off_b = [blocks[k] for k in keys if k % 2 == 0]
    on_b = [blocks[k] for k in keys if k % 2 == 1]
    off = [x for v in off_b for x in v]
    on = [x for v in on_b for x in v]

    def q(v, p):
        v = sorted(v)
        return v[min(len(v) - 1, int(p * len(v)))]

    def stats(v):
        v = sorted(v)
        tailn = max(5, len(v) // 20)
        return dict(n=len(v), p50=q(v, .50), p95=q(v, .95), p99=q(v, .99), mx=v[-1],
                    # mean of the worst 5%: a single max is ONE sample and swings with
                    # whatever else the box is doing, and even the worst 1% is only three
                    # samples here; averaging ~16 makes this a stable tail statistic
                    tail=sum(v[-tailn:]) / tailn)

    a, b = stats(off), stats(on)
    # Paired blockwise difference: each ON block against the OFF block beside it, so
    # scheduler/thermal drift over the run cancels instead of landing on one arm.
    pairs = sorted(q(x, .90) - q(y, .90) for x, y in zip(on_b, off_b))
    paired = pairs[len(pairs) // 2] if pairs else 0.0
    # The measurement noise of this statistic, on this box, right now: the SAME comparison
    # made between two halves of the baseline, which by construction differ only by noise.
    half = len(off_b) // 2
    noise = abs(q([x for v in off_b[:half] for x in v], .99)
                - q([x for v in off_b[half:] for x in v], .99)) if half >= 2 else 0.0

    print("\n  detect thread, reader OFF: n=%d p50=%.2f p95=%.2f p99=%.2f max=%.2f"
          % (a["n"], a["p50"], a["p95"], a["p99"], a["mx"]))
    print("  detect thread, reader ON : n=%d p50=%.2f p95=%.2f p99=%.2f max=%.2f"
          % (b["n"], b["p50"], b["p95"], b["p99"], b["mx"]))
    print("  delta p99 %+.2f ms (noise floor %.2f)   paired blockwise p90 %+.2f ms   "
          "delta tail5%% %+.2f ms   verdicts=%d"
          % (b["p99"] - a["p99"], noise, paired, b["tail"] - a["tail"], reader.verdicts))
    assert a["n"] > 150 and b["n"] > 150, "loop did not run long enough"
    assert len(pairs) >= 8, "not enough interleaved blocks to pair"
    assert reader.verdicts >= 1, "the reader never graded anything: the test proves nothing"

    # PRIMARY: the paired, drift-cancelling version of the coordinator's +1 ms p99 budget.
    # Median over ~20 block pairs, so one unlucky block cannot decide the verdict.
    assert paired <= 1.0, \
        "reader costs %.2f ms of detect-thread latency (paired blockwise p90)" % paired
    # SECONDARY: the pooled p99 budget, widened by the statistic's own measured noise floor
    # so a box that is busy with something else fails loudly rather than flakily. The
    # regression this gates was 60-271 ms; 1 ms plus a ~1-3 ms noise floor still catches it
    # by two orders of magnitude.
    assert b["p99"] - a["p99"] <= max(1.0, noise), \
        "reader costs %.2f ms of detect-thread p99 (noise floor %.2f)" % (
            b["p99"] - a["p99"], noise)
    # The reader must not introduce a new CLASS of stall, measured against the baseline's
    # own tail so this still means something on an already-stalling box -- which is exactly
    # when the absolute bar below cannot be measured.
    assert b["tail"] - a["tail"] <= 5.0, \
        "reader pushed the detect-thread tail from %.1f to %.1f ms" % (a["tail"], b["tail"])
    if a["mx"] >= 15.0:
        pytest.skip("machine too loaded to prove the absolute 20 ms bar: the BASELINE "
                    "already peaks at %.1f ms (paired blockwise delta %+.2f ms and the "
                    "tail check above still passed)" % (a["mx"], paired))
    assert b["mx"] < 20.0, "reader left a %.1f ms detect-thread stall" % b["mx"]


# --------------------------------------------------------------------------- fail-soft
def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("ORION_BANNER_VERDICT_LIVE", "0")
    assert bvl.BannerVerdictLive.create() is None


def test_default_is_on_for_ship(monkeypatch, caplog):
    """[SHIP CONFIG 2026-09-17] The default FLIPPED: OFF (09-14) -> ON.

    It shipped OFF after the 09-14 session put 62-271 ms stalls into the detect thread, and the
    stall budget was signed off by `test_the_reader_costs_the_detect_thread_nothing_measurable`
    above plus every graded session from 09-15 on, all of which ran ORION_BANNER_VERDICT_LIVE=1
    from the dev launch line.  A customer never runs that launcher, so leaving the SOURCE
    default at 0 would have shipped a build with no banner verdicts at all -- and therefore a
    `banner_lead_trim` (default TRUE) that never receives an observation.

    Asserted on the LOG rather than the return value: create() also returns None on every
    fail-soft path (panel_grade is untracked dev tooling), and only the log distinguishes
    "switched off" from "armed but unavailable".
    """
    monkeypatch.delenv("ORION_BANNER_VERDICT_LIVE", raising=False)
    with caplog.at_level(logging.INFO):
        bvl.BannerVerdictLive.create()
    assert not any("reader: OFF" in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]


def test_create_is_fail_soft_when_panel_grade_is_missing(monkeypatch, caplog):
    monkeypatch.setenv("ORION_BANNER_VERDICT_LIVE", "1")
    monkeypatch.setattr(bvl, "load_panel_grade", lambda: None)
    with caplog.at_level(logging.WARNING):
        assert bvl.BannerVerdictLive.create() is None
    assert any("DISABLED" in r.message or "DISABLED" in r.getMessage()
               for r in caplog.records)


def test_create_is_fail_soft_when_the_library_is_empty(monkeypatch):
    monkeypatch.setenv("ORION_BANNER_VERDICT_LIVE", "1")

    class _Stub:
        NCC_MATCH = pg.NCC_MATCH
        LIB_PATH = "nowhere.npz"

        @staticmethod
        def load_library():
            return [], []

    monkeypatch.setattr(bvl, "load_panel_grade", lambda: _Stub)
    assert bvl.BannerVerdictLive.create() is None


def test_reader_exception_disables_for_the_session_with_one_warning(library, matcher,
                                                                    monkeypatch, caplog):
    """A fault in the grader must kill the FEATURE, not the detect loop, and warn ONCE.

    Drives the real worker thread so the production failure path (worker catches -> one
    WARNING -> enabled=False -> submit() becomes a no-op) is the one under test.
    """
    import time as _time
    sink = []
    calls = []

    def exploding(*_a, **_k):
        calls.append(1)
        raise RuntimeError("synthetic grader fault")

    monkeypatch.setattr(bvl, "read_strip", exploding)
    frame = build_frame(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    with caplog.at_level(logging.WARNING):
        live = bvl.BannerVerdictLive(pg, matcher, emit_line=sink.append, stride=1)
        try:
            deadline = _time.monotonic() + 5.0
            while live.enabled and _time.monotonic() < deadline:
                live.submit(frame, 1)
                _time.sleep(0.005)
            # 20 more frames after the fault: no further work, no further warnings
            for i in range(20):
                assert live.submit(frame, i) is False
        finally:
            live.stop()
    assert live.enabled is False, "reader stayed enabled after a grader fault"
    assert len(calls) == 1, "kept grading after the fault"
    warnings = [r for r in caplog.records
                if r.levelno >= logging.WARNING and "DISABLED" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert sink == []


def test_submit_never_raises_on_a_bad_frame(matcher):
    live = bvl.BannerVerdictLive(pg, matcher, emit_line=None, stride=1, start_worker=False)
    assert live.submit(None, 1) is False
    assert live.submit(np.zeros((4, 4), np.uint8), 2) is False
    assert live.submit(np.zeros((8, 8, 3), np.uint8), 3) is False


# --------------------------------------------------------------------------- wiring
def test_the_orchestrator_release_edge_feeds_the_reader(monkeypatch):
    """Attribution is only worth anything if the release edge actually arrives.

    ``release_shot_gate`` is the single funnel for every engine release -- the normal one
    and the METER BACKSTOP one (2026-09-15: epoch 6 fired the backstop, released, and never
    produced a ``release marker`` line, so the estimator's marker is NOT a complete feed).
    A disarm is not a release: the bot did not shoot, so nothing may be attributed to it.
    """
    monkeypatch.setenv("ORION_SIMPLE_READER", "1")
    from remote_play_orchestrator import OrchestratorConfig, RemotePlayOrchestrator
    orch = RemotePlayOrchestrator(OrchestratorConfig(
        console_ip="1.2.3.4", virtual_controller=False, hidhide=False,
        auto_launch_client=False))
    assert orch._banner_verdict is None            # the gate is off by default: no-op path
    orch.release_shot_gate(5, 1_789_511_828_608.1)

    seen = []

    class _Reader:
        def note_release(self, seq, release_ms):
            seen.append((seq, release_ms))

    orch._banner_verdict = _Reader()
    orch.release_shot_gate(6, 1_789_511_832_058.9)  # the backstop release
    assert seen == [(6, 1_789_511_832_058.9)]
    orch.disarm_shot_gate(7, "manual_cancel")
    assert len(seen) == 1, "a disarmed press was attributed as a shot"

    class _Exploding:
        def note_release(self, *_a, **_k):
            raise RuntimeError("synthetic")

    orch._banner_verdict = _Exploding()
    assert orch.release_shot_gate(8, 1.0) is True   # never takes the shot gate down


# --------------------------------------------------------------------------- CLI parity
def test_strip_of_is_a_pure_refactor_of_strip_from_frame(library, tmp_path):
    """The ONE change to the validated grader: strip_of() now delegates its crop.

    Proven byte-for-byte on a real PNG round trip, at both the native size and a size that
    exercises the resize branch, so the offline verdicts cannot have moved.
    """
    session = tmp_path / "session_test"
    session.mkdir()
    (session / "frames.csv").write_text("idx,detected,t_wall\n0,1,0.0\n", encoding="utf-8")
    for size in ((1280, 720), (1920, 1080)):
        frame = build_frame(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                      ("OPEN", "white")], size=size)
        path = str(session / "f00000_1_raw.png")
        assert cv2.imwrite(path, frame)
        row = {"idx": "0", "detected": "1", "t_wall": "0.0"}
        from_disk = pg.strip_of(str(session), row)
        direct = pg.strip_from_frame(cv2.imread(path))
        assert from_disk is not None and direct is not None
        assert np.array_equal(from_disk, direct), size
        assert from_disk.shape == (STRIP_H, STRIP_W, 3), size
    assert pg.strip_from_frame(None) is None


def test_panel_grade_cli_still_grades_the_archived_session_identically(tmp_path):
    """panel_grade.py is untracked but VALIDATED (28/28, 51/51); exposing strip_from_frame
    must not move a single verdict.

    The archived session holds only frames.csv + panel_grade.csv (the PNG frames were
    deleted to free 12 GB), so when the frames are gone this asserts the next best thing:
    the function path the live reader uses reproduces the archived CSV's verdict vocabulary,
    and the CLI's frame->strip helper is a pure refactor of the disk path.
    """
    import csv
    import subprocess
    import sys

    session = os.path.join("logs", "diagnostics", "framedump_archive",
                           "session_20260912_124114")
    csv_path = os.path.join(session, "panel_grade.csv")
    if not os.path.isfile(csv_path):
        pytest.skip("archived session_20260912_124114/panel_grade.csv is not present")
    rows = list(csv.DictReader(open(csv_path, newline="", encoding="utf-8")))
    assert rows, "archived panel_grade.csv is empty"

    frames = sorted(f for f in os.listdir(session) if f.endswith("_raw.png"))
    if not frames:
        # No pixels on disk: re-running the CLI is impossible. Pin the two things that are
        # still checkable -- the archived vocabulary is exactly the shipped library's, and
        # strip_of() is now literally strip_from_frame(imread(...)).
        _, labels = pg.load_library()
        known = {str(x) for x in labels} | {"", "UNKNOWN"}
        words = {r["word"] for r in rows} | {r["coverage"] for r in rows}
        assert words <= known, sorted(words - known)
        img = np.zeros((720, 1280, 3), np.uint8)
        img[pg.Y0:pg.Y1, pg.X0:pg.X1] = 77
        strip = pg.strip_from_frame(img)
        assert strip.shape == (STRIP_H, STRIP_W, 3)
        assert int(strip.min()) == 77 and int(strip.max()) == 77
        big = np.zeros((1080, 1920, 3), np.uint8)
        assert pg.strip_from_frame(big).shape == (STRIP_H, STRIP_W, 3)
        assert pg.strip_from_frame(None) is None
        pytest.skip("no *_raw.png frames in the archived session: CLI re-run not possible "
                    "(vocabulary + strip_from_frame parity asserted instead)")

    out = tmp_path / "panel_grade.csv"
    rc = subprocess.run([sys.executable, os.path.join("tools", "timing", "panel_grade.py"),
                         session, "--out", str(out)], capture_output=True, text=True)
    assert rc.returncode == 0, rc.stdout + rc.stderr
    fresh = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
    keep = ("ev", "color", "word", "ncc", "timing_color", "coverage", "cov_ncc", "cells")
    assert [{k: r[k] for k in keep} for r in fresh] == \
           [{k: r[k] for k in keep} for r in rows]


# ------------------------------------------------------- the DISTANCE cell (09-17)
def test_the_distance_is_forwarded_and_voted_over_the_appearance(library, matcher,
                                                                 monkeypatch):
    """[ORION_BANNER_DISTANCE 2026-09-17] The panel prints the shot's own distance, which
    is a free exact RANGE label on every graded shot. The synthetic strips here carry word
    templates, not printed digits, so the READ itself is stubbed -- what this pins is the
    wiring: the value reaches the payload, and it is the appearance's MODE, not the last
    sample (three of 1,845 corpus frames read wrong on a single ghosted frame)."""
    reads = iter(["23'5\"", "23'5\"", "18'8\"", "23'5\""])
    monkeypatch.setattr(bvl._bd, "read_distance",
                        lambda strip, cell, planes_=None: (next(reads, "23'5\""),
                                                           23.417, {}))
    sink = []
    live = make_reader(matcher, sink, require_release=True)
    strip = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    live.note_release(5, 1757800000000.0 - 1000.0)
    feed(live, strip, n=4, t=1757800000000.0, seq=1)
    (ev,) = _events(sink)
    assert ev["distance"] == "23'5\""
    assert ev["distance_ft"] == pytest.approx(23 + 5 / 12.0, abs=1e-3)


def test_an_unreadable_distance_never_holds_the_verdict_back(library, matcher, monkeypatch):
    """Fail-closed: the timing verdict is what the owner is waiting for, and it must not
    wait for a cell the reader cannot resolve."""
    monkeypatch.setattr(bvl._bd, "read_distance",
                        lambda strip, cell, planes_=None: ("", None, {"reason": "x"}))
    sink = []
    live = make_reader(matcher, sink, require_release=True)
    strip = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    live.note_release(6, 1757800000000.0 - 1000.0)
    feed(live, strip, t=1757800000000.0, seq=2)
    (ev,) = _events(sink)
    assert ev["timing"] == "EXCELLENT"
    assert ev["distance"] == "" and ev["distance_ft"] == -1.0


def test_a_missing_distance_reader_is_not_fatal(library, matcher, monkeypatch):
    """banner_distance is imported optionally; without it the reader is unchanged."""
    monkeypatch.setattr(bvl, "_bd", None)
    sink = []
    live = make_reader(matcher, sink, require_release=True)
    assert live._dist is None
    strip = build_strip(library, [("EXCELLENT", "green"), ("WIDE OPEN", "white"),
                                  ("OPEN", "white")])
    live.note_release(7, 1757800000000.0 - 1000.0)
    feed(live, strip, t=1757800000000.0, seq=3)
    (ev,) = _events(sink)
    assert ev["timing"] == "EXCELLENT" and ev["distance"] == ""


def test_incomplete_one_cell_layout_is_not_coverage_absent(library, matcher):
    # A visible timing cell does not prove that a coverage cell never existed:
    # the rest of the panel may still be fading in or have been occluded.
    panel = build_strip(library, [("LATE", "red")], widths=(135,))
    event = bvl.read_strip(pg, matcher, panel)
    assert event is not None and event["timing"] == "LATE"
    assert event["has_coverage"] is True  # unknown layout must not train as OPEN
    assert not bvl._complete(event)
