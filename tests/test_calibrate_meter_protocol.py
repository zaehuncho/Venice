"""orch.calibrate_meter (plan B3) — the wired sidecar stub, implemented.

start = snapshot + reset + RELEARN (10-shot window), per-shot IPC status
{"event":"calibrate_meter_status","shots_done":k,"shots_needed":5,"state":...},
finish = derive/clamp/persist + LOCKED, cancel = restore. The status line is emitted on
stdout exactly like pose_landmark so RemotePlaySession can parse it into the
meterCalibration* properties.
"""
import json

import numpy as np
import pytest

from simple_meter_reader import ColorCalibrator


def _build_orch(monkeypatch, tmp_path):
    monkeypatch.setenv("ORION_SIMPLE_READER", "1")
    from remote_play_orchestrator import RemotePlayOrchestrator, OrchestratorConfig
    cfg = OrchestratorConfig(console_ip="1.2.3.4", virtual_controller=False,
                             hidhide=False, auto_launch_client=False)
    orch = RemotePlayOrchestrator(cfg)
    # isolate persistence: the calibrate flow must never touch the repo's calibration/ store
    orch._meter_detector._calibrator = ColorCalibrator(
        profile_path=str(tmp_path / "meter_color_profiles.json"))
    orch._meter_detector._apply_baked_bands()
    return orch


def _conc_hist(vals_counts, size=256):
    h = np.zeros(size, np.int64)
    for v, c in vals_counts:
        h[int(v)] += int(c)
    return h


def _inject_shot(cal, hue=50, red_bgr=(10, 10, 250), peak=95.0, frames=12, locked=True):
    """One committed-quality staged shot (mirrors tests/test_color_calibrator.py)."""
    cal.begin_shot()
    red = np.stack([_conc_hist([(red_bgr[0], 5000)]), _conc_hist([(red_bgr[1], 5000)]),
                    _conc_hist([(red_bgr[2], 5000)])])
    green = np.stack([_conc_hist([(hue, 5000)]), _conc_hist([(200, 5000)]),
                      _conc_hist([(200, 5000)])])
    cal._stage["red"] = red
    cal._stage["green"] = green
    cal._stage["frames"] = frames
    cal._stage["peak"] = peak
    cal._stage["locked_any"] = locked
    cal.end_shot()


def _status_events(capsys):
    out = capsys.readouterr().out
    evs = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("event") == "calibrate_meter_status":
            evs.append(msg)
    return evs


def test_start_emits_status_and_opens_relearn(monkeypatch, tmp_path, capsys):
    orch = _build_orch(monkeypatch, tmp_path)
    assert orch.calibrate_meter("start", 5) is True
    cal = orch._meter_detector._calibrator
    assert cal.state == "relearn"
    assert cal._user_cal is not None
    evs = _status_events(capsys)
    assert evs, "no calibrate_meter_status emitted"
    last = evs[-1]
    assert last["shots_needed"] == 5
    assert last["shots_done"] == 0
    assert last["state"] == "relearn"
    assert last["calibrating"] is True


def test_cancel_restores_snapshot_and_emits(monkeypatch, tmp_path, capsys):
    orch = _build_orch(monkeypatch, tmp_path)
    cal = orch._meter_detector._calibrator
    orch.calibrate_meter("start", 5)
    assert cal.state == "relearn"
    orch.calibrate_meter("cancel", 0)
    assert cal.state == "seed"                 # snapshot restored (factory)
    assert cal._user_cal is None
    evs = _status_events(capsys)
    assert evs[-1]["calibrating"] is False
    assert evs[-1]["state"] == "seed"


def test_finish_without_commits_restores(monkeypatch, tmp_path, capsys):
    orch = _build_orch(monkeypatch, tmp_path)
    cal = orch._meter_detector._calibrator
    orch.calibrate_meter("start", 5)
    orch.calibrate_meter("finish", 0)
    assert cal.state == "seed"                 # nothing committed -> snapshot restored
    assert cal.baked is None
    assert not (tmp_path / "meter_color_profiles.json").exists()


def test_finish_with_commits_bakes_and_persists(monkeypatch, tmp_path, capsys):
    orch = _build_orch(monkeypatch, tmp_path)
    cal = orch._meter_detector._calibrator
    orch.calibrate_meter("start", 5)
    # two committed shots inside the window (injected staging, hue 50 / red 250)
    _inject_shot(cal, hue=50)
    _inject_shot(cal, hue=50)
    orch.calibrate_meter("finish", 2)
    assert cal.state == "locked"               # finish = derive/clamp/persist + LOCKED
    assert cal.baked is not None
    assert (tmp_path / "meter_color_profiles.json").exists()
    evs = _status_events(capsys)
    assert evs[-1]["state"] == "locked"
    assert evs[-1]["learned_date"] != ""


def test_status_emission_dedupes_on_version(monkeypatch, tmp_path, capsys):
    orch = _build_orch(monkeypatch, tmp_path)
    orch.calibrate_meter("start", 5)
    _status_events(capsys)                     # drain
    orch._emit_calibration_status()            # same version -> no emit
    assert _status_events(capsys) == []
    orch._emit_calibration_status(force=True)  # force -> emits again
    assert len(_status_events(capsys)) == 1


def test_calibrate_meter_ignored_on_serving_chain(monkeypatch, tmp_path):
    monkeypatch.setenv("ORION_SIMPLE_READER", "0")
    from remote_play_orchestrator import RemotePlayOrchestrator, OrchestratorConfig
    cfg = OrchestratorConfig(console_ip="1.2.3.4", virtual_controller=False,
                             hidhide=False, auto_launch_client=False)
    orch = RemotePlayOrchestrator(cfg)
    assert type(orch._meter_detector).__name__ == "MeterDetector"
    assert orch.calibrate_meter("start", 5) is False   # guarded no-op, must not raise


def test_release_marker_corroborates_current_shot(monkeypatch, tmp_path):
    orch = _build_orch(monkeypatch, tmp_path)
    reader = orch._meter_detector
    cal = reader._calibrator
    cal.begin_shot()
    assert cal._stage["release"] is False
    orch.mark_release(1234567.0, 7)            # native release_marker relay
    assert cal._stage["release"] is True       # corroboration proxy half 1 (B3 [fix])
