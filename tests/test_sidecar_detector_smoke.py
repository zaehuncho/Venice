"""Compiled-sidecar detector provider/performance gate."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "autogreen_sidecar_smoke",
    ROOT / "native_orion" / "backend" / "autogreen_sidecar.py",
)
sidecar = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(sidecar)


def _install_detector(monkeypatch, provider):
    class Detector:
        ok = True

        def __init__(self, model_path):
            self.model_path = model_path
            self.provider = provider

        def detect_box(self, _frame):
            return None

    monkeypatch.setitem(
        sys.modules,
        "meter_detector_yolo",
        SimpleNamespace(MeterYoloLocator=Detector),
    )


def test_detector_smoke_accepts_fast_directml_and_writes_machine_result(
    monkeypatch, tmp_path
):
    _install_detector(monkeypatch, "DmlExecutionProvider")
    result = tmp_path / "smoke.json"

    code = sidecar._detector_provider_smoke(
        tmp_path / "orion_meter_detector.onnx",
        warmup=0,
        runs=3,
        output_path=result,
    )

    payload = json.loads(result.read_text(encoding="utf-8"))
    assert code == 0
    assert payload["ok"] is True
    assert payload["provider"] == "DmlExecutionProvider"
    assert payload["p90_ms"] <= 35.0


def test_detector_smoke_rejects_silent_cpu_fallback(monkeypatch, tmp_path):
    _install_detector(monkeypatch, "CPUExecutionProvider")
    result = tmp_path / "smoke.json"

    code = sidecar._detector_provider_smoke(
        tmp_path / "orion_meter_detector.onnx",
        warmup=0,
        runs=1,
        output_path=result,
    )

    assert code == 5
    assert json.loads(result.read_text(encoding="utf-8"))["ok"] is False

