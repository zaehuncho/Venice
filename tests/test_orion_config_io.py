"""Tests for the shared settings loader (orion_config_io)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orion_config_io import load_settings_raw, locate_settings


def test_load_normal(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"a": 1, "b": "x"}), encoding="utf-8")
    raw = load_settings_raw(str(p))
    assert raw == {"a": 1, "b": "x"}


def test_load_bom_robust(tmp_path):
    # a BOM-prefixed file (e.g. PowerShell-written) must still parse, not silently drop to defaults
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"meter_enabled": True}), encoding="utf-8-sig")
    raw = load_settings_raw(str(p))
    assert raw == {"meter_enabled": True}


def test_missing_returns_none(tmp_path):
    assert load_settings_raw(str(tmp_path / "nope.json")) is None


def test_invalid_json_returns_none(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert load_settings_raw(str(p)) is None


def test_non_dict_returns_none(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert load_settings_raw(str(p)) is None


def test_locate_explicit(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{}", encoding="utf-8")
    assert locate_settings(str(p)) == str(p)
    assert locate_settings(str(tmp_path / "missing.json")) is None
