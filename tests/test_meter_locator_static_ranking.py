"""Quarantine changes ranking, never the evidence needed to propose a meter."""
from types import SimpleNamespace

import pytest

from test_meter_locator_cv import court, draw_meter, clean_env, locator
from simple_meter_reader import SimpleMeterReader


def hint(box, start=9.0, end=11.0):
    x, y, w, h = box[:4]
    return (x + w / 2, y + h / 2, 48.0, 96.0, start, end)


def publish(loc, zones):
    assert hasattr(loc, "set_static_rank_zones")
    loc.set_static_rank_zones(zones)


@pytest.mark.parametrize("scope", ["full", "right"])
def test_next_equal_confidence_candidate_beats_quarantined_larger_blob(locator, scope):
    f = court(False)
    draw_meter(f, 800, 300, 65)
    draw_meter(f, 1030, 300, 35)
    scan = locator.detect_box if scope == "full" else (
        lambda frame, ts: locator.detect_box_tile(frame, "right", ts=ts))
    before = scan(f, ts=10.0)
    assert before and 790 < before[0] < 825
    publish(locator, [hint(before)])
    after = scan(f, ts=10.02)
    assert after and after[0] > 1000
    assert after[4] == before[4]


def test_only_candidate_is_never_excluded(locator):
    f = court(False)
    draw_meter(f, 800, 300, 40)
    before = locator.detect_box(f, ts=10)
    assert before
    publish(locator, [hint(before)])
    assert locator.detect_box(f, ts=10.02) == before
    for i, fill in enumerate((45, 50, 55, 60)):
        growing = court(False)
        draw_meter(growing, 800, 300, fill)
        assert locator.detect_box(growing, ts=10.04 + i / 60) is not None


def test_quarantined_roi_does_not_hide_distant_candidate(locator):
    f = court(False)
    draw_meter(f, 350, 300, 65)
    before = locator.detect_box(f, ts=10)
    assert before
    draw_meter(f, 1000, 300, 35)
    publish(locator, [hint(before)])
    after = locator.detect_box(f, ts=10.02)
    assert after and after[0] > 950
    assert locator.stats["static_roi_bypass"] == 1


@pytest.mark.parametrize("start,end", [(1, 2), (11, 12)])
def test_expired_or_future_zone_cannot_change_ranking(locator, start, end):
    f = court(False)
    draw_meter(f, 800, 300, 65)
    draw_meter(f, 1030, 300, 35)
    before = locator.detect_box(f, ts=10)
    publish(locator, [hint(before, start, end)])
    assert locator.detect_box(f, ts=10.02) == before


def test_all_quarantined_retains_original_winner(locator):
    f = court(False)
    draw_meter(f, 800, 300, 65)
    draw_meter(f, 1030, 300, 35)
    before = locator.detect_box(f, ts=10)
    second = list(before); second[0] += 230
    publish(locator, [hint(before), hint(second)])
    assert locator.detect_box(f, ts=10.02) == before


def test_clear_and_reset_remove_ranking_hints(locator):
    f = court(False)
    draw_meter(f, 800, 300, 65)
    before = locator.detect_box(f, ts=10)
    publish(locator, [hint(before)])
    publish(locator, [])
    assert locator._static_rank_zones == ()
    publish(locator, [hint(before)])
    locator.reset()
    assert locator._static_rank_zones == ()


def test_flag_off_retains_original_ranking(locator):
    f = court(False)
    draw_meter(f, 800, 300, 65)
    draw_meter(f, 1030, 300, 35)
    before = locator.detect_box(f, ts=10)
    locator.static_rank = False
    publish(locator, [hint(before)])
    assert locator.detect_box(f, ts=10.02) == before


def test_no_tip_impostor_does_not_gain_acceptance(locator):
    f = court(False)
    draw_meter(f, 800, 300, 65)
    draw_meter(f, 1030, 300, 35, tip="none")
    before = locator.detect_box(f, ts=10)
    publish(locator, [hint(before)])
    assert locator.detect_box(f, ts=10.02) == before


def test_reader_publishes_only_armed_active_quarantines():
    r = object.__new__(SimpleMeterReader)
    seen = []
    r._meter_detector = SimpleNamespace(_base=SimpleNamespace(set_static_rank_zones=seen.append))
    r._static_zone_q = True
    r._static_zone_px = 48
    r._static_zone_strikes = 2
    r._static_zone_ttl_s = 30
    r._static_zones = [dict(cx=812, cy=357, n=2, ts=9, ttl=2),
                       dict(cx=600, cy=357, n=1, ts=9, ttl=2),
                       dict(cx=400, cy=357, n=2, ts=1, ttl=2)]
    r._shot_armed_hw = True
    assert hasattr(r, "_publish_static_rank_zones")
    r._publish_static_rank_zones(10)
    assert seen[-1] == ((812.0, 357.0, 48.0, 96.0, 9.0, 11.0),)
    assert len(r._static_zones) == 3  # ranking must not mutate quarantine evidence
    r._shot_armed_hw = False
    r._publish_static_rank_zones(10)
    assert seen[-1] == ()
    r._shot_armed_hw = True
    r._static_zone_q = False
    r._publish_static_rank_zones(10)
    assert seen[-1] == ()


def test_reader_legacy_proposer_remains_compatible():
    r = object.__new__(SimpleMeterReader)
    r._meter_detector = SimpleNamespace(_base=object())
    assert hasattr(r, "_publish_static_rank_zones")
    r._publish_static_rank_zones(10)


@pytest.mark.parametrize("enabled", [False, True])
def test_reader_wires_hint_before_real_locator_submission(clean_env, monkeypatch, enabled):
    from test_reader_fresh_onset_lock import _reader
    import player_anchor as pa
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    monkeypatch.setenv("ORION_ANCHORED_SEARCH", "0")
    pa.ARM.reset()
    pa.ANCHOR.reset(keep_identity=False)
    r = _reader()
    try:
        f = court(False)
        draw_meter(f, 800, 300, 65)
        draw_meter(f, 1030, 300, 35)
        r._shot_armed_hw = True
        r._meter_detector._base.static_rank = enabled
        r._static_zones = [dict(cx=813, cy=357, n=r._static_zone_strikes, ts=99, ttl=30)]
        sample = r.detect(f, ts=100)
        found, box, conf, ts = r._meter_detector.latest()
        assert found and (box[0] > 1000) is enabled
        assert bool(sample.detected) is enabled
        assert r._static_zones  # winning another candidate did not erase the old evidence
    finally:
        r._meter_detector.stop()
        pa.ARM.reset()
        pa.ANCHOR.reset(keep_identity=False)


def test_bad_hint_is_discarded_and_snapshot_is_immutable(locator):
    zone = [812, 357, 48, 96, 9, 11]
    publish(locator, [zone])
    zone[0] = 400
    assert locator._static_rank_zones[0][0] == 812
    publish(locator, [(float("nan"), 1, 2, 3, 4, 5)])
    assert locator._static_rank_zones == ()


def test_real_confidence_still_outranks_unquarantined_fallback(locator):
    f = court(False)
    draw_meter(f, 800, 300, 65)
    draw_meter(f, 1030, 300, 92, tip="none")
    before = locator.detect_box(f, ts=10)
    assert before and 790 < before[0] < 825 and before[4] == pytest.approx(.9)
    publish(locator, [hint(before)])
    for i in range(3):
        assert locator.detect_box(f, ts=10.02 + i / 60) == before
