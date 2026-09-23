"""Hardened Venice release PACKER + server-shard packaging mode.

Covers the soundness fixes and the server-shard assembly:
  FIX #2  lab/stray-exe rejection (with the exact OrionNative.packed.exe exception)
  FIX #3  --verify-only validates WITHOUT copying/signing
  FIX #4  internal->customer stages: Owner/Staff smoked then stripped, full coverage
  FIX #5  atomic staging leaves the prior artifact set intact on injected failure
  FIX #6  customer (Native+Updater) startup + every tamper class fails closed
  FIX #7  packer provenance recorded; a dirty packer tree is refused
  FIX #9  the default pack command enables Lethe DLL-search-order hardening
  FIX #10 audit stream-scans a secret past the old cap; the packer command is never recorded
  FIX #1  server-shard assembles the three-exe chain and asserts manifest coverage; gate fail-closed

Hermetic/offline: fixtures + mocks, never the real Lethe pack or the real signing key.
"""
from __future__ import annotations

import base64
import ast
import hashlib
import importlib.util
import inspect
import json
import shutil
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import package_orion_release as pkg  # noqa: E402
from tools import release_filter_policy as policy  # noqa: E402
from tools import security_audit as audit  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "pack_lethe_release_hardening", ROOT / "tools" / "security" / "pack_lethe_release.py"
)
pack = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pack)

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding, NoEncryption, PrivateFormat, PublicFormat,
)

TEST_KEY_ID = "orion-release-test-v1"


def _test_key(parent: Path) -> tuple[Path, str]:
    key = Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(b"packer-hardening-test-key").digest()
    )
    key_path = parent / ".packer-hardening-test-key.pem"
    key_path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    pub_b64 = base64.b64encode(
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    return key_path, pub_b64


def _write_files(package_dir: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        target = package_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


# The EXACT policy tools/package_orion_release.write_security_policy() emits. The
# acceptance gate validates every value, so a stub policy is not a usable fixture.
_PRODUCTION_POLICY = json.dumps({
    "schema": "orion.security_policy.v1",
    "require_release_manifest": True,
    "lock_automation_on_integrity_failure": True,
    "lock_automation_on_debugger": True,
    "lock_automation_on_analysis_tool": True,
    "allow_local_dev_bypass": False,
}).encode("utf-8")

_CUSTOMER_FILES = {
    "OrionNative.exe": b"MZ-native-app",
    "OrionUpdater.exe": b"MZ-updater",
    "SecurityCore.dll": b"security-core",
    "OrionCommon.dll": b"common",
    "libcrypto-3-x64.dll": b"libcrypto",
    "security_policy.json": _PRODUCTION_POLICY,
}

_SHARD_FILES = {
    "OrionNative.exe": b"lethe-bootstrap-staged-as-native",
    "OrionNative.packed.exe": b"packed-inner-payload",
    "OrionActivate.exe": b"unpacked-activation-broker",
    "OrionUpdater.exe": b"MZ-updater",
    "SecurityCore.dll": b"security-core",
    "Qt6Core.dll": b"qt6core",
    "Qt6Network.dll": b"qt6network",
    "libcrypto-3-x64.dll": b"libcrypto",
    "security_policy.json": _PRODUCTION_POLICY,
    "platforms/qwindows.dll": b"qwindows-plugin",
    "tls/qschannelbackend.dll": b"schannel-backend",
}


def _build_signed_package(package_dir: Path, key_path: Path, files: dict[str, bytes]) -> None:
    package_dir.mkdir(parents=True, exist_ok=True)
    _write_files(package_dir, files)
    pkg.write_release_manifest(
        package_dir, version="1.0.0", signing_key=key_path,
        public_key_id=TEST_KEY_ID, customer=True,
    )


# --------------------------------------------------------------------------- #
# FIX #2 — no lab/test/stray exe may ship (with the OrionNative.packed.exe exception)
# --------------------------------------------------------------------------- #
def test_lab_and_stray_exes_are_not_release_admitted():
    assert not policy.is_release_admitted_executable("GreenWindowMathChecks.exe")
    assert not policy.is_release_admitted_executable("OrionNativeTests.exe")
    assert not policy.is_release_admitted_executable("random_tool.exe")
    assert not policy.is_release_admitted_executable("Foo.packed.exe")
    assert policy.is_release_admitted_executable("OrionNative.exe")
    assert policy.is_release_admitted_executable("OrionUpdater.exe")
    assert policy.is_release_admitted_executable("OrionSidecar.exe")
    # The broker is admitted only in server-shard mode.
    assert not policy.is_release_admitted_executable("OrionActivate.exe")
    assert policy.is_release_admitted_executable("OrionActivate.exe", server_shard=True)


def test_should_copy_top_file_drops_lab_and_stray_exes(tmp_path):
    for bad in ("GreenWindowMathChecks.exe", "OrionNativeTests.exe",
                "random_tool.exe", "Foo.packed.exe"):
        assert pkg.should_copy_top_file(tmp_path / bad) is False, bad
    for good in ("OrionNative.exe", "OrionUpdater.exe", "OrionSidecar.exe",
                 "SecurityCore.dll", "meter_config.json"):
        assert pkg.should_copy_top_file(tmp_path / good) is True, good


def test_packed_payload_exact_name_is_the_only_permitted_packed_exe():
    assert policy.is_stray_packed_executable("Foo.packed.exe")
    assert policy.is_stray_packed_executable("OrionUpdater.packed.exe")
    assert not policy.is_stray_packed_executable("OrionNative.packed.exe")
    assert policy.is_forbidden_file_name("GreenWindowMathChecks.exe")
    assert policy.is_forbidden_file_name("Foo.packed.exe")
    assert not policy.is_forbidden_file_name("OrionNative.packed.exe")


def test_scan_forbidden_flags_lab_and_stray_but_not_the_shard_payload(tmp_path):
    _write_files(tmp_path, {
        "OrionNative.exe": b"x",
        "OrionNative.packed.exe": b"x",          # permitted shard payload
        "GreenWindowMathChecks.exe": b"x",       # lab binary — must be flagged
        "Something.packed.exe": b"x",            # stray packer output — must be flagged
    })
    findings = set(pkg.scan_forbidden(tmp_path))
    assert "GreenWindowMathChecks.exe" in findings
    assert "Something.packed.exe" in findings
    assert "OrionNative.packed.exe" not in findings
    assert "OrionNative.exe" not in findings


def test_audit_fails_on_contaminated_build_dir(tmp_path):
    _write_files(tmp_path, {
        "OrionNative.exe": b"x",
        "GreenWindowMathChecks.exe": b"x",
        "Stray.packed.exe": b"x",
        "OrionNative.packed.exe": b"x",  # exempt
    })
    findings = audit.audit_package_structure(tmp_path)
    lab = [f for f in findings if f.code == "LAB_OR_STRAY_EXECUTABLE_PACKAGED"]
    flagged = {Path(f.path).name for f in lab}
    assert "GreenWindowMathChecks.exe" in flagged
    assert "Stray.packed.exe" in flagged
    assert "OrionNative.packed.exe" not in flagged


# --------------------------------------------------------------------------- #
# FIX #3 — --verify-only validates WITHOUT copying/signing
# --------------------------------------------------------------------------- #
def test_verify_only_passes_on_a_signed_package_and_never_writes(tmp_path):
    package = tmp_path / "orion-package-packed"
    key_path, pub_b64 = _test_key(tmp_path)
    _build_signed_package(package, key_path, _CUSTOMER_FILES)
    sig_before = (package / "release_manifest.sig").read_bytes()
    manifest_before = (package / "release_manifest.json").read_bytes()

    rc = pack.verify_only(package, public_key_b64=pub_b64, key_id=TEST_KEY_ID)
    assert rc == 0
    # Nothing was regenerated or re-signed.
    assert (package / "release_manifest.sig").read_bytes() == sig_before
    assert (package / "release_manifest.json").read_bytes() == manifest_before


def test_verify_only_fails_on_tampered_input_without_signing(tmp_path):
    package = tmp_path / "orion-package-packed"
    key_path, pub_b64 = _test_key(tmp_path)
    _build_signed_package(package, key_path, _CUSTOMER_FILES)
    sig_before = (package / "release_manifest.sig").read_bytes()

    # Tamper a manifest-covered DLL — hash no longer matches.
    (package / "SecurityCore.dll").write_bytes(b"tampered-core")

    with pytest.raises(SystemExit) as exc:
        pack.verify_only(package, public_key_b64=pub_b64, key_id=TEST_KEY_ID)
    assert "integrity" in str(exc.value).lower()
    # verify-only did NOT re-sign to paper over the tamper.
    assert (package / "release_manifest.sig").read_bytes() == sig_before


# --------------------------------------------------------------------------- #
# FIX #6 — customer startup + every tamper class fails closed
# --------------------------------------------------------------------------- #
def test_customer_startup_smoke_passes_from_renamed_clean_dir(tmp_path):
    package = tmp_path / "orion-package-packed"
    key_path, pub_b64 = _test_key(tmp_path)
    _build_signed_package(package, key_path, _CUSTOMER_FILES)
    # Proves position-independence + Native/Updater present & covered.
    pack.customer_startup_smoke(package, probe=pack._pinned_probe(pub_b64, TEST_KEY_ID))


def test_customer_startup_smoke_detects_missing_updater(tmp_path):
    package = tmp_path / "orion-package-packed"
    key_path, pub_b64 = _test_key(tmp_path)
    files = dict(_CUSTOMER_FILES)
    files.pop("OrionUpdater.exe")
    _build_signed_package(package, key_path, files)
    with pytest.raises(RuntimeError, match="not present/covered"):
        pack.customer_startup_smoke(package, probe=pack._pinned_probe(pub_b64, TEST_KEY_ID))


def test_every_tamper_class_fails_closed(tmp_path):
    package = tmp_path / "orion-package-packed"
    key_path, pub_b64 = _test_key(tmp_path)
    _build_signed_package(package, key_path, _CUSTOMER_FILES)
    # No exception == every tamper class was rejected by the integrity gate.
    pack.tamper_refusal_smoke(package, probe=pack._pinned_probe(pub_b64, TEST_KEY_ID))


def test_shard_tamper_class_includes_packed_payload(tmp_path):
    package = tmp_path / "orion-package-packed"
    key_path, pub_b64 = _test_key(tmp_path)
    _build_signed_package(package, key_path, _SHARD_FILES)
    labels = [label for label, _ in pack.tamper_variants(package, server_shard=True)]
    assert "tamper-packed-payload" in labels
    assert "tamper-packed-payload" not in [
        label for label, _ in pack.tamper_variants(package, server_shard=False)
    ]
    pack.tamper_refusal_smoke(package, server_shard=True,
                              probe=pack._pinned_probe(pub_b64, TEST_KEY_ID))


# --------------------------------------------------------------------------- #
# FIX #7 — packer provenance recorded; a dirty tree is refused
# --------------------------------------------------------------------------- #
def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True, text=True)


def _fake_lethe_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "lethe.py").write_text("# lethe cli\n", encoding="utf-8")
    (root / "packer").mkdir()
    (root / "packer" / "orchestrator.py").write_text("# orchestrator\n", encoding="utf-8")
    (root / "packer" / "assemble.py").write_text("# assemble\n", encoding="utf-8")
    (root / "bootstrap").mkdir()
    (root / "bootstrap" / "shard_bootstrap.c").write_text("/* bootstrap */\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "packer baseline")


def test_provenance_records_commit_and_files_for_clean_tree(tmp_path):
    repo = tmp_path / "lethe"
    _fake_lethe_repo(repo)
    prov = pack.collect_packer_provenance(repo)
    assert prov["dirty"] is False
    assert len(prov["commit"]) == 40
    assert "lethe.py" in prov["files"]
    assert "packer/orchestrator.py" in prov["files"]
    assert "bootstrap/shard_bootstrap.c" in prov["files"]


def test_dirty_packer_tree_is_refused_by_default(tmp_path):
    repo = tmp_path / "lethe"
    _fake_lethe_repo(repo)
    (repo / "lethe.py").write_text("# tampered after commit\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="DIRTY"):
        pack.collect_packer_provenance(repo)
    # An explicit opt-out is allowed for NON-release test builds only.
    prov = pack.collect_packer_provenance(repo, allow_dirty=True)
    assert prov["dirty"] is True


def test_pinned_commit_mismatch_is_refused(tmp_path):
    repo = tmp_path / "lethe"
    _fake_lethe_repo(repo)
    with pytest.raises(SystemExit, match="does not match the pinned"):
        pack.collect_packer_provenance(repo, expected_commit="0" * 40)


# --------------------------------------------------------------------------- #
# FIX #9 / #10 — DLL-search hardening in the default command; command never recorded
# --------------------------------------------------------------------------- #
def test_default_pack_command_enables_dll_search_hardening(monkeypatch, tmp_path):
    fake_cli = tmp_path / "lethe.py"
    fake_cli.write_text("# cli\n", encoding="utf-8")
    monkeypatch.setattr(pack, "DEFAULT_LETHE_CLI", str(fake_cli))
    command = pack._resolve_default_packer_command(server_shard=False, shard_url="https://x")
    assert "--process-hardening" in command
    shard_command = pack._resolve_default_packer_command(server_shard=True, shard_url="https://api.zaeorion.com")
    assert "--process-hardening" in shard_command
    assert "--server-shard" in shard_command


def test_credential_bearing_args_are_refused():
    with pytest.raises(ValueError, match="must not carry credentials"):
        pack.assert_no_credential_bearing_args("lethe {input} {output} --shard-auth SECRET")
    with pytest.raises(ValueError, match="must not carry credentials"):
        pack.assert_no_credential_bearing_args("lethe {input} {output} --token abc")
    # Env-injected secrets: the template itself carries no credential.
    pack.assert_no_credential_bearing_args(
        "lethe {input} {output} --process-hardening --server-shard --shard-url https://x")


def test_packer_command_template_is_never_recorded():
    redacted = pack.redact_packer_command(
        'py lethe.py {input} {output} --shard-url https://secret.example --process-hardening')
    assert "secret.example" not in redacted
    assert "<redacted>" in redacted
    assert "--process-hardening" in redacted
    report = pack.build_report(
        mode="server-shard", input_dir=Path("in"), output_dir=Path("out"),
        targets=[], provenance={"commit": "abc"}, process_hardening=True,
        server_shard=True, shard={"packed_payload": "OrionNative.packed.exe"})
    blob = json.dumps(report)
    assert "redacted" in report["packer"]["command_template"]
    assert "{input}" not in blob and "{output}" not in blob
    assert "lethe.py" not in blob


def test_audit_stream_scans_a_secret_past_the_old_size_cap(tmp_path):
    # A first-party binary larger than the retired 25 MB cap, with a secret near the end.
    big = tmp_path / "OrionSidecar.exe"
    fake_aws = "AKIA" + "IOSFODNN7EXAMPLE"
    payload = b"\x00" * (26 * 1024 * 1024) + b"\x00cfg " + fake_aws.encode() + b"\x00"
    big.write_bytes(payload)
    assert big.stat().st_size > audit.BINARY_STRING_SCAN_MAX_BYTES
    findings = audit.audit_package_secret_content(tmp_path)
    assert any(f.code == "AWS_ACCESS_KEY" for f in findings), \
        "secret past the old cap must still be caught by the streaming scan"


def test_third_party_runtime_dll_is_not_scanned_by_name():
    # opencv/Qt third-party runtimes are excluded — only first-party binaries are scanned.
    assert not audit.should_scan_binary_strings(Path("opencv_world4110.dll"))
    assert audit.should_scan_binary_strings(Path("OrionSidecar.exe"))
    assert audit.should_scan_binary_strings(Path("packet_bridge/VeniceNetSvc.exe"))
    assert audit.should_scan_binary_strings(Path("OrionNative.packed.exe"))


# --------------------------------------------------------------------------- #
# FIX #1 — server-shard assembly + coverage + gate fail-closed
# --------------------------------------------------------------------------- #
def test_stage_server_shard_chain_assembles_three_exes(tmp_path, monkeypatch):
    package = tmp_path / "stage"
    package.mkdir()
    _write_files(package, {
        "OrionNative.exe": b"the-real-app-to-pack",
        "SecurityCore.dll": b"x",
        "Qt6Core.dll": b"x",
        "Qt6Network.dll": b"x",
        "libcrypto-3-x64.dll": b"x",
    })
    bootstrap = tmp_path / "LetheShardBootstrap.exe"
    bootstrap.write_bytes(b"bootstrap-bytes")
    broker = tmp_path / "OrionActivate.exe"
    broker.write_bytes(b"broker-bytes")

    def fake_pack(command, cwd=None):
        (package / "OrionNative.packed.exe").write_bytes(b"packed-app-bytes")

    monkeypatch.setattr(pack, "run_packer_command", fake_pack)
    info = pack.stage_server_shard_chain(
        package, command_template="lethe {input} {output} --server-shard",
        bootstrap_exe=bootstrap, broker_exe=broker)

    assert (package / "OrionNative.packed.exe").read_bytes() == b"packed-app-bytes"
    assert (package / "OrionNative.exe").read_bytes() == b"bootstrap-bytes"      # bootstrap staged
    assert (package / "OrionActivate.exe").read_bytes() == b"broker-bytes"       # broker unpacked
    assert info["packed_payload"] == "OrionNative.packed.exe"


def test_shard_coverage_asserts_every_essential(tmp_path):
    package = tmp_path / "orion-package-packed"
    key_path, _pub = _test_key(tmp_path)
    _build_signed_package(package, key_path, _SHARD_FILES)
    # Fully assembled + covered -> passes.
    pack.assert_server_shard_coverage(package)


def test_shard_coverage_fails_closed_when_essential_missing(tmp_path):
    package = tmp_path / "orion-package-packed"
    key_path, _pub = _test_key(tmp_path)
    files = dict(_SHARD_FILES)
    files.pop("Qt6Network.dll")  # drop a shard essential
    _build_signed_package(package, key_path, files)
    with pytest.raises(SystemExit, match="fail-closed"):
        pack.assert_server_shard_coverage(package)


def test_shard_coverage_fails_closed_when_essential_uncovered(tmp_path):
    package = tmp_path / "orion-package-packed"
    key_path, _pub = _test_key(tmp_path)
    _build_signed_package(package, key_path, _SHARD_FILES)
    # Present on disk but dropped from the manifest -> uncovered -> fail closed.
    manifest_path = package / "release_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].pop("OrionActivate.exe")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SystemExit, match="fail-closed"):
        pack.assert_server_shard_coverage(package)


def test_shard_gate_verifies_full_chain(tmp_path):
    package = tmp_path / "orion-package-packed"
    key_path, pub_b64 = _test_key(tmp_path)
    _build_signed_package(package, key_path, _SHARD_FILES)
    # The gate verifies coverage + release integrity (signature against the key).
    pack.validate_server_shard_release_mode(
        True, package, public_key_b64=pub_b64, key_id=TEST_KEY_ID)


def test_shard_gate_fails_closed_when_broker_missing(tmp_path):
    package = tmp_path / "orion-package-packed"
    key_path, pub_b64 = _test_key(tmp_path)
    files = dict(_SHARD_FILES)
    files.pop("OrionActivate.exe")  # no broker -> chain incomplete
    _build_signed_package(package, key_path, files)
    with pytest.raises(SystemExit, match="fail-closed"):
        pack.validate_server_shard_release_mode(
            True, package, public_key_b64=pub_b64, key_id=TEST_KEY_ID)


# --------------------------------------------------------------------------- #
# FIX #4 / #5 — internal->customer stages; atomic promotion
# --------------------------------------------------------------------------- #
def _install_pipeline_mocks(monkeypatch, tmp_path):
    """Neutralize the rig-only steps so main()'s orchestration can run hermetically."""
    monkeypatch.setattr(pkg, "require_trusted_production_signing_key", lambda *a, **k: None)
    monkeypatch.setattr(pack, "audit_package", lambda _pkg, **_kw: None)
    monkeypatch.setattr(pack, "internal_admin_smoke", lambda *a, **k: None)
    monkeypatch.setattr(pack, "customer_startup_smoke", lambda *a, **k: None)
    monkeypatch.setattr(pack, "tamper_refusal_smoke", lambda *a, **k: None)
    monkeypatch.setattr(pack, "collect_packer_provenance", lambda *a, **k: {"commit": "deadbeef", "dirty": False})
    # Stub the integrity verifier via monkeypatch so it is auto-reverted (no leak into
    # later tests that rely on the real verify_release_integrity module).
    monkeypatch.setattr(pack, "_VRI", types.SimpleNamespace(
        verify=lambda d, p, k: {"ok": True, "errors": [], "warnings": [], "checked": 1}))
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    monkeypatch.setattr(pack, "RELEASE_DIR", release_dir)
    monkeypatch.setattr(pack, "PACK_ARCHIVE_DIR", tmp_path / "archive")
    return release_dir


def _full_input_package(tmp_path, key_path) -> Path:
    """A FULL/internal input package (contains Owner/Staff) for pack to consume."""
    inp = tmp_path / "input-package"
    files = dict(_CUSTOMER_FILES)
    files["OrionOwner.exe"] = b"owner-admin-tool"
    files["OrionStaff.exe"] = b"staff-admin-tool"
    inp.mkdir()
    _write_files(inp, files)
    pkg.write_release_manifest(inp, version="1.0.0", signing_key=key_path,
                              public_key_id=TEST_KEY_ID, customer=False)
    return inp


def test_internal_to_customer_strips_owner_staff_with_full_coverage(monkeypatch, tmp_path):
    key_path, _pub = _test_key(tmp_path)
    release_dir = _install_pipeline_mocks(monkeypatch, tmp_path)
    inp = _full_input_package(tmp_path, key_path)
    out = tmp_path / "out" / "orion-package-packed"

    rc = pack.main([
        "--no-pack", "--input", str(inp), "--output", str(out),
        "--signing-key", str(key_path),
    ])
    assert rc == 0

    # Owner/Staff are gone from the published customer package...
    assert not (out / "OrionOwner.exe").exists()
    assert not (out / "OrionStaff.exe").exists()
    manifest = json.loads((out / "release_manifest.json").read_text(encoding="utf-8"))
    assert manifest["audience"] == "customer"
    assert "OrionOwner.exe" not in manifest["files"]
    assert "OrionStaff.exe" not in manifest["files"]
    # ...and EVERY remaining file is covered (no unmanifested Owner/Staff audit failure).
    on_disk = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    on_disk -= {"release_manifest.json", "release_manifest.sig"}
    assert on_disk == set(manifest["files"])
    # The complete artifact set was promoted.
    assert (release_dir / "orion-packing-report.json").is_file()
    assert (release_dir / "update_manifest.json").is_file()
    assert (release_dir / "orion-package-packed-1.0.0.zip").is_file()


@pytest.mark.parametrize("failure_point", ["packer", "sign", "zip", "update-manifest"])
def test_atomic_staging_preserves_prior_artifacts_on_failure(monkeypatch, tmp_path, failure_point):
    key_path, _pub = _test_key(tmp_path)
    release_dir = _install_pipeline_mocks(monkeypatch, tmp_path)
    inp = _full_input_package(tmp_path, key_path)
    out_parent = tmp_path / "out"
    out_parent.mkdir()
    out = out_parent / "orion-package-packed"

    # Pre-existing published artifact set — must survive an injected failure intact.
    out.mkdir()
    (out / "PRIOR.txt").write_text("prior-package", encoding="utf-8")
    prior_zip = release_dir / "orion-package-packed-1.0.0.zip"
    prior_zip.write_text("prior-zip", encoding="utf-8")
    prior_manifest = release_dir / "update_manifest.json"
    prior_manifest.write_text("prior-manifest", encoding="utf-8")
    prior_report = release_dir / "orion-packing-report.json"
    prior_report.write_text("prior-report", encoding="utf-8")

    # A successful packer for every case except the "packer" (target-2) failure.
    monkeypatch.setattr(pack, "run_packer", lambda *a, **k: [{"target": "OrionNative.exe"}])
    if failure_point == "packer":
        def boom(*_a, **_k):
            raise RuntimeError("target 2 pack failed")
        monkeypatch.setattr(pack, "run_packer", boom)
    elif failure_point == "sign":
        def boom(*_a, **_k):
            raise RuntimeError("customer manifest signing failed")
        monkeypatch.setattr(pack, "write_customer_manifest", boom)
    elif failure_point == "zip":
        def boom(*_a, **_k):
            raise RuntimeError("zip failed")
        monkeypatch.setattr(pkg, "create_archive", boom)
    elif failure_point == "update-manifest":
        def boom(*_a, **_k):
            raise RuntimeError("update manifest signing failed")
        monkeypatch.setattr(pkg, "sign_update_manifest", boom)

    with pytest.raises(Exception):
        pack.main([
            "--input", str(inp), "--output", str(out),
            "--signing-key", str(key_path),
            "--packer-command", "dummy {input} {output}",
            "--allow-dirty-packer", "--non-production-build",
        ])

    # Prior artifacts untouched; no half-packed staging dir left behind.
    assert (out / "PRIOR.txt").read_text(encoding="utf-8") == "prior-package"
    assert prior_zip.read_text(encoding="utf-8") == "prior-zip"
    assert prior_manifest.read_text(encoding="utf-8") == "prior-manifest"
    assert prior_report.read_text(encoding="utf-8") == "prior-report"
    assert not list(out_parent.glob(".orion-package-packed.staging-*"))


# =========================================================================== #
# ROUND 2 (2026-09-20) — the re-review blockers
#
#   R2-1  --verify-only enforces CUSTOMER ACCEPTANCE, not just integrity:
#         unmanifested files, an internal-audience manifest, manifested Owner/Staff,
#         a signed-but-unsafe security_policy.json, and an unadmitted executable
#         must ALL fail without changing a single input byte.
#   R2-4  copy filter and audit are in lockstep on executables (profile-aware,
#         by full relative path).
#   R2-5  production provenance is not bypassable (pin required, dirty refused).
#   R2-7  publication survives a KILL between moves: a durable journal replays
#         forward to the complete new set, or back to the complete old set.
# =========================================================================== #


def _bytes_snapshot(package_dir: Path) -> dict[str, bytes]:
    return {
        path.relative_to(package_dir).as_posix(): path.read_bytes()
        for path in sorted(package_dir.rglob("*")) if path.is_file()
    }


def _resign(package_dir: Path, key_path: Path, *, customer: bool = True) -> None:
    pkg.write_release_manifest(package_dir, version="1.0.0", signing_key=key_path,
                               public_key_id=TEST_KEY_ID, customer=customer)


# --------------------------------------------------------------------------- #
# R2-1 — verify-only must refuse an unmanifested runtime file
# --------------------------------------------------------------------------- #
def test_verify_only_refuses_an_unmanifested_runtime_file(tmp_path):
    package = tmp_path / "orion-package-packed"
    key_path, pub_b64 = _test_key(tmp_path)
    _build_signed_package(package, key_path, _CUSTOMER_FILES)
    # Signature + every manifested hash still verify; the extra DLL is simply not
    # covered. verify_release_integrity records that as a WARNING, so the old gate
    # exited 0 on it. AGENTS.md makes an unmanifested runtime file a release blocker.
    (package / "unmanifested-runtime.dll").write_bytes(b"attacker-supplied")
    before = _bytes_snapshot(package)

    with pytest.raises(SystemExit) as exc:
        pack.verify_only(package, public_key_b64=pub_b64, key_id=TEST_KEY_ID)
    # [RT-MED-08 2026-09-23] The integrity gate now refuses the unlisted file even earlier, at the
    # signed-inventory check; either refusal is fail-closed.
    assert ("UNMANIFESTED_PACKAGE_FILE" in str(exc.value)
            or "Unlisted (unverified) file in package: unmanifested-runtime.dll" in str(exc.value))
    assert _bytes_snapshot(package) == before, "verify-only must never rewrite its input"


def test_verify_only_refuses_an_internal_audience_package(tmp_path):
    package = tmp_path / "orion-package-packed"
    key_path, pub_b64 = _test_key(tmp_path)
    package.mkdir(parents=True)
    _write_files(package, _CUSTOMER_FILES)
    _resign(package, key_path, customer=False)          # correctly signed, audience=internal
    before = _bytes_snapshot(package)

    with pytest.raises(SystemExit, match="audience"):
        pack.verify_only(package, public_key_b64=pub_b64, key_id=TEST_KEY_ID)
    assert _bytes_snapshot(package) == before


def test_verify_only_refuses_manifested_owner_staff(tmp_path):
    package = tmp_path / "orion-package-packed"
    key_path, pub_b64 = _test_key(tmp_path)
    files = dict(_CUSTOMER_FILES)
    files["OrionOwner.exe"] = b"owner-admin-tool"
    files["OrionStaff.exe"] = b"staff-admin-tool"
    package.mkdir(parents=True)
    _write_files(package, files)
    # customer=False so the admin tools ARE listed: a signed manifest that covers the
    # privileged tools must still be refused as a customer package.
    _resign(package, key_path, customer=False)
    before = _bytes_snapshot(package)

    with pytest.raises(SystemExit) as exc:
        pack.verify_only(package, public_key_b64=pub_b64, key_id=TEST_KEY_ID)
    message = str(exc.value)
    assert "OrionOwner.exe" in message and "OrionStaff.exe" in message
    assert _bytes_snapshot(package) == before


def test_verify_only_refuses_a_signed_but_unsafe_security_policy(tmp_path):
    package = tmp_path / "orion-package-packed"
    key_path, pub_b64 = _test_key(tmp_path)
    files = dict(_CUSTOMER_FILES)
    files["security_policy.json"] = json.dumps({
        "schema": "orion.security_policy.v1",
        "require_release_manifest": False,     # fails OPEN
        "lock_automation_on_integrity_failure": True,
        "lock_automation_on_debugger": True,
        "lock_automation_on_analysis_tool": True,
        "allow_local_dev_bypass": True,        # dev bypass in a customer package
    }).encode("utf-8")
    _build_signed_package(package, key_path, files)     # signature covers the bad policy
    before = _bytes_snapshot(package)

    with pytest.raises(SystemExit) as exc:
        pack.verify_only(package, public_key_b64=pub_b64, key_id=TEST_KEY_ID)
    message = str(exc.value)
    assert "RELEASE_MANIFEST_NOT_REQUIRED" in message
    assert "LOCAL_DEV_BYPASS_ALLOWED" in message
    assert _bytes_snapshot(package) == before


def test_verify_only_refuses_a_missing_security_policy(tmp_path):
    package = tmp_path / "orion-package-packed"
    key_path, pub_b64 = _test_key(tmp_path)
    files = dict(_CUSTOMER_FILES)
    files.pop("security_policy.json")
    _build_signed_package(package, key_path, files)

    with pytest.raises(SystemExit, match="SECURITY_POLICY_MISSING"):
        pack.verify_only(package, public_key_b64=pub_b64, key_id=TEST_KEY_ID)


def test_verify_only_refuses_an_unadmitted_executable(tmp_path):
    package = tmp_path / "orion-package-packed"
    key_path, pub_b64 = _test_key(tmp_path)
    files = dict(_CUSTOMER_FILES)
    files["random_tool.exe"] = b"MZ-arbitrary"          # manifested, signed, still stray
    _build_signed_package(package, key_path, files)

    with pytest.raises(SystemExit, match="UNADMITTED_EXECUTABLE_PACKAGED"):
        pack.verify_only(package, public_key_b64=pub_b64, key_id=TEST_KEY_ID)


def test_verify_only_refuses_a_packed_payload_outside_shard_mode(tmp_path):
    package = tmp_path / "orion-package-packed"
    key_path, pub_b64 = _test_key(tmp_path)
    files = dict(_CUSTOMER_FILES)
    files["OrionNative.packed.exe"] = b"packed-inner-payload"
    _build_signed_package(package, key_path, files)

    # Non-shard profile: the packed payload is packer debris.
    with pytest.raises(SystemExit, match="UNADMITTED_EXECUTABLE_PACKAGED"):
        pack.assert_customer_package_acceptance(package, server_shard=False)
    # Shard profile: it is the expected inner payload.
    pack.assert_customer_package_acceptance(package, server_shard=True)


def test_verify_only_accepts_a_correct_shard_package(tmp_path):
    package = tmp_path / "orion-package-packed"
    key_path, pub_b64 = _test_key(tmp_path)
    _build_signed_package(package, key_path, _SHARD_FILES)
    assert pack.verify_only(package, server_shard=True,
                            public_key_b64=pub_b64, key_id=TEST_KEY_ID) == 0


def test_verify_only_refuses_while_a_publish_is_mid_flight(tmp_path, monkeypatch):
    package = tmp_path / "orion-package-packed"
    key_path, pub_b64 = _test_key(tmp_path)
    _build_signed_package(package, key_path, _CUSTOMER_FILES)
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    monkeypatch.setattr(pack, "RELEASE_DIR", release_dir)
    (release_dir / pack.PUBLISH_JOURNAL_NAME).write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit, match="interrupted publish"):
        pack.verify_only(package, public_key_b64=pub_b64, key_id=TEST_KEY_ID)


# --------------------------------------------------------------------------- #
# R2-4 — copy filter and audit agree on EVERY executable, by full relative path
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rel_path", [
    "random_tool.exe",
    "RANDOM_TOOL.EXE",
    "GreenWindowMathChecks.exe",
    "tools/helper.exe",
    "nested/dir/OrionNative.exe",          # admitted name, WRONG location
    "Something.packed.exe",
])
def test_audit_flags_every_unadmitted_executable(tmp_path, rel_path):
    _write_files(tmp_path, {rel_path: b"MZ", "OrionNative.exe": b"MZ"})
    findings = audit.audit_package_structure(tmp_path)
    flagged = {f.path.split("/")[-1].lower() for f in findings
               if f.code in {"UNADMITTED_EXECUTABLE_PACKAGED",
                             "LAB_OR_STRAY_EXECUTABLE_PACKAGED", "TEST_BINARY_PACKAGED"}}
    assert Path(rel_path).name.lower() in flagged, f"{rel_path} must be rejected by the audit"


@pytest.mark.parametrize("rel_path", [
    "OrionNative.exe",
    "OrionUpdater.exe",
    "OrionSidecar.exe",
    "chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe",
    "packet_bridge/VeniceNetSvc.exe",
])
def test_audit_admits_the_real_release_executables(tmp_path, rel_path):
    _write_files(tmp_path, {rel_path: b"MZ"})
    findings = audit.audit_executable_admission(tmp_path)
    assert not findings, [f.format() for f in findings]


def test_packed_payload_is_admitted_only_in_shard_mode(tmp_path):
    _write_files(tmp_path, {"OrionNative.packed.exe": b"MZ", "OrionActivate.exe": b"MZ"})
    non_shard = audit.audit_executable_admission(tmp_path, server_shard=False)
    assert {Path(f.path).name for f in non_shard} == {"OrionNative.packed.exe", "OrionActivate.exe"}
    assert not audit.audit_executable_admission(tmp_path, server_shard=True)


def test_copy_filter_and_audit_agree(tmp_path):
    """Every name the copy filter drops must also be an audit finding, and vice versa."""
    names = ["random_tool.exe", "GreenWindowMathChecks.exe", "Foo.packed.exe",
             "OrionNativeTests.exe", "OrionNative.exe", "OrionUpdater.exe", "OrionSidecar.exe"]
    for name in names:
        copied = pkg.should_copy_top_file(tmp_path / name)
        admitted = policy.is_admitted_package_executable(name)
        assert copied == admitted, f"copy filter and audit disagree on {name}"


# --------------------------------------------------------------------------- #
# R2-5 — production provenance cannot be bypassed from the CLI
# --------------------------------------------------------------------------- #
def _prod_argv(tmp_path, key_path, **extra) -> list[str]:
    argv = ["--input", str(tmp_path / "input-package"),
            "--output", str(tmp_path / "out" / "orion-package-packed"),
            "--signing-key", str(key_path),
            "--packer-command", "dummy {input} {output}"]
    for flag in extra.get("flags", []):
        argv.append(flag)
    return argv


def test_production_pack_requires_an_exact_packer_commit_pin(monkeypatch, tmp_path):
    key_path, _pub = _test_key(tmp_path)
    _install_pipeline_mocks(monkeypatch, tmp_path)
    _full_input_package(tmp_path, key_path)
    with pytest.raises(SystemExit) as exc:
        pack.main(_prod_argv(tmp_path, key_path))
    assert exc.value.code == 2   # argparse refusal, before any staging


def test_production_pack_refuses_allow_dirty_packer(monkeypatch, tmp_path):
    key_path, _pub = _test_key(tmp_path)
    _install_pipeline_mocks(monkeypatch, tmp_path)
    _full_input_package(tmp_path, key_path)
    with pytest.raises(SystemExit) as exc:
        pack.main(_prod_argv(tmp_path, key_path,
                             flags=["--allow-dirty-packer", "--expected-packer-commit", "a" * 40]))
    assert exc.value.code == 2


def test_non_production_build_refuses_the_production_signing_key(monkeypatch, tmp_path):
    key_path, _pub = _test_key(tmp_path)
    _install_pipeline_mocks(monkeypatch, tmp_path)
    _full_input_package(tmp_path, key_path)
    monkeypatch.setattr(pkg, "is_trusted_production_signing_key", lambda *a, **k: True)
    with pytest.raises(SystemExit) as exc:
        pack.main(_prod_argv(tmp_path, key_path,
                             flags=["--non-production-build", "--allow-dirty-packer"]))
    assert exc.value.code == 2


def test_non_production_build_stamps_the_report(monkeypatch, tmp_path):
    key_path, _pub = _test_key(tmp_path)
    release_dir = _install_pipeline_mocks(monkeypatch, tmp_path)
    inp = _full_input_package(tmp_path, key_path)
    monkeypatch.setattr(pkg, "is_trusted_production_signing_key", lambda *a, **k: False)
    monkeypatch.setattr(pack, "run_packer", lambda *a, **k: [{"target": "OrionNative.exe"}])
    rc = pack.main(["--input", str(inp),
                    "--output", str(tmp_path / "out" / "orion-package-packed"),
                    "--signing-key", str(key_path),
                    "--packer-command", "dummy {input} {output}",
                    "--allow-dirty-packer", "--non-production-build"])
    assert rc == 0
    report = json.loads((release_dir / "orion-packing-report.json").read_text(encoding="utf-8"))
    assert report["production"] is False


def test_production_report_is_stamped_production_true(monkeypatch, tmp_path):
    key_path, _pub = _test_key(tmp_path)
    release_dir = _install_pipeline_mocks(monkeypatch, tmp_path)
    inp = _full_input_package(tmp_path, key_path)
    rc = pack.main(["--no-pack", "--input", str(inp),
                    "--output", str(tmp_path / "out" / "orion-package-packed"),
                    "--signing-key", str(key_path)])
    assert rc == 0
    report = json.loads((release_dir / "orion-packing-report.json").read_text(encoding="utf-8"))
    assert report["production"] is True


# --------------------------------------------------------------------------- #
# R2-7 — a KILL between promotion moves leaves a recoverable journal
# --------------------------------------------------------------------------- #
def _publish_set(tmp_path) -> tuple[Path, Path, list[tuple[Path, Path]]]:
    """A staged NEW set + a published OLD set, plus the (staged, published) plan."""
    release_dir = tmp_path / "release"
    staging = tmp_path / "staging"
    release_dir.mkdir()
    staging.mkdir()
    plan: list[tuple[Path, Path]] = []
    for name in ("orion-package-packed.zip", "update_manifest.json", "orion-packing-report.json"):
        staged = staging / name
        staged.write_text(f"new-{name}", encoding="utf-8")
        published = release_dir / name
        published.write_text(f"old-{name}", encoding="utf-8")
        plan.append((staged, published))
    return release_dir, staging, plan


def _observed(release_dir: Path, plan) -> set[str]:
    return {p.read_text(encoding="utf-8") for _s, p in plan if p.exists()}


@pytest.mark.parametrize("kill_after", [0, 1, 2])
def test_kill_between_moves_is_recovered_to_a_complete_set(tmp_path, monkeypatch, kill_after):
    release_dir, _staging, plan = _publish_set(tmp_path)
    archive = tmp_path / "archive"

    # Simulate a KILL: the in-process rollback never runs, so the journal survives
    # exactly as it would after `taskkill` / power loss mid-promotion.
    monkeypatch.setattr(pack, "_rollback_promotion", lambda *a, **k: None)
    real_replace = Path.replace
    calls = {"n": 0}
    publish_targets = {str(p.resolve()) for _s, p in plan}

    def killing_replace(self, target):
        if str(Path(target).resolve()) in publish_targets:
            if calls["n"] == kill_after:
                raise KeyboardInterrupt("simulated kill mid-promotion")
            calls["n"] += 1
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", killing_replace)
    with pytest.raises(KeyboardInterrupt):
        pack.promote_pack_output(plan, archive, release_dir)
    monkeypatch.setattr(Path, "replace", real_replace)

    journal = pack.publish_journal_path(release_dir)
    assert journal.is_file(), "a killed promotion must leave a journal to replay"

    # The staged set survives a kill (the cleanup never ran), so recovery completes
    # the transaction FORWARD rather than discarding a verified build.
    assert pack.recover_publish_journal(release_dir) == "roll-forward"
    assert not journal.exists()

    observed = _observed(release_dir, plan)
    complete_new = {f"new-{p.name}" for _s, p in plan}
    complete_old = {f"old-{p.name}" for _s, p in plan}
    assert observed in (complete_new, complete_old), (
        f"a reader observed a MIXED artifact set after recovery: {observed}")
    assert len(observed) == len(plan), "every published artifact must exist after recovery"


def test_recovery_rolls_back_when_the_staged_set_is_gone(tmp_path, monkeypatch):
    release_dir, staging, plan = _publish_set(tmp_path)
    archive = tmp_path / "archive"
    monkeypatch.setattr(pack, "_rollback_promotion", lambda *a, **k: None)
    real_replace = Path.replace
    publish_targets = {str(p.resolve()) for _s, p in plan}

    def killing_replace(self, target):
        if str(Path(target).resolve()) in publish_targets and (release_dir / plan[0][1].name).exists():
            # Kill right after the FIRST artifact is published.
            if any(p.read_text(encoding="utf-8").startswith("new-") for _s, p in plan if p.exists()):
                raise KeyboardInterrupt("simulated kill mid-promotion")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", killing_replace)
    with pytest.raises(KeyboardInterrupt):
        pack.promote_pack_output(plan, archive, release_dir)
    monkeypatch.setattr(Path, "replace", real_replace)

    # The staging tree is gone (the packer's finally-cleanup won the race) -> the only
    # complete set left is the OLD one, so recovery must roll back to it.
    shutil.rmtree(staging)
    assert pack.recover_publish_journal(release_dir) == "roll-back"
    assert _observed(release_dir, plan) == {f"old-{p.name}" for _s, p in plan}


def test_successful_promotion_leaves_no_journal(tmp_path):
    release_dir, _staging, plan = _publish_set(tmp_path)
    pack.promote_pack_output(plan, tmp_path / "archive", release_dir)
    assert not pack.publish_journal_path(release_dir).exists()
    assert _observed(release_dir, plan) == {f"new-{p.name}" for _s, p in plan}
    assert pack.recover_publish_journal(release_dir) is None


def test_assembly_recovers_interrupted_publication_before_requiring_default_input():
    """A crash can temporarily remove the default unpacked input name."""
    body = ast.parse(textwrap.dedent(inspect.getsource(pack.main)))
    calls = [node for node in ast.walk(body) if isinstance(node, ast.Call)]
    recovery = [node.lineno for node in calls
                if isinstance(node.func, ast.Name) and node.func.id == "recover_publish_journal"]
    input_checks = [node.lineno for node in calls
                    if isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "input_dir" and node.func.attr == "is_dir"]
    assert len(recovery) == len(input_checks) == 1
    assert recovery[0] < input_checks[0], (
        "recover the journal before a missing input name can abort assembly")


# --------------------------------------------------------------------------- #
# R2-3 — the installer cannot be built from an unverified package, and a failed
#        broker registration is FATAL on a shard install
# --------------------------------------------------------------------------- #
_BUILD_PS1 = (ROOT / "installer" / "build_installer.ps1").read_text(
    encoding="utf-8", errors="replace")
_ISS = (ROOT / "installer" / "orion.iss").read_text(encoding="utf-8", errors="replace")


def test_installer_runs_the_pinned_key_verifier_before_iscc():
    assert "--verify-only" in _BUILD_PS1, \
        "build_installer.ps1 must cryptographically verify the package before compiling"
    verify_at = _BUILD_PS1.index("--verify-only")
    iscc_at = _BUILD_PS1.index("& $Iscc @IsccArgs")
    assert verify_at < iscc_at, "the verification gate must run BEFORE iscc"
    # A failed verification aborts the build.
    tail = _BUILD_PS1[verify_at:iscc_at]
    assert "throw" in tail and "verification FAILED" in tail
    # Shard packages are verified in the shard profile.
    assert '$VerifyArgs += "--server-shard"' in _BUILD_PS1


def test_installer_requires_the_broker_dependency_set():
    for dependency in ("SecurityCore.dll", "Qt6Core.dll", "Qt6Network.dll", "libcrypto-3-x64.dll"):
        assert f'$Required += "{dependency}"' in _BUILD_PS1, \
            f"a server-shard installer must require the broker dependency {dependency}"


def test_broker_registration_failure_is_fatal_on_a_shard_install():
    start = _ISS.index("procedure RegisterActivationBroker")
    body = _ISS[start:_ISS.index("procedure CurStepChanged", start)]
    assert "RaiseException" in body, (
        "a nonzero OrionActivate.exe --register must FAIL the install: the customer's only "
        "activation path is orion://, so a dead handler cannot be reported as success")
    # The non-shard build still exits early (no broker present).
    assert "if not FileExists(BrokerPath) then" in body
    assert "Activation broker registration failed" in body
