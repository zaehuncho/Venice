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

from simple_meter_reader import SimpleMeterReader


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
