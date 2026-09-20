"""shot_range.py -- THREE / MID / UNKNOWN from the nameplate "3" cell.

These tests drive the classifier with SYNTHETIC cell readings and the live reader with a
synthetic frame feed (a plain numpy array and an injected plate), so nothing here needs a
capture card, a template or a real press.  They pin:

  * the per-sample rule (the study's dark/bright gate),
  * the majority vote, the split refusal and the min-sample floor,
  * that an UNCALIBRATED classifier reports the evidence but verdicts `unknown`,
  * the press window (press+40..120 ms, at most `max_samples` frames),
  * the plate contract (no plate / stale plate / unreadable cell all degrade to `unknown`),
  * the message shape on the stdout channel,
  * the detect-thread cost budget.
"""

import json
import time

import numpy as np
import pytest

import shot_range
from shot_range import (RANGE_MID, RANGE_THREE, RANGE_UNKNOWN, ShotRangeClassifier,
                        ShotRangeReader, cell_box, cell_stats)


# --------------------------------------------------------------------------- helpers
def _frame(cell_value=0, bg=200, plate=(300.0, 400.0, 1.0), w=1280, h=720):
    """A frame whose "3" cell is painted `cell_value` on a `bg` background."""
    img = np.full((h, w, 3), bg, np.uint8)
    box = cell_box(plate[0], plate[1], plate[2], w, h)
    assert box is not None
    x0, y0, x1, y1 = box
    img[y0:y1, x0:x1] = cell_value
    return img


def _icon_frame(plate=(300.0, 400.0, 1.0)):
    """The icon PRESENT: a near-black disc with a bright glyph in the middle of it."""
    img = _frame(cell_value=10, plate=plate)
    box = cell_box(plate[0], plate[1], plate[2], img.shape[1], img.shape[0])
    x0, y0, x1, y1 = box
    gx0 = x0 + (x1 - x0) // 4
    gy0 = y0 + (y1 - y0) // 4
    img[gy0:gy0 + max(1, (y1 - y0) // 2), gx0:gx0 + max(1, (x1 - x0) // 2)] = 250
    return img


def _reader(**kw):
    kw.setdefault('start_thread', False)
    return ShotRangeReader(**kw)


# --------------------------------------------------------------------------- geometry
def test_cell_box_is_one_pitch_left_of_the_ps_disc():
    box = cell_box(300.0, 400.0, 1.0, 1280, 720)
    x0, y0, x1, y1 = box
    assert abs((x0 + x1) / 2.0 - (300.0 - shot_range.PITCH_PX)) <= 1.0
    assert abs((y0 + y1) / 2.0 - 400.0) <= 1.0


def test_cell_box_refuses_a_box_that_does_not_fit():
    assert cell_box(2.0, 400.0, 1.0, 1280, 720) is None
    assert cell_box(300.0, 400.0, 1.0, 0, 0) is None


def test_cell_stats_reads_dark_and_bright_fractions():
    dark = cell_stats(_frame(cell_value=5), 300.0, 400.0, 1.0)
    assert dark is not None
    assert dark[1] == pytest.approx(1.0)
    assert dark[2] == pytest.approx(0.0)
    bright = cell_stats(_frame(cell_value=230), 300.0, 400.0, 1.0)
    assert bright[1] == pytest.approx(0.0)
    assert bright[2] == pytest.approx(1.0)


def test_cell_stats_never_raises_on_a_missing_frame():
    assert cell_stats(None, 1.0, 1.0, 1.0) is None


# --------------------------------------------------------------------------- policy
def test_sample_rule_matches_the_study_gate():
    clf = ShotRangeClassifier(calibrated=True)
    assert clf.sample_range(40.0, 0.50, 0.20) == RANGE_THREE
    assert clf.sample_range(40.0, 0.27, 0.20) == RANGE_MID      # not dark enough
    assert clf.sample_range(40.0, 0.50, 0.11) == RANGE_MID      # no glyph
    assert clf.sample_range(40.0, None, 0.20) == RANGE_MID      # unreadable -> never three


def test_majority_vote_wins():
    clf = ShotRangeClassifier(calibrated=True)
    out = clf.classify([(40, 20, 0.5, 0.3), (60, 20, 0.5, 0.3), (80, 190, 0.0, 0.9)])
    assert out['range'] == RANGE_THREE
    assert out['conf'] == pytest.approx(2 / 3, abs=1e-3)
    assert out['reason'] == 'vote'


def test_a_split_vote_is_unknown():
    clf = ShotRangeClassifier(calibrated=True)
    out = clf.classify([(40, 20, 0.5, 0.3), (60, 190, 0.0, 0.9)])
    assert out['range'] == RANGE_UNKNOWN
    assert out['reason'] == 'split'
    assert out['conf'] == 0.0


def test_one_sample_is_not_a_verdict():
    clf = ShotRangeClassifier(calibrated=True, min_samples=2)
    out = clf.classify([(40, 20, 0.5, 0.3)])
    assert out['range'] == RANGE_UNKNOWN
    assert out['reason'] == 'too_few_samples'
    assert out['samples'] == 1


def test_no_samples_is_unknown():
    out = ShotRangeClassifier(calibrated=True).classify([])
    assert out['range'] == RANGE_UNKNOWN
    assert out['reason'] == 'no_samples'
    assert out['dark'] is None


def test_uncalibrated_reports_evidence_but_verdicts_unknown():
    """The shipped default. See the module docstring: the 2026-09-17 offline pass did not
    clear the 90 % agreement bar, so the vote is RECORDED and the lead is not moved."""
    clf = ShotRangeClassifier()          # calibrated defaults to False
    out = clf.classify([(40, 20, 0.5, 0.3), (60, 20, 0.5, 0.3)])
    assert out['evidence'] == RANGE_THREE
    assert out['range'] == RANGE_UNKNOWN
    assert out['reason'] == 'uncalibrated'
    assert out['conf'] == 0.0
    assert out['dark'] == pytest.approx(0.5)


def test_classifier_from_env_reads_every_knob():
    env = {'ORION_SHOT_RANGE_DARK_MIN': '0.4', 'ORION_SHOT_RANGE_BRIGHT_MIN': '0.2',
           'ORION_SHOT_RANGE_MIN_SAMPLES': '3', 'ORION_SHOT_RANGE_CALIBRATED': '1'}
    clf = ShotRangeClassifier.from_env(env)
    assert (clf.dark_min, clf.bright_min, clf.min_samples, clf.calibrated) == (
        0.4, 0.2, 3, True)


def test_classifier_from_env_ignores_junk():
    clf = ShotRangeClassifier.from_env({'ORION_SHOT_RANGE_DARK_MIN': 'banana'})
    assert clf.dark_min == shot_range.DEFAULT_DARK_MIN


# --------------------------------------------------------------------------- live reader
def test_window_collects_only_frames_inside_press_plus_40_to_120():
    seen = []
    rdr = _reader(plate_fn=lambda: (300.0, 400.0, 1.0, 100.0),
                  on_result=lambda ep, p, c: seen.append((ep, p, c)),
                  classifier=ShotRangeClassifier(calibrated=True))
    rdr.note_press(7, 100_000.0)
    rdr.note_frame(_icon_frame(), 100.020)      # +20 ms: too early
    assert rdr._frames == []                    # noqa: SLF001
    rdr.note_frame(_icon_frame(), 100.050)      # +50
    rdr.note_frame(_icon_frame(), 100.080)      # +80
    rdr.note_frame(_icon_frame(), 100.110)      # +110 -> third sample closes the window
    rdr._worker_drain()                         # noqa: SLF001
    assert len(seen) == 1
    epoch, payload, cells = seen[0]
    assert epoch == 7
    assert payload['samples'] == 3
    assert payload['range'] == RANGE_THREE
    assert [c[0] for c in cells] == [50.0, 80.0, 110.0]


def test_a_frame_past_the_window_closes_it_with_what_it_has():
    seen = []
    rdr = _reader(plate_fn=lambda: (300.0, 400.0, 1.0, 100.0),
                  on_result=lambda ep, p, c: seen.append(p),
                  classifier=ShotRangeClassifier(calibrated=True))
    rdr.note_press(9, 100_000.0)
    rdr.note_frame(_icon_frame(), 100.050)
    rdr.note_frame(_icon_frame(), 100.090)
    rdr.note_frame(_icon_frame(), 100.400)      # past hi_ms: flush
    rdr._worker_drain()                         # noqa: SLF001
    assert len(seen) == 1
    assert seen[0]['samples'] == 2
    assert seen[0]['range'] == RANGE_THREE


def test_close_flushes_a_window_that_never_filled():
    seen = []
    rdr = _reader(plate_fn=lambda: (300.0, 400.0, 1.0, 100.0),
                  on_result=lambda ep, p, c: seen.append(p))
    rdr.note_press(11, 100_000.0)
    rdr.note_frame(_icon_frame(), 100.050)
    rdr.note_close(11)
    rdr._worker_drain()                         # noqa: SLF001
    assert len(seen) == 1
    assert seen[0]['samples'] == 1
    assert seen[0]['range'] == RANGE_UNKNOWN    # one frame is never a verdict


def test_a_duplicate_epoch_does_not_restart_the_window():
    """The native re-sends the same arm with source=type_upgrade; the press did not move."""
    rdr = _reader(plate_fn=lambda: (300.0, 400.0, 1.0, 100.0))
    assert rdr.note_press(3, 100_000.0) is True
    rdr.note_frame(_icon_frame(), 100.050)
    assert rdr.note_press(3, 100_500.0) is False
    assert len(rdr._frames) == 1                # noqa: SLF001
    assert rdr.presses == 1


def test_no_plate_is_unknown_not_a_guess():
    seen = []
    rdr = _reader(plate_fn=lambda: None, on_result=lambda ep, p, c: seen.append(p),
                  classifier=ShotRangeClassifier(calibrated=True))
    rdr.note_press(4, 100_000.0)
    for t in (100.050, 100.080, 100.110):
        rdr.note_frame(_icon_frame(), t)
    rdr._worker_drain()                         # noqa: SLF001
    assert seen[0]['range'] == RANGE_UNKNOWN
    assert seen[0]['source'] == 'no_plate'
    assert seen[0]['samples'] == 0


def test_a_stale_plate_is_refused():
    seen = []
    # plate stamped 5 s before the window's own clock
    rdr = _reader(plate_fn=lambda: (300.0, 400.0, 1.0, 95.0),
                  on_result=lambda ep, p, c: seen.append(p),
                  classifier=ShotRangeClassifier(calibrated=True))
    rdr.note_press(5, 100_000.0)
    for t in (100.050, 100.080, 100.110):
        rdr.note_frame(_icon_frame(), t)
    rdr._worker_drain()                         # noqa: SLF001
    assert seen[0]['source'] == 'plate_stale'
    assert seen[0]['range'] == RANGE_UNKNOWN


def test_a_plate_off_the_edge_of_the_frame_is_unreadable():
    seen = []
    rdr = _reader(plate_fn=lambda: (2.0, 400.0, 1.0, 100.0),
                  on_result=lambda ep, p, c: seen.append(p),
                  classifier=ShotRangeClassifier(calibrated=True))
    rdr.note_press(6, 100_000.0)
    for t in (100.050, 100.080, 100.110):
        rdr.note_frame(_icon_frame(), t)
    rdr._worker_drain()                         # noqa: SLF001
    assert seen[0]['source'] == 'cell_unreadable'
    assert seen[0]['range'] == RANGE_UNKNOWN


def test_a_mid_range_press_reads_mid():
    seen = []
    rdr = _reader(plate_fn=lambda: (300.0, 400.0, 1.0, 100.0),
                  on_result=lambda ep, p, c: seen.append(p),
                  classifier=ShotRangeClassifier(calibrated=True))
    rdr.note_press(8, 100_000.0)
    for t in (100.050, 100.080, 100.110):
        rdr.note_frame(_frame(cell_value=210), t)     # court wood: bright, not dark
    rdr._worker_drain()                               # noqa: SLF001
    assert seen[0]['range'] == RANGE_MID


# --------------------------------------------------------------------------- the message
def test_emitted_line_is_the_agreed_schema():
    lines = []
    rdr = _reader(plate_fn=lambda: (300.0, 400.0, 1.0, 100.0), emit=lines.append,
                  classifier=ShotRangeClassifier(calibrated=True))
    rdr.note_press(42, 100_000.0)
    for t in (100.050, 100.080, 100.110):
        rdr.note_frame(_icon_frame(), t)
    rdr._worker_drain()                         # noqa: SLF001
    assert len(lines) == 1
    assert lines[0].endswith('\n')
    msg = json.loads(lines[0])
    assert msg['event'] == 'shot_range'
    assert msg['release_seq'] == 42
    assert msg['range'] == RANGE_THREE
    assert 0.0 <= msg['conf'] <= 1.0
    for key in ('samples', 'source', 'reason', 'evidence', 'mean', 'dark', 'bright', 't_ms'):
        assert key in msg


def test_create_is_off_when_the_knob_is_zero():
    assert ShotRangeReader.create(env={'ORION_SHOT_RANGE': '0'}) is None


def test_create_is_off_under_pytest_unless_asked():
    assert ShotRangeReader.create(env={'PYTEST_CURRENT_TEST': 'x'}) is None
    rdr = ShotRangeReader.create(env={'ORION_SHOT_RANGE': '1', 'PYTEST_CURRENT_TEST': 'x'},
                                 start_thread=False)
    assert rdr is not None


def test_create_never_raises_on_a_bad_env():
    assert ShotRangeReader.create(env={'ORION_SHOT_RANGE': '1',
                                       'ORION_SHOT_RANGE_SAMPLES': 'nope',
                                       'PYTEST_CURRENT_TEST': 'x'},
                                  start_thread=False) is not None


# --------------------------------------------------------------------------- cost
def test_detect_thread_cost_is_under_the_budget():
    """<= 0.05 ms/frame on the detect thread: a bounds check and a reference append."""
    rdr = _reader(plate_fn=lambda: (300.0, 400.0, 1.0, 100.0))
    frame = _icon_frame()
    n = 4000
    t0 = time.perf_counter()
    for i in range(n):
        # every 4th frame opens a new press so both branches are exercised
        if i % 4 == 0:
            rdr.note_press(1000 + i, 100_000.0)
        rdr.note_frame(frame, 100.050 + (i % 4) * 0.02)
    per_ms = (time.perf_counter() - t0) * 1000.0 / n
    assert per_ms < 0.05, 'detect-thread cost %.4f ms/frame' % per_ms


# --------------------------------------------------------------- the 09-19 re-measurement
# [2026-09-19] The 09-17 excuse for the 38.5 % matrix was "player_anchor locked the owner's
# plate on 2 of 360 pickups", so the matrix had 26 joined presses. With the anchor locking
# 90-100 % of the presses whose plate is on screen, the live sampler has now written
# `range_cells` on 102 presses that also carry the game's own banner.distance_ft
# (tools/diagnostics/shot_range_confusion.py --live-records):
#
#     label \\ verdict    three   mid   unknown          three  P 0.849  R 0.898
#     three (>= 22 ft)      79     9       0              mid    P 0.000  R 0.000
#     mid                   14     0       0    -> 77.5 %
#
# MID RECALL IS ZERO and the 77.5 % is the 86:14 class imbalance of three-point drills.
# The nameplate strips were then cut at the plate the anchor found: at 17.2 / 19.7 / 20.1 /
# 21.1 ft the plate reads [3][PS][trimuzis] with the "3" cell plainly drawn, so on the
# owner's court the cell is not a behind-the-arc flag at the press. These guard the refusal
# to arm on it -- delete them only with a matrix that separates.

# the measured p50 of each population, dark and bright, straight out of that run
MEASURED_MID_P50 = (0.536, 0.338)
MEASURED_THREE_P50 = (0.493, 0.355)


def test_the_measured_mid_population_is_indistinguishable_from_three():
    """Both medians clear both floors, and the MID one clears the dark floor by more."""
    clf = ShotRangeClassifier(calibrated=True)
    for dark, bright in (MEASURED_MID_P50, MEASURED_THREE_P50):
        assert clf.sample_range(100.0, dark, bright) == RANGE_THREE, (dark, bright)
    assert MEASURED_MID_P50[0] > MEASURED_THREE_P50[0]


def test_a_measured_mid_press_would_be_called_three_if_it_were_armed():
    """The whole three-sample window, not one reading: this is the 14/14 in the matrix."""
    clf = ShotRangeClassifier(calibrated=True)
    dark, bright = MEASURED_MID_P50
    out = clf.classify([(45.0, 95.0, dark, bright), (62.0, 95.0, dark, bright),
                        (78.0, 95.0, dark, bright)])
    assert out['range'] == RANGE_THREE
    assert out['conf'] == pytest.approx(1.0)


def test_the_shipped_default_still_refuses_to_answer_on_that_population():
    """ORION_SHOT_RANGE_CALIBRATED stays 0 while mid recall is 0: the evidence is recorded
    for the next calibration, the lead is not moved."""
    clf = ShotRangeClassifier()
    dark, bright = MEASURED_MID_P50
    out = clf.classify([(45.0, 95.0, dark, bright), (62.0, 95.0, dark, bright)])
    assert out['range'] == RANGE_UNKNOWN
    assert out['evidence'] == RANGE_THREE
    assert out['reason'] == 'uncalibrated'


def test_calibrated_is_off_in_the_shipped_environment():
    assert ShotRangeClassifier.from_env({}).calibrated is False
    assert ShotRangeClassifier.from_env(
        {'ORION_SHOT_RANGE_CALIBRATED': '1'}).calibrated is True


def test_an_unreadable_cell_is_unknown_not_mid():
    """The 9 "three -> mid" calls in the 09-19 matrix are dark=0.0 with bright 0.0/0.82/1.0
    -- an ROI that missed the plate, not an icon that was absent. A reading like that must
    never become a verdict that moves the lead."""
    clf = ShotRangeClassifier(calibrated=True)
    for bright in (0.0, 0.816, 1.0):
        out = clf.classify([(45.0, 150.0, 0.0, bright), (62.0, 150.0, 0.5, 0.3)])
        assert out['range'] == RANGE_UNKNOWN, (bright, out)
        assert out['reason'] == 'split'
