"""Offline Windows updater canary: never touches the installed app or the network."""

import base64
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "native_orion" / "build" / "Release"
_spec = importlib.util.spec_from_file_location("package_orion_release", ROOT / "tools" / "package_orion_release.py")
pkg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pkg)

ROOT_RUNTIME = (
    "OrionUpdater.exe", "UpdaterCore.dll", "SecurityCore.dll", "OrionCommon.dll",
    "Qt6Core.dll", "Qt6Gui.dll", "Qt6Network.dll", "Qt6Widgets.dll",
    "libcrypto-3-x64.dll",
)
PLUGINS = (
    "platforms/qwindows.dll", "platforms/qoffscreen.dll",
    "tls/qschannelbackend.dll", "tls/qcertonlybackend.dll",
)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sign_release(root: Path, key, *, listed: set[str], version: str) -> None:
    files = {}
    for rel in sorted(listed):
        path = root / rel
        files[rel] = {"sha256": _hash(path), "size": path.stat().st_size}
    manifest = {
        "schema": "orion.release_manifest.v1", "signature_alg": "ed25519",
        "signature_required": True, "public_key_id": "orion-updater-fixture-v1",
        # [RT-MED-08 2026-09-23] The updater takes the installed version from the VERIFIED
        # installed manifest (never --current-version), exactly like a packaged release.
        "version": version,
        "files": files,
    }
    raw = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    (root / "release_manifest.json").write_bytes(raw)
    (root / "release_manifest.sig").write_bytes(
        base64.urlsafe_b64encode(key.sign(raw)).rstrip(b"=") + b"\n"
    )


def _inventory(root: Path, listed: set[str]) -> dict[str, str]:
    return {rel: _hash(root / rel) for rel in sorted(listed)}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows image-lock and Qt updater canary")
@pytest.mark.parametrize("inject_failure", [False, True])
def test_staged_updater_full_copy_or_exact_rollback(tmp_path, inject_failure):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    if not (BUILD / "OrionUpdater.exe").is_file():
        pytest.skip("A coherent native_orion/build/Release updater is required")
    install = tmp_path / "Venice"
    install.mkdir()
    old_listed = set(ROOT_RUNTIME + PLUGINS + ("OrionNative.exe", "plugins/retired.dll"))
    for rel in ROOT_RUNTIME + PLUGINS:
        source = BUILD / rel
        if not source.is_file():
            pytest.skip(f"Coherent updater runtime dependency is missing: {rel}")
        target = install / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (install / "OrionNative.exe").write_bytes(b"old-placeholder-not-executed")
    (install / "plugins").mkdir()
    (install / "plugins" / "retired.dll").write_bytes(b"retired-runtime")
    (install / "customer-data.txt").write_bytes(b"customer-data-must-survive")

    key = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"orion-a6-offline-fixture").digest())
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    _sign_release(install, key, listed=old_listed, version="1.0.0")
    old_inventory = _inventory(install, old_listed)
    old_manifest_hashes = {name: _hash(install / name) for name in ("release_manifest.json", "release_manifest.sig")}

    new = tmp_path / "new-package"
    shutil.copytree(install, new)
    (new / "customer-data.txt").unlink()
    (new / "plugins" / "retired.dll").unlink()
    for rel in ("OrionUpdater.exe", "UpdaterCore.dll", "Qt6Core.dll", "OrionNative.exe"):
        with (new / rel).open("ab") as file:
            file.write(b"\nupdated-overlay\n")
    new_listed = old_listed - {"plugins/retired.dll"}
    _sign_release(new, key, listed=new_listed, version="1.0.1")
    expected_new = _inventory(new, new_listed)

    artifact = tmp_path / "artifact.zip"
    with zipfile.ZipFile(artifact, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(new.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(new).as_posix())
    update = pkg.build_update_manifest(
        version="1.0.1", min_version="0.0.0", artifact_sha256=_hash(artifact),
        artifact_url="https://example.invalid/offline-fixture.zip",
        public_key_id="orion-updater-fixture-v1", published_at="2026-09-23T00:00:00Z",
    )
    update["signature"] = base64.urlsafe_b64encode(
        key.sign(pkg.canonical_signing_payload(update))
    ).rstrip(b"=").decode()
    manifest = tmp_path / "update_manifest.json"
    manifest.write_text(json.dumps(update), encoding="utf-8")
    pubkeys = tmp_path / "pubkeys.json"
    pubkeys.write_text(json.dumps({"keys": {"orion-updater-fixture-v1": base64.b64encode(pub).decode()}}),
                       encoding="utf-8")

    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["ORION_RELEASE_MANIFEST_TEST_PUBKEY_ID"] = "orion-updater-fixture-v1"
    env["ORION_RELEASE_MANIFEST_TEST_PUBKEY_B64"] = base64.b64encode(pub).decode()
    args = [str(install / "OrionUpdater.exe"), "--install-dir", str(install),
            "--manifest-file", str(manifest), "--artifact-file", str(artifact),
            "--pubkeys-file", str(pubkeys), "--current-version", "1.0.0",
            "--stage-runtime", "--exit-on-complete", "--no-relaunch"]
    if inject_failure:
        args.append("--inject-failure-after-prune")
    owner = None
    if not inject_failure:
        owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(6)"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        args.extend(("--launcher-pid", str(owner.pid)))
    try:
        # DEVNULL avoids the detached second stage inheriting an open capture
        # pipe, which would make subprocess.run wait for the child as well.
        first = subprocess.run(args, cwd=install, env=env, timeout=20,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if owner is not None:
            time.sleep(0.6)
            assert owner.poll() is None
            assert _inventory(install, old_listed) == old_inventory, "update began while launcher PID was alive"
            owner.wait(timeout=10)
    finally:
        if owner is not None and owner.poll() is None:
            owner.terminate()
            owner.wait(timeout=5)
    assert first.returncode == 0

    log = install / "orion_updater.log"
    deadline = time.monotonic() + 45
    expected_log = ("Injected development fault" if inject_failure else "Update to 1.0.1 complete")
    while time.monotonic() < deadline:
        if log.is_file() and expected_log in log.read_text(encoding="utf-8", errors="replace"):
            break
        time.sleep(0.1)
    assert log.is_file(), "staged helper did not write its update log"
    assert expected_log in log.read_text(encoding="utf-8", errors="replace")
    assert (install / "customer-data.txt").read_bytes() == b"customer-data-must-survive"
    # The installed updater's bounded cleaner waits for the staged process to
    # exit, then releases its sibling DLL closure instead of accumulating it.
    cleanup_deadline = time.monotonic() + 15
    while time.monotonic() < cleanup_deadline and list(tmp_path.glob(".orion_updater_stage-*")):
        time.sleep(0.1)
    assert not list(tmp_path.glob(".orion_updater_stage-*"))
    if inject_failure:
        assert _inventory(install, old_listed) == old_inventory
        assert {name: _hash(install / name) for name in old_manifest_hashes} == old_manifest_hashes
    else:
        assert _inventory(install, new_listed) == expected_new
        assert not (install / "plugins" / "retired.dll").exists()
