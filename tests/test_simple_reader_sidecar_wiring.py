"""ORION_SIMPLE_READER wiring + emission-contract coverage.

Proves (1) the flag actually swaps the orchestrator's detector to SimpleMeterReader (and OFF keeps
the chain's MeterDetector), (2) the reader's detect() emits the SAME DetectResult contract the
orchestrator/sidecar/timing stack read, (3) the feed gate accepts it, and (4) the sidecar telemetry
payload built from it still carries meter_present / pixel_age_ms / heartbeat / stalled unchanged.
"""
import os

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


def _build_orch(monkeypatch, flag_value):
    if flag_value is None:
        monkeypatch.delenv("ORION_SIMPLE_READER", raising=False)
    else:
        monkeypatch.setenv("ORION_SIMPLE_READER", flag_value)
    from remote_play_orchestrator import RemotePlayOrchestrator, OrchestratorConfig
    cfg = OrchestratorConfig(console_ip="1.2.3.4", virtual_controller=False,
                             hidhide=False, auto_launch_client=False)
    return RemotePlayOrchestrator(cfg)


def test_flag_on_swaps_detector_to_simple_reader(monkeypatch):
    orch = _build_orch(monkeypatch, "1")
    det = orch._meter_detector
    assert det is not None
    assert type(det).__name__ == "SimpleMeterReader"
    assert hasattr(det, "detect")          # drop-in for MeterDetector.detect()
    assert hasattr(det, "last_debug")       # orch reads getattr(det,'last_debug',{})
    assert det._require_gameplay_eligibility is True


def test_flag_off_keeps_serving_chain(monkeypatch):
    orch = _build_orch(monkeypatch, "0")
    assert type(orch._meter_detector).__name__ == "MeterDetector"


def test_flag_absent_defaults_to_simple_reader(monkeypatch):
    """FLIPPED 2026-07-25: absent flag now selects the SHIPPED reader, not the retired chain.

    This previously asserted the pre-retirement default (absent -> MeterDetector). The YOLO
    serving chain is retired and SimpleMeterReader is the only path the product ships, so
    defaulting to the retired chain meant any entry point that did not go through the native
    launcher (which injects ORION_SIMPLE_READER=1) silently fell back to a dead detector.
    The flag remains as an explicit opt-OUT -- test_flag_off_keeps_serving_chain above still
    pins that "0" reaches the chain."""
    orch = _build_orch(monkeypatch, None)
    assert type(orch._meter_detector).__name__ == "SimpleMeterReader"


def test_detect_emits_full_detectresult_contract():
    """Every attribute the orchestrator reads off the detect() result must exist."""
    r = SimpleMeterReader(1920, 1080)
    res = r.detect(_meter_frame(1.0))
    # fields the orchestrator _processing_loop consumes (remote_play_orchestrator.py ~1850-2090)
    for attr in ("detected", "bbox", "fill_pct", "confidence", "consecutive_frames",
                 "fill_velocity_pct_s", "fill_acceleration_pct_s2", "eta_ms",
                 "eta_to_green_center_ms", "green_window_center_pct", "green_window_confidence",
                 "green_window_start_pct", "green_window_end_pct", "green_window_width_pct",
                 "rejection_reason", "rise_state", "top_pixel_row",
                 "raw_fill_pct", "fill_estimator_mode", "fill_estimator_generation",
                 "gameplay_structure_verified", "gameplay_structure_epoch"):
        assert hasattr(res, attr), f"missing contract field: {attr}"
    assert res.detected is True
    assert res.bbox[2] > 0 and res.bbox[3] > 0
    assert 0.0 <= res.fill_pct <= 100.0
    assert res.fill_pct > 90.0


def test_tracking_wire_uses_canonical_estimator_generation_string():
    """The generation must not cross JSON as an IEEE-754 number."""
    from dataclasses import asdict
    from remote_play_orchestrator import _MeterTrackPayload

    payload = asdict(_MeterTrackPayload(
        fill_pct=31.25,
        coarse_fill_pct=31.13,
        fill_estimator_mode="subpixel",
        fill_estimator_generation="9007199254740993",
        confidence=0.91,
    ))
    assert payload["fill_estimator_mode"] == "subpixel"
    assert payload["fill_estimator_generation"] == "9007199254740993"
    assert isinstance(payload["fill_estimator_generation"], str)
    assert payload["coarse_fill_pct"] == pytest.approx(31.13)


def test_detect_result_is_the_real_dataclass():
    """When meter_detector imports, detect() returns the ACTUAL DetectResult so field parity with
    the chain is guaranteed (not a look-alike)."""
    from meter_detector import DetectResult
    res = SimpleMeterReader(1920, 1080).detect(_meter_frame(1.0))
    assert isinstance(res, DetectResult)


def test_feed_gate_accepts_a_fresh_simple_detection():
    from remote_play_orchestrator import RemotePlayOrchestrator
    res = SimpleMeterReader(1920, 1080).detect(_meter_frame(1.0))
    # a fresh detect carries rejection_reason='green_not_found' -> the rising-phase FED reason
    assert res.rejection_reason in ("", "green_not_found")
    assert RemotePlayOrchestrator._should_feed_engine(res) is True


def test_feed_gate_rejects_no_meter():
    from remote_play_orchestrator import RemotePlayOrchestrator
    res = SimpleMeterReader(1920, 1080).detect(np.full((1080, 1920, 3), 40, np.uint8))
    assert res.detected is False
    assert RemotePlayOrchestrator._should_feed_engine(res) is False


def test_arm_shot_gate_arms_the_simple_reader(monkeypatch):
    """The shot-start plumb (_arm_shot_gate, called by arm_pose / controller edges) arms the reader
    and sets a bounded deadline."""
    orch = _build_orch(monkeypatch, "1")
    reader = orch._meter_detector
    assert reader._shot_armed is False
    orch._frame_seq = 500
    orch._arm_shot_gate()
    assert reader._shot_armed is True                     # reader armed immediately
    assert orch._shot_gate_deadline_seq == 500 + orch._shot_gate_arm_frames
    # Production scene gate opens on this trusted physical signal and preserves immediate
    # structure-proven acquisition.
    assert reader.detect(_meter_frame(1.0), ts=0.0).detected is True


def test_local_then_native_arm_installs_tokenized_reader_epoch(monkeypatch):
    orch = _build_orch(monkeypatch, "1")
    reader = orch._meter_detector

    orch._arm_shot_gate("square")
    assert orch._shot_gate_epoch == 0
    assert reader._physical_shot_epoch == 0

    assert orch.arm_shot_gate("square_edge", "73") is True
    assert orch._shot_gate_epoch == 73
    assert reader._physical_shot_epoch == 73


def test_native_then_local_arm_cannot_clobber_tokenized_reader_epoch(monkeypatch):
    orch = _build_orch(monkeypatch, "1")
    reader = orch._meter_detector

    assert orch.arm_shot_gate("square_edge", "74") is True
    reader._gameplay_structure_verified = True
    orch._arm_shot_gate("square")

    assert orch._shot_gate_epoch == 74
    assert reader._physical_shot_epoch == 74
    assert reader._gameplay_structure_verified is True


def test_live_reader_suppresses_meter_and_overlay_until_physical_arm(monkeypatch):
    """Valid-looking pixels alone cannot publish telemetry/overlay in menus or loading screens."""
    orch = _build_orch(monkeypatch, "1")
    reader = orch._meter_detector
    out = reader.detect(_meter_frame(1.0), ts=0.0)
    assert out.detected is False
    assert out.rejection_reason == "gameplay_ineligible"

    orch._last_meter_bbox = (10, 20, 30, 40)
    orch._last_meter_bbox_wh = (1280, 720)
    orch._last_green_window = {"start": 80.0, "end": 90.0}
    orch._green_show_streak = 2
    orch._green_hide_streak = 7
    orch._clear_meter_overlay_immediate()
    assert orch._last_meter_bbox is None
    assert orch._last_meter_bbox_wh is None
    assert orch._last_green_window is None
    assert orch._green_show_streak == orch._green_hide_streak == 0


def test_arm_pose_plumbs_to_reader(monkeypatch):
    """arm_pose (native pose_arm stdin cmd) arms the shot-gate even when pose timing is absent."""
    orch = _build_orch(monkeypatch, "1")
    orch._pose_timing = None
    orch._frame_seq = 10
    assert orch.arm_pose("41") is True
    assert orch._meter_detector._shot_armed is True
    assert orch._shot_gate_deadline_seq >= 10
    assert orch._active_pose_arm_token == 41


def test_first_edge_then_pose_arm_resets_reader_once_for_same_shot(monkeypatch):
    """The early native edge must not cause pose_arm to erase the acquired rise ~190ms later."""
    orch = _build_orch(monkeypatch, "1")
    reader = orch._meter_detector
    calls = []
    original = reader.notify_physical_shot_start

    def counted():
        calls.append(True)
        return original()

    reader.notify_physical_shot_start = counted
    assert orch.arm_shot_gate("square_edge") is True
    assert len(calls) == 1
    assert orch._shot_gate_edge_pending is True

    # Tokenized beginShot for the same physical gesture refreshes deadlines but
    # does not begin a second reader epoch.
    assert orch.arm_pose("42") is True
    assert len(calls) == 1
    assert orch._shot_gate_edge_pending is False

    # A legacy pose_arm with no preceding edge still opens a fresh epoch.
    assert orch.arm_pose("43") is True
    assert len(calls) == 2


def test_arm_pose_binds_same_token_to_pose_detector(monkeypatch):
    class FakePose:
        call = None

        def notify_shot_start(self, frame_seq, timestamp=0.0, arm_token=0):
            self.call = (frame_seq, arm_token)

    orch = _build_orch(monkeypatch, "1")
    pose = FakePose()
    orch._pose_timing = pose
    orch._frame_seq = 23

    assert orch.arm_pose("18446744073709551615") is True
    assert pose.call == (23, 0xFFFFFFFFFFFFFFFF)
    assert orch._active_pose_arm_token == 0xFFFFFFFFFFFFFFFF


def test_invalid_pose_arm_cannot_open_meter_gate(monkeypatch):
    orch = _build_orch(monkeypatch, "1")
    assert orch.arm_pose("0") is False
    assert orch._active_pose_arm_token == 0
    assert orch._meter_detector._shot_armed is False


def test_arm_shot_gate_noop_on_serving_chain(monkeypatch):
    """For the serving chain (flag off) the arm plumb is a guarded no-op (MeterDetector has no
    set_shot_state) -- it must not raise."""
    orch = _build_orch(monkeypatch, "0")
    assert type(orch._meter_detector).__name__ == "MeterDetector"
    orch._arm_shot_gate()                                  # must not raise
    assert not hasattr(orch._meter_detector, "set_shot_state")


def test_processing_loop_arm_gate_expires(monkeypatch):
    """Both frame and monotonic deadlines govern the per-frame reader arm."""
    orch = _build_orch(monkeypatch, "1")
    reader = orch._meter_detector
    orch._frame_seq = 100
    orch._arm_shot_gate()
    gate_started = orch._shot_gate_deadline_monotonic - orch._shot_gate_max_seconds
    merged, hardware = orch._shot_gate_state(
        orch._frame_seq, monotonic_now=gate_started + 0.1)
    reader.set_shot_state(merged, 0.0, hardware)
    assert reader._shot_armed is True

    # Detector sequence can freeze forever on a dark/loading screen; wall time must still close it.
    merged, hardware = orch._shot_gate_state(
        orch._frame_seq,
        monotonic_now=orch._shot_gate_deadline_monotonic + 0.001,
    )
    reader.set_shot_state(merged, 0.0, hardware)
    assert reader._shot_armed is False


def test_long_shot_gate_outlives_old_two_second_frame_cap(monkeypatch):
    orch = _build_orch(monkeypatch, "1")
    orch._frame_seq = 100
    orch._arm_shot_gate()
    gate_started = orch._shot_gate_deadline_monotonic - orch._shot_gate_max_seconds

    # 180 frames is three seconds at the detector's real 60 Hz cadence: the old 120-frame
    # implementation had already disarmed here, just as a long Go-To meter appeared.
    merged, hardware = orch._shot_gate_state(280, monotonic_now=gate_started + 3.0)
    assert merged is True
    assert hardware is True


def test_repeated_physical_arm_starts_a_fresh_reader_epoch(monkeypatch):
    orch = _build_orch(monkeypatch, "1")
    reader = orch._meter_detector
    orch._arm_shot_gate("pose")
    first = reader.detect(_meter_frame(1.0), ts=1.0)
    assert first.detected is True
    assert reader._prev_hw_armed is True

    # A second shot can begin before the long arm deadline expires. The explicit start hook must
    # force the next read to observe a new hardware edge instead of inheriting the old tracker.
    orch._frame_seq += 30
    orch._arm_shot_gate("pose")
    assert reader._prev_hw_armed is False
    second = reader.detect(_meter_frame(0.55), ts=2.0)
    assert second.detected is True
    assert reader._prev_hw_armed is True
    assert second.fill_pct < first.fill_pct


def test_release_shortens_but_does_not_immediately_close_meter_vision(monkeypatch):
    orch = _build_orch(monkeypatch, "1")
    orch._frame_seq = 50
    orch._arm_shot_gate()
    full_deadline = orch._shot_gate_deadline_monotonic
    orch.mark_release(seq=1)

    assert orch._shot_gate_deadline_monotonic < full_deadline
    remaining = orch._shot_gate_deadline_monotonic - __import__('time').perf_counter()
    assert 0.0 < remaining <= orch._shot_gate_post_release_seconds


def test_sidecar_payload_contract_intact_over_simple_reader():
    """Build the sidecar 'shot'/present payload the way autogreen_sidecar does and confirm the
    staleness contract (meter_present / pixel_age_ms / heartbeat / stalled) is preserved."""
    import importlib.util
    _sc = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "native_orion", "backend", "autogreen_sidecar.py")
    _spec = importlib.util.spec_from_file_location("_autogreen_sidecar_ut", _sc)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _apply_stall_override = _mod._apply_stall_override

    r = SimpleMeterReader(1920, 1080)
    res = r.detect(_meter_frame(1.0))
    meter_present = bool(res.detected and res.bbox and res.bbox[2] > 0 and res.bbox[3] > 0)
    payload = {
        "event": "telemetry",
        "meter_present": meter_present,
        "raw_fed": res.rejection_reason in ("", "green_not_found"),
        "pixel_age_ms": 5.0,
        "frame_count": 10,
        "heartbeat": 1,
        "shot": {
            "fill_pct": float(res.fill_pct) if meter_present else 0.0,
            "confidence": float(res.confidence) if meter_present else 0.0,
            "rtt_offset_ms": 0.0,
        },
        "bbox": [int(v) for v in res.bbox],
    }
    # fresh live frame: NOT stalled -> meter served
    assert _apply_stall_override(payload, payload["pixel_age_ms"], 250.0) is False
    assert payload["stalled"] is False
    assert payload["meter_present"] is True
    assert payload["shot"]["fill_pct"] > 90.0

    # stale pixels (>= watchdog): stall override forces a no-meter emission + zeroed fill
    stalled = _apply_stall_override(payload, 500.0, 250.0)
    assert stalled is True
    assert payload["stalled"] is True
    assert payload["meter_present"] is False
    assert payload["raw_fed"] is False
    assert payload["shot"]["fill_pct"] == 0.0


# --------------------------------------------------------------------------- #
#  [ORION_SHOT_GATE_TYPE / _RELEASE 2026-09-15] native command -> reader -> locator
# --------------------------------------------------------------------------- #

def test_native_arm_type_reaches_the_locator_and_the_release_closes_the_press(monkeypatch):
    """End to end on the REAL orchestrator + REAL SimpleMeterReader: the shot type the engine
    put on shot_gate_arm ends up bounding the locator's meter-onset window, and the engine's
    release marker closes that window instead of it timing out on ORION_ANCHOR_ARM_S."""
    import player_anchor as pa

    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    pa.reset_all(keep_identity=False)
    orch = _build_orch(monkeypatch, "1")
    reader = orch._meter_detector

    assert orch.arm_shot_gate("square_edge", "31", "Left Fade", True) is True
    reader.detect(_meter_frame(1.0), ts=100.0)          # the frame-clock republish
    assert pa.ARM.armed and pa.ARM.epoch == 31
    assert pa.ARM.shot_type == "Left Fade" and pa.ARM.rhythm is True
    assert pa.onset_window_ms(pa.ARM.shot_type) == pytest.approx((575.0, 1075.0))

    # the blind 200 ms grace re-types the press: same epoch, no new press timestamp
    assert orch.arm_shot_gate("type_upgrade", "31", "Right Fade", False) is True
    assert pa.ARM.shot_type == "Right Fade" and pa.ARM.press_ts == pytest.approx(100.0)

    assert orch.release_shot_gate("31", 1757913600123.4) is True
    assert not pa.ARM.armed
    reader.detect(_meter_frame(1.0), ts=100.02)         # must NOT re-arm the answered press
    assert not pa.ARM.armed
    pa.reset_all(keep_identity=False)


def test_native_arm_without_a_type_keeps_the_union_window(monkeypatch):
    """BACKWARD COMPATIBILITY: an old native's two-argument arm still works end to end."""
    import player_anchor as pa

    monkeypatch.setenv("ORION_PLAYER_ANCHOR", "1")
    pa.reset_all(keep_identity=False)
    orch = _build_orch(monkeypatch, "1")
    assert orch.arm_shot_gate("square_edge", "32") is True
    orch._meter_detector.detect(_meter_frame(1.0), ts=200.0)
    assert pa.ARM.armed and pa.ARM.shot_type == ""
    assert pa.onset_window_ms(pa.ARM.shot_type) == pytest.approx((50.0, 1100.0))
    pa.reset_all(keep_identity=False)


def test_sidecar_stdin_contract_for_the_three_shot_gate_commands():
    """The sidecar's stdin dispatch reads exactly these keys; pin the names so a rename on
    either side of the boundary fails here rather than silently in production."""
    import inspect
    import remote_play_orchestrator as rpo

    src = _sidecar_source()
    for needle in ('elif cmd == "shot_gate_arm"', 'elif cmd == "shot_gate_release"',
                   'elif cmd == "shot_gate_disarm"', 'msg.get("shot_type"',
                   'msg.get("rhythm"', 'msg.get("release_ms"', 'msg.get("reason"'):
        assert needle in src, needle

    arm = inspect.signature(rpo.RemotePlayOrchestrator.arm_shot_gate).parameters
    assert list(arm) == ["self", "source", "shot_epoch", "shot_type", "rhythm", "press_ms"]
    # every new argument is optional: an old sidecar calling the two-argument form still works
    assert arm["shot_type"].default == ""
    assert arm["rhythm"].default is False
    rel = inspect.signature(rpo.RemotePlayOrchestrator.release_shot_gate).parameters
    assert list(rel) == ["self", "shot_epoch", "release_ms"]
    dis = inspect.signature(rpo.RemotePlayOrchestrator.disarm_shot_gate).parameters
    assert list(dis) == ["self", "shot_epoch", "reason"]


def _sidecar_source():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "native_orion", "backend", "autogreen_sidecar.py")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()
