"""Regression lock for the contour circularity geometry gate.

The gate is now ON by default with min_circularity=0.05 — permissive enough
to pass thin Straight-style meters (circularity ~0.06) while rejecting
non-circular noise blobs. The threshold must stay at or below 0.05 to avoid
dropping real meters.
"""
import json
from pathlib import Path

import pytest

pytest.importorskip("cv2")

from meter_detector import DetectorConfig, load_detector_config

ROOT = Path(__file__).resolve().parents[1]


def test_geometry_gate_on_by_default():
    # Gate is ON by default for noise rejection.
    assert DetectorConfig().geometry_gate_enabled is True


def test_geometry_gate_threshold_safe():
    # min_circularity must be <= 0.05 to avoid dropping thin real meters.
    assert DetectorConfig().min_circularity <= 0.05


def test_geometry_gate_config_roundtrip(tmp_path):
    # The knob still loads when explicitly set.
    p = tmp_path / "s.json"
    p.write_text(json.dumps(
        {"rejection_filters": {"geometry_gate_enabled": True, "min_circularity": 0.22}}))
    cfg = load_detector_config(str(p))
    assert cfg.geometry_gate_enabled is True
    assert abs(cfg.min_circularity - 0.22) < 1e-6
