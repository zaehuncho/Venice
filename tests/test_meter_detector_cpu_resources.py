"""Detector sessions leave CPU capacity for decode, tracking and input work."""
from __future__ import annotations

import sys
import types

import numpy as np
import pytest

import meter_detector_yolo as detector


def _make_locator(monkeypatch, tmp_path, providers):
    captured = {}

    class Options:
        def __init__(self):
            self.intra_op_num_threads = 0
            self.inter_op_num_threads = 0
            self.execution_mode = "default"
            self.enable_mem_pattern = True
            self.config = {}

        def add_session_config_entry(self, name, value):
            self.config[name] = value

    class Session:
        def __init__(self, path, sess_options, providers):
            captured.update(path=path, options=sess_options,
                            providers=list(providers))

        def get_inputs(self):
            return [types.SimpleNamespace(
                name="images", shape=[1, 3, 960, 960], type="tensor(float16)")]

        def get_providers(self):
            return captured["providers"]

    fake_ort = types.SimpleNamespace(
        get_available_providers=lambda: providers,
        SessionOptions=Options,
        InferenceSession=Session,
        ExecutionMode=types.SimpleNamespace(ORT_SEQUENTIAL="sequential"),
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setattr(detector, "cv2", object())
    model = tmp_path / "meter.onnx"
    model.write_bytes(b"fixture")
    locator = detector.MeterYoloLocator(model_path=str(model), conf_thres=0.20)
    assert locator.ok
    return locator, captured


@pytest.mark.parametrize("providers", [
    ["CPUExecutionProvider"],
    ["CUDAExecutionProvider", "CPUExecutionProvider"],
    ["DmlExecutionProvider", "CPUExecutionProvider"],
    ["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"],
])
def test_all_provider_sessions_bound_threads_and_sleep_between_work(
        monkeypatch, tmp_path, providers):
    monkeypatch.delenv("ORION_METER_PROVIDER_PRIORITY", raising=False)
    monkeypatch.delenv("ORION_METER_CPU_THREADS", raising=False)
    monkeypatch.setattr(detector.os, "cpu_count", lambda: 32)
    locator, captured = _make_locator(monkeypatch, tmp_path, providers)

    options = captured["options"]
    assert options.intra_op_num_threads == 2
    assert options.inter_op_num_threads == 1
    assert options.execution_mode == "sequential"
    assert options.config == {
        "session.intra_op.allow_spinning": "0",
        "session.inter_op.allow_spinning": "0",
    }
    assert options.enable_mem_pattern is ("DmlExecutionProvider" not in providers)
    # Resource controls must not trade away detection resolution or precision.
    assert locator.imgsz == 960
    assert locator._in_dtype == np.float16
    assert locator.conf_thres == 0.20
    assert locator.provider == captured["providers"][0]


@pytest.mark.parametrize(("raw", "cpu_count", "expected"), [
    ("1", 32, 1),
    ("2", 32, 2),
    ("4", 32, 4),
    ("999", 32, 4),
    ("0", 32, 1),
    ("-2", 32, 1),
    ("not-an-int", 32, 2),
    ("", 32, 2),
    ("4", 2, 2),
    ("4", 1, 1),
    ("4", None, 1),
])
def test_cpu_thread_override_is_bounded_and_invalid_values_are_usable(
        monkeypatch, tmp_path, raw, cpu_count, expected):
    monkeypatch.setenv("ORION_METER_CPU_THREADS", raw)
    monkeypatch.setattr(detector.os, "cpu_count", lambda: cpu_count)
    _, captured = _make_locator(
        monkeypatch, tmp_path, ["CPUExecutionProvider"])
    assert captured["options"].intra_op_num_threads == expected
