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


def test_server_shard_gate_fails_closed_without_a_verified_chain():
    # FIX #1: the gate now VERIFIES the assembled chain rather than blocking forever.
    # Requesting shard mode without a verified assembled stage still fails closed.
    with pytest.raises(ValueError, match="FAIL-CLOSED"):
        pack.validate_server_shard_release_mode(True)


def test_non_shard_release_validation_is_unchanged():
    assert pack.validate_server_shard_release_mode(False) is None


def test_installer_defaults_to_packed_package():
    source = (ROOT / "installer" / "build_installer.ps1").read_text(encoding="utf-8")
    assert 'Join-Path $Root "release\\orion-package-packed"' in source
    assert 'Join-Path $Root "release\\orion-package"\n' not in source


def test_installer_points_launch_and_protocol_at_broker_for_shard():
    iss = (ROOT / "installer" / "orion.iss").read_text(encoding="utf-8")
    # The server-shard launch entry point, shortcuts, and orion:// handler must be the
    # broker, not the packed inner OrionNative.exe.
    assert '#define LaunchExeName "OrionActivate.exe"' in iss
    assert 'RegisterActivationBroker' in iss
    assert 'Filename: "{app}\\{#LaunchExeName}"' in iss
    # A shard package must ship the packed payload + signed manifest beside the broker.
    assert "OrionNative.packed.exe" in iss
    assert "release_manifest.sig" in iss


def test_installer_build_requires_shard_payload_when_broker_present():
    build = (ROOT / "installer" / "build_installer.ps1").read_text(encoding="utf-8")
    assert "OrionActivate.exe" in build
    assert "OrionNative.packed.exe" in build
