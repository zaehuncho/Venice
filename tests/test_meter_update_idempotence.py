"""A full remap notification must not masquerade as a changed meter profile."""
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture(params=("simple", "compressed"))
def orch(monkeypatch, request):
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")
    module_path = os.environ.get("ORION_ORCHESTRATOR_TEST_MODULE")
    if module_path:
        spec = importlib.util.spec_from_file_location("meter_update_candidate", module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    else:
        import remote_play_orchestrator as module
    from simple_meter_reader import SimpleMeterReader
    from compressed_meter_reader import CompressedMeterReader
    reader_class = SimpleMeterReader if request.param == "simple" else CompressedMeterReader
    cfg = SimpleNamespace(meter_color="White", meter_style="Arrow2", confidence_threshold=.32,
                          meter_hsv_low=[0, 0, 200], meter_hsv_high=[180, 80, 255])
    det = reader_class(1280, 720, cfg=cfg)
    instance = object.__new__(module.RemotePlayOrchestrator)
    instance._meter_detector = det
    instance.config = SimpleNamespace(meter_color="White", meter_style="Arrow2")
    return instance


def seed(det):
    det._det_state = "locked"
    det._det_active_box = (600, 300, 24, 120)
    det._subpx_D = 115.0
    det._subpx_ruler_hist = [(115.0, 5.0)] * 3
    det._sess_track_h = [115.0] * 3
    det._physical_shot_epoch = 36


def snapshot(det):
    return (det._det_state, det._det_active_box, det._subpx_D,
            list(det._subpx_ruler_hist), list(det._sess_track_h),
            det._cfg.meter_hsv_low, det._cfg.meter_hsv_high,
            det._physical_shot_epoch)


@pytest.mark.parametrize("kwargs", [
    dict(meter_style="Arrow2", meter_color="White"),
    dict(meter_color="White"),
    dict(meter_style="Arrow2"),
    dict(meter_style=" arrow2 ", meter_color=" white "),
    {},
])
def test_noop_remap_preserves_active_shot_and_session_ruler(orch, kwargs):
    seed(orch._meter_detector)
    before = snapshot(orch._meter_detector)
    for _ in range(3):
        orch.update_meter(**kwargs)
    assert snapshot(orch._meter_detector) == before


@pytest.mark.parametrize("kwargs, color, style", [
    (dict(meter_color="Purple"), "Purple", "Arrow2"),
    (dict(meter_style="Pill"), "White", "Pill"),
    (dict(meter_color="Purple", meter_style="Pill"), "Purple", "Pill"),
])
def test_real_profile_change_still_discards_old_meter_state(orch, kwargs, color, style):
    det = orch._meter_detector
    seed(det)
    orch.update_meter(**kwargs)
    assert det._det_state == "idle"
    assert det._det_active_box is None
    assert det._subpx_D is None
    assert det._subpx_ruler_hist == []
    assert det._sess_track_h == []
    assert det._physical_shot_epoch == 36  # no invented arm/ownership
    assert det._meter_color == color
    assert det._tracking_meter_style == style.casefold()


def test_externally_mutated_shared_cfg_is_not_applied_identity(orch):
    det = orch._meter_detector
    seed(det)
    det._cfg.meter_color = "Purple"
    det._cfg.meter_style = "Pill"
    orch.config.meter_color = "Purple"
    orch.config.meter_style = "Pill"
    orch.update_meter(meter_color="Purple", meter_style="Pill")
    assert det._meter_color == "Purple"
    assert det._tracking_meter_style == "pill"
    assert det._det_active_box is None
    assert det._subpx_ruler_hist == []


def test_explicit_hsv_reload_remains_a_real_profile_change(orch):
    det = orch._meter_detector
    seed(det)
    det._cfg.meter_hsv_low = [1, 2, 3]
    det._cfg.meter_hsv_high = [4, 5, 6]
    det.reload_config(det._cfg)
    assert det._det_state == "idle"
    assert det._subpx_ruler_hist == []
    assert det._cfg.meter_hsv_low == [1, 2, 3]


def test_noop_notification_reconciles_unapplied_shared_cfg_without_reset(orch):
    det = orch._meter_detector
    seed(det)
    before = snapshot(det)
    det._cfg.meter_color = "Purple"
    det._cfg.meter_style = "Pill"
    orch.update_meter(meter_color="White", meter_style="Arrow2")
    assert snapshot(det) == before
    assert det._cfg.meter_color == "White"
    assert det._cfg.meter_style == "Arrow2"


def test_repeated_remap_does_not_reset_async_locator(orch):
    det = orch._meter_detector
    resets = []
    det._meter_detector = SimpleNamespace(reset=lambda: resets.append("reset"))
    seed(det)
    orch.update_meter(meter_color="White", meter_style="Arrow2")
    assert resets == []
    orch.update_meter(meter_color="Purple")
    assert resets


def test_legacy_detector_noop_style_after_initial_apply_is_idempotent(orch):
    resets = []
    cfg = SimpleNamespace(meter_color="White", park_temporal_enabled=False)
    det = SimpleNamespace(_cfg=cfg, _active=None,
                          reload_config=lambda cfg: resets.append("reload"),
                          reset_tracking=lambda: resets.append("reset"))
    def style(value):
        det._active = value
        resets.append("style")
    det.set_active_style = style
    orch._meter_detector = det
    orch.update_meter(meter_color="White", meter_style="Arrow2")
    assert det._active == "Arrow2"
    resets.clear()
    cfg.meter_style = "Pill"  # caller changed the shared object, not applied state
    orch.update_meter(meter_color="White", meter_style="Arrow2")
    assert resets == []
    orch.update_meter(meter_style="Pill")
    assert resets == ["style"]


def test_detector_replacement_does_not_inherit_previous_applied_profile(orch):
    orch.update_meter(meter_color="White", meter_style="Arrow2")
    from simple_meter_reader import SimpleMeterReader
    det = SimpleMeterReader(1280, 720, cfg=SimpleNamespace(
        meter_color="Red", meter_style="Pill", confidence_threshold=.32))
    orch._meter_detector = det
    seed(det)
    orch.update_meter(meter_color="White", meter_style="Arrow2")
    assert det._meter_color == "White"
    assert det._tracking_meter_style == "arrow2"
    assert det._det_state == "idle"
