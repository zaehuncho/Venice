"""Identity/causality contracts for offline timing attribution (not banner grades)."""
from tools.timing.timing_motion_audit import parse_log, motion_features, regression


def line(ms, body):
    return f"2026-09-08T12:00:{ms // 1000:02d}.{ms % 1000:03d}Z  {body}"


def shot(epoch=1, seq=1, offset=0):
    return [line(offset, f"Physical shot epoch: epoch={epoch} intent=square_edge"),
            line(offset+200, f"Release delivery identity: physical_epoch={epoch} shot_attempt=1 release_seq={seq}"),
            line(offset+1200, f"Release landing: seq={seq} graded=1 peak_fill=96 green_start=94 green_end=98 shot=Standstill")]


def test_resets_do_not_reuse_draw_or_release_identity():
    data = shot()
    data.insert(1, line(50, "DEV FIRE OFFSET DRAW: shot_attempt=1 offset_ms=4"))
    data.insert(2, line(60, "DEV FIRE OFFSET APPLIED: shot_attempt=1 applied_ms=4"))
    rows, _ = parse_log(data + shot(offset=3000))
    assert len(rows) == 2
    assert rows[0]["offset_draw_ms"] == 4
    assert rows[0]["offset_applied_ms"] == 4
    assert rows[1]["offset_draw_ms"] is None
    assert rows[0]["session"] != rows[1]["session"]


def test_abort_families_count_raw_and_deduplicate_by_press():
    rows, stats = parse_log([line(0, "Physical shot epoch: epoch=1 intent=square_edge"),
        line(200, "SHOT NOT OWNED: physical_epoch=1 reason=detector_authority_lost"),
        line(201, "Shot abort identity: physical_epoch=1 reason=detector_authority_lost")])
    assert stats["abort_lines"] == 2
    assert rows[0]["abort_reasons"] == ["detector_authority_lost"]
    assert rows[0]["peak_fill"] is None


def test_landing_after_next_press_uses_release_identity_not_nearest_press():
    data = shot()
    data.insert(2, line(500, "Physical shot epoch: epoch=2 intent=square_edge"))
    rows, _ = parse_log(data)
    assert rows[0]["peak_fill"] == 96
    assert rows[1]["peak_fill"] is None


def test_noncausal_landing_and_duplicate_echo_are_rejected():
    data = shot()
    rows, stats = parse_log([data[2].replace("01.200", "00.100")] + data + [data[-1]])
    assert rows[0]["peak_fill"] == 96
    assert stats["unjoined_landings"] == 2


def frame(t, fill, x, w=20, gen=1):
    return {"wall_ms": t, "fill_pct": fill, "x": x, "y": 50, "w": w, "h": 100,
            "detected": "1", "fed": "1", "fill_estimator_generation": str(gen)}


def test_motion_uses_only_anchor_to_fire_and_reports_estimator_change():
    fs = [frame(0, 10, 0), frame(20, 20, 2), frame(40, 30, 4, gen=2),
          frame(60, 40, 6, gen=2), frame(80, 50, 900)]
    v = motion_features(fs, 60)
    assert v["motion_n"] == 3
    assert v["center_speed_px_s"] == 100
    assert v["estimator_changes"] == 1
    assert v["width_change_pct"] == 0


def test_anchor_not_extrapolated_from_late_acquisition_or_long_gap():
    assert motion_features([frame(0, 25, 0), frame(20, 30, 2)], 20)["motion_n"] == 0
    assert motion_features([frame(0, 10, 0), frame(200, 30, 2)], 200)["motion_n"] == 0


def test_regression_never_interprets_missing_or_constant_offsets_as_zero_slope():
    assert regression([(0, x) for x in range(20)])["slope"] is None
    assert regression([(None, 93), (float("nan"), 95)])["n"] == 0
    r = regression([(x, 94 + .22*x) for x in range(-8, 9)])
    assert abs(r["slope"] - .22) < 1e-10
    assert abs(r["r"] - 1) < 1e-10
