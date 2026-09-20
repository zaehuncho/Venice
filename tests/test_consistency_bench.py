"""Production-profile and physical-epoch contracts for consistency_bench."""

from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "consistency_bench", ROOT / "tools" / "quality" / "consistency_bench.py"
)
bench = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(bench)


def _events():
    return {
        "press": [{"t": 1001.0, "epoch": 7, "intent": "square_edge"}],
        "abort": [],
        "presstip": [],
        "landing": [],
        "rel_timing": [{"t": 1001.8, "seq": 1, "shot_type": "Standstill"}],
        "not_owned": [],
        "cap_health": [],
    }


def test_replay_profile_is_the_shipped_armed_detector_profile(tmp_path):
    model = tmp_path / "detector.onnx"
    model.write_bytes(b"model-a")
    profile = bench.resolve_replay_profile(model)

    assert profile["ORION_METER_MODEL"] == str(model.resolve())
    for key in (
        "ORION_READER_ANCHOR",
        "ORION_READER_PCTL_FILL",
        "ORION_READER_TRACK_H_CAP",
        "ORION_METER_SUBPIXEL_SESSION_RULER",
        "ORION_METER_SUBPIXEL_SESSION_PROVISIONAL",
        "ORION_METER_SUBPIXEL_BASE_HOLD",
        "ORION_METER_DETECTOR",
        "ORION_LATENCY_REGIME_REOPEN_SOFT",
        "ORION_METER_PARTIAL_OCCLUSION",
    ):
        assert profile[key] == "1"
    assert profile["ORION_METER_PARTIAL_OCCLUSION_MAX_MS"] == "120"
    assert profile["ORION_METER_NEGATIVE_BRIDGE_MAX_MS"] == "45"
    assert profile["ORION_METER_PARTIAL_OCCLUSION_MIN_DIRECT"] == "3"
    assert profile["ORION_METER_PARTIAL_OCCLUSION_MIN_COLS"] == "2"
    assert profile["ORION_METER_DETECTOR_CONF"] == "0.35"
    assert profile["ORION_METER_PROVIDER_PRIORITY"] == "dml,cpu"
    assert profile["ORION_TIMING_PROFILE_ID"] == "2k27-2026-09-04-v2"
    # Mode 2 continuously prioritizes inference and was the old unrealistic
    # benchmark. Production uses hardware-armed mode 1.
    assert profile["ORION_METER_DETECTOR_SYNC_ACQUIRE"] == "1"
    header = (
        ROOT / "native_orion" / "src" / "SidecarReaderProfile.h"
    ).read_text(encoding="utf-8")
    for key in bench._REPLAY_ENV:
        if key in (
            "ORION_READER_OCCLUSION",
            "ORION_READER_ROBUST",
            "ORION_READER_SCALE_ADAPT",
            "ORION_READER_TIP_ENFORCE",
            "ORION_READER_ARMED_HOLD",
            "ORION_COLOR_CAL",
            "ORION_GREEN_ZONE_WINDOW",
            "ORION_GREEN_SELF_GRADE",
        ):
            continue
        assert f'"{key}"' in header


def test_reader_config_resolves_live_settings_with_bounded_confidence(tmp_path):
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "meter_style": "Pill",
                "meter_color": "White",
                "detection_confidence_percent": 140,
            }
        ),
        encoding="utf-8",
    )
    cfg = bench.resolve_reader_config(root=str(tmp_path))
    assert cfg == {
        "meter_style": "Pill",
        "meter_color": "White",
        "confidence_threshold": 1.0,
    }


def test_build_runs_ends_hardware_arm_at_recorded_release():
    generation = {"anchor": 1000.0, "t_max_ms": 5000.0}
    runs = bench.build_runs(generation, _events())

    assert len(runs) == 1
    assert runs[0]["press_ms"] == 1000.0
    assert runs[0]["terminal_ms"] == pytest.approx(1800.0)
    assert runs[0]["arm_end_ms"] == pytest.approx(1800.0)
    assert runs[0]["fired"] is True
    assert runs[0]["release_seq"] == 1


def test_replay_cache_identity_binds_model_and_physical_epochs(tmp_path):
    model = tmp_path / "detector.onnx"
    model.write_bytes(b"model-a")
    profile = bench.resolve_replay_profile(model)
    cfg = {"meter_style": "Arrow2", "meter_color": "White", "confidence_threshold": 0.5}
    runs = {0: bench.build_runs({"anchor": 1000.0, "t_max_ms": 5000.0}, _events())}

    identity_a = bench.replay_profile_id(profile, cfg, runs)
    cache = tmp_path / "replay.csv"
    with cache.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["profile_id", "det"])
        writer.writeheader()
        writer.writerow({"profile_id": identity_a, "det": "1"})
    assert bench.replay_cache_matches(cache, identity_a)

    model.write_bytes(b"model-b")
    identity_b = bench.replay_profile_id(profile, cfg, runs)
    assert identity_b != identity_a
    assert not bench.replay_cache_matches(cache, identity_b)


def test_not_owned_terminal_is_not_misreported_as_silent(tmp_path):
    log = tmp_path / "orion.log"
    log.write_text(
        "\n".join(
            [
                "1970-01-01T00:16:41.000Z Physical shot epoch: "
                "epoch=9 intent=square_edge route=RawInput",
                "1970-01-01T00:16:41.250Z SHOT NOT OWNED: "
                "reason=press_unanswered_no_meter physical_epoch=9 "
                "shot_type=Left_Fade",
            ]
        ),
        encoding="utf-8",
    )
    events = bench.parse_log([str(log)])
    runs = bench.build_runs({"anchor": 1000.0, "t_max_ms": 5000.0}, events)

    assert len(runs) == 1
    assert runs[0]["silent"] is False
    assert runs[0]["aborts"] == ["not_owned:press_unanswered_no_meter"]
    assert runs[0]["arm_end_ms"] == pytest.approx(1250.0)


def _six_press_events(start=1001.0):
    events = {key: [] for key in _events()}
    events["press"] = [
        {"t": start + 10 * i, "epoch": i + 1, "intent": "square_edge"}
        for i in range(6)
    ]
    return events


def test_log_drift_counts_not_owned_instead_of_silent():
    events = _six_press_events()
    events["not_owned"] = [
        {"t": p["t"] + 0.25, "epoch": p["epoch"],
         "reason": "press_unanswered_no_meter", "shot_type": "Standstill"}
        for p in events["press"]
    ]
    report = bench.log_outcome_drift(events)
    assert "abort 1.00->1.00" in report
    assert "silent 0.00->0.00" in report
    assert "NOT human-banner" in report


def test_log_drift_counts_ungraded_releases_instead_of_silent():
    events = _six_press_events()
    events["rel_timing"] = [
        {"t": p["t"] + 0.8, "seq": p["epoch"], "shot_type": "Standstill"}
        for p in events["press"]
    ]
    report = bench.log_outcome_drift(events)
    assert "fired_ungraded 1.00->1.00" in report
    assert "silent 0.00->0.00" in report


def test_log_drift_splits_epoch_reset_without_a_ten_minute_gap():
    events = _six_press_events()
    events["press"] += _six_press_events(1061.0)["press"]
    report = bench.log_outcome_drift(events)
    assert report.count("n=6 presses:") == 2


def test_log_drift_deduplicates_both_abort_families_per_press():
    events = _six_press_events()
    for p in events["press"]:
        event = {"t": p["t"] + 0.25, "epoch": p["epoch"],
                 "reason": "live_tip_deadline_missed", "shot_type": "Standstill"}
        events["abort"].append(event)
        events["not_owned"].append(dict(event))
    report = bench.log_outcome_drift(events)
    assert "n=6 presses: abort 1.00->1.00" in report


@pytest.mark.parametrize("family", ["abort", "not_owned", "presstip"])
def test_build_runs_never_borrows_an_event_before_the_press(family):
    events = _events()
    events["rel_timing"] = []
    events[family] = [{"t": 1000.5, "epoch": 7, "seq": 1,
                       "reason": "stale", "shot_type": "Right_Fade"}]
    run = bench.build_runs({"anchor": 1000.0, "t_max_ms": 5000.0}, events)[0]
    assert run["silent"] is True
    assert run["aborts"] == []
    assert run["shot_type"] is None


@pytest.mark.parametrize("peak,start,end", [
    (98.0, 97.0, None), (float("nan"), 97.0, 99.0),
    (98.0, 99.0, 97.0), (98.0, 97.0, float("inf")),
])
def test_invalid_landing_measurements_stay_ungraded(peak, start, end):
    landing = {"peak_fill": peak, "green_start": start, "green_end": end}
    assert bench.grade_landing(landing) is None
    assert bench.outcome_class({"landing": landing}) == "fired_ungraded"


def test_release_uses_authoritative_epoch_instead_of_a_nearer_press():
    events = _events()
    events["press"].append({"t": 1001.2, "epoch": 8, "intent": "square_edge"})
    events["presstip"] = [{"t": 1002.0, "epoch": 7, "seq": 1, "shot_type": "Standstill"}]
    events["landing"] = [{"t": 1002.0, "seq": 1, "peak_fill": 98.0,
                          "green_start": 97.0, "green_end": 99.0, "shot_type": "Standstill"}]
    first, second = bench.build_runs({"anchor": 1000.0, "t_max_ms": 5000.0}, events)
    assert first["fired"] is True
    assert first["release_seq"] == 1
    assert first["terminal_ms"] == pytest.approx(1800.0)
    assert second["silent"] is True
    assert second["release_seq"] is None
    assert second["fired"] is False


@pytest.mark.parametrize("landing_time", [1000.5, 1001.5])
def test_landing_never_precedes_its_press_or_release(landing_time):
    events = _events()
    events["presstip"] = [{"t": 1002.0, "epoch": 7, "seq": 1, "shot_type": "Standstill"}]
    events["landing"] = [{"t": landing_time, "seq": 1, "peak_fill": 98.0,
                          "green_start": 97.0, "green_end": 99.0, "shot_type": "Standstill"}]
    run = bench.build_runs({"anchor": 1000.0, "t_max_ms": 5000.0}, events)[0]
    assert run["landing"] is None
    assert bench.outcome_class(run) == "fired_ungraded"


def test_contradictory_pre_press_release_cannot_authorize_a_landing():
    events = _events()
    events["rel_timing"][0]["t"] = 1000.5
    events["presstip"] = [{"t": 1002.0, "epoch": 7, "seq": 1, "shot_type": "Standstill"}]
    events["landing"] = [{"t": 1002.0, "seq": 1, "peak_fill": 98.0,
                          "green_start": 97.0, "green_end": 99.0, "shot_type": "Standstill"}]
    run = bench.build_runs({"anchor": 1000.0, "t_max_ms": 5000.0}, events)[0]
    assert run["landing"] is None
    assert run["fired"] is False


def test_duplicate_overlapping_log_records_do_not_erase_sessions():
    events = _six_press_events()
    original = bench.log_outcome_drift(events)
    events["press"] += [dict(p) for p in events["press"]]
    assert bench.log_outcome_drift(events) == original
    assert "n=6 presses:" in original
    runs = bench.build_runs({"anchor": 1000.0, "t_max_ms": 60000.0}, events)
    assert len(runs) == 6
