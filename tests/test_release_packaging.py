"""Release-packaging security — the launch-critical integrity chain, previously untested.

Two contracts are pinned here:
  1. write_release_manifest() emits EXACTLY the `orion.release_manifest.v1` schema that the native
     SecurityManager::verifyReleaseIntegrity() parses (native_orion/src/SecurityManager.cpp:355):
     a `schema` string, a `files` map of safe relative POSIX paths -> {sha256,size}, 64-char
     lowercase hex, the manifest self-excluded. A drift here makes a PRODUCTION build fail closed
     on startup (releaseManifestRequired() is hard-true in prod), so it is a hard launch gate.
  2. scan_forbidden() keeps secrets / private keys / dev launchers / RE material OUT of a shipped
     package, while leaving the legitimate runtime (exe / dll / certifi cacert.pem) untouched.

Offline + hermetic: operates on a tmp fixture, never runs the real GB-scale packaging.
"""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "package_orion_release", _ROOT / "tools" / "package_orion_release.py"
)
pkg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pkg)


def _release_test_key(package_dir: Path):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat,
    )

    key = Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(b"orion-release-manifest-test-key").digest()
    )
    key_path = package_dir.parent / f".{package_dir.name}-release-test-key.pem"
    key_path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return key_path, public


def _write_signed_release_manifest(package_dir: Path, version: str | None = None):
    key_path, public = _release_test_key(package_dir)
    manifest = pkg.write_release_manifest(
        package_dir,
        version=version,
        signing_key=key_path,
        public_key_id="orion-release-test-v1",
    )
    return manifest, public


# --------------------------------------------------------------------------- #
# 1. manifest <-> SecurityManager schema contract
# --------------------------------------------------------------------------- #
def test_customer_package_strips_privileged_executables_before_manifest(tmp_path):
    (tmp_path / "OrionNative.exe").write_bytes(b"customer")
    for name in pkg.CUSTOMER_EXCLUDED_FILES:
        (tmp_path / name).write_bytes(b"staff-only")

    pkg.strip_customer_excluded_files(tmp_path)
    manifest, _public = _write_signed_release_manifest(tmp_path)

    assert manifest["audience"] == "customer"
    assert not (pkg.CUSTOMER_EXCLUDED_FILES & set(manifest["files"]))
    assert all(not (tmp_path / name).exists() for name in pkg.CUSTOMER_EXCLUDED_FILES)


def test_manifest_matches_securitymanager_schema(tmp_path):
    (tmp_path / "OrionNative.exe").write_bytes(b"the-exe")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "AutomationCore.dll").write_bytes(b"the-dll")

    _write_signed_release_manifest(tmp_path)
    manifest = json.loads((tmp_path / "release_manifest.json").read_text(encoding="utf-8"))

    # native parser: SecurityManager.cpp:373 requires this exact schema string.
    assert manifest["schema"] == "orion.release_manifest.v1"
    files = manifest["files"]
    assert files, "manifest must list files (empty -> native reports 'no files' -> lock)"

    # self-excluded (the manifest can't hash itself).
    assert "release_manifest.json" not in files
    assert "release_manifest.sig" not in files
    assert manifest["signature_required"] is True
    assert manifest["signature_alg"] == "ed25519"
    assert manifest["public_key_id"] == "orion-release-test-v1"
    # relative POSIX paths for every shipped file.
    assert "OrionNative.exe" in files
    assert "sub/AutomationCore.dll" in files

    for rel, meta in files.items():
        # looksSafeManifestPath() (SecurityManager.cpp): relative, no traversal.
        assert not rel.startswith("/")
        assert ".." not in rel.split("/")
        assert "\\" not in rel, "paths must be POSIX for the native fromNativeSeparators() round-trip"
        digest = meta["sha256"]
        assert len(digest) == 64, "native rejects any hash whose length != 64"
        assert digest == digest.lower(), "native lower-cases the stored hash before comparing"
        int(digest, 16)  # must be valid hex

    # hashes are correct (a lie here would lock a genuine build).
    assert files["OrionNative.exe"]["sha256"] == hashlib.sha256(b"the-exe").hexdigest()
    assert files["sub/AutomationCore.dll"]["sha256"] == hashlib.sha256(b"the-dll").hexdigest()


def test_manifest_hash_is_tamper_evident(tmp_path):
    """The whole point: modifying a shipped file after manifest generation must change its hash
    (so the native verifier's recompute != stored -> lock)."""
    exe = tmp_path / "OrionNative.exe"
    exe.write_bytes(b"original")
    _write_signed_release_manifest(tmp_path)
    stored = json.loads((tmp_path / "release_manifest.json").read_text())["files"]["OrionNative.exe"]["sha256"]
    exe.write_bytes(b"tampered")  # attacker patches the binary
    assert hashlib.sha256(exe.read_bytes()).hexdigest() != stored


def test_release_manifest_signature_covers_exact_bytes_and_is_deterministic(tmp_path):
    pytest.importorskip("cryptography")
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    (tmp_path / "OrionNative.exe").write_bytes(b"the-exe")
    key_path, public = _release_test_key(tmp_path)
    pkg.write_release_manifest(
        tmp_path,
        version="1.2.3",
        signing_key=key_path,
        public_key_id="orion-release-test-v1",
    )
    manifest_bytes = (tmp_path / pkg.RELEASE_MANIFEST_NAME).read_bytes()
    signature_path = tmp_path / pkg.RELEASE_MANIFEST_SIG_NAME
    encoded_once = signature_path.read_bytes()
    signature = pkg.decode_signature_bytes(encoded_once.decode("ascii"))
    Ed25519PublicKey.from_public_bytes(public).verify(signature, manifest_bytes)

    # Signing unchanged bytes again must emit byte-identical output.
    pkg.write_release_manifest_signature(tmp_path, key_path)
    assert signature_path.read_bytes() == encoded_once

    # Even semantically irrelevant whitespace changes the exact signed bytes.
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, manifest_bytes + b" ")
        raise AssertionError("modified manifest bytes must not verify")
    except InvalidSignature:
        pass


def test_unsigned_release_manifest_requires_explicit_dev_mode(tmp_path):
    (tmp_path / "OrionNative.exe").write_bytes(b"the-exe")
    with pytest.raises(SystemExit, match="requires an Ed25519 signing key"):
        pkg.write_release_manifest(tmp_path)

    manifest = pkg.write_release_manifest(tmp_path, allow_unsigned_dev=True)
    assert manifest["signature_required"] is False
    assert not (tmp_path / pkg.RELEASE_MANIFEST_SIG_NAME).exists()


def test_sidecar_source_override_is_explicit_dev_only(monkeypatch, tmp_path):
    monkeypatch.setenv("ORION_SIDECAR_DIST", str(tmp_path / "environment-injection"))
    assert pkg.sidecar_dist_dir() == pkg.SIDECAR_DIST_DEFAULT.resolve()
    with pytest.raises(SystemExit, match="requires --allow-dev-build"):
        pkg.sidecar_dist_dir(dev_override=tmp_path)
    assert pkg.sidecar_dist_dir(
        allow_dev_build=True, dev_override=tmp_path
    ) == tmp_path.resolve()


def test_compiled_sidecar_collision_is_a_hard_failure(monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    package = tmp_path / "package"
    dist.mkdir()
    package.mkdir()
    (dist / "OrionSidecar.exe").write_bytes(b"sidecar")
    (dist / "SecurityCore.dll").write_bytes(b"sidecar-copy")
    (dist / pkg.SIDECAR_BUILD_MANIFEST_NAME).write_text("{}", encoding="utf-8")
    (package / "SecurityCore.dll").write_bytes(b"native-runtime")
    monkeypatch.setattr(pkg, "verify_sidecar_build_manifest", lambda _root, _dist: [])

    with pytest.raises(SystemExit, match="overwrite existing runtime files"):
        pkg.copy_compiled_sidecar(package, dist)


def test_compiled_sidecar_forbidden_inventory_is_not_silently_filtered(monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    package = tmp_path / "package"
    dist.mkdir()
    package.mkdir()
    (dist / "OrionSidecar.exe").write_bytes(b"sidecar")
    (dist / "debug.pdb").write_bytes(b"symbols")
    (dist / pkg.SIDECAR_BUILD_MANIFEST_NAME).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pkg, "verify_sidecar_build_manifest", lambda _root, _dist: [])

    with pytest.raises(SystemExit, match="bundle contains files"):
        pkg.copy_compiled_sidecar(package, dist)


def test_compiled_sidecar_postcopy_must_match_v2_files_map(monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    package = tmp_path / "package"
    dist.mkdir()
    package.mkdir()
    executable = dist / "OrionSidecar.exe"
    executable.write_bytes(b"sidecar")
    (dist / pkg.SIDECAR_BUILD_MANIFEST_NAME).write_text(json.dumps({
        "files": {
            "OrionSidecar.exe": {"sha256": "00" * 32, "size": executable.stat().st_size},
        },
    }), encoding="utf-8")
    monkeypatch.setattr(pkg, "verify_sidecar_build_manifest", lambda _root, _dist: [])

    with pytest.raises(SystemExit, match="does not match its v2 files map"):
        pkg.copy_compiled_sidecar(package, dist)


# --------------------------------------------------------------------------- #
# 1c. compiled packet-bridge service + WinDivert driver (inbound meter delay)
#     WAVE 3 (2026-08-08): the C++ VeniceNetSvc.exe (native_orion/venicenet_service)
#     replaced the Nuitka nexus_svc.py bundle. The staging source is a CMake build
#     OUTPUT dir — it also contains logs and test exes that must never ship.
# --------------------------------------------------------------------------- #
def _write_fake_service_bundle(dist: Path, *, with_windivert: bool = True) -> None:
    (dist).mkdir(parents=True, exist_ok=True)
    (dist / pkg.VENICE_SERVICE_EXE).write_bytes(b"compiled-service")
    # Real neighbours in native_orion/build/venicenet_service/Release that the
    # allow-list staging must LEAVE BEHIND (owner-rig log, unit-test client).
    (dist / "venicenet_svc.log").write_bytes(b"owner-rig log lines")
    (dist / "OrionVeniceNetServiceTests.exe").write_bytes(b"test-client")
    if with_windivert:
        # The venicenet_service CMake POST_BUILD stages the pair next to the exe.
        (dist / "WinDivert64.dll").write_bytes(b"windivert-dll")
        (dist / "WinDivert64.sys").write_bytes(b"windivert-sys")


def test_copy_compiled_service_ships_exe_windivert_and_notice(tmp_path):
    dist = tmp_path / "venicenet_service_release"
    _write_fake_service_bundle(dist)
    package = tmp_path / "package"
    package.mkdir()

    pkg.copy_compiled_service(package, dist)

    bridge = package / pkg.PACKET_BRIDGE_SUBDIR
    # The elevated host and the WinDivert pair land in the isolated subdir.
    assert (bridge / pkg.VENICE_SERVICE_EXE).read_bytes() == b"compiled-service"
    assert (bridge / "WinDivert64.dll").read_bytes() == b"windivert-dll"
    assert (bridge / "WinDivert64.sys").read_bytes() == b"windivert-sys"
    # LGPL attribution ships with the driver.
    assert "WinDivert" in (bridge / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    # Nothing the release scan would reject.
    assert pkg.scan_forbidden(package) == []


def test_copy_compiled_service_requires_the_compiled_exe(tmp_path):
    dist = tmp_path / "venicenet_service_release"
    dist.mkdir()
    (dist / "WinDivert64.dll").write_bytes(b"windivert-dll")  # build dir without the exe
    package = tmp_path / "package"
    package.mkdir()
    with pytest.raises(SystemExit, match="VeniceNetSvc.exe is required"):
        pkg.copy_compiled_service(package, dist)


def test_copy_compiled_service_requires_windivert_driver(monkeypatch, tmp_path):
    dist = tmp_path / "venicenet_service_release"
    _write_fake_service_bundle(dist, with_windivert=False)
    # Deny the vendor-override fallback too, so the driver is genuinely absent.
    monkeypatch.setattr(pkg, "WINDIVERT_VENDOR_DIR", tmp_path / "no-windivert-here")
    package = tmp_path / "package"
    package.mkdir()
    with pytest.raises(SystemExit, match="WinDivert driver pair"):
        pkg.copy_compiled_service(package, dist)


def test_copy_compiled_service_never_ships_build_dir_strays(tmp_path):
    """The service staging is an explicit allow-list over a CMake OUTPUT dir. The
    owner-rig service log, the unit-test client, and debug symbols all live next to
    the exe — none of them may ride into the package (the Nuitka-era recursive copy
    would have refused the whole bundle; the allow-list simply leaves strays behind)."""
    dist = tmp_path / "venicenet_service_release"
    _write_fake_service_bundle(dist)
    (dist / "VeniceNetSvc.pdb").write_bytes(b"symbols")  # debug symbols must never ship
    package = tmp_path / "package"
    package.mkdir()

    pkg.copy_compiled_service(package, dist)

    bridge = package / pkg.PACKET_BRIDGE_SUBDIR
    assert not (bridge / "venicenet_svc.log").exists()
    assert not (bridge / "OrionVeniceNetServiceTests.exe").exists()
    assert not (bridge / "VeniceNetSvc.pdb").exists()
    # Exactly the exe + driver pair + the licence notice, nothing else.
    shipped = sorted(p.name for p in bridge.iterdir())
    assert shipped == [
        "THIRD_PARTY_NOTICES.txt",
        pkg.VENICE_SERVICE_EXE,
        "WinDivert64.dll",
        "WinDivert64.sys",
    ]
    assert pkg.scan_forbidden(package) == []


def test_copy_runtime_ships_the_packet_bridge_service(monkeypatch, tmp_path):
    """Revert-trace: copy_runtime wires in the compiled bridge, so a packaged
    layout contains packet_bridge/VeniceNetSvc.exe + WinDivert. Before this
    change copy_runtime never produced that payload at all."""
    real_copy_service = pkg.copy_compiled_service  # capture BEFORE the helper stubs it
    root, _build = _configure_minimal_runtime(monkeypatch, tmp_path)
    # Undo the helper's stub for THIS test — we want the real service copy to run.
    monkeypatch.setattr(pkg, "copy_compiled_service", real_copy_service)
    dist = tmp_path / "sidecar.dist"
    _write_fake_sidecar_bundle(dist)
    monkeypatch.setattr(pkg, "verify_sidecar_build_manifest", lambda _root, _dist: [])
    service_dist = tmp_path / "venicenet_service_release"
    _write_fake_service_bundle(service_dist)

    package = tmp_path / "package"
    pkg.copy_runtime(dist, package_dir=package, service_dist=service_dist)

    bridge = package / pkg.PACKET_BRIDGE_SUBDIR
    assert (bridge / pkg.VENICE_SERVICE_EXE).is_file()
    assert (bridge / "WinDivert64.sys").is_file()
    # And the required-files gate for publish now covers them.
    for rel in pkg.COMPILED_SERVICE_REQUIRED_FILES:
        assert (package / rel).is_file(), rel
    assert pkg.scan_forbidden(package) == []


def test_packaged_libcrypto_must_match_build_pin(monkeypatch, tmp_path):
    release = tmp_path / "build" / "Release"
    release.mkdir(parents=True)
    runtime = release / "libcrypto-3-x64.dll"
    runtime.write_bytes(b"pinned-libcrypto-bytes")
    digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
    (release.parent / "CMakeCache.txt").write_text(
        f"ORION_LIBCRYPTO_SHA256:STRING={digest}\n", encoding="utf-8"
    )
    monkeypatch.setattr(pkg, "BUILD_DIR", release)
    pkg.require_pinned_production_libcrypto()

    runtime.write_bytes(b"replaced-after-build")
    with pytest.raises(SystemExit, match="does not match"):
        pkg.require_pinned_production_libcrypto()


# --------------------------------------------------------------------------- #
# 2. forbidden-file scan
# --------------------------------------------------------------------------- #
def test_scan_forbidden_catches_secrets_keys_and_dev_artifacts(tmp_path):
    # --- legitimate runtime: must NEVER be flagged ---
    (tmp_path / "OrionNative.exe").write_bytes(b"x")
    (tmp_path / "Qt6Core.dll").write_bytes(b"x")
    (tmp_path / "cacert.pem").write_bytes(b"x")   # certifi TLS trust store — legitimately ships
    (tmp_path / "release_manifest.json").write_bytes(b"{}")
    (tmp_path / "security_policy.json").write_bytes(b"{}")

    # --- forbidden: every one must be caught ---
    (tmp_path / "signing.pfx").write_bytes(b"x")            # PKCS#12 private key
    (tmp_path / "client.p12").write_bytes(b"x")
    (tmp_path / "run_orion.local.ps1").write_bytes(b"x")   # dev launcher (test license + dev flags)
    (tmp_path / "Launch Orion.local.bat").write_bytes(b"x")
    (tmp_path / "settings.json.local").write_bytes(b"x")
    (tmp_path / "settings.json").write_bytes(b"x")
    (tmp_path / "learning.json").write_bytes(b"x")
    (tmp_path / "auth_tokens.json").write_bytes(b"x")
    (tmp_path / ".env").write_bytes(b"x")
    (tmp_path / "OrionNative.pdb").write_bytes(b"x")
    (tmp_path / "codesigning").mkdir()
    (tmp_path / "codesigning" / "cert.spc").write_bytes(b"x")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "m.cpython-311.pyc").write_bytes(b"x")

    findings = set(pkg.scan_forbidden(tmp_path))

    for legit in ("OrionNative.exe", "Qt6Core.dll", "cacert.pem",
                  "release_manifest.json", "security_policy.json"):
        assert legit not in findings, f"legitimate runtime file {legit!r} was wrongly flagged"

    for bad in ("signing.pfx", "client.p12", "run_orion.local.ps1", "Launch Orion.local.bat",
                "settings.json.local", "settings.json", "learning.json", "auth_tokens.json",
                ".env", "OrionNative.pdb"):
        assert bad in findings, f"forbidden file {bad!r} was NOT caught by scan_forbidden"

    # dir-component matches (codesigning/, __pycache__/) flag their contents.
    assert any("codesigning" in f for f in findings)
    assert any(f.endswith(".pyc") or "__pycache__" in f for f in findings)


def test_scan_forbidden_clean_package_has_no_findings(tmp_path):
    """A package with only legitimate runtime files scans clean (no false positives that would
    make --strict packaging refuse a valid build)."""
    (tmp_path / "OrionNative.exe").write_bytes(b"x")
    (tmp_path / "AutomationCore.dll").write_bytes(b"x")
    (tmp_path / "python311.dll").write_bytes(b"x")
    (tmp_path / "cacert.pem").write_bytes(b"x")
    (tmp_path / "meter_styles").mkdir()
    (tmp_path / "meter_styles" / "Arrow2.json").write_bytes(b"{}")
    (tmp_path / "release_manifest.json").write_bytes(b"{}")
    assert pkg.scan_forbidden(tmp_path) == []


def test_scan_forbidden_catches_stale_backup_binaries(tmp_path):
    """Regression for the real chiaki-ng deploy tree: OrionStream.exe.bak / .exe.tier2 /
    .exe.running-old / .exe.bak-<stamp> are stale UNVERIFIED executables that used to be
    copied into the package by copy_tree. All must be flagged; a Windows SxS
    .exe.manifest must NOT be."""
    (tmp_path / "OrionStream.exe").write_bytes(b"x")           # legit
    (tmp_path / "OrionStream.exe.manifest").write_bytes(b"x")  # legit SxS manifest
    stale = [
        "OrionStream.exe.bak",
        "OrionStream.exe.bak-20260617-1834",
        "OrionStream.exe.prehwfix.bak",
        "OrionStream.exe.running-old",
        "OrionStream.exe.tier2",
        "VisionCore.dll.old",
        "notes.bak",
        "signing.key",
        "debug.log",
    ]
    for name in stale:
        (tmp_path / name).write_bytes(b"x")

    findings = set(pkg.scan_forbidden(tmp_path))
    assert "OrionStream.exe" not in findings
    assert "OrionStream.exe.manifest" not in findings
    for name in stale:
        assert name in findings, f"stale/backup artifact {name!r} was NOT caught"


def test_canonical_filter_is_case_insensitive_and_complete():
    forbidden_names = (
        "Get-PSN-AccountID.bat",
        "GET-PSN-ACCOUNTID.BAT",
        "artifact.backup",
        "artifact.dmp",
        "artifact.old",
        "artifact.orig",
        "artifact.ppk",
        "artifact.sqlite3",
        "artifact.tmp",
    )

    assert all(name == name.lower() for name in pkg.FORBIDDEN_NAMES)
    assert all(pkg.is_forbidden_file_name(name) for name in forbidden_names)
    assert not pkg.is_forbidden_file_name("cacert.pem")


def test_copy_filters_agree_with_scan(tmp_path):
    """The copy phase must never be looser than the post-package scan: anything
    scan_forbidden would flag is refused by should_copy_top_file and dropped by copy_tree."""
    # top-level copy filter: forbidden .json/.env/dev launchers are refused even though
    # the filter otherwise accepts any top-level .json.
    for bad in ("settings.json", "auth_tokens.json", "learning.json", ".env",
                "run_orion.local.ps1", "OrionStream.exe.bak"):
        assert pkg.should_copy_top_file(tmp_path / bad) is False, f"{bad!r} would be copied"
    for good in ("OrionNative.exe", "SecurityCore.dll", "meter_config.json"):
        assert pkg.should_copy_top_file(tmp_path / good) is True, f"{good!r} would be dropped"

    # recursive copy filter: forbidden files and dirs are dropped in-flight.
    src = tmp_path / "src"
    (src / "codesigning").mkdir(parents=True)
    (src / "codesigning" / "cert.pfx").write_bytes(b"x")
    (src / "OrionStream.exe").write_bytes(b"x")
    (src / "OrionStream.exe.bak").write_bytes(b"x")
    (src / ".env").write_bytes(b"x")
    (src / "qml").mkdir()
    (src / "qml" / "Thing.qml").write_bytes(b"x")
    dst = tmp_path / "dst"
    pkg.copy_tree(src, dst)
    copied = {p.relative_to(dst).as_posix() for p in dst.rglob("*")}
    assert "OrionStream.exe" in copied and "qml/Thing.qml" in copied
    assert not any("codesigning" in c or c.endswith((".bak", ".env")) for c in copied)
    assert pkg.scan_forbidden(dst) == []


def test_recursive_copy_drops_every_observed_chiaki_backup_name(tmp_path):
    source = tmp_path / "chiaki-runtime"
    destination = tmp_path / "package-runtime"
    source.mkdir()
    (source / "OrionStream.exe").write_bytes(b"current")
    stale = {
        "OrionStream.exe.bak",
        "OrionStream.exe.bak-20260617-preoccfix",
        "OrionStream.exe.prehwfix.bak",
        "OrionStream.exe.running-old",
        "OrionStream.exe.tier2",
    }
    for name in stale:
        (source / name).write_bytes(b"stale")

    pkg.copy_tree(source, destination)

    assert (destination / "OrionStream.exe").read_bytes() == b"current"
    assert all(not (destination / name).exists() for name in stale)
    assert pkg.scan_forbidden(destination) == []


def _write_publishable_stage(stage: Path, *, signed: bool = True) -> None:
    (stage / "OrionSidecar.exe").write_bytes(b"compiled-sidecar")
    (stage / "ORION_SIDECAR_BUILD.json").write_text('{"schema":"test"}', encoding="utf-8")
    (stage / "security_policy.json").write_text('{"schema":"test"}', encoding="utf-8")
    (stage / pkg.RELEASE_MANIFEST_NAME).write_text('{"schema":"test"}', encoding="utf-8")
    # The packet-bridge host + WinDivert driver are publish-required too (a package
    # missing them ships a Meter Delay toggle that cannot engage).
    for rel in pkg.COMPILED_SERVICE_REQUIRED_FILES:
        target = stage / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"packet-bridge")
    if signed:
        (stage / pkg.RELEASE_MANIFEST_SIG_NAME).write_text("test-signature", encoding="ascii")


def test_incomplete_stage_never_replaces_existing_package(tmp_path):
    package = tmp_path / "orion-package"
    archive = tmp_path / "archive"
    package.mkdir()
    (package / "known-good.txt").write_text("preserve", encoding="utf-8")
    stage = pkg.create_package_staging_dir(package)
    (stage / "OrionStream.exe.bak-20260731").write_bytes(b"stale")

    with pytest.raises(SystemExit, match="staged package is incomplete"):
        pkg.publish_staged_package(stage, package, archive, require_signature=True)

    assert (package / "known-good.txt").read_text(encoding="utf-8") == "preserve"
    assert not archive.exists()
    pkg.cleanup_package_staging_dir(stage, package)
    assert not stage.exists()


def test_staging_cleanup_refuses_any_non_packager_tree(tmp_path):
    package = tmp_path / "orion-package"
    source_tree = tmp_path / "source"
    source_tree.mkdir()
    (source_tree / "keep.py").write_text("# user source\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Refusing non-packager staging path"):
        pkg.cleanup_package_staging_dir(source_tree, package)

    assert (source_tree / "keep.py").is_file()


def test_production_key_refusal_leaves_existing_package_and_no_stage(monkeypatch, tmp_path):
    package = tmp_path / "orion-package"
    package.mkdir()
    (package / "prior-runtime.txt").write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(pkg, "PACKAGE_DIR", package)
    monkeypatch.setattr(pkg, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(pkg, "require_production_build", lambda: None)
    monkeypatch.delenv(pkg.SIGNING_KEY_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["package_orion_release.py", "--skip-archive"])

    with pytest.raises(SystemExit, match="production packaging requires --signing-key"):
        pkg.main()

    assert (package / "prior-runtime.txt").read_text(encoding="utf-8") == "preserve"
    assert not list(tmp_path.glob(".orion-package.staging-*"))


def test_publish_swap_archives_prior_package_only_after_stage_is_valid(tmp_path):
    package = tmp_path / "orion-package"
    archive = tmp_path / "archive"
    package.mkdir()
    (package / "prior-runtime.txt").write_text("old", encoding="utf-8")
    stage = pkg.create_package_staging_dir(package)
    _write_publishable_stage(stage)

    archived = pkg.publish_staged_package(stage, package, archive, require_signature=True)

    assert archived is not None
    assert not stage.exists()
    assert (package / "OrionSidecar.exe").is_file()
    assert (package / pkg.RELEASE_MANIFEST_SIG_NAME).is_file()
    assert not (package / "prior-runtime.txt").exists()
    assert (archived / "prior-runtime.txt").read_text(encoding="utf-8") == "old"
    assert (archived / "ARCHIVE_MANIFEST.json").is_file()


def test_failed_publish_swap_restores_prior_package(monkeypatch, tmp_path):
    package = (tmp_path / "orion-package").resolve()
    archive = (tmp_path / "archive").resolve()
    package.mkdir()
    (package / "prior-runtime.txt").write_text("old", encoding="utf-8")
    stage = pkg.create_package_staging_dir(package)
    _write_publishable_stage(stage)
    original_replace = Path.replace

    def fail_only_stage_swap(path: Path, target: Path):
        if path.resolve() == stage:
            raise OSError("simulated publish rename failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_only_stage_swap)

    with pytest.raises(OSError, match="simulated publish rename failure"):
        pkg.publish_staged_package(stage, package, archive, require_signature=True)

    assert (package / "prior-runtime.txt").read_text(encoding="utf-8") == "old"
    assert stage.exists()
    pkg.cleanup_package_staging_dir(stage, package)


# --------------------------------------------------------------------------- #
# 2b. DEBUG-DLL rejection (opencv_world4110d.dll / Qt6Cored.dll must never ship)
# --------------------------------------------------------------------------- #
def test_is_debug_dll_rejects_debug_twins_keeps_release(tmp_path):
    """The MSVC debug twin `<base>d.dll` (shipped next to its release `<base>.dll`) is
    rejected; the release DLL and legit names that merely end in 'd' are kept."""
    # release siblings present -> their debug twins are debug DLLs
    for base in ("opencv_world4110", "opencv_videoio_msmf4110_64", "Qt6Core", "Qt6Gui"):
        (tmp_path / f"{base}.dll").write_bytes(b"release")
        (tmp_path / f"{base}d.dll").write_bytes(b"debug")
        assert pkg.is_debug_dll(tmp_path / f"{base}d.dll"), f"{base}d.dll not caught"
        assert not pkg.is_debug_dll(tmp_path / f"{base}.dll"), f"{base}.dll wrongly flagged"

    # opencv debug name is caught by pattern even with NO release sibling present
    # (post-copy scan where the release twin was already dropped).
    lone = tmp_path / "lone"
    lone.mkdir()
    (lone / "opencv_world4110d.dll").write_bytes(b"debug")
    assert pkg.is_debug_dll(lone / "opencv_world4110d.dll")

    # false-positive guards: a real lib ending in 'd' with NO `<base minus d>.dll` sibling.
    legit = tmp_path / "legit"
    legit.mkdir()
    (legit / "Qt6Gamepad.dll").write_bytes(b"x")   # release Gamepad module, ends in 'd'
    (legit / "world.dll").write_bytes(b"x")         # hypothetical lib literally named world.dll
    assert not pkg.is_debug_dll(legit / "Qt6Gamepad.dll")
    assert not pkg.is_debug_dll(legit / "world.dll")


def test_scan_forbidden_catches_debug_dll(tmp_path):
    """A debug DLL that slips into the package tree is flagged by scan_forbidden; the
    release DLL alongside it is not."""
    (tmp_path / "OrionNative.exe").write_bytes(b"x")
    (tmp_path / "opencv_world4110.dll").write_bytes(b"release")   # legit release OpenCV
    (tmp_path / "opencv_world4110d.dll").write_bytes(b"debug")    # 121 MB debug twin in real life
    (tmp_path / "Qt6Core.dll").write_bytes(b"release")
    (tmp_path / "Qt6Cored.dll").write_bytes(b"debug")
    findings = set(pkg.scan_forbidden(tmp_path))
    assert "opencv_world4110d.dll" in findings
    assert "Qt6Cored.dll" in findings
    assert "opencv_world4110.dll" not in findings
    assert "Qt6Core.dll" not in findings


def test_copy_filters_drop_debug_dll(tmp_path):
    """Both copy filters refuse the debug twin: the top-level filter and the recursive
    copy_tree ignore. The release DLL is copied."""
    (tmp_path / "opencv_world4110.dll").write_bytes(b"release")
    (tmp_path / "opencv_world4110d.dll").write_bytes(b"debug")
    assert pkg.should_copy_top_file(tmp_path / "opencv_world4110.dll") is True
    assert pkg.should_copy_top_file(tmp_path / "opencv_world4110d.dll") is False

    src = tmp_path / "src"
    src.mkdir()
    (src / "opencv_world4110.dll").write_bytes(b"release")
    (src / "opencv_world4110d.dll").write_bytes(b"debug")
    dst = tmp_path / "dst"
    pkg.copy_tree(src, dst)
    copied = {p.name for p in dst.rglob("*")}
    assert "opencv_world4110.dll" in copied
    assert "opencv_world4110d.dll" not in copied


# --------------------------------------------------------------------------- #
# 2c. timing models are owned by the verified sidecar bundle
# --------------------------------------------------------------------------- #
def _write_fake_sidecar_bundle(dist: Path, model_payload: bytes = b"verified-model") -> None:
    bundled_files = {
        "OrionSidecar.exe": b"compiled-sidecar",
        "models/latency_factory_prior.json": model_payload + b"-latency",
        "models/orion_meter_detector.onnx": model_payload + b"-detector",
        "models/tip_registration.json": model_payload + b"-tip",
    }
    file_map = {}
    for relative, payload in bundled_files.items():
        path = dist / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        file_map[relative] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
    (dist / pkg.SIDECAR_BUILD_MANIFEST_NAME).write_text(
        json.dumps({"files": file_map}), encoding="utf-8"
    )


def _configure_minimal_runtime(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "root"
    build = tmp_path / "build"
    root.mkdir()
    build.mkdir()
    (build / "OrionNative.exe").write_bytes(b"native")
    monkeypatch.setattr(pkg, "ROOT", root)
    monkeypatch.setattr(pkg, "BUILD_DIR", build)
    monkeypatch.setattr(pkg, "ESSENTIAL_FILES", {"OrionNative.exe"})
    monkeypatch.setattr(pkg, "copy_chiaki_runtime", lambda _package: None)
    monkeypatch.setattr(pkg, "copy_compiled_service", lambda _package, _dist=None: None)
    monkeypatch.setattr(pkg, "write_security_policy", lambda _package: None)
    monkeypatch.setattr(pkg, "write_runtime_readme", lambda _package: None)
    monkeypatch.setattr(pkg, "strip_app_qml_source", lambda _package: None)
    return root, build


def test_copy_runtime_gets_timing_models_only_from_verified_sidecar(
    monkeypatch, tmp_path
):
    root, _build = _configure_minimal_runtime(monkeypatch, tmp_path)
    backend = root / "native_orion" / "backend"
    backend.mkdir(parents=True)
    (backend / "autogreen_sidecar.py").write_text(
        "# compiled into OrionSidecar.exe\n", encoding="utf-8"
    )
    (backend / "ps5_remoteplay_helper.py").write_text(
        "# standalone setup helper\n", encoding="utf-8"
    )
    source_models = root / "models"
    source_models.mkdir()
    for name in (
        "latency_factory_prior.json",
        "orion_meter_detector.onnx",
        "tip_registration.json",
    ):
        (source_models / name).write_bytes(b"unverified-repository-copy")

    dist = tmp_path / "sidecar.dist"
    _write_fake_sidecar_bundle(dist)
    verified = []

    def verify_sidecar(actual_root: Path, actual_dist: Path) -> list[str]:
        verified.append((actual_root, actual_dist))
        return []

    monkeypatch.setattr(pkg, "verify_sidecar_build_manifest", verify_sidecar)
    package = tmp_path / "package"
    pkg.copy_runtime(dist, package_dir=package)

    assert verified == [(root, dist.resolve())]
    assert (package / "models" / "latency_factory_prior.json").read_bytes() == (
        b"verified-model-latency"
    )
    assert (package / "models" / "tip_registration.json").read_bytes() == (
        b"verified-model-tip"
    )
    assert (package / "models" / "orion_meter_detector.onnx").read_bytes() == (
        b"verified-model-detector"
    )
    assert (package / "backend" / "ps5_remoteplay_helper.py").is_file()
    assert not any(
        path.name == "autogreen_sidecar.py" for path in package.rglob("*")
    )
    assert pkg.scan_forbidden(package) == []


def test_stale_sidecar_cannot_seed_release_with_repository_timing_models(
    monkeypatch, tmp_path
):
    root, _build = _configure_minimal_runtime(monkeypatch, tmp_path)
    source_models = root / "models"
    source_models.mkdir()
    for name in (
        "latency_factory_prior.json",
        "orion_meter_detector.onnx",
        "tip_registration.json",
    ):
        (source_models / name).write_bytes(b"repository-model")

    dist = tmp_path / "sidecar.dist"
    _write_fake_sidecar_bundle(dist)
    monkeypatch.setattr(
        pkg,
        "verify_sidecar_build_manifest",
        lambda _root, _dist: ["model inputs changed after sidecar build"],
    )
    package = tmp_path / "package"

    with pytest.raises(SystemExit, match="compiled sidecar is stale or invalid"):
        pkg.copy_runtime(dist, package_dir=package)

    assert not (package / "models").exists()


def test_verified_sidecar_still_cannot_smuggle_an_unapproved_model(monkeypatch, tmp_path):
    dist = tmp_path / "sidecar.dist"
    _write_fake_sidecar_bundle(dist)
    unexpected = dist / "models" / "experimental.pt"
    unexpected.write_bytes(b"development-weight")
    monkeypatch.setattr(pkg, "verify_sidecar_build_manifest", lambda _root, _dist: [])
    package = tmp_path / "package"
    package.mkdir()

    with pytest.raises(SystemExit, match="unapproved sidecar model"):
        pkg.copy_compiled_sidecar(package, dist)

    assert not any(package.rglob("*"))


def test_scan_forbidden_flags_stray_models_not_in_whitelist(tmp_path):
    """A stray models/ weight (a future bulk-copy regression) is flagged unless whitelisted."""
    (tmp_path / "OrionNative.exe").write_bytes(b"x")
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "orion_meter_n_v7_release.pt").write_bytes(b"dev-weight")
    (tmp_path / "models" / "orion_pose2k_s.pt").write_bytes(b"dev-weight")
    findings = set(pkg.scan_forbidden(tmp_path))
    assert "models/orion_meter_n_v7_release.pt" in findings
    assert "models/orion_pose2k_s.pt" in findings


# --------------------------------------------------------------------------- #
# 2d. artifact_url must be HTTPS (CRIT-2, local half)
# --------------------------------------------------------------------------- #
def test_build_update_manifest_rejects_non_https_artifact_url():
    with pytest.raises(SystemExit):
        pkg.build_update_manifest(version="1.0.0", artifact_sha256="ab" * 32,
                                  artifact_url="http://dl.example/o.zip")
    # https and empty are both accepted
    assert pkg.build_update_manifest(version="1.0.0", artifact_sha256="ab" * 32,
                                     artifact_url="https://dl.example/o.zip")["artifact_url"]
    assert pkg.build_update_manifest(version="1.0.0", artifact_sha256="ab" * 32,
                                     artifact_url="")["artifact_url"] == ""


# --------------------------------------------------------------------------- #
# 3. secret-CONTENT scan (name filters can't catch a pasted key)
# --------------------------------------------------------------------------- #
def test_scan_secret_content_catches_pasted_secrets(tmp_path):
    # fake secrets assembled by concatenation so this test file itself never contains
    # a secret-shaped literal (the tracked-source audit would flag it).
    fake_aws = "AKIA" + "IOSFODNN7EXAMPLE"
    fake_pem = "-----BEGIN " + "PRIVATE KEY-----\nMC4CAQ==\n-----END " + "PRIVATE KEY-----"
    (tmp_path / "OrionNative.exe").write_bytes(b"x")
    (tmp_path / "config.json").write_text(json.dumps({"aws": fake_aws}), encoding="utf-8")
    (tmp_path / "notes.txt").write_text(fake_pem, encoding="utf-8")

    findings = pkg.scan_secret_content(tmp_path)
    assert any("config.json" in f and "AWS_ACCESS_KEY" in f for f in findings)
    assert any("notes.txt" in f and "PRIVATE_KEY_BLOCK" in f for f in findings)


def test_scan_secret_content_clean_package(tmp_path):
    (tmp_path / "OrionNative.exe").write_bytes(b"MZ\x00\x01")
    (tmp_path / "meter_styles").mkdir()
    (tmp_path / "meter_styles" / "Arrow2.json").write_text('{"tip": [1, 2]}', encoding="utf-8")
    assert pkg.scan_secret_content(tmp_path) == []


# --------------------------------------------------------------------------- #
# 4. auto-updater artifact + signed Ed25519 update manifest
# --------------------------------------------------------------------------- #
def test_canonical_signing_payload_matches_server_bytes():
    """Byte-exact contract with backend/lambda_function.py sign_manifest_ed25519 AND
    native UpdateManifest.cpp canonicalManifestSigningString(): the eight signed fields,
    fixed order, compact separators, lowercase booleans, ensure_ascii."""
    manifest = pkg.build_update_manifest(
        version="1.2.3",
        artifact_sha256="ab" * 32,
        artifact_url="https://dl.example/orion-package-1.2.3.zip",
        min_version="1.0.0",
        published_at="2026-07-09T00:00:00Z",
    )
    expected = (
        '{"latest_version":"1.2.3"'
        ',"minimum_supported_version":"1.0.0"'
        ',"artifact_url":"https://dl.example/orion-package-1.2.3.zip"'
        ',"sha256":"' + "ab" * 32 + '"'
        ',"published_at":"2026-07-09T00:00:00Z"'
        ',"mandatory":false'
        ',"allow_rollback":false'
        ',"public_key_id":"orion-ed25519-v1"}'
    ).encode("ascii")
    assert pkg.canonical_signing_payload(manifest) == expected


def test_sign_update_manifest_roundtrip(tmp_path):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat,
    )

    key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "update_signing.pem"
    key_path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))

    manifest = pkg.build_update_manifest(
        version="1.2.3", artifact_sha256="cd" * 32,
        artifact_url="https://dl.example/o.zip", published_at="2026-07-09T00:00:00Z",
    )
    signed = pkg.sign_update_manifest(manifest, key_path)

    # server format: base64url, no padding
    assert signed["signature"] and "=" not in signed["signature"]
    assert signed["signature_alg"] == "ed25519"

    sig = pkg.decode_signature_bytes(signed["signature"])
    pub = pkg.decode_ed25519_public_key(signed["public_key_b64"])
    assert len(sig) == 64 and len(pub) == 32
    # verifies against the canonical payload — exactly what the native client checks
    Ed25519PublicKey.from_public_bytes(pub).verify(sig, pkg.canonical_signing_payload(signed))

    # tampering any signed field breaks verification
    tampered = dict(signed)
    tampered["latest_version"] = "9.9.9"
    from cryptography.exceptions import InvalidSignature
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, pkg.canonical_signing_payload(tampered))
        raise AssertionError("tampered manifest must not verify")
    except InvalidSignature:
        pass


def test_create_archive_hash_and_determinism(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "OrionNative.exe").write_bytes(b"exe-bytes")
    (package / "sub").mkdir()
    (package / "sub" / "SecurityCore.dll").write_bytes(b"dll-bytes")

    zip_path, digest = pkg.create_archive(package, "1.2.3", out_dir=tmp_path / "out")
    assert zip_path.name == "orion-package-1.2.3.zip"
    assert digest == hashlib.sha256(zip_path.read_bytes()).hexdigest()

    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        assert sorted(zf.namelist()) == ["OrionNative.exe", "sub/SecurityCore.dll"]

    # reproducible: same package bytes -> same artifact hash (comparable across rebuilds)
    _, digest2 = pkg.create_archive(package, "1.2.3", out_dir=tmp_path / "out2")
    assert digest2 == digest


def test_copy_runtime_prunes_unused_quick_styles_after_stripping_qml_source(monkeypatch, tmp_path):
    """[2026-09-21] copy_runtime is the ONLY staging path, so the Qt style prune must hang off
    it (after the app's own QML source is stripped, before the manifest is written) or the
    2,200 dead style files come straight back in the next package."""
    _root, _build = _configure_minimal_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(pkg, "copy_compiled_sidecar", lambda _package, _dist=None: True)
    calls: list[str] = []
    monkeypatch.setattr(pkg, "strip_app_qml_source", lambda _package: calls.append("strip"))
    monkeypatch.setattr(pkg, "prune_unused_quick_styles",
                        lambda package, **kw: calls.append(f"prune:{package.name}") or {"styles": 0, "files": 0})
    package = tmp_path / "package"
    pkg.copy_runtime(tmp_path / "sidecar.dist", package_dir=package)
    assert calls == ["strip", "prune:package"]
