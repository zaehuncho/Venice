"""verify_release_integrity.py — the CI pre-ship gate must agree with the native verifier.

Pins that the tool PASSes a clean package, FAILs a tampered one (hash mismatch — the exact native
lock condition), FAILs a secret/key leak even when it is listed in the manifest, and matches the
native path-safety rule. A green run of this tool is the contract for "a prod build won't lock".
"""
import importlib.util
import hashlib
import base64
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v = _load("verify_release_integrity", "tools/verify_release_integrity.py")
pkg = _load("package_orion_release", "tools/package_orion_release.py")


def _write_signed_release_manifest(package_dir: Path) -> str:
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat,
    )

    key = Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(b"orion-release-integrity-test-key").digest()
    )
    key_path = package_dir.parent / f".{package_dir.name}-release-integrity-key.pem"
    key_path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    pkg.write_release_manifest(
        package_dir,
        signing_key=key_path,
        public_key_id="orion-release-test-v1",
    )
    return base64.b64encode(public).decode("ascii")


def _verify_test_release(package_dir: Path, public_key_b64: str):
    return v.verify(package_dir, public_key_b64, "orion-release-test-v1")


def test_clean_package_passes(tmp_path):
    (tmp_path / "OrionNative.exe").write_bytes(b"exe")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "AutomationCore.dll").write_bytes(b"dll")
    public_key = _write_signed_release_manifest(tmp_path)
    r = _verify_test_release(tmp_path, public_key)
    assert r["ok"] is True and r["errors"] == [] and r["checked"] == 2


def test_missing_manifest_fails(tmp_path):
    (tmp_path / "OrionNative.exe").write_bytes(b"exe")
    r = v.verify(tmp_path)
    assert r["ok"] is False and any("missing" in e.lower() for e in r["errors"])


def test_tampered_file_fails_like_native(tmp_path):
    exe = tmp_path / "OrionNative.exe"
    exe.write_bytes(b"original")
    public_key = _write_signed_release_manifest(tmp_path)
    exe.write_bytes(b"patched-by-attacker")  # modify after manifest generation
    r = _verify_test_release(tmp_path, public_key)
    assert r["ok"] is False
    assert any("Hash mismatch" in e for e in r["errors"])


def test_secret_in_package_fails_even_if_manifested(tmp_path):
    (tmp_path / "OrionNative.exe").write_bytes(b"exe")
    (tmp_path / "signing.pfx").write_bytes(b"private-key")
    public_key = _write_signed_release_manifest(tmp_path)  # secret is IN the manifest — hashes fine, still forbidden
    r = _verify_test_release(tmp_path, public_key)
    assert r["ok"] is False
    assert any("Forbidden file" in e and "signing.pfx" in e for e in r["errors"])


def test_unlisted_file_fails_release_package_gate(tmp_path):
    (tmp_path / "OrionNative.exe").write_bytes(b"exe")
    public_key = _write_signed_release_manifest(tmp_path)
    (tmp_path / "snuck_in.dll").write_bytes(b"late addition")  # added AFTER manifest
    r = _verify_test_release(tmp_path, public_key)
    assert r["ok"] is False
    assert any("Unlisted" in e and "snuck_in.dll" in e for e in r["errors"])


def test_path_safety_matches_native_rule():
    assert v.looks_safe_manifest_path("bin/app.exe") is True
    assert v.looks_safe_manifest_path("qml/pages/Main.qml") is True
    assert v.looks_safe_manifest_path("../escape.dll") is False
    assert v.looks_safe_manifest_path("a/../../etc/passwd") is False
    assert v.looks_safe_manifest_path("/abs/path") is False
    assert v.looks_safe_manifest_path("C:/abs/win") is False
    assert v.looks_safe_manifest_path("..") is False


def test_secret_content_in_package_fails(tmp_path):
    """A secret PASTED INTO a legitimately-shipped file (name filters can't see it) is a
    hard verify failure. Fake key assembled by concatenation so this test source never
    contains a secret-shaped literal."""
    (tmp_path / "OrionNative.exe").write_bytes(b"exe")
    (tmp_path / "config.json").write_text('{"k": "' + "AKIA" + "IOSFODNN7EXAMPLE" + '"}', encoding="utf-8")
    public_key = _write_signed_release_manifest(tmp_path)
    r = _verify_test_release(tmp_path, public_key)
    assert r["ok"] is False
    assert any("Secret content" in e and "config.json" in e for e in r["errors"])


def test_missing_release_manifest_signature_fails(tmp_path):
    (tmp_path / "OrionNative.exe").write_bytes(b"exe")
    public_key = _write_signed_release_manifest(tmp_path)
    (tmp_path / "release_manifest.sig").unlink()

    r = _verify_test_release(tmp_path, public_key)
    assert r["ok"] is False
    assert any("signature missing" in error.lower() for error in r["errors"])


def test_exact_manifest_byte_tamper_fails_signature(tmp_path):
    (tmp_path / "OrionNative.exe").write_bytes(b"exe")
    public_key = _write_signed_release_manifest(tmp_path)
    manifest = tmp_path / "release_manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b" ")  # valid JSON, different signed bytes

    r = _verify_test_release(tmp_path, public_key)
    assert r["ok"] is False
    assert any("signature INVALID" in error for error in r["errors"])


# --------------------------------------------------------------------------- #
# signed /api/update manifest verification (native updater acceptance test)
# --------------------------------------------------------------------------- #
def _signed_manifest(tmp_path, **overrides):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    manifest = pkg.build_update_manifest(
        version="1.2.3", artifact_sha256=overrides.pop("artifact_sha256", "ab" * 32),
        artifact_url="https://dl.example/o.zip", published_at="2026-07-09T00:00:00Z",
    )
    signed = dict(manifest)
    signed["signature_alg"] = "ed25519"
    signed["public_key_b64"] = public
    signed["signature"] = base64.b64encode(
        key.sign(pkg.canonical_signing_payload(signed))
    ).decode("ascii")
    signed.update(overrides)
    path = tmp_path / "update_manifest.json"
    path.write_text(json.dumps(signed), encoding="utf-8")
    return path, public


def _verify_fixture_manifest(path, public, artifact=None):
    return v.verify_update_manifest(
        path, artifact, trusted_keys={pkg.ED25519_KEY_ID: public},
    )


def test_update_manifest_valid_signature_passes(tmp_path):
    path, public = _signed_manifest(tmp_path)
    r = _verify_fixture_manifest(path, public)
    assert r["ok"] is True and r["errors"] == []


def test_update_manifest_tampered_field_fails(tmp_path):
    # attacker rewrites artifact_url after signing -> canonical payload changes -> reject
    path, public = _signed_manifest(tmp_path, artifact_url="https://evil.example/o.zip")
    r = _verify_fixture_manifest(path, public)
    assert r["ok"] is False
    assert any("signature INVALID" in e for e in r["errors"])


def test_update_manifest_unsigned_fails(tmp_path):
    manifest = pkg.build_update_manifest(version="1.2.3", artifact_sha256="ab" * 32)
    path = tmp_path / "update_manifest.unsigned.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    r = v.verify_update_manifest(path)
    assert r["ok"] is False
    assert any("unsigned" in e.lower() or "signature missing" in e for e in r["errors"])


def test_update_manifest_artifact_hash_gate(tmp_path):
    import hashlib
    artifact = tmp_path / "orion-package-1.2.3.zip"
    artifact.write_bytes(b"artifact-bytes")
    good = hashlib.sha256(artifact.read_bytes()).hexdigest()

    ok_path, public = _signed_manifest(tmp_path, artifact_sha256=good)
    assert _verify_fixture_manifest(ok_path, public, artifact) == {"ok": True, "errors": []}

    artifact.write_bytes(b"swapped-after-signing")
    r = _verify_fixture_manifest(ok_path, public, artifact)
    assert r["ok"] is False
    assert any("Artifact sha256 mismatch" in e for e in r["errors"])
