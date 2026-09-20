from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pack_lethe_release_shard_gate",
    ROOT / "tools" / "security" / "pack_lethe_release.py",
)
pack = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pack)


def test_server_shard_release_refuses_before_broker_exists():
    with pytest.raises(ValueError, match="signed outer activation broker"):
        pack.validate_server_shard_release_mode(True)


def test_non_shard_release_validation_is_unchanged():
    assert pack.validate_server_shard_release_mode(False) is None


def test_installer_defaults_to_packed_package():
    source = (ROOT / "installer" / "build_installer.ps1").read_text(encoding="utf-8")
    assert 'Join-Path $Root "release\\orion-package-packed"' in source
    assert 'Join-Path $Root "release\\orion-package"\n' not in source
