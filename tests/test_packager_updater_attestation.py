"""[2026-09-21 Codex r3 F1] The packager binds the updater's attested KEY BYTES and the
attested BINARY BYTES to what ships, not just a key id and a CMake cache line.

Pure-policy tests over tools/package_orion_release.py: no build tree, no exe is run.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pk():
    spec = importlib.util.spec_from_file_location(
        "package_orion_release", ROOT / "tools" / "package_orion_release.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["package_orion_release"] = mod
    spec.loader.exec_module(mod)
    return mod


def _good_profile(pk):
    return {
        "profile": "production",
        "version": "1.0.0",
        "local_manifest_allowed": False,
        "embedded_key_ids": [pk.ED25519_KEY_ID],
        "embedded_keys": [{"id": pk.ED25519_KEY_ID,
                           "sha256": pk.APPROVED_UPDATE_KEYS[pk.ED25519_KEY_ID]}],
    }


def test_approved_fingerprint_is_the_sha256_of_the_pinned_public_key_bytes(pk):
    raw = base64.b64decode(pk.ED25519_PUBLIC_KEY_B64)
    assert len(raw) == 32
    assert pk.APPROVED_UPDATE_KEYS[pk.ED25519_KEY_ID] == hashlib.sha256(raw).hexdigest()


def test_good_production_profile_is_accepted(pk):
    pk.check_updater_build_profile(_good_profile(pk), pk.APPROVED_UPDATE_KEYS)


def test_same_id_over_different_key_bytes_is_refused(pk):
    profile = _good_profile(pk)
    profile["embedded_keys"][0]["sha256"] = "0" * 64
    with pytest.raises(SystemExit, match="DIFFERENT key bytes"):
        pk.check_updater_build_profile(profile, pk.APPROVED_UPDATE_KEYS)


def test_unapproved_secondary_key_is_refused(pk):
    profile = _good_profile(pk)
    profile["embedded_keys"].append({"id": "attacker-rotation-key", "sha256": "1" * 64})
    with pytest.raises(SystemExit, match="unapproved"):
        pk.check_updater_build_profile(profile, pk.APPROVED_UPDATE_KEYS)


def test_missing_fingerprints_or_wrong_profile_is_refused(pk):
    profile = _good_profile(pk)
    del profile["embedded_keys"]
    with pytest.raises(SystemExit, match="no embedded keys"):
        pk.check_updater_build_profile(profile, pk.APPROVED_UPDATE_KEYS)
    profile = _good_profile(pk)
    profile["profile"] = "development"
    with pytest.raises(SystemExit, match="profile='development'"):
        pk.check_updater_build_profile(profile, pk.APPROVED_UPDATE_KEYS)
    profile = _good_profile(pk)
    profile["local_manifest_allowed"] = True
    with pytest.raises(SystemExit, match="manifest-file"):
        pk.check_updater_build_profile(profile, pk.APPROVED_UPDATE_KEYS)


def test_staged_updater_must_be_the_attested_bytes(pk, tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "OrionUpdater.exe").write_bytes(b"attested bytes")
    monkeypatch.setattr(pk, "_ATTESTED_UPDATER_SHA256", hashlib.sha256(b"attested bytes").hexdigest())
    pk.require_staged_updater_matches_attestation(staging)          # identical: accepted
    (staging / "OrionUpdater.exe").write_bytes(b"swapped after attestation")
    with pytest.raises(SystemExit, match="differs from the binary that attested"):
        pk.require_staged_updater_matches_attestation(staging)
    (staging / "OrionUpdater.exe").unlink()
    with pytest.raises(SystemExit, match="no OrionUpdater.exe"):
        pk.require_staged_updater_matches_attestation(staging)
    monkeypatch.setattr(pk, "_ATTESTED_UPDATER_SHA256", None)
    pk.require_staged_updater_matches_attestation(staging)          # dev mode: no attestation, no check


def test_build_profile_attestation_does_not_force_qt_offscreen(pk):
    """The Windows updater's early attestation path must use the native platform.

    QT's offscreen plugin can hang before main reaches writeBuildProfile, causing
    the production packaging gate to time out even though the signed updater is
    valid.  Keep the subprocess environment sanitization pinned by policy.
    """
    source = inspect.getsource(pk.require_production_updater_binary)
    assert 'env.pop("QT_QPA_PLATFORM", None)' in source
    assert 'env["QT_QPA_PLATFORM"] = "offscreen"' not in source
