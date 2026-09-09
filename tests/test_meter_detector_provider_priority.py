"""ONNX provider selection for the production meter locator."""
from __future__ import annotations

import types

from meter_detector_yolo import (
    MeterYoloLocator,
    _provider_priority,
    _resolve_default_model,
)


def test_default_provider_order_prefers_cuda_then_directml_then_cpu(monkeypatch):
    monkeypatch.delenv("ORION_METER_PROVIDER_PRIORITY", raising=False)
    available = ["CPUExecutionProvider", "DmlExecutionProvider",
                 "CUDAExecutionProvider"]
    assert _provider_priority(available) == [
        "CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"]


def test_directml_is_accelerated_fallback_when_cuda_absent(monkeypatch):
    monkeypatch.delenv("ORION_METER_PROVIDER_PRIORITY", raising=False)
    assert _provider_priority(
        ["CPUExecutionProvider", "DmlExecutionProvider"]) == [
            "DmlExecutionProvider", "CPUExecutionProvider"]


def test_explicit_provider_priority_accepts_aliases_and_deduplicates():
    available = ["CUDAExecutionProvider", "DmlExecutionProvider",
                 "CPUExecutionProvider"]
    assert _provider_priority(available, "cpu,directml,cpu,cuda") == [
        "CPUExecutionProvider", "DmlExecutionProvider", "CUDAExecutionProvider"]


def test_unavailable_preference_falls_back_to_installed_defaults():
    assert _provider_priority(
        ["DmlExecutionProvider", "CPUExecutionProvider"], "cuda,unknown") == [
            "DmlExecutionProvider", "CPUExecutionProvider"]


def test_directml_primary_applies_required_session_options(monkeypatch, tmp_path):
    captured = {}

    class _Input:
        name = "images"
        shape = [1, 3, 960, 960]
        type = "tensor(float)"

    class _Session:
        def __init__(self, path, sess_options, providers):
            captured.update(path=path, options=sess_options,
                            providers=list(providers))

        def get_inputs(self):
            return [_Input()]

        def get_providers(self):
            return list(captured["providers"])

    class _Options:
        def __init__(self):
            self.log_severity_level = None
            self.enable_mem_pattern = True
            self.execution_mode = None
            self.config = {}

        def add_session_config_entry(self, key, value):
            self.config[key] = value

    fake_ort = types.SimpleNamespace(
        get_available_providers=lambda: [
            "DmlExecutionProvider", "CPUExecutionProvider"],
        SessionOptions=_Options,
        InferenceSession=_Session,
        ExecutionMode=types.SimpleNamespace(ORT_SEQUENTIAL="sequential"),
    )
    monkeypatch.setitem(__import__("sys").modules, "onnxruntime", fake_ort)
    monkeypatch.delenv("ORION_METER_PROVIDER_PRIORITY", raising=False)
    model = tmp_path / "meter.onnx"
    model.write_bytes(b"fixture")

    locator = MeterYoloLocator(model_path=str(model), conf_thres=0.20)

    assert locator.ok and locator.provider == "DmlExecutionProvider"
    assert locator.conf_thres == 0.20
    assert captured["providers"] == [
        "DmlExecutionProvider", "CPUExecutionProvider"]
    assert captured["options"].enable_mem_pattern is False
    assert captured["options"].execution_mode == "sequential"


def test_explicit_production_dml_cpu_does_not_select_unrequested_cuda():
    # A packaged runtime may deliberately exclude CUDA. Development launchers
    # with a different installed ORT wheel must provide their own explicit list.
    assert _provider_priority(
        ["CUDAExecutionProvider", "CPUExecutionProvider"], "dml,cpu") == [
            "CPUExecutionProvider"]


def test_explicit_cpu_only_does_not_enable_acceleration():
    assert _provider_priority(
        ["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"],
        "cpu") == ["CPUExecutionProvider"]


def test_canonical_model_precedes_development_candidates(tmp_path):
    canonical = tmp_path / "models" / "orion_meter_detector.onnx"
    n4 = (tmp_path / "runs" / "detect" / "logs" / "diagnostics" /
          "meter_train" / "meter2k27_n4_lowfill" / "weights" / "best.onnx")
    n3 = (tmp_path / "runs" / "detect" / "logs" / "diagnostics" /
          "meter_train" / "meter2k27_n3_pill" / "weights" / "best.onnx")
    for path in (canonical, n4, n3):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(path.name.encode("ascii"))

    assert _resolve_default_model(str(tmp_path)) == str(canonical)
    canonical.unlink()
    # 2026-09-08: n3_pill is the shipped geometry (box h 107 px @720p) every meter-time
    # constant and the session ruler were calibrated against; the 9-epoch n4_lowfill draws
    # boxes +14 px taller with 2-5x the height jitter, so it is the LAST resort, never the
    # first fallback (it was silently promoted to canonical on 09-04 and shifted the ruler).
    assert _resolve_default_model(str(tmp_path)) == str(n3)
    n3.unlink()
    assert _resolve_default_model(str(tmp_path)) == str(n4)


def test_missing_model_reports_stable_canonical_path(tmp_path):
    expected = tmp_path / "models" / "orion_meter_detector.onnx"
    assert _resolve_default_model(str(tmp_path)) == str(expected)
