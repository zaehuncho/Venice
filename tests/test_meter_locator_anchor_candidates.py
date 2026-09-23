"""Anchor padding supplies context, not candidates that can hide the owner meter.

These are controlled pixel fixtures with a supplied anchor. They measure candidate
selection, not nameplate acquisition accuracy or live shot timing.
"""
import os

import pytest

from meter_locator_cv import MeterContourLocator
from player_anchor import Anchor
from test_meter_locator_cv import court, draw_meter


@pytest.fixture
def anchored(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("ORION_CV_", "ORION_METER_", "ORION_ANCHOR", "ORION_PLAYER_")):
            monkeypatch.delenv(key, raising=False)

    def make(x=800, confidence=.9, scope="full"):
        loc = MeterContourLocator()
        anchor = Anchor(x - 17, 561, 1, confidence, .9, .9, 4, 10,
                        -30, 150, 85, 70, 130, 1280, 720)
        monkeypatch.setattr(loc, "_update_anchor", lambda *_: anchor)
        monkeypatch.setattr(loc, "_press_state", lambda _: (True, 200, "Standstill"))
        scan = loc.detect_box if scope == "full" else (
            lambda frame, ts: loc.detect_box_tile(frame, scope, ts=ts))
        return loc, anchor, scan

    return make


@pytest.mark.parametrize("confidence", [.35, .9])
@pytest.mark.parametrize("x,scope", [(300, "full"), (300, "left"), (800, "right")])
@pytest.mark.parametrize("pad_side", [-1, 1])
def test_padding_candidate_cannot_hide_valid_in_patch_meter(anchored, confidence, x, scope, pad_side):
    loc, anchor, scan = anchored(x, confidence, scope)
    frame = court(False)
    draw_meter(frame, x, 300, 35)
    outside_x = x + pad_side * 105
    draw_meter(frame, outside_x, 300, 65)
    assert anchor.contains_box(x, 304, 26, 107)
    assert not anchor.contains_box(outside_x, 304, 26, 107)
    result = scan(frame, ts=10)
    assert result is not None and abs(result[0] - x) <= 1
    assert result[4] == pytest.approx(.9)
    assert loc.stats["anchor_patch_hit"] == 1
    assert loc.stats["full"] == 1, "the valid patch needs no second full-band scan"


@pytest.mark.parametrize("confidence,accepted", [(.35, True), (.9, False)])
def test_sole_outside_candidate_keeps_existing_confidence_policy(anchored, confidence, accepted):
    _, _, scan = anchored(confidence=confidence)
    frame = court(False)
    draw_meter(frame, 905, 300, 65)
    result = scan(frame, ts=10)
    assert (result is not None) is accepted
    if result:
        assert abs(result[0] - 905) <= 1


def test_padding_neighbour_remains_visible_to_lone_column_gate(anchored):
    loc, anchor, scan = anchored()
    frame = court(False)
    # The first column is barely inside the anchor; its twin sits outside it,
    # but inside the padded pixel crop. Removing padding would accept a digit.
    x = 881
    draw_meter(frame, x, 300, 40)
    frame[368:408, x + 31:x + 42] = 255
    assert anchor.contains_box(x, 304, 26, 107)
    assert not anchor.contains_box(x + 24, 304, 26, 107)
    assert scan(frame, ts=10) is None
    assert loc.stats["not_lone"] > 0


def test_quarantine_ranking_still_selects_clean_in_patch_candidate(anchored):
    loc, _, scan = anchored()
    frame = court(False)
    draw_meter(frame, 745, 300, 65)
    draw_meter(frame, 840, 300, 35)
    loc.set_static_rank_zones(((758, 357, 20, 96, 9, 11),))
    result = scan(frame, ts=10)
    assert result is not None and abs(result[0] - 840) <= 1


def test_no_eligible_in_patch_candidate_never_mints_a_result(anchored):
    loc, _, scan = anchored()
    frame = court(False)
    draw_meter(frame, 800, 300, 35, tip="none")
    draw_meter(frame, 905, 300, 65)
    loc.tipless_armed = False  # exercise green-tip path independently
    assert scan(frame, ts=10) is None
    assert loc.stats["no_tip"] > 0


@pytest.mark.parametrize("confidence,expected", [(.35, 1000), (.9, 300)])
def test_advisory_anchor_cannot_trap_search_on_quarantined_candidate(anchored, confidence, expected):
    loc, _, scan = anchored(x=300, confidence=confidence)
    frame = court(False)
    draw_meter(frame, 300, 300, 65)
    draw_meter(frame, 1000, 300, 35)
    loc.set_static_rank_zones(((313, 357, 48, 96, 9, 11),))
    result = scan(frame, ts=10)
    assert result is not None and abs(result[0] - expected) <= 1
    assert result[4] == pytest.approx(.9)
    assert loc.stats.get("static_anchor_bypass", 0) == (1 if confidence < .75 else 0)
    assert loc.stats["full"] == (2 if confidence < .75 else 1)


@pytest.mark.parametrize("alternative", ["none", "lower_confidence", "also_quarantined"])
def test_advisory_anchor_quarantine_fallback_preserves_original_when_no_better_evidence(
        anchored, alternative):
    loc, _, scan = anchored(x=300, confidence=.35)
    frame = court(False)
    draw_meter(frame, 300, 300, 65)
    zones = [(313, 357, 48, 96, 9, 11)]
    if alternative == "lower_confidence":
        draw_meter(frame, 1000, 300, 92, tip="none")
    if alternative == "also_quarantined":
        draw_meter(frame, 1000, 300, 85)
        zones.append((1013, 357, 48, 96, 9, 11))
    loc.set_static_rank_zones(zones)
    for i in range(3):
        result = scan(frame, ts=10 + i / 60)
        assert result is not None and abs(result[0] - 300) <= 1
        assert loc._find_col[0] == 65, "losing band candidate cannot replace patch metadata"


@pytest.mark.parametrize("zones", [[], [(313, 357, 48, 96, 1, 2)]])
def test_without_active_quarantine_advisory_anchor_keeps_patch_priority(anchored, zones):
    loc, _, scan = anchored(x=300, confidence=.35)
    frame = court(False)
    draw_meter(frame, 300, 300, 35)
    draw_meter(frame, 1000, 300, 65)
    loc.set_static_rank_zones(zones)
    result = scan(frame, ts=10)
    assert result is not None and abs(result[0] - 300) <= 1
    assert loc.stats["full"] == 1


@pytest.mark.parametrize("clear", ["expiry", "reset", "explicit"])
def test_quarantine_fallback_does_not_survive_its_hint_lifecycle(anchored, clear):
    loc, _, scan = anchored(x=300, confidence=.35)
    frame = court(False)
    draw_meter(frame, 300, 300, 65)
    draw_meter(frame, 1000, 300, 35)
    loc.set_static_rank_zones(((313, 357, 48, 96, 9, 11),))
    assert scan(frame, ts=10)[0] == 1000
    if clear == "reset":
        loc.reset()
    if clear == "explicit":
        loc.set_static_rank_zones(())
    result = scan(frame, ts=12 if clear == "expiry" else 10.02)
    assert result is not None and result[0] == 300


def test_advisory_escape_tracks_clean_alternative_without_repeated_band_scans(anchored):
    loc, _, scan = anchored(x=300, confidence=.35)
    loc.set_static_rank_zones(((313, 357, 48, 96, 9, 11),))
    for i in range(5):
        frame = court(False)
        draw_meter(frame, 300, 300, 65)
        draw_meter(frame, 1000 + i * 2, 300, 35 + i)
        result = scan(frame, ts=10 + i / 60)
        assert result is not None and abs(result[0] - (1000 + i * 2)) <= 1
        assert loc.stats["full"] == 2, "only the first escape needs the band"
    assert loc.stats["static_anchor_bypass"] == 5


def test_advisory_escape_reopens_band_when_tracked_alternative_leaves_roi(anchored):
    loc, _, scan = anchored(x=300, confidence=.35)
    loc.set_static_rank_zones(((313, 357, 48, 96, 9, 11),))
    frame = court(False)
    draw_meter(frame, 300, 300, 65)
    draw_meter(frame, 1000, 300, 35)
    assert scan(frame, ts=10)[0] == 1000
    frame = court(False)
    draw_meter(frame, 300, 300, 65)
    draw_meter(frame, 800, 300, 40)
    result = scan(frame, ts=10.02)
    assert result is not None and result[0] == 800
    assert loc.stats["full"] == 3
