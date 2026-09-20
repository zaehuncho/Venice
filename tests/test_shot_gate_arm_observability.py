"""Path A (t=0 shot_gate_arm) observability contract.

The native first-edge command {"cmd":"shot_gate_arm"} used to be COMPLETELY silent end to
end, which let a prior analysis wrongly conclude the reader ran unarmed through shot
acquisition (the visible "POSE ARM" line belongs to the LATER pose_arm command).  These
tests pin the permanent forensics added 2026-08-03:

  native side  ->  "shot_gate_arm send: epoch=.. source=.. sent=.."     (RemotePlaySession)
  orch side    ->  "SHOT-GATE ARM RECEIPT: src=.. epoch=.. notify=.."   (arm_shot_gate)
  reader side  ->  "READER ACQUIRE: tier=.. floor_px=.. armed=.. hw=.." (lock seat)

plus the _arm_shot_gate return contract the receipt line is built from.
"""
import logging

import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader


# [ORION_READER_IDLE_PUBLISH_GATE 2026-09-15] The fixtures below feed a meter with NO press
# armed and (mostly) a constant fill -- byte for byte the shape the reader's idle publication
# gate now withholds from the engine and the overlay (see SimpleMeterReader._idle_publish_ok).
# The gate is a PUBLICATION policy with its own suite (tests/test_idle_publish_gate.py); these
# tests are about what the reader MEASURES, so the gate is switched off here and they keep
# measuring it.
@pytest.fixture(autouse=True)
def _idle_publish_gate_off(monkeypatch):
    monkeypatch.setenv("ORION_READER_IDLE_PUBLISH_GATE", "0")



def _meter_frame(fill_frac=1.0):
    H, W = 1080, 1920
    f = np.full((H, W, 3), 40, np.uint8)
    top = int(round(600 - fill_frac * 130))
    f[top:600, 900:912] = (0, 0, 255)
    f[top:600, 912:924] = (40, 40, 255)
    f[464:470, 900:924] = (60, 200, 60)
    return f


def _build_orch(monkeypatch, flag_value="1"):
    monkeypatch.setenv("ORION_SIMPLE_READER", flag_value)
    from remote_play_orchestrator import RemotePlayOrchestrator, OrchestratorConfig
    cfg = OrchestratorConfig(console_ip="1.2.3.4", virtual_controller=False,
                             hidhide=False, auto_launch_client=False)
    return RemotePlayOrchestrator(cfg)


# --------------------------------------------------------------------------- #
#  orchestrator RECEIPT line (arrival half of the native's silent Path A)
# --------------------------------------------------------------------------- #

def test_native_arm_logs_one_receipt_line(monkeypatch, caplog):
    orch = _build_orch(monkeypatch)
    orch._frame_seq = 321
    with caplog.at_level(logging.WARNING, logger="RemotePlayOrchestrator"):
        assert orch.arm_shot_gate("square_edge", "88") is True
    msgs = [r.getMessage() for r in caplog.records
            if "SHOT-GATE ARM RECEIPT" in r.getMessage()]
    assert len(msgs) == 1
    m = msgs[0]
    assert "src=square_edge" in m
    assert "epoch=88" in m
    assert "effective_epoch=88" in m
    assert "notify=1" in m
    assert "frame_seq=321" in m


def test_duplicate_native_arm_reports_no_notify_and_keeps_epoch(monkeypatch, caplog):
    """A refresh for the SAME shot must not report a fresh reader epoch (notify=0)."""
    orch = _build_orch(monkeypatch)
    assert orch.arm_shot_gate("square_edge", "90") is True
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="RemotePlayOrchestrator"):
        assert orch.arm_shot_gate("square_edge", "90") is True
    msgs = [r.getMessage() for r in caplog.records
            if "SHOT-GATE ARM RECEIPT" in r.getMessage()]
    assert len(msgs) == 1
    assert "epoch=90" in msgs[0]
    assert "effective_epoch=90" in msgs[0]
    assert "notify=0" in msgs[0]
    assert orch._meter_detector._physical_shot_epoch == 90


# --------------------------------------------------------------------------- #
#  [ORION_SHOT_GATE_TYPE 2026-09-15] the type/rhythm channel on the same receipt
# --------------------------------------------------------------------------- #

def test_receipt_carries_the_shot_type_and_forwards_it_to_the_reader(monkeypatch, caplog):
    orch = _build_orch(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="RemotePlayOrchestrator"):
        assert orch.arm_shot_gate("square_edge", "101", "Left Fade", True) is True
    m = [r.getMessage() for r in caplog.records if "SHOT-GATE ARM RECEIPT" in r.getMessage()][0]
    assert "shot_type=Left_Fade" in m        # one whitespace-free token, like every other field
    assert "rhythm=1" in m
    assert "typed=1" in m
    assert orch._meter_detector._pa_shot_type == "Left Fade"
    assert orch._meter_detector._pa_shot_type_epoch == 101
    assert orch._meter_detector._pa_rhythm is True


def test_old_format_arm_without_a_shot_type_still_parses(monkeypatch, caplog):
    """BACKWARD COMPATIBILITY: an old native sends source+epoch only. The receipt must still
    be emitted, the reader must still be armed, and the type must read `unclassified`."""
    orch = _build_orch(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="RemotePlayOrchestrator"):
        assert orch.arm_shot_gate("square_edge", "102") is True
    m = [r.getMessage() for r in caplog.records if "SHOT-GATE ARM RECEIPT" in r.getMessage()][0]
    assert "epoch=102" in m and "notify=1" in m
    assert "shot_type=unclassified" in m and "rhythm=0" in m and "typed=0" in m
    assert orch._meter_detector._physical_shot_epoch == 102
    assert orch._meter_detector._pa_shot_type == ""


def test_type_upgrade_refreshes_the_type_without_a_new_reader_epoch(monkeypatch, caplog):
    """The blind 200 ms grace re-sends the SAME epoch with source=type_upgrade. That is a
    duplicate arm (notify=0 -- the early trajectory is kept) that changes only the type."""
    orch = _build_orch(monkeypatch)
    assert orch.arm_shot_gate("square_edge", "103", "Standstill", False) is True
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="RemotePlayOrchestrator"):
        assert orch.arm_shot_gate("type_upgrade", "103", "Right Fade", True) is True
    m = [r.getMessage() for r in caplog.records if "SHOT-GATE ARM RECEIPT" in r.getMessage()][0]
    assert "src=type_upgrade" in m and "notify=0" in m and "shot_type=Right_Fade" in m
    assert orch._meter_detector._physical_shot_epoch == 103
    assert orch._meter_detector._pa_shot_type == "Right Fade"
    assert orch._meter_detector._pa_rhythm is True


# --------------------------------------------------------------------------- #
#  [ORION_SHOT_GATE_RELEASE 2026-09-15] the closing half of the arm
# --------------------------------------------------------------------------- #

def test_release_marker_logs_one_receipt_and_closes_the_press(monkeypatch, caplog):
    orch = _build_orch(monkeypatch)
    orch._frame_seq = 77
    assert orch.arm_shot_gate("square_edge", "110", "Standstill") is True
    with caplog.at_level(logging.WARNING, logger="RemotePlayOrchestrator"):
        assert orch.release_shot_gate("110", 1757913600123.4) is True
    msgs = [r.getMessage() for r in caplog.records if "SHOT-GATE RELEASE RECEIPT" in r.getMessage()]
    assert len(msgs) == 1
    assert "epoch=110" in msgs[0]
    assert "reason=release" in msgs[0]
    assert "release_ms=1757913600123.4" in msgs[0]
    assert "frame_seq=77" in msgs[0]
    assert orch._meter_detector._pa_released_epoch == 110


def test_disarm_marker_logs_its_own_receipt_with_the_reason(monkeypatch, caplog):
    orch = _build_orch(monkeypatch)
    assert orch.arm_shot_gate("square_edge", "111", "Standstill") is True
    with caplog.at_level(logging.WARNING, logger="RemotePlayOrchestrator"):
        assert orch.disarm_shot_gate("111", "square_early_release") is True
    msgs = [r.getMessage() for r in caplog.records if "SHOT-GATE DISARM RECEIPT" in r.getMessage()]
    assert len(msgs) == 1
    assert "epoch=111" in msgs[0] and "reason=square_early_release" in msgs[0]
    assert orch._meter_detector._pa_released_epoch == 111


def test_a_close_never_touches_the_post_release_detector_deadlines(monkeypatch):
    """The latency oracle and the release-window diagnostic still need ~1.2 s of post-release
    meter frames: _settle_shot_gate stays the only owner of those deadlines."""
    orch = _build_orch(monkeypatch)
    assert orch.arm_shot_gate("square_edge", "112", "Standstill") is True
    before = (orch._shot_gate_deadline_seq, orch._shot_gate_deadline_monotonic,
              orch._shot_gate_hw_deadline_seq, orch._shot_gate_hw_deadline_monotonic,
              orch._shot_gate_edge_pending)
    orch.release_shot_gate("112", 1.0)
    after = (orch._shot_gate_deadline_seq, orch._shot_gate_deadline_monotonic,
             orch._shot_gate_hw_deadline_seq, orch._shot_gate_hw_deadline_monotonic,
             orch._shot_gate_edge_pending)
    assert before == after
    assert orch._meter_detector._shot_armed is True


def test_a_close_for_a_retired_press_does_not_close_the_live_one(monkeypatch, caplog):
    orch = _build_orch(monkeypatch)
    assert orch.arm_shot_gate("square_edge", "120", "Standstill") is True
    assert orch.arm_shot_gate("square_edge", "121", "Standstill") is True
    with caplog.at_level(logging.WARNING, logger="RemotePlayOrchestrator"):
        assert orch.release_shot_gate("120", 5.0) is True     # the PREVIOUS shot's marker
    m = [r.getMessage() for r in caplog.records if "SHOT-GATE RELEASE RECEIPT" in r.getMessage()][0]
    assert "epoch=120" in m and "closed=0" in m
    assert orch._meter_detector._pa_released_epoch == 0
    assert orch._meter_detector._physical_shot_epoch == 121


def test_arm_shot_gate_return_contract(monkeypatch):
    """(incoming, effective, notified) — the tuple the receipt line is printed from."""
    orch = _build_orch(monkeypatch)

    # fresh tokenized epoch -> installed + reader notified
    assert orch._arm_shot_gate("square_edge", "5") == (5, 5, True)
    # exact duplicate -> kept, NOT re-notified (would erase the early trajectory)
    assert orch._arm_shot_gate("square_edge", "5") == (5, 5, False)
    # stale lower epoch -> current epoch kept, not notified
    assert orch._arm_shot_gate("square_edge", "4") == (4, 5, False)
    # untokenized local refresh -> epoch kept, not notified
    assert orch._arm_shot_gate("pose") == (0, 5, False)
    # newer epoch -> supersedes + notifies
    assert orch._arm_shot_gate("square_edge", "6") == (6, 6, True)


def test_untokenized_arm_on_clean_gate_still_wakes_reader(monkeypatch):
    """Legacy/local arms (no epoch anywhere) must keep waking the reader (notified=True)."""
    orch = _build_orch(monkeypatch)
    assert orch._arm_shot_gate("pose") == (0, 0, True)


# --------------------------------------------------------------------------- #
#  reader ACQUIRE line (armed/hw state + floor at the first accepted sample)
# --------------------------------------------------------------------------- #

def test_armed_acquire_logs_tier_floor_and_epoch(caplog):
    r = SimpleMeterReader(1920, 1080)
    r.notify_physical_shot_start(7)
    r.set_shot_state(True, 1.0, True)
    with caplog.at_level(logging.WARNING, logger="simple_reader"):
        res = r.detect(_meter_frame(1.0), ts=0.0)
    assert res.detected is True
    msgs = [rec.getMessage() for rec in caplog.records
            if "READER ACQUIRE" in rec.getMessage()]
    assert len(msgs) == 1
    m = msgs[0]
    assert "tier=scan_default" in m
    assert "floor_px=%d" % r._h_acq_armed in m      # ARMED floor (15 @1080p), not 33
    assert "armed=1" in m
    assert "hw=1" in m
    assert "epoch=7" in m


def test_unarmed_acquire_logs_cold_floor(caplog):
    r = SimpleMeterReader(1920, 1080)
    with caplog.at_level(logging.WARNING, logger="simple_reader"):
        res = r.detect(_meter_frame(1.0), ts=0.0)
    assert res.detected is True
    msgs = [rec.getMessage() for rec in caplog.records
            if "READER ACQUIRE" in rec.getMessage()]
    assert len(msgs) == 1
    m = msgs[0]
    assert "floor_px=%d" % r._h_acq in m            # COLD floor (33 @1080p)
    assert "armed=0" in m
    assert "hw=0" in m
    assert "epoch=0" in m


def test_unarmed_reacquire_is_rate_limited(caplog):
    """Menu/decor churn must not spam the 60fps log: unarmed seats log at most 1 per 2s."""
    r = SimpleMeterReader(1920, 1080)
    with caplog.at_level(logging.WARNING, logger="simple_reader"):
        assert r.detect(_meter_frame(1.0), ts=0.0).detected is True
        # force a lock drop, then an immediate unarmed re-acquire inside the 2s window
        r.conf = 0.0
        r.box = None
        r.tmpl = None
        assert r.detect(_meter_frame(1.0), ts=0.1).detected is True
    msgs = [rec.getMessage() for rec in caplog.records
            if "READER ACQUIRE" in rec.getMessage()]
    assert len(msgs) == 1


def test_armed_reacquire_always_logs(caplog):
    """Armed seats are the signal being hunted — never rate-limit them."""
    r = SimpleMeterReader(1920, 1080)
    r.notify_physical_shot_start(11)
    r.set_shot_state(True, 1.0, True)
    with caplog.at_level(logging.WARNING, logger="simple_reader"):
        assert r.detect(_meter_frame(1.0), ts=0.0).detected is True
        r.conf = 0.0
        r.box = None
        r.tmpl = None
        assert r.detect(_meter_frame(1.0), ts=0.1).detected is True
    msgs = [rec.getMessage() for rec in caplog.records
            if "READER ACQUIRE" in rec.getMessage()]
    assert len(msgs) == 2


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


# ----------------------------------------------------- [ORION_CV_TIPLESS_ARMED] the press window
# The locator's tip-less acceptance path needs WHEN (a press is open and the meter is due) but
# not WHERE (the nameplate anchor, which costs a plate search per frame). The reader is the only
# place a press epoch and a frame timestamp are known together, so it publishes the press window
# whenever EITHER consumer wants it -- and the anchor's own forensics stay behind the anchor's
# own switch, so a tipless-only install does not start emitting blank PICKUP: lines.
#
# [SHIP CONFIG 2026-09-17] ORION_PLAYER_ANCHOR now defaults ON, so the tests below turn it OFF
# explicitly instead of taking the default. What they measure -- that the press window is
# published for the TIPLESS consumer alone, with no anchor in play -- is unchanged, and that is
# still a real configuration (it is what the anchor's kill switch leaves behind).
import types

import player_anchor as _pa


def _armed_reader(epoch=9, ts=1000.0, shot_type="Right Fade"):
    r = SimpleMeterReader(1920, 1080)
    r.notify_physical_shot_start(epoch)
    r.notify_physical_shot_type(epoch, shot_type)
    r.set_shot_state(True, 1.0, True)
    r.detect(_meter_frame(1.0), ts=ts)
    return r


def test_press_window_is_published_with_the_anchor_switched_off(monkeypatch):
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "0")   # [SHIP CONFIG 2026-09-17] now ON by default
    monkeypatch.delenv("ORION_CV_TIPLESS_ARMED", raising=False)
    _pa.ARM.reset()
    try:
        assert _pa.enabled() is False
        _armed_reader(epoch=9, ts=1000.0)
        st = _pa.ARM.state()
        assert st[3] is True and st[0] == 9
        assert st[1] == pytest.approx(1000.0)        # the FRAME clock, not wall time
        assert st[2] == "Right Fade"
    finally:
        _pa.ARM.reset()


def test_press_window_stays_unpublished_when_neither_consumer_wants_it(monkeypatch):
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "0")   # [SHIP CONFIG 2026-09-17] now ON by default
    monkeypatch.setenv("ORION_CV_TIPLESS_ARMED", "0")
    _pa.ARM.reset()
    try:
        _armed_reader(epoch=9, ts=1000.0)
        assert _pa.ARM.state()[3] is False
    finally:
        _pa.ARM.reset()


def test_the_release_closes_the_arm_only_window(monkeypatch):
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "0")   # [SHIP CONFIG 2026-09-17] now ON by default
    monkeypatch.delenv("ORION_CV_TIPLESS_ARMED", raising=False)
    _pa.ARM.reset()
    try:
        r = _armed_reader(epoch=9, ts=1000.0)
        assert _pa.ARM.state()[3] is True
        r.set_shot_state(False, 0.0, False)
        r.detect(_meter_frame(1.0), ts=1000.1)
        assert _pa.ARM.state()[3] is False
    finally:
        _pa.ARM.reset()


def test_anchor_forensics_stay_behind_the_anchor_switch(monkeypatch, caplog):
    """PICKUP: describes what the ANCHOR did; with the anchor off every field but the epoch
    would be blank, so an arm-only publication must not start a census of empty lines."""
    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "0")   # [SHIP CONFIG 2026-09-17] now ON by default
    monkeypatch.delenv("ORION_CV_TIPLESS_ARMED", raising=False)
    _pa.ARM.reset()
    try:
        with caplog.at_level(logging.ERROR, logger="simple_reader"):
            r = _armed_reader(epoch=9, ts=1000.0)
            r.set_shot_state(False, 0.0, False)
            r.detect(_meter_frame(1.0), ts=1000.1)
        assert not [m for m in caplog.records if m.getMessage().startswith("PICKUP:")]
    finally:
        _pa.ARM.reset()


def _reader_with_tipless_record(rec):
    r = SimpleMeterReader(1920, 1080)
    r._meter_detector = types.SimpleNamespace(_base=types.SimpleNamespace(tipless=rec))
    return r


def test_tipless_lock_line_is_emitted_once_per_press_at_error_level(caplog):
    """ERROR, like PICKUP:, because every sidecar WARNING shares one 1 s throttle slot that the
    press's own arm receipt has already taken -- a WARNING here dies on every press."""
    rec = {"epoch": 8, "fill": 42.0, "rise_frames": 2, "conf": 0.70, "logged": 0}
    r = _reader_with_tipless_record(rec)
    with caplog.at_level(logging.ERROR, logger="simple_reader"):
        r._flush_tipless_line()
        r._flush_tipless_line()
    lines = [m.getMessage() for m in caplog.records
             if m.getMessage().startswith("TIPLESS LOCK:")]
    assert lines == ["TIPLESS LOCK: epoch=8 fill=42.0 rise_frames=2 conf=0.70"]
    assert rec["logged"] == 1


def test_no_tipless_line_for_a_press_the_path_never_produced(caplog):
    rec = {"epoch": 8, "fill": -1.0, "rise_frames": 0, "conf": 0.0, "logged": 0}
    r = _reader_with_tipless_record(rec)
    with caplog.at_level(logging.ERROR, logger="simple_reader"):
        r._flush_tipless_line()
    assert not [m for m in caplog.records
                if m.getMessage().startswith("TIPLESS LOCK:")]
    assert rec["logged"] == 0
