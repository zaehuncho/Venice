"""Fail-first source/fixture guards for the P-B install/update patch.

These deliberately do not launch an installer, updater, service, or product EXE.
The native behavioral tests in OrionUpdaterTests.cpp must also pass after an
isolated build; source guards are not a substitute for that executable proof.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8-sig")


def test_fixed_install_path_and_uninstall_preserves_foreign_files():
    iss = source("installer/orion.iss")
    assert re.search(r"(?m)^DisableDirPage=yes$", iss)
    assert "PrepareToInstall" in iss and "protected install directory" in iss
    assert not re.search(r'(?im)^Type:\s*filesandordirs;\s*Name:\s*"\{app\}"\s*$', iss)


def test_orphaned_stream_and_bridge_service_are_covered():
    iss = source("installer/orion.iss")
    updater = source("native_orion/src/updater_main.cpp")
    assert "Names[6] := 'OrionStream.exe'" in iss
    assert 'venicenetsvc.exe' in updater.lower()
    assert "stopBridgeServiceForUpdate" in updater


def test_elevation_uses_real_write_probe_and_decline_keeps_launcher():
    controller = source("native_orion/src/OrionAppController.cpp")
    section = controller.split("void OrionAppController::startUpdate()", 1)[1].split(
        "void OrionAppController::toggleDefenseMode()", 1
    )[0]
    assert "QTemporaryFile" in section
    assert "QFileInfo(installDir).isWritable()" not in section
    assert "Update: elevation was declined or unavailable" in section
    assert "return;" in section.split("Update: elevation was declined or unavailable", 1)[1].split(
        "#else", 1
    )[0]


def test_stage_is_exact_and_version_and_origin_are_not_caller_controlled():
    updater = source("native_orion/src/updater_main.cpp")
    archive = source("native_orion/src/UpdaterArchive.cpp")
    assert "verifyExactManifestInventory(stageDir, newFiles" in updater
    assert "bool verifyExactManifestInventory(" in archive
    assert "verifiedInstalledVersion(" in updater
    assert "evaluateUpdate(installedVersion, manifest)" in updater
    assert "productionManifestUrlAllowed(" in updater


def test_restore_skips_unchanged_files_and_keeps_recovery_state():
    archive = source("native_orion/src/UpdaterArchive.cpp")
    updater = source("native_orion/src/updater_main.cpp")
    assert "skipIdenticalFiles" in archive
    assert "retrySharingViolation" in archive
    assert "recovery marker" in updater.lower()
    assert "QSaveFile recoveryJournal(recoveryPath)" in updater
    assert updater.index("recoveryJournal.commit()") < updater.index("updater::applyTree(stageDir")


def test_update_manifest_unknown_self_key_is_rejected(tmp_path: Path):
    cryptography = pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    spec = importlib.util.spec_from_file_location(
        "verify_release_integrity_pb", ROOT / "tools/verify_release_integrity.py"
    )
    verifier = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(verifier)
    package_spec = importlib.util.spec_from_file_location(
        "package_orion_release_pb", ROOT / "tools/package_orion_release.py"
    )
    package = importlib.util.module_from_spec(package_spec)
    assert package_spec.loader is not None
    package_spec.loader.exec_module(package)

    key = Ed25519PrivateKey.generate()  # ephemeral fixture only
    payload = package.build_update_manifest(
        version="9.9.9",
        artifact_sha256="ab" * 32,
        artifact_url="https://updates.example.invalid/unit.zip",
        published_at="2026-09-23T00:00:00Z",
    )
    payload["public_key_id"] = "untrusted-fixture-key"
    payload["public_key_b64"] = base64.b64encode(
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    payload["signature_alg"] = "ed25519"
    payload["signature"] = base64.b64encode(
        key.sign(package.canonical_signing_payload(payload))
    ).decode("ascii")
    path = tmp_path / "unknown-key-update.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = verifier.verify_update_manifest(path)
    assert result["ok"] is False
    assert any("public_key_id" in error or "trusted" in error for error in result["errors"])
