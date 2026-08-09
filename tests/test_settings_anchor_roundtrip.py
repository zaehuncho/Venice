"""Settings round-trip for the trained template anchor (C++ Train button -> Python detector).

The in-launcher Train button (OrionAppController::trainMeterAnchor) writes a
``meter_template_anchor`` object (and optional ``meter_trained_green_hsv_*`` arrays)
into settings.json via the signed ``AppConfig::save()`` path. This test pins the
EXACT JSON shape the C++ emits and asserts the Python loader + ``_TemplateAnchor``
consume it — the cross-language contract. It also guards the no-BOM requirement
(the Python loader opens settings.json as plain utf-8).
"""
import json
import os
import tempfile

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from meter_detector import load_detector_config, _TemplateAnchor


def _write_template(cal_dir):
    os.makedirs(cal_dir, exist_ok=True)
    # A distinctive high-contrast glyph crop, like the Train button would save.
    glyph = np.full((30, 34, 3), 40, np.uint8)
    cv2.rectangle(glyph, (0, 0), (33, 29), (245, 245, 245), -1)
    for off in (5, 13, 21):
        cv2.rectangle(glyph, (6, off), (28, off + 5), (10, 10, 10), -1)
    path = os.path.join(cal_dir, "meter_anchor.png")
    cv2.imwrite(path, glyph)
    return path


# The verbatim object AppConfig::save() writes (see OrionAppController::trainMeterAnchor).
ANCHOR_SETTINGS = {
    "meter_color": "Purple",
    "meter_template_anchor": {
        "enabled": True,
        "template_path": "calibration/meter_anchor.png",
        "search_band": {"x0": 0.10, "y0": 0.05, "x1": 0.35, "y1": 0.30},
        "meter_offset": {"dx": -120, "dy": 40, "w": 18, "h": 120},
        "match_confidence_min": 0.6,
        "ref_wh": [960, 540],
    },
    "meter_trained_green_hsv_low": [40, 80, 80],
    "meter_trained_green_hsv_high": [80, 255, 255],
}


def test_anchor_settings_roundtrip_loads_into_config():
    td = tempfile.mkdtemp(prefix="orion_settings_")
    settings_path = os.path.join(td, "settings.json")
    # No BOM (C++ QJsonDocument::toJson writes plain utf-8).
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(ANCHOR_SETTINGS, f, indent=2)
    _write_template(os.path.join(td, "calibration"))

    cfg = load_detector_config(settings_path)

    assert cfg.template_anchor_enabled is True
    assert cfg.template_anchor_path == "calibration/meter_anchor.png"
    assert cfg.template_anchor_offset == [-120, 40, 18, 120]
    assert cfg.template_anchor_search_band == [0.10, 0.05, 0.35, 0.30]
    assert cfg.template_anchor_min_score == pytest.approx(0.6)
    assert cfg.template_anchor_ref_wh == [960, 540]
    # The green-window half of Train.
    assert cfg.trained_green_hsv_low == [40, 80, 80]
    assert cfg.trained_green_hsv_high == [80, 255, 255]


def test_anchor_arms_from_roundtripped_settings():
    td = tempfile.mkdtemp(prefix="orion_settings_")
    settings_path = os.path.join(td, "settings.json")
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(ANCHOR_SETTINGS, f, indent=2)
    _write_template(os.path.join(td, "calibration"))

    cfg = load_detector_config(settings_path)
    # template_path is relative -> resolves against the settings dir.
    anchor = _TemplateAnchor(cfg, base_dirs=[td])
    assert anchor.enabled is True


def test_no_bom_required_and_disabled_when_absent():
    td = tempfile.mkdtemp(prefix="orion_settings_")
    settings_path = os.path.join(td, "settings.json")
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump({"meter_color": "Purple"}, f)
    cfg = load_detector_config(settings_path)
    assert cfg.template_anchor_enabled is False
    assert _TemplateAnchor(cfg).enabled is False
