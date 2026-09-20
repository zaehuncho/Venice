"""shot_records.py -- one labelled training record per shot, assembled live.

These tests drive the recorder with SYNTHETIC events in the order the sidecar produces
them (press -> pickup -> release -> oracle -> banner) and assert the join, the tempo
thresholds, the unanswered-press outcome, the append-not-truncate contract and the cost
budget on the control thread.
"""

import json
import os
import time

import pytest

import shot_records
from shot_records import ShotRecorder


# --------------------------------------------------------------------------- helpers
class _Clock:
    """Deterministic ms clock so deadline behaviour is testable without sleeping."""

    def __init__(self, t=1_000_000.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, ms):
        self.t += float(ms)
        return self.t


def _recorder(tmp_path, clock=None, **kw):
    clock = clock or _Clock()
    rec = ShotRecorder(str(tmp_path / "session_test.jsonl"), session="session_test",
                       start_thread=False, clock=clock, **kw)
    return rec, clock


def _lines(rec):
    if not os.path.exists(rec.path):
        return []
    with open(rec.path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


PICKUP = {"epoch": 77, "first_sight_fill": 12.5, "first_sight_ms_after_press": 468.0,
          "anchor_used": 1, "anchor_conf": 0.71, "refused_outside_patch": 0,
          "expect_accept": 1, "anchor_ms": 0.31, "patch_hits": 12, "logged": 1}

ORACLE = {"seq": 77, "gap_px": 2.1, "gap_pct": 1.8, "settled_fill": 90.1,
          "green_bottom_pct": 91.4, "verdict_proxy": "green", "n": 14,
          "end_reason": "window"}

BANNER = {"event": "banner_verdict", "timing": "EXCELLENT", "timing_color": "green",
          "green": True, "coverage": "OPEN", "ncc": 0.981, "seq": 5, "attributed": 1,
          "release_seq": 77, "release_delay_ms": 1210.0, "onset_ms": 42.0,
          "emit_latency_ms": 115.0}


# --------------------------------------------------------------------------- tempo
@pytest.mark.parametrize("shot_type,onset_engine_ms,expected", [
    ("Standstill", 0.0, "quick"),
    ("Standstill", 499.9, "quick"),
    ("Standstill", 500.0, "normal"),      # the cut point itself is NORMAL (< is quick)
    ("Standstill", 580.0, "normal"),      # and the upper cut point too (> is slow)
    ("Standstill", 580.1, "slow"),
    ("", 499.0, "quick"),                 # empty type buckets as Standstill
    ("Left Fade", 774.9, "quick"),
    ("Left Fade", 775.0, "normal"),
    ("Left Fade", 915.0, "normal"),
    ("Right Fade", 915.1, "slow"),
    ("Go-To", 100.0, "normal"),           # "Other" is never sub-bucketed
    ("Go-To", 5000.0, "normal"),
    ("Standstill", -1.0, "normal"),       # no onset is not a tempo
])
def test_tempo_thresholds_match_the_engine(shot_type, onset_engine_ms, expected):
    assert shot_records.tempo_for(shot_type, onset_engine_ms) == expected


def test_tempo_uses_the_engine_frame_not_the_capture_frame(tmp_path):
    """The engine's onset is the sidecar's first sight + the 36 ms accept lag.

    470 ms of capture-clock onset is 506 ms to the engine, which is NORMAL, not QUICK --
    getting this backwards files the record in a bucket the engine's trim never reads.
    """
    rec, _ = _recorder(tmp_path)
    rec.note_press(77, shot_type="Standstill")
    rec.note_pickup(77, dict(PICKUP, first_sight_ms_after_press=470.0))
    rec.note_release(77, release_ms=0.0)
    rec.close_all("t")
    rec.drain()
    row = _lines(rec)[0]
    assert row["onset_ms"] == 470.0
    assert row["onset_lag_ms"] == 36.0
    assert row["onset_engine_ms"] == 506.0
    assert row["tempo"] == "normal"
    assert row["tempo_key"] == "Standstill/normal"


def test_onset_lag_is_configurable_and_recorded(tmp_path):
    rec, _ = _recorder(tmp_path, onset_lag_ms=0.0)
    rec.note_press(77, shot_type="Standstill")
    rec.note_pickup(77, dict(PICKUP, first_sight_ms_after_press=470.0))
    rec.close_all("t")
    rec.drain()
    row = _lines(rec)[0]
    assert row["onset_lag_ms"] == 0.0 and row["onset_engine_ms"] == 470.0
    assert row["tempo"] == "quick"


# --------------------------------------------------------------------------- assembly
def test_full_synthetic_shot_yields_one_complete_record(tmp_path):
    rec, clock = _recorder(tmp_path)
    press_ms = clock.t
    rec.note_press(77, ts_ms=press_ms, mono_ms=5.0, source="square",
                   shot_type="Standstill", rhythm=False)
    rec.note_pickup(77, PICKUP)
    clock.advance(651.0)
    rec.note_release(77, release_ms=press_ms + 651.0)
    clock.advance(500.0)
    rec.note_oracle(77, ORACLE)
    clock.advance(700.0)
    rec.note_banner(BANNER)
    # release + oracle + banner => the record is COMPLETE and closes without waiting out
    # the grace period.
    assert rec.snapshot()["open"] == 0
    rec.drain()

    rows = _lines(rec)
    assert len(rows) == 1
    row = rows[0]
    assert row["schema"] == shot_records.SCHEMA
    assert row["epoch"] == 77 and row["seq"] == 1
    assert row["source"] == "square" and row["shot_type"] == "Standstill"
    assert row["rhythm"] == 0
    assert row["outcome"] == "released" and row["closed_reason"] == "complete"
    assert row["press_ts_ms"] == press_ms
    assert row["release_after_press_ms"] == 651.0
    assert row["pickup"]["first_sight_fill"] == 12.5
    assert row["pickup"]["patch_hits"] == 12
    assert row["onset_ms"] == 468.0 and row["onset_source"] == "pickup"
    assert row["onset_engine_ms"] == 504.0 and row["tempo"] == "normal"
    assert row["oracle"]["gap_px"] == 2.1
    assert row["oracle"]["verdict_proxy"] == "green"
    assert row["oracle"]["settled_fill"] == 90.1
    assert row["banner"]["timing"] == "EXCELLENT" and row["banner"]["green"] == 1
    assert row["banner"]["release_delay_ms"] == 1210.0
    # reserved, but PINNED now so today's corpus stays joinable with tomorrow's
    assert row["pose_track"] is None
    assert row["pose_track_schema"] == "coco17/[ts_ms,x,y,conf]"
    assert row["pose_keypoints"] == 17
    assert row["icon_off_ms"] is None


def test_press_with_no_release_is_unanswered(tmp_path):
    rec, clock = _recorder(tmp_path, timeout_ms=8000.0)
    rec.note_press(91, shot_type="Standstill", source="square")
    rec.note_pickup(91, dict(PICKUP, epoch=91, first_sight_ms_after_press=-1.0,
                             first_sight_fill=-1.0, anchor_used=0))
    clock.advance(7999.0)
    rec.tick()
    assert rec.snapshot()["open"] == 1        # not yet
    clock.advance(2.0)
    rec.tick()
    rec.drain()
    row = _lines(rec)[0]
    assert row["outcome"] == "unanswered"
    assert row["closed_reason"] == "timeout"
    assert row["release_ms"] is None and row["release_after_press_ms"] is None
    # a press that never picked a meter up still records that fact
    assert row["onset_ms"] is None and row["tempo"] == "normal"
    assert row["banner"] is None and row["oracle"] is None


def test_disarm_records_the_reason(tmp_path):
    rec, clock = _recorder(tmp_path)
    rec.note_press(12, shot_type="Left Fade")
    rec.note_disarm(12, "manual_cancel")
    clock.advance(1001.0)
    rec.tick()
    rec.drain()
    row = _lines(rec)[0]
    assert row["outcome"] == "disarmed" and row["disarm_reason"] == "manual_cancel"


def test_released_press_waits_out_the_grace_for_late_instruments(tmp_path):
    """The banner lands 1.0-1.7 s after the release; the record must still be open."""
    rec, clock = _recorder(tmp_path, grace_ms=3500.0)
    rec.note_press(5, shot_type="Standstill")
    rec.note_release(5, release_ms=0.0)
    clock.advance(3499.0)
    rec.tick()
    assert rec.snapshot()["open"] == 1
    assert rec.note_banner(dict(BANNER, release_seq=5)) is True
    clock.advance(2.0)
    rec.tick()
    rec.drain()
    row = _lines(rec)[0]
    assert row["banner"]["timing"] == "EXCELLENT"
    assert row["closed_reason"] == "grace"


def test_type_upgrade_keeps_one_record_and_rebuckets(tmp_path):
    """The native re-sends the SAME epoch with source=type_upgrade on its 200 ms grace."""
    rec, clock = _recorder(tmp_path)
    rec.note_press(31, ts_ms=1000.0, source="square", shot_type="Standstill")
    rec.note_pickup(31, dict(PICKUP, epoch=31, first_sight_ms_after_press=800.0))
    assert rec.snapshot()["presses"] == 1
    rec.note_press(31, ts_ms=9999.0, source="type_upgrade", shot_type="Left Fade")
    assert rec.snapshot()["presses"] == 1
    rec.close_all("t")
    rec.drain()
    rows = _lines(rec)
    assert len(rows) == 1
    assert rows[0]["press_ts_ms"] == 1000.0            # identity is NOT reset
    assert rows[0]["shot_type"] == "Standstill"
    assert rows[0]["shot_type_upgraded"] == "Left Fade"
    # 800 + 36 = 836 -> a fade at 836 ms is NORMAL; the same onset on a Standstill is SLOW
    assert rows[0]["tempo"] == "normal"
    assert rows[0]["tempo_key"] == "Left Fade/normal"


def test_unattributed_banner_and_orphans_never_join(tmp_path):
    rec, _ = _recorder(tmp_path)
    rec.note_press(4, shot_type="Standstill")
    assert rec.note_banner(dict(BANNER, release_seq=4, attributed=0)) is False
    assert rec.note_banner(dict(BANNER, release_seq=999)) is False
    assert rec.note_oracle(999, ORACLE) is False
    snap = rec.snapshot()
    assert snap["orphan_banner"] == 1 and snap["orphan_oracle"] == 1
    rec.close_all("t")
    rec.drain()
    assert _lines(rec)[0]["banner"] is None


def test_pickup_beats_the_detect_loop_fallback(tmp_path):
    rec, _ = _recorder(tmp_path)
    rec.note_press(8, shot_type="Standstill")
    assert rec.note_onset(8, 610.0, fill=9.0) is True
    rec.note_pickup(8, dict(PICKUP, epoch=8, first_sight_ms_after_press=468.0))
    # the pickup record is authoritative; a later fallback cannot overwrite it
    assert rec.note_onset(8, 999.0) is False
    rec.close_all("t")
    rec.drain()
    row = _lines(rec)[0]
    assert row["onset_ms"] == 468.0 and row["onset_source"] == "pickup"


def test_detect_loop_onset_is_used_when_there_is_no_pickup_record(tmp_path):
    rec, _ = _recorder(tmp_path)
    rec.note_press(8, shot_type="Standstill")
    rec.note_onset(8, 610.0, fill=9.0)
    rec.close_all("t")
    rec.drain()
    row = _lines(rec)[0]
    assert row["onset_ms"] == 610.0 and row["onset_source"] == "detect_loop"
    assert row["tempo"] == "slow"          # 610 + 36 = 646 > 580
    assert row["pickup"] is None


def test_icon_samples_are_recorded_for_offline_derivation(tmp_path):
    rec, _ = _recorder(tmp_path)
    rec.note_press(3, shot_type="Standstill")
    for i in range(5):
        rec.note_icon_sample(3, 100.0 * i, 30.0 if i < 3 else 150.0, 0.8 if i < 3 else 0.05)
    rec.close_all("t")
    rec.drain()
    row = _lines(rec)[0]
    assert row["icon_source"] == "nameplate_cell_10hz"
    assert len(row["icon_cell"]) == 5
    assert row["icon_cell"][0] == [0.0, 30.0, 0.8]
    assert row["icon_off_ms"] is None      # derived offline, never guessed live


def test_open_presses_are_bounded(tmp_path):
    rec, _ = _recorder(tmp_path, max_open=3)
    for ep in range(1, 8):
        rec.note_press(ep, shot_type="Standstill")
    assert rec.snapshot()["open"] == 3
    rec.drain()
    rows = _lines(rec)
    assert [r["epoch"] for r in rows] == [1, 2, 3, 4]
    assert all(r["closed_reason"] == "superseded" for r in rows)


# --------------------------------------------------------------------------- file
def test_jsonl_is_appended_never_overwritten(tmp_path):
    path = tmp_path / "session_test.jsonl"
    path.write_text('{"schema":"pre-existing"}\n', encoding="utf-8")
    rec, _ = _recorder(tmp_path)
    rec.note_press(1, shot_type="Standstill")
    rec.close_all("t")
    rec.drain()
    # a second recorder generation on the same path (a mid-session sidecar restart)
    rec2, _ = _recorder(tmp_path)
    rec2.note_press(2, shot_type="Standstill")
    rec2.close_all("t")
    rec2.drain()
    rows = _lines(rec)
    assert len(rows) == 3
    assert rows[0]["schema"] == "pre-existing"
    assert [r["epoch"] for r in rows[1:]] == [1, 2]


def test_every_record_is_one_valid_json_line(tmp_path):
    rec, _ = _recorder(tmp_path)
    for ep in (11, 12, 13):
        rec.note_press(ep, shot_type="Standstill")
        rec.note_release(ep, release_ms=0.0)
    rec.close_all("t")
    rec.drain()
    with open(rec.path, encoding="utf-8") as fh:
        raw = fh.read()
    assert raw.endswith("\n")
    assert len(raw.strip().splitlines()) == 3
    for ln in raw.strip().splitlines():
        json.loads(ln)                     # raises on a torn/interleaved write


def test_disabled_by_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ORION_SHOT_RECORDS", "0")
    assert ShotRecorder.create(log=None) is None


def test_create_honours_root_and_session(monkeypatch, tmp_path):
    monkeypatch.setenv("ORION_SHOT_RECORDS", "1")
    monkeypatch.setenv("ORION_SHOT_RECORDS_ROOT", str(tmp_path / "records"))
    monkeypatch.setenv("ORION_SHOT_RECORDS_SESSION", "session_20260916_2233")
    rec = ShotRecorder.create(start_thread=False)
    assert rec is not None
    assert rec.path == str(tmp_path / "records" / "session_20260916_2233.jsonl")
    rec.note_press(1, shot_type="Standstill")
    rec.close_all("t")
    rec.drain()
    assert os.path.exists(rec.path)


def test_session_name_follows_the_framedump_dir(monkeypatch):
    monkeypatch.setenv("ORION_FRAMEDUMP_DIR", r"D:\NexusVision\framedump\session_20260916_2233")
    assert ShotRecorder.default_session_name() == "session_20260916_2233"


# --------------------------------------------------------------------------- cost
def test_control_thread_cost_per_shot_is_under_2ms(tmp_path):
    """The press/release pair runs on the sidecar's stdin thread, between the native's
    arm command and the reader's wake-up. Budget: < 2 ms per shot."""
    rec, clock = _recorder(tmp_path)
    n = 300
    worst = 0.0
    started = time.perf_counter()
    for ep in range(1, n + 1):
        t0 = time.perf_counter()
        rec.note_press(ep, ts_ms=clock.t, mono_ms=0.0, source="square",
                       shot_type="Standstill")
        rec.note_release(ep, release_ms=clock.t + 650.0)
        worst = max(worst, time.perf_counter() - t0)
        clock.advance(2000.0)
    mean_ms = (time.perf_counter() - started) / n * 1000.0
    rec.drain()
    assert mean_ms < 2.0, "mean %.3f ms/shot on the control thread" % mean_ms
    assert worst * 1000.0 < 2.0, "worst %.3f ms" % (worst * 1000.0)


def test_writer_failure_never_raises_into_the_caller(tmp_path):
    rec, _ = _recorder(tmp_path / "does" / "not" / "exist")
    rec.note_press(1, shot_type="Standstill")
    rec.close_all("t")
    rec.drain()
    assert rec.write_errors == 1
    assert rec.records_written == 0


# --------------------------------------------------------------------------- range
# [ORION_SHOT_RANGE 2026-09-17] THREE / MID for the press, from shot_range.py.
def test_range_defaults_to_unknown(tmp_path):
    rec, clock = _recorder(tmp_path)
    rec.note_press(1, ts_ms=clock.t, mono_ms=0.0, shot_type="Left Fade")
    rec.note_release(1, clock.advance(700))
    rec.close_all("test")
    rec.drain()
    row = _lines(rec)[0]
    assert row["range"] == "unknown"
    assert row["range_conf"] == 0.0
    assert row["range_source"] == "unavailable"
    assert row["range_cells"] is None


def test_note_range_files_the_reading_and_its_cells(tmp_path):
    rec, clock = _recorder(tmp_path)
    rec.note_press(2, ts_ms=clock.t, mono_ms=0.0, shot_type="Right Fade")
    assert rec.note_range(2, {"range": "mid", "conf": 0.667, "source": "anchor",
                              "reason": "vote", "evidence": "mid"},
                          [[50.0, 180.0, 0.02, 0.9], [80.0, 178.0, 0.03, 0.88]]) is True
    rec.note_release(2, clock.advance(700))
    rec.close_all("test")
    rec.drain()
    row = _lines(rec)[0]
    assert row["range"] == "mid"
    assert row["range_conf"] == pytest.approx(0.667)
    assert row["range_evidence"] == "mid"
    assert row["range_reason"] == "vote"
    assert len(row["range_cells"]) == 2


def test_note_range_for_an_unknown_epoch_is_refused(tmp_path):
    rec, _clock = _recorder(tmp_path)
    assert rec.note_range(99, {"range": "three"}) is False


def test_the_summary_line_carries_the_range(tmp_path, caplog):
    rec, clock = _recorder(tmp_path)
    rec.note_press(3, ts_ms=clock.t, mono_ms=0.0, shot_type="Left Fade")
    rec.note_range(3, {"range": "three", "conf": 1.0, "source": "anchor"})
    rec.note_release(3, clock.advance(700))
    with caplog.at_level("ERROR", logger="ShotRecords"):
        rec.close_all("test")
        rec.drain()
    line = [r.getMessage() for r in caplog.records if "SHOT RECORD:" in r.getMessage()]
    # `dist=` is APPENDED after `range=` ([ORION_BANNER_DISTANCE 2026-09-17]), so the
    # assertion is on the field, not on the end of the line.
    assert line and "range=three" in line[0]
    assert line[0].endswith("dist=-")          # no banner -> no distance, never a guess


def test_the_banner_block_carries_the_games_own_distance(tmp_path):
    """[ORION_BANNER_DISTANCE 2026-09-17] The panel prints the shot's distance (`23'5"`).
    That is the free, exact RANGE label shot_range.py's calibration is measured against, so
    it belongs in the record whether or not the range classifier ever ships a verdict."""
    rec, clock = _recorder(tmp_path)
    rec.note_press(9, ts_ms=clock.t, mono_ms=0.0, shot_type="Standstill")
    rec.note_release(9, clock.advance(640))
    assert rec.note_banner({"attributed": 1, "release_seq": 9, "timing": "EXCELLENT",
                            "timing_color": "green", "green": True, "coverage": None,
                            "distance": "23'5\"", "distance_ft": 23.417,
                            "release_delay_ms": 1200.0, "onset_ms": clock.t,
                            "emit_latency_ms": 99.0, "ncc": 0.99, "seq": 1}) is True
    rec.close_all("test")
    rec.drain()
    out = _lines(rec)
    assert out and out[-1]["banner"]["distance"] == "23'5\""
    assert out[-1]["banner"]["distance_ft"] == pytest.approx(23.417)


def test_an_unread_distance_is_null_not_zero(tmp_path):
    rec, clock = _recorder(tmp_path)
    rec.note_press(10, ts_ms=clock.t, mono_ms=0.0, shot_type="Standstill")
    rec.note_release(10, clock.advance(640))
    rec.note_banner({"attributed": 1, "release_seq": 10, "timing": "LATE",
                     "timing_color": "red", "green": False, "coverage": None,
                     "distance": "", "distance_ft": -1.0, "release_delay_ms": 1200.0,
                     "onset_ms": clock.t, "emit_latency_ms": 99.0, "ncc": 0.99, "seq": 2})
    rec.close_all("test")
    rec.drain()
    b = _lines(rec)[-1]["banner"]
    assert b["distance"] is None and b["distance_ft"] is None
