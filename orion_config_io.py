"""Shared settings.json locator + loader.

Centralizes the discovery/load that controller_remap and rtt_sync_engine duplicated. Reads with
utf-8-sig so a stray BOM (e.g. a PowerShell-written settings.json) doesn't silently fail json.load
and drop the app to defaults — see the settings-BOM gotcha. All three modules live at the repo root,
so the module-dir search resolves identically to the previous per-caller logic.
"""
from __future__ import annotations

import json
import os


def locate_settings(settings_path: str | None = None) -> str | None:
    """Return the settings.json path: the explicit one if it exists, else the first of
    (this module's dir, %USERPROFILE%/Desktop/NexusVision) that has a settings.json. None if missing."""
    if settings_path and os.path.isfile(settings_path):
        return settings_path
    if settings_path is None:
        base = os.path.dirname(os.path.abspath(__file__))
        desk = os.path.join(os.environ.get('USERPROFILE', os.path.expanduser('~')), 'Desktop', 'NexusVision')
        for d in (base, desk):
            p = os.path.join(d, 'settings.json')
            if os.path.isfile(p):
                return p
    return None


_FLAT_TO_NESTED = {
    'no_meter_enabled': ('no_meter', 'enabled'),
    'no_meter_base_offset_ms': ('no_meter', 'base_offset_ms'),
    'no_meter_release_point': ('no_meter', 'release_point'),
    'no_meter_confidence_gate': ('no_meter', 'confidence_gate'),
    'no_meter_push_release_window_ms': ('no_meter', 'push_release_window_ms'),
    'no_meter_handedness': ('no_meter', 'handedness'),
    'tempo_mode_enabled': ('tempo', 'enabled'),
}


def migrate_flat_keys(data: dict) -> dict:
    """Migrate legacy flat keys into nested form, then delete the flat duplicates.

    settings.json historically had both flat (no_meter_enabled) and nested
    (no_meter.enabled) versions of the same keys. This consolidates to nested
    only, preferring the nested value when both exist (nested is authoritative).
    """
    changed = False
    for flat_key, (section, nested_key) in _FLAT_TO_NESTED.items():
        if flat_key in data:
            section_dict = data.setdefault(section, {})
            if nested_key not in section_dict:
                section_dict[nested_key] = data[flat_key]
            del data[flat_key]
            changed = True
    if changed:
        p = locate_settings()
        if p:
            try:
                with open(p, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4)
            except Exception:
                pass
    return data


def load_settings_raw(settings_path: str | None = None) -> dict | None:
    """Locate + load settings.json as a dict, or None if missing/unreadable. utf-8-sig = BOM-robust."""
    p = locate_settings(settings_path)
    if not p:
        return None
    try:
        with open(p, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = migrate_flat_keys(data)
        return data if isinstance(data, dict) else None
    except Exception:
        return None
