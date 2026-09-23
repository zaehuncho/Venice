#!/usr/bin/env python3
"""Build a hardened Orion runtime package from native_orion/build/Release.

The package is intentionally runtime-only. It excludes developer symbols,
import libraries, tests, local settings, vault material, logs, DBs, and caches.
It also writes a release manifest used by SecurityCore to fail closed when a
shipped runtime file is changed.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

try:
    from release_filter_policy import (
        COMPILED_SERVICE_REQUIRED_FILES,
        COMPILED_SIDECAR_REQUIRED_FILES,
        CROWN_JEWEL_PY_NAMES,
        FORBIDDEN_FILE_SUFFIXES as FORBIDDEN_SUFFIXES,
        FORBIDDEN_NAME_SUFFIXES,
        FORBIDDEN_PATH_COMPONENTS as FORBIDDEN_NAMES,
        is_crown_jewel_python,
        is_debug_dll,
        is_forbidden_file_name,
        is_release_admitted_executable,
        is_stale_binary_name,
        is_unapproved_model_path,
    )
except ModuleNotFoundError:  # Imported by path from the repository root in tests/tools.
    from tools.release_filter_policy import (
        COMPILED_SERVICE_REQUIRED_FILES,
        COMPILED_SIDECAR_REQUIRED_FILES,
        CROWN_JEWEL_PY_NAMES,
        FORBIDDEN_FILE_SUFFIXES as FORBIDDEN_SUFFIXES,
        FORBIDDEN_NAME_SUFFIXES,
        FORBIDDEN_PATH_COMPONENTS as FORBIDDEN_NAMES,
        is_crown_jewel_python,
        is_debug_dll,
        is_forbidden_file_name,
        is_release_admitted_executable,
        is_stale_binary_name,
        is_unapproved_model_path,
    )

try:
    from sidecar_bundle_manifest import (
        MANIFEST_NAME as SIDECAR_BUILD_MANIFEST_NAME,
        verify_manifest as verify_sidecar_build_manifest,
    )
except ModuleNotFoundError:  # Imported by path from the repository root in tests/tools.
    from tools.sidecar_bundle_manifest import (
        MANIFEST_NAME as SIDECAR_BUILD_MANIFEST_NAME,
        verify_manifest as verify_sidecar_build_manifest,
    )


ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = ROOT / "native_orion" / "build" / "Release"
PACKAGE_DIR = ROOT / "release" / "orion-package"
ARCHIVE_DIR = ROOT / "archive" / "local-runtime"
RELEASE_DIR = ROOT / "release"

# ── auto-updater manifest (must stay byte-compatible with backend/lambda_function.py) ──
# The server signer (sign_manifest_ed25519) and the native verifier
# (native_orion/src/UpdateManifest.cpp canonicalManifestSigningString) both produce the
# compact JSON of EXACTLY these fields in EXACTLY this order. Do not reorder.
MANIFEST_SIGN_FIELDS = [
    "latest_version", "minimum_supported_version",
    "artifact_url", "sha256", "published_at",
    "mandatory", "allow_rollback", "public_key_id",
]
ED25519_KEY_ID = "orion-ed25519-v1"
ED25519_PUBLIC_KEY_B64 = "OJQ2E7ZAFClOCM4S4/5QzLeQjjEMSZjPiGMzbiLZdWs="
SIGNING_KEY_ENV = "ORION_UPDATE_SIGNING_KEY_PEM"  # path to a local Ed25519 private key PEM
RELEASE_MANIFEST_NAME = "release_manifest.json"
RELEASE_MANIFEST_SIG_NAME = "release_manifest.sig"

TOP_LEVEL_DIRS = {
    "generic",
    "iconengines",
    "imageformats",
    "networkinformation",
    "platforms",
    "qml",
    "tls",
}

CHIAKI_RUNTIME_SOURCE = ROOT / "native_orion" / "deploy" / "chiaki-ng-orion" / "chiaki-ng-Win"
CHIAKI_PACKAGE_RELATIVE_DIR = Path("chiaki-ng-orion") / "chiaki-ng-Win"

# Nuitka-compiled sidecar bundle (docs/IP_PROTECTION_PLAN.md Phase B/D). Production requires this
# executable and cannot fall back to loose Python sources. Its source-binding manifest prevents a
# release from silently combining current native code with an older detector executable. Dev builds
# remain source-default and may opt in to a different compiled bundle only through
# the explicit --allow-dev-build + --dev-sidecar-dist CLI pair. Environment state
# is intentionally ignored so a production invocation cannot be redirected.
SIDECAR_DIST_DEFAULT = ROOT / "build" / "sidecar" / "autogreen_sidecar.dist"

# WAVE 3 (2026-08-08): the inbound-meter-delay packet bridge is the C++ VeniceNetSvc.exe
# (native_orion/venicenet_service, wave 2A) — a single self-contained Win32 exe with NO
# Python runtime, replacing the Nuitka-compiled nexus_svc.py bundle. It ships in the same
# packet_bridge/ subdirectory the legacy bundle used (the app's availability comments and
# the installer's fail-soft probe both know that path). A loose nexus_svc.py + .venv311
# interpreter must NEVER ship (docs/IP_PROTECTION_PLAN.md:360), and the retired Nuitka
# bundle must not either — only the exe + the WinDivert driver pair land in the package.
SERVICE_DIST_DEFAULT = ROOT / "native_orion" / "build" / "venicenet_service" / "Release"
PACKET_BRIDGE_SUBDIR = "packet_bridge"
VENICE_SERVICE_EXE = "VeniceNetSvc.exe"
# WinDivert kernel driver + user-mode DLL. Loaded on demand by the elevated bridge
# (VeniceNetSvc GetProcAddress -> WinDivert64.dll -> WinDivert64.sys). The pair staged
# next to the built exe (CMake POST_BUILD, from vendor/windivert) is preferred; the
# committed vendor dir is the fallback so the shipped .sys/.dll always match what the
# service was built against.
WINDIVERT_VENDOR_DIR = ROOT / "vendor" / "windivert"
WINDIVERT_FILES = ("WinDivert64.dll", "WinDivert64.sys")

ESSENTIAL_FILES = {
    "OrionNative.exe",
    "OrionOwner.exe",
    "OrionStaff.exe",
    "OrionUpdater.exe",
    "OrionCommon.dll",
    "AutomationCore.dll",
    "VisionCore.dll",
    "RemotePlayCore.dll",
    "SecurityCore.dll",
    "UpdaterCore.dll",
    "ViGEmClient.dll",
    "libcrypto-3-x64.dll",  # OpenSSL: required by OrionUpdater for Ed25519 verification
    "opencv_world4110.dll", # OpenCV: the exe imports it at load — a missing copy is a
                            # hard "DLL not found" launch failure (a stale package shipped
                            # without it). Listed here so packaging fails loud, not silent.
}

# Internal-only executables must be absent from customer packages and manifests.
# The installer exclusion remains defence in depth for older package archives.
# [ORION_QT_STYLE_PRUNE 2026-09-21] The Qt style allowlist is canonical in release_filter_policy
# so the packager's prune and the independent audit read ONE policy.
try:
    from release_filter_policy import (
        QT_QUICK_KNOWN_STYLES,
        QT_QUICK_NATIVE_STYLE_CONSUMER,
        QT_QUICK_STYLE_ROOTS,
        QT_QUICK_STYLE_SHARED_DIRS,
        qt_quick_style_allowed_root_dlls,
        qt_quick_style_requirements_missing,
        qt_quick_style_violations,
    )
except ModuleNotFoundError:  # imported by path from the repository root
    from tools.release_filter_policy import (
        QT_QUICK_KNOWN_STYLES,
        QT_QUICK_NATIVE_STYLE_CONSUMER,
        QT_QUICK_STYLE_ROOTS,
        QT_QUICK_STYLE_SHARED_DIRS,
        qt_quick_style_allowed_root_dlls,
        qt_quick_style_requirements_missing,
        qt_quick_style_violations,
    )

CUSTOMER_EXCLUDED_FILES = {"OrionOwner.exe", "OrionStaff.exe"}


def strip_customer_excluded_files(package_dir: Path) -> None:
    """Keep privileged tools out of the customer ZIP, not merely its manifest."""
    for name in CUSTOMER_EXCLUDED_FILES:
        (package_dir / name).unlink(missing_ok=True)

# Crown-jewel and model policy is canonical in release_filter_policy.py and is
# shared with the independent security audit. Never redeclare it here.


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def should_copy_top_file(path: Path, *, server_shard: bool = False) -> bool:
    name = path.name
    if is_forbidden_file_name(name):
        # Forbidden beats essential: a name on both lists must never ship silently.
        # This now also drops lab/check binaries (GreenWindowMathChecks.exe) and any
        # stray *.packed.exe (except the one permitted server-shard payload name).
        return False
    if is_debug_dll(path):
        # MSVC debug DLL twin (opencv_world4110d.dll, Qt6Cored.dll, ...): never ships.
        return False
    if name in ESSENTIAL_FILES:
        return True
    suffix = path.suffix.lower()
    if suffix == ".exe":
        # Release blocker FIX: NEVER admit an arbitrary top-level .exe. Only the
        # per-profile executable allowlist ships; every unrecognized exe in the build
        # dir (a lab/check binary, a stray packer output, a one-off tool) is dropped
        # here and separately flagged by the security audit, instead of leaking in.
        return is_release_admitted_executable(name, server_shard=server_shard)
    return suffix in {".dll", ".json"}


def _copy_tree_ignore(src: str, names: list[str]) -> set[str]:
    """copytree ignore callback: drop anything the forbidden scan would flag — forbidden
    directory names (codesigning/, .vault/, __pycache__/, ...) AND forbidden file
    names/suffixes/stale-binary patterns. Keeps the copy phase and scan_forbidden in
    lockstep instead of maintaining a second, weaker glob list."""
    ignored: set[str] = set()
    for name in names:
        lower = name.lower()
        full = Path(src) / name
        if lower in FORBIDDEN_NAMES:
            ignored.add(name)
        elif full.is_file() and (is_forbidden_file_name(name) or is_debug_dll(full)):
            ignored.add(name)
    return ignored


def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=_copy_tree_ignore)


def copy_chiaki_runtime(package_dir: Path | None = None) -> None:
    package_dir = (package_dir or PACKAGE_DIR).resolve()
    stream_exe = CHIAKI_RUNTIME_SOURCE / "OrionStream.exe"
    if not stream_exe.exists():
        raise SystemExit(f"Custom Chiaki runtime missing: {stream_exe}")
    copy_tree(CHIAKI_RUNTIME_SOURCE, package_dir / CHIAKI_PACKAGE_RELATIVE_DIR)


def _write_archive_manifest(target: Path, source: Path) -> None:
    manifest = {
        "archived_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": str(source),
        "target": str(target),
        "reason": "package_orion_release regenerated runtime package",
    }
    (target / "ARCHIVE_MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def archive_existing_package(package_dir: Path | None = None,
                             archive_dir: Path | None = None) -> Path | None:
    """Move an existing derived package into the recoverable local archive.

    If metadata creation fails, restore the original package rather than leaving
    the public package path empty.
    """
    package_dir = (package_dir or PACKAGE_DIR).resolve()
    archive_dir = (archive_dir or ARCHIVE_DIR).resolve()
    if not package_dir.exists():
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target = archive_dir / f"orion-package-{stamp}"
    if target.exists():
        raise RuntimeError(f"Refusing to overwrite existing package archive: {target}")
    package_dir.replace(target)
    try:
        _write_archive_manifest(target, package_dir)
    except BaseException:
        target.replace(package_dir)
        raise
    return target


def create_package_staging_dir(package_dir: Path | None = None) -> Path:
    """Create a private, same-volume staging directory beside the package path."""
    package_dir = (package_dir or PACKAGE_DIR).resolve()
    package_dir.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(
        prefix=f".{package_dir.name}.staging-",
        dir=package_dir.parent,
    )).resolve()


def _validate_staging_path(staging_dir: Path, package_dir: Path) -> tuple[Path, Path]:
    staging_dir = staging_dir.resolve()
    package_dir = package_dir.resolve()
    expected_prefix = f".{package_dir.name}.staging-"
    if staging_dir.parent != package_dir.parent or not staging_dir.name.startswith(expected_prefix):
        raise ValueError(
            f"Refusing non-packager staging path {staging_dir}; expected a sibling named "
            f"{expected_prefix}*"
        )
    return staging_dir, package_dir


def cleanup_package_staging_dir(staging_dir: Path,
                                package_dir: Path | None = None) -> None:
    """Remove only a packager-created derived staging tree, never a source tree."""
    package_dir = (package_dir or PACKAGE_DIR).resolve()
    staging_dir, _ = _validate_staging_path(staging_dir, package_dir)
    if staging_dir.exists():
        shutil.rmtree(staging_dir)


def write_security_policy(package_dir: Path) -> None:
    policy = {
        "schema": "orion.security_policy.v1",
        "require_release_manifest": True,
        "lock_automation_on_integrity_failure": True,
        "lock_automation_on_debugger": True,
        "lock_automation_on_analysis_tool": True,
        "allow_local_dev_bypass": False,
        "notes": "Production runtime policy. SecurityCore fails closed for automation when integrity checks fail.",
    }
    (package_dir / "security_policy.json").write_text(json.dumps(policy, indent=2), encoding="utf-8")


def sidecar_dist_dir(*, allow_dev_build: bool = False,
                     dev_override: str | Path | None = None) -> Path:
    """Resolve the compiled sidecar source without trusting process environment.

    Production has one canonical, source-bound input. A developer may point at a
    different bundle only by combining the conspicuous --allow-dev-build and
    --dev-sidecar-dist options; the production path rejects that combination.
    """
    if dev_override:
        if not allow_dev_build:
            raise SystemExit(
                "[orion-package] REFUSED: --dev-sidecar-dist requires --allow-dev-build; "
                "production always uses build/sidecar/autogreen_sidecar.dist"
            )
        return Path(dev_override).resolve()
    return SIDECAR_DIST_DEFAULT.resolve()


def copy_compiled_sidecar(package_dir: Path, dist: Path | None = None) -> bool:
    """Copy the current, source-bound Nuitka sidecar into the package root.

    Production cannot use loose Python as a fallback. Standalone Nuitka requires the executable
    to remain beside its dependency files, so the whole .dist tree lands at the package root.

    Missing, stale, or tampered bundles fail packaging before any release manifest is written.
    Forbidden-file filtering still applies to every bundled dependency."""
    dist = (dist or sidecar_dist_dir()).resolve()
    exe = dist / "OrionSidecar.exe"
    if not dist.is_dir() or not exe.is_file():
        raise SystemExit(
            "[orion-package] compiled OrionSidecar.exe is required for production; "
            "run scripts/build_orion_sidecar.ps1"
        )
    freshness_failures = verify_sidecar_build_manifest(ROOT, dist)
    if freshness_failures:
        details = "\n  - ".join(freshness_failures)
        raise SystemExit(
            "[orion-package] compiled sidecar is stale or invalid; "
            "run scripts/build_orion_sidecar.ps1:\n  - " + details
        )
    copies: list[tuple[Path, Path]] = []
    collisions: list[str] = []
    rejected: list[str] = []
    for item in dist.rglob("*"):
        if not item.is_file():
            continue
        rel = item.relative_to(dist)
        if any(part.lower() in FORBIDDEN_NAMES for part in rel.parts):
            rejected.append(f"{rel.as_posix()} (forbidden path component)")
            continue
        if is_forbidden_file_name(item.name) or is_debug_dll(item):
            rejected.append(f"{rel.as_posix()} (forbidden/debug file)")
            continue
        if is_unapproved_model_path(rel):
            rejected.append(f"{rel.as_posix()} (unapproved sidecar model)")
            continue
        dst = package_dir / rel
        if dst.exists():
            collisions.append(rel.as_posix())
            continue
        copies.append((item, dst))
    if rejected:
        raise SystemExit(
            "[orion-package] REFUSED: verified sidecar bundle contains files that release policy "
            "would omit (bundle must be rebuilt cleanly):\n  - "
            + "\n  - ".join(sorted(rejected))
        )
    if collisions:
        raise SystemExit(
            "[orion-package] REFUSED: compiled sidecar would overwrite existing runtime files:\n  - "
            + "\n  - ".join(sorted(collisions))
        )
    for item, dst in copies:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, dst)

    # v2 binds every standalone dependency. Prove the package contains that exact
    # inventory after copy; filtering, collisions, or a post-verify source race can
    # never create a partial bundle that launches with missing/swapped dependencies.
    try:
        bundle_manifest = json.loads(
            (dist / SIDECAR_BUILD_MANIFEST_NAME).read_text(encoding="utf-8")
        )
        recorded_files = bundle_manifest["files"]
        if not isinstance(recorded_files, dict) or not recorded_files:
            raise ValueError("files map missing or empty")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"[orion-package] invalid verified sidecar bundle manifest during copy: {exc}"
        ) from exc

    copied_failures: list[str] = []
    for rel_name, metadata in recorded_files.items():
        rel = Path(str(rel_name))
        if rel.is_absolute() or ".." in rel.parts or not isinstance(metadata, dict):
            copied_failures.append(f"unsafe/invalid manifest entry: {rel_name}")
            continue
        destination = package_dir / rel
        expected_hash = str(metadata.get("sha256", "")).lower()
        expected_size = metadata.get("size")
        if (not destination.is_file()
                or destination.stat().st_size != expected_size
                or sha256_file(destination) != expected_hash):
            copied_failures.append(str(rel_name))
    manifest_source = dist / SIDECAR_BUILD_MANIFEST_NAME
    manifest_destination = package_dir / SIDECAR_BUILD_MANIFEST_NAME
    if (not manifest_destination.is_file()
            or manifest_destination.read_bytes() != manifest_source.read_bytes()):
        copied_failures.append(SIDECAR_BUILD_MANIFEST_NAME)
    if copied_failures:
        raise SystemExit(
            "[orion-package] copied sidecar bundle does not match its v2 files map:\n  - "
            + "\n  - ".join(sorted(copied_failures))
        )
    return True


def _find_windivert_source(service_dist: Path) -> Path | None:
    """Directory holding a matched WinDivert64.dll/.sys pair, or None.

    Prefers the pair staged next to the built service exe (the venicenet_service
    CMake POST_BUILD copies vendor/windivert there, so it is guaranteed to match
    what the exe was built against), then the committed vendor override.
    """
    candidates: list[Path] = []
    for sys_path in service_dist.rglob("WinDivert64.sys"):
        candidates.append(sys_path.parent)
    candidates.append(WINDIVERT_VENDOR_DIR)
    for directory in candidates:
        if all((directory / name).is_file() for name in WINDIVERT_FILES):
            return directory
    return None


_THIRD_PARTY_NOTICE = """\
Third-party components bundled with the Venice packet bridge
============================================================

WinDivert (WinDivert64.dll, WinDivert64.sys)
    Copyright (C) basil00 and contributors.
    Dual-licensed under your choice of the GNU Lesser General Public License
    (LGPL) version 3, or the GNU General Public License (GPL) version 2.
    Bundled UNMODIFIED. Source: https://github.com/basil00/WinDivert and
    https://reqrypt.org/windivert.html (this build ships WinDivert 2.2.2-A).

This component is used under the LGPL option and is dynamically loaded
(GetProcAddress at runtime); you may replace it with a compatible build. The
full licence text accompanies the upstream source distribution.
"""


def copy_compiled_service(package_dir: Path, dist: Path | None = None) -> bool:
    """Copy the C++ packet-bridge service + its WinDivert driver (wave 3, 2026-08-08).

    The elevated VeniceNetSvc host and the on-demand WinDivert kernel driver are
    what make the inbound meter delay actually apply on an installed build. They land
    in the PACKET_BRIDGE_SUBDIR (same subdir the retired Nuitka bundle used) so the
    installer's registration probe and the app's availability comments keep pointing
    at one canonical path.

    Unlike the Nuitka era, the service build directory is NOT a shippable dist tree —
    it is a CMake output dir that also holds venicenet_svc.log, test executables, and
    intermediate artifacts. So this stages an EXPLICIT allow-list (the exe + the
    WinDivert pair + the licence notice), never a recursive copy. Missing exe or
    driver fails packaging loudly; the release-policy filters are still applied to
    the staged names as a belt-and-braces check."""
    dist = (dist or SERVICE_DIST_DEFAULT).resolve()
    exe = dist / VENICE_SERVICE_EXE
    if not dist.is_dir() or not exe.is_file():
        raise SystemExit(
            f"[orion-package] compiled {VENICE_SERVICE_EXE} is required to ship the "
            "inbound meter delay; build it: cmake --build native_orion/build "
            "--config Release --target VeniceNetSvc"
        )
    windivert_src = _find_windivert_source(dist)
    if windivert_src is None:
        raise SystemExit(
            "[orion-package] WinDivert driver pair (WinDivert64.dll + WinDivert64.sys) "
            f"not found in the service build dir {dist} or in {WINDIVERT_VENDOR_DIR}; the "
            "meter-delay bridge cannot open a packet handle without it"
        )

    bridge_dir = package_dir / PACKET_BRIDGE_SUBDIR
    if bridge_dir.exists():
        raise SystemExit(
            f"[orion-package] REFUSED: packet-bridge destination already exists: {bridge_dir}"
        )

    staged: list[Path] = [exe] + [windivert_src / name for name in WINDIVERT_FILES]
    rejected = [
        item.name
        for item in staged
        if is_forbidden_file_name(item.name) or is_debug_dll(item)
    ]
    if rejected:
        raise SystemExit(
            "[orion-package] REFUSED: packet-bridge staging would ship files that release "
            "policy forbids: " + ", ".join(sorted(rejected))
        )

    bridge_dir.mkdir(parents=True, exist_ok=False)
    for item in staged:
        shutil.copy2(item, bridge_dir / item.name)

    (bridge_dir / "THIRD_PARTY_NOTICES.txt").write_text(_THIRD_PARTY_NOTICE, encoding="utf-8")
    return True


def copy_runtime(sidecar_dist: Path | None = None,
                 package_dir: Path | None = None,
                 service_dist: Path | None = None) -> None:
    package_dir = (package_dir or PACKAGE_DIR).resolve()
    if not BUILD_DIR.exists():
        raise SystemExit(f"Build output not found: {BUILD_DIR}")

    missing = [name for name in ESSENTIAL_FILES if not (BUILD_DIR / name).exists()]
    if missing:
        raise SystemExit(f"Missing essential runtime files: {', '.join(sorted(missing))}")

    package_dir.mkdir(parents=True, exist_ok=True)
    if any(package_dir.iterdir()):
        raise SystemExit(
            f"[orion-package] REFUSED: runtime copy destination is not empty: {package_dir}"
        )

    for item in BUILD_DIR.iterdir():
        if item.is_file() and should_copy_top_file(item):
            shutil.copy2(item, package_dir / item.name)
        elif item.is_dir() and item.name in TOP_LEVEL_DIRS:
            copy_tree(item, package_dir / item.name)

    for rel_dir in ("assets", "meter_styles"):
        src = ROOT / rel_dir
        if src.exists():
            copy_tree(src, package_dir / rel_dir)

    copy_chiaki_runtime(package_dir)

    # Production always launches the source-bound OrionSidecar.exe. Do not ship
    # its 160KB Python entry point as dead, readable source. The standalone PS5
    # setup helper remains loose and lands at the exact app-relative path used by
    # RemotePlaySession::helperPath().
    backend_dst = package_dir / "backend"
    backend_dst.mkdir(parents=True, exist_ok=True)
    for script in ("ps5_remoteplay_helper.py",):
        src = ROOT / "native_orion" / "backend" / script
        if src.exists():
            shutil.copy2(src, backend_dst / script)

    selected_sidecar_dist = (sidecar_dist or sidecar_dist_dir()).resolve()
    copy_compiled_sidecar(package_dir, selected_sidecar_dist)
    print(f"[orion-package] included source-bound OrionSidecar.exe bundle from {selected_sidecar_dist}")

    selected_service_dist = (service_dist or SERVICE_DIST_DEFAULT).resolve()
    copy_compiled_service(package_dir, selected_service_dist)
    print(f"[orion-package] included compiled {VENICE_SERVICE_EXE} + WinDivert bridge from {selected_service_dist}")

    write_security_policy(package_dir)
    write_runtime_readme(package_dir)
    strip_app_qml_source(package_dir)
    # [ORION_QT_STYLE_PRUNE 2026-09-21] A root whose SOURCE deploys Qt must have it staged.
    required_roots = set()
    if (BUILD_DIR / "qml" / "QtQuick" / "Controls").is_dir():
        required_roots.add(".")
    # The stream runtime is copied whole by copy_chiaki_runtime(); a copied tree that lost its
    # Controls is a broken stream. (A fixture that stubs the copy has no stream dir at all, and
    # a missing OrionStream.exe is refused by copy_chiaki_runtime and the audit's essentials.)
    if ((CHIAKI_RUNTIME_SOURCE / "qml" / "QtQuick" / "Controls").is_dir()
            and (package_dir / CHIAKI_PACKAGE_RELATIVE_DIR).is_dir()):
        required_roots.add(CHIAKI_PACKAGE_RELATIVE_DIR.as_posix())
    prune_unused_quick_styles(package_dir, required_roots=frozenset(required_roots))


def scan_forbidden(package_dir: Path) -> list[str]:
    findings: list[str] = []
    for path in package_dir.rglob("*"):
        rel = path.relative_to(package_dir).as_posix()
        parts = {part.lower() for part in path.relative_to(package_dir).parts}
        if any(part in FORBIDDEN_NAMES for part in parts):
            findings.append(rel)
            continue
        if path.is_file() and is_crown_jewel_python(path):
            # A crown-jewel reader/timing module must never ship as readable source (see
            # CROWN_JEWEL_PY_NAMES) — the compiled OrionSidecar.exe embeds it.
            findings.append(rel)
            continue
        if path.is_file() and is_forbidden_file_name(path.name):
            findings.append(rel)
            continue
        if path.is_file() and is_debug_dll(path):
            # MSVC debug DLL twin (opencv_world4110d.dll, Qt6Cored.dll, ...) must never ship.
            findings.append(rel)
            continue
        if path.is_file() and is_unapproved_model_path(path.relative_to(package_dir)):
            # Timing JSON is admitted only as part of the verified sidecar bundle; every
            # other models/ file is development baggage.
            findings.append(rel)
    return sorted(findings)


def _load_security_audit():
    existing = sys.modules.get("security_audit")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("security_audit", ROOT / "tools" / "security_audit.py")
    mod = importlib.util.module_from_spec(spec)
    # Must be registered BEFORE exec: @dataclass resolves cls.__module__ via sys.modules.
    sys.modules["security_audit"] = mod
    spec.loader.exec_module(mod)
    return mod


def scan_secret_content(package_dir: Path) -> list[str]:
    """Content-level secret scan of everything that would SHIP (text files + strings of the
    Orion binaries): AWS keys, Discord bot tokens, private-key PEM blocks, concrete dev
    keys, Stripe/GitHub tokens. Name-based exclusion alone cannot catch a secret pasted
    into a legitimately-shipped file. Delegates to tools/security_audit.py so the two
    gates share one pattern set."""
    audit = _load_security_audit()
    return sorted(
        f"{finding.path} [{finding.severity} {finding.code}]"
        for finding in audit.audit_package_secret_content(package_dir)
    )


def validate_staged_package_for_publish(staging_dir: Path,
                                        *, require_signature: bool) -> None:
    """Fail closed before the staging tree can replace the published package."""
    required = set(COMPILED_SIDECAR_REQUIRED_FILES) | set(COMPILED_SERVICE_REQUIRED_FILES) | {
        RELEASE_MANIFEST_NAME,
        "security_policy.json",
    }
    if require_signature:
        required.add(RELEASE_MANIFEST_SIG_NAME)
    missing = sorted(name for name in required if not (staging_dir / name).is_file())
    if missing:
        raise SystemExit(
            "[orion-package] REFUSED: staged package is incomplete; missing: "
            + ", ".join(missing)
        )
    forbidden = scan_forbidden(staging_dir)
    if forbidden:
        raise SystemExit(
            "[orion-package] REFUSED: staged package contains forbidden files:\n  - "
            + "\n  - ".join(forbidden)
        )
    secrets = scan_secret_content(staging_dir)
    if secrets:
        raise SystemExit(
            "[orion-package] REFUSED: staged package contains secret-looking content:\n  - "
            + "\n  - ".join(secrets)
        )


def publish_staged_package(staging_dir: Path,
                           package_dir: Path | None = None,
                           archive_dir: Path | None = None,
                           *, require_signature: bool = True) -> Path | None:
    """Publish a validated package, restoring the prior one if the swap fails."""
    package_dir = (package_dir or PACKAGE_DIR).resolve()
    archive_dir = (archive_dir or ARCHIVE_DIR).resolve()
    staging_dir, package_dir = _validate_staging_path(staging_dir, package_dir)
    if not staging_dir.is_dir():
        raise FileNotFoundError(f"Package staging directory is missing: {staging_dir}")
    validate_staged_package_for_publish(staging_dir, require_signature=require_signature)

    archived = archive_existing_package(package_dir, archive_dir)
    try:
        staging_dir.replace(package_dir)
    except BaseException:
        if archived is not None and not package_dir.exists():
            (archived / "ARCHIVE_MANIFEST.json").unlink(missing_ok=True)
            archived.replace(package_dir)
        raise
    return archived


def publish_release_unit(staging_dir: Path, staged_archive: Path, staged_manifest: Path,
                         package_dir: Path, archive_path: Path, manifest_path: Path,
                         *, require_signature: bool = True,
                         archive_dir: Path | None = None) -> None:
    """Commit one preverified release unit, restoring every previous output on failure.

    Windows has no atomic rename of three independent paths. All three are built and
    verified privately first; same-volume renames form a rollback-capable commit. A
    failed commit leaves the prior triple byte-for-byte intact. Consumers must not
    upload from these paths until this function returns successfully.
    """
    staging_dir, package_dir = _validate_staging_path(staging_dir, package_dir)
    validate_staged_package_for_publish(staging_dir, require_signature=require_signature)
    archive_path = Path(archive_path).resolve()
    manifest_path = Path(manifest_path).resolve()
    staged_archive = Path(staged_archive).resolve()
    staged_manifest = Path(staged_manifest).resolve()
    if not staged_archive.is_file() or not staged_manifest.is_file():
        raise FileNotFoundError("Staged archive or update manifest is missing")
    targets = ((staging_dir, package_dir),
               (staged_archive, archive_path),
               (staged_manifest, manifest_path))
    if len({str(target).lower() for _, target in targets}) != 3:
        raise ValueError("Release publication targets must be distinct")
    token = uuid.uuid4().hex
    backups: list[tuple[Path, Path]] = []
    installed: list[tuple[Path, Path]] = []
    try:
        for _, target in targets:
            if target.exists():
                backup = target.with_name(f".{target.name}.previous-{token}")
                target.replace(backup)
                backups.append((backup, target))
        for source, target in targets:
            source.replace(target)
            installed.append((source, target))
    except BaseException:
        # Reverse new publications before restoring any old name. Keep the staged
        # files available to the caller's cleanup even on a partially failed swap.
        for source, target in reversed(installed):
            if target.exists():
                target.replace(source)
        for backup, target in reversed(backups):
            if backup.exists():
                backup.replace(target)
        raise
    for backup, original in backups:
        if original == package_dir and archive_dir is not None:
            archive_dir = Path(archive_dir).resolve()
            archive_dir.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
            archived = archive_dir / f"orion-package-{stamp}"
            try:
                backup.replace(archived)
                _write_archive_manifest(archived, package_dir)
            except BaseException as exc:
                # The coherent newly published unit is already selected. Retain
                # the old backup under its private sibling name if archiving
                # fails; never falsely report a failed publication after commit.
                print(f"[orion-package] WARNING: previous package archive incomplete: {exc}")
            continue
        if backup.is_dir():
            shutil.rmtree(backup)
        else:
            backup.unlink()


def strip_app_qml_source(package_dir: Path) -> None:
    """Remove copied Orion QML source; the app loads compiled QML resources."""
    qml_dir = package_dir / "qml"
    for rel in ("Main.qml", "admin", "components", "pages"):
        target = qml_dir / rel
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    for module_dir in ("OrionNative", "OrionOwner", "OrionStaff"):
        target = package_dir / module_dir
        if target.exists():
            shutil.rmtree(target)


# [ORION_QT_STYLE_PRUNE 2026-09-21] Qt Quick Controls ships one directory per visual style, and
# windeployqt copies every one of them because the style is picked at runtime, not by an import
# it can scan. Each app in this package pins exactly one (the allowlist and the reasoning live in
# release_filter_policy.QT_QUICK_STYLE_ROOTS), so the rest is dead weight: FluentWinUI3 alone is
# ~840 PNG/QML files per copy, and the package carries two Qt runtimes (OrionNative +
# OrionStream), so the unused styles were 2,272 of the 2,940 packaged files (2026-09-21 count).
#
# FAIL-CLOSED, three ways (2026-09-21 Codex review):
#   * a root the SOURCE build deploys Qt for (`required_roots`) must have a Controls tree in the
#     staged package -- a silent no-op on a real build would ship a launcher with no UI;
#   * a pinned style must be COMPLETE (qmldir, its QML plugin, its paired StyleImpl DLL, the
#     shared impl plugin and the two core DLLs) -- a present-but-empty style directory renders an
#     empty window, not a crash;
#   * after pruning, release_filter_policy.qt_quick_style_violations() -- the same check the
#     independent audit runs -- must come back empty.
def prune_unused_quick_styles(package_dir: Path,
                              required_roots: frozenset[str] = frozenset()) -> dict[str, int]:
    """Drop every Qt Quick Controls style no packaged app selects (plus its DLLs and selectors).

    Returns {"styles": <style dirs removed>, "files": <files removed>}. A root with no Qt
    deployment is left alone unless it is in `required_roots`.
    """
    removed_styles = 0
    removed_files = 0
    for rel_root, keep in QT_QUICK_STYLE_ROOTS:
        app_root = package_dir / rel_root
        controls = app_root / "qml" / "QtQuick" / "Controls"
        if not controls.is_dir():
            if rel_root in required_roots:
                raise SystemExit(
                    f"[orion-package] REFUSED: {rel_root!r} has no qml/QtQuick/Controls tree in "
                    f"the staged package although the source build deploys one")
            continue
        missing = qt_quick_style_requirements_missing(app_root, keep)
        if missing:
            raise SystemExit(
                f"[orion-package] REFUSED: pinned Qt Quick Controls style(s) {sorted(keep)} under "
                f"{rel_root!r} are incomplete; missing: {', '.join(missing)}")
        doomed: list[Path] = []
        for entry in sorted(controls.iterdir()):
            if entry.is_dir() and entry.name not in keep and entry.name not in QT_QUICK_STYLE_SHARED_DIRS:
                doomed.append(entry)
        allowed_dlls = qt_quick_style_allowed_root_dlls(keep)
        for dll in sorted(app_root.glob("Qt6QuickControls2*.dll")):
            if dll.name not in allowed_dlls:
                doomed.append(dll)
        # Qt Quick Dialogs ships a `+<Style>` file-selector folder per style; one for a style the
        # app can never select is unreachable, and pruning it makes that unreachability a fact
        # the audit can check rather than a property of Qt's selector logic.
        for style in sorted(QT_QUICK_KNOWN_STYLES - keep):
            for selector in sorted((app_root / "qml").rglob("+" + style)):
                if selector.is_dir():
                    doomed.append(selector)
        native_style = app_root / "qml" / "QtQuick" / "NativeStyle"
        if QT_QUICK_NATIVE_STYLE_CONSUMER not in keep and native_style.is_dir():
            doomed.append(native_style)
        for target in doomed:
            if target.is_dir():
                removed_files += sum(1 for f in target.rglob("*") if f.is_file())
                shutil.rmtree(target)
                if target.parent == controls:
                    removed_styles += 1
            elif target.is_file():
                removed_files += 1
                target.unlink()
    leftovers = qt_quick_style_violations(package_dir)
    if leftovers:
        raise SystemExit(
            "[orion-package] REFUSED: Qt Quick style content survived the prune: "
            + ", ".join(leftovers))
    print(f"[orion-package] pruned {removed_styles} unused Qt Quick Controls styles "
          f"({removed_files} files) - each app keeps only the style it pins")
    return {"styles": removed_styles, "files": removed_files}


def write_runtime_readme(package_dir: Path) -> None:
    readme = """# Orion Native Runtime

This is the stripped Orion runtime package.

- Launch with `OrionNative.exe`.
- Keep `release_manifest.json` and `security_policy.json` next to the EXE.
- Local settings, license cache, logs, symbols, import libraries, tests, and vault
  material are intentionally not included.
- The bundled `chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe` is Orion's
  custom low-latency Remote Play client. Keep it with this package.
- If runtime files are modified, SecurityCore will lock automation.
"""
    (package_dir / "README.md").write_text(readme, encoding="utf-8")


def _release_manifest_bytes(manifest: dict) -> bytes:
    """Stable UTF-8/LF representation. These exact bytes are signed and verified."""
    return (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


def write_release_manifest_signature(package_dir: Path, key_path: Path) -> bytes:
    """Sign the already-written manifest bytes and emit deterministic base64url.

    Ed25519 signatures are deterministic for a given key/message. Writing bytes
    directly avoids Windows newline translation changing either the signed input
    or the detached signature representation.
    """
    manifest_path = package_dir / RELEASE_MANIFEST_NAME
    manifest_bytes = manifest_path.read_bytes()
    key = load_signing_key(key_path, package_dir=package_dir)
    signature = key.sign(manifest_bytes)
    encoded = base64.urlsafe_b64encode(signature).rstrip(b"=") + b"\n"
    (package_dir / RELEASE_MANIFEST_SIG_NAME).write_bytes(encoded)
    return signature


def write_release_manifest(package_dir: Path, version: str | None = None,
                           signing_key: Path | None = None,
                           public_key_id: str = ED25519_KEY_ID,
                           allow_unsigned_dev: bool = False,
                           customer: bool = True) -> dict:
    if signing_key is None and not allow_unsigned_dev:
        raise SystemExit(
            "[orion-package] REFUSED: release_manifest.json requires an Ed25519 signing key "
            "(allow_unsigned_dev is for explicit local development only)"
        )
    # Remove stale self/signature files before enumerating. Neither can hash itself,
    # and a signature from an earlier manifest must never survive regeneration.
    for name in (RELEASE_MANIFEST_NAME, RELEASE_MANIFEST_SIG_NAME):
        (package_dir / name).unlink(missing_ok=True)

    files: dict[str, dict[str, str | int]] = {}
    for path in sorted(p for p in package_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(package_dir).as_posix()
        if rel in (RELEASE_MANIFEST_NAME, RELEASE_MANIFEST_SIG_NAME):
            continue
        # Customer manifests must not reference internal admin/staff binaries — the
        # customer installer (installer/orion.iss:109) strips them, so listing them
        # here would fail SecurityManager::verifyReleaseIntegrity() on install.
        if customer and path.name in CUSTOMER_EXCLUDED_FILES:
            continue
        files[rel] = {
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }

    manifest = {
        "schema": "orion.release_manifest.v1",
        "audience": "customer" if customer else "internal",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "file_count": len(files),
        "files": files,
        "public_key_id": public_key_id,
        "signature_alg": "ed25519",
        "signature_required": signing_key is not None,
        "signature_note": (
            "Detached signature is stored in release_manifest.sig."
            if signing_key is not None
            else "UNSIGNED DEVELOPMENT MANIFEST - NEVER SHIP."
        ),
    }
    if version:
        manifest["version"] = version
    (package_dir / RELEASE_MANIFEST_NAME).write_bytes(_release_manifest_bytes(manifest))
    if signing_key is not None:
        write_release_manifest_signature(package_dir, signing_key)
    return manifest


# ── auto-updater artifact + signed manifest ──────────────────────────────────────────


def default_version() -> str:
    """Version of the build being packaged = PROJECT_VERSION from native_orion/CMakeLists.txt
    (compiled into the binaries as ORION_NATIVE_VERSION; appVersion() reports it to /api/update)."""
    text = (ROOT / "native_orion" / "CMakeLists.txt").read_text(encoding="utf-8", errors="replace")
    match = re.search(r"project\(\s*OrionNative\s+VERSION\s+([0-9]+(?:\.[0-9]+)*)", text)
    if not match:
        raise SystemExit("Could not parse PROJECT_VERSION from native_orion/CMakeLists.txt; pass --version")
    return match.group(1)


def create_archive(package_dir: Path, version: str, out_dir: Path | None = None) -> tuple[Path, str]:
    """Zip the package into the shippable update artifact and return (path, sha256).
    Entries are sorted and timestamps fixed so the same package bytes always produce the
    same archive hash (reproducible artifact == comparable hashes across rebuilds)."""
    out_dir = out_dir or RELEASE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"orion-package-{version}.zip"
    temporary = zip_path.with_name(f".{zip_path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(p for p in package_dir.rglob("*") if p.is_file()):
                rel = path.relative_to(package_dir).as_posix()
                info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, path.read_bytes())
        digest = sha256_file(temporary)
        temporary.replace(zip_path)
        return zip_path, digest
    finally:
        temporary.unlink(missing_ok=True)


def canonical_signing_payload(manifest: dict) -> bytes:
    """Byte-exact reproduction of the server signer (backend/lambda_function.py
    sign_manifest_ed25519) and of the native verifier's canonicalManifestSigningString():
    compact JSON of MANIFEST_SIGN_FIELDS in fixed order, ensure_ascii, no whitespace."""
    canonical = {field: manifest[field] for field in MANIFEST_SIGN_FIELDS}
    return json.dumps(canonical, sort_keys=False, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def decode_signature_bytes(encoded: str) -> bytes:
    """Accept the same encodings the native client does: 128-char hex, base64url
    (padding optional), or standard base64. Returns b'' when nothing decodes to 64 bytes."""
    text = (encoded or "").strip()
    if len(text) == 128:
        try:
            raw = bytes.fromhex(text)
            if len(raw) == 64:
                return raw
        except ValueError:
            pass
    for altchars in (b"-_", b"+/"):
        try:
            raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=altchars, validate=False)
            if len(raw) == 64:
                return raw
        except (ValueError, TypeError):
            continue
    return b""


def decode_ed25519_public_key(encoded: str) -> bytes:
    """Native decodeEd25519PublicKey(): 64-char hex or (url-safe) base64 of the raw 32 bytes."""
    text = (encoded or "").strip()
    if len(text) == 64:
        try:
            raw = bytes.fromhex(text)
            if len(raw) == 32:
                return raw
        except ValueError:
            pass
    for altchars in (b"+/", b"-_"):
        try:
            raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=altchars, validate=False)
            if len(raw) == 32:
                return raw
        except (ValueError, TypeError):
            continue
    return b""


# [2026-09-21] The signing key may stay ENCRYPTED on disk (PKCS8 "ENCRYPTED PRIVATE KEY" or a
# legacy "Proc-Type: 4,ENCRYPTED" PEM). The passphrase is taken from this environment variable
# or, when nothing is set and the run is interactive, from a hidden prompt. It is never
# accepted on the command line (process lists, shell history), never logged, and the key is
# decrypted in memory only -- no plaintext copy is ever written.
SIGNING_KEY_PASSPHRASE_ENV = "ORION_UPDATE_SIGNING_KEY_PASSPHRASE"


def _signing_key_passphrase(pem: bytes) -> bytes | None:
    encrypted = b"ENCRYPTED" in pem
    supplied = os.environ.get(SIGNING_KEY_PASSPHRASE_ENV)
    if supplied:
        return supplied.encode("utf-8")
    if not encrypted:
        return None
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass("Update signing-key passphrase (not echoed): ").encode("utf-8")
    raise SystemExit(
        "[orion-package] REFUSED: the signing key is encrypted; set "
        f"{SIGNING_KEY_PASSPHRASE_ENV} in the environment (never on the command line) or run "
        "interactively to be prompted")


def load_signing_key(key_path: Path, package_dir: Path | None = None):
    """Load a LOCAL Ed25519 private key PEM. The key must live OUTSIDE the package tree and
    outside the repo's shippable dirs — it is read, used, and never copied anywhere."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key_path = key_path.resolve()
    forbidden_package_dir = (package_dir or PACKAGE_DIR).resolve()
    if key_path == forbidden_package_dir or forbidden_package_dir in key_path.parents:
        raise SystemExit(f"REFUSED: signing key must not live inside the package dir: {key_path}")
    pem = key_path.read_bytes()
    try:
        key = load_pem_private_key(pem, password=_signing_key_passphrase(pem))
    except (ValueError, TypeError) as exc:
        # cryptography's messages name the condition, never the passphrase; keep it that way.
        raise SystemExit(
            "[orion-package] REFUSED: could not decrypt the signing key with the supplied "
            f"passphrase ({exc.__class__.__name__}: {exc})")
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit(f"REFUSED: {key_path} is not an Ed25519 private key")
    return key


def is_trusted_production_signing_key(key_path: Path,
                                      package_dir: Path | None = None) -> bool:
    """Return whether *key_path* IS the production key the shipped binaries trust.

    The non-raising twin of require_trusted_production_signing_key(): release gates use
    it to tell a production run from a test build, so a relaxation (dirty packer tree,
    unpinned packer commit) can be refused for the production key specifically.
    """
    expected = decode_ed25519_public_key(ED25519_PUBLIC_KEY_B64)
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = load_signing_key(key_path, package_dir=package_dir)
    actual = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return len(expected) == 32 and actual == expected


def require_trusted_production_signing_key(key_path: Path,
                                           package_dir: Path | None = None) -> None:
    """Ensure production output is signed by the key the native binaries trust."""
    if not is_trusted_production_signing_key(key_path, package_dir=package_dir):
        raise SystemExit(
            "[orion-package] REFUSED: --signing-key does not match the embedded "
            f"trusted Ed25519 key id {ED25519_KEY_ID}"
        )


def _validate_artifact_url(url: str) -> None:
    """CRIT-2 (local half): any URL we emit into the signed update manifest MUST be HTTPS.
    The server /api/update also enforces HTTPS + an owned-host allow-list before it stores a
    manifest (backend/lambda_function.py _artifact_url_allowed), so this is a defence-in-depth
    check that fails loud locally — a http:// typo never gets signed into an artifact here.
    Empty is allowed: the URL is optional (server-side signing / a later --artifact-url pass)."""
    if url and not url.lower().startswith("https://"):
        raise SystemExit(
            f"[orion-package] REFUSED: artifact_url must be https:// (CRIT-2), got {url!r}"
        )


def build_update_manifest(
    *,
    version: str,
    artifact_sha256: str,
    artifact_url: str = "",
    min_version: str = "",
    mandatory: bool = False,
    allow_rollback: bool = False,
    release_notes: str = "",
    published_at: str | None = None,
    public_key_id: str = ED25519_KEY_ID,
) -> dict:
    """The /api/update manifest for this artifact — same field names and shapes that
    handle_get_update serves and the native updater consumes. Unsigned until
    sign_update_manifest() (local key) or POST /api/update (server SSM key) signs it."""
    _validate_artifact_url(artifact_url)
    return {
        "ok": True,
        "latest_version": version,
        # Default floor never blocks an existing client; raising it is an explicit
        # operator decision (--min-version), not a packaging side effect.
        "minimum_supported_version": min_version or "0.0.0",
        "artifact_url": artifact_url,
        "sha256": artifact_sha256,
        "signature_alg": "ed25519",
        "signature": "",
        "public_key_id": public_key_id,
        "public_key_b64": "",
        "release_notes": release_notes,
        "published_at": published_at or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mandatory": bool(mandatory),
        "allow_rollback": bool(allow_rollback),
    }


def sign_update_manifest(manifest: dict, key_path: Path) -> dict:
    """Sign the canonical payload exactly like the server does: Ed25519 over
    canonical_signing_payload(), base64url without padding. Embeds the matching raw
    public key (b64) so verify_release_integrity.py / the native client can verify offline."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = load_signing_key(key_path)
    signed = dict(manifest)
    signed["signature"] = base64.urlsafe_b64encode(key.sign(canonical_signing_payload(manifest))).rstrip(b"=").decode("ascii")
    signed["signature_alg"] = "ed25519"
    signed["public_key_b64"] = base64.b64encode(
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    return signed


def require_production_build() -> None:
    """Refuse to package a build that still carries developer escape hatches.

    The shipped binaries must be configured with -DORION_PRODUCTION=ON, which
    compiles out the local-dev license path, update-gate skip, debug UI, and
    makes the release-integrity manifest unconditionally required.
    """
    cache = BUILD_DIR.parent / "CMakeCache.txt"
    if not cache.exists():
        raise SystemExit(f"CMakeCache.txt not found next to the build output: {cache}")
    text = cache.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith("ORION_PRODUCTION:") and line.split("=", 1)[-1].strip().upper() == "ON":
            return
    raise SystemExit(
        "[orion-package] REFUSED: build was not configured with -DORION_PRODUCTION=ON.\n"
        "  Reconfigure + rebuild first:\n"
        "    cmake -S native_orion -B native_orion/build -DORION_PRODUCTION=ON\n"
        "    cmake --build native_orion/build --config Release\n"
        "  (or pass --allow-dev-build for a local, never-shipped test package)"
    )


# [2026-09-21 Codex r3 F1] Every key id a production updater may embed, with the SHA-256 of
# its 32 raw public-key bytes. Rotation adds the new pair here in the SAME change that
# embeds it (docs/UPDATER_CLIENT.md "Key rotation"); an id that is not listed, or a listed
# id over different bytes, refuses the package.
def _key_fingerprint_b64(encoded_b64: str) -> str:
    return hashlib.sha256(base64.b64decode(encoded_b64)).hexdigest()


APPROVED_UPDATE_KEYS: dict[str, str] = {
    ED25519_KEY_ID: _key_fingerprint_b64(ED25519_PUBLIC_KEY_B64),
}

# sha256 of the OrionUpdater.exe bytes that produced the accepted attestation; the staged
# copy must match it, or a binary swapped between attestation and staging would ship.
_ATTESTED_UPDATER_SHA256: str | None = None


def check_updater_build_profile(profile: dict, approved: dict[str, str]) -> None:
    """Pure policy over the attestation JSON (unit-tested)."""
    if profile.get("profile") != "production":
        raise SystemExit(
            f"[orion-package] REFUSED: OrionUpdater.exe reports profile={profile.get('profile')!r}; "
            "rebuild the Release tree with -DORION_PRODUCTION=ON (the binary, not just the cache)")
    if profile.get("local_manifest_allowed") is not False:
        raise SystemExit("[orion-package] REFUSED: OrionUpdater.exe still honours --manifest-file")
    keys = profile.get("embedded_keys")
    if not isinstance(keys, list) or not keys:
        raise SystemExit("[orion-package] REFUSED: OrionUpdater.exe attests no embedded keys with fingerprints")
    seen: dict[str, str] = {}
    for entry in keys:
        key_id = str(entry.get("id", ""))
        fingerprint = str(entry.get("sha256", "")).lower()
        if key_id not in approved:
            raise SystemExit(
                f"[orion-package] REFUSED: OrionUpdater.exe embeds an unapproved update-signing key id {key_id!r}")
        if fingerprint != approved[key_id]:
            raise SystemExit(
                f"[orion-package] REFUSED: OrionUpdater.exe embeds key id {key_id!r} over DIFFERENT key bytes "
                "than the pinned production public key")
        seen[key_id] = fingerprint
    if ED25519_KEY_ID not in seen:
        raise SystemExit(
            f"[orion-package] REFUSED: OrionUpdater.exe does not embed the production key id {ED25519_KEY_ID!r}")


def require_staged_updater_matches_attestation(staging_dir: Path) -> None:
    """[Codex r3 F1] The bytes that attested must be the bytes that ship."""
    if _ATTESTED_UPDATER_SHA256 is None:
        return
    staged = staging_dir / "OrionUpdater.exe"
    if not staged.is_file():
        raise SystemExit("[orion-package] REFUSED: staged package has no OrionUpdater.exe")
    actual = sha256_file(staged)
    if actual != _ATTESTED_UPDATER_SHA256:
        raise SystemExit(
            "[orion-package] REFUSED: staged OrionUpdater.exe differs from the binary that attested "
            f"(attested {_ATTESTED_UPDATER_SHA256[:16]}…, staged {actual[:16]}…)")
    print(f"[orion-package] staged OrionUpdater.exe matches attestation ({actual[:16]}…)")


def require_production_updater_binary() -> None:
    """[2026-09-21 Codex F1] The CMake cache says how the tree was CONFIGURED; it says
    nothing about the bytes in Release/. A stale or dev-built OrionUpdater.exe next to a
    production cache would ship with its external key overrides and --manifest-file
    intact. Ask the binary itself: `--build-profile <json>` is written by the compiled
    code and reports the profile and the embedded key ids."""
    global _ATTESTED_UPDATER_SHA256
    exe = BUILD_DIR / "OrionUpdater.exe"
    if not exe.exists():
        raise SystemExit(f"[orion-package] REFUSED: {exe} is missing")
    before = sha256_file(exe)
    with tempfile.TemporaryDirectory(prefix="orion-build-profile-") as tmp:
        out = Path(tmp) / "build_profile.json"
        env = dict(os.environ)
        # --build-profile exits before any window is constructed.  Forcing Qt's
        # offscreen platform on Windows can hang during QApplication startup when
        # the plugin is absent or quarantined, which turns a valid production
        # attestation into a 60-second packaging failure.  Let the native Windows
        # platform initialize normally and explicitly discard a caller-provided
        # override so this security gate is deterministic.
        env.pop("QT_QPA_PLATFORM", None)
        try:
            result = subprocess.run([str(exe), "--build-profile", str(out)], env=env,
                                    capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SystemExit(f"[orion-package] REFUSED: could not read the updater's build profile: {exc}")
        if result.returncode != 0 or not out.exists():
            raise SystemExit(
                f"[orion-package] REFUSED: OrionUpdater.exe --build-profile exited {result.returncode}; "
                "an updater that cannot attest its build profile is not a production updater")
        try:
            profile = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"[orion-package] REFUSED: unreadable build profile: {exc}")
    check_updater_build_profile(profile, APPROVED_UPDATE_KEYS)
    after = sha256_file(exe)
    if after != before:
        raise SystemExit("[orion-package] REFUSED: OrionUpdater.exe changed while it was being attested")
    _ATTESTED_UPDATER_SHA256 = after
    print(f"[orion-package] updater build profile OK: production, keys={sorted(APPROVED_UPDATE_KEYS)} "
          f"sha256={after[:16]}…")


def require_pinned_production_libcrypto() -> None:
    """Bind packaged libcrypto bytes to SecurityCore's compile-time trust pin."""
    cache = BUILD_DIR.parent / "CMakeCache.txt"
    if not cache.is_file():
        raise SystemExit(f"[orion-package] CMake cache missing for libcrypto pin: {cache}")
    pinned = ""
    for line in cache.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("ORION_LIBCRYPTO_SHA256:"):
            pinned = line.split("=", 1)[-1].strip().lower()
            break
    if len(pinned) != 64 or any(ch not in "0123456789abcdef" for ch in pinned):
        raise SystemExit(
            "[orion-package] REFUSED: production build has no valid "
            "ORION_LIBCRYPTO_SHA256 compile-time pin"
        )
    runtime = BUILD_DIR / "libcrypto-3-x64.dll"
    if not runtime.is_file():
        raise SystemExit(f"[orion-package] pinned libcrypto runtime missing: {runtime}")
    actual = sha256_file(runtime)
    if actual != pinned:
        raise SystemExit(
            "[orion-package] REFUSED: staged libcrypto-3-x64.dll does not match "
            f"SecurityCore's build pin (expected {pinned}, got {actual})"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true",
                        help="Deprecated: forbidden content is now ALWAYS a hard failure.")
    parser.add_argument("--allow-dev-build", action="store_true",
                        help="Package a non-production build (local testing only; never ship).")
    parser.add_argument("--dev-sidecar-dist", default="",
                        help="DEV ONLY: alternate compiled sidecar .dist directory. Requires "
                             "--allow-dev-build; production always uses the canonical build output.")
    parser.add_argument("--build-dir", default=None,
                        help="Build output to package (default native_orion/build/Release). "
                             "Strict release packaging points this at the production tree "
                             "(native_orion/build_prod/Release).")
    parser.add_argument("--version", default=None,
                        help="Release version (default: PROJECT_VERSION from native_orion/CMakeLists.txt).")
    parser.add_argument("--min-version", default="",
                        help="minimum_supported_version for the update manifest (default 0.0.0 = block nobody).")
    parser.add_argument("--artifact-url", default="",
                        help="Public download URL for the zip artifact, embedded+signed in the update manifest.")
    parser.add_argument("--mandatory", action="store_true", help="Mark the update mandatory in the manifest.")
    parser.add_argument("--allow-rollback", action="store_true", help="Allow downgrades in the manifest.")
    parser.add_argument("--release-notes", default="", help="Release notes for the update manifest.")
    parser.add_argument("--signing-key", default=os.environ.get(SIGNING_KEY_ENV, ""),
                        help=f"Path to a LOCAL Ed25519 private key PEM (or set {SIGNING_KEY_ENV}). "
                             "Signs the exact release_manifest.json bytes and the update manifest. "
                             "Required for production packaging.")
    parser.add_argument("--skip-archive", action="store_true",
                        help="Skip the zip artifact + update manifest (package dir only).")
    parser.add_argument("--skip-verify", action="store_true",
                        help="Skip the final verify_release_integrity.py run (NOT for shipped builds).")
    parser.add_argument("--customer", action=argparse.BooleanOptionalAction, default=True,
                        help="Customer manifest: omits CUSTOMER_EXCLUDED_FILES (internal admin/staff "
                             "binaries) from release_manifest.json. Must stay in sync with the "
                             "Excludes= list in installer/orion.iss. Pass --no-customer for an "
                             "internal build whose manifest covers every shipped binary.")
    args = parser.parse_args()

    if args.build_dir:
        global BUILD_DIR
        BUILD_DIR = Path(args.build_dir).resolve()

    if args.skip_verify and not args.allow_dev_build:
        raise SystemExit(
            "[orion-package] REFUSED: --skip-verify is development-only; "
            "production output must pass the final integrity verifier"
        )

    signing_key_path = Path(args.signing_key).expanduser().resolve() if args.signing_key else None
    if not args.allow_dev_build:
        require_production_build()
        if signing_key_path is None:
            raise SystemExit(
                f"[orion-package] REFUSED: production packaging requires --signing-key "
                f"or {SIGNING_KEY_ENV} for release_manifest.sig"
            )
        require_trusted_production_signing_key(signing_key_path)
        require_pinned_production_libcrypto()
        require_production_updater_binary()
    elif signing_key_path is not None:
        # Fail before archiving/copying when a developer supplied an unusable key.
        load_signing_key(signing_key_path)

    selected_sidecar_dist = sidecar_dist_dir(
        allow_dev_build=args.allow_dev_build,
        dev_override=args.dev_sidecar_dist or None,
    )

    version = args.version or default_version()
    # Reject late metadata errors before any published path can change.
    if not args.skip_archive:
        _validate_artifact_url(args.artifact_url)
    staging_dir = create_package_staging_dir(PACKAGE_DIR)
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    release_stage = Path(tempfile.mkdtemp(prefix=".orion-release-stage-", dir=RELEASE_DIR))
    update_manifest_path: Path | None = None
    artifact_path: Path | None = None
    try:
        copy_runtime(selected_sidecar_dist, package_dir=staging_dir)
        if not args.allow_dev_build:
            require_staged_updater_matches_attestation(staging_dir)
        if args.customer:
            strip_customer_excluded_files(staging_dir)
        write_release_manifest(
            staging_dir,
            version,
            signing_key=signing_key_path,
            allow_unsigned_dev=args.allow_dev_build and signing_key_path is None,
            customer=args.customer,
        )

        # Hard ship gates run against private staging. No rejected tree can
        # replace the last published package or masquerade as a complete build.
        findings = scan_forbidden(staging_dir)
        if findings:
            print("[orion-package] FAIL: forbidden files in staged package:")
            for finding in findings:
                print(f"  - {finding}")
            return 2
        secrets = scan_secret_content(staging_dir)
        if secrets:
            print("[orion-package] FAIL: secret-looking content in staged package:")
            for finding in secrets:
                print(f"  - {finding}")
            return 3

        if not args.skip_verify:
            result = subprocess.run([
                sys.executable,
                str(ROOT / "tools" / "verify_release_integrity.py"),
                "--package",
                str(staging_dir),
            ])
            if result.returncode != 0:
                print("[orion-package] FAIL: staged integrity gate failed; published package unchanged.")
                return 4

        staged_manifest: Path | None = None
        staged_artifact: Path | None = None
        if not args.skip_archive:
            staged_artifact, digest = create_archive(staging_dir, version, release_stage)
            manifest = build_update_manifest(
                version=version,
                artifact_sha256=digest,
                artifact_url=args.artifact_url,
                min_version=args.min_version,
                mandatory=args.mandatory,
                allow_rollback=args.allow_rollback,
                release_notes=args.release_notes,
            )
            if signing_key_path is not None:
                manifest = sign_update_manifest(manifest, signing_key_path)
                staged_manifest = release_stage / "update_manifest.json"
                update_manifest_path = RELEASE_DIR / "update_manifest.json"
            else:
                staged_manifest = release_stage / "update_manifest.unsigned.json"
                update_manifest_path = RELEASE_DIR / "update_manifest.unsigned.json"
            staged_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            artifact_path = RELEASE_DIR / staged_artifact.name
            # Even development output must bind the staged ZIP bytes to the staged
            # manifest before publication. The signed path additionally runs the
            # exact independent release verifier below.
            if sha256_file(staged_artifact) != manifest["sha256"]:
                raise RuntimeError("Staged archive/manifest SHA-256 mismatch")

        if not args.skip_verify:
            cmd = [sys.executable, str(ROOT / "tools" / "verify_release_integrity.py"),
                   "--package", str(staging_dir)]
            if staged_manifest is not None and staged_manifest.name == "update_manifest.json":
                cmd += ["--update-manifest", str(staged_manifest),
                        "--artifact", str(staged_artifact)]
            result = subprocess.run(cmd)
            if result.returncode != 0:
                print("[orion-package] FAIL: staged release unit integrity gate failed; published output unchanged.")
                return 4

        if args.skip_archive:
            publish_staged_package(
                staging_dir, PACKAGE_DIR, ARCHIVE_DIR,
                require_signature=not args.allow_dev_build,
            )
        else:
            publish_release_unit(
                staging_dir, staged_artifact, staged_manifest,
                PACKAGE_DIR, artifact_path, update_manifest_path,
                require_signature=not args.allow_dev_build,
                archive_dir=ARCHIVE_DIR,
            )
    finally:
        # A successful publish moves the tree, so this is then a no-op. Every
        # failure removes only the derived private staging directory.
        if staging_dir.exists():
            cleanup_package_staging_dir(staging_dir, PACKAGE_DIR)
        shutil.rmtree(release_stage, ignore_errors=True)

    print(f"[orion-package] wrote {PACKAGE_DIR}")
    print(f"[orion-package] manifest files: {len(json.loads((PACKAGE_DIR / RELEASE_MANIFEST_NAME).read_text(encoding='utf-8'))['files'])}")
    print(f"[orion-package] manifest signature: "
          f"{'signed' if (PACKAGE_DIR / RELEASE_MANIFEST_SIG_NAME).is_file() else 'UNSIGNED DEV OUTPUT'}")

    if artifact_path is not None:
        print(f"[orion-package] artifact: {artifact_path} sha256={sha256_file(artifact_path)}")
        print(f"[orion-package] update manifest: {update_manifest_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
