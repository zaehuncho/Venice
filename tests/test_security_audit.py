from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "security_audit", ROOT / "tools" / "security_audit.py"
)
security_audit = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = security_audit
_SPEC.loader.exec_module(security_audit)


def test_source_audit_fails_closed_when_git_inventory_fails(monkeypatch, tmp_path):
    def fail_git(*_args, **_kwargs):
        raise subprocess.CalledProcessError(128, ["git", "ls-files"])

    monkeypatch.setattr(security_audit.subprocess, "run", fail_git)

    findings = security_audit.audit_tracked_source(tmp_path)

    assert len(findings) == 1
    assert findings[0].severity == "HIGH"
    assert findings[0].code == "SOURCE_INVENTORY_FAILED"


def _write_package_manifest(package_dir: Path, files: dict[str, str]) -> None:
    manifest = {
        "schema": "orion.release_manifest.v1",
        "files": {name: {"sha256": digest} for name, digest in files.items()},
    }
    (package_dir / "release_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _write_security_policy(package_dir: Path) -> None:
    policy = {
        "schema": "orion.security_policy.v1",
        "require_release_manifest": True,
        "allow_local_dev_bypass": False,
    }
    (package_dir / "security_policy.json").write_text(json.dumps(policy), encoding="utf-8")


def test_secret_scan_catches_concrete_dev_key():
    findings = security_audit.scan_secret_text(
        "sample.txt",
        "ORION_DEV_KEY=NVDEV-20260524-BE66CE5F\n",
    )

    assert any(f.code == "CONCRETE_DEV_KEY" for f in findings)


def test_secret_scan_catches_gumroad_token_after_ssm_name():
    findings = security_audit.scan_secret_text(
        "handoff.md",
        "SSM /orion/gumroad_webhook_token = abcdefghijklmnopqrstuvwxyz012345",
    )

    assert any(f.code == "GUMROAD_TOKEN" for f in findings)


def test_secret_scan_allows_sellhub_ssm_parameter_reference():
    findings = security_audit.scan_secret_text(
        "handoff.md",
        'SELLHUB_SECRET_SSM = "/orion/sellhub_webhook_secret"',
    )

    assert not any(f.code == "SELLHUB_SECRET" for f in findings)


def test_secret_scan_catches_concrete_sellhub_value():
    label = "sellhub_" + "webhook_secret"
    concrete_value = "s" * 32

    findings = security_audit.scan_secret_text(
        "handoff.md",
        f"{label}={concrete_value}",
    )

    assert any(f.code == "SELLHUB_SECRET" for f in findings)


def test_package_audit_catches_manifest_hash_mismatch(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    runtime = package / "OrionNative.exe"
    runtime.write_bytes(b"first")
    _write_security_policy(package)
    _write_package_manifest(package, {"OrionNative.exe": "0" * 64})

    findings = security_audit.audit_package_manifest(package)

    assert any(f.code == "MANIFEST_HASH_MISMATCH" for f in findings)


def test_package_audit_rejects_local_artifacts(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "settings.json").write_text("{}", encoding="utf-8")
    (package / "debug.pdb").write_bytes(b"symbols")
    (package / "OrionNativeTests.exe").write_bytes(b"tests")

    findings = security_audit.audit_package_structure(package)
    codes = {f.code for f in findings}

    assert "FORBIDDEN_PACKAGE_ARTIFACT" in codes
    assert "FORBIDDEN_PACKAGE_SUFFIX" in codes
    assert "TEST_BINARY_PACKAGED" in codes


def test_package_audit_rejects_dated_backup_binary(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    allowed_manifest = package / "OrionStream.exe.manifest"
    allowed_manifest.write_bytes(b"manifest")
    rejected = {
        "OrionStream.exe.bak-20260731-preoccfix",
        "OrionStream.exe.running-old",
        "OrionStream.exe.tier2",
        "VisionCore.dll.repaired",
    }
    for name in rejected:
        (package / name).write_bytes(b"backup")

    findings = security_audit.audit_package_structure(package)
    rejected_findings = {
        Path(finding.path).name
        for finding in findings
        if finding.code == "FORBIDDEN_PACKAGE_SUFFIX"
    }

    assert rejected <= rejected_findings
    assert allowed_manifest.name not in rejected_findings


def test_package_audit_uses_canonical_names_and_suffixes(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "codesigning").mkdir()
    (package / "codesigning" / "public.txt").write_text("placeholder", encoding="utf-8")
    (package / "Get-PSN-AccountID.bat").write_text("@echo off\n", encoding="utf-8")
    for name in ("runtime.old", "runtime.orig", "runtime.ppk", "runtime.tmp"):
        (package / name).write_bytes(b"forbidden")
    (package / "cacert.pem").write_text("public certificate bundle", encoding="utf-8")

    findings = security_audit.audit_package_structure(package)
    forbidden_paths = {
        Path(finding.path).name
        for finding in findings
        if finding.code in {"FORBIDDEN_PACKAGE_ARTIFACT", "FORBIDDEN_PACKAGE_SUFFIX"}
    }

    assert "Get-PSN-AccountID.bat" in forbidden_paths
    assert {"runtime.old", "runtime.orig", "runtime.ppk", "runtime.tmp"} <= forbidden_paths
    assert any("codesigning" in finding.path for finding in findings)
    assert "cacert.pem" not in forbidden_paths


def test_package_audit_rejects_real_debug_twins_without_gamepad_false_positive(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "Qt6Core.dll").write_bytes(b"release")
    (package / "Qt6Cored.dll").write_bytes(b"debug")
    (package / "opencv_world4110d.dll").write_bytes(b"debug")
    (package / "Qt6Gamepad.dll").write_bytes(b"release")

    findings = security_audit.audit_package_structure(package)
    debug_names = {
        Path(finding.path).name
        for finding in findings
        if finding.code == "DEBUG_RUNTIME_DLL"
    }

    assert {"Qt6Cored.dll", "opencv_world4110d.dll"} <= debug_names
    assert "Qt6Gamepad.dll" not in debug_names


def test_package_audit_requires_owner_and_staff_tools(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    for name in security_audit.ESSENTIAL_PACKAGE_FILES - {"OrionOwner.exe", "OrionStaff.exe"}:
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"placeholder")

    findings = security_audit.audit_package_structure(package)
    missing = {Path(f.path).name for f in findings if f.code == "PACKAGE_ESSENTIAL_MISSING"}

    assert {"OrionOwner.exe", "OrionStaff.exe"} <= missing


def test_customer_package_audit_does_not_require_internal_tools(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    for name in security_audit.ESSENTIAL_PACKAGE_FILES - {"OrionOwner.exe", "OrionStaff.exe"}:
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"placeholder")
    (package / "release_manifest.json").write_text(
        json.dumps({"schema": "orion.release_manifest.v1", "audience": "customer", "files": {"OrionNative.exe": {"sha256": "0" * 64}}}),
        encoding="utf-8",
    )

    findings = security_audit.audit_package_structure(package)

    assert not any(f.code == "PACKAGE_ESSENTIAL_MISSING" for f in findings)


def test_package_audit_requires_custom_chiaki_runtime(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    for name in security_audit.ESSENTIAL_PACKAGE_FILES - {"chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe"}:
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"placeholder")

    findings = security_audit.audit_package_structure(package)

    assert any(
        f.code == "PACKAGE_ESSENTIAL_MISSING"
        and f.path.endswith("chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe")
        for f in findings
    )


def test_package_audit_requires_compiled_source_bound_sidecar(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    missing_sidecar = {"OrionSidecar.exe", "ORION_SIDECAR_BUILD.json"}
    for name in security_audit.ESSENTIAL_PACKAGE_FILES - missing_sidecar:
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"placeholder")

    findings = security_audit.audit_package_structure(package)
    missing = {
        Path(finding.path).name
        for finding in findings
        if finding.code == "PACKAGE_ESSENTIAL_MISSING"
    }

    assert missing_sidecar <= missing


def test_package_audit_requires_detached_release_manifest_signature(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    for name in security_audit.ESSENTIAL_PACKAGE_FILES - {"release_manifest.sig"}:
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"placeholder")

    findings = security_audit.audit_package_structure(package)

    assert any(
        finding.code == "PACKAGE_ESSENTIAL_MISSING"
        and finding.path.endswith("release_manifest.sig")
        for finding in findings
    )


def test_detached_manifest_signature_is_required_but_not_recursively_manifested(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    runtime = package / "OrionNative.exe"
    runtime.write_bytes(b"runtime")
    (package / "release_manifest.sig").write_bytes(b"detached-signature-placeholder")
    _write_package_manifest(
        package,
        {"OrionNative.exe": security_audit.sha256_file(runtime)},
    )

    findings = security_audit.audit_package_manifest(package)

    assert not any(
        finding.code == "UNMANIFESTED_PACKAGE_FILE"
        and finding.path.endswith("release_manifest.sig")
        for finding in findings
    )


def test_package_audit_rejects_crown_jewel_python_and_unapproved_models(tmp_path):
    package = tmp_path / "package"
    crown_dir = package / "native_orion" / "backend"
    crown_dir.mkdir(parents=True)
    (crown_dir / "simple_meter_reader.py").write_text("# embedded only\n", encoding="utf-8")
    # The compiled sidecar entry point is source-bound and must never ship loose.
    (crown_dir / "autogreen_sidecar.py").write_text("# launcher\n", encoding="utf-8")
    model_dir = package / "models" / "experimental"
    model_dir.mkdir(parents=True)
    (model_dir / "meter-v99.pt").write_bytes(b"unapproved")
    (model_dir / "meter-v99.json").write_text("{}", encoding="utf-8")

    findings = security_audit.audit_package_structure(package)
    crown_paths = {f.path for f in findings if f.code == "CROWN_JEWEL_SOURCE_PACKAGED"}
    model_paths = {f.path for f in findings if f.code == "UNAPPROVED_MODEL_PACKAGED"}

    assert any(path.endswith("native_orion/backend/simple_meter_reader.py") for path in crown_paths)
    assert any(path.endswith("native_orion/backend/autogreen_sidecar.py") for path in crown_paths)
    assert any(path.endswith("models/experimental/meter-v99.pt") for path in model_paths)
    assert any(path.endswith("models/experimental/meter-v99.json") for path in model_paths)


def test_package_audit_requires_tls_backend_for_https(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    for name in security_audit.ESSENTIAL_PACKAGE_FILES - {"tls/qschannelbackend.dll"}:
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"placeholder")

    findings = security_audit.audit_package_structure(package)

    assert any(f.code == "PACKAGE_ESSENTIAL_MISSING" and f.path.endswith("tls/qschannelbackend.dll") for f in findings)


def test_package_audit_rejects_loose_orion_qml_source(tmp_path):
    package = tmp_path / "package"
    source = package / "OrionStaff" / "qml" / "admin"
    source.mkdir(parents=True)
    (source / "AdminMain.qml").write_text("import QtQuick\n", encoding="utf-8")

    findings = security_audit.audit_package_structure(package)

    assert any(f.code == "APP_QML_SOURCE_PACKAGED" for f in findings)


def test_package_audit_rejects_qml_debug_plugins_and_removed_psn_helper(tmp_path):
    package = tmp_path / "package"
    (package / "chiaki-ng-orion" / "chiaki-ng-Win" / "qmltooling").mkdir(parents=True)
    (package / "chiaki-ng-orion" / "chiaki-ng-Win" / "qmltooling" / "qmldbg_tcp.dll").write_bytes(b"debug")
    (package / "plugins").mkdir()
    (package / "plugins" / "qmldbg_server.dll").write_bytes(b"debug")
    (package / "Get-PSN-AccountID.bat").write_text("@echo off\n", encoding="utf-8")
    (package / "tools").mkdir()
    (package / "tools" / "get_psn_account_id.py").write_text("print('removed')\n", encoding="utf-8")

    findings = security_audit.audit_package_structure(package)
    codes = {f.code for f in findings}

    assert "FORBIDDEN_PACKAGE_ARTIFACT" in codes
    assert "QML_DEBUGGER_PLUGIN_PACKAGED" in codes


def test_package_audit_scans_binary_strings_for_dev_keys(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "OrionStaff.exe").write_bytes(
        b"\x00\x01compiled-data\x00NVDEV-20260524-BE66CE5F\x00more-data"
    )

    findings = security_audit.audit_package_secret_content(package)

    assert any(f.code == "CONCRETE_DEV_KEY" and f.path.endswith(":strings") for f in findings)


def test_security_policy_must_fail_closed(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    policy = {
        "schema": "orion.security_policy.v1",
        "require_release_manifest": False,
        "allow_local_dev_bypass": True,
    }
    (package / "security_policy.json").write_text(json.dumps(policy), encoding="utf-8")
    _write_package_manifest(package, {})

    findings = security_audit.audit_package_manifest(package)
    codes = {f.code for f in findings}

    assert "RELEASE_MANIFEST_NOT_REQUIRED" in codes
    assert "LOCAL_DEV_BYPASS_ALLOWED" in codes
